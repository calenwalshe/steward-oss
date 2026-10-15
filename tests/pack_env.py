"""The empty-machine fixture behind the pack test and the live package test (Phase 19, plan 09, G20 tiers 2 and 3).

Everything the installed `steward` can see lives under one tmp directory: HOME, the XDG directories, STEWARD_HOME,
the config and data directories, npm's cache and prefix, and a PATH made of three tmp directories (fake `claude`
and `codex`, the tmp prefix's bin, and symlinks to a short list of real tools). Nothing of the test process's own
HOME, XDG_*, STEWARD_* or PATH is inherited, so nothing here can reach the live system. Installs are `--offline`
from a local tarball; nothing is published and no registry is contacted.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from npm_helpers import NODE, NPM, REPO, load_module

PACK_TIMEOUT_S = 180
RUN_TIMEOUT_S = 60

# Real tools the panes and scripts need. Deliberately absent: uv, systemctl, tg, cloudflared, curl.
_TOOLS = (
    "bash", "sh", "env", "cat", "sleep", "date", "tput", "stty", "head", "tail", "grep", "sed", "awk", "cut", "tr",
    "wc", "sort", "uniq", "ls", "mkdir", "rm", "cp", "mv", "dirname", "basename", "readlink", "uname", "id",
    "hostname", "ps", "pgrep", "find", "xargs", "tee", "printf", "touch", "chmod", "ln", "mktemp", "sha256sum",
    "seq", "clear", "tty", "kill", "expr", "test", "true", "false",
)


def find_python() -> str | None:
    """A real (non-venv) interpreter >= 3.12: the running one resolved through its symlinks when it is new enough,
    else the first `python3.14`..`python3.12` on PATH. A venv's `python` loses its `pyvenv.cfg` when linked elsewhere."""
    candidates = []
    if sys.version_info >= (3, 12):
        candidates.append(os.path.realpath(sys.executable))
    for name in ("python3.14", "python3.13", "python3.12", "python3"):
        found = shutil.which(name)
        if found:
            candidates.append(os.path.realpath(found))
    for path in candidates:
        res = subprocess.run([path, "-c", "import sys; print(sys.version_info >= (3, 12))"], capture_output=True,
                             text=True)
        if res.returncode == 0 and res.stdout.strip() == "True":
            return path
    return None


def skip_reason() -> str | None:
    """Why the real-process tests cannot run here (None: they can)."""
    if not NODE or not NPM:
        return "node and npm are required for the pack test"
    if find_python() is None:
        return "Python 3.12 or newer is required for the pack test"
    if shutil.which("git") is None:
        return "git is required for the pack test"
    return None


def package_name() -> str:
    return load_module("pack_config").FULL_NAME


def bump(version: str) -> str:
    """The next patch version (`0.1.0` -> `0.1.1`); the upgrade tests build a second tarball with it."""
    major, minor, patch = (int(p) for p in version.split("+")[0].split(".")[:3])
    return f"{major}.{minor}.{patch + 1}"


def pyproject_version() -> str:
    return tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]


# -- the empty machine --------------------------------------------------------------------------------------------


def real_bins(realbin: Path, *, python: str | None = None, herdr: bool = True, git: bool = True,
              with_python: bool = True) -> Path:
    """Symlink node, npm, git, a real Python >= 3.12 (as `python3` and `python3.12`), herdr and the short tool list
    into `realbin`. `with_python=False` leaves Python out (the missing-Python message test)."""
    realbin.mkdir(parents=True, exist_ok=True)

    def link(name: str, target: str | None) -> None:
        if target and not (realbin / name).exists():
            (realbin / name).symlink_to(target)

    link("node", NODE and os.path.realpath(NODE))
    link("npm", NPM and os.path.realpath(NPM))
    if git:
        link("git", shutil.which("git"))
    if herdr:
        link("herdr", shutil.which("herdr"))
    if with_python:
        py = python or find_python()
        link("python3", py)
        link("python3.12", py)
    for name in _TOOLS:
        link(name, shutil.which(name))
    return realbin


_FAKE_CLAUDE = """#!/bin/sh
# Fixed strings only: no account data, no network (tests/pack_env.py).
case "$1" in
  --version) echo "fake claude 0.0.0"; exit 0 ;;
  auth) echo '{{"loggedIn": {logged_in}}}'; exit 0 ;;
esac
echo "fake claude ready"
while read -r _line; do :; done
"""

