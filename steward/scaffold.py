"""`steward init`: scaffold the whole Steward system in a Herdr workspace from ONE pure plan (Phase 19, G3-G6, G10).

    detect(...)          -> Detected      reads only (files, `herdr ... list`, process-info, prerequisite checks)
    plan_scaffold(d)     -> [Action]      pure: no I/O; `--dry-run`, the intro and the tests show exactly this list
    apply_scaffold(a, c) -> {lines, ...}  executes the list in order, stops at the first failing step

install.json is stamped LAST and only when every step succeeded, so a half-failed run is simply run again. Every
step is idempotent: directories and templates are never overwritten, the exceptions registry only gains missing
default sections, and panes are found by tab label and pane label (plus the control/advisor role tokens), so a
re-run creates nothing twice.

Every Herdr call goes through the injectable `Runner` and the `herdr_api` object, so tests use a fake.
"""

from __future__ import annotations

import json
import os
import platform
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from steward import (
    advisor_pane,
    cockpit,
    components,
    exceptions,
    installstate,
    launch,
    migrations,
    prereq,
    templates_io,
    version as version_module,
)
from steward.advisor_pane import ADVISOR_ROLE_VALUE
from steward.control_pane import CONTROL_ROLE_TOKEN, CONTROL_ROLE_VALUE
from steward.herdr_transport import HerdrApiError, HerdrUnavailable
from steward.store import resolve_config_dir, resolve_home

FIRST_USE = "Run Steward on <repo>: <goal>"
JOBS_NOTE = "running jobs keep their old worker code until their next launch"
SETTLE_S = 1.0  # between Ctrl-C and re-running a pane's command
CONTROL_PING_TIMEOUT_S = 5.0

# (role, pane label in the `steward` tab, argv fragment of its live process)
REVIVABLE = (("control", "steward control", "steward.cli control"), ("advisor", "steward advisor", "steward.cli advisor"))


@dataclass(frozen=True)
class Action:
    id: str
    kind: str  # create | run | note | refuse | migrate | restart | stamp
    text: str  # one line: what will be created or done
    detail: dict = field(default_factory=dict, compare=True, hash=False)


@dataclass
class Detected:
    """Everything the plan depends on, gathered read-only by `detect`."""

    home: Path
    config_dir: Path
    version: str
    install_root: str
    python: str
    cwd: str
    checks: list = field(default_factory=list)
    sock: str | None = None
    workspace: str | None = None
    package_mode: bool = False
    install: dict | None = None
    install_error: str | None = None
    home_exists: bool = False
    config_exists: bool = False
    registry_path: Path | None = None
    registry_exists: bool = False
    config_schema: int = 1
    tabs: list = field(default_factory=list)
    panes: list = field(default_factory=list)
    control_pane: str | None = None
    advisor_pane: str | None = None
    herdr_error: str | None = None
    trajectory_on: bool = True
    new_workspace: bool = False
    no_operator: bool = False
    jobs_running: int = 0
    this_roots: tuple = ()  # realpaths that identify THIS install (install root, directory of the running package)
    recorded_root: str | None = None  # realpath of install.json's install_root
    recorded_root_exists: bool = True
    control_roots: dict = field(default_factory=dict)  # control pane id -> realpath it runs from (None: unknown)
    dead: dict = field(default_factory=dict)  # role -> labelled pane whose process is gone (revive in place)
    unreported: dict = field(default_factory=dict)  # role -> labelled pane that is live but has no token yet

    @property
    def upgrading(self) -> bool:
        return self.install is not None and str(self.install.get("version")) != str(self.version)

    @property
    def migration_pending(self) -> bool:
        return self.config_schema < migrations.CURRENT_SCHEMA

    def tabs_labelled(self, label: str) -> list[dict]:
        rows = [t for t in self.tabs if t.get("label") == label]
        if self.workspace:
            rows = [t for t in rows if t.get("workspace_id") in (None, self.workspace)]
        return rows


# -- detect ---------------------------------------------------------------------------------------------------------


def _json_result(runner, argv: list[str], cwd) -> dict | None:
    return cockpit._json(runner, argv, cwd)


def _trajectory_on(home: Path, open_store: bool = True) -> bool:
    """Config (and STEWARD_TRAJECTORY) first; the global kill switch only when a store already exists (never
    creates one) and `open_store` allows it."""
    from steward import trajectory

    try:
        mode = trajectory.load_trajectory_config().mode
    except (OSError, ValueError, TypeError):
        mode = "shadow"
    if open_store and (home / "steward.db").exists():
        from steward.store import Store

        try:
            mode = trajectory.effective_mode(Store(home), "_steward_up", trajectory.load_trajectory_config()).mode
        except Exception:  # noqa: BLE001 - an unreadable store never blocks the scaffold
            pass
    return mode != "off"


