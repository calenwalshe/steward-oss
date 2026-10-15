"""Code-judged cleanup on terminal transitions and incorporation (spec 2026-10-08 §5.3). Owner decisions are never
closed here: they are carried forward (`owner_decision_carried`) and listed in the digest until answered."""
from __future__ import annotations

import logging
import time

from steward import approvals, blockers, ownership
from steward.models import JobState, Judgment
from steward.store import Store

STALE_KINDS = ("acceptance", "delivery", "tech_lead", "permission", "general")
FIXUP_STOP_KEY = "cleanup_fixup_stop"


def _is_owner_decision(rec: dict) -> bool:
    return bool(rec.get("owner_decision")) or rec.get("kind") == "owner_question"


def _close_approvals(store: Store, job_id: str, cause: str) -> tuple[list[str], list[str]]:
    closed, carried = [], []
    for rec in approvals.list_approvals(store, job_id, status="pending"):
        if _is_owner_decision(rec):
            carried.append(rec["id"])
            continue
        if rec.get("kind", "general") in STALE_KINDS:
            approvals.mark_stale(store, job_id, rec["id"], reason=f"superseded: {cause}")
            approvals.annotate(store, job_id, rec["id"], closed_reason=f"superseded: {cause}")
            closed.append(rec["id"])
    return closed, carried


def _resolve_blockers(store: Store, job, cause: str, now: float) -> tuple[list[str], list[str]]:
    resolved, carried = [], []
    for b in blockers.open_blockers(store, job.job_id):
        if b.kind in ownership.CARRY_FORWARD_KINDS or ownership.carries_forward(job, b):
            carried.append(b.blocker_id)
            continue
        blockers.transition(store, b.blocker_id, "resolved", actor=ownership.CONTROLLER,
                            reason=f"superseded: {cause}", now=now)
        resolved.append(b.blocker_id)
    return resolved, carried


def _mark_fixups(store: Store, parent_id: str, cause: str) -> list[str]:
    """Children (`kv children` and reverse `autopilot_followup`) still live are marked for the sweep to stop:
    stopping needs the runner path (control `stop`), which a store hook must not call."""
    from steward.train_housekeeping import MERGE_GOAL_PREFIX
    live = {JobState.CREATED, JobState.CONTRACTED, JobState.RUNNING, JobState.PAUSED, JobState.WORKER_FINISHED,
            JobState.CHECKS_PASSED, JobState.NEEDS_HUMAN}
    kids = set(store.kv_get(parent_id, "children", default=[]) or [])
    kids |= {j.job_id for j in store.list_jobs(live) if store.kv_get(j.job_id, "autopilot_followup") == parent_id}
    marked = []
    for cid in kids:
        c = store.get_job(cid)
        if c is None or c.state not in live or not c.goal.startswith(MERGE_GOAL_PREFIX):
            continue
        store.kv_set(cid, FIXUP_STOP_KEY, {"parent": parent_id, "cause": cause, "at": time.time()})
        marked.append(cid)
    return marked


def _kinds(store: Store, job_id: str, ids: list[str]) -> list[str]:
    return sorted({r.get("kind", "general") for r in approvals.list_approvals(store, job_id) if r["id"] in ids})


def _review_in_flight(store: Store, job_id: str) -> bool:
    """A tech-lead review awaiting its answer needs the blockers open so the sweep can discard the late answer
    (`tech_lead_answer_discarded`); the ownership sweep then resolves them."""
    try:
        from steward import fleet_tech_lead
        return bool((store.kv_get(job_id, fleet_tech_lead.JOB_STATE) or {}).get("in_flight"))
    except Exception:  # noqa: BLE001
        return False


def _reconcile_first(store: Store, job_id: str, now: float) -> None:
    """Let ownership clear blockers with its state-aware reasons (`job STOPPED`) before the generic sweep below."""
    if not ownership.enabled():
        return
    ownership.reconcile_job(store, job_id, now=now)


