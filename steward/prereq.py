"""The prerequisite model behind `steward doctor` and the intro page (Phase 19, G9/G10).

`detect` returns one `Check` per item and never raises: each check is its own small function and a failure
becomes `check failed: <ExceptionType>` on that row while the others still report. Every external dependency
(environment, runner, `which`, home, config dir, Python version) is a parameter, so nothing here calls
`subprocess` directly, creates a directory or opens the store.

Required: a Herdr pane, Herdr >= TESTED_HERDR, Python >= 3.12, git, the `claude` CLI logged in. Everything else
is optional and degrades to a NAMED off state. Only the `loggedIn` boolean of `claude auth status --json` is read;
nothing else from that output is kept, printed or serialised.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from steward import keys
from steward.runner_exec import Runner

TESTED_HERDR = "0.9.1"
MIN_PYTHON = (3, 12)
TIMEOUT_S = 5
WIDTH = 76

REQUIRED, OPTIONAL = "required", "optional"
OK, MISSING, OFF = "ok", "missing", "off"

# Fix ids the intro may apply locally (G9); everything else is command text for the user to run.
LOCAL_FIXES = ("uv_python", "keys_env", "config_dirs")


@dataclass(frozen=True)
class Fix:
    kind: str  # "local" (an id the intro can apply) or "command" (text for the user)
    text: str
    id: str = ""


@dataclass(frozen=True)
class Check:
    id: str
    label: str
    level: str  # REQUIRED | OPTIONAL
    status: str  # OK | MISSING | OFF
    detail: str
    fix: Fix | None = None


def _ver_tuple(text: str) -> tuple[int, ...] | None:
    hit = re.search(r"(\d+(?:\.\d+)+)", text or "")
    return tuple(int(p) for p in hit.group(1).split(".")) if hit else None


def _ok(id_: str, label: str, level: str, detail: str) -> Check:
    return Check(id_, label, level, OK, detail)


def _bad(id_: str, label: str, level: str, detail: str, fix: Fix | None = None) -> Check:
    return Check(id_, label, level, MISSING if level == REQUIRED else OFF, detail, fix)


def _cmd(text: str) -> Fix:
    return Fix("command", text)


# -- required ---------------------------------------------------------------------------------------------------------


def _herdr_pane(ctx) -> Check:
    env = ctx["env"]
    if env.get("HERDR_ENV") == "1" and env.get("HERDR_SOCKET_PATH"):
        return _ok("herdr_pane", "Herdr pane", REQUIRED, "inside a pane")
    return _bad("herdr_pane", "Herdr pane", REQUIRED, "no pane", _cmd("run steward inside a Herdr pane"))


def _herdr_version(ctx) -> Check:
    fix = _cmd(f"install Herdr {TESTED_HERDR} or newer")
    if not ctx["which"]("herdr"):
        return _bad("herdr_version", "Herdr", REQUIRED, "herdr not found", fix)
    res = ctx["runner"].run(["herdr", "--version"], ".", timeout=TIMEOUT_S)
    got = _ver_tuple(res.stdout)
    if res.exit_code != 0 or got is None:
        return _bad("herdr_version", "Herdr", REQUIRED, "version unknown", fix)
    text = ".".join(str(p) for p in got)
    if got < _ver_tuple(TESTED_HERDR):
        return _bad("herdr_version", "Herdr", REQUIRED, f"{text} (tested {TESTED_HERDR})", fix)
    return _ok("herdr_version", "Herdr", REQUIRED, text)


def _python(ctx) -> Check:
    got = tuple(ctx["python_version"])
    text = ".".join(str(p) for p in got[:3])
    if got >= MIN_PYTHON:
        return _ok("python", "Python", REQUIRED, text)
    if ctx["which"]("uv"):
        fix = Fix("local", "uv python install 3.12", "uv_python")
    else:
        fix = _cmd("install Python 3.12 or newer")
    return _bad("python", "Python", REQUIRED, f"{text} (need 3.12+)", fix)


def _git(ctx) -> Check:
    if not ctx["which"]("git"):
        return _bad("git", "git", REQUIRED, "git not found", _cmd("install git"))
    res = ctx["runner"].run(["git", "--version"], ".", timeout=TIMEOUT_S)
    got = _ver_tuple(res.stdout)
    return _ok("git", "git", REQUIRED, ".".join(str(p) for p in got) if got else "found")


def _claude(ctx) -> Check:
    if not ctx["which"]("claude"):
        return _bad("claude", "claude CLI", REQUIRED, "claude not found", _cmd("install the Claude Code CLI"))
    res = ctx["runner"].run(["claude", "auth", "status", "--json"], ".", timeout=TIMEOUT_S)
    data = json.loads(res.stdout or "")
    logged_in = data.get("loggedIn") is True if isinstance(data, dict) else False
    del data, res  # nothing but the boolean leaves this function
    if logged_in:
        return _ok("claude", "claude CLI", REQUIRED, "logged in")
    return _bad("claude", "claude CLI", REQUIRED, "not logged in", _cmd("claude auth login"))


# -- optional ---------------------------------------------------------------------------------------------------------


def _jev(ctx) -> Check:
    state = keys.status("TYPESAFE_API_KEY")
    if state.startswith("set"):
        return _ok("jev", "Jev observer", OPTIONAL, state)
    return _bad("jev", "Jev observer", OPTIONAL, "off (no key)",
                Fix("local", "add the key to keys.env", "keys_env"))


def _codex(ctx) -> Check:
    if not ctx["which"]("codex"):
        return _bad("codex", "codex", OPTIONAL, "off (no codex)", _cmd("install the Codex CLI"))
    if ctx.get("light"):  # light mode never spawns a process: found on PATH is all it can say
        return _ok("codex", "codex", OPTIONAL, "found")
    res = ctx["runner"].run(["codex", "login", "status"], ".", timeout=TIMEOUT_S)
    if res.exit_code == 0:
        return _ok("codex", "codex", OPTIONAL, "logged in")
    return _bad("codex", "codex", OPTIONAL, "off (not logged in)", _cmd("codex login"))


def _research(ctx) -> Check:
    path = Path(ctx["env"].get("STEWARD_CLAUDE_JSON") or Path(ctx["home"]) / ".claude.json")
    try:
        servers = json.loads(path.read_text()).get("mcpServers") or {}
    except (OSError, ValueError):
        servers = {}
    if isinstance(servers, dict) and isinstance(servers.get("research"), dict):
        return _ok("research", "research MCP", OPTIONAL, "configured")
    return _bad("research", "research MCP", OPTIONAL, "off (no research server)",
                _cmd("add a research MCP server"))


def _systemd(ctx) -> Check:
    off = "off (no systemd user manager)"
    if not ctx["which"]("systemctl"):
        return _bad("systemd", "systemd", OPTIONAL, off, _cmd("use Herdr panes"))
    if ctx.get("light"):  # light mode never spawns: a running user manager owns its private socket
        runtime = ctx["env"].get("XDG_RUNTIME_DIR")
        if runtime and (Path(runtime) / "systemd" / "private").exists():
            return _ok("systemd", "systemd", OPTIONAL, "user manager running")
        return _bad("systemd", "systemd", OPTIONAL, off, _cmd("use Herdr panes"))
    res = ctx["runner"].run(["systemctl", "--user", "is-system-running"], ".", timeout=TIMEOUT_S)
    if res.stdout.strip() in ("running", "degraded", "starting", "initializing"):
        return _ok("systemd", "systemd", OPTIONAL, "user manager running")
    return _bad("systemd", "systemd", OPTIONAL, off, _cmd("use Herdr panes"))


def _connector(ctx) -> Check:
    try:
        cfg = json.loads((Path(ctx["config_dir"]) / "mcp.json").read_text())
    except (OSError, ValueError):
        cfg = {}
    if isinstance(cfg, dict) and cfg.get("public_base_url"):
        return _ok("connector", "public connector", OPTIONAL, "configured")
    return _bad("connector", "public connector", OPTIONAL, "off (no public connector)",
                _cmd("steward mcp bootstrap"))


def _telegram(ctx) -> Check:
    if not ctx["which"]("tg"):
        return _bad("telegram", "telegram alerts", OPTIONAL, "off (no tg)", _cmd("install the tg command"))
    try:
        data = tomllib.loads((Path(ctx["config_dir"]) / "steward.toml").read_text())
    except (OSError, ValueError):
        data = {}
    alerts = data.get("alerts")
    if isinstance(alerts, dict) and alerts.get("telegram") is True:
        return _ok("telegram", "telegram alerts", OPTIONAL, "on")
    return _bad("telegram", "telegram alerts", OPTIONAL, "off (alerts off)", _cmd("set [alerts] telegram = true"))


def _plugins(ctx) -> Check:
    names = [Path(p).name.lower() for p in ctx["plugin_dirs"]()]
    skills = Path(ctx["env"].get("STEWARD_CLAUDE_DIR") or Path(ctx["home"]) / ".claude") / "skills"
    try:
        names += [p.name.lower() for p in skills.iterdir()]
    except OSError:
        pass
    found = sorted({w for w in ("gsd", "superpowers") if any(w in n for n in names)})
    if found:
        return _ok("plugins", "GSD / superpowers", OPTIONAL, ", ".join(found))
    return _bad("plugins", "GSD / superpowers", OPTIONAL, "off (workers run without them)",
                _cmd("install the plugins"))


def _cloudflared(ctx) -> Check:
    if ctx["which"]("cloudflared"):
        return _ok("cloudflared", "cloudflared", OPTIONAL, "found")
    return _bad("cloudflared", "cloudflared", OPTIONAL, "off (no cloudflared)", _cmd("install cloudflared"))


CHECKS: tuple[tuple[str, str, str, Callable], ...] = (
    ("herdr_pane", "Herdr pane", REQUIRED, _herdr_pane),
    ("herdr_version", "Herdr", REQUIRED, _herdr_version),
    ("python", "Python", REQUIRED, _python),
    ("git", "git", REQUIRED, _git),
    ("claude", "claude CLI", REQUIRED, _claude),
    ("jev", "Jev observer", OPTIONAL, _jev),
    ("codex", "codex", OPTIONAL, _codex),
    ("research", "research MCP", OPTIONAL, _research),
    ("systemd", "systemd", OPTIONAL, _systemd),
    ("connector", "public connector", OPTIONAL, _connector),
    ("telegram", "telegram alerts", OPTIONAL, _telegram),
    ("plugins", "GSD / superpowers", OPTIONAL, _plugins),
    ("cloudflared", "cloudflared", OPTIONAL, _cloudflared),
)


# The subset `detect(light=True)` runs: file and PATH lookups only (the status pane calls it every few seconds).
LIGHT_IDS = ("jev", "codex", "research", "systemd", "connector", "telegram", "plugins", "cloudflared")


def detect(
    runner: Runner | None = None,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    home: Path | str | None = None,
    config_dir: Path | str | None = None,
    python_version: tuple[int, ...] | None = None,
    plugin_dirs: Callable[[], list[str]] | None = None,
    light: bool = False,
) -> list[Check]:
    """Run every check. Read-only; a check that fails reports `check failed: <ExceptionType>` and the rest go on.
    `light=True` runs only LIGHT_IDS, never spawns a process (`runner` may be None) and skips the Herdr, git, Python
    and claude checks (systemd is answered from its socket file)."""
    env = os.environ if env is None else env
    if config_dir is None:
        from steward.store import resolve_config_dir

        config_dir = resolve_config_dir()
    if plugin_dirs is None:
        from steward.worktree import user_plugin_dirs as plugin_dirs
    ctx = {
        "env": env,
        "runner": runner,
        "which": which or shutil.which,
        "home": Path(home) if home is not None else Path.home(),
        "config_dir": Path(config_dir),
        "python_version": python_version or sys.version_info[:3],
        "plugin_dirs": plugin_dirs,
        "light": light,
    }
    out: list[Check] = []
    for id_, label, level, fn in CHECKS:
        if light and id_ not in LIGHT_IDS:
            continue
        try:
            out.append(fn(ctx))
        except Exception as exc:  # one failing probe never stops the others
            out.append(Check(id_, label, level, MISSING if level == REQUIRED else OFF,
                             f"check failed: {type(exc).__name__}"))
    return out


def required_missing(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.level == REQUIRED and c.status != OK]


def _mark(check: Check) -> str:
    if check.status == OK:
        return "✓"
    return "✗" if check.level == REQUIRED else "–"


def render_text(checks: list[Check]) -> str:
    """One checklist, required rows first; one line per row, at most 76 columns; the fix follows an arrow."""
    rows = sorted(checks, key=lambda c: c.level != REQUIRED)  # stable: required first, order kept
    lines = []
    for c in rows:
        line = f"{_mark(c)} {c.label:<18} {c.detail}"
        if c.fix is not None and c.status != OK:
            line += f" → {c.fix.text}"
        if len(line) > WIDTH:
            line = line[: WIDTH - 1] + "…"
        lines.append(line)
    return "\n".join(lines)


def to_json(checks: list[Check]) -> list[dict]:
    return [
        {
            "id": c.id,
            "level": c.level,
            "status": c.status,
            "detail": c.detail,
            "fix": c.fix.text if c.fix else None,
            "fix_id": c.fix.id if c.fix and c.fix.kind == "local" else None,
        }
        for c in checks
    ]
