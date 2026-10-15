"""Job replacement links (issue M).

Two jobs for one item both reached ACCEPTED because nothing recorded that
one superseded the other (efbe078f merged while its duplicate 60b4238c kept
running; df09f7ed and d91e7946 were both accepted for the same item). A
replacement is a job-level link:

- kv `replaced_by` on OLD and kv `replaces` on NEW, plus one event on each;
- OLD is stopped at once through the normal stop control (every non-terminal
  state; its branch and worktree are never deleted);
- accept, incorporate and the autopilot/delivery merge paths refuse a job
  whose `replaced_by` is set, naming the replacement;
- if OLD is already incorporated or merged the link is still recorded, with
  a warning instead of a stop.

`steward replace OLD NEW` and `steward new --replaces OLD` both land here via
the control pane (relayed writes); `steward result` and `steward jobs` read
the links back through `labels`.
"""

from __future__ import annotations

from typing import Callable

from steward import followup
from steward.models import JobState, Judgment
from steward.store import Store

K_REPLACED_BY = "replaced_by"
K_REPLACES = "replaces"
RULE = "replace"

_DONE_FOR_GOOD = (JobState.ACCEPTED, JobState.RELEASED)


def replaced_by(store: Store, job_id: str) -> str | None:
    """The job that superseded `job_id`, or None."""
    return store.kv_get(job_id, K_REPLACED_BY) or None


def replaces(store: Store, job_id: str) -> str | None:
    """The job `job_id` superseded, or None."""
    return store.kv_get(job_id, K_REPLACES) or None


def refusal(store: Store, job_id: str, action: str) -> str | None:
    """The message an `action` (accept/incorporate/merge) must refuse with when the job was replaced; else None."""
    new = replaced_by(store, job_id)
    if not new:
        return None
    return f"job {job_id[:8]} was replaced by {new[:8]}; {action} refused (work on {new[:8]} instead)"


def already_done_warning(store: Store, job) -> str | None:
    """Why OLD will not be stopped: it is already incorporated, or already ACCEPTED/RELEASED."""
    merged = store.kv_get(job.job_id, "incorporated_commit")
    if merged:
        return (f"{job.job_id[:8]} is already incorporated ({str(merged)[:7]}); the link is recorded but nothing "
                "was stopped")
    if job.state in _DONE_FOR_GOOD:
        return f"{job.job_id[:8]} is already {job.state.value}; the link is recorded but nothing was stopped"
    return None


def link(store: Store, old_ref: str, new_ref: str) -> dict:
    """Record the link both ways (kv + one event each). Pure store write; no stop.

    Returns a relay-shaped dict: `accepted`, `detail`, `old`, `new`, and `warning` when OLD was already done."""
    old = followup.resolve_job(store, old_ref)
    if old is None:
        return {"accepted": False, "error_kind": "unknown_job", "detail": f"unknown job to replace: {old_ref}"}
    new = followup.resolve_job(store, new_ref)
    if new is None:
        return {"accepted": False, "error_kind": "unknown_job", "detail": f"unknown replacement job: {new_ref}"}
    if old.job_id == new.job_id:
        return {"accepted": False, "error_kind": "self_replace", "detail": "a job cannot replace itself"}
    if new.state in (JobState.STOPPED, JobState.FAILED):
        return {"accepted": False, "error_kind": "dead_replacement",
                "detail": f"replacement {new.job_id[:8]} is {new.state.value}; pick a live job"}
    prior = replaced_by(store, old.job_id)
    if prior and prior != new.job_id:
        return {"accepted": False, "error_kind": "already_replaced",
                "detail": f"job {old.job_id[:8]} is already replaced by {prior[:8]}"}
    warning = already_done_warning(store, old)
    out = {"accepted": True, "old": old.job_id, "new": new.job_id, "warning": warning,
           "already_linked": prior == new.job_id}
    if prior == new.job_id:
        out["detail"] = f"{old.job_id[:8]} already replaced by {new.job_id[:8]}"
        return out
    store.kv_set(old.job_id, K_REPLACED_BY, new.job_id)
    store.kv_set(new.job_id, K_REPLACES, old.job_id)
    store.append_event(old.job_id, what="replaced",
                       evidence={"replaced_by": new.job_id, "previous_state": old.state.value,
                                 **({"warning": warning} if warning else {})},
                       rule=RULE, judgment=Judgment.human)
    store.append_event(new.job_id, what="replaces", evidence={"replaces": old.job_id, "old_state": old.state.value},
                       rule=RULE, judgment=Judgment.human)
    out["detail"] = f"{old.job_id[:8]} replaced by {new.job_id[:8]}"
    return out


def replace(store: Store, old_ref: str, new_ref: str, *, stop: Callable[[str], dict]) -> dict:
    """`link` then stop OLD through `stop(job_id)` (the normal stop control) unless OLD is already done.

    OLD's branch and worktree are never touched. The stop's own receipt is reported, never a failure of the
    replace: a terminal OLD (STOPPED/FAILED) just records the link."""
    out = link(store, old_ref, new_ref)
    if not out["accepted"]:
        return out
    if out.get("warning"):
        out["stopped"] = False
        return out
    receipt = stop(out["old"])
    out["stopped"] = bool(receipt.get("accepted"))
    out["stop_detail"] = str(receipt.get("detail") or "")
    state = store.get_job(out["old"]).state.value
    if out["stopped"]:
        out["detail"] += f"; {out['old'][:8]} now {state} (branch and worktree kept)"
    else:
        out["detail"] += f"; not stopped: {out['stop_detail']}"
    return out


def labels(store: Store, job_id: str) -> list[str]:
    """`replaced by <id8>` / `replaces <id8>` for `steward jobs` and `steward result`."""
    out = []
    new = replaced_by(store, job_id)
    if new:
        out.append(f"replaced by {new[:8]}")
    old = replaces(store, job_id)
    if old:
        out.append(f"replaces {old[:8]}")
    return out
