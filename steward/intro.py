"""The first-run screen (Phase 19, G8/G9/G10): one plain-ANSI page, one keypress, a live cockpit.

    welcome_screen(...)   pure: the lines of the page for a given size (80x24 and a 40x30 phone are unit-tested)
    run(term, ctx)        the key loop: detect -> page -> Enter -> `scaffold.apply_scaffold` -> last page
    run_text(checks)      the same checklist as text for every non-TTY path

The page and the behaviour share ONE source: the checklist is `prereq.detect` (what `steward doctor` prints) and
"what will be created" is `scaffold.render_plan(scaffold.plan_scaffold(...))`; Enter applies exactly that plan.
No curses: the terminal sits behind the small `Terminal` protocol (`RealTerminal` uses termios cbreak and restores
it in `finally`; tests use a scripted fake).

Fixes (G9) are local, reversible and user-space only: `apply_fix` accepts three ids and nothing else (create the
directories, create an empty keys.env, `uv python install 3.12`). Installs, logins and anything needing root are
printed as the exact command; the `l` key only opens `claude auth login` in a new pane for the user to complete.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from steward import cockpit, components, installstate, keys, prereq, scaffold, templates_io
from steward import version as version_module
from steward.jobview import fit, vlen
from steward.store import resolve_config_dir, resolve_home

CLEAR = "\x1b[H\x1b[J"
WIDE = 60  # below this width rows drop their padding (the phone is 40 columns)
MAX_WHATS_NEW = 10
UV_TIMEOUT_S = 900

TITLE = "Steward"
WHAT_IS = (
    "One controller, one worker per job.",
    "Workers code in their own worktree.",
    "Checks, not claims, decide 'done'.",
    "You steer; you accept or release.",
)
HINT_OUTSIDE_HERDR = "run steward inside a Herdr pane to set up"
REQUIRED_FIRST = "fix the required items first"
ALL_SET = "everything is in place: nothing to create"

# (key, long label, short label); `l` and `r` only appear when a login pane can be opened.
_KEYS = (("Enter", "scaffold", "go"), ("f", "fixes", "fix"), ("l", "login", "login"), ("r", "recheck", "re"),
         ("o", "optional", "opt"), ("q", "quit", "quit"))

# what's new, keyed by the running major version; an unknown major gets the generic line, never a blocker
WHATS_NEW: dict[int, tuple[str, ...]] = {
    0: (
        "The first packaged release: install it, run `steward` in a Herdr pane, press Enter.",
        "Optional doors (MCP, tunnel, Telegram, Jev) stay off until you run `steward enable <name>`.",
    ),
}
GENERIC_WHATS_NEW = "Steward {new} replaces {old}. Your config, jobs and optional components are kept."

LOCAL_FIXES = prereq.LOCAL_FIXES  # ("uv_python", "keys_env", "config_dirs"): nothing else is ever applied
_FIX_NAMES = {"config_dirs": "create directories", "keys_env": "create keys.env", "uv_python": "uv python install 3.12"}


# -- the terminal -----------------------------------------------------------------------------------------------------


class Terminal(Protocol):
    def write(self, text: str) -> None: ...

    def read_key(self) -> str: ...  # "enter", "esc", "ctrl-c", "eof" or one printable character

    def read_line(self, prompt: str, secret: bool = False) -> str: ...

    def size(self) -> tuple[int, int]: ...  # (columns, rows)

    def is_tty(self) -> bool: ...


class RealTerminal:
    """stdin in cbreak mode for the keys (output processing stays on, so `\\n` is a newline), cooked mode for typed
    lines. `__exit__` restores the saved settings even when the flow raised (a crash never leaves raw mode)."""

    def __init__(self, *, fd: int | None = None, out=None, termios_mod=None, tty_mod=None, read=None, size=None,
                 input_fn=None, getpass_fn=None):
        if termios_mod is None:
            import termios as termios_mod
        if tty_mod is None:
            import tty as tty_mod
        self._termios, self._tty = termios_mod, tty_mod
        self._fd = sys.stdin.fileno() if fd is None else fd
        self._out = out if out is not None else sys.stdout
        self._read = read or (lambda n: os.read(self._fd, n))
        self._size = size or (lambda: tuple(shutil.get_terminal_size((80, 24))))
        self._input = input_fn or input
        self._getpass = getpass_fn or getpass.getpass
        self._saved = None
        self._buf: list[str] = []

    def __enter__(self) -> "RealTerminal":
        self._saved = self._termios.tcgetattr(self._fd)
        try:
            self._tty.setcbreak(self._fd)
            self._termios.tcflush(self._fd, self._termios.TCIFLUSH)  # nothing typed before the page counts
        except BaseException:
            self._restore()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self._restore()

    def _restore(self) -> None:
        if self._saved is not None:
            try:
                self._out.flush()
            finally:
                self._termios.tcsetattr(self._fd, self._termios.TCSADRAIN, self._saved)

    def write(self, text: str) -> None:
        self._out.write(text)
        self._out.flush()

    def read_key(self) -> str:
        while not self._buf:
            data = self._read(64)
            if not data:
                return "eof"
            self._buf.extend(self._parse(data.decode("utf-8", "ignore")))
        return self._buf.pop(0)

    @staticmethod
    def _parse(text: str) -> list[str]:
        keys_: list[str] = []
        i = 0
        while i < len(text):
            c = text[i]
            if c == "\x1b":
                j = i + 1
                if j < len(text) and text[j] in "[O":  # an escape sequence (arrows...) is one "esc"
                    j += 1
                    while j < len(text) and not (0x40 <= ord(text[j]) <= 0x7E):
                        j += 1
                    j += 1
                keys_.append("esc")
                i = max(j, i + 1)
                continue
            if c in "\r\n":
                keys_.append("enter")
            elif c == "\x03":
                keys_.append("ctrl-c")
            elif c == "\x04":
                keys_.append("eof")
            elif c.isprintable():
                keys_.append(c)
            i += 1
        return keys_

    def read_line(self, prompt: str, secret: bool = False) -> str:
        """A typed line in cooked mode (echo and editing on; hidden for a secret), then back to cbreak."""
        if self._saved is not None:
            self._termios.tcsetattr(self._fd, self._termios.TCSADRAIN, self._saved)
        try:
            return (self._getpass if secret else self._input)(prompt)
        finally:
            if self._saved is not None:
                self._tty.setcbreak(self._fd)

    def size(self) -> tuple[int, int]:
        cols, rows = self._size()
        return int(cols), int(rows)

    def is_tty(self) -> bool:
        return True


# -- pure screens -------------------------------------------------------------------------------------------------------


def _paint(text: str, code: str, color: bool) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if color and text else text


def _mark(check) -> tuple[str, str]:
    if check.status == prereq.OK:
        return "✓", "32"
    return ("✗", "31") if check.level == prereq.REQUIRED else ("–", "2")


def _fix_text(check) -> str:
    if check.fix is None or check.status == prereq.OK:
        return ""
    return "→ " + ("[f] " if check.fix.kind == "local" else "") + check.fix.text


def _row(check, cols: int, color: bool) -> str:
    """`✓ label  detail`; a row that is not ok ends with `→ fix` (local fixes tagged `[f]`). When the row is too wide
    the detail goes first so the fix stays readable; what still does not fit is cut at a word with an ellipsis."""
    mark, code = _mark(check)
    label = check.label if cols < WIDE else f"{check.label:<18}"
    fix = _fix_text(check)
    line = f"{mark} {label} {check.detail}" + (f" {fix}" if fix else "")
    if fix and len(line) > cols:
        line = f"{mark} {check.label} {fix}"
    line = fit(line, cols)
    return _paint(line[:1], code, color) + line[1:]


def keys_line(cols: int, login: bool = False) -> str:
    entries = [e for e in _KEYS if login or e[0] not in ("l", "r")]
    for form, sep in ((1, "  "), (2, "  "), (2, " ")):
        text = sep.join(f"{e[0]} {e[form]}" for e in entries)
        if len(text) <= cols:
            return text
    return fit(" ".join(f"{e[0]} {e[2]}" for e in entries if e[0] in ("Enter", "q")), cols)


def _create_block(actions, complete: bool, refuse: bool, cols: int, color: bool, keep: int | None) -> list[str]:
    if complete:
        return [_paint(fit(ALL_SET, cols), "32", color)]
    if refuse:
        first = actions[0]
        text = REQUIRED_FIRST if first.detail.get("reason") == "prerequisites" else first.text
        return [_paint(fit(text, cols), "31", color)]
    items = [fit(line, cols) for line in scaffold.render_plan(actions).splitlines()[1:]]
    if not items:
        return ["will create: nothing"]
    if keep is None or keep >= len(items):
        return ["will create:", *items]
    if keep <= 0:
        return [fit(f"will create: {len(items)} steps", cols)]
    return ["will create:", *items[: keep - 1], fit(f"+{len(items) - keep + 1} more steps", cols)]


def welcome_screen(checks, actions, cols: int, rows: int, color: bool = True, *, version: str = "",
                   notes=(), complete: bool = False, offered=(), login: bool = False) -> list[str]:
    """The page as a list of lines, every one within `cols` visible cells and never more than `rows` of them. When the
    height is short the optional rows collapse to `+N optional off, press o`, then the blank separators go, then the
    `will create` list is cut to its first steps; the title, the required rows and the keys line always stay."""
    cols, rows = max(int(cols), 20), max(int(rows), 8)
    req = [c for c in checks if c.level == prereq.REQUIRED]
    opt = [c for c in checks if c.level != prereq.REQUIRED]
    refuse = bool(actions) and actions[0].kind == "refuse"
    title = [_paint(fit(f"{TITLE} {version}".strip(), cols), "1", color)]
    what = [fit(line, cols) for line in WHAT_IS]
    req_rows = [_row(c, cols, color) for c in req]
    opt_full = [_row(c, cols, color) for c in opt]
    n_off = sum(1 for c in opt if c.status != prereq.OK)
    if not opt:
        opt_short: list[str] = []
    elif n_off:
        opt_short = [_paint(fit(f"+{n_off} optional off, press o", cols), "2", color)]
    else:
        opt_short = [fit(f"{len(opt)} optional on, press o", cols)]
    n_items = 0 if (complete or refuse) else max(len(scaffold.render_plan(actions).splitlines()) - 1, 0)
    note_lines = [fit(n, cols) for n in notes]
    fixes_line = (fit("f will: " + ", ".join(_FIX_NAMES.get(i, t) for i, t in offered), cols) if offered else None)
    keys_l = keys_line(cols, login)

    def build(expand: bool, blanks: bool, keep: int | None, with_what: bool = True, with_fixes: bool = True,
              with_notes: bool = True) -> list[str]:
        sections = [
            title + (what if with_what else []),
            req_rows + (opt_full if expand else opt_short),
            _create_block(actions, complete, refuse, cols, color, keep),
            (note_lines if with_notes else []) + ([fixes_line] if fixes_line and with_fixes else []) + [keys_l],
        ]
        out: list[str] = []
        for i, sec in enumerate(sections):
            if i and blanks:
                out.append("")
            out += sec
        return out

    attempts = [(True, True, None), (False, True, None), (False, False, None)]
    attempts += [(False, False, keep) for keep in range(n_items - 1, -1, -1)]
    attempts += [(False, False, 0, True, False, True), (False, False, 0, True, False, False),
                 (False, False, 0, False, False, False)]
    lines: list[str] = []
    for attempt in attempts:
        lines = build(*attempt)
        if len(lines) <= rows:
            return lines
    return lines[: rows - 1] + [keys_l]


def optional_screen(status_rows, cols: int, rows: int, color: bool = True, notes=()) -> list[str]:
    """The eight components with their on/off state; a digit selects one."""
    cols, rows = max(int(cols), 20), max(int(rows), 8)
    out = [_paint(fit("Optional components", cols), "1", color)]
    for n, (cid, on, text) in enumerate(status_rows, start=1):
        title = components.COMPONENTS[cid].title if cid in components.COMPONENTS else ""
        line = f"{n} {'✓' if on else '–'} {cid:<13} {text}" + (f"  {title}" if cols >= WIDE else "")
        out.append(fit(line, cols))
    room = max(rows - len(out) - 1, 0)
    out += [fit(n, cols) for n in list(notes)[-room:]] if room else []
    out.append(fit(f"1-{len(status_rows)} select  b back  q quit", cols))
    return out[: rows - 1] + [out[-1]] if len(out) > rows else out


def component_screen(cid: str, plan_lines, cols: int, rows: int, color: bool = True, can_enable: bool = True
                     ) -> list[str]:
    """One component's plan text (what it writes, runs and exposes) and the confirm key."""
    cols, rows = max(int(cols), 20), max(int(rows), 8)
    keys_l = fit("y enable  b back  q quit" if can_enable else "b back  q quit", cols)
    room = rows - 2
    body = [fit(line, cols) for line in plan_lines]
    if len(body) > room:
        body = body[: room - 1] + [fit(f"+{len(body) - room + 1} more lines", cols)]
    return [_paint(fit(f"{cid}", cols), "1", color), *body[:room], keys_l]


