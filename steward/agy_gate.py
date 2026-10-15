"""The agy hook gate: every agy tool call is relayed to Steward as a typed permission request and blocks until Steward
answers (docs/fleet-evidence/agy-permission-audit.md, "Hook-gate route"; observed against agy 1.2.16, 2026-10-04).

Headless agy auto-denies any tool a static `permissions.allow` rule does not cover, and a PreToolUse hook can only
narrow what that rule allows. So the gate is the pair:
- a broad allow rule, in a Steward-owned agy HOME used only by the Steward-launched process (agy reads its config
  from `$HOME/.gemini`; the user's own `~/.gemini` is only ever read, for the sign-in token);
- a PreToolUse hook (matcher "*") in that home's `config/hooks.json`, which asks Steward over a unix socket and prints
  allow only when Steward approved.

Measured agy behaviour this design depends on:
- the hook's deny is honoured; its allow lets the tool run under the broad rule;
- a hook that exits non-zero, is killed, exceeds its timeout, or prints malformed JSON, `{}` or `{"decision":""}`
  blocks the tool, BUT blank output and `{"decision":"ask"}` let it run (the allow rule answers). So the hook command
  is a fixed `case` that can print only the exact allow line or a deny line;
- agy re-reads its hook files from disk at every turn (`~/.gemini/config/hooks.json`,
  `~/.gemini/antigravity-cli/hooks.json`, and the workspace's `.agents/`, `.agent/`, `_agents/`), and a hook of the
  same name with `"enabled": false` disables the gate's. A string check on tool arguments cannot stop an obfuscated
  write, so agy runs in a bubblewrap sandbox where every file agy reads as configuration (the whole gate root, a
  read-only per-spawn snapshot of its config, the workspace customization roots, the user's own `~/.gemini`) is
  read-only and only agy's state dir and the job worktree stay writable. Without `bwrap` the gate refuses to start;
- a subagent's tool calls never reach the hook, so the tools that start another agent loop are denied outright;
- a hook command that runs a script file can be swapped mid-run by an approved write. So the relay client is inline
  in hooks.json and runs on the root-owned system interpreter; the hook name is also random per spawn, and a tool
  call that names a gate path is denied without asking (defence in depth).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import socket
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

# What the Steward-owned agy home pre-approves: everything, so agy's own permission manager never auto-denies. Every
# call still passes the hook first, and only Steward's approval lets the hook print allow.
ALLOW_RULES = ("command(*)", "read_file(*)", "read_file(/)", "write_file(*)", "write_file(/)", "mcp(*)",
               "read_url(*)", "execute_url(*)")
ALLOW_LINE = '{"decision":"allow"}'
DENY_FALLBACK = '{"decision":"deny","reason":"steward gate: no valid answer from Steward"}'
TOKEN_FILE = Path(".gemini/antigravity-cli/antigravity-oauth-token")
# Workspace dirs agy reads hooks or config from (strace of agy 1.2.16): an empty read-only mount covers each.
WORKSPACE_ROOTS = (".agents", ".agent", "_agents", "_agent", ".gemini")
# What agy reads as configuration inside its (otherwise writable) state dir: replaced by read-only snapshot copies.
CLI_LOCKED_FILES = {"settings.json": None, "hooks.json": "{}\n", "keybindings.json": "{}\n"}
CLI_LOCKED_DIRS = ("secplugins", "plugins", "plugin_data", "skills")
SYSTEM_PYTHON = "/usr/bin/python3"
# A subagent's tool calls run WITHOUT the PreToolUse hook (measured: its `run_command` ran unasked under the allow
# rule), so tools that start another agent loop are denied outright: never relayed, never approvable.
EXCLUDED_TOOLS = frozenset({"invoke_subagent", "define_subagent", "browser_subagent", "schedule", "run_workflow"})
ONBOARDING_FILE = Path(".gemini/antigravity-cli/cache/onboarding.json")
_MAX_REQUEST = 4_000_000

# The relay client agy runs for every tool call. Inline (never a file the agent could rewrite); prints the exact
# allow line only for an explicit approval; every failure prints a deny.
_CLIENT = r"""
import json, socket, sys
try:
    data = sys.stdin.buffer.read(4000000)
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(float(sys.argv[2]))
    s.connect(sys.argv[1])
    s.sendall(data.replace(b"\n", b" ") + b"\n")
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    ans = json.loads(buf)
    if ans.get("decision") == "allow":
        print('{"decision":"allow"}')
    else:
        print(json.dumps({"decision": "deny", "reason": str(ans.get("reason") or "denied by Steward")[:500]}))