def _jobs_running(home: Path) -> int:
    if not (home / "steward.db").exists():
        return 0
    from steward.models import JobState
    from steward.store import Store

    try:
        return len(Store(home).list_jobs({JobState.RUNNING, JobState.PAUSED}))
    except Exception:  # noqa: BLE001
        return 0


def detect(
    *,
    runner,
    herdr_api,
    sock: str | None,
    workspace: str | None,
    cwd: str | Path | None = None,
    new_workspace: bool = False,
    no_operator: bool = False,
    home: Path | str | None = None,
    config_dir: Path | str | None = None,
    with_prereq: bool = True,
) -> Detected:
    """Gather the state the plan needs. Never mutates: file reads, `herdr tab|pane|workspace list`, `pane.list`
    and the prerequisite checks (read-only by contract). Opens the store only when one already exists."""
    home_p = resolve_home(home)
    config_p = resolve_config_dir(config_dir)
    cwd_s = str(cwd or os.getcwd())
    d = Detected(
        home=home_p, config_dir=config_p, version=version_module.version(),
        install_root=str(version_module.install_root()), python=platform.python_version(), cwd=cwd_s,
        sock=sock, workspace=workspace, package_mode=version_module.is_package_install(),
        new_workspace=new_workspace, no_operator=no_operator,
    )
    d.home_exists, d.config_exists = home_p.is_dir(), config_p.is_dir()
    d.registry_path = exceptions.registry_path()
    d.registry_exists = d.registry_path.exists()
    try:
        d.install = installstate.load(home_p)
    except installstate.InstallStateError as exc:
        d.install_error = str(exc)
    try:
        d.config_schema = migrations.config_schema((config_p / migrations.SCHEMA_FILE).read_text())
    except (OSError, migrations.MigrationError):
        d.config_schema = migrations.CURRENT_SCHEMA
    d.this_roots = tuple(sorted({os.path.realpath(d.install_root), os.path.realpath(launch._STEWARD_ROOT)}))
    recorded = (d.install or {}).get("install_root")
    if recorded:
        d.recorded_root = os.path.realpath(str(recorded))
        d.recorded_root_exists = os.path.exists(d.recorded_root)
    if with_prereq:
        d.checks = list(prereq.detect(runner=runner))
    d.trajectory_on = _trajectory_on(home_p, open_store=False)

    if sock:
        _detect_herdr(d, runner, herdr_api)
    # Another install's store is never opened (opening one may migrate its schema): only after the G18 check.
    if foreign_reason(d) is None:
        if not _trajectory_on(home_p):
            d.trajectory_on = False
            d.dead.pop("advisor", None)
            d.unreported.pop("advisor", None)
        d.jobs_running = _jobs_running(home_p)
    return d


_DELETED = " (deleted)"


def _control_root(where: str, d: Detected) -> str:
    """The realpath a control pane runs from. `npm install -g` over a running install renames the old directory to
    `.<name>-<random>` and deletes it, so the pane's cwd reads `<scope>/.<name>-<random> (deleted)`: that is THIS
    install's own predecessor (same parent directory, npm's staging name), not another install, and maps to this
    install's root so the upgrade can restart it in place. Any other deleted path stays as it is (foreign)."""
    root = os.path.realpath(where)
    if root.endswith(_DELETED):
        gone = root[: -len(_DELETED)]
        for mine in d.this_roots:
            if (os.path.dirname(gone) == os.path.dirname(mine)
                    and os.path.basename(gone).startswith(f".{os.path.basename(mine)}-")):
                return mine
    return root


def _detect_herdr(d: Detected, runner, herdr_api) -> None:
    cwd = d.cwd
    if d.new_workspace:
        rows = (_json_result(runner, ["workspace", "list"], cwd) or {}).get("workspaces") or []
        hit = next((w for w in rows if w.get("label") == cockpit.WORKSPACE_LABEL), None)
        d.workspace = hit["workspace_id"] if hit else None
    d.tabs = list((_json_result(runner, ["tab", "list"], cwd) or {}).get("tabs") or [])
    try:
        rows = launch.list_panes(runner, cwd)
    except RuntimeError as exc:
        rows, d.herdr_error = [], str(exc)
    try:
        api_rows = list((herdr_api.call("pane.list", {}) or {}).get("panes") or [])
    except (HerdrApiError, HerdrUnavailable, OSError) as exc:
        api_rows, d.herdr_error = [], d.herdr_error or str(exc)
    merged: dict[str, dict] = {}
    for row in [*rows, *api_rows]:
        pid = row.get("pane_id")
        if pid:
            merged.setdefault(pid, {}).update({k: v for k, v in row.items() if v is not None})
    d.panes = list(merged.values())
    for row in d.panes:
        role = (row.get("tokens") or {}).get(CONTROL_ROLE_TOKEN)
        if role == CONTROL_ROLE_VALUE:
            where = row.get("foreground_cwd") or row.get("cwd")
            d.control_roots[row["pane_id"]] = _control_root(str(where), d) if where else None
        if role == CONTROL_ROLE_VALUE and d.control_pane is None:
            d.control_pane = row["pane_id"]
        elif role == ADVISOR_ROLE_VALUE and d.advisor_pane is None:
            d.advisor_pane = row["pane_id"]
    _detect_dead(d, runner)


