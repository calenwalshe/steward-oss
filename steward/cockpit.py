"""The `cockpit` Herdr tab: an operator Claude (left) and the status pane (right) (Phase 19, G4/G5).

`ensure_cockpit` is idempotent by LABEL, never by a remembered id: the tab is labelled `cockpit`, its panes
`operator` and `steward dash`. A second call finds them, focuses the tab and creates nothing. A missing pane is
recreated by splitting the surviving one (Herdr splits only right or down); a dead pane (only a shell is left in it)
is restarted in place with `herdr pane run`; nothing is ever closed. A command is only ever typed into a pane this
module just created or found by label inside the cockpit tab. When a pane's state cannot be read it is left alone.

Every Herdr call goes through the injectable `Runner`, so tests use a fake.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Callable

from steward import launch, owner, pkgenv, version
from steward.runner_exec import Runner
from steward.store import resolve_config_dir, resolve_home

COCKPIT_TAB_LABEL = "cockpit"
OPERATOR_LABEL = "operator"
DASH_LABEL = "steward dash"
WORKSPACE_LABEL = "steward"  # `--new-workspace`
SPLIT_RATIO = "0.5"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


# -- the guide and the seed ---------------------------------------------------------------------------------------


def steward_invocation(which: Callable[[str], str | None] | None = None, python: str | None = None) -> str:
    """How the operator types a Steward command: `steward` when it is on PATH, else `<python> -m steward.cli`."""
    if (which or shutil.which)("steward"):
        return "steward"
    return f"{shlex.quote(python or sys.executable)} -m steward.cli"


def render_guide(owner_name: str, steward: str) -> str:
    """The operator guide template with `%%OWNER%%`, `%%OWNER_POSS%%` and `%%STEWARD%%` filled in."""
    text = (TEMPLATE_DIR / "OPERATOR.md").read_text()
    return (text.replace("%%OWNER_POSS%%", f"{owner_name}'s").replace("%%OWNER%%", owner_name)
            .replace("%%STEWARD%%", steward))


def seed_text(guide_path: str | Path, workspace_label: str, dash_pane: str, *, owner_name: str, steward: str,
              operator_pane: str | None = None) -> str:
    """The operator's first prompt: one paragraph (it is passed as a single argument) ending with the reply instruction."""
    where = f"your pane: {operator_pane}; " if operator_pane else ""
    cap = owner_name[:1].upper() + owner_name[1:]
    return (
        f"You are the Steward operator in Herdr workspace '{workspace_label}' ({where}the live status panel is pane "
        f"{dash_pane}, and the 'steward' tab holds the control and advisor panes; leave those alone). Read "
        f"{guide_path} now, including \"Workbench posture\". Posture: {cap} shapes work with you, then it runs, "
        f"{owner_name} steers it, and {owner_name} accepts it. SHAPE: when work is described, draft it together: goal, "
        f"the measurable finish line (counts, durations, files, behaviour) and the don'ts; show it in a few lines and "
        f"launch only on an explicit go. RUN: no permission ceremony; only outbound or irreversible acts wait. "
        f"STEER: turn plain words into {steward} steer, focus or avoid. FINISH: when a job reaches NEEDS_HUMAN, show "
        f"the finish-line evidence (verification and the actual outputs) and accept only when {owner_name} says so. "
        f"Then reply in ONE line: that you are ready, plus the count of active Steward jobs from `{steward} status`."
    )


def operator_args(config_dir: Path | str | None = None) -> list[str]:
    """`[cockpit] operator_args` of steward.toml (a string parsed with shlex, or a list of strings); [] by default,
    so no permission-skipping flag ever ships. Anything unreadable is [] too."""
    try:
        data = tomllib.loads((resolve_config_dir(config_dir) / "steward.toml").read_text())
        raw = (data.get("cockpit") or {}).get("operator_args")
    except (OSError, ValueError, AttributeError):
        return []
    if isinstance(raw, str):
        try:
            return shlex.split(raw)
        except ValueError:
            return []
    if isinstance(raw, list) and all(isinstance(a, str) for a in raw):
        return list(raw)
    return []


def _exports() -> str:
    """The pane environment (`launch._env_exports`), plus PYTHONPATH when it is not package mode so that
    `python -m steward.cli` works from the operator's own directory."""
    out = launch._env_exports()
    if not version.is_package_install():
        root = shlex.quote(str(version.install_root()))
        out += f"export PYTHONPATH={root}${{PYTHONPATH:+:$PYTHONPATH}} && "
    return out


def operator_command(seed_path: str | Path, *, cwd: str | Path, args: list[str] | None = None) -> str:
    """`claude <operator_args> "$(cat <seed>)"` in `cwd`, with the pane environment exported first."""
    argv = " ".join(["claude", *(shlex.quote(a) for a in (args or []))])
    return _exports() + f"cd {shlex.quote(str(cwd))} && {argv} \"$(cat {shlex.quote(str(seed_path))})\""


