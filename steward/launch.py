"""Fabric-native job launch (OPX-05 carried item, 11-CONTEXT's closing line).

`launch_tab()` and `ensure_control_pane()` are the two functions
`steward.cli`'s `launch` command and its control-pane handler
(`_h_launch`) both call — the same code either runs directly (a future
non-relayed caller) or inside `steward control`, once a `launch` cmd frame
reaches it (11-02: `launch` is now relayed like every other write verb).

Two worker layouts for the job's own tab (one job = one tab, USERLOOP-LOG
bug 2c):
  - `fabric` (default): the runner pane runs `steward run --worker fabric`;
    `FabricWorker` itself splits off the (larger, review-sized) worker pane
    once dispatched (`steward.cli._fabric_spawn_pane`'s existing `--ratio
    0.2` split) — this module never splits a second pane for the worker,
    so a fabric launch is exactly two panes: runner + worker.
  - `herdr-tui` (legacy, `--worker herdr-tui`): the runner pane runs
    `steward run --worker herdr`, which drives the old TUI-scraping
    `HerdrWorker`'s own pane split — same one-split shape.

`ensure_control_pane()` is the split+start `steward control` does
interactively for `--ensure`, reused here so a fresh `launch` always leaves
a live control pane in the workspace behind it (OPX-01): idempotent via
`steward.control_pane.find_control_pane` — a control pane already reachable
is left alone, never duplicated.

`ensure_advisor_pane()` re-exports `steward.advisor_pane.ensure_advisor_pane`
(the split+start logic actually lives there — see its own module docstring,
12-02-SUMMARY's Deviations) so `_h_launch` (TRJ-12: "steward launch ensures
an advisor pane") can call it as `launch.ensure_advisor_pane(...)`, the same
shape as `ensure_control_pane`, without duplicating the pane-creation code.

Every herdr call goes through the injected `CommandRunner` (SP-1); nothing
here sends a Herdr frame itself except through the injected `herdr_api`
passed to `ensure_control_pane` (for `pane.list`/`pane.report_metadata` —
ordinary pane-lifecycle management, not a frame conversation, so SP-31
doesn't apply to any of it).
"""

from __future__ import annotations

import json
import os
import shlex
import sqlite3
import sys
from pathlib import Path
from typing import Any

from steward import deployed, pkgenv, proclock
from steward.advisor_pane import ensure_advisor_pane as _ensure_advisor_pane
from steward.advisor_pane import find_advisor_pane
from steward.control_pane import find_control_pane
from steward import tabnames
from steward.runner_exec import Runner as CommandRunner
from steward.store import resolve_home

_STEWARD_ROOT = Path(__file__).resolve().parent.parent

# NB-04 (SP-36): the one dedicated tab `steward up` creates the cockpit in,
# so a fresh control/advisor pair never lands in the caller's own tab or a
# job tab.
STEWARD_TAB_LABEL = "steward"


def worker_run_command(
    job_id: str,
    *,
    worker: str,
    kind: str,
    pane: str,
    sock: str | None,
    agent_args: list[str],
    interval: float,
    close_pane_on_exit: bool = True,
    root: Path | None = None,
    live_root: Path | None = None,
) -> str:
    """The shell command line typed into the job's own runner pane.

    `root` is the code tree the runner starts from (default `code_root()`: the deployed revision, issue W);
    `live_root` is exported as STEWARD_LIVE_ROOT when `root` is a pinned tree.

    `worker="fabric"` never passes `--pane`: the new pane's own shell gets
    `$HERDR_PANE_ID` set by Herdr, and `steward run --worker fabric` already
    falls back to it (`steward.cli._fabric_run_args`) — the same pattern
    `steward shim`'s own `--pane` default uses. `--sock` is passed
    explicitly when known, mirroring `steward.cli._shim_run_command`, rather
    than relying solely on the new pane's own `$HERDR_SOCKET_PATH`.

    `close_pane_on_exit` (POL-03, default True) appends `--close-pane-on-exit`
    so the runner closes its own pane once the job settles into
    ACCEPTED/STOPPED/FAILED/RELEASED; `steward launch --keep-pane` passes
    `False` to opt out. Placed before `--agent-arg` so a caller matching on
    the command's trailing `--agent-arg=...` tail is unaffected either way.
    """
    if worker == "fabric" and kind == "codex":
        raise ValueError("--worker fabric does not support --kind codex yet (WRK-01 scope); use --worker herdr-tui")

    parts = [sys.executable, "-m", "steward.cli", "run", job_id]
    if worker == "fabric":
        parts += ["--worker", "fabric", "--agent", "claude"]
        if sock:
            parts += ["--sock", sock]
    else:
        parts += ["--worker", "herdr", "--kind", kind, "--pane", pane]
    parts += ["--interval", str(interval)]
    if close_pane_on_exit:
        parts += ["--close-pane-on-exit"]
    parts += [f"--agent-arg={a}" for a in agent_args]
    if root is None:
        root, live_root = code_root()
    return (_env_exports() + deployed.export_line(live_root) + f"cd {shlex.quote(str(root))} && "
            + " ".join(shlex.quote(p) for p in parts))