_FAKE_CODEX = """#!/bin/sh
case "$1" in
  --version) echo "fake codex 0.0.0"; exit 0 ;;
  login) exit {login_exit} ;;
esac
exit 0
"""


def write_fake_bins(fakebin: Path, *, claude_logged_in: bool = True, codex: str = "ok") -> Path:
    """Executable shell fakes in `fakebin`. `claude`: `--version`, `auth status --json`; anything else prints
    `fake claude ready` and waits on the terminal. `codex` is `ok`, `logged_out` or `absent` (no file written)."""
    fakebin.mkdir(parents=True, exist_ok=True)
    claude = fakebin / "claude"
    claude.write_text(_FAKE_CLAUDE.format(logged_in="true" if claude_logged_in else "false"))
    claude.chmod(0o755)
    if codex != "absent":
        fake = fakebin / "codex"
        fake.write_text(_FAKE_CODEX.format(login_exit=0 if codex == "ok" else 1))
        fake.chmod(0o755)
    return fakebin


def clean_env(tmp: Path) -> dict[str, str]:
    """The empty-machine environment under `tmp`; its directories are created (except the STEWARD_* ones). PATH = fakebin, prefix/bin, realbin;
    no HERDR_* variable, no STEWARD_OWNER_NAME. The npm_config_* values keep npm offline-quiet and inside tmp."""
    tmp = Path(tmp)
    dirs = {
        "HOME": tmp / "home",
        "XDG_CONFIG_HOME": tmp / "xdg" / "config",
        "XDG_DATA_HOME": tmp / "xdg" / "data",
        "XDG_CACHE_HOME": tmp / "xdg" / "cache",
        "STEWARD_HOME": tmp / "steward_home",
        "STEWARD_CONFIG_DIR": tmp / "steward_config",
        "STEWARD_DATA_DIR": tmp / "steward_data",
        "npm_config_cache": tmp / "npm_cache",
        "npm_config_prefix": tmp / "prefix",
    }
    # The STEWARD_* directories are NOT created: a new machine has none, and `steward init` must create them (0700).
    for name, path in dirs.items():
        if not name.startswith("STEWARD_"):
            path.mkdir(parents=True, exist_ok=True)
    fakebin, realbin = tmp / "fakebin", tmp / "realbin"
    fakebin.mkdir(exist_ok=True)
    realbin.mkdir(exist_ok=True)
    env = {name: str(path) for name, path in dirs.items()}
    env["PATH"] = os.pathsep.join([str(fakebin), str(dirs["npm_config_prefix"] / "bin"), str(realbin)])
    env["npm_config_userconfig"] = str(tmp / "npmrc")
    env["npm_config_globalconfig"] = str(tmp / "npmrc-global")
    env.update({"npm_config_update_notifier": "false", "npm_config_fund": "false", "npm_config_audit": "false"})
    env["LANG"] = "C.UTF-8"
    return env


def machine(tmp: Path, *, claude_logged_in: bool = True, codex: str = "ok", with_python: bool = True,
            herdr: bool = True) -> dict[str, str]:
    """`clean_env` plus the fakes and the real-tool links: a ready empty machine."""
    env = clean_env(tmp)
    write_fake_bins(Path(tmp) / "fakebin", claude_logged_in=claude_logged_in, codex=codex)
    real_bins(Path(tmp) / "realbin", with_python=with_python, herdr=herdr)
    return env


def prefix_of(env: dict[str, str]) -> Path:
    return Path(env["npm_config_prefix"])


def install_root(env: dict[str, str]) -> Path:
    """Where the global install puts the package: `<prefix>/lib/node_modules/<scope>/<name>`."""
    return prefix_of(env) / "lib" / "node_modules" / package_name()


# -- build, install, run -------------------------------------------------------------------------------------------