def dash_command() -> str:
    """The status pane: `<python> -m steward.cli dash` from the install root, with the pane environment."""
    root = shlex.quote(str(version.install_root()))
    return launch._env_exports() + f"cd {root} && {shlex.quote(sys.executable)} -m steward.cli dash"


def write_operator_files(owner_name: str, steward: str, workspace_label: str, dash_pane: str,
                         operator_pane: str | None = None, home: Path | str | None = None) -> Path:
    """Write `$STEWARD_HOME/cockpit/OPERATOR.md` (the rendered guide) and `seed.txt` (the first prompt); both are
    generated artifacts, rewritten on every call, never user config. Returns the seed path."""
    directory = resolve_home(home) / "cockpit"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    guide_path, seed_path = directory / "OPERATOR.md", directory / "seed.txt"
    guide_path.write_text(render_guide(owner_name, steward))
    seed_path.write_text(seed_text(guide_path, workspace_label, dash_pane, owner_name=owner_name, steward=steward,
                                   operator_pane=operator_pane))
    return seed_path


# -- Herdr helpers ------------------------------------------------------------------------------------------------


def _json(runner: Runner, argv: list[str], cwd) -> dict | None:
    res = runner.run(["herdr", *argv], cwd=cwd)
    if res.exit_code != 0:
        return None
    try:
        out = json.loads(res.stdout).get("result")
    except (ValueError, AttributeError):
        return None
    return out if isinstance(out, dict) else None


def _alive(runner: Runner, pane_id: str, needle: str | tuple[str, ...], cwd) -> bool:
    """False only when Herdr says the pane has no foreground process whose argv mentions `needle` (or one of them; a
    shell is all that is left). An unreadable answer counts as alive: nothing is ever typed into a pane of unknown
    state."""
    needles = (needle,) if isinstance(needle, str) else needle
    info = _json(runner, ["pane", "process-info", "--pane", pane_id], cwd)
    if info is None:
        return True
    procs = ((info.get("process_info") or {}).get("foreground_processes")) or []
    for proc in procs:
        argv = proc.get("argv") if isinstance(proc, dict) else proc
        text = " ".join(str(a) for a in argv) if isinstance(argv, list) else str(argv or "")
        if any(n in text for n in needles):
            return True
    return False


def _split(runner: Runner, base: str, direction: str, cwd) -> str | None:
    res = runner.run(["herdr", "pane", "split", "--pane", base, "--direction", direction, "--ratio", SPLIT_RATIO,
                      "--cwd", str(cwd), "--no-focus"], cwd=cwd)
    if res.exit_code != 0:
        return None
    try:
        return json.loads(res.stdout)["result"]["pane"]["pane_id"]
    except (ValueError, KeyError, TypeError):
        return None


def _workspace_label(runner: Runner, workspace: str, cwd) -> str:
    rows = (_json(runner, ["workspace", "list"], cwd) or {}).get("workspaces") or []
    hit = next((w for w in rows if w.get("workspace_id") == workspace and w.get("label")), None)
    return str(hit["label"]) if hit else workspace


def _focus(runner: Runner, argv: list[str], cwd) -> None:
    runner.run(["herdr", *argv], cwd=cwd)


# -- ensure_cockpit -----------------------------------------------------------------------------------------------