def code_root() -> tuple[Path, Path | None]:
    """(tree to start a Steward process from, live root to export or None): the deployed revision (`steward.deployed`,
    issue W) — this process's own tree when that cannot be told (a normal launch never fails on it; healers call
    `deployed.launch_root` themselves and hold instead)."""
    from steward import stability

    try:
        return deployed.launch_root(resolve_home(), stability.LIVE_ROOT)
    except Exception:  # noqa: BLE001 - unknown deployed revision, git trouble: today's behaviour
        return _STEWARD_ROOT, None


def _env_exports() -> str:
    """A new pane's shell does not inherit the caller's environment, so the
    Steward env that selects the store and the exceptions registry is
    re-exported explicitly (same bug class as the 10-02 shim fix; found in
    11-03 acceptance: the runner looked up the job in the default store)."""
    out = ""
    for name in ("STEWARD_HOME", "STEWARD_EXCEPTIONS"):
        value = os.environ.get(name)
        if value:
            out += f"export {name}={shlex.quote(value)} && "
    # Package mode (STEWARD_INSTALL_ROOT set): PYTHONPATH and the forwarded variables. Empty otherwise.
    return out + pkgenv.env_exports(skip=("STEWARD_HOME", "STEWARD_EXCEPTIONS")) + tmpdir_export()


def tmpdir_export() -> str:
    """A caller-chosen TMPDIR reaches the panes it starts (scratch off a quota-limited /tmp). Unset: nothing."""
    value = os.environ.get("TMPDIR")
    return f"export TMPDIR={shlex.quote(value)} && " if value else ""


def launch_label(store: Any, job: Any) -> str:
    """The grammar label a job's tab is created with: a running glyph and the job's short name."""
    return tabnames.tab_label(tabnames.RUNNING, tabnames.job_name(store, job))