def build_tarball(tmp: Path, version: str | None = None) -> Path:
    """Run the real build with `--pack` (the pre-pack audit included) into `<tmp>/dist-<v>` and return the tarball.
    npm runs under a clean environment of its own, so its cache and logs stay in tmp."""
    tmp = Path(tmp)
    version = version or pyproject_version()
    env = machine(tmp / f"build-{version}")
    cmd = [sys.executable, str(REPO / "packaging" / "npm" / "steward_pack_build.py"), "--out",
           str(tmp / f"dist-{version}"), "--pack"]
    if version != pyproject_version():
        cmd += ["--version", version]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=PACK_TIMEOUT_S, cwd=str(REPO))
    assert res.returncode == 0, f"pack failed:\n{res.stdout}\n{res.stderr}"
    tarballs = sorted(tmp.glob(f"*-{version}.tgz"))
    assert len(tarballs) == 1, f"expected one tarball for {version}, found {tarballs}"
    return tarballs[0]


def install_tarball(env: dict[str, str], tarball: Path) -> subprocess.CompletedProcess:
    """`npm install -g --prefix <tmp prefix> --offline ... --ignore-scripts <tarball>`; it fails closed instead of
    reaching for the network (`--offline`, a tmp cache, a zero-dependency package)."""
    cmd = [NPM, "install", "-g", "--prefix", str(prefix_of(env)), "--offline", "--no-audit", "--no-fund",
           "--ignore-scripts", str(tarball)]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=PACK_TIMEOUT_S)