def _detect_dead(d: Detected, runner) -> None:
    """Labelled control/advisor panes in the `steward` tab with no role token: dead (only a shell left) or live but
    not reported yet. Nothing is revived while a live pane of that role exists anywhere (one controller)."""
    steward_tabs = {t.get("tab_id") for t in d.tabs_labelled(launch.STEWARD_TAB_LABEL)}
    for role, label, needle in REVIVABLE:
        if role == "advisor" and not d.trajectory_on:
            continue
        if (d.control_pane if role == "control" else d.advisor_pane) is not None:
            continue
        row = next((p for p in d.panes if p.get("tab_id") in steward_tabs and p.get("label") == label), None)
        if row is None or (row.get("tokens") or {}).get(CONTROL_ROLE_TOKEN):
            continue
        if cockpit._alive(runner, row["pane_id"], needle, d.cwd):
            d.unreported[role] = row["pane_id"]
        else:
            d.dead[role] = row["pane_id"]


# -- another install (G18) ------------------------------------------------------------------------------------------

WAYS_OUT = (
    "Ways out:\n"
    "  - run this install in a separate Herdr session: `herdr --session <name>`, then `steward init` inside it\n"
    "  - or give it a separate state directory: `STEWARD_HOME=<dir> steward init`\n"
    "  - or stop the other cockpit first, then run `steward init` again"
)


def _stale_manifest(d: Detected) -> bool:
    return bool(d.recorded_root) and d.recorded_root not in d.this_roots and not d.recorded_root_exists


def foreign_reason(d: Detected) -> str | None:
    """Why this package install must not scaffold into or adopt what it found, or None (G18). Only in package mode:
    (a) install.json was written by an install at another root that still exists; (b) a live control pane (role
    token `control`) runs from another root (its foreground cwd; the control command always `cd`s into its install
    root), or from a place that cannot be read. Nothing is stopped, renamed or closed here."""
    if not d.package_mode:
        return None
    this = d.install_root
    if d.recorded_root and d.recorded_root not in d.this_roots and d.recorded_root_exists:
        return (f"STEWARD_HOME {d.home} is managed by the install at {d.recorded_root} (its install.json).\n"
                f"This install ({this}) will not scaffold into it: two installs must not share one state directory.\n"
                + WAYS_OUT)
    for pane, root in d.control_roots.items():
        if root is not None and root in d.this_roots:
            continue
        where = f"from {root}" if root else "from a directory Herdr did not report (cannot tell which install)"
        return (f"A Steward control pane is already running in this Herdr session: pane {pane}, {where}.\n"
                f"This install ({this}) will not scaffold into it or adopt it: two installs must not share one "
                "cockpit.\n" + WAYS_OUT)
    return None


# -- plan -----------------------------------------------------------------------------------------------------------


def _refuse(text: str, **detail) -> list[Action]:
    return [Action("refuse", "refuse", text, dict(detail))]