def launch_tab(
    runner_exec: CommandRunner,
    job: Any,
    *,
    workspace: str,
    worker: str = "fabric",
    kind: str = "claude",
    agent_args: list[str],
    interval: float,
    sock: str | None = None,
    close_pane_on_exit: bool = True,
    store: Any = None,
    name: str | None = None,
    root: Path | None = None,
    live_root: Path | None = None,
) -> tuple[str, str]:
    """Open a Herdr tab for `job` (one job = one tab) and start its runner in
    it. Returns `(tab_id, runner_pane_id)`. Everything goes through the
    injected `runner_exec` (SP-1) — never a bare subprocess call.

    The tab is titled `● <short name>` (`steward.tabnames`): `name` when the
    operator passed one (stored as the job's `short_name`), else the stored or
    goal-derived name. Without a `store` the name is only derived, not kept."""
    id8 = job.job_id[:8]
    if store is not None and name:
        store.kv_set(job.job_id, tabnames.NAME_KEY, " ".join(name.split()))
    short = tabnames.job_name(store, job) if store is not None else (name or tabnames.derive_name(job.goal))
    label = tabnames.tab_label(tabnames.RUNNING, short)
    create = runner_exec.run(
        ["herdr", "tab", "create", "--workspace", workspace, "--cwd", job.repo, "--label", label, "--no-focus"],
        cwd=job.repo,
    )
    if create.exit_code != 0:
        raise RuntimeError(f"herdr tab create failed: {create.stderr.strip()}")
    data = json.loads(create.stdout)
    tab_id = data["result"]["tab"]["tab_id"]
    runner_pane = data["result"]["root_pane"]["pane_id"]

    try:
        runner_exec.run(["herdr", "pane", "rename", runner_pane, f"runner {id8}"], cwd=job.repo)
        command = worker_run_command(
            job.job_id, worker=worker, kind=kind, pane=runner_pane, sock=sock,
            agent_args=agent_args, interval=interval, close_pane_on_exit=close_pane_on_exit, root=root,
            live_root=live_root,
        )
        started = runner_exec.run(["herdr", "pane", "run", runner_pane, command], cwd=job.repo)
        if getattr(started, "exit_code", 0) != 0:
            raise RuntimeError(f"herdr pane run failed: {(started.stderr or started.stdout or '').strip()[:200]}")
    except Exception as exc:
        # a tab whose runner never started would strand the job: close it before reporting the failure
        try:
            runner_exec.run(["herdr", "tab", "close", tab_id], cwd=job.repo)
        except Exception:  # noqa: BLE001 - cleanup is best-effort; the original error is what matters
            pass
        raise RuntimeError(f"{exc} (partial tab {tab_id} closed)") from exc

    # Everything responsible for the job lives in its tab: a live watchers
    # feed goes right of the runner. Best-effort — never fails the launch.
    try:
        open_watchers_pane(runner_exec, job.job_id, runner_pane, cwd=job.repo)
    except Exception:  # noqa: BLE001
        pass

    return tab_id, runner_pane


# Share of the runner pane that the runner keeps when the watchers feed is
# split off to its right (so the feed gets ~45% of the width).
WATCHERS_RATIO = "0.55"


def watchers_label(job_id: str) -> str:
    return f"watchers {job_id[:8]}"


def watchers_command(job_id: str) -> str:
    """The shell line that runs the live feed in a new pane (same env export
    and cwd prefix the runner command uses)."""
    parts = [sys.executable, "-m", "steward.cli", "watchers", job_id, "--follow"]
    root, live_root = code_root()
    return (_env_exports() + deployed.export_line(live_root) + f"cd {shlex.quote(str(root))} && "
            + " ".join(shlex.quote(p) for p in parts))


def list_panes(runner_exec: CommandRunner, cwd: str | Path) -> list[dict]:
    """`herdr pane list` -> the pane dicts (label, pane_id, tab_id, ...)."""
    res = runner_exec.run(["herdr", "pane", "list"], cwd=cwd)
    if res.exit_code != 0:
        raise RuntimeError(f"herdr pane list failed: {res.stderr.strip()}")
    try:
        return json.loads(res.stdout)["result"]["panes"]
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"unexpected herdr pane list output: {exc}") from exc


def find_pane_by_label(panes: list[dict], label: str) -> dict | None:
    return next((p for p in panes if p.get("label") == label), None)


def open_watchers_pane(runner_exec: CommandRunner, job_id: str, runner_pane: str, *, cwd: str | Path) -> str:
    """Split `runner_pane` to the right (no focus), label the new pane
    `watchers <job8>` and run the live feed in it. Returns the new pane id;
    raises RuntimeError when the split fails."""
    split = runner_exec.run(
        ["herdr", "pane", "split", "--pane", runner_pane, "--direction", "right",
         "--ratio", WATCHERS_RATIO, "--cwd", str(cwd), "--no-focus"],
        cwd=cwd,
    )
    if split.exit_code != 0:
        raise RuntimeError(f"herdr pane split failed: {split.stderr.strip()}")
    try:
        new_pane = json.loads(split.stdout)["result"]["pane"]["pane_id"]
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"unexpected herdr pane split output: {exc}") from exc
    runner_exec.run(["herdr", "pane", "rename", new_pane, watchers_label(job_id)], cwd=cwd)
    runner_exec.run(["herdr", "pane", "run", new_pane, watchers_command(job_id)], cwd=cwd)
    return new_pane


