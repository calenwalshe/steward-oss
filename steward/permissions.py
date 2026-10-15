"""Default-allow permission policy for worker tool calls (SP-36, NB-02).

`decide()` is the runner's single seam for "does this tool call need a
human, or does it just happen": everything is allowed by default —
read-only network (curl/wget GET, WebFetch, WebSearch), package installs,
scripts, long-running commands, redirects and writes *inside* the job
worktree — except the outbound/irreversible list SP-36 carves out: deploy/
publish/push/release, messaging, payments/credentials, sudo/systemctl, and
any write (or redirect, or `rm`) that reaches outside the worktree.

This is a heuristic, not a sandbox: `decide()` looks at the shell text (or
the tool's structured input) the same way a human skimming the approval
card would, not a real shell parser. When it can't tell, it asks — SP-36
narrows the human-approval surface, it does not remove it.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_TMP_ROOT = Path("/tmp")
# Redirecting to these discards or echoes output; it writes nothing anywhere.
_DEV_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str


def _ok(reason: str) -> Decision:
    return Decision(True, reason)


def _ask(reason: str) -> Decision:
    return Decision(False, reason)


# -- Bash: deny-list (SP-36's outbound/irreversible list) -------------------

# Every rule below is matched (re.match) against the simple commands `command_words` extracts:
# the executable and its arguments, never heredoc bodies, `-c` strings of interpreters
# (python/node/perl...) or quoted prose. `bash -c "..."`, `sh -c`, `eval`, `$(...)` and
# sudo/env/xargs wrappers ARE commands, so what they run is parsed and judged too.
_COMMAND_WORD_DENY: list[tuple[re.Pattern, str]] = [
    (re.compile(r"git\s+(?:-\S+\s+(?:[^-\s]\S*\s+)?)*push\b"), "git push"),
    (re.compile(r"gh\s+release\b"), "gh release"),
    (re.compile(r"gh\s+pr\s+merge\b"), "gh pr merge"),
    (re.compile(r"(npm|cargo|yarn|pnpm)\s+publish\b"), "package publish"),
    (re.compile(r"twine\s+upload\b"), "twine upload (PyPI publish)"),
    (re.compile(r"docker\s+push\b"), "docker push"),
    (re.compile(r"(fly|flyctl|vercel|netlify|wrangler)\s+(deploy|publish)\b"), "deploy"),
    (re.compile(r"(mail|sendmail|mutt|msmtp)(?=\s|$)"), "mail"),
    (re.compile(r"tg(?=\s|$)"), "tg (telegram)"),
    (re.compile(r"terraform\s+apply\b"), "terraform apply"),
    (re.compile(r"kubectl\s+apply\b"), "kubectl apply"),
    (re.compile(r"(ssh|scp|rsync|sftp|ssh-add|ssh-keygen|ssh-copy-id|sshpass)(?=\s|$)"), "remote transfer (ssh/scp/rsync)"),
    (re.compile(r"sudo(?=\s|$)"), "sudo"),
    (re.compile(r"systemctl(?=\s|$)"), "systemctl"),
]

_CURL_WGET_HEAD_RE = re.compile(r"(curl|wget)(?=\s|$)")
_CURL_WGET_WRITE_FLAGS_RE = re.compile(
    r"((?<!\S)-X\s*(?i:POST|PUT|PATCH|DELETE)\b|(?i:--request(=|\s+)(POST|PUT|PATCH|DELETE)\b|"
    r"--data\b|--data-raw\b|--data-binary\b|--data-urlencode\b|--post-data\b|--post-file\b|--form\b|--upload-file\b)|"
    r"(?<!\S)-d|(?<!\S)-F|(?<!\S)-T\s)",
)
# Short flags are case-sensitive (`-D` dumps headers, `-f` fails fast: both read-only); `-X` takes its method in any case,
# and long options keep matching case-insensitively.


def is_remote_write(head: str) -> bool:
    """A `curl`/`wget` command (as returned by `command_words`) with a write method / body."""
    return bool(_CURL_WGET_HEAD_RE.match(head) and _CURL_WGET_WRITE_FLAGS_RE.search(head))


_SENSITIVE_PATH_RE = re.compile(
    r"((^|[\s/'\"=])\.api-keys(?![\w.-])|(^|[\s/])keys\.env(\s|$|['\"])|~/\.ssh(?:/|$|\s)|/\.ssh(?:/|$)|[\w~./-]+\.pem(?:\s|$|['\"])|(^|/)\.env(\s|$|['\"]))"
)

def sensitive_path_in(argvs: list[list[str]]) -> str | None:
    """A credential path named by a real argument word. A word containing whitespace is quoted
    prose (a commit message, an echo string), not a path, and is skipped."""
    for argv in argvs:
        for w in argv[1:]:
            if not any(c.isspace() for c in w):
                m = _SENSITIVE_PATH_RE.search(w)
                if m:
                    return m.group(0).strip()
    return None


_SUBSTITUTION_RE = re.compile(r"\$\(|`")
_REDIRECT_RE = re.compile(r"(?:\d)?(>>?|<)\s*([^\s&|;]+)")


def _resolve(token: str, worktree: Path) -> Path | None:
    token = token.strip("'\"")
    if not token or token in ("&", "|", ";"):
        return None
    try:
        path = Path(token).expanduser()
    except ValueError:
        return None
    if not path.is_absolute():
        path = worktree / path
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def _outside_worktree(path: Path, worktree: Path) -> bool:
    if path == worktree or worktree in path.parents:
        return False
    if path == _TMP_ROOT or _TMP_ROOT in path.parents:
        return False
    return True


# Commands whose non-flag arguments are all write targets.
_WRITE_ALL_ARGS = frozenset({"rm", "rmdir", "touch", "mkdir", "tee", "truncate", "shred", "unlink"})
# Commands whose LAST non-flag argument (or `-t DIR`) is the write target.
_WRITE_DEST_ARG = frozenset({"cp", "mv", "install", "ln", "rsync"})
# Leading words that just wrap the real command.
_WRAPPERS = frozenset({"sudo", "env", "nice", "nohup", "time", "command", "exec", "xargs"})


def _segments(command: str) -> list[list[str]]:
    """The simple commands in a shell line, split on `&&`, `||`, `;`, `|` and `&`."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return []
    segments, current = [], []
    for tok in tokens:
        if tok and set(tok) <= set(";&|"):
            if current:
                segments.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


