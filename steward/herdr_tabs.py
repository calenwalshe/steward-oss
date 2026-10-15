"""Tab lifecycle for jobs launched via `steward launch` (USERLOOP-LOG bug 2c).

Every job `steward launch` opens gets its own Herdr tab, recorded as
`herdr_tab` in kv. These two helpers are the only code that touches that tab
after launch, and both are no-ops when no tab is on record (a job started
via `steward run --pane <existing runner>` rather than `steward launch`
never has one) — never a tab Steward didn't create. Both read the tab id
from kv rather than from a live worker/runner object, so they work even when
the process that launched the job (or its Runner) is gone (SP-9 style
restart-safety).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from steward import approvals as approvals_module
from steward import eta as eta_module, jobview, questions as questions_module, tabnames
from steward.models import Judgment, JobState
from steward.runner_exec import Runner
from steward.store import Store

_GOAL_CAP = 28
_HERDR_TAB_KEY = "herdr_tab"
_CLOSED_KEY = "herdr_tab_closed"
DEFAULT_GRACE_S = 60.0
KEEP_ENV = "STEWARD_KEEP_JOB_TABS"


def mark_accepted(store: Store, job_id: str, goal: str, runner_exec: Runner) -> None:
    """ACCEPTED: refresh this one job's tab label now (`✓ <name>`); the worker
    pane stays open for review (`steward run` has already exited). A no-op
    without a recorded tab."""
    tab_id = store.kv_get(job_id, _HERDR_TAB_KEY)
    job = store.get_job(job_id)
    if not tab_id or job is None:
        return
    label = job_label(store, job)
    runner_exec.run(["herdr", "tab", "rename", tab_id, label], cwd=store.home)
    store.kv_set(job_id, tabnames.LABEL_KEY, label)


def close(store: Store, job_id: str, runner_exec: Runner) -> None:
    """Terminal (STOPPED/FAILED/RELEASED): close the job's tab, if Steward
    created one. Best-effort — a herdr failure here (tab already closed by
    hand, workspace gone) is never fatal to the job's own state."""
    tab_id = store.kv_get(job_id, _HERDR_TAB_KEY)
    if not tab_id:
        return
    runner_exec.run(["herdr", "tab", "close", tab_id], cwd=store.home)


def _settled_at(store: Store, job) -> datetime:
    """When the job reached its closable resting state: its last state change,
    or the incorporation event if that came later (ACCEPTED -> merged)."""
    stamps = [job.updated_at] + [e.ts for e in store.events_of(job.job_id, ("incorporated",))]
    return max(datetime.fromisoformat(t) for t in stamps)


def closable(store: Store, job) -> bool:
    """RELEASED, STOPPED, or ACCEPTED-and-incorporated. Everything else
    (RUNNING, PAUSED, NEEDS_HUMAN, merely-ACCEPTED, ...) keeps its tab."""
    if job.state in (JobState.RELEASED, JobState.STOPPED):
        return True
    return job.state == JobState.ACCEPTED and bool(store.kv_get(job.job_id, "incorporated_commit"))


def _drain_legacy_questions(store: Store, runner_exec: Runner, job, job_tab: str) -> None:
    """Questions now live as a pane in the job tab and close with it; a pre-existing separate
    `? questions <id8>` tab of a finished job is closed here, once (kv `questions_tab_drained`)."""
    if store.kv_get(job.job_id, _DRAINED_KEY):
        return
    found = _legacy_questions_tabs(runner_exec, store, job.job_id)
    if found is None:  # herdr unreachable: look again next tick
        return
    for tab in found:
        if tab != job_tab:
            runner_exec.run(["herdr", "tab", "close", tab], cwd=store.home)
    store.kv_set(job.job_id, _DRAINED_KEY, True)