def done_screen(result: dict, cwd: str | Path, cols: int, rows: int, color: bool = True) -> list[str]:
    """After the scaffold: the first thing to say and where the cockpit is, plus anything the run left to read
    (newer templates written beside the user's files, restarts deferred while a pane was busy)."""
    cols, rows = max(int(cols), 20), max(int(rows), 8)
    errors = list(result.get("errors") or [])
    keys_l = fit("Enter done", cols)
    if errors:
        lines = [_paint(fit("Setup stopped; nothing was recorded as installed.", cols), "31", color)]
        lines += [fit(f"error: {e}", cols) for e in errors]
        lines.append(fit("Fix the error and run `steward init` again.", cols))
        lines.append(keys_l)
        return lines if len(lines) <= rows else lines[: rows - 1] + [keys_l]
    repo = Path(str(cwd)).name or "this-repo"
    cockpit_res = result.get("cockpit") or {}
    lines = [_paint(fit("Steward is set up.", cols), "1", color), ""]
    if cockpit_res.get("operator_pane") or not cockpit_res:
        lines += [fit("In the operator pane, say:", cols), fit(f"  Run Steward on {repo}: <goal>", cols)]
    else:
        lines += [fit(cockpit_res.get("operator_note") or "No operator pane was started.", cols),
                  fit("Start one with `steward cockpit`, then say:", cols),
                  fit(f"  Run Steward on {repo}: <goal>", cols)]
    lines += ["", fit(f"The cockpit is the `{cockpit.COCKPIT_TAB_LABEL}` tab: operator left, status right.", cols),
              fit("Control and advisor run in the `steward` tab.", cols)]
    extra = [line for line in result.get("lines") or [] if "template was written beside" in line]
    extra += [f"restart deferred: {role} is busy; `steward up` tries again" for role in result.get("deferred") or []]
    if extra:
        lines.append("")
        for line in extra:
            lines += textwrap.wrap(line, cols)[:3]
    lines.append(keys_l)
    if len(lines) > rows:
        lines = [ln for ln in lines if ln != ""]
    return lines if len(lines) <= rows else lines[: rows - 1] + [keys_l]