def plan_scaffold(d: Detected) -> list[Action]:
    """The ordered action list for `d`. Pure. A `refuse` action is always alone."""
    missing = prereq.required_missing(d.checks)
    if missing:
        return _refuse(
            "required prerequisites are missing: " + ", ".join(c.label for c in missing),
            reason="prerequisites", missing=[c.id for c in missing], checklist=prereq.render_text(d.checks),
        )
    if d.install_error:
        return _refuse(f"install manifest unreadable: {d.install_error}", reason="install_json")
    reason = foreign_reason(d)
    if reason:
        return _refuse(reason.splitlines()[0], reason="foreign", message="\n".join(reason.splitlines()[1:]))

    actions: list[Action] = []
    if d.package_mode and _stale_manifest(d):
        actions.append(Action("stale_install", "note",
                              f"install.json named the install at {d.recorded_root}, which no longer exists: treating "
                              "it as stale; this install takes over"))
    if d.new_workspace:
        if d.workspace:
            text = f"workspace `{cockpit.WORKSPACE_LABEL}` exists ({d.workspace}): use it"
        else:
            text = f"create workspace `{cockpit.WORKSPACE_LABEL}` (its first tab becomes `cockpit`)"
        actions.append(Action("workspace", "create", text))

    if d.home_exists and d.config_exists:
        dirs_text = f"directories already exist: {d.home} and {d.config_dir}"
    else:
        dirs_text = f"create {d.home} and {d.config_dir} (0700)"
    actions.append(Action("dirs", "create", dirs_text))

    if d.migration_pending:
        actions.append(Action("migrate", "migrate",
                              f"migrate the config in {d.config_dir} from schema {d.config_schema} to "
                              f"{migrations.CURRENT_SCHEMA} (a .bak copy is written first)"))

    actions.append(Action("templates", "create",
                          f"config templates in {d.config_dir}: steward.toml, trajectory.toml, roster.toml "
                          "(written when missing; an existing file is never overwritten)"))

    reg = d.registry_path or Path(d.config_dir) / "exceptions.toml"
    if d.registry_exists:
        actions.append(Action("exceptions_sync", "create", f"exceptions registry {reg}: add missing default sections"))
    else:
        actions.append(Action("exceptions_sync", "create", f"exceptions registry {reg}: create it with the defaults"))

    if d.dead:
        names = ", ".join(f"steward {role} ({pane})" for role, pane in d.dead.items())
        actions.append(Action("revive", "run", f"restart dead panes in place: {names}", dict(d.dead)))

    have_control = d.control_pane is not None or "control" in d.dead or "control" in d.unreported
    have_advisor = (d.advisor_pane is not None or "advisor" in d.dead or "advisor" in d.unreported
                    or not d.trajectory_on)
    tab = f"tab `{launch.STEWARD_TAB_LABEL}`"
    if have_control and have_advisor:
        ca = f"{tab}: control pane" + (" and advisor pane" if d.trajectory_on else "") + " already running"
    elif not d.trajectory_on:
        ca = f"{tab}: control only (trajectory off): start the control pane"
    elif have_control:
        ca = f"{tab}: control pane already running; start the advisor pane beside it"
    else:
        ca = f"{tab}: start the control pane (left) and the advisor pane beside it"
    actions.append(Action("control_advisor", "run", ca))

    cockpit_tab = f"tab `{cockpit.COCKPIT_TAB_LABEL}`"
    has_cockpit = bool(d.tabs_labelled(cockpit.COCKPIT_TAB_LABEL)) and not (d.new_workspace and not d.workspace)
    if has_cockpit:
        ck = f"{cockpit_tab}: already there (missing panes added, dead ones restarted in place)"
    elif d.no_operator:
        ck = f"{cockpit_tab}: `{cockpit.DASH_LABEL}` status pane, dash only (--no-operator)"
    else:
        ck = (f"{cockpit_tab}: operator pane (claude, in {d.cwd}) left and `{cockpit.DASH_LABEL}` status pane "
              "right")
    actions.append(Action("cockpit", "run", ck))

    if d.upgrading:
        old = (d.install or {}).get("version")
        why = f"after the upgrade from {old} to {d.version}"
        actions.append(Action("restart_control", "restart",
                              f"{why}: restart the control pane in place (deferred while it is busy)"))
        if d.trajectory_on:
            actions.append(Action("restart_advisor", "restart",
                                  f"{why}: restart the advisor pane in place (deferred while a review awaits advice)"))
        actions.append(Action("restart_dash", "restart", f"{why}: restart the `{cockpit.DASH_LABEL}` pane in place"))
        if d.jobs_running:
            actions.append(Action("jobs_note", "note", JOBS_NOTE))

    actions.append(Action("stamp", "stamp",
                          f"record the install in {Path(d.home) / installstate.FILE_NAME} (last, only when every "
                          "step succeeded)"))
    return actions


def render_plan(actions: list[Action]) -> str:
    """What `--dry-run` prints (and what the intro shows): the numbered action list, or the refusal."""
    if actions and actions[0].kind == "refuse":
        refuse = actions[0]
        if refuse.detail.get("message"):
            return "\n".join(["Steward will not set up this workspace.", refuse.text, refuse.detail["message"]])
        lines = [f"Steward will not set up this workspace: {refuse.text}"]
        if refuse.detail.get("checklist"):
            lines += ["", refuse.detail["checklist"], "", "Fix the required items, then run `steward init` again."]
        return "\n".join(lines)
    lines = ["Steward will set up this Herdr workspace:"]
    lines += [f"  {n}. {a.text}" for n, a in enumerate(actions, start=1)]
    return "\n".join(lines)