def sweep(
    store: Store,
    runner_exec: Runner,
    *,
    now: datetime | None = None,
    grace_s: float = DEFAULT_GRACE_S,
) -> list[str]:
    """Close the recorded tab of every finished job whose grace has elapsed.
    Run from the control pane's tick. Only a tab recorded as a job's own
    (`herdr_tab` kv, written by `steward launch`) is ever closed, and never
    one a still-live job also records. One `job_tab_closed` event per close;
    a close is attempted once (`herdr_tab_closed` kv), best-effort."""
    if os.environ.get(KEEP_ENV, "").strip() == "1":
        return []
    now = now or datetime.now(timezone.utc)
    jobs = store.list_jobs()
    tabs = {j.job_id: store.kv_get(j.job_id, _HERDR_TAB_KEY) for j in jobs}
    closed: list[str] = []
    for job in jobs:
        tab_id = tabs[job.job_id]
        if not tab_id or not closable(store, job):
            continue
        if store.kv_get(job.job_id, _CLOSED_KEY) and store.kv_get(job.job_id, _DRAINED_KEY):
            continue  # nothing left to do for it: skip the settle-time read (every finished job, every 5 s tick)
        if (now - _settled_at(store, job)).total_seconds() < grace_s:
            continue
        _drain_legacy_questions(store, runner_exec, job, tab_id)
        if store.kv_get(job.job_id, _CLOSED_KEY):
            continue
        if any(o.job_id != job.job_id and tabs[o.job_id] == tab_id and not closable(store, o) for o in jobs):
            continue
        result = runner_exec.run(["herdr", "tab", "close", tab_id], cwd=store.home)
        store.kv_set(job.job_id, _CLOSED_KEY, tab_id)
        store.append_event(
            job.job_id,
            what="job_tab_closed",
            evidence={"tab_id": tab_id, "state": job.state.value, "grace_s": grace_s,
                      "returncode": getattr(result, "returncode", None)},
            rule="job_tab_auto_close",
            judgment=Judgment.code,
        )
        closed.append(job.job_id)
    return closed


# ---- layout: one ordering for the tab bar and the status pane's [n] ----------------------------------------------

OPS_PANE_LABEL = "steward status"
PHONE_PANE_LABEL = "📱 phone"
DONE_PANE_LABEL = "✓ done"  # the completed-jobs tab (`steward done-log`), found by its pane like the phone tab
LEGACY_QUESTIONS_PREFIX = "? questions "
FIRST_JOB_POSITION = 3  # zone A holds `⌂ ops` and `⚕ steward`; jobs start at tab 3
MAX_KEYED_POSITION = 9  # prefix+1..9 / the status pane's digit keys
_DRAINED_KEY = "questions_tab_drained"

_DONE_STATES = frozenset({"ACCEPTED", "RELEASED", "STOPPED", "FAILED"})


def job_group(state: str, merged: bool = False) -> int:
    """0 active, 1 accepted-but-not-merged, 2 done."""
    if state not in _DONE_STATES:
        return 0
    return 1 if state == "ACCEPTED" and not merged else 2


def order_jobs(items: list, get: Callable[[Any], tuple[str, str, bool]]) -> list:
    """THE job order (status pane `[n]` and tab bar): active jobs by created_at, then accepted-not-merged, then
    done. `get(item) -> (state, created_at, merged)`; stable for equal keys."""
    def key(item):
        state, created, merged = get(item)
        return (job_group(state, merged), created or "")
    return sorted(items, key=key)


def job_position(rank: int) -> int:
    """Tab position (1-based) of the job at 0-based `rank` in `order_jobs`."""
    return FIRST_JOB_POSITION + rank


@dataclass(frozen=True)
class Slot:
    position: int
    tab_id: str
    job_id: str | None
    label: str
    kind: str  # ops | steward | job | other | phone | done


def pinned_tabs(tabs: list[dict], panes: list[dict]) -> dict[str, str]:
    """`{"ops", "steward", "phone", "done"}` -> tab id, found by the panes each tab holds, never by the tab's label
    (the owner renames tabs freely)."""
    from steward.control_pane import CONTROL_ROLE_TOKEN, CONTROL_ROLE_VALUE

    known = {t["tab_id"] for t in tabs}
    found: dict[str, str] = {}
    for pane in panes:
        tab = pane.get("tab_id")
        if tab not in known:
            continue
        if pane.get("label") == OPS_PANE_LABEL:
            found.setdefault("ops", tab)
        if (pane.get("tokens") or {}).get(CONTROL_ROLE_TOKEN) == CONTROL_ROLE_VALUE:
            found.setdefault("steward", tab)
        if pane.get("label") == PHONE_PANE_LABEL:
            found.setdefault("phone", tab)
        if pane.get("label") == DONE_PANE_LABEL:
            found.setdefault("done", tab)
    return found


