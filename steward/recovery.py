"""Bounded recovery policy (SP-12, SP-13, SP-21).

`dead` is a code-only decision: no model is consulted before replacing an
exited worker. `suspect_stall` earns exactly one diagnosis per stall episode
— an episode is "diagnosed" until the heartbeat classifier reports
`progressing` again, at which point the marker resets and a future stall can
be diagnosed again. Every `replace`/`guide` action spends one recovery from
the job's budget (`recoveries_used`, persisted in the store so it survives a
controller restart); once the budget is spent the job is escalated to
`NEEDS_HUMAN` with the evidence that led there, never silently retried again.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from steward import exceptions
from steward.diagnosis import Diagnosis, Diagnoser
from steward.heartbeat import Class
from steward.models import Judgment
from steward.store import Store

_RECOVERIES_USED_KEY = "recoveries_used"
_DIAGNOSES_USED_KEY = "diagnoses_used"
_STALL_EPISODE_DIAGNOSED_KEY = "stall_episode_diagnosed"
_JEV_DIAGNOSED_KEY = "jev_diagnosed_fingerprint"
ISSUE = "recovery:worker"


def _ownership_on() -> bool:
    try:
        from steward import ownership

        return ownership.enabled()
    except Exception:  # noqa: BLE001 - no ownership layer means legacy recovery behavior
        return False


def _actor(kind: str):
    from steward.actor import Actor, ActorRef

    return ActorRef(Actor.MODEL_SUPERVISOR if kind == "model" else Actor.CONTROLLER, "recovery")


def _attempt(store: Store, job_id: str, *, change: str, hypothesis: str, by_model: bool, now: float) -> None:
    if not _ownership_on():
        return
    from steward import attempts

    attempts.fail_pending(store, job_id, ISSUE, detail={"why": "recovery needed again"}, now=now)
    attempts.record(store, job_id, issue_key=ISSUE, actor=_actor("model" if by_model else "code"),
                    hypothesis=hypothesis or "the worker stopped making progress", change=change,
                    success_observation="the heartbeat class returns to progressing", now=now)


def _rounds_exhausted(store: Store, job_id: str, rule: str, used: int, now: float) -> None:
    if not _ownership_on():
        return
    from steward import attempts, ownership

    attempts.fail_pending(store, job_id, ISSUE, detail={"why": rule}, now=now)
    b = ownership.request_strategy_review(store, job_id, blocker_id=None,
                                          reason=f"recovery: {used} unproductive rounds ({rule})",
                                          signature=ISSUE, now=now)
    store.append_event(job_id, what="recovery_strategy_review_requested",
                       evidence={"issue_key": ISSUE, "rounds": used, "rule": rule,
                                 "blocker_id": getattr(b, "blocker_id", None)},
                       rule="recovery_strategy", judgment=Judgment.code)


@dataclass(frozen=True)
class RecoveryPolicy:
    max_recoveries: int = 2


@dataclass(frozen=True)
class Action:
    kind: str  # "none" | "replace" | "guide" | "needs_human"
    detail: str
    judgment: Judgment
    rule: str
    diagnosis: Diagnosis | None = None


def decide(
    store: Store,
    job_id: str,
    cls: Class | str,
    diagnoser: Diagnoser | None,
    context: dict,
    policy: RecoveryPolicy | None = None,
    now: float | None = None,
) -> Action:
    now = time.time() if now is None else now
    policy = policy or RecoveryPolicy()
    cls = Class(cls) if not isinstance(cls, Class) else cls
    recoveries_used = store.kv_get(job_id, _RECOVERIES_USED_KEY, default=0)

    if cls == Class.dead:
        if recoveries_used >= policy.max_recoveries:
            _rounds_exhausted(store, job_id, "recovery_budget_exhausted", recoveries_used, now)
            return _needs_human(
                store, job_id, context,
                detail="recovery budget exhausted; worker exited and cannot be replaced",
                rule="recovery_budget_exhausted",
            )
        _spend_recovery(store, job_id)
        _attempt(store, job_id, change="replace the exited worker from its checkpoint",
                 hypothesis="the worker process exited", by_model=False, now=now)
        from steward import decision_trace
        store.append_event(
            job_id,
            what="worker replacement decided",
            evidence={"cls": cls.value, "recoveries_used": recoveries_used + 1,
                      "run_id": decision_trace.run_id(store, job_id)},
            rule="worker_exited",
            judgment=Judgment.code,
        )
        return Action(
            kind="replace",
            detail="worker process exited; replacing",
            judgment=Judgment.code,
            rule="worker_exited",
        )

    if cls == Class.suspect_stall:
        return _decide_suspect_stall(store, job_id, diagnoser, context, policy, recoveries_used, now)

    # progressing / waiting / quiet_known_process / idle: never spend a
    # diagnosis or a recovery. A return to progressing clears the "already
    # diagnosed this episode" marker so a *future* stall gets its own
    # diagnosis (SP-21: cap self-reflection per episode, not forever).
    if cls == Class.progressing:
        store.kv_set(job_id, _STALL_EPISODE_DIAGNOSED_KEY, False)
        store.kv_set(job_id, _JEV_DIAGNOSED_KEY, None)
        if _ownership_on() and recoveries_used:
            from steward import attempts

            store.kv_set(job_id, _RECOVERIES_USED_KEY, 0)
            n = attempts.progressed(store, job_id, ISSUE, detail={"class": "progressing"}, now=now)
            store.append_event(job_id, what="recovery_progress_observed",
                               evidence={"rounds_reset_from": recoveries_used, "attempts_progressed": n},
                               rule="recovery_strategy", judgment=Judgment.code)
    return Action(
        kind="none",
        detail=f"class={cls.value}; no recovery action",
        judgment=Judgment.code,
        rule="no_recovery_needed",
    )


def ruling_pending(store: Store, job_id: str) -> str | None:
    """RC-3 (2026-10-11): a worker waiting on a TL ruling is not stalled, so a diagnosis nudge would steer against it.
    Pending = the newest yield is STOPPED at the already-ruled SHA, or a send-back newer than it is unacknowledged."""
    from steward import pause_checkpoint

    if pause_checkpoint.pause_mode(store, job_id) != "off":  # spec 2026-10-13: a parked pause is waiting on us
        cp = pause_checkpoint.load(store, job_id)
        if cp["awaiting_review"] or cp["review_in_flight"]:
            return "a pause review is outstanding"
        if cp["waiting_on"] == "owner":
            return "waiting on the owner's decision (ask_owner)"

    last = store.last_event(job_id, ["yield_classified", "sent_back"])
    ev = (last.evidence or {}) if last else {}
    if last is not None and last.what == "yield_classified":
        stopped = ev.get("lane") == "STOPPED" and "same_as_last_ruled" in (ev.get("failed_preconditions") or [])
        return "last yield claims done at the ruled SHA" if stopped else None
    if last is not None:
        from steward.steering import Steering

        rec = next((i for i in Steering(store).all_instructions(job_id) if i["rev"] == ev.get("steering_rev")), None)
        if rec is not None and rec.get("status") != "acknowledged":
            return f"send-back rev {rec['rev']} not yet acknowledged"
    return None


def jev_diagnosis_capped(store: Store, job_id: str) -> str | None:
    """RC-3 item 4: the Jev path shares the stall-episode cap, one diagnosis per (yield sha, last_said hash).
    Returns why it is capped, else marks this state diagnosed and returns None."""
    if store.kv_get(job_id, _STALL_EPISODE_DIAGNOSED_KEY, default=False):
        return "stall episode already diagnosed"
    y = store.last_event(job_id, ["yield_classified"])
    ev = (y.evidence or {}) if y else {}
    fp = f"{ev.get('sha')}|{hashlib.sha1((ev.get('last_said') or '').encode()).hexdigest()[:12]}"
    if store.kv_get(job_id, _JEV_DIAGNOSED_KEY) == fp:
        return "already diagnosed at this yield"
    store.kv_set(job_id, _JEV_DIAGNOSED_KEY, fp)
    return None


def _decide_suspect_stall(
    store: Store,
    job_id: str,
    diagnoser: Diagnoser | None,
    context: dict,
    policy: RecoveryPolicy,
    recoveries_used: int,
    now: float,
) -> Action:
    already_diagnosed = store.kv_get(job_id, _STALL_EPISODE_DIAGNOSED_KEY, default=False)
    if already_diagnosed:
        return Action(
            kind="none",
            detail="stall episode already diagnosed this episode; awaiting recovery or progress",
            judgment=Judgment.code,
            rule="stall_episode_diagnosis_cap",
        )
    pending = ruling_pending(store, job_id)
    if pending:
        return Action(kind="none", detail=f"awaiting a tech-lead ruling ({pending}); not a stall",
                      judgment=Judgment.code, rule="ruling_pending")

    if diagnoser is None:
        # Sol switched off for this job: no model call; rules-only fallback
        # escalates to the human (no new authority) and spends the episode slot.
        store.kv_set(job_id, _STALL_EPISODE_DIAGNOSED_KEY, True)
        return _needs_human(
            store, job_id, context,
            detail="suspected stall; sol is off so no diagnosis was made",
            rule="sol_off_rules_only",
            extra={"sol": "off"},
        )

    try:
        exceptions.use(store, job_id, "supervisor-model", {"role": "sol"})
    except exceptions.UnregisteredPath:
        # Fail closed (D10): Sol never runs on an unregistered path. This
        # spends the episode's one diagnosis slot (like a real diagnosis
        # would) so a stuck registry doesn't re-escalate every tick.
        store.kv_set(job_id, _STALL_EPISODE_DIAGNOSED_KEY, True)
        return _needs_human(
            store, job_id, context,
            detail="supervisor-model path unregistered",
            rule="supervisor_model_unregistered",
            diagnosis=Diagnosis(verdict="needs_human", guidance="", hypothesis="supervisor-model path unregistered"),
        )

    diagnosis = diagnoser.diagnose(context)
    diagnoses_used = store.kv_get(job_id, _DIAGNOSES_USED_KEY, default=0) + 1
    store.kv_set(job_id, _DIAGNOSES_USED_KEY, diagnoses_used)
    store.kv_set(job_id, _STALL_EPISODE_DIAGNOSED_KEY, True)
    store.append_event(
        job_id,
        what="stall diagnosis produced",
        evidence={
            "verdict": diagnosis.verdict,
            "hypothesis": diagnosis.hypothesis,
            "diagnoses_used": diagnoses_used,
        },
        rule="suspect_stall_diagnosis",
        judgment=Judgment.model,
    )

    if diagnosis.verdict == "needs_human":
        return _needs_human(
            store, job_id, context,
            detail=diagnosis.hypothesis or "diagnosis recommended needs_human",
            rule="diagnosis_verdict_needs_human",
            diagnosis=diagnosis,
        )

    if recoveries_used >= policy.max_recoveries:
        _rounds_exhausted(store, job_id, "recovery_budget_exhausted", recoveries_used, now)
        return _needs_human(
            store, job_id, context,
            detail="recovery budget exhausted after diagnosis",
            rule="recovery_budget_exhausted",
            diagnosis=diagnosis,
        )

    _spend_recovery(store, job_id)
    _attempt(store, job_id, change=diagnosis.guidance or diagnosis.verdict,
             hypothesis=diagnosis.hypothesis, by_model=True, now=now)

    if diagnosis.verdict == "replace_worker":
        store.append_event(
            job_id,
            what="worker replacement authorized by diagnosis",
            evidence={"guidance": diagnosis.guidance, "hypothesis": diagnosis.hypothesis},
            rule="diagnosis_verdict_replace_worker",
            judgment=Judgment.model_informed_code_authorized,
        )
        return Action(
            kind="replace",
            detail=diagnosis.guidance or "replacing worker per diagnosis",
            judgment=Judgment.model_informed_code_authorized,
            rule="diagnosis_verdict_replace_worker",
            diagnosis=diagnosis,
        )

    # retry_with_guidance
    store.append_event(
        job_id,
        what="retry guidance authorized by diagnosis",
        evidence={"guidance": diagnosis.guidance, "hypothesis": diagnosis.hypothesis},
        rule="diagnosis_verdict_retry_with_guidance",
        judgment=Judgment.model_informed_code_authorized,
    )
    return Action(
        kind="guide",
        detail=diagnosis.guidance,
        judgment=Judgment.model_informed_code_authorized,
        rule="diagnosis_verdict_retry_with_guidance",
        diagnosis=diagnosis,
    )


def _spend_recovery(store: Store, job_id: str) -> None:
    used = store.kv_get(job_id, _RECOVERIES_USED_KEY, default=0)
    store.kv_set(job_id, _RECOVERIES_USED_KEY, used + 1)


def _needs_human(
    store: Store,
    job_id: str,
    context: dict,
    *,
    detail: str,
    rule: str,
    diagnosis: Diagnosis | None = None,
    extra: dict | None = None,
) -> Action:
    store.append_event(
        job_id,
        what="recovery escalated to human",
        evidence={
            "detail": detail,
            "signals": context.get("signals"),
            "handoff": context.get("handoff"),
            **(extra or {}),
        },
        rule=rule,
        judgment=Judgment.code,
    )
    return Action(kind="needs_human", detail=detail, judgment=Judgment.code, rule=rule, diagnosis=diagnosis)


def flow_recovery_check(store: Store, job_id: str, policy: RecoveryPolicy | None = None) -> dict:
    """v2.3 Flow `recovery`: does the runner's OWN predicate (its last heartbeat class is `dead` or
    `suspect_stall`) and the remaining budget already qualify recovery? Read only: Flow never spends a recovery,
    never diagnoses and never replaces a worker; a qualified job is recovered by the runner's existing loop."""
    policy = policy or RecoveryPolicy()
    cls = store.kv_get(job_id, "last_class")
    used = int(store.kv_get(job_id, _RECOVERIES_USED_KEY, default=0) or 0)
    left = max(0, policy.max_recoveries - used)
    if cls not in (Class.dead.value, Class.suspect_stall.value):
        return {"qualified": False, "reason": "runner_predicate_not_met", "class": cls, "budget_left": left}
    if left <= 0:
        return {"qualified": False, "reason": "recovery_budget_exhausted", "class": cls, "budget_left": 0}
    return {"qualified": True, "reason": "recovery_qualified_runner_owns", "class": cls, "budget_left": left}