def start_control_on(runner_exec: CommandRunner, sock: str, pane_id: str, *, root: Path | None = None,
                     live_root: Path | None = None) -> None:
    """Renames `pane_id` and starts `steward control --sock ... --pane ...`
    on it — the half of `ensure_control_pane` that's the same whether the
    pane came from a split (legacy caller-pane shape), is a brand-new
    tab's root pane (`ensure_steward_up`, NB-04), or is an existing pane
    revived or restarted in place (`steward init`/`up`, Phase 19 G5)."""
    runner_exec.run(["herdr", "pane", "rename", pane_id, "steward control"], cwd=Path.cwd())

    steward_home = os.environ.get("STEWARD_HOME") or str(resolve_home())
    if root is None:
        root, live_root = code_root()
    parts = [sys.executable, "-m", "steward.cli", "control", "--sock", sock, "--pane", pane_id]
    exceptions_path = os.environ.get("STEWARD_EXCEPTIONS")
    command = (
        f"export STEWARD_HOME={shlex.quote(steward_home)} && "
        + (f"export STEWARD_EXCEPTIONS={shlex.quote(exceptions_path)} && " if exceptions_path else "")
        + pkgenv.env_exports(skip=("STEWARD_HOME", "STEWARD_EXCEPTIONS"))
        + deployed.export_line(live_root)
        + f"cd {shlex.quote(str(root))} && "
        + " ".join(shlex.quote(p) for p in parts)
    )
    runner_exec.run(["herdr", "pane", "run", pane_id, command], cwd=Path.cwd())


# The old private name; `ensure_steward_up`/`ensure_control_pane` call it through this module global.
_start_control_on = start_control_on


def ensure_control_pane(
    runner_exec: CommandRunner, herdr_api: Any, sock: str, caller_pane: str
) -> dict:
    """Make sure a `steward control` pane is live in the workspace (OPX-01),
    idempotent: a pane already carrying the `steward_role=control` token is
    left alone. Otherwise splits one off `caller_pane` (same shape
    `steward control --ensure` uses interactively) and starts it there.

    Returns `{"pane_id": ..., "created": bool}` on success, or
    `{"pane_id": None, "created": False, "error": "..."}` on failure — never
    raises, so a launch's tab/runner already having been created is never
    rolled back just because the control pane couldn't be confirmed.

    Kept for callers that already have a specific pane to split off of
    (`steward control --ensure` with `--pane`); `_h_launch` and every other
    "no control pane" self-heal go through `ensure_steward_up` instead
    (NB-04, SP-36) so a fresh pair never lands in the caller's own tab or a
    job tab."""
    existing = find_control_pane(herdr_api)
    if existing is not None:
        return {"pane_id": existing, "created": False}

    split = runner_exec.run(
        ["herdr", "pane", "split", "--pane", caller_pane, "--direction", "down", "--ratio", "0.15", "--no-focus"],
        cwd=Path.cwd(),
    )
    if split.exit_code != 0:
        return {"pane_id": None, "created": False, "error": f"herdr pane split failed: {split.stderr.strip()}"}
    try:
        data = json.loads(split.stdout)
        new_pane = data["result"]["pane"]["pane_id"]
    except (ValueError, KeyError) as exc:
        return {"pane_id": None, "created": False, "error": f"unexpected herdr pane split output: {exc}"}

    _start_control_on(runner_exec, sock, new_pane)
    return {"pane_id": new_pane, "created": True}


def ensure_advisor_pane(
    runner_exec: CommandRunner, herdr_api: Any, sock: str, caller_pane: str, *, reviewer: str = "claude"
) -> dict:
    """Re-exports `steward.advisor_pane.ensure_advisor_pane` (TRJ-12) under
    `launch.ensure_advisor_pane`, the same shape as `ensure_control_pane`,
    so `_h_launch` can ensure both cockpit panes from one module."""
    return _ensure_advisor_pane(runner_exec, herdr_api, sock, caller_pane, reviewer=reviewer)