def _job_data(store: Store, job, now: datetime | None = None) -> dict:
    from steward import delivery, ownership

    events = jobview.event_dicts(store.events(job.job_id))
    state = job.state.value
    proj = ownership.projection(store, job.job_id)
    kind, detail = jobview.liveness(state, events, now, ownership=proj,
                                    delivery=delivery.liveness_hint(store, job.job_id, state))
    try:
        qsum = questions_module.summary(questions_module.load(store, job.job_id))
    except Exception:  # noqa: BLE001 - a broken queue never blocks a label
        qsum = None
    reason = jobview.reason(approvals_module.list_approvals(store, job.job_id), state, events, questions=qsum,
                            ownership=proj)
    eff = store.kv_get(job.job_id, "efficiency_state")
    eta = eff.get("eta") if isinstance(eff, dict) and isinstance(eff.get("eta"), dict) else None
    age = detail.rsplit(" ", 2)[-2] if kind == "quiet" and detail.endswith(" ago") else ""
    if eta and eta.get("source"):  # the stored ETA counted down to now, so the tab says what the pane says (`fin`, `+12m`, `~45m`)
        try:
            eta = {**eta, **{k: v for k, v in eta_module.render(eta, datetime.now(timezone.utc).timestamp()).items()
                             if k in ("p50_min", "overdue_min", "tab")}}
        except Exception:  # noqa: BLE001 - a broken ETA never blocks a label
            pass
    return {"state": state, "kind": kind, "reason": reason, "eta": eta,
            "waiting": (qsum or {}).get("waiting") or 0, "quiet_age": age}


def job_label(store: Store, job, now: datetime | None = None, persist: bool = True) -> str:
    """The tab title this job should carry right now."""
    d = _job_data(store, job, now)
    return tabnames.tab_label(
        tabnames.status_glyph(d["state"], d["kind"], d["reason"], d["eta"]),
        tabnames.job_name(store, job, persist),
        tabnames.info_text(d["waiting"], d["reason"], d["eta"], d["kind"], d["quiet_age"]),
    )


def layout(store: Store, tabs: list[dict], panes: list[dict], *, now: datetime | None = None,
           persist: bool = True) -> list[Slot]:
    """The desired full tab order: ops, steward, jobs (status-pane order), any tab Steward does not manage, done, phone."""
    pins = pinned_tabs(tabs, panes)
    by_id = {t["tab_id"]: t for t in tabs}
    pinned_ids = set(pins.values())
    jobs = []
    for job in store.list_jobs():
        tab = store.kv_get(job.job_id, _HERDR_TAB_KEY)
        if tab in by_id and tab not in pinned_ids and not store.kv_get(job.job_id, _CLOSED_KEY):
            jobs.append((job, tab))
    jobs = order_jobs(jobs, lambda jt: (jt[0].state.value, jt[0].created_at, bool(store.kv_get(jt[0].job_id, "incorporated_commit"))))
    slots: list[tuple[str, str, str | None, str]] = []
    for kind, label in (("ops", tabnames.PINNED_OPS), ("steward", tabnames.PINNED_STEWARD)):
        if kind in pins:
            slots.append((kind, pins[kind], None, by_id[pins[kind]].get("label", label)))
    seen = set()
    for job, tab in jobs:
        if tab in seen:  # two jobs recording one tab: the first owns it
            continue
        seen.add(tab)
        slots.append(("job", tab, job.job_id, job_label(store, job, now, persist)))
    claimed = pinned_ids | seen
    for t in tabs:
        if t["tab_id"] not in claimed:
            slots.append(("other", t["tab_id"], None, t.get("label", "")))
    for kind, label in (("done", tabnames.PINNED_DONE), ("phone", tabnames.PINNED_PHONE)):
        if kind in pins:
            slots.append((kind, pins[kind], None, by_id[pins[kind]].get("label", label)))
    return [Slot(i, tab, jid, label, kind) for i, (kind, tab, jid, label) in enumerate(slots, 1)]


def plan_moves(current: list[str], desired: list[str]) -> list[tuple[str, int]]:
    """Minimal `(tab_id, insert_index)` moves turning `current` into `desired` (same members): tabs on a longest
    already-in-order run stay put, each other tab is moved right after its desired predecessor."""
    rank = {t: i for i, t in enumerate(desired)}
    seq = [t for t in current if t in rank]
    best: list[list[str]] = []  # best[i] = longest increasing run ending at seq[i]
    for i, t in enumerate(seq):
        cand = max((best[j] for j in range(i) if rank[seq[j]] < rank[t]), key=len, default=[])
        best.append(cand + [t])
    keep = set(max(best, key=len, default=[]))
    sim, moves = list(seq), []
    for i, t in enumerate(desired):
        if t not in rank or t in keep or t not in sim:
            continue
        sim.remove(t)
        idx = sim.index(desired[i - 1]) + 1 if i else 0
        sim.insert(idx, t)
        moves.append((t, idx))
    return moves