def whats_new_lines(old_seen: object, new_version: str) -> list[str]:
    """At most MAX_WHATS_NEW plain lines for the running major (generic text for an unknown one)."""
    major = version_module.major(new_version)
    texts = WHATS_NEW.get(major) or (GENERIC_WHATS_NEW.format(new=new_version, old=old_seen),)
    return [str(t) for t in texts][:MAX_WHATS_NEW]


def whatsnew_screen(old_seen: object, new_version: str, cols: int, rows: int, color: bool = True) -> list[str]:
    cols, rows = max(int(cols), 20), max(int(rows), 8)
    body: list[str] = []
    for text in whats_new_lines(old_seen, new_version):
        body += textwrap.wrap(text, cols - 2) or [""]
    room = min(MAX_WHATS_NEW, rows - 3)
    if len(body) > room:
        body = body[: room - 1] + ["…"]
    return [_paint(fit(f"What's new in Steward {new_version}", cols), "1", color), "",
            *[fit(f" {b}", cols) for b in body], "", fit("Enter continue  q quit", cols)][:rows]


def run_text(checks, out: Callable[[str], None] = print) -> int:
    """The non-TTY path: the same checklist as text; exit 1 when a required item is missing."""
    out(prereq.render_text(checks))
    return 1 if prereq.required_missing(checks) else 0