_SHELL_KEYWORDS = frozenset({"then", "do", "else", "elif", "if", "while", "until", "!", "{", "(", "time"})


_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
# Wrapper options that take a value (`sudo -u bob`, `xargs -I {}`, `env -u X`): the value is not the command.
_WRAPPER_VALUE_FLAGS = frozenset({"-u", "-g", "-n", "-C", "-I", "-P", "-L", "-a", "-d", "-S"})
_MAX_DEPTH = 4


def _shell_script(argv: list[str]) -> str | None:
    """The command string of `bash -c '...'` / `sh -lc '...'` / `eval '...'`, else None."""
    if argv[0] == "eval":
        return " ".join(argv[1:]) or None
    if argv[0] not in _SHELLS:
        return None
    for i, w in enumerate(argv[1:], 1):
        if w.startswith("-") and not w.startswith("--") and "c" in w[1:]:
            return argv[i + 1] if i + 1 < len(argv) else None
        if not w.startswith("-"):
            return None  # `bash script.sh`: a file, not a string
    return None


def _heredoc_shell_bodies(command: str) -> list[str]:
    """Bodies of heredocs whose reader is a shell (`bash <<EOF`, `cat <<EOF | sh`): they run, so
    they are commands. Bodies fed to anything else (cat, python, node...) are data."""
    lines, out, i = command.split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        m = _HEREDOC_RE.search(line)
        i += 1
        if not m:
            continue
        body = []
        while i < len(lines) and lines[i].strip() != m.group(2):
            body.append(lines[i])
            i += 1
        i += 1
        try:
            argvs = _commands(line[: m.start()] + line[m.end():], _depth=_MAX_DEPTH)
        except Exception:  # noqa: BLE001
            argvs = []
        if any(a[0] in _SHELLS and _shell_script(a) is None and not [w for w in a[1:] if not w.startswith("-")] for a in argvs):
            out.append("\n".join(body))
    return out