def _adopt_manual_rename(store: Store, job, live_label: str) -> None:
    """A tab label Steward did not write (kv `herdr_tab_label` differs): take the name part as the job's name."""
    written = store.kv_get(job.job_id, tabnames.LABEL_KEY)
    if written is None or live_label == written:
        return
    name = tabnames.parse_label(live_label)
    if name and name != store.kv_get(job.job_id, tabnames.NAME_KEY):
        store.kv_set(job.job_id, tabnames.NAME_KEY, name)
        store.append_event(job.job_id, what="job_renamed", evidence={"name": name, "via": "herdr tab rename"},
                           rule="tab_rename_adopted", judgment=Judgment.human)


def _api_lists(herdr_api) -> tuple[list[dict], list[dict]]:
    tabs = herdr_api.call("tab.list", {}).get("tabs", [])
    panes = herdr_api.call("pane.list", {}).get("panes", [])
    return tabs, panes


def refresh(store: Store, runner_exec: Runner, herdr_api, *, now: datetime | None = None) -> dict:
    """Bring the tab bar to `layout()`: rename a job tab only when its label differs from what is on screen, then
    `tab.move` (socket API; the CLI has no verb) the fewest tabs needed. Best-effort and never raises; a quiet tick
    makes no rename or move call. Single writer: the control pane."""
    result: dict = {"renamed": [], "moved": []}
    try:
        tabs, panes = _api_lists(herdr_api)
        pins = pinned_tabs(tabs, panes)
        ws = next((t.get("workspace_id") for t in tabs if t["tab_id"] == pins.get("steward")), None)
        if ws:
            tabs = [t for t in tabs if t.get("workspace_id") in (None, ws)]
        live = {t["tab_id"]: t.get("label", "") for t in tabs}
        for job in store.list_jobs():
            tab = store.kv_get(job.job_id, _HERDR_TAB_KEY)
            if tab in live and not store.kv_get(job.job_id, _CLOSED_KEY):
                _adopt_manual_rename(store, job, live[tab])
        slots = layout(store, tabs, panes, now=now)
        for slot in slots:
            if slot.kind == "job" and slot.label != live.get(slot.tab_id):
                res = runner_exec.run(["herdr", "tab", "rename", slot.tab_id, slot.label], cwd=store.home)
                if getattr(res, "exit_code", 0) == 0:
                    store.kv_set(slot.job_id, tabnames.LABEL_KEY, slot.label)
                    result["renamed"].append((slot.tab_id, slot.label))
        for tab_id, index in plan_moves([t["tab_id"] for t in tabs], [s.tab_id for s in slots]):
            herdr_api.call("tab.move", {"tab_id": tab_id, "insert_index": index})
            result["moved"].append((tab_id, index))
    except Exception as exc:  # noqa: BLE001 - same contract as sweep: a tab cosmetic never stops the control tick
        result["error"] = str(exc)
    return result


# ---- legacy `? questions <id8>` tabs ------------------------------------------------------------------------------


def _legacy_questions_tabs(runner_exec: Runner, store: Store, job_id: str) -> list[str] | None:
    res = runner_exec.run(["herdr", "pane", "list"], cwd=store.home)
    if getattr(res, "exit_code", 1) != 0:
        return None
    try:
        panes = json.loads(res.stdout)["result"]["panes"]
    except (ValueError, KeyError, TypeError):
        return None
    label = f"{LEGACY_QUESTIONS_PREFIX}{job_id[:8]}"
    return sorted({p["tab_id"] for p in panes if p.get("label") == label and p.get("tab_id")})


# ---- one-time migration: `steward tabs --tidy [--dry-run]` ---------------------------------------------------------