def ensure_cockpit(
    runner_exec: Runner,
    herdr_api,  # the fabric API; unused here: every call goes through `runner_exec` (kept for the caller's symmetry)
    sock: str,
    workspace: str | None,
    *,
    cwd: str | Path,
    operator: bool = True,
    new_workspace: bool = False,
    focus: bool = True,
    which: Callable[[str], str | None] | None = None,
) -> dict:
    """Make sure the `cockpit` tab with its operator pane and dash pane exists, without creating anything twice.

    Returns `{"workspace", "tab_id", "operator_pane", "dash_pane", "created": [...], "restarted": [...],
    "operator_note"}` (`created` lists "workspace", "tab", "operator", "dash"), or `{"error": ..., "created": [...]}`.
    Never raises on a Herdr failure. With `new_workspace`, an existing workspace labelled `steward` is only focused."""
    which = which or shutil.which
    runner = runner_exec
    result: dict = {"workspace": workspace, "tab_id": None, "operator_pane": None, "dash_pane": None,
                    "created": [], "restarted": [], "operator_note": None}
    created, restarted = result["created"], result["restarted"]

    want_operator = operator
    if not operator:
        result["operator_note"] = "operator skipped (--no-operator)"
    elif not which("claude"):
        want_operator = False
        result["operator_note"] = "operator skipped (no claude)"

    def fail(message: str) -> dict:
        result["error"] = message
        return result

    root_pane: str | None = None
    tab_id: str | None = None

    if new_workspace:
        rows = (_json(runner, ["workspace", "list"], cwd) or {}).get("workspaces") or []
        existing = next((w for w in rows if w.get("label") == WORKSPACE_LABEL), None)
        if existing:
            result["workspace"] = existing["workspace_id"]
            result["workspace_focused"] = True
            _focus(runner, ["workspace", "focus", existing["workspace_id"]], cwd)
            return result
        res = runner.run(["herdr", "workspace", "create", "--label", WORKSPACE_LABEL, "--cwd", str(cwd), "--focus"], cwd=cwd)
        if res.exit_code != 0:
            return fail(f"herdr workspace create failed: {res.stderr.strip()}")
        try:
            data = json.loads(res.stdout)["result"]
            workspace = data["workspace"]["workspace_id"]
            tab_id = data["tab"]["tab_id"]
            root_pane = data["root_pane"]["pane_id"]
        except (ValueError, KeyError, TypeError) as exc:
            return fail(f"unexpected herdr workspace create output: {exc}")
        result["workspace"] = workspace
        created.append("workspace")
        runner.run(["herdr", "tab", "rename", tab_id, COCKPIT_TAB_LABEL], cwd=cwd)
    else:
        if not workspace:
            return fail("no Herdr workspace")
        tabs = (_json(runner, ["tab", "list"], cwd) or {}).get("tabs") or []
        tab = next((t for t in tabs if t.get("label") == COCKPIT_TAB_LABEL and t.get("workspace_id") == workspace), None)
        if tab is not None:
            tab_id = tab["tab_id"]
        else:
            res = runner.run(["herdr", "tab", "create", "--workspace", workspace, "--label", COCKPIT_TAB_LABEL,
                              "--cwd", str(cwd), "--no-focus"], cwd=cwd)
            if res.exit_code != 0:
                return fail(f"herdr tab create failed: {res.stderr.strip()}")
            try:
                data = json.loads(res.stdout)["result"]
                tab_id = data["tab"]["tab_id"]
                root_pane = data["root_pane"]["pane_id"]
            except (ValueError, KeyError, TypeError) as exc:
                return fail(f"unexpected herdr tab create output: {exc}")
            created.append("tab")
    result["tab_id"] = tab_id

    # Panes of this tab, found by label.
    try:
        in_tab = [p for p in launch.list_panes(runner, cwd) if p.get("tab_id") == tab_id]
    except RuntimeError as exc:
        return fail(str(exc))
    operator_row = launch.find_pane_by_label(in_tab, OPERATOR_LABEL) if want_operator else None
    dash_row = launch.find_pane_by_label(in_tab, DASH_LABEL)
    operator_pane = operator_row["pane_id"] if operator_row else None
    dash_pane = dash_row["pane_id"] if dash_row else None
    fresh_operator = fresh_dash = False

    # A brand-new tab's root pane becomes the operator pane (or the dash pane without an operator).
    if root_pane is not None:
        if want_operator:
            operator_pane, fresh_operator = root_pane, True
            runner.run(["herdr", "pane", "rename", root_pane, OPERATOR_LABEL], cwd=cwd)
            created.append("operator")
        else:
            dash_pane, fresh_dash = root_pane, True
            runner.run(["herdr", "pane", "rename", root_pane, DASH_LABEL], cwd=cwd)
            created.append("dash")

    # The dash pane: split the operator (or, in an unlabelled tab, its first pane) to the right.
    if dash_pane is None:
        base = operator_pane or (in_tab[0]["pane_id"] if in_tab else None)
        if base is None:
            return fail("the cockpit tab has no pane to split")
        dash_pane = _split(runner, base, "right", cwd)
        if dash_pane is None:
            return fail("herdr pane split failed")
        fresh_dash = True
        runner.run(["herdr", "pane", "rename", dash_pane, DASH_LABEL], cwd=cwd)
        created.append("dash")

    # The operator pane: split the dash pane downwards.
    if want_operator and operator_pane is None:
        operator_pane = _split(runner, dash_pane, "down", cwd)
        if operator_pane is None:
            return fail("herdr pane split failed")
        fresh_operator = True
        runner.run(["herdr", "pane", "rename", operator_pane, OPERATOR_LABEL], cwd=cwd)
        created.append("operator")

    # Dead panes found by label are restarted in place.
    restart_dash = not fresh_dash and not _alive(runner, dash_pane, ("steward.dashapp", "steward-dash.sh"), cwd)
    restart_operator = (want_operator and not fresh_operator and operator_pane is not None
                        and not _alive(runner, operator_pane, "claude", cwd))

    if fresh_dash or restart_dash:
        runner.run(["herdr", "pane", "run", dash_pane, dash_command()], cwd=cwd)
        if restart_dash:
            restarted.append("dash")
    if fresh_operator or restart_operator:
        steward = steward_invocation(which)
        name = owner.name()
        label = _workspace_label(runner, result["workspace"], cwd) if result["workspace"] else WORKSPACE_LABEL
        seed_path = write_operator_files(name, steward, label, dash_pane, operator_pane)
        runner.run(["herdr", "pane", "run", operator_pane, operator_command(seed_path, cwd=cwd, args=operator_args())], cwd=cwd)
        if restart_operator:
            restarted.append("operator")

    result["operator_pane"], result["dash_pane"] = operator_pane, dash_pane
    if focus and tab_id:
        _focus(runner, ["tab", "focus", tab_id], cwd)
    return result