def _commands(command: str, _depth: int = 0) -> list[list[str]]:
    """Each simple command in `command` as a word list whose FIRST word is the executable
    (basename). Wrappers (sudo/env/xargs/...), env assignments, shell keywords and `$(`/backtick
    substitutions yield extra entries starting at the wrapped command; `bash -c`/`eval` strings
    and shell heredocs are parsed recursively. Other heredoc bodies and interpreter `-c` strings
    (python -c, node -e ...) are data and never appear as commands."""
    out: list[list[str]] = []
    for words in _segments(_strip_heredocs(command)):
        starts = [0] + [i for i, w in enumerate(words) if i and (w.startswith("$(") or w.startswith("`"))]
        for st in starts:
            seg = list(words[st:])
            seg[0] = seg[0].lstrip("$(`")
            seg = [w for w in seg if w]
            wrapped, skip = False, False
            for i, w in enumerate(seg):
                if skip:
                    skip = False
                    continue
                if w in _SHELL_KEYWORDS or ("=" in w and not w.startswith("-") and "/" not in w.split("=")[0]):
                    continue
                if wrapped and w.startswith("-"):
                    skip = w in _WRAPPER_VALUE_FLAGS
                    continue
                out.append([Path(w).name, *seg[i + 1:]])
                if Path(w).name not in _WRAPPERS:
                    break
                wrapped = True  # a wrapper: every later word may be the wrapped command (xargs -I {} ssh)
    if _depth < _MAX_DEPTH:
        for argv in list(out):
            script = _shell_script(argv)
            if script:
                out.extend(_commands(script, _depth + 1))
        for body in _heredoc_shell_bodies(command):
            out.extend(_commands(body, _depth + 1))
    return out


def command_words(command: str) -> list[str]:
    """`_commands` with each command's words space-joined, for the anchored rule tables."""
    return [" ".join(argv) for argv in _commands(command)]


def _outside_write(words: list[str], worktree: Path, cwd: Path | None = None) -> str | None:
    """A reason to ask if this simple command writes outside the worktree, else None."""
    while words and (words[0] in _WRAPPERS or "=" in words[0] and not words[0].startswith("-")):
        words = words[1:]
    if not words:
        return None
    cmd = Path(words[0]).name
    args = [w for w in words[1:] if not w.startswith("-")]

    def outside(token: str) -> bool:
        target = _resolve(token, cwd or worktree)
        return target is not None and _outside_worktree(target, worktree)

    if cmd in _WRITE_ALL_ARGS:
        for a in args:
            if outside(a):
                return f"{cmd} targets a path outside the worktree: {a}"
    if cmd in _WRITE_DEST_ARG:
        dest = None
        for i, w in enumerate(words[1:], 1):
            if w in ("-t", "--target-directory") and i + 1 < len(words):
                dest = words[i + 1]
            elif w.startswith("--target-directory="):
                dest = w.split("=", 1)[1]
        dest = dest or (args[-1] if len(args) >= 2 else None)
        if dest and not (cmd == "rsync" and ":" in dest) and outside(dest):
            return f"{cmd} writes to a path outside the worktree: {dest}"
        if cmd == "mv" and len(args) >= 2 and any(outside(a) for a in args[:-1]):
            return f"mv moves a file from outside the worktree: {args[0]}"
    if cmd == "dd":
        for w in words[1:]:
            if w.startswith("of=") and outside(w[3:]):
                return f"dd writes to a path outside the worktree: {w[3:]}"
    if cmd in ("sed", "perl") and any(w == "-i" or w.startswith("-i") for w in words[1:]):
        for a in args[1:] if cmd == "sed" else args:
            if a.startswith("/") and outside(a):
                return f"{cmd} -i edits a file outside the worktree: {a}"
    return None


_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _strip_heredocs(command: str, data_only: bool = False) -> str:
    """Drop heredoc bodies (the lines up to the closing delimiter): they are data fed to a
    command, not shell to be checked — prose like `| a | b |` must not look like a pipeline."""
    lines, out, i = command.split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = _HEREDOC_RE.search(line)
        i += 1
        if m and data_only and not re.search(r"(^|[;&|]\s*)(cat|tee)\b", line[: m.start()]):
            m = None  # an interpreter heredoc: its body runs, so keep it for checking
        if m:
            delim = m.group(2)
            while i < len(lines) and lines[i].strip() != delim:
                i += 1
            i += 1  # skip the closing delimiter
    return "\n".join(out)


def _cd(args: list[str], cwd: Path) -> Path:
    target = next((a for a in args if not a.startswith("-")), None)
    if target is None or target == "~":
        return Path.home()
    resolved = _resolve(target, cwd)
    return resolved if resolved is not None else cwd