def tidy(store: Store, runner_exec: Runner, herdr_api, *, dry_run: bool = False,
         say: Callable[[str], None] = print) -> list[str]:
    """Bring an existing workspace to the new layout and print every step before (dry-run) or as it is done.
    Cosmetic except one thing: a legacy `? questions <id8>` tab's pane is moved into its job's tab (same
    workspace, found by label). A running job's runner/worker panes are never moved."""
    steps: list[str] = []

    def step(text: str, *argv: str, api: tuple | None = None) -> None:
        steps.append(text)
        say(("would: " if dry_run else "do: ") + text)
        if dry_run:
            return
        if api:
            herdr_api.call(*api)
        else:
            runner_exec.run(list(argv), cwd=store.home)

    tabs, panes = _api_lists(herdr_api)
    pins = pinned_tabs(tabs, panes)
    live = {t["tab_id"]: t.get("label", "") for t in tabs}
    if "done" not in pins and "steward" in pins:  # the completed-jobs tab, created in the steward tab's workspace
        ws = next((t.get("workspace_id") for t in tabs if t["tab_id"] == pins["steward"]), None)
        steps.append(f"create the {tabnames.PINNED_DONE!r} tab (steward done-log) in workspace {ws}")
        say(("would: " if dry_run else "do: ") + steps[-1])
        if not dry_run and ws:
            from steward import donelog

            donelog.open_done_tab(runner_exec, ws)
            tabs, panes = _api_lists(herdr_api)
            pins = pinned_tabs(tabs, panes)
            live = {t["tab_id"]: t.get("label", "") for t in tabs}
    for kind, want in (("ops", tabnames.PINNED_OPS), ("steward", tabnames.PINNED_STEWARD), ("phone", tabnames.PINNED_PHONE),
                      ("done", tabnames.PINNED_DONE)):
        if kind in pins and live[pins[kind]] != want:
            step(f"rename {kind} tab {pins[kind]} {live[pins[kind]]!r} -> {want!r}", "herdr", "tab", "rename", pins[kind], want)
    for job in store.list_jobs():
        tab = store.kv_get(job.job_id, _HERDR_TAB_KEY)
        if tab not in live or store.kv_get(job.job_id, _CLOSED_KEY):
            continue
        had_name = bool(store.kv_get(job.job_id, tabnames.NAME_KEY))
        name = tabnames.job_name(store, job, persist=not dry_run)
        if not had_name:
            steps.append(f"set short_name {job.job_id[:8]} -> {name!r}")
            say(("would: " if dry_run else "do: ") + steps[-1])
        protected = {p["pane_id"] for p in panes if p.get("tab_id") == tab}  # the job's own panes stay where they are
        legacy_label = f"{LEGACY_QUESTIONS_PREFIX}{job.job_id[:8]}"
        for qp in [p for p in panes if p.get("label") == legacy_label and p.get("tab_id") != tab]:
            anchor = next((p for p in panes if p.get("tab_id") == tab and p.get("label") == f"watchers {job.job_id[:8]}"), None) \
                or next((p for p in panes if p.get("tab_id") == tab and p.get("label") == f"runner {job.job_id[:8]}"), None)
            if anchor is None or qp["pane_id"] in protected:
                steps.append(f"skip questions pane {qp['pane_id']}: no anchor pane in {tab}")
                say("skip: " + steps[-1])
                continue
            step(f"move questions pane {qp['pane_id']} into job tab {tab} (split down off {anchor['pane_id']})",
                 "herdr", "pane", "move", qp["pane_id"], "--tab", tab, "--split", "down",
                 "--target-pane", anchor["pane_id"], "--no-focus")
            step(f"rename pane {qp['pane_id']} -> 'questions {job.job_id[:8]}'",
                 "herdr", "pane", "rename", qp["pane_id"], f"questions {job.job_id[:8]}")
            old_tab = qp["tab_id"]
            if dry_run:
                steps.append(f"close tab {old_tab} once it is empty")
                say("would: " + steps[-1])
            else:
                _, after = _api_lists(herdr_api)
                if not any(p.get("tab_id") == old_tab for p in after):
                    step(f"close emptied tab {old_tab}", "herdr", "tab", "close", old_tab)
            live.pop(old_tab, None)
    if dry_run:
        moving_tabs = [t for t in tabs if t["tab_id"] in live]
        slots = layout(store, moving_tabs, panes, persist=False)
        for slot in slots:
            if slot.kind == "job" and slot.label != live.get(slot.tab_id):
                steps.append(f"rename job tab {slot.tab_id} {live.get(slot.tab_id)!r} -> {slot.label!r}")
                say("would: " + steps[-1])
        for tab_id, index in plan_moves([t["tab_id"] for t in moving_tabs], [s.tab_id for s in slots]):
            steps.append(f"tab.move {tab_id} -> position {index + 1}")
            say("would: " + steps[-1])
    else:
        res = refresh(store, runner_exec, herdr_api)
        for tab_id, label in res["renamed"]:
            steps.append(f"rename job tab {tab_id} -> {label!r}")
            say(f"do: {steps[-1]}")
        for tab_id, index in res["moved"]:
            steps.append(f"tab.move {tab_id} -> position {index + 1}")
            say(f"do: {steps[-1]}")
    return steps