# -- fixes (G9) ---------------------------------------------------------------------------------------------------


@dataclass
class Ctx:
    """What the intro needs; every process and Herdr call goes through `runner` / `herdr_api`."""

    runner: Any
    herdr_api: Any
    sock: str | None
    workspace: str | None
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    cwd: str = ""
    new_workspace: bool = False
    no_operator: bool = False
    home: Path | str | None = None
    config_dir: Path | str | None = None
    which: Callable[[str], str | None] = shutil.which
    which_for: Callable[[list], Callable[[str], str | None]] | None = None
    components_ctx: Callable[[Terminal, Callable[[str], None]], Any] | None = None


def claude_aware_which(checks) -> Callable[[str], str | None]:
    """`which` for the scaffold: `claude` counts as present exactly when its prerequisite check passed."""
    claude_ok = any(c.id == "claude" and c.status == prereq.OK for c in checks)

    def which(name: str) -> str | None:
        return ("claude" if claude_ok else None) if name == "claude" else shutil.which(name)

    return which


def offered_fixes(checks, d) -> list[tuple[str, str]]:
    """`(id, text)` for every LOCAL fix the intro can apply right now: missing directories, a keys.env that does not
    exist yet (Jev row), `uv python install 3.12` (old Python, `uv` present)."""
    out: list[tuple[str, str]] = []
    if not (d.home_exists and d.config_exists):
        out.append(("config_dirs", "create the directories"))
    for c in checks:
        if c.status == prereq.OK or c.fix is None or c.fix.kind != "local" or c.fix.id not in LOCAL_FIXES:
            continue
        if c.fix.id == "keys_env" and _keys_env(d).exists():
            continue
        if (c.fix.id, c.fix.text) not in out:
            out.append((c.fix.id, c.fix.text))
    return out


