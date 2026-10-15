"""Why a job is in NEEDS_HUMAN, decided by code from the event log (spec 2026-10-04 tech-lead-parked-reopen, §3).

kinds:  acceptance  a turn end classified CANDIDATE (clean tree, new SHA; `yield_classified`) -> the owner's decision
        stuck       a turn end classified STOPPED (no candidate), or Steward
                    parked it (finish-line bounces exhausted, verification crashed, startup blocked)
        question    a question is waiting in the queue -> the owner answers
        floor       closeout is blocked by a hard constraint -> the owner
        unknown     none of the above (fleet repair, deadline candidate without a marker...) -> treated as the owner's
Precedence: floor > question > acceptance/stuck (by the newest parking reason, else the newest done marker).
`kind()` is pure over the Store and cached by the latest event seq; it never consults a model.
"""
from __future__ import annotations

from steward.models import JobState
from steward.store import Store

KINDS = ("stuck", "acceptance", "question", "floor", "unknown")
CACHE_KEY = "hold_kind"

# `yield_classified` (v2.7 Phase 40) is the live source: CANDIDATE -> acceptance, STOPPED -> stuck. The older rules
# stay recognised only so events already in a store keep their kind.
_MARKER_RULES = {"steward_done_marker": "STEWARD-DONE", "fabric_done_frame": "fabric_done",
                 "idle_after_final_message": "idle_after_final_message"}
_YIELD_RULE = "yield_classification"
_STUCK_RULES = {"finish_line_bounces_exhausted"}
_STUCK_WHAT_PREFIXES = ("worker startup blocked", "background verification crashed", "finish line still not met",
                        "tests unverified")


def _scan(events) -> tuple[str | None, str | None, str]:
    """-> (kind-by-reason, marker, rule) walking backwards: the newest parking reason wins; else the newest marker."""
    for e in reversed(events):
        if e.rule in _STUCK_RULES or any(e.what.startswith(p) for p in _STUCK_WHAT_PREFIXES):
            return "stuck", None, e.rule
        if e.rule == _YIELD_RULE:
            lane = (e.evidence or {}).get("lane")
            if lane == "CANDIDATE":
                return "acceptance", (e.evidence or {}).get("marker") or "fabric_done", e.rule
            if lane == "STOPPED":
                return "stuck", "yield_stopped", e.rule
            continue  # WAITING / ASKING: not a parking reason
        if e.rule in _MARKER_RULES:
            marker = (e.evidence or {}).get("marker") or _MARKER_RULES[e.rule]
            return ("stuck" if e.rule == "idle_after_final_message" else "acceptance"), marker, e.rule
    return None, None, ""


def worker_claim(events) -> str | None:
    """Return the worker's latest done/unfinished claim as evidence, never as a decision."""
    for e in reversed(events):
        if e.rule == _YIELD_RULE:
            lane = (e.evidence or {}).get("lane")
            if lane == "CANDIDATE":
                return "done"
            if lane == "STOPPED":
                return "unfinished"
            continue
        if e.rule in _MARKER_RULES:
            return "unfinished" if e.rule == "idle_after_final_message" else "done"
    return None


def kind(store: Store, job_id: str) -> dict | None:
    from steward import questions

    job = store.get_job(job_id)
    if job is None or job.state != JobState.NEEDS_HUMAN:
        return None
    events = store.events(job_id)
    seq = events[-1].seq if events else 0
    cached = store.kv_get(job_id, CACHE_KEY)
    if isinstance(cached, dict) and cached.get("seq") == seq:
        return cached
    by_reason, marker, rule = _scan(events)
    if store.kv_get(job_id, "closeout_blocked", default=False) or any(
            "hard constraint violation" in e.what for e in events[-20:]):
        k = "floor"
    elif questions.summary(questions.load(store, job_id)).get("waiting", 0) > 0:
        k = "question"
    else:
        k = by_reason or "unknown"
    reason = next((e.what for e in reversed(events) if e.rule and e.rule != "t"), "")
    out = {"kind": k, "rule": rule, "marker": marker, "reason": reason[:200], "seq": seq}
    store.kv_set(job_id, CACHE_KEY, out)
    return out


def resolve_reopens(store: Store, job_id: str) -> int:
    """Score pending tech-lead reopens (spec §6): the reopened attempt parked again as `stuck` -> wrong; it reached
    CHECKS_PASSED/ACCEPTED or an `acceptance` hold -> correct. Pending entries whose attempt is still running stay."""
    from steward import tech_lead_actions as tla
    prec = store.kv_get(job_id, tla.PRECISION_KEY) or []
    job = store.get_job(job_id)
    if job is None:
        return 0
    events = store.events(job_id)
    last_seq = events[-1].seq if events else 0
    changed = 0
    for p in prec:
        if p.get("action") != "reopen" or p.get("outcome") != "pending":
            continue
        if last_seq <= int(p.get("seq") or 0):
            continue  # nothing happened since the reopen
        if job.state in (JobState.CHECKS_PASSED, JobState.ACCEPTED, JobState.RELEASED):
            p["outcome"], changed = "correct", changed + 1
        elif job.state == JobState.NEEDS_HUMAN:
            k = kind(store, job_id) or {"kind": "unknown"}
            if k["kind"] == "stuck":
                p["outcome"], changed = "wrong", changed + 1
            elif k["kind"] == "acceptance":
                p["outcome"], changed = "correct", changed + 1
    if changed:
        store.kv_set(job_id, tla.PRECISION_KEY, prec)
    return changed