def run_installed(env: dict[str, str], *args: str, cwd: Path | str | None = None, input: str | None = "",
                  timeout: int = RUN_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Run `<prefix>/bin/steward` (the installed npm shim) under `env` only."""
    return subprocess.run([str(prefix_of(env) / "bin" / "steward"), *args], env=env, cwd=cwd, input=input,
                          capture_output=True, text=True, timeout=timeout)


def tree(path: Path | str) -> list[str]:
    """Every file and directory under `path` (relative, sorted); [] when it does not exist."""
    root = Path(path)
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")) if root.exists() else []


def state_dirs(env: dict[str, str]) -> dict[str, Path]:
    """The directories an installed `steward` must not have written to unless it was asked to set something up."""
    return {name: Path(env[name]) for name in (
        "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "STEWARD_HOME", "STEWARD_CONFIG_DIR", "STEWARD_DATA_DIR")}


def export_prefix(env: dict[str, str], names: tuple[str, ...] | None = None) -> str:
    """`export NAME=value && ...` for the clean environment: a typed pane command starts with it, because a Herdr
    pane inherits the Herdr server's environment, not the test's."""
    keys = names or tuple(sorted(env))
    return "".join(f"export {k}={shlex.quote(env[k])} && " for k in keys if k in env)


def export_lines(env: dict[str, str]) -> str:
    """The same exports as `export_prefix`, one per line: a file a pane shell sources with `. <file>`."""
    return "".join(f"export {k}={shlex.quote(v)}\n" for k, v in sorted(env.items()))


def wait_for(predicate, *, timeout: float = 60.0, poll: float = 0.25, what: str = "condition"):
    """Poll `predicate` until it returns a truthy value; fail with `what` after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(poll)
    raise AssertionError(f"{what} did not happen within {timeout}s (last: {last!r})")


def load_json(text: str):
    return json.loads(text)


# -- a throwaway Herdr session that runs the INSTALLED package (tier 3) ----------------------------------------------

_pane_counter = 0


def _ancestors() -> set[int]:
    found, pid = set(), os.getpid()
    while pid > 1 and pid not in found:
        found.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return found


def leftover_processes(fragment: str = "steward-test-") -> list[str]:
    """`pgrep -af <fragment>` lines, minus this process, its ancestors (a shell whose command line merely mentions
    the fragment) and the pgrep itself. Empty means no throwaway session left a process behind."""
    res = subprocess.run(["pgrep", "-af", fragment], capture_output=True, text=True)
    mine = _ancestors()
    out = []
    for line in res.stdout.splitlines():
        pid_text, _, rest = line.partition(" ")
        if not pid_text.isdigit() or int(pid_text) in mine or rest.startswith("pgrep"):
            continue
        out.append(line)
    return out


def sha256_tree(path: Path | str) -> dict[str, str]:
    """`relative path -> sha256` for every file under `path` (a re-run must leave every one of them equal)."""
    import hashlib

    root = Path(path)
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _herdr_session_class():
    from herdr_session import HerdrTestSession

    class PackSession(HerdrTestSession):
        """A `steward-test-*` session whose server (and so every pane's first shell) starts from a SMALL environment:
        `/bin/sh` as the shell (no rc file, so no secrets from the host's profile), the empty machine's PATH, and a
        real HOME only for Herdr's own session directory. `herdr --session <name> ...` is the CLI the helpers use."""

        def __init__(self, env: dict[str, str], name: str):
            super().__init__(name=name)
            self.machine_env = env
            self.pane_env_file = Path(env["HOME"]).parent / "pane-env.sh"
            self.pane_env_file.write_text(export_lines(env))
            self.runs = 0

        def _clean_env(self) -> dict:
            return {"HOME": str(Path.home()), "PATH": self.machine_env["PATH"], "TERM": "xterm-256color",
                    "LANG": "C.UTF-8", "SHELL": "/bin/sh", "USER": "steward-test"}

        # -- the herdr CLI against this session only
        def cli(self, *args: str, timeout: int = 20) -> str:
            res = subprocess.run(["herdr", "--session", self.name, *args], capture_output=True, text=True,
                                 env=self._clean_env(), timeout=timeout)
            if res.returncode != 0:
                raise AssertionError(f"herdr {' '.join(args)} failed ({res.returncode}): {res.stderr.strip()}")
            return res.stdout

        def tabs(self) -> list[dict]:
            return json.loads(self.cli("tab", "list"))["result"]["tabs"]

        def pane_list(self) -> list[dict]:
            return json.loads(self.cli("pane", "list"))["result"]["panes"]

        def tab_labelled(self, label: str) -> dict | None:
            return next((t for t in self.tabs() if t.get("label") == label), None)

        def pane_labelled(self, label: str, tab_id: str | None = None) -> dict | None:
            return next((p for p in self.pane_list()
                         if p.get("label") == label and (tab_id is None or p.get("tab_id") == tab_id)), None)

        def ids(self) -> dict[str, list[str]]:
            return {"tabs": sorted(t["tab_id"] for t in self.tabs()), "panes": sorted(p["pane_id"] for p in self.pane_list())}

        def argvs(self, pane_id: str) -> list[list[str]]:
            info = json.loads(self.cli("pane", "process-info", "--pane", pane_id))["result"]["process_info"]
            return [list(p.get("argv") or []) for p in info.get("foreground_processes") or []]

        def pids(self, pane_id: str) -> list[int]:
            """The pane's foreground process GROUP id as a one-item list: stable while one command runs (the status
            script's short-lived children come and go), different after the command is re-run."""
            info = json.loads(self.cli("pane", "process-info", "--pane", pane_id))["result"]["process_info"]
            group = info.get("foreground_process_group_id")
            return [int(group)] if group else []

        def read(self, pane_id: str, lines: int = 400, source: str = "recent-unwrapped") -> str:
            return self.cli("pane", "read", pane_id, "--source", source, "--lines", str(lines))

        def visible(self, pane_id: str) -> str:
            return self.read(pane_id, lines=60, source="visible")

        def send_key(self, pane_id: str, key: str) -> None:
            self.api.call("pane.send_input", {"pane_id": pane_id, "keys": [key]})

        # -- typed commands
        def run(self, pane_id: str, command: str, *, timeout: float = 90.0) -> tuple[int, str]:
            """Type `. <env file> && <command>; echo EXIT-<n>=$?` into the pane, wait for the marker, and return
            `(exit code, the pane text)`. Each run uses its own marker, so an earlier run's cannot satisfy this one."""
            import re

            self.runs += 1
            marker = f"EXIT-{self.runs}"
            self.run_in_pane(pane_id, f". {self.pane_env_file} && {command}; echo \"{marker}=$?\"")
            pattern = re.compile(rf"(?m)^{marker}=(\d+)\s*$")
            text = wait_for(lambda: (lambda t: t if pattern.search(t) else None)(self.read(pane_id)),
                            timeout=timeout, what=f"`{command}` to finish in {pane_id}")
            return int(pattern.search(text).group(1)), text

    return PackSession


def new_session(env: dict[str, str], tag: str):
    """A started `steward-test-pk<pid>-<tag>` session (the caller tears it down in a `finally`)."""
    global _pane_counter
    _pane_counter += 1
    session = _herdr_session_class()(env, f"steward-test-pk{os.getpid()}-{tag}{_pane_counter}")
    session.start()
    return session


def session_exists(name: str) -> bool:
    res = subprocess.run(["herdr", "session", "list"], capture_output=True, text=True)
    return any(line.split()[:1] == [name] for line in res.stdout.splitlines())