def _keys_env(d) -> Path:
    return Path(d.config_dir) / keys.KEYS_ENV_NAME


def display_checks(checks, d) -> list:
    """The checklist as drawn: a Jev row whose keys.env now exists no longer offers `[f]` (only the key is missing)."""
    out = []
    for c in checks:
        if c.fix is not None and c.fix.id == "keys_env" and _keys_env(d).exists():
            c = prereq.Check(c.id, c.label, c.level, c.status, c.detail,
                             prereq.Fix("command", f"add TYPESAFE_API_KEY to {keys.KEYS_ENV_NAME}"))
        out.append(c)
    return out


def apply_fix(fix_id: str, ctx: Ctx) -> str:
    """Apply ONE local fix and return one result line. Only the ids in LOCAL_FIXES exist; there is no way to pass a
    command through here, and nothing it runs is sudo, a package-manager install or a login."""
    if fix_id == "config_dirs":
        made = templates_io.ensure_dirs(resolve_home(ctx.home), resolve_config_dir(ctx.config_dir))
        return "created " + ", ".join(str(p) for p in made) if made else "directories already exist"
    if fix_id == "keys_env":
        path = Path(resolve_config_dir(ctx.config_dir)) / keys.KEYS_ENV_NAME
        if path.exists():
            return f"{path} already exists: left as it is"
        keys.ensure_keys_env()
        return f"created {path} (0600): add your keys there"
    if fix_id == "uv_python":
        if not ctx.which("uv"):
            return "uv is not installed: install uv, then run `uv python install 3.12`"
        res = ctx.runner.run(["uv", "python", "install", "3.12"], ctx.cwd or ".", timeout=UV_TIMEOUT_S)
        if res.exit_code != 0:
            return f"`uv python install 3.12` failed (exit {res.exit_code}): run it yourself to see why"
        return "Python 3.12 installed by uv: start Steward under it"
    raise ValueError(f"not a local fix: {fix_id!r}")


