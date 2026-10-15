"""A Steward self-merge waiting for its deploy: runners do not self-restart onto it until `deploy-self` ran.

The owner's decision 2026-10-06 (job f257aa7b, question skew-window, option B): autopilot merges into the LIVE
checkout, then re-tests the merged tree (66 min for 233d178) before `deploy-self`. Runners used to self-restart onto the new HEAD
within a second while the control pane stayed on the old code until the deploy: approvals written by new-code
runners into a table the old control did not know ("unknown approval"). Now autopilot marks the merge pending just
before it lands; runners (`selfrestart.SelfRestarter`) wait while the mark is set; the mark is cleared when that
merge attempt ends, whatever the outcome, and by any successful `deploy-self`, so everything moves together.

A mark older than `MAX_AGE_S` is ignored (a killed autopilot must never freeze the runners' self-restart).

Several Steward merges can await one deploy (delivery marks its merges too, 2026-10-07): the marks are a list, one
entry per job, so one job's clear never drops another's. The pre-list single-slot record (`KV_KEY`, still written by
a worker running older code during a deploy) is honoured and cleared, never overwritten by the list."""
from __future__ import annotations

import time

from steward.store import Store

KV_JOB = "_steward"  # kv has no foreign key into jobs: a fixed namespace, like the control receipts' "_control"
KV_KEY = "deploy_pending"        # legacy single slot {job_id, since}
KV_MARKS = "deploy_pending_marks"  # [{job_id, since}, ...]
MAX_AGE_S = 4 * 3600.0


def _marks(kv) -> list[dict]:
    rec = kv.kv_get(KV_JOB, KV_MARKS)
    return [m for m in rec if isinstance(m, dict)] if isinstance(rec, list) else []


def mark(store: Store, job_id: str, *, now: float | None = None) -> None:
    """Add (or refresh) this job's mark; other jobs' marks stay (one write transaction: no lost update)."""
    with store.immediate() as tx:
        tx.kv_set(KV_JOB, KV_MARKS, [m for m in _marks(tx) if m.get("job_id") != job_id]
                  + [{"job_id": job_id, "since": time.time() if now is None else now}])


def clear(store: Store, job_id: str | None = None) -> None:
    """Drop every mark, or only `job_id`'s own when given (another job's merge keeps waiting)."""
    with store.immediate() as tx:
        if tx.kv_get(KV_JOB, KV_MARKS) is not None:
            tx.kv_set(KV_JOB, KV_MARKS, [] if job_id is None else [m for m in _marks(tx) if m.get("job_id") != job_id])
    rec = store.kv_get(KV_JOB, KV_KEY)
    if rec is not None and (job_id is None or not isinstance(rec, dict) or rec.get("job_id") == job_id):
        store.kv_delete(KV_JOB, KV_KEY)


def pending(store: Store, *, now: float | None = None) -> dict | None:
    """The oldest live mark ({job_id, since}), or None (none, malformed, or all older than MAX_AGE_S)."""
    now = time.time() if now is None else now
    legacy = store.kv_get(KV_JOB, KV_KEY)
    live = [m for m in _marks(store) + ([legacy] if isinstance(legacy, dict) else [])
            if isinstance(m.get("since"), (int, float)) and 0 <= now - m["since"] < MAX_AGE_S]
    return min(live, key=lambda m: m["since"]) if live else None