def _on_resolved(store: Store, job_id: str, *, cause: str, now: float | None = None,
                resolve_blockers: bool = True, reconcile: bool = False) -> dict:
    """`reconcile=True` (the set_state hook) runs `ownership.reconcile_job` first; its failure is recorded as
    `cleanup_failed` and never blocks the transition."""
    now = time.time() if now is None else now
    job = store.get_job(job_id)
    out = {"approvals": [], "blockers": [], "carried": [], "fixups_marked": []}
    if job is None:
        return out
    defer = reconcile and _review_in_flight(store, job_id)
    if reconcile and not defer:
        try:
            _reconcile_first(store, job_id, now)
        except Exception as exc:  # noqa: BLE001
            store.append_event(job_id, what="cleanup_failed", evidence={"cause": cause, "stage": "reconcile",
                                                                        "error": str(exc)[:300]},
                               rule="cleanup", judgment=Judgment.code)
    try:
        out["approvals"], carried_a = _close_approvals(store, job_id, cause)
        out["blockers"], carried_b = _resolve_blockers(store, job, cause, now) if resolve_blockers and not defer else ([], [])
        out["carried"] = carried_a + carried_b
        out["fixups_marked"] = _mark_fixups(store, job_id, cause)
    except Exception as exc:  # noqa: BLE001 - cleanup never blocks a transition; the sweep retries
        store.append_event(job_id, what="cleanup_failed", evidence={"cause": cause, "error": str(exc)[:300]},
                           rule="cleanup", judgment=Judgment.code)
        return out
    if out["approvals"]:
        store.append_event(job_id, what="approvals_superseded",
                           evidence={"cause": cause, "n": len(out["approvals"]), "ids": out["approvals"],
                                     "kinds": _kinds(store, job_id, out["approvals"])},
                           rule="cleanup", judgment=Judgment.code)
    if out["blockers"]:
        store.append_event(job_id, what="blockers_superseded",
                           evidence={"cause": cause, "n": len(out["blockers"]), "ids": out["blockers"],
                                     "kinds": sorted({b.kind for i in out["blockers"]
                                                      if (b := blockers.get(store, i))})},
                           rule="cleanup", judgment=Judgment.code)
    if out["carried"]:
        store.append_event(job_id, what="owner_decision_carried", evidence={"ids": out["carried"], "cause": cause},
                           rule="cleanup", judgment=Judgment.code)
    return out


def on_resolved(store: Store, job_id: str, *, cause: str, now: float | None = None,
                resolve_blockers: bool = True, reconcile: bool = False) -> dict:
    """Never raises: any failure (including a failing event write) is swallowed and logged."""
    try:
        return _on_resolved(store, job_id, cause=cause, now=now, reconcile=reconcile,
                            resolve_blockers=resolve_blockers)
    except Exception as exc:  # noqa: BLE001 - cleanup never blocks a transition; the sweep retries
        try:
            store.append_event(job_id, what="cleanup_failed", evidence={"cause": cause, "error": str(exc)[:300]},
                               rule="cleanup", judgment=Judgment.code)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("cleanup_failed event write failed for %s", job_id)
        return {"approvals": [], "blockers": [], "carried": [], "fixups_marked": []}


def pending_fixup_stops(store: Store) -> list[tuple[str, str]]:
    return [(j.job_id, str((store.kv_get(j.job_id, FIXUP_STOP_KEY) or {}).get("cause", "")))
            for j in store.list_jobs() if store.kv_get(j.job_id, FIXUP_STOP_KEY)]


def clear_fixup_stop(store: Store, child_id: str) -> None:
    store.kv_delete(child_id, FIXUP_STOP_KEY)


def backfill(store: Store, *, dry_run: bool) -> dict:
    from steward import incorporate as inc
    out: dict = {"jobs": 0, "approvals": 0, "blockers": 0, "released": [], "would": []}
    for job in store.list_jobs({JobState.RELEASED, JobState.STOPPED, JobState.FAILED}):
        pend = [a for a in approvals.list_approvals(store, job.job_id, status="pending") if not _is_owner_decision(a)]
        opn = [b for b in blockers.open_blockers(store, job.job_id) if b.kind not in ownership.CARRY_FORWARD_KINDS]
        if not (pend or opn):
            continue
        out["jobs"] += 1
        out["approvals"] += len(pend)
        out["blockers"] += len(opn)
        if not dry_run:
            on_resolved(store, job.job_id, cause=f"backfill: job {job.state.value}")
    for job in store.list_jobs({JobState.ACCEPTED}):
        if inc.release_evidence(store, job)["ok"]:
            out["would"].append(job.job_id)
            if not dry_run and inc.release_on_incorporation(store, job.job_id):
                out["released"].append(job.job_id)
    return out
