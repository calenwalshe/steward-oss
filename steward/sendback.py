"""Send back (rework): a human — or, for a stuck parked job, the tech lead — sends a NEEDS_HUMAN job back to work with new instructions.

Human-only relayed write (`steward send-back`, phone `S`, MCP `send_back_job`). The tech lead's reopen (spec 2026-10-04) calls this with judgment=model_informed_code_authorized; NEEDS_HUMAN -> RUNNING is not a human-only transition (machine._HUMAN_ONLY_TRANSITIONS), acceptance is. The job
continues in the SAME worktree/branch (its commits stay) with a fresh worker attempt.

`send_back` is the store half (runs inside the control pane): it validates the state, closes the
open approval WITHOUT stopping the job (the `deny` path stops it), records the instructions as a
steering revision (verb `focus`, or `goal` with `--goal-append`, which also appends to the stored
contract goal), flips NEEDS_HUMAN -> RUNNING and leaves a `rework_pending` marker. The launch half
(`cli._h_send_back`) then opens a fresh job tab whose `steward run` sees the marker and calls
`Runner.start_rework`, which dispatches the new attempt.
"""

from __future__ import annotations

import dataclasses
from datetime import date

from steward import approvals as approvals_module
from steward import masking
from steward.contract import load_contract, render
from steward.models import JobState, Judgment
from steward.steering import Steering
from steward.store import Store

REWORK_KEY = "rework_pending"
EVENT_CAP = 500


def cap_text(text: str, n: int = EVENT_CAP) -> str:
    text = " ".join(masking.mask(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _send_back_live(store: Store, job_id: str, instructions: str, *, origin: str, request_id: str | None,
                    judgment: Judgment) -> dict:
    """WORKER_FINISHED, verification queued/running in the background, worker still attached (mechanics item 12):
    record the instructions as a pending steer. The job's runner pre-empts the background verification on its next
    tick (`Runner._bg_verify_tick`): delivers the steer to the live worker, consumes the verification, withdraws the
    job's queued check runs and returns the job to RUNNING. No new tab or attempt — the worker keeps its context."""
    receipt = Steering(store).record(job_id, "focus", instructions, request_id=request_id, origin=origin)
    if not receipt.accepted:
        return {"accepted": False, "detail": receipt.detail}
    rev = len(Steering(store).all_instructions(job_id))
    closed = approvals_module.close_for_send_back(store, job_id, "sent back",
                                                   decided_by="human" if judgment == Judgment.human else origin)
    closed += approvals_module.close_tech_lead_superseded(store, job_id, "sent back")
    store.append_event(
        job_id, what="sent_back",
        evidence={"instructions": cap_text(instructions), "by": origin, "goal_append": False, "steering_rev": rev,
                  "closed_approvals": closed, "live": True},
        rule="operator_send_back" if judgment == Judgment.human else "tech_lead_send_back", judgment=judgment,
    )
    store.kv_set(job_id, "verify_rounds", 0)
    return {"accepted": True, "live": True, "steering_rev": rev, "closed_approvals": closed,
            "state": JobState.WORKER_FINISHED.value,
            "detail": (f"{job_id}\tWORKER_FINISHED (sent back; steering rev {rev}: the runner pre-empts the "
                       "background verification and delivers it to the live worker on its next tick)")}


def rework_pending(store: Store, job_id: str) -> dict | None:
    return store.kv_get(job_id, REWORK_KEY) or None


def send_back(
    store: Store, job_id: str, instructions: str, *, goal_append: bool = False, origin: str = "cli",
    request_id: str | None = None, judgment: Judgment = Judgment.human,
) -> dict:
    job = store.get_job(job_id)
    if job is None:
        return {"accepted": False, "detail": f"unknown job: {job_id}", "error_kind": "unknown_job"}
    from steward import bg_verify

    live = job.state == JobState.WORKER_FINISHED and bg_verify.pending(store, job_id)
    if job.state != JobState.NEEDS_HUMAN and not live:
        need = ("NEEDS_HUMAN, or WORKER_FINISHED with verification in the background (this one is verifying "
                "inline, under 2 min: retry when it parks)") if job.state == JobState.WORKER_FINISHED else "NEEDS_HUMAN"
        return {
            "accepted": False, "error_kind": "illegal_state",
            "detail": f"job {job_id[:8]} is {job.state.value}; send-back needs {need}",
        }
    instructions = (instructions or "").strip()
    if not instructions:
        return {"accepted": False, "detail": "send-back needs instructions", "error_kind": "empty"}
    if live and goal_append:
        return {"accepted": False, "error_kind": "illegal_state",
                "detail": "--goal-append needs NEEDS_HUMAN; a live send-back steers the worker with focus text"}
    if live:
        return _send_back_live(store, job_id, instructions, origin=origin, request_id=request_id, judgment=judgment)

    verb = "goal" if goal_append else "focus"
    receipt = Steering(store).record(job_id, verb, instructions, request_id=request_id, origin=origin)
    if not receipt.accepted:
        return {"accepted": False, "detail": receipt.detail}
    rev = len(Steering(store).all_instructions(job_id))

    closed = approvals_module.close_for_send_back(
        store, job_id, "sent back",
        decided_by="human" if judgment == Judgment.human else origin,
    )
    closed += approvals_module.close_tech_lead_superseded(store, job_id, "sent back")

    result: dict = {"accepted": True, "steering_rev": rev, "closed_approvals": closed}
    if goal_append:
        contract = load_contract(store, job_id)
        if contract is not None:
            contract = dataclasses.replace(
                contract, goal=f"{contract.goal}\n\nREWORK ({date.today().isoformat()}): {instructions}",
                revision=contract.revision + 1,
            )
            store.kv_set(job_id, "contract", dataclasses.asdict(contract))
            result["contract_text"] = render(contract)

    store.append_event(
        job_id, what="sent_back",
        evidence={"instructions": cap_text(instructions), "by": origin, "goal_append": goal_append,
                  "steering_rev": rev, "closed_approvals": closed},
        rule="operator_send_back" if judgment == Judgment.human else "tech_lead_send_back", judgment=judgment,
    )
    try:
        import time as _time

        from steward import actor as actor_module
        from steward import decision_trace

        decision_trace.safe_record(store, job_id=job_id,
                                   actor=actor_module.classify(origin, actor_module.current()), stage="effect",
                                   decision="send_back", now=_time.time(), evidence_refs=[f"steering_rev:{rev}"])
    except Exception:  # noqa: BLE001 - tracing never blocks a send-back
        pass

    # A fresh attempt starts clean: a later `done` goes through verification/closeout again.
    store.kv_set(job_id, "verify_rounds", 0)
    store.kv_set(job_id, "last_class", None)
    store.kv_set(job_id, "stall_episode_diagnosed", False)
    store.kv_set(job_id, REWORK_KEY, {"rev": rev, "text": instructions, "goal_append": goal_append, "by": origin})
    store.set_state(
        job_id, JobState.RUNNING, what="sent back for rework; new attempt pending",
        evidence={"steering_rev": rev, "by": origin},
        rule="operator_send_back" if judgment == Judgment.human else "tech_lead_send_back", judgment=judgment,
    )
    result["state"] = JobState.RUNNING.value
    result["detail"] = f"{job_id}\tRUNNING (sent back; steering rev {rev})"
    return result