def _outside_redirect(words: list[str], worktree: Path, cwd: Path) -> str | None:
    """`> path`, `>> path`, `2> path`, `&> path` targets (input `<` redirects are reads)."""
    for i, w in enumerate(words):
        m = re.match(r"^(?:\d|&)?>>?(.*)$", w)
        if not m:
            continue
        raw = m.group(1) or (words[i + 1] if i + 1 < len(words) else "")
        if not raw or raw.startswith("&") or raw in _DEV_SINKS:
            continue
        if _SUBSTITUTION_RE.search(raw):
            # Can't verify a substituted target stays in the worktree — ask rather than guess.
            return f"redirect target uses command substitution: {raw}"
        target = _resolve(raw, cwd)
        if target is not None and _outside_worktree(target, worktree):
            return f"redirect targets a path outside the worktree: {raw}"
    return None


def _decide_bash(command: str, worktree: Path) -> Decision:
    # Rules judge COMMANDS (see `_commands`): heredoc bodies fed to cat/python/node, interpreter
    # `-c` strings and quoted prose such as `curl` in a test string never trip them.
    argvs = _commands(command)
    heads = [" ".join(a) for a in argvs]
    for pattern, reason in _COMMAND_WORD_DENY:
        if any(pattern.match(h) for h in heads):
            return _ask(reason)

    if any(is_remote_write(h) for h in heads):
        return _ask("curl/wget with a write method to a remote host")

    sensitive = sensitive_path_in(argvs)
    if sensitive:
        return _ask(f"reads a credential path: {sensitive}")

    # Write targets: any command in the chain that writes, moves, deletes or
    # links a path outside the worktree (and outside /tmp) still needs a human
    # — everything else (relative paths, /tmp, paths inside the worktree) is
    # allowed by default. Each `&&`/`||`/`;`/`|` segment is checked, relative
    # paths resolve against the directory the chain has `cd`-ed into (reads
    # anywhere are fine), and heredoc bodies are data, not commands.
    body = _strip_heredocs(command)
    cwd = worktree
    for words in _segments(body):
        if words and words[0] == "cd":
            cwd = _cd(words[1:], cwd)
            continue
        reason = _outside_write(words, worktree, cwd) or _outside_redirect(words, worktree, cwd)
        if reason:
            return _ask(reason)

    return _ok("default allow (SP-36)")


# -- Write/Edit/NotebookEdit: tool paths outside the worktree ---------------

_PATH_INPUT_KEYS = ("file_path", "path", "notebook_path")



UNVERIFIABLE = "<unverifiable>"  # a write target Steward cannot resolve (command substitution): never grant-covered


def command_hold_reason(head: str) -> str | None:
    """The deny-list reason one simple command (an entry of `command_words`) trips, else None. Outside-worktree
    writes and redirects are judged separately by `write_targets` (Plan 2: grants cover each piece separately)."""
    for pattern, reason in _COMMAND_WORD_DENY:
        if pattern.match(head):
            return reason
    if is_remote_write(head):
        return "curl/wget with a write method to a remote host"
    return None


def write_targets(command: str, worktree: Path | str) -> list[str]:
    """EVERY resolved path outside the worktree (and /tmp) that this command line writes, moves, deletes, links or
    redirects to — the judgement `_decide_bash` makes, as data, without stopping at the first hit, so a grant for
    one path can never cover a second, ungranted one in the same line. An unresolvable target is `UNVERIFIABLE`."""
    wt = Path(worktree).resolve()
    out: list[str] = []
    cwd = wt

    def add(token: str, base: Path) -> None:
        if _SUBSTITUTION_RE.search(token):
            out.append(UNVERIFIABLE)
            return
        target = _resolve(token, base)
        if target is not None and target != wt and not target.is_relative_to(wt):
            out.append(str(target))

    for words in _segments(_strip_heredocs(command)):
        if words and words[0] == "cd":
            cwd = _cd(words[1:], cwd)
            continue
        for i, w in enumerate(words):
            m = re.match(r"^(?:\d|&)?>>?(.*)$", w)
            if m:
                raw = m.group(1) or (words[i + 1] if i + 1 < len(words) else "")
                if raw and not raw.startswith("&") and raw not in _DEV_SINKS:
                    add(raw, cwd)
        ws = list(words)
        while ws and (ws[0] in _WRAPPERS or "=" in ws[0] and not ws[0].startswith("-")):
            ws = ws[1:]
        if not ws:
            continue
        cmd = Path(ws[0]).name
        args, skip = [], False
        for w in ws[1:]:
            if skip:
                skip = False
                continue
            if re.match(r"^(?:\d|&)?>>?", w) or w == "<":
                skip = not re.match(r"^(?:\d|&)?>>?.+$", w)
                continue
            if not w.startswith("-"):
                args.append(w)
        if cmd in _WRITE_ALL_ARGS:
            for a in args:
                add(a, cwd)
        elif cmd in _WRITE_DEST_ARG:
            dest = None
            for i, w in enumerate(ws[1:], 1):
                if w in ("-t", "--target-directory") and i + 1 < len(ws):
                    dest = ws[i + 1]
                elif w.startswith("--target-directory="):
                    dest = w.split("=", 1)[1]
            dest = dest or (args[-1] if len(args) >= 2 else None)
            if dest and not (cmd == "rsync" and ":" in dest):
                add(dest, cwd)
            if cmd == "mv":
                for a in args[:-1]:
                    add(a, cwd)
        elif cmd == "dd":
            for w in ws[1:]:
                if w.startswith("of="):
                    add(w[3:], cwd)
        elif cmd in ("sed", "perl") and any(w == "-i" or w.startswith("-i") for w in ws[1:]):
            for a in (args[1:] if cmd == "sed" else args):
                add(a, cwd)
    return list(dict.fromkeys(out))