except Exception as exc:
    print(json.dumps({"decision": "deny", "reason": "steward gate unreachable: " + type(exc).__name__}))
"""


def hook_command(sock: str, wait_s: float, python: str | None = None, inner: str | None = None) -> str:
    """The `sh -c` command agy runs per tool call. Whatever the inner relay prints (or if it crashes, hangs past
    `wait_s`, prints nothing or prints junk), the outer `case` emits either the exact allow line or a deny line, so
    agy never sees the blank or `ask` output it would treat as "allowed by the rule". `inner` replaces the relay
    client (tests use it to prove that normalisation)."""
    relay = inner or " ".join(shlex.quote(x) for x in (python or sys.executable, "-I", "-S", "-c", _CLIENT, sock,
                                                       f"{wait_s:.0f}"))
    return (f"r=$({relay}); case \"$r\" in {shlex.quote(ALLOW_LINE)}) printf '%s\\n' {shlex.quote(ALLOW_LINE)};; "
            f"'{{\"decision\": \"deny\"'*) printf '%s\\n' \"$r\";; "
            f"*) printf '%s\\n' {shlex.quote(DENY_FALLBACK)};; esac")


def permission_view(name: str, args: dict) -> tuple[str, dict]:
    """An agy tool call in the tool vocabulary `steward.permissions.decide` judges (as the codex/pi adapters do), so
    one authority policy covers every runtime. Tools without a Claude equivalent keep an `agy:` name and their
    arguments, so the request is still shown and decided, never silently passed."""
    a = args if isinstance(args, dict) else {}
    if name == "run_command":
        return "Bash", {"command": str(a.get("CommandLine") or ""), "cwd": str(a.get("Cwd") or "")}
    if name == "send_command_input":
        return "Bash", {"command": str(a.get("Input") or "")}
    if name == "write_to_file":
        return "Write", {"file_path": str(a.get("TargetFile") or ""), "content": str(a.get("CodeContent") or "")[:2000]}
    if name in ("replace_file_content", "multi_replace_file_content", "sed_file"):
        return "Edit", {"file_path": str(a.get("TargetFile") or a.get("File") or "")}
    if name == "notebook_edit":
        return "NotebookEdit", {"notebook_path": str(a.get("TargetFile") or a.get("NotebookPath") or "")}
    if name == "view_file":
        return "Read", {"file_path": str(a.get("AbsolutePath") or "")}
    if name == "read_url_content":
        return "WebFetch", {"url": str(a.get("Url") or "")}
    if name == "search_web":
        return "WebSearch", {"query": str(a.get("query") or a.get("Query") or "")}
    if name == "call_mcp_tool":
        return (f"mcp__{a.get('ServerName') or '?'}__{a.get('ToolName') or '?'}",
                a.get("Arguments") if isinstance(a.get("Arguments"), dict) else {})
    view = {k: v for k, v in a.items() if k not in ("toolAction", "toolSummary")}
    text = json.dumps(view, default=str)
    return f"agy:{name}", (view if len(text) <= 4000 else {"args": text[:4000]})


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


@dataclass
class GateRequest:
    request_id: str
    tool: str
    input: dict
    raw: dict
    answered: threading.Event = field(default_factory=threading.Event)
    approve: bool = False
    reason: str = ""


class AgyGate:
    """One Steward-owned gate per adapter: the agy HOME it materialises at every spawn, and the socket the hook asks.
    `pending()` hands each new request to the adapter once; `answer()` releases the blocked hook. Anything not
    approved in time, and everything still open at `close()`, is denied."""

    def __init__(self, root: Path | str, *, token_home: Path | str | None = None, answer_timeout_s: float = 3600.0,
                 python: str | None = None):
        self.root = Path(root).resolve()
        self.home = self.root / "home"
        self.token_home = Path(token_home) if token_home else Path.home()
        self.answer_timeout_s = answer_timeout_s
        self.python = python or (SYSTEM_PYTHON if Path(SYSTEM_PYTHON).is_file() else sys.executable)
        self._snap: Path | None = None
        self._made: list[Path] = []
        self._snaps: list[Path] = []
        self.sock_dir = Path(tempfile.mkdtemp(prefix="sg-"))
        os.chmod(self.sock_dir, 0o700)
        self.sock_path = str(self.sock_dir / "s")
        self.hook_name = ""
        self._lock = threading.Lock()
        self._new: list[GateRequest] = []
        self._open: dict[str, GateRequest] = {}
        self.hard_denied: list[str] = []
        self._seq = 0
        self._server: socket.socket | None = None
        self._closed = False

    # -- the agy home -----------------------------------------------------------------------------------------------
    def materialize(self) -> dict[str, str]:
        """(Re)write the gate's config for the next agy process and return its env overrides. Conversation state under
        the home survives (reattach needs it). The config agy reads is a fresh read-only snapshot per spawn (new hook
        name, this gate's socket), mounted over the home by `sandbox_argv`."""
        src = self.token_home / TOKEN_FILE
        if not src.is_file():
            raise FileNotFoundError(f"agy is not signed in (no {src}); sign in to agy once, then retry")
        gem = self.home / ".gemini"
        cli, cfg = gem / "antigravity-cli", gem / "config"
        for d in (cli / "cache", cfg, *(cli / x for x in CLI_LOCKED_DIRS)):
            d.mkdir(parents=True, exist_ok=True)
        for p in (self.root, self.home, gem, cli, cfg):
            os.chmod(p, 0o700)
        for name in CLI_LOCKED_FILES:  # mount points; outside the sandbox they hold nothing that allows anything
            (cli / name).touch(exist_ok=True)
        tok = self.home / TOKEN_FILE
        if not tok.exists():  # a refreshed token in the gate home is newer than the user's: keep it
            shutil.copyfile(src, tok)
        os.chmod(tok, 0o600)
        onboarding = self.token_home / ONBOARDING_FILE
        if onboarding.is_file() and not (self.home / ONBOARDING_FILE).exists():
            shutil.copyfile(onboarding, self.home / ONBOARDING_FILE)

        self.hook_name = "steward-gate-" + secrets.token_hex(16)
        snap = self.root / "spawn" / secrets.token_hex(8)  # removed by close(), once no process mounts it
        (snap / "config/projects").mkdir(parents=True)
        cmd = hook_command(self.sock_path, self.answer_timeout_s + 5, python=self.python)
        (snap / "config/hooks.json").write_text(json.dumps({self.hook_name: {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": cmd, "timeout": int(self.answer_timeout_s + 15)}]}]}}, indent=1) + "\n")
        (snap / "config/mcp_config.json").write_text("")
        (snap / "config/.migrated").write_text("")
        (snap / "config/projects/default-cli-project.json").write_text(json.dumps(
            {"id": "default-cli-project", "name": "CLI Project", "projectResources": {}}, indent=2) + "\n")
        (snap / "cli").mkdir()
        for name, text in CLI_LOCKED_FILES.items():
            (snap / "cli" / name).write_text(text if text is not None else json.dumps(
                {"permissions": {"allow": list(ALLOW_RULES)}}, indent=1) + "\n")
        for d in CLI_LOCKED_DIRS:
            (snap / "cli" / d).mkdir()
        self._snap = snap
        self._snaps.append(snap)
        env = {"HOME": str(self.home)}
        gitconfig = self.token_home / ".gitconfig"
        if "GIT_CONFIG_GLOBAL" not in os.environ and gitconfig.is_file():
            env["GIT_CONFIG_GLOBAL"] = str(gitconfig)  # the worker's commits keep the user's identity
        return env

    def sandbox_argv(self, worktree: Path | str) -> list[str]:
        """The bubblewrap prefix for the agy process: the host as it is, except that everything agy reads as gate
        configuration is read-only for agy and every tool it runs (no nested user namespaces, so no remounting)."""
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise FileNotFoundError("bwrap (bubblewrap) is required to run agy behind the Steward hook gate")
        if self._snap is None:
            raise RuntimeError("materialize() first")
        wt = Path(worktree).resolve()
        gem, snap = self.home / ".gemini", self._snap
        cli = gem / "antigravity-cli"
        argv = [bwrap, "--bind", "/", "/", "--ro-bind", str(self.root), str(self.root),
                "--bind", str(self.home), str(self.home), "--ro-bind", str(gem), str(gem), "--bind", str(cli), str(cli)]
        for name in (*CLI_LOCKED_FILES, *CLI_LOCKED_DIRS):
            argv += ["--ro-bind", str(snap / "cli" / name), str(cli / name)]
        argv += ["--ro-bind", str(snap / "config"), str(gem / "config"),
                 "--ro-bind", str(self.sock_dir), str(self.sock_dir)]
        user_gemini = (self.token_home / ".gemini").resolve()
        if user_gemini.is_dir():
            argv += ["--ro-bind", str(user_gemini), str(user_gemini)]
        for name in WORKSPACE_ROOTS:
            p = wt / name
            if p.exists() and not p.is_dir():
                raise RuntimeError(f"{p} is not a directory; the gate cannot cover it")
            if not p.exists():
                p.mkdir()
                self._made.append(p)
            argv += ["--tmpfs", str(p), "--remount-ro", str(p)]
        # a fresh /dev: the host's devpts refuses new PTYs from inside the user namespace, and agy runs commands in one
        return argv + ["--dev", "/dev", "--unshare-user", "--disable-userns", "--die-with-parent", "--chdir", str(wt),
                       "--"]

    # -- the relay ----------------------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._server is not None:
            return
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(self.sock_path)
        os.chmod(self.sock_path, 0o600)
        srv.listen(16)
        self._server = srv
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _guard(self, args, artifacts: str | None = None) -> str | None:
        """A tool call that names the gate itself (its home config, token or socket, or any `.gemini` dir) is denied
        without asking: defence in depth on top of the read-only sandbox. agy's own artifact dir for the conversation
        (it lives in the gate home) stays usable, but never with a `..` that could climb out of it."""
        protected = (str(self.root), str(self.sock_dir), ".gemini")
        for s in _strings(args):
            if artifacts and artifacts.startswith(str(self.home)) and ".." not in s:
                s = s.replace(artifacts, "")
            for p in protected:
                if p and p in s:
                    return p
        return None

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(30)
            buf = b""
            try:
                while not buf.endswith(b"\n") and len(buf) < _MAX_REQUEST:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                raw = json.loads(buf)
                call = raw.get("toolCall") or {}
                name, args = str(call.get("name") or ""), call.get("args") or {}
            except (OSError, ValueError, AttributeError):
                self._reply(conn, False, "steward gate: unreadable request")
                return
            if name in EXCLUDED_TOOLS:
                self.hard_denied.append(name)
                self._reply(conn, False, f"steward gate: {name} starts another agent loop whose tool calls bypass "
                                         "the hook; Steward never allows it")
                return
            hit = self._guard(args, raw.get("artifactDirectoryPath") if isinstance(raw, dict) else None)
            if hit is not None:
                self.hard_denied.append(name)
                self._reply(conn, False, f"steward gate: {name} names the gate's own files ({hit}); denied")
                return
            with self._lock:
                if self._closed:
                    self._reply(conn, False, "steward gate: closed")
                    return
                self._seq += 1
                tool, view = permission_view(name, args)
                req = GateRequest(request_id=f"agy-{self._seq}-{secrets.token_hex(4)}", tool=tool, input=view, raw=raw)
                self._new.append(req)
                self._open[req.request_id] = req
            req.answered.wait(self.answer_timeout_s)
            with self._lock:  # once popped, a late answer() is refused, so the reply below is what Steward sees
                self._open.pop(req.request_id, None)
                ok = req.answered.is_set()
            if not ok:
                self._reply(conn, False, f"steward gate: no answer from Steward within {self.answer_timeout_s:.0f}s")
            else:
                self._reply(conn, req.approve, req.reason or ("approved by Steward" if req.approve else
                                                              "denied by Steward"))

    @staticmethod
    def _reply(conn: socket.socket, approve: bool, reason: str) -> None:
        line = ALLOW_LINE if approve else json.dumps({"decision": "deny", "reason": reason[:500]})
        try:
            conn.sendall(line.encode() + b"\n")
        except OSError:
            pass

    def pending(self) -> list[GateRequest]:
        with self._lock:
            new, self._new = self._new, []
        return new

    def answer(self, request_id: str, approve: bool, reason: str | None = None) -> bool:
        with self._lock:
            req = self._open.get(str(request_id))
            if req is None or req.answered.is_set():
                return False
            req.approve, req.reason = bool(approve), reason or ""
            req.answered.set()
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for req in self._open.values():
                if not req.answered.is_set():
                    req.approve, req.reason = False, "steward gate: the lane stopped"
                    req.answered.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        shutil.rmtree(self.sock_dir, ignore_errors=True)
        for p in self._made:  # the empty mount points the sandbox needed in the worktree
            try:
                p.rmdir()
            except OSError:
                pass
        self._made = []
        for snap in self._snaps:
            shutil.rmtree(snap, ignore_errors=True)
        self._snaps = []


def gate_root(home: Path | str, *parts: str) -> Path:
    """Where a lane's gate lives: under Steward's own state dir, outside every job worktree. Stable per lane, so a
    reattach finds the conversation agy stored in the gate home."""
    return Path(home).joinpath("agy-gate", *(re.sub(r"[^A-Za-z0-9_.-]", "_", p) for p in parts if p))