def ensure_steward_up(
    runner_exec: CommandRunner,
    herdr_api: Any,
    sock: str,
    workspace: str,
    *,
    trajectory_on: bool = True,
    reviewer: str = "claude",
    known_control: str | None = None,
    known_advisor: str | None = None,
) -> dict:
    """`steward up` (NB-04, SP-36 "defaults, not blockers"): idempotently
    make sure a `steward control` pane — and, unless trajectory mode is
    off, a `steward advisor` pane — are live somewhere in `workspace`,
    reused wherever they already are (by `steward_role` token). A brand-new
    pair never lands in the caller's own tab or a job tab: when no control
    pane exists yet, a dedicated tab labelled "steward" is created first
    and the control pane becomes its root (left) pane; the advisor (if
    needed) is then split off *that* pane (right) — still inside the
    steward tab, wherever `up` itself was called from.

    Returns `{"control_pane": ..., "advisor_pane": ..., "created": [...]}`
    (`created` lists which of "control"/"advisor" were freshly started this
    call) plus `"tab_id"` when a new tab was created, or `"error"` — never
    raises.

    `known_control` / `known_advisor` name a pane the caller has just
    revived in place (its process has not reported its role token yet), so
    it is used instead of starting a second one (Phase 19 G5)."""
    result: dict = {"created": []}

    control_pane_id = known_control or find_control_pane(herdr_api)
    if control_pane_id is None:
        create = runner_exec.run(
            ["herdr", "tab", "create", "--workspace", workspace, "--label", STEWARD_TAB_LABEL, "--no-focus"],
            cwd=Path.cwd(),
        )
        if create.exit_code != 0:
            return {"control_pane": None, "created": [], "error": f"herdr tab create failed: {create.stderr.strip()}"}
        try:
            data = json.loads(create.stdout)
            tab_id = data["result"]["tab"]["tab_id"]
            control_pane_id = data["result"]["root_pane"]["pane_id"]
        except (ValueError, KeyError) as exc:
            return {"control_pane": None, "created": [], "error": f"unexpected herdr tab create output: {exc}"}
        _start_control_on(runner_exec, sock, control_pane_id)
        result["tab_id"] = tab_id
        result["created"].append("control")
    result["control_pane"] = control_pane_id

    if trajectory_on:
        advisor_pane_id = known_advisor or find_advisor_pane(herdr_api)
        if advisor_pane_id is None:
            ensured = _ensure_advisor_pane(runner_exec, herdr_api, sock, control_pane_id, reviewer=reviewer)
            advisor_pane_id = ensured.get("pane_id")
            if ensured.get("error"):
                result["error"] = ensured["error"]
            elif ensured.get("created"):
                result["created"].append("advisor")
        result["advisor_pane"] = advisor_pane_id

    return result


def runner_pane_id(runner_exec: CommandRunner, store: Any, job: Any) -> str | None:
    """The job's runner pane: the recorded `runner_pane`, else the pane labelled `runner <id8>`."""
    recorded = store.kv_get(job.job_id, "runner_pane")
    panes = list_panes(runner_exec, job.repo)
    if recorded and any(p.get("pane_id") == recorded for p in panes):
        return recorded
    found = find_pane_by_label(panes, f"runner {job.job_id[:8]}")
    return found.get("pane_id") if found else None