def _extract_path(command_or_input: Any) -> str | None:
    if isinstance(command_or_input, dict):
        for key in _PATH_INPUT_KEYS:
            value = command_or_input.get(key)
            if value:
                return str(value)
        return None
    if isinstance(command_or_input, str):
        return command_or_input
    return None


# Steward's own READ-ONLY MCP tools (every `readOnlyHint` tool in `steward.mcp_server.build_tools`; a test
# keeps this list a subset of those). Explicit names, never a pattern: a new or write tool is never in it.
STEW_READONLY_TOOLS = frozenset({
    "list_jobs", "get_job", "get_current_step", "get_recent_events", "get_verification", "get_diff_summary",
    "explain_blocker", "explain_job", "get_supervisors", "get_watchers_feed", "get_tokens", "get_worker_activity",
    "get_decision", "preview_incorporate", "get_result", "get_questions",
})
_STEW_TOOL_RE = re.compile(r"(?:stew\.|mcp__stew__)([A-Za-z0-9_]+)$")


def stew_readonly_tool(tool: str) -> str | None:
    """The bare tool name when `tool` (`stew.x` as Codex shows it, or `mcp__stew__x`) is one of Steward's own
    read-only tools, else None. Write/outbound stew tools (steer_job, approve_action, ...) never match."""
    m = _STEW_TOOL_RE.match((tool or "").strip())
    return m.group(1) if m and m.group(1) in STEW_READONLY_TOOLS else None


def decide(tool: str, command_or_input: Any, worktree: Path | str) -> Decision:
    """`allow`/`ask` for one worker tool call (SP-36 default-allow).

    `tool` is the Claude/Codex tool name ("Bash", "Write", "Edit",
    "WebFetch", "WebSearch", ...); `command_or_input` is either the Bash
    command string or the tool's structured input dict (from a typed
    `permission_request` frame's `input` field). Anything not covered below
    (WebFetch, WebSearch, Read, Glob, Grep, an unrecognized future tool
    name...) is allowed by default — SP-36 narrows the human-approval
    surface to the outbound/irreversible list, not the other way around.
    """
    worktree_path = Path(worktree).resolve()

    if stew_readonly_tool(tool):
        return _ok("Steward read-only MCP tool")

    if tool.startswith("mcp__research__"):
        # Sanctioned read-only research (a job capability); the runner meters it
        # against the job's spend cap (`steward.research`).
        return _ok("research MCP (sanctioned, read-only)")

    if tool == "Bash":
        command = command_or_input if isinstance(command_or_input, str) else (command_or_input or {}).get("command", "")
        return _decide_bash(command or "", worktree_path)

    if tool in ("Write", "Edit", "NotebookEdit"):
        raw_path = _extract_path(command_or_input)
        if raw_path:
            target = _resolve(raw_path, worktree_path)
            if target is not None and _outside_worktree(target, worktree_path):
                return _ask(f"{tool} path outside the worktree: {raw_path}")
        return _ok(f"{tool} inside worktree")

    return _ok("default allow (SP-36)")