def run_fixes(ctx: Ctx, offered) -> list[str]:
    return [apply_fix(fix_id, ctx) for fix_id, _text in offered]


def login_offered(checks, ctx: Ctx) -> bool:
    """`l` is offered only for an installed-but-logged-out claude, and only when there is a pane to split."""
    claude = next((c for c in checks if c.id == "claude"), None)
    return bool(claude and claude.status != prereq.OK and claude.detail == "not logged in"
                and ctx.env.get("HERDR_PANE_ID"))


def open_login_pane(ctx: Ctx) -> str:
    """Split the current pane downwards and type `claude auth login` into it. The user completes the login there."""
    pane = ctx.env.get("HERDR_PANE_ID") or ""
    cwd = ctx.cwd or "."
    res = ctx.runner.run(["herdr", "pane", "split", "--pane", pane, "--direction", "down", "--cwd", str(cwd)], cwd=cwd)
    try:
        new = json.loads(res.stdout)["result"]["pane"]["pane_id"] if res.exit_code == 0 else None
    except (ValueError, KeyError, TypeError):
        new = None
    if not new:
        return "could not open a pane: run `claude auth login` yourself"
    ctx.runner.run(["herdr", "pane", "rename", new, "claude login"], cwd=cwd)
    ctx.runner.run(["herdr", "pane", "run", new, "claude auth login"], cwd=cwd)
    return "claude login is open in the pane below: finish it there, then press r"


# -- the flow ------------------------------------------------------------------------------------------------------


def _observe(ctx: Ctx):
    d = scaffold.detect(runner=ctx.runner, herdr_api=ctx.herdr_api, sock=ctx.sock, workspace=ctx.workspace,
                        cwd=ctx.cwd or os.getcwd(), new_workspace=ctx.new_workspace, no_operator=ctx.no_operator,
                        home=ctx.home, config_dir=ctx.config_dir)
    return d, scaffold.plan_scaffold(d)


def is_complete(d, actions) -> bool:
    """True when running the scaffold would change nothing: the install is recorded and current, the directories,
    templates, registry, control/advisor panes and the cockpit are all there."""
    if actions and actions[0].kind == "refuse":
        return False
    if d.install is None or d.install_error or d.upgrading or d.migration_pending or d.new_workspace:
        return False
    if not (d.home_exists and d.config_exists and d.registry_exists) or d.dead or d.unreported:
        return False
    if any(not (Path(d.config_dir) / name).exists() for name in templates_io.NAMES):
        return False
    if d.control_pane is None or (d.trajectory_on and d.advisor_pane is None):
        return False
    return bool(d.tabs_labelled(cockpit.COCKPIT_TAB_LABEL)) and any(
        p.get("label") == cockpit.DASH_LABEL for p in d.panes)


def _mark_seen(d) -> bool:
    state = installstate.load(d.home)
    if state is None:
        return False
    installstate.mark_intro_seen(state, d.version)
    installstate.save(state, d.home)
    return True


def _draw(term: Terminal, lines: list[str]) -> None:
    term.write(CLEAR + "\n".join(lines))


def _size(term: Terminal) -> tuple[int, int]:
    cols, rows = term.size()
    return cols, rows


def _quit_code(d) -> int:
    return 1 if prereq.required_missing(d.checks) else 0


def _components_ctx(ctx: Ctx, term: Terminal, out: Callable[[str], None]):
    if ctx.components_ctx is not None:
        return ctx.components_ctx(term, out)
    return components.make_ctx(
        runner=ctx.runner, home=ctx.home, config_dir=ctx.config_dir, env=ctx.env, which=ctx.which,
        ask=lambda prompt: term.read_line(prompt), ask_secret=lambda prompt: term.read_line(prompt, secret=True),
        out=out, yes=True, interactive=True, cwd=ctx.cwd or None,
    )