def _proc_cmdline(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return None


def _stop_runner_process(store: Any, job_id: str, *, sleep: Any, kill: Any = None, cmdline: Any = None,
                         wait_s: float = 10.0) -> bool:
    """Signal the job's recorded runner pid (SIGINT, then SIGTERM) and wait for it to exit. The runner pane's
    tty carries the mailbox, so a typed Ctrl-C does not reliably reach it. Returns False when there is no
    live pid that is this job's `steward run` (the caller then falls back to the keystroke)."""
    import os
    import signal

    kill = kill or os.kill
    cmdline = cmdline or _proc_cmdline
    rec = store.kv_get(job_id, "runner_code")
    pid = rec.get("pid") if isinstance(rec, dict) else None
    if not isinstance(pid, int) or f"run {job_id}" not in (cmdline(pid) or ""):
        return False
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            kill(pid, sig)
        except ProcessLookupError:
            return True
        waited = 0.0
        while waited < wait_s:
            if f"run {job_id}" not in (cmdline(pid) or ""):
                return True
            sleep(0.5)
            waited += 0.5
    return f"run {job_id}" not in (cmdline(pid) or "")


def restart_runner(
    runner_exec: CommandRunner, store: Any, job: Any, *, launch_args: dict | None = None,
    sleep: Any = None, settle_s: float = 2.0, kill: Any = None, cmdline: Any = None,
) -> dict:
    """Restart a RUNNING job's runner in its own pane on the current code, WITHOUT touching the worker:
    Ctrl-C the runner pane's foreground `steward run`, then type the same `steward run` command again. The
    job is RUNNING, so it takes the existing resume path (`Runner.resume_all` -> `attach()` for a fabric worker:
    the shim is still alive at its recorded pane, the peer is rebound, the unacked outbox resent). Never
    starts a second writer. Returns `{"accepted", "detail", "pane"}`."""
    import time as _time

    sleep = sleep or _time.sleep
    args = dict(launch_args or {})
    try:
        pane = runner_pane_id(runner_exec, store, job)
    except RuntimeError as exc:
        return {"accepted": False, "detail": f"cannot list panes: {exc}"}
    if pane is None:
        return {"accepted": False, "detail": f"no runner pane found for {job.job_id[:8]} (label 'runner {job.job_id[:8]}')"}
    worker = args.get("worker") or "fabric"
    try:
        command = worker_run_command(
            job.job_id, worker=worker, kind=args.get("kind", "claude"), pane=pane, sock=args.get("sock"),
            agent_args=list(args.get("agent_args") or []), interval=args.get("interval", 15.0),
            close_pane_on_exit=not args.get("keep_pane", False),
        )
    except ValueError as exc:
        return {"accepted": False, "detail": str(exc)}
    if not _stop_runner_process(store, job.job_id, sleep=sleep, kill=kill, cmdline=cmdline):
        # no live recorded pid: fall back to a keystroke (the runner's mailbox tty may swallow it)
        runner_exec.run(["herdr", "pane", "send-keys", pane, "ctrl+c"], cwd=job.repo)
    sleep(settle_s)
    if runner_alive(store.home, job.job_id):  # recheck right before launch (issue W): never a second runner
        return {"accepted": False, "pane": pane,
                "detail": f"the runner for {job.job_id[:8]} did not stop; not starting a second"}
    proclock.mark_starting(store.home, job.job_id, pane)
    res = runner_exec.run(["herdr", "pane", "run", pane, command], cwd=job.repo)
    if res.exit_code != 0:
        return {"accepted": False, "detail": f"herdr pane run failed: {res.stderr.strip()[:120]}", "pane": pane}
    return {"accepted": True, "detail": f"runner for {job.job_id[:8]} restarted in {pane}; worker untouched", "pane": pane}


# -- self-heal (issue W) ----------------------------------------------------------------------------------------------

def runner_alive(home: Any, job_id: str) -> bool:
    """A runner for `job_id` is running: it holds its lock, or a lock-less (pre-change) `steward run` of it is live."""
    return proclock.is_held(proclock.runner_lock(home, job_id)) or bool(proclock.runner_processes(home, job_id))


def _job_workspace(store: Any, job: Any) -> str | None:
    ws = store.kv_get(job.job_id, "herdr_workspace")
    if ws:
        return ws
    for key in ("herdr_tab", "runner_pane", "herdr_pane"):
        v = store.kv_get(job.job_id, key)
        if isinstance(v, str) and ":" in v:
            return v.split(":", 1)[0]
    return os.environ.get("HERDR_WORKSPACE_ID")


def _heal_states():
    from steward.models import JobState
    return frozenset({JobState.RUNNING, JobState.PAUSED, JobState.WORKER_FINISHED, JobState.CHECKS_PASSED})


# States whose dead runner the control pane's `runner_restart mode=heal` restores (spec 2026-10-08 §5.1: heal the
# runner in that state). `heal_runner`'s own default stays RUNNING-only (the watchdog's issue-W contract).
HEAL_STATES = _heal_states()


def heal_runner(runner_exec: CommandRunner, store: Any, job: Any, *, launch_args: dict | None = None,
                allowed_states: Any = None) -> dict:
    """Restore a RUNNING job's DEAD runner (issue W; the control pane's `runner_restart` in mode `heal`, which the
    watchdog relays). Rechecks right before launching, in order: the job is RUNNING; no runner holds the lock (or runs
    lock-less); no start is already in progress (`STARTING_GRACE_S`). Any failed check makes the request a no-op, so
    duplicates never launch anything, and the runner's own lock is the final guard. Starts from the deployed revision
    (`steward.deployed`). A missing runner pane gets a new job tab. The worker and its pane are never touched: the new
    runner takes the resume path and re-attaches to the live shim."""
    from steward import stability
    from steward.models import JobState

    j8 = job.job_id[:8]
    allowed = {getattr(x, "value", x) for x in (allowed_states or (JobState.RUNNING,))}
    if getattr(job.state, "value", job.state) not in allowed:
        return {"accepted": False, "detail": f"job {j8} is {getattr(job.state, 'value', job.state)}; only a "
                                             f"{'/'.join(sorted(allowed))} job's runner is restored"}
    if runner_alive(store.home, job.job_id):
        return {"accepted": True, "noop": True, "detail": f"runner for {j8} is already running"}
    if proclock.starting(store.home, job.job_id):
        return {"accepted": True, "noop": True, "detail": f"runner for {j8} is already starting"}
    try:
        root, live_root = deployed.launch_root(store.home, stability.LIVE_ROOT)
    except deployed.UnknownDeployed as exc:
        return {"accepted": False, "error_kind": "deployed_unknown", "detail": f"not restoring runner {j8}: {exc}"}
    args = dict(launch_args if launch_args is not None else (store.kv_get(job.job_id, "launch_args") or {}))
    worker = args.get("worker") or "fabric"
    try:
        pane = runner_pane_id(runner_exec, store, job)
    except RuntimeError as exc:
        return {"accepted": False, "detail": f"cannot list panes: {exc}"}
    if pane is None:
        workspace = _job_workspace(store, job)
        if not workspace:
            return {"accepted": False, "detail": f"runner pane for {j8} is gone and no workspace is known"}
        proclock.mark_starting(store.home, job.job_id, None)
        try:
            tab_id, pane = launch_tab(
                runner_exec, job, workspace=workspace, worker=worker, kind=args.get("kind", "claude"),
                agent_args=list(args.get("agent_args") or []), interval=args.get("interval", 15.0),
                sock=args.get("sock"), close_pane_on_exit=not args.get("keep_pane", False), store=store,
                root=root, live_root=live_root)
        except (RuntimeError, ValueError, KeyError) as exc:
            return {"accepted": False, "detail": f"cannot recreate the runner pane for {j8}: {exc}"}
        proclock.mark_starting(store.home, job.job_id, pane)
        try:
            store.kv_set(job.job_id, "herdr_tab", tab_id)
            store.kv_set(job.job_id, "runner_pane", pane)
        except sqlite3.OperationalError:  # locked DB: the runner is started; `runner pane` is found by label later
            pass
        return {"accepted": True, "pane": pane, "created_pane": True,
                "detail": f"runner for {j8} restored in a new tab ({pane}); worker untouched"}
    try:
        command = worker_run_command(
            job.job_id, worker=worker, kind=args.get("kind", "claude"), pane=pane, sock=args.get("sock"),
            agent_args=list(args.get("agent_args") or []), interval=args.get("interval", 15.0),
            close_pane_on_exit=not args.get("keep_pane", False), root=root, live_root=live_root)
    except ValueError as exc:
        return {"accepted": False, "detail": str(exc)}
    if runner_alive(store.home, job.job_id):  # last recheck, immediately before typing
        return {"accepted": True, "noop": True, "detail": f"runner for {j8} is already running"}
    proclock.mark_starting(store.home, job.job_id, pane)
    res = runner_exec.run(["herdr", "pane", "run", pane, command], cwd=job.repo)
    if getattr(res, "exit_code", 0) != 0:
        return {"accepted": False, "pane": pane,
                "detail": f"herdr pane run failed: {(res.stderr or res.stdout or '').strip()[:120]}"}
    return {"accepted": True, "pane": pane, "detail": f"runner for {j8} restored in {pane}; worker untouched"}