# -- apply ----------------------------------------------------------------------------------------------------------


@dataclass
class Ctx:
    """What `apply_scaffold` needs to act: the Herdr seams and the detected state."""

    runner: Any
    herdr_api: Any
    sock: str
    detected: Detected
    workspace: str | None = None
    which: Callable[[str], str | None] | None = None
    reviewer: str = "claude"
    sleep: Callable[[float], None] | None = None
    kill: Callable[[int, int], None] | None = None  # os.kill by default (control and advisor are stopped by signal)
    cmdline: Callable[[int], str | None] | None = None  # /proc/<pid>/cmdline by default
    fresh: set = field(default_factory=set)  # pane ids started or revived by this run (already on this code)
    tabs: list = field(default_factory=list)  # tab labels this run ensured
    prelabelled: set = field(default_factory=set)  # empty panes this run labelled for the cockpit step to start
    revived: dict = field(default_factory=dict)  # role -> pane revived in place by this run

    def __post_init__(self):
        if self.workspace is None:
            self.workspace = self.detected.workspace


class StepFailed(RuntimeError):
    pass


def _working_state(d: Detected) -> dict:
    if d.install is not None:
        return dict(d.install)
    return installstate.new_state(d.version, d.install_root, d.python)


def _step_workspace(ctx: Ctx, out: dict) -> None:
    """`--new-workspace`: use the workspace labelled `steward`, or create it. A new workspace's first tab becomes the
    `cockpit` tab and its root pane is labelled for the cockpit (nothing is typed into it here; the `cockpit` step
    starts it). Writes no file."""
    d = ctx.detected
    if d.workspace:
        ctx.workspace = d.workspace
        ctx.runner.run(["herdr", "workspace", "focus", d.workspace], cwd=d.cwd)
        out["lines"].append(f"workspace {cockpit.WORKSPACE_LABEL} already there: {d.workspace}")
        return
    res = ctx.runner.run(["herdr", "workspace", "create", "--label", cockpit.WORKSPACE_LABEL, "--cwd", d.cwd,
                          "--focus"], cwd=d.cwd)
    if res.exit_code != 0:
        raise StepFailed(f"herdr workspace create failed: {res.stderr.strip()}")
    try:
        data = json.loads(res.stdout)["result"]
        workspace, tab_id, root = (data["workspace"]["workspace_id"], data["tab"]["tab_id"],
                                   data["root_pane"]["pane_id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise StepFailed(f"unexpected herdr workspace create output: {exc}") from exc
    ctx.runner.run(["herdr", "tab", "rename", tab_id, cockpit.COCKPIT_TAB_LABEL], cwd=d.cwd)
    label = cockpit.DASH_LABEL if d.no_operator else cockpit.OPERATOR_LABEL
    ctx.runner.run(["herdr", "pane", "rename", root, label], cwd=d.cwd)
    ctx.workspace = workspace
    ctx.prelabelled.add(root)
    out["lines"].append(f"workspace {cockpit.WORKSPACE_LABEL} created: {workspace}")
    out["created"].append("workspace")


def _step_dirs(ctx: Ctx, out: dict) -> None:
    d = ctx.detected
    for made in templates_io.ensure_dirs(d.home, d.config_dir):
        out["lines"].append(f"created {made}")
        out["created"].append(str(made))


def _step_migrate(ctx: Ctx, out: dict, state: dict) -> None:
    d = ctx.detected
    from_version = str((d.install or {}).get("version") or d.version)
    try:
        changes = migrations.run(d.config_dir, from_version, target=migrations.CURRENT_SCHEMA,
                                 migrations=migrations.MIGRATIONS, log=out["lines"].append)
    except migrations.MigrationError as exc:
        raise StepFailed(str(exc)) from exc
    state["config_schema"] = migrations.CURRENT_SCHEMA
    out["migrations"] = changes


def _step_templates(ctx: Ctx, out: dict, state: dict) -> None:
    d = ctx.detected
    outcomes = templates_io.write_templates(d.config_dir, state, version=d.version)
    out["templates"] = [o.action for o in outcomes]
    for o in outcomes:
        if o.action == templates_io.CREATED:
            out["lines"].append(f"created {o.path}")
            out["created"].append(str(o.path))
        elif o.action == templates_io.NEW_WRITTEN:
            out["lines"].append(f"a newer template was written beside your file: {o.path.name} (compare, then merge "
                                "what you want)")


def _step_exceptions(ctx: Ctx, out: dict, state: dict) -> None:
    path = exceptions.registry_path()
    existed = path.exists()
    added = exceptions.sync()
    out["exceptions_added"] = added
    if not existed and path.exists():
        installstate.record_file(state, path, "config")
        out["lines"].append(f"created {path}")
        out["created"].append(str(path))
    if added:
        out["lines"].append(f"exceptions registry: added {', '.join(added)}")


def revive_dead_panes(ctx: Ctx) -> dict:
    """Re-run the control / advisor command in the labelled pane whose process is gone (same pane id, no second
    `steward` tab). Returns role -> pane id of every pane revived."""
    d = ctx.detected
    revived: dict = {}
    for role, pane in d.dead.items():
        if role == "control":
            launch.start_control_on(ctx.runner, ctx.sock, pane)
        elif role == "advisor":
            advisor_pane.start_advisor_on(ctx.runner, ctx.sock, pane, ctx.reviewer)
        else:
            continue
        revived[role] = pane
        ctx.fresh.add(pane)
    ctx.revived.update(revived)
    return revived


def _step_revive(ctx: Ctx, out: dict) -> None:
    revive_dead_panes(ctx)


def _step_control_advisor(ctx: Ctx, out: dict) -> None:
    d = ctx.detected
    known = {**d.unreported, **ctx.revived}
    extra = {f"known_{role}": pane for role, pane in known.items() if pane}
    res = launch.ensure_steward_up(ctx.runner, ctx.herdr_api, ctx.sock, ctx.workspace, trajectory_on=d.trajectory_on,
                                   reviewer=ctx.reviewer, **extra)
    if res.get("error"):
        raise StepFailed(res["error"])
    created = res.get("created", [])
    if "tab_id" in res:
        out["lines"].append(f"steward tab created: {res['tab_id']}")
        out["created"].append(f"tab:{launch.STEWARD_TAB_LABEL}")
    ctx.tabs.append(launch.STEWARD_TAB_LABEL)
    for role in ("control", "advisor"):
        pane = res.get(f"{role}_pane")
        if pane is None:
            continue
        if role in created:
            word = "started"
            ctx.fresh.add(pane)
            out["created"].append(f"pane:{role}")
        elif role in ctx.revived:
            word = "restarted in place"
        else:
            word = "already running"
        out["lines"].append(f"{role} pane {word}: {pane}")


def _step_cockpit(ctx: Ctx, out: dict) -> None:
    d = ctx.detected
    res = cockpit.ensure_cockpit(ctx.runner, ctx.herdr_api, ctx.sock, ctx.workspace, cwd=d.cwd,
                                 operator=not d.no_operator, which=ctx.which)
    if res.get("error"):
        raise StepFailed(res["error"])
    created, restarted = res.get("created", []), res.get("restarted", [])
    ctx.tabs.append(cockpit.COCKPIT_TAB_LABEL)
    out["cockpit"] = res

    def word(what: str) -> str:
        if what in restarted and res.get(f"{what}_pane") in ctx.prelabelled:
            return "started"
        if what in restarted:
            return "restarted in place"
        return "started" if what in created else "already running"

    if "tab" in created or ctx.prelabelled:
        out["lines"].append(f"cockpit tab created: {res.get('tab_id')}")
        out["created"].append(f"tab:{cockpit.COCKPIT_TAB_LABEL}")
    else:
        out["lines"].append(f"cockpit tab already there: {res.get('tab_id')}")
    if res.get("operator_pane"):
        out["lines"].append(f"operator pane {word('operator')}: {res['operator_pane']}")
    elif res.get("operator_note"):
        out["lines"].append(res["operator_note"])
    out["lines"].append(f"dash pane {word('dash')}: {res.get('dash_pane')}")
    for what, key in (("operator", "operator_pane"), ("dash", "dash_pane")):
        if (what in created or what in restarted) and res.get(key):
            ctx.fresh.add(res[key])


def control_idle(ctx: Ctx, pane_id: str) -> bool:
    """The control pane processes frames one at a time, so an answered no-op `ping` means no frame is in flight."""
    from steward import health
    from steward.store import Store

    try:
        return health._ping(Store(ctx.detected.home), ctx.herdr_api, timeout_s=CONTROL_PING_TIMEOUT_S,
                            sleep=time.sleep, clock=time.time) is None
    except Exception:  # noqa: BLE001 - cannot prove idle: defer
        return False


def advisor_idle(ctx: Ctx, pane_id: str) -> bool:
    """Idle when no review packet of a live job is still waiting for advice."""
    from steward.store import Store

    try:
        return not advisor_pane.pending_reviews(Store(ctx.detected.home))
    except Exception:  # noqa: BLE001
        return False


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _current_panes(ctx: Ctx) -> tuple[list[dict], list[dict]]:
    tabs = list((_json_result(ctx.runner, ["tab", "list"], ctx.detected.cwd) or {}).get("tabs") or [])
    try:
        panes = launch.list_panes(ctx.runner, ctx.detected.cwd)
    except RuntimeError:
        panes = []
    return tabs, panes


def _role_pane(ctx: Ctx, role: str) -> str | None:
    """The pane to restart: control/advisor by role token (else by label in the `steward` tab), the status pane by
    label in the `cockpit` tab of this workspace."""
    tabs, panes = _current_panes(ctx)

    def in_tabs(label: str, tab_label: str) -> str | None:
        ids = {t.get("tab_id") for t in tabs if t.get("label") == tab_label
               and (not ctx.workspace or t.get("workspace_id") in (None, ctx.workspace))}
        row = next((p for p in panes if p.get("tab_id") in ids and p.get("label") == label), None)
        return row.get("pane_id") if row else None

    if role == "dash":
        return in_tabs(cockpit.DASH_LABEL, cockpit.COCKPIT_TAB_LABEL)
    finder = launch.find_control_pane if role == "control" else advisor_pane.find_advisor_pane
    return finder(ctx.herdr_api) or in_tabs(f"steward {role}", launch.STEWARD_TAB_LABEL)


STOP_WAIT_S = 10.0
_NEEDLES = {"control": "steward.cli control", "advisor": "steward.cli advisor"}


def _stop_pane_process(ctx: Ctx, pane: str, needle: str) -> bool:
    """Stop the `steward control` / `steward advisor` process in `pane` by SIGINT, then SIGTERM, and wait for it to
    exit. Both put their tty in raw mode (the Herdr mailbox), so a typed Ctrl-C is only a byte they read: it never
    reaches them as a signal (found by the live upgrade test, 19-09). The pid comes from `herdr pane process-info`
    and is signalled only when its argv names `needle` and `/proc` still shows that program at the pid. False when
    there is no such pid (an older Herdr, a pane with only a shell): the caller falls back to the keystroke."""
    import signal

    kill = ctx.kill or os.kill
    cmdline = ctx.cmdline or launch._proc_cmdline
    info = _json_result(ctx.runner, ["pane", "process-info", "--pane", pane], ctx.detected.cwd) or {}
    pid = None
    for proc in ((info.get("process_info") or {}).get("foreground_processes")) or []:
        argv = proc.get("argv") if isinstance(proc, dict) else None
        if isinstance(argv, list) and needle in " ".join(str(a) for a in argv) and isinstance(proc.get("pid"), int):
            pid = proc["pid"]
            break
    if pid is None or needle not in (cmdline(pid) or ""):
        return False
    sleep = ctx.sleep or _sleep
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            kill(pid, sig)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        waited = 0.0
        while waited < STOP_WAIT_S:
            if needle not in (cmdline(pid) or ""):
                return True
            sleep(0.25)
            waited += 0.25
    return needle not in (cmdline(pid) or "")


def _restart(ctx: Ctx, role: str, out: dict) -> None:
    """Stop the pane's process (a signal for control and advisor, Ctrl-C for the status pane), settle, re-run the
    pane's command in the same pane. Control and advisor only when idle; otherwise the
    restart is deferred with one line and the version is not stamped, so the next run retries."""
    pane = _role_pane(ctx, role)
    if pane is None or pane in ctx.fresh:
        return
    probe = {"control": control_idle, "advisor": advisor_idle}.get(role)
    if probe is not None and not probe(ctx, pane):
        out["deferred"].append(role)
        out["lines"].append(f"{role} pane busy: restart deferred, run steward up again")
        return
    cwd = ctx.detected.cwd
    if not (role in _NEEDLES and _stop_pane_process(ctx, pane, _NEEDLES[role])):
        ctx.runner.run(["herdr", "pane", "send-keys", pane, "ctrl+c"], cwd=cwd)
    (ctx.sleep or _sleep)(SETTLE_S)
    if role == "control":
        launch.start_control_on(ctx.runner, ctx.sock, pane)
    elif role == "advisor":
        advisor_pane.start_advisor_on(ctx.runner, ctx.sock, pane, ctx.reviewer)
    else:
        ctx.runner.run(["herdr", "pane", "run", pane, cockpit.dash_command()], cwd=cwd)
    ctx.fresh.add(pane)
    out.setdefault("restarted", []).append(role)
    out["lines"].append(f"{role} pane restarted: {pane}")


def _step_services(ctx: Ctx, out: dict, state: dict) -> None:
    """G14: the `services` tab for components running without systemd (created, revived in place; never twice)."""
    res = components.ensure_services_tab(ctx.runner, ctx.workspace, state, ctx.detected.cwd)
    if res.get("error"):
        out["lines"].append(f"services tab: {res['error']}")
        return
    for kind, word in (("created", "started"), ("revived", "restarted in place")):
        for target in res[kind]:
            if target != "tab":
                out["lines"].append(f"service {target} {word}: pane {components.pane_label(target)}")


def _step_stamp(ctx: Ctx, out: dict, state: dict) -> None:
    d = ctx.detected
    if not out.get("deferred"):
        state["version"] = d.version
    state["install_root"] = d.install_root
    state["python"] = d.python
    tabs = list(state.get("tabs") or [])
    for label in ctx.tabs:
        if label not in tabs:
            tabs.append(label)
    state["tabs"] = tabs
    path = installstate.save(state, d.home)
    out["stamped"] = str(path)


def apply_scaffold(actions: list[Action], ctx: Ctx) -> dict:
    """Execute `actions` in order. Returns `{"lines", "created", "errors", "deferred", ...}`; a failing step stops the
    run, is reported in `errors`, and install.json is not stamped."""
    out: dict = {"lines": [], "created": [], "errors": [], "deferred": []}
    if actions and actions[0].kind == "refuse":
        out["errors"].append(actions[0].text)
        return out
    state = _working_state(ctx.detected)
    steps: dict[str, Callable[[], None]] = {
        "workspace": lambda: _step_workspace(ctx, out),
        "dirs": lambda: _step_dirs(ctx, out),
        "migrate": lambda: _step_migrate(ctx, out, state),
        "templates": lambda: _step_templates(ctx, out, state),
        "exceptions_sync": lambda: _step_exceptions(ctx, out, state),
        "revive": lambda: _step_revive(ctx, out),
        "control_advisor": lambda: _step_control_advisor(ctx, out),
        "cockpit": lambda: _step_cockpit(ctx, out),
        "restart_control": lambda: _restart(ctx, "control", out),
        "restart_advisor": lambda: _restart(ctx, "advisor", out),
        "restart_dash": lambda: _restart(ctx, "dash", out),
        "stamp": lambda: (_step_services(ctx, out, state), _step_stamp(ctx, out, state)),
    }
    for action in actions:
        if action.kind == "note":
            out["lines"].append(action.text)
            continue
        step = steps.get(action.id)
        if step is None:
            continue
        try:
            step()
        except StepFailed as exc:
            out["errors"].append(f"{action.id}: {exc}")
            return out
        except OSError as exc:
            out["errors"].append(f"{action.id}: {type(exc).__name__}: {exc}")
            return out
    return out


def upgrade_if_needed(ctx: Ctx) -> dict:
    """`steward up` with an install.json (G15, G17): when the installed version differs from the running one (or a
    config migration is pending) run the migrations (backup first), refresh the templates (a changed one is written
    as `.new`), restart control, advisor and dash in place when idle, say what happens to running jobs, and stamp
    the new version unless a restart was deferred. Returns the same shape as `apply_scaffold`."""
    d = ctx.detected
    out: dict = {"lines": [], "created": [], "errors": [], "deferred": [], "restarted": []}
    if d.install is None:
        return out
    state = _working_state(d)
    if not (d.upgrading or d.migration_pending):
        tabs = list(state.get("tabs") or [])
        try:
            _step_services(ctx, out, state)
            if list(state.get("tabs") or []) != tabs:
                installstate.save(state, d.home)
        except OSError as exc:
            out["errors"].append(f"{type(exc).__name__}: {exc}")
        return out
    try:
        if d.migration_pending:
            _step_migrate(ctx, out, state)
        _step_templates(ctx, out, state)
        if d.upgrading:
            for role in ("control", "advisor", "dash"):
                if role == "advisor" and not d.trajectory_on:
                    continue
                _restart(ctx, role, out)
            if d.jobs_running:
                out["lines"].append(JOBS_NOTE)
        _step_services(ctx, out, state)
        _step_stamp(ctx, out, state)
    except StepFailed as exc:
        out["errors"].append(str(exc))
    except OSError as exc:
        out["errors"].append(f"{type(exc).__name__}: {exc}")
    return out


def first_use_line(workspace_label: str | None = None) -> str:
    return (f"Ready. In the operator pane (tab {cockpit.COCKPIT_TAB_LABEL}), say: {FIRST_USE}")