def _optional_flow(term: Terminal, ctx: Ctx, d, color: bool) -> int | None:
    """The `o` page. Returns an exit code when the user quit, None to go back to the first page."""
    notes: list[str] = []
    while True:
        try:
            state = installstate.load(d.home)
        except installstate.InstallStateError:
            state = None
        cols, rows = _size(term)
        _draw(term, optional_screen(components.status(state), cols, rows, color, notes))
        key = term.read_key().lower()
        notes = []
        if key in ("q", "eof", "ctrl-c"):
            return _quit_code(d)
        if key in ("b", "esc"):
            return None
        if not (key.isdigit() and 1 <= int(key) <= len(components.COMPONENT_IDS)):
            continue
        cid = components.COMPONENT_IDS[int(key) - 1]
        sink: list[str] = []
        cctx = _components_ctx(ctx, term, sink.append)
        if any(row_id == cid and on for row_id, on, _text in components.status(state)):
            notes = [f"{cid} is on; turn it off with `steward disable {cid}`"]
            continue
        plan = components.plan(cid, cctx, None, state)
        while True:
            cols, rows = _size(term)
            _draw(term, component_screen(cid, plan.text().splitlines(), cols, rows, color, not plan.blockers))
            sub = term.read_key().lower()
            if sub in ("q", "eof", "ctrl-c"):
                return _quit_code(d)
            if sub in ("b", "esc"):
                break
            if sub == "y" and not plan.blockers:
                sink.clear()
                code = components.enable(cid, cctx)
                notes = [line for text in sink for line in text.splitlines() if line.strip()][-4:]
                if code != 0 and not notes:
                    notes = [f"{cid}: not enabled"]
                break


def run(term: Terminal, ctx: Ctx) -> int:
    """The first-run flow. Returns the exit code: 0 after a finished intro (or `q` when nothing required is missing),
    1 when a required item is missing or the scaffold failed. `intro_seen` is written only by a finished intro."""
    color = term.is_tty() and not ctx.env.get("NO_COLOR")
    d, actions = _observe(ctx)
    needs, why = installstate.needs_intro(d.install, d.version)
    if needs and why == "major":
        cols, rows = _size(term)
        seen = (d.install or {}).get("intro_seen")
        _draw(term, whatsnew_screen(seen, d.version, cols, rows, color))
        if term.read_key().lower() != "enter":
            return 0
    notes: list[str] = []
    while True:
        offered = offered_fixes(d.checks, d)
        login = login_offered(d.checks, ctx)
        complete = is_complete(d, actions)
        cols, rows = _size(term)
        _draw(term, welcome_screen(display_checks(d.checks, d), actions, cols, rows, color, version=d.version,
                                   notes=notes, complete=complete, offered=offered, login=login))
        key = term.read_key().lower()
        notes = []
        if key in ("q", "eof", "ctrl-c"):
            return _quit_code(d)
        if key == "f":
            notes = run_fixes(ctx, offered) or ["nothing to fix here"]
            d, actions = _observe(ctx)
        elif key == "l" and login:
            notes = [open_login_pane(ctx)]
        elif key == "r":
            d, actions = _observe(ctx)
        elif key == "o":
            code = _optional_flow(term, ctx, d, color)
            if code is not None:
                return code
            d, actions = _observe(ctx)
        elif key == "enter":
            if actions and actions[0].kind == "refuse":
                notes = [REQUIRED_FIRST if actions[0].detail.get("reason") == "prerequisites" else actions[0].text]
                continue
            if complete:
                _mark_seen(d)
                return 0
            which_for = ctx.which_for or claude_aware_which
            sctx = scaffold.Ctx(runner=ctx.runner, herdr_api=ctx.herdr_api, sock=ctx.sock or "", detected=d,
                                which=which_for(d.checks))
            result = scaffold.apply_scaffold(actions, sctx)
            ok = not result["errors"]
            if ok:
                _mark_seen(d)
            cols, rows = _size(term)
            _draw(term, done_screen(result, ctx.cwd or os.getcwd(), cols, rows, color))
            term.read_key()
            return 0 if ok else 1
