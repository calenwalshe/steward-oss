"""Continuous job ownership: reconciliation over durable blockers (spec §4, §5, §8; Plan 1).

`ensure_classified` turns a legacy NEEDS_HUMAN park (closeout reasons, runner escalation rules, recovery, fleet) and
the waiting question queue into coexisting blockers; owner-only kinds are confirmed by code without paying for a
model review, everything else is a candidate for technical review. `reconcile_job` clears blockers whose explicit
clearing predicate is satisfied by current evidence (deterministic; an uncertain predicate goes to review, never
guessed true), resolves everything a terminal owner decision ended, publishes the projection and one
`ownership_status` event per change, and sends the owner one precise notice per confirmed blocker. Legacy job states
are untouched. Nothing here calls a model or the network; reviews run on the tech lead's own threads.
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Callable

from steward import blockers, decision_trace, hold, intents
from steward.actor import Actor, ActorRef
from steward.blockers import Blocker, HumanRequest
from steward.models import JobState, Judgment
from steward.store import Store

ENV_SWITCH = "STEWARD_OWNERSHIP"
CLASSIFIED_KEY = "ownership_classified_seq"
PROJECTION_KEY = "ownership_projection"
RECONCILED_AT_KEY = "ownership_reconciled_at"
NOTIFIED_PREFIX = "ownership_notified:"
STATUS_WHAT = "ownership_status"
CONTROLLER = ActorRef(Actor.CONTROLLER, "ownership")

COALESCE_S = 30.0
RECONCILE_EVERY_S = 30.0
AGING_MIN = 60.0
UNREVIEWED_FAILOPEN_S = AGING_MIN * 60.0
AGING_BOOST = 1000.0
MAX_CONCURRENT_REVIEWS = 3
REVIEW_LEASE_S = 360.0
REVIEW_KIND = "tech_review"
REVIEW_INTENT_KEY = "ownership_review_intent"
RECOVER_EVERY_S = 60.0
_LEAD_EVENTS = ("tech_lead_answered", "tech_lead_answer_discarded", "tech_lead_action", "tech_lead_reopen")
JEV_CONCERN_TTL_S = 3600.0
REWORK_LAUNCH_GRACE_S = 120.0
LAUNCH_RETRY_S = (60.0, 300.0, 900.0)
LAUNCH_LEASE_S = 120.0

STATUSES = ("none", "needs_owner", "technical_review_unavailable", "awaiting_technical_review", "waiting_external")
REVIEW_STATUSES = ("awaiting_technical_review", "technical_review_unavailable", "waiting_external")
PARK_RULES = frozenset({"acceptance_unresolved", "verify_rounds_exhausted", "finish_line_bounces_exhausted",
                        "bg_verify_unverified", "bg_verify_lost", "worker_start_blocked", "fleet_repair_no_executor",
                        "fleet_required_task_stuck", "fleet_closeout_unavailable", "fleet_repair_exhausted"})
PARK_WHATS = frozenset({"recovery escalated to human"})
_RULE_KINDS = {"verify_rounds_exhausted": "verification_failing", "finish_line_bounces_exhausted": "finish_line_unmet",
               "bg_verify_unverified": "verification_unverified", "bg_verify_lost": "verification_unverified",
               "worker_start_blocked": "worker_start_blocked",
               "fleet_repair_no_executor": "fleet_blocked", "fleet_required_task_stuck": "fleet_blocked",
               "fleet_closeout_unavailable": "fleet_blocked", "fleet_repair_exhausted": "fleet_blocked"}
_ACCEPTANCE_REASONS = (
    (re.compile(r"^\d+ baseline failure\(s\) still failing"), "baseline_failures_remaining"),
    (re.compile(r"^test-weakening detected"), "test_weakening"),
    (re.compile(r"^hard constraint violation"), "hard_constraint"),
    (re.compile(r"review-enforcement constraint\(s\) active$"), "review_constraint"),
    (re.compile(r"^contract has no done_when"), "contract_incomplete"),
    (re.compile(r"^fleet candidate verified"), "acceptance_decision"),
    (re.compile(r"^(?:deadline verification pending|acceptance unresolved \(deadline\))"), "deadline_evidence_pending"),
    (re.compile(r"^outcome unproven"), "outcome_unproven"),
    (re.compile(r"^checks passed — "), "acceptance_decision"),
)
_DONE_STATES = (JobState.STOPPING, JobState.STOPPED, JobState.RELEASED, JobState.FAILED, JobState.ACCEPTED)
_RESUMED_STATES = (JobState.RUNNING, JobState.PAUSED, JobState.WORKER_FINISHED, JobState.CHECKS_PASSED)
_PARK_KINDS = frozenset(set(_RULE_KINDS.values()) | {
    "verification_unverified", "baseline_failures_remaining", "test_weakening", "contract_incomplete",
    "deadline_evidence_pending", "worker_parked_unfinished", "recovery_exhausted", "unclassified_hold",
    "baseline_pending"})
_NEW_ATTEMPT_WHATS = ("worker attempt started", "sent back for rework; new attempt pending")

PREDICATES: dict[str, Callable[[Store, str, Blocker, dict], str]] = {}
CHEAP_CHECKS: dict[str, Callable[[Store, str, Blocker], dict | None]] = {}
MILESTONES: dict[str, Callable[[Store, str], dict]] = {}
DELIVERY_KINDS: set[str] = set()  # Plan 4 registers kinds that stay open on ACCEPTED jobs
# An unanswered owner question is the owner's decision, not the job's: accepting (or merging, or releasing) the job
# does not answer it, so it carries forward as an open owner decision until `steward answer` settles it. A STOPPED or
# FAILED job's questions still resolve with the job: the work they were asked for was abandoned.
CARRY_FORWARD_STATES = (JobState.ACCEPTED, JobState.RELEASED)
CARRY_FORWARD_KINDS = frozenset({"owner_question"})
# Kinds raised BECAUSE a job is stuck in a non-terminal "done" state (ACCEPTED / STOPPING): the done-state clear must
# not resolve them, or the job is silent again. They resolve with the job once it is terminal (spec 2026-10-08 §5.1
# "The ask": the liveness_stuck blocker is the stuck job's actor until the owner answers).
UNTIL_TERMINAL_KINDS = frozenset({"liveness_stuck"})
_DONE_REASONS = tuple(f"job {s.value}" for s in _DONE_STATES)
_KIND_REVIEWERS: dict[str, str] = {}
_STRATEGY_HOOKS: list[Callable] = []
_authority_fn: Callable[[Store, str], int] | None = None


def enabled() -> bool:
    if os.environ.get(ENV_SWITCH, "1").strip().lower() in ("0", "false", "off", "no"):
        return False
    try:
        from steward import tech_lead_policy

        return bool(tech_lead_policy.load_or_defaults()[0].ownership_enabled)
    except Exception:  # noqa: BLE001 - an unreadable policy keeps the default (on)
        return True


def authority_rev(store: Store, job_id: str) -> int:
    """The revision decisions are bound to (Plan 2 installs grant revisions via `set_authority_rev_fn`)."""
    return int(_authority_fn(store, job_id)) if _authority_fn else int(store.get_revision(job_id))


def set_authority_rev_fn(fn: Callable[[Store, str], int] | None) -> None:
    global _authority_fn
    _authority_fn = fn


def register_predicate(name: str, fn: Callable[[Store, str, Blocker, dict], str]) -> None:
    PREDICATES[name] = fn


def register_cheap_check(name: str, fn: Callable[[Store, str, Blocker], dict | None]) -> None:
    CHEAP_CHECKS[name] = fn


def register_milestone(name: str, fn: Callable[[Store, str], dict]) -> None:
    MILESTONES[name] = fn


def register_kind_reviewer(kind: str, owner: str) -> None:
    """Route a blocker kind to its designated reviewer rather than the normal lead."""
    _KIND_REVIEWERS[kind] = owner


def register_strategy_hook(fn: Callable) -> None:
    """Register a non-blocking observer for strategy review requests."""
    if fn not in _STRATEGY_HOOKS:
        _STRATEGY_HOOKS.append(fn)


# -- classification --------------------------------------------------------------------------------------------


def park_event(events):
    for e in reversed(events):
        if e.rule in PARK_RULES or e.what in PARK_WHATS:
            return e
    return None


def _spec(kind: str, subject: str, claim: str, evidence: list[str], basis: int, rule: str, **kw) -> dict:
    return {"kind": kind, "subject": subject, "claim": claim, "evidence": evidence, "basis_seq": basis,
            "origin_rule": rule, **kw}


def classify_needs_human(store: Store, job_id: str, events) -> list[dict]:
    """Code-only: the blockers a NEEDS_HUMAN job's current park implies (kwargs for `blockers.raise_blocker`)."""
    park = park_event(events)
    claim = hold.worker_claim(events)
    basis = park.seq if park else (events[-1].seq if events else 0)
    ref = f"event:{basis}"
    out: list[dict] = []
    if park is None:
        last = events[-1].what if events else "parked"
        return [_spec("unclassified_hold", "needs_human", f"parked: {last}", [ref], basis, "")]
    if park.rule == "acceptance_unresolved":
        from steward import closeout

        if park.what == closeout.UNATTRIBUTED_WHAT:
            return [_spec("baseline_pending", "baseline_pending", "waiting for the background baseline to finish",
                          [ref], basis, park.rule, disposition="waiting_external",
                          wake={"kind": "external", "on": "baseline_done"})]
        if park.what == closeout.UNVERIFIED_WHAT:
            out.append(_spec("verification_unverified", "verification_unverified",
                             "; ".join((park.evidence or {}).get("reasons") or ["tests unverified"]), [ref], basis,
                             park.rule))
        elif park.what == closeout.CANDIDATE_READY_WHAT:
            out.append(_spec("acceptance_decision", "acceptance", "deadline candidate ready", [ref], basis, park.rule))
        else:
            for reason in (park.evidence or {}).get("reasons") or []:
                kind = next((k for rx, k in _ACCEPTANCE_REASONS if rx.search(str(reason))), "unclassified_hold")
                out.append(_spec(kind, kind if kind != "unclassified_hold" else str(reason), str(reason),
                                 [ref, str(reason)[:120]], basis, park.rule))
        if claim == "unfinished":
            out.append(_spec("worker_parked_unfinished", "worker_parked_unfinished",
                             "the worker went idle without claiming done", [ref], basis, park.rule))
        if not out:
            out.append(_spec("acceptance_decision", "acceptance", "checks passed; acceptance unresolved", [ref], basis,
                             park.rule))
        if any(not blockers.spec(s["kind"]).owner_only for s in out):  # acceptance waits for technical review
            out = [s for s in out if s["kind"] not in ("acceptance_decision", "review_constraint")]
        return out
    if park.what in PARK_WHATS:
        detail = str((park.evidence or {}).get("detail") or park.rule)
        return [_spec("recovery_exhausted", park.rule, f"recovery escalated: {detail}", [ref], basis, park.rule)]
    kind = _RULE_KINDS.get(park.rule, "unclassified_hold")
    return [_spec(kind, kind if kind != "unclassified_hold" else park.what, park.what, [ref], basis, park.rule)]


def _question_specs(store: Store, job_id: str) -> list[dict]:
    from steward import questions

    out = []
    for r in questions.load(store, job_id):
        if r.get("status") in questions.WAITING:
            s = _spec("owner_question", str(r.get("qid")), str(r.get("question") or r.get("title") or "")[:400],
                      [f"question:{r.get('qid')}"], 0, "question_waiting",
                      disposition="confirmed_human", responsible="owner",
                      affected=("all",) if r.get("blocking") else ("progress",),
                      clearing={"predicate": "question_answered", "params": {"qid": r.get("qid")}})
            if _closed_with_job(store, job_id, s["subject"]):
                s["recur_after_s"] = 0.0  # the job's end closed it, not an answer: reopen it as a new episode
            out.append(s)
    return out


def _closed_with_job(store: Store, job_id: str, qid: str) -> bool:
    """The question's latest blocker was resolved only because its job finished (`job ACCEPTED` …), while the question
    itself is still waiting: the pre-carry-forward sweep hid a live owner decision."""
    key = blockers.issue_key("owner_question", qid)
    r = store._conn.execute("SELECT disposition, resolution_json FROM blockers WHERE job_id = ? AND issue_key = ?"
                            " ORDER BY episode DESC LIMIT 1", (job_id, key)).fetchone()
    if r is None or r["disposition"] != "resolved":
        return False
    try:
        reason = (json.loads(r["resolution_json"] or "{}") or {}).get("reason")
    except ValueError:
        return False
    return reason in _DONE_REASONS


def carries_forward(job, b: Blocker) -> bool:
    """An open owner decision that outlives its finished job (see CARRY_FORWARD_STATES)."""
    return job.state in CARRY_FORWARD_STATES and b.kind in CARRY_FORWARD_KINDS


def carried_label(job) -> str:
    from steward import jobview

    return f"{jobview.CARRIED_PREFIX} from {job.state.value.lower()} job {job.job_id[:8]}"


def carried_decisions(store: Store, job_ids=None) -> dict[str, list[dict]]:
    """`{job_id: [decision, ...]}`: the open owner decisions of finished (ACCEPTED/RELEASED) jobs, for views that
    otherwise only list active jobs. Read-only; one query plus one job read per job that has any."""
    rows = store._conn.execute("SELECT DISTINCT job_id FROM blockers WHERE disposition = 'confirmed_human' AND kind IN"
                               f" ({','.join('?' * len(CARRY_FORWARD_KINDS))})",
                               tuple(sorted(CARRY_FORWARD_KINDS))).fetchall()
    want = None if job_ids is None else set(job_ids)
    out: dict[str, list[dict]] = {}
    for (jid,) in rows:
        if want is not None and jid not in want:
            continue
        job = store.get_job(jid)
        if job is None or job.state not in CARRY_FORWARD_STATES:
            continue
        label = carried_label(job)
        out[jid] = [{"job_id": jid, "state": job.state.value, "blocker_id": b.blocker_id,
                     "qid": (b.clearing.get("params") or {}).get("qid"), "claim": b.claim, "label": label,
                     "text": f"{label}: {b.claim}"[:300]}
                    for b in blockers.open_blockers(store, jid)
                    if b.disposition == "confirmed_human" and carries_forward(job, b)]
        if not out[jid]:
            del out[jid]
    return out


# -- reopening owner decisions the pre-carry-forward sweep closed ------------------------------------------------
_JOB_END_REASONS = tuple(f"job {s.value}" for s in CARRY_FORWARD_STATES)  # what that sweep wrote: "job ACCEPTED", …
REOPEN_EVENT = "owner_decision_reopened"


class ReopenRefused(Exception):
    """`reopen_decision` will not reopen this question; `kind` is the receipt's `error_kind`."""

    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind


def _latest_question_blocker(store: Store, job_id: str, qid: str) -> Blocker | None:
    r = store._conn.execute("SELECT blocker_id FROM blockers WHERE job_id = ? AND issue_key = ? ORDER BY episode DESC"
                            " LIMIT 1", (job_id, blockers.issue_key("owner_question", qid))).fetchone()
    return blockers.get(store, r["blocker_id"]) if r is not None else None


def closed_by_job_end(store: Store, job_ids=None) -> list[dict]:
    """The owner questions `reopen_decision` would reopen: on an ACCEPTED/RELEASED job, the question is still waiting
    but its latest blocker episode was resolved with `job ACCEPTED`/`job RELEASED` (the sweep before carry-forward,
    84bc6350 / 58b8065c). Read-only."""
    from steward import questions

    if job_ids is None:
        job_ids = [r[0] for r in store._conn.execute("SELECT DISTINCT job_id FROM blockers WHERE kind = 'owner_question'"
                                                     " AND disposition = 'resolved' ORDER BY job_id").fetchall()]
    out = []
    for jid in job_ids:
        job = store.get_job(jid)
        if job is None or job.state not in CARRY_FORWARD_STATES:
            continue
        for r in questions.load(store, jid):
            if r.get("status") not in questions.WAITING:
                continue
            b = _latest_question_blocker(store, jid, str(r.get("qid")))
            reason = (b.resolution or {}).get("reason") if b is not None and not b.is_open else None
            if reason in _JOB_END_REASONS:
                out.append({"job_id": jid, "state": job.state.value, "qid": str(r.get("qid")),
                            "title": str(r.get("title") or r.get("question") or "")[:120],
                            "blocker_id": b.blocker_id, "episode": b.episode, "resolved_reason": reason})
    return out


def reopen_decision(store: Store, job_id: str, qid: str, *, actor: ActorRef, reason: str,
                    now: float | None = None) -> dict:
    """Reopen one owner question the job's end closed while it was unanswered: a new `owner_question` episode
    (confirmed_human, raised by `actor`) plus an `owner_decision_reopened` event naming the actor and the reason. It
    never answers or re-asks the question. Idempotent: an already-open decision returns `reopened: False`. Raises
    `ReopenRefused` for an unknown job/question, a job that is not ACCEPTED/RELEASED, a settled question, or a blocker
    closed by anything other than the job's end."""
    from steward import questions

    now = time.time() if now is None else now
    job = store.get_job(job_id)
    if job is None:
        raise ReopenRefused("unknown_job", f"unknown job: {job_id}")
    if job.state not in CARRY_FORWARD_STATES:
        raise ReopenRefused("refused", f"job {job_id[:8]} is {job.state.value}: only an ACCEPTED or RELEASED job"
                                       " carries an owner decision forward")
    rec = next((r for r in questions.load(store, job_id) if str(r.get("qid")) == qid), None)
    if rec is None:
        raise ReopenRefused("unknown_question", f"job {job_id[:8]} has no question {qid}")
    if rec.get("status") not in questions.WAITING:
        raise ReopenRefused("answered", f"question {qid} is {rec.get('status')}: a settled question is not reopened")
    prev = _latest_question_blocker(store, job_id, qid)
    if prev is not None and prev.is_open:
        return {"reopened": False, "blocker_id": prev.blocker_id, "episode": prev.episode,
                "detail": f"{qid} is already an open owner decision ({prev.blocker_id})"}
    closed_why = (prev.resolution or {}).get("reason") if prev is not None else None
    if closed_why not in _JOB_END_REASONS:
        raise ReopenRefused("refused", f"{qid} was not closed by the job's end"
                                       f" ({'no blocker' if prev is None else f'resolved: {closed_why}'})")
    s = next(s for s in _question_specs(store, job_id) if s["subject"] == qid)
    s.update(recur_after_s=0.0, origin_rule="owner_decision_reopen")
    b = _raise(store, job_id, s, now, actor=actor)
    store.append_event(job_id, what=REOPEN_EVENT,
                       evidence={"qid": qid, "blocker_id": b.blocker_id, "episode": b.episode,
                                 "previous_blocker_id": prev.blocker_id, "previous_reason": closed_why,
                                 "by": actor.label(), "reason": reason[:400]},
                       rule="owner_decision_reopen", judgment=blockers._judgment(actor))
    return {"reopened": True, "blocker_id": b.blocker_id, "episode": b.episode,
            "detail": f"{qid} reopened as an open owner decision ({b.blocker_id}, episode {b.episode})"}


def _owner_request(kind: str, b_claim: str, evidence: list[str]) -> HumanRequest:
    why = {"acceptance_decision": "acceptance is the owner's decision (Steward never accepts from NEEDS_HUMAN)",
           "hard_constraint": "an explicit owner constraint was violated; only the owner may waive or redirect",
           "review_constraint": "the owner's review-enforced constraints need the owner's acceptance review",
           "owner_question": "only the owner can answer an owner question"}.get(kind, "reserved to the owner")
    decision = {"acceptance_decision": f"Accept or send back: {b_claim}",
                "hard_constraint": f"Decide how to proceed: {b_claim}"}.get(kind, b_claim)
    return blockers.validate_human_request({"decision": decision, "why_authority_insufficient": why,
                                            "evidence": evidence, "continuing": "nothing else runs for this job"})


def _raise(store: Store, job_id: str, s: dict, now: float, notifier=None, notify_fn=None,
           actor: ActorRef = CONTROLLER) -> Blocker:
    s = dict(s)
    ks = blockers.spec(s["kind"])
    want_confirm = ks.owner_only and s.get("disposition", "candidate") == "candidate"
    b, created = blockers.raise_blocker(store, job_id, origin_actor=actor, now=now,
                                        authority_rev=authority_rev(store, job_id), **s)
    if created:
        decision_trace.safe_record(store, job_id=job_id, actor=CONTROLLER, stage="effect",
                                   decision=f"raise_blocker:{b.kind}", now=now, blocker_id=b.blocker_id,
                                   evidence_refs=b.evidence, authority_rev=b.authority_rev,
                                   versions={"rule": b.origin_rule})
    if created and want_confirm:
        b = confirm_human(store, b.blocker_id, _owner_request(b.kind, b.claim, list(b.evidence)), actor=CONTROLLER,
                          now=now, notifier=notifier, notify_fn=notify_fn)
    elif created and b.disposition == "confirmed_human" and b.human_request is None:
        with store._conn:
            store._conn.execute("UPDATE blockers SET human_request_json = ? WHERE blocker_id = ?",
                                (json.dumps(_owner_request(b.kind, b.claim, list(b.evidence)).to_dict()),
                                 b.blocker_id))
        b = blockers.get(store, b.blocker_id)
    return b


def _park_events(store: Store, job_id: str) -> list:
    """What `park_event` / `classify_needs_human` read, without the job's whole history (TUI speed plan 12b): the
    park events, the worker's done/unfinished markers, and the job's newest event (`events[-1]`)."""
    events = store.events_of(job_id, PARK_WHATS, rules=PARK_RULES | set(hold._MARKER_RULES))
    newest = store.events_since(job_id, _last_seq(store, job_id) - 1)
    if newest and (not events or events[-1].seq != newest[-1].seq):
        events.append(newest[-1])
    return events


def _last_seq(store: Store, job_id: str) -> int:
    r = store._conn.execute("SELECT MAX(seq) FROM events WHERE job_id = ?", (job_id,)).fetchone()
    return int(r[0] or 0)


def _classify(store: Store, job, now: float, notifier=None, notify_fn=None) -> None:
    if job.state == JobState.NEEDS_HUMAN:
        for s in classify_needs_human(store, job.job_id, _park_events(store, job.job_id)):
            _raise(store, job.job_id, s, now, notifier, notify_fn)
    if job.state not in _DONE_STATES or job.state in CARRY_FORWARD_STATES:
        for s in _question_specs(store, job.job_id):
            _raise(store, job.job_id, s, now, notifier, notify_fn)


def ensure_classified(store: Store, job_id: str, *, now: float | None = None, head: str | None = None,
                      notifier=None, notify_fn=None) -> list[Blocker]:
    """Classify + reconcile when the job's event log moved since the last pass; cheap no-op otherwise."""
    if not enabled():
        return []
    now = time.time() if now is None else now
    if (store.kv_get(job_id, CLASSIFIED_KEY) == _last_seq(store, job_id)
            and notifier is None and notify_fn is None):
        return blockers.open_blockers(store, job_id)
    reconcile_job(store, job_id, now=now, head=head, notifier=notifier, notify_fn=notify_fn)
    return blockers.open_blockers(store, job_id)


# -- clearing predicates ---------------------------------------------------------------------------------------


def _since(store: Store, job_id: str, b: Blocker):
    return store.events_since(job_id, b.basis_seq)


def _p_job_left_needs_human(store, job_id, b, facts):
    return "holds" if facts["state"] == JobState.NEEDS_HUMAN else "cleared"


def _p_job_resumed(store, job_id, b, facts):
    if facts["state"] not in _RESUMED_STATES:
        return "holds"
    return "cleared" if any(e.what == "worker attempt started" for e in _since(store, job_id, b)) or \
        facts["state"] in (JobState.WORKER_FINISHED, JobState.CHECKS_PASSED) else "holds"


def _p_worker_started(store, job_id, b, facts):
    return "cleared" if any(e.what == "worker attempt started" for e in _since(store, job_id, b)) else "holds"


def _p_verification_current_green(store, job_id, b, facts):
    head = facts.get("head")
    if not head:
        return "uncertain"
    rows = [c for c in store.get_checks(job_id, label="verify") if c["commit_sha"] == head]
    if not rows:
        return "holds"
    return "cleared" if all(c["exit_code"] == 0 for c in rows) else "holds"


def _p_baseline_reattributed(store, job_id, b, facts):
    from steward import closeout

    return "cleared" if any(e.what == closeout.REATTRIBUTED_WHAT for e in _since(store, job_id, b)) else "holds"


def _p_question_answered(store, job_id, b, facts):
    from steward import questions

    qid = (b.clearing.get("params") or {}).get("qid")
    rec = next((r for r in questions.load(store, job_id) if r.get("qid") == qid), None)
    if rec is None:
        return "uncertain"
    return "holds" if rec.get("status") in questions.WAITING else "cleared"


def _p_approval_decided(store, job_id, b, facts):
    from steward import approvals

    aid = (b.clearing.get("params") or {}).get("approval_id")
    rec = approvals.get_approval(store, job_id, aid) if aid else None
    if rec is None:
        return "holds" if aid is None else "uncertain"
    return "holds" if rec["status"] == "pending" else "cleared"


def _p_signal_expired(store, job_id, b, facts):
    last = float((b.wake or {}).get("last_seen") or b.updated_epoch)
    return "cleared" if facts["now"] - last >= JEV_CONCERN_TTL_S else "holds"


def _p_watchdog_target_healthy(store, job_id, b, facts):
    from steward import watchdog  # issue W: a breaker blocker clears once the runner is healthy and the breaker reset

    return watchdog.target_healthy_predicate(store, job_id, b, facts)


def _p_checks_passed_since(store, job_id, b, facts):
    return "cleared" if any(e.what == "checks passed at exact commit" for e in _since(store, job_id, b)) else "holds"


for _n, _f in {"job_left_needs_human": _p_job_left_needs_human, "job_resumed": _p_job_resumed,
               "worker_started": _p_worker_started, "verification_current_green": _p_verification_current_green,
               "baseline_reattributed": _p_baseline_reattributed, "question_answered": _p_question_answered,
               "approval_decided": _p_approval_decided, "signal_expired": _p_signal_expired,
               "checks_passed_since": _p_checks_passed_since,
               "watchdog_target_healthy": _p_watchdog_target_healthy}.items():
    register_predicate(_n, _f)


# -- reconcile, projection, view -------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconcileResult:
    job_id: str
    cleared: list[str]
    status: str
    changed: bool


@dataclass(frozen=True)
class OwnerView:
    status: str
    text: str
    blocker_ids: tuple[str, ...]
    next_retry_at: float | None
    next_actor: str


def _hhmm(epoch: float | None) -> str:
    return time.strftime("%H:%M", time.localtime(epoch)) if epoch else "?"


def owner_view(store: Store, job_id: str, *, now: float | None = None) -> OwnerView:
    def with_status(text: str) -> str:
        try:
            from steward import independent_review, waits

            extra = [s for s in (independent_review.status_suffix(store, job_id),
                                 waits.status_suffix(store, job_id, now=now if now is not None else time.time())) if s]
            return (text + " · " + " · ".join(extra))[:300] if extra else text[:300]
        except Exception:  # noqa: BLE001 - status text never fails the view
            return text[:300]

    open_ = blockers.open_blockers(store, job_id)
    confirmed = [b for b in open_ if b.disposition == "confirmed_human"]
    reviewable = [b for b in open_ if b.disposition in ("candidate", "reviewing")]
    waiting = [b for b in open_ if b.disposition == "waiting_external"]
    ids = tuple(b.blocker_id for b in open_)
    if confirmed:
        top = max(confirmed, key=lambda b: blockers.spec(b.kind).impact)
        text = "decide: " + str((top.human_request or {}).get("decision") or top.claim)
        job = store.get_job(job_id)
        if job is not None and carries_forward(job, top):
            text = f"{carried_label(job)}: {top.claim}"
        if len(confirmed) > 1:
            text += f" (+{len(confirmed) - 1} more owner decision(s))"
        if reviewable:
            text += f" ({len(reviewable)} under technical review)"
        return OwnerView("needs_owner", with_status(text), ids, None, "owner")
    unavailable = [b for b in reviewable if b.review_state == "unavailable"]
    if unavailable:
        nxt = min((b.next_review_epoch or 0) for b in unavailable) or None
        return OwnerView("technical_review_unavailable",
                         with_status(f"technical review unavailable; retry {_hhmm(nxt)}: {unavailable[0].claim}"), ids, nxt,
                         "controller")
    if reviewable:
        top = max(reviewable, key=lambda b: (blockers.spec(b.kind).impact, -b.created_epoch))
        more = f" (+{len(reviewable) - 1} more)" if len(reviewable) > 1 else ""
        return OwnerView("awaiting_technical_review", with_status(f"awaiting technical review: {top.claim}{more}"), ids,
                         None, "tech_lead")
    if waiting:
        return OwnerView("waiting_external", with_status(f"waiting: {waiting[0].claim}"), ids, None, "external")
    return OwnerView("none", with_status(""), ids, None, "worker")


def projection(store: Store, job_id: str) -> dict:
    return store.kv_get(job_id, PROJECTION_KEY) or {}


def _milestones(store: Store, job_id: str, job) -> dict:
    # Only what the two checks below read (the worker's done/unfinished markers, the exact-commit pass): the delivery
    # sweep calls this for every accepted job every 30 s, and a full history decode made that tick ~1.5 s.
    events = store.events_of(job_id, ("checks passed at exact commit",), rules=tuple(hold._MARKER_RULES))
    out = {
        "worker_completion": {"status": "done" if job.state in (JobState.WORKER_FINISHED, JobState.CHECKS_PASSED,
                                                                 JobState.ACCEPTED, JobState.RELEASED)
                              or hold.worker_claim(events) == "done" else "pending"},
        "verification": {"status": "done" if any(e.what == "checks passed at exact commit" for e in events)
                         else "pending"},
        "acceptance": {"status": "done" if job.state in (JobState.ACCEPTED, JobState.RELEASED) else "pending"},
    }
    for name, fn in MILESTONES.items():
        try:
            out[name] = fn(store, job_id)
        except Exception as exc:  # noqa: BLE001 - a broken milestone source is visible, never fatal
            out[name] = {"status": "unknown", "error": f"{type(exc).__name__}"}
    return out


def _annotate_approvals(store: Store, job_id: str, view: OwnerView) -> None:
    from steward import approvals

    confirmed = {b.blocker_id for b in blockers.open_blockers(store, job_id) if b.disposition == "confirmed_human"}
    awaiting = view.status in REVIEW_STATUSES
    for a in approvals.list_approvals(store, job_id, status="pending"):
        if a.get("kind") not in ("general", "acceptance", "tech_lead"):
            continue
        want = awaiting and a.get("blocker_id") not in confirmed
        if bool(a.get("awaiting_review")) != want:
            approvals.annotate(store, job_id, a["id"], awaiting_review=want)


def _clear(store: Store, job, now: float, head: str | None) -> list[str]:
    cleared = []
    facts = {"head": head, "now": now, "state": job.state}
    for b in blockers.open_blockers(store, job.job_id):
        if (job.state in _DONE_STATES and not (job.state == JobState.ACCEPTED and b.kind in DELIVERY_KINDS)
                and not carries_forward(job, b)
                and not (b.kind in UNTIL_TERMINAL_KINDS and job.state in (JobState.ACCEPTED, JobState.STOPPING))):
            blockers.transition(store, b.blocker_id, "resolved", actor=CONTROLLER, reason=f"job {job.state.value}",
                                now=now)
            cleared.append(b.blocker_id)
            continue
        if (b.kind in _PARK_KINDS and b.origin_actor == CONTROLLER.label()
                and any(e.what in _NEW_ATTEMPT_WHATS for e in _since(store, job.job_id, b))):
            blockers.transition(store, b.blocker_id, "resolved", actor=CONTROLLER,
                                reason="superseded: a newer attempt started after this park", now=now)
            cleared.append(b.blocker_id)
            continue
        for name, check in CHEAP_CHECKS.items():
            hit = check(store, job.job_id, b)
            if hit:
                blockers.transition(store, b.blocker_id, "resolved", actor=CONTROLLER,
                                    reason=f"cheap check {name}: {hit.get('reason', '')}", now=now,
                                    evidence=hit.get("evidence") or ())
                cleared.append(b.blocker_id)
                break
        else:
            name = (b.clearing or {}).get("predicate") or blockers.spec(b.kind).predicate
            fn = PREDICATES.get(name or "")
            if fn is not None and fn(store, job.job_id, b, facts) == "cleared":
                blockers.transition(store, b.blocker_id, "resolved", actor=CONTROLLER, reason=f"predicate {name}",
                                    now=now, resolution={"predicate": name})
                cleared.append(b.blocker_id)
    return cleared


def _cause_predicate(b: Blocker) -> str | None:
    """The predicate that says whether the blocker's cause still holds. A confirmed escalation's recorded clearing is
    `approval_decided` (the owner's answer), which hides the cause; for a lead-resolvable kind the kind's own predicate
    is the cause."""
    name = (b.clearing or {}).get("predicate") or blockers.spec(b.kind).predicate
    if name == "approval_decided" and not blockers.spec(b.kind).owner_only:
        alt = blockers.spec(b.kind).predicate
        if alt and alt != "approval_decided":
            return alt
    return name


def _clear_cause_cleared(store: Store, job, now: float, head: str | None) -> list[str]:
    """Resolve every open blocker whose clearing condition no longer holds ('cause cleared') and close the pending
    tech_lead approvals linked to it as superseded, with one event per job."""
    from steward import approvals

    facts = {"head": head, "now": now, "state": job.state}
    pending = {a.get("blocker_id") for a in approvals.list_approvals(store, job.job_id, status="pending")
               if a.get("kind") == "tech_lead" and a.get("blocker_id")}
    cleared = []
    for b in blockers.open_blockers(store, job.job_id):
        if b.blocker_id not in pending:
            continue
        fn = PREDICATES.get(_cause_predicate(b) or "")
        if fn is None or fn(store, job.job_id, b, facts) != "cleared":
            continue
        name = _cause_predicate(b)
        blockers.transition(store, b.blocker_id, "resolved", actor=CONTROLLER, reason="cause cleared", now=now,
                            resolution={"predicate": name})
        cleared.append(b.blocker_id)
    if cleared:
        approvals.close_tech_lead_superseded(store, job.job_id, "cause cleared", blocker_ids=set(cleared))
    return cleared


def _publish(store: Store, job, now: float, notifier=None, notify_fn=None) -> tuple[str, bool]:
    park = park_event(_park_events(store, job.job_id)) if job.state == JobState.NEEDS_HUMAN else None
    if park is not None and park.rule == "acceptance_unresolved" and not any(
            b.kind != "owner_question" for b in blockers.open_blockers(store, job.job_id)):
        # every non-question blocker of this park was resolved (e.g. the lead overruled a false positive): what is
        # left is the owner's acceptance decision (spec §2: acceptance stays human-owned)
        _raise(store, job.job_id, _spec("acceptance_decision", "acceptance",
                                        "technical review cleared every technical blocker", [f"event:{_last_seq(store, job.job_id)}"],
                                        _last_seq(store, job.job_id), "ownership"), now, notifier, notify_fn)
    for b in blockers.open_blockers(store, job.job_id):
        if b.disposition == "confirmed_human" and blockers.spec(b.kind).notify_on_confirm:
            notify_owner(store, b, notifier=notifier, notify_fn=notify_fn, now=now)
    view = owner_view(store, job.job_id, now=now)
    owner_kinds = sorted({b.kind for b in blockers.open_blockers(store, job.job_id)
                          if b.disposition == "confirmed_human"})
    proj = {"status": view.status, "text": view.text, "blockers": list(view.blocker_ids),
            "next_actor": view.next_actor, "next_retry_at": view.next_retry_at, "state": job.state.value,
            "milestones": _milestones(store, job.job_id, job), "owner_kinds": owner_kinds}
    prev = projection(store, job.job_id)
    changed = (prev.get("status"), prev.get("text"), prev.get("blockers"), prev.get("owner_kinds")) != (
        view.status, view.text, list(view.blocker_ids), owner_kinds)
    store.kv_set(job.job_id, PROJECTION_KEY, proj)
    if changed and (view.status != "none" or prev.get("status") not in (None, "none")):
        store.append_event(job.job_id, what=STATUS_WHAT,
                           evidence={"status": view.status, "text": view.text, "blockers": list(view.blocker_ids),
                                     "next_retry_at": view.next_retry_at, "next_actor": view.next_actor,
                                     "owner_kinds": owner_kinds},
                           rule="ownership", judgment=Judgment.code)
    _annotate_approvals(store, job.job_id, view)
    return view.status, changed


def reconcile_job(store: Store, job_id: str, *, now: float, head: str | None = None, notifier=None,
                  notify_fn=None) -> ReconcileResult:
    job = store.get_job(job_id)
    if job is None or not enabled():
        return ReconcileResult(job_id, [], "none", False)
    cleared = _clear(store, job, now, head)
    _classify(store, job, now, notifier, notify_fn)
    cleared += _clear(store, job, now, head)
    cleared += _clear_cause_cleared(store, job, now, head)
    _note_answers(store, job, cleared, now)
    status, changed = _publish(store, job, now, notifier, notify_fn)
    store.kv_set(job_id, CLASSIFIED_KEY, _last_seq(store, job_id))
    return ReconcileResult(job_id, cleared, status, changed)


# -- confirming and notifying ----------------------------------------------------------------------------------


def _notice_body(b: Blocker) -> str:
    return str((b.human_request or {}).get("decision") or b.claim)[:120]


UNDELIVERED_PREFIX = "owner_notice_undelivered:"  # u4: kv per blocker whose notice never left (shown in health)
NOTICE_ATTEMPTS = 2  # u4: one send plus one retry


def _channel(notifier, notify_fn) -> str:
    return "notifier" if notifier is not None else "notify_fn" if notify_fn is not None else "none"


def _emit_failure_reason(store: Store, job_id: str, revision: str, after_seq: int) -> str:
    """Why `notify.emit` returned False: its own `notify_failed` event (a raise or a refusal), else a dedupe/hold."""
    for e in reversed(store.events_of(job_id, ("notify_failed",))):
        if e.seq <= after_seq:
            break
        if (e.evidence or {}).get("revision") == revision:
            ev = e.evidence or {}
            return str(ev.get("error") or ev.get("detail") or "notifier failed")[:200]
    return "not sent: deduplicated or held by the notice gate"


def _send_notice(store: Store, b: Blocker, kind: str, *, notifier, notify_fn) -> tuple[bool, str]:
    """One send attempt -> (delivered, reason). Only a transport's own success is a delivery; a raising transport
    raises out of here (the caller counts it as a failed attempt and retries)."""
    from steward import notify, push

    if notifier is not None:
        title, body = push.phone_event(store, b.job_id, kind, _notice_body(b))
        revision = f"blocker:{b.blocker_id}"
        ev = notify.PhoneEvent(kind=kind, job_id=b.job_id, title=title, body=body,
                               deep_link=notify.deep_link(store.kv_get(b.job_id, "herdr_workspace"),
                                                          store.kv_get(b.job_id, "herdr_tab"),
                                                          store.kv_get(b.job_id, "herdr_pane")),
                               revision=revision)
        before = store._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events WHERE job_id = ?",
                                     (b.job_id,)).fetchone()[0]
        if notify.emit(store, notifier, ev):  # emit never raises: a notifier's exception is its notify_failed event
            return True, ""
        return False, _emit_failure_reason(store, b.job_id, revision, int(before))
    with speaking():
        res = notify_fn(b.job_id, kind, "Steward: ready" if kind == "READY" else "Steward: needs you",
                        _notice_body(b))
    ok = res is not False  # a receipt-less callable (returns None) is taken at its word; False is a refusal
    return ok, "" if ok else "notify_fn reported failure"


def notify_owner(store: Store, b: Blocker, *, notifier=None, notify_fn=None, now: float | None = None) -> bool:
    """One owner notice per confirmed blocker (revision `blocker:<id>`), through the phone notifier or the caller's
    notify_fn. Questions are notified by the question queue, never here.

    u4 (2026-10-14): the receipt is the transport's answer, never the attempt. A failed send is retried once; the
    outcome is one `owner_notice_delivered` or `owner_notice_undelivered` event, and an undelivered notice stays
    listed (`undelivered_notices`, red in health) until it is delivered or its blocker closes."""
    if b.kind == "owner_question":
        return False
    kind = "READY" if b.kind == "acceptance_decision" else "NEEDS_YOU"
    key = NOTIFIED_PREFIX + b.blocker_id
    if store.kv_get(b.job_id, key):
        return False
    intent_key = intents.intent_key(b.job_id, "owner_notice", b.blocker_id)
    now = time.time() if now is None else now
    claim = intents.claim(store, key=intent_key, job_id=b.job_id, kind="owner_notice", lease_s=120.0,
                          now=now, blocker_id=b.blocker_id, reclaimable=False)
    if claim is None:
        return False
    channel = _channel(notifier, notify_fn)
    sent, reason, attempts = False, "no notifier configured", 1
    if channel != "none":  # only a missing transport stops at once; a refusal or a raise gets exactly one retry
        for attempts in range(1, NOTICE_ATTEMPTS + 1):
            try:
                sent, reason = _send_notice(store, b, kind, notifier=notifier, notify_fn=notify_fn)
            except Exception as exc:  # noqa: BLE001 - a raising transport is a failed attempt
                sent, reason = False, f"{type(exc).__name__}: {str(exc)[:160]}"
            if sent:
                break
    if sent:
        store.kv_set(b.job_id, key, True)
        store.kv_delete(b.job_id, UNDELIVERED_PREFIX + b.blocker_id)
        intents.complete(store, intent_key, receipt={"notified": True, "channel": channel, "attempts": attempts},
                         now=now)
        store.append_event(b.job_id, what="owner_notice_delivered",
                           evidence={"blocker_id": b.blocker_id, "kind": kind, "channel": channel,
                                     "attempts": attempts}, rule="owner_notice_receipt", judgment=Judgment.code)
    else:
        intents.fail(store, intent_key, reason=reason, now=now, retryable=True)
        store.kv_set(b.job_id, UNDELIVERED_PREFIX + b.blocker_id,
                     {"blocker_id": b.blocker_id, "kind": kind, "reason": reason, "attempts": attempts, "at": now})
        store.append_event(b.job_id, what="owner_notice_undelivered",
                           evidence={"blocker_id": b.blocker_id, "kind": kind, "reason": reason,
                                     "attempts": attempts}, rule="owner_notice_receipt", judgment=Judgment.code)
    return sent


def undelivered_notices(store: Store) -> list[dict]:
    """Owner notices that never left, for blockers still open: [{job_id, blocker_id, kind, reason, attempts, at}]."""
    rows = store._conn.execute("SELECT job_id, key, value_json FROM kv WHERE key LIKE ?",
                               (UNDELIVERED_PREFIX + "%",)).fetchall()
    out = []
    for job_id, _key, value in rows:
        try:
            rec = json.loads(value)
        except (TypeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        b = blockers.get(store, str(rec.get("blocker_id")))
        if b is None or not b.is_open:
            continue
        out.append({"job_id": job_id, **rec})
    return out


_SPEAKING = contextvars.ContextVar("ownership_speaking", default=False)


class speaking:
    """Marks a notice as ownership's own (the notification gate lets it through)."""

    def __enter__(self):
        self._t = _SPEAKING.set(True)

    def __exit__(self, *exc):
        _SPEAKING.reset(self._t)


def confirm_human(store: Store, blocker_id: str, request: HumanRequest, *, actor: ActorRef, now: float,
                  notifier=None, notify_fn=None) -> Blocker:
    """Confirm a genuine owner requirement: the exact decision + why authority is insufficient, one linked approval
    (when the kind has one), one notice."""
    from steward import approvals

    b = blockers.transition(store, blocker_id, "confirmed_human", actor=actor, reason="confirmed human requirement",
                            now=now, human_request=request)
    decision_trace.safe_record(store, job_id=b.job_id, actor=actor, stage="effect",
                               decision=f"owner_request:{b.kind}", now=now, blocker_id=b.blocker_id,
                               evidence_refs=list(request.evidence), authority_rev=b.authority_rev)
    ks = blockers.spec(b.kind)
    if ks.approval_kind and not (b.clearing.get("params") or {}).get("approval_id"):
        rec = approvals.open_approval(store, b.job_id, f"{request.decision}"[:300], kind=ks.approval_kind)
        approvals.annotate(store, b.job_id, rec["id"], blocker_id=b.blocker_id)
        with store._conn:
            store._conn.execute("UPDATE blockers SET clearing_json = ? WHERE blocker_id = ?",
                                (json.dumps({"predicate": "approval_decided",
                                                           "params": {"approval_id": rec["id"]}}), b.blocker_id))
        b = blockers.get(store, b.blocker_id)
    if ks.notify_on_confirm:
        notify_owner(store, b, notifier=notifier, notify_fn=notify_fn, now=now)
    return b


def owner_notice_allowed(store: Store, job_id: str, kind: str, *, notifier=None) -> tuple[bool, str]:
    """Gate legacy notices only when ownership has current evidence; fail open on classification errors."""
    if kind not in ("NEEDS_YOU", "READY") or _SPEAKING.get():
        return True, "not_gated"
    if kind == "NEEDS_YOU":  # the coordinator works the park first; coordinator_link.sweep delivers the one line
        try:
            from steward import coordinator_link

            if coordinator_link.holding(store, job_id):
                return False, "awaiting_coordinator"
        except Exception:  # noqa: BLE001 - fail open
            pass
    if not enabled():
        return True, "not_gated"
    job = store.get_job(job_id)
    if job is None or job.state != JobState.NEEDS_HUMAN:
        return True, "not_parked"
    try:
        ensure_classified(store, job_id, notifier=notifier)
        view = owner_view(store, job_id)
    except Exception as exc:  # noqa: BLE001 - fail open
        return True, f"ownership_error:{type(exc).__name__}"
    if view.status in REVIEW_STATUSES:
        return False, view.status
    if view.status == "needs_owner":
        return False, "ownership_notifies"
    return True, "no_blockers"


# -- review scheduling and the sweep (Task 8) -------------------------------------------------------------------

FACT_SOURCE = "ownership"
NO_LEAD_RETRY_S = 1800.0
_FACT_KEY_PREFIX = "ownership_fact:"
PACKET_RULES = ("Blockers are claims under review, not facts. reopen sends the parked worker back with exact "
                "instructions; resolve_blocker overrules ONE mistaken blocker (blocker_id, reason, evidence, "
                "confidence) - never an owner_only one; wait {blocker_id, until: evidence_change|time, minutes} "
                "when the next step needs named evidence or time; escalate ONLY with {decision: the exact owner "
                "decision, why_authority_insufficient, evidence, continuing}. A worker's NEEDS_HUMAN or STOP is "
                "not proof the owner is needed.")


@dataclass(frozen=True)
class ReviewRequest:
    job_id: str
    blocker_ids: tuple[str, ...]
    priority: float
    reason: str


def _policy():
    try:
        from steward import tech_lead_policy

        return tech_lead_policy.load_or_defaults()[0]
    except Exception:  # noqa: BLE001 - defaults
        return None


def _pv(pol, name: str, default):
    return getattr(pol, name, default) if pol is not None else default


def review_eligible(b: Blocker, now: float, *, coalesce_s: float = COALESCE_S) -> str | None:
    """Why `b` may be reviewed now ("new" | "evidence_changed" | "retry" | "wake"), else None."""
    if b.disposition != "candidate" or b.review_state == "in_flight":
        return None
    if b.next_review_epoch is not None and now < float(b.next_review_epoch):
        return None
    if now - b.updated_epoch < coalesce_s and not any(e.startswith("strategy_review:") for e in b.evidence):
        return None
    if b.review_state == "unavailable":
        return "retry"
    d = blockers.reusable_diagnosis(b)
    if d is not None:  # unchanged evidence and authority: reuse, never pay again (spec §5, §7)
        return "wake" if d.get("wake_at") is not None and now >= float(d["wake_at"]) else None
    return "evidence_changed" if b.diagnosis else "new"


def priority(b: Blocker, now: float, *, aging_min: float = AGING_MIN) -> float:
    age_min = max(0.0, now - b.created_epoch) / 60.0
    p = blockers.spec(b.kind).impact * (1.0 + age_min / 60.0) + max(0.0, b.priority)
    return p + AGING_BOOST if age_min >= aging_min else p


def due_reviews(store: Store, *, now: float, coalesce_s: float = COALESCE_S,
                aging_min: float = AGING_MIN) -> list[ReviewRequest]:
    out = []
    for jid in blockers.jobs_with_open_blockers(store):
        open_ = blockers.open_blockers(store, jid)
        if any(b.disposition == "reviewing" or b.review_state == "in_flight" for b in open_):
            continue  # one review per job
        if any(b.disposition == "confirmed_human" and "all" in b.affected for b in open_):
            continue  # a blocking owner question must be answered before technical review resumes
        hits = [(b, why) for b in open_ if b.kind not in _KIND_REVIEWERS
                for why in [review_eligible(b, now, coalesce_s=coalesce_s)] if why]
        if not hits:
            continue
        top_b, top_why = max(hits, key=lambda h: priority(h[0], now, aging_min=aging_min))
        out.append(ReviewRequest(jid, tuple(b.blocker_id for b, _ in hits),
                                 priority(top_b, now, aging_min=aging_min), top_why))
    return sorted(out, key=lambda r: (-r.priority, r.job_id))


def packet_section(store: Store, job_id: str) -> dict:
    """The blockers a review must address (bounded; masked at write time)."""
    rows = []
    for b in blockers.open_blockers(store, job_id)[:12]:
        d = b.diagnosis or {}
        rows.append({"blocker_id": b.blocker_id, "kind": b.kind, "claim": b.claim[:300],
                     "disposition": b.disposition, "owner_only": blockers.spec(b.kind).owner_only,
                     "evidence": list(b.evidence)[:8], "origin": b.origin_actor,
                     "previous_diagnosis": {k: d[k] for k in ("lead", "summary", "actions") if k in d} or None,
                     **({"owner_decision": (b.human_request or {}).get("decision")} if b.human_request else {})})
    answers = _answers(store, job_id)
    return {"blockers": rows, "ownership_rules": PACKET_RULES, **({"answers": answers} if answers else {})} if rows else {}


def submit_review_fact(store: Store, b: Blocker, *, reason: str, now: float | None = None) -> None:
    """Route a RUNNING job's blocker to that job's runner lead: one Arbiter fact per evidence generation."""
    now = time.time() if now is None else now
    key = _FACT_KEY_PREFIX + b.blocker_id
    if store.kv_get(b.job_id, key) == b.evidence_digest:
        return
    from steward.fleet_runtime import Arbiter

    Arbiter(store).submit(b.job_id, FACT_SOURCE, f"blocker:{b.kind}", blocker_id=b.blocker_id, reason=reason)
    store.kv_set(b.job_id, key, b.evidence_digest)
    blockers.set_review_state(store, b.blocker_id, "queued", now=now)


def mark_reviewing(store: Store, blocker_ids, *, now: float) -> None:
    for bid in blocker_ids:
        b = blockers.get(store, bid)
        if b is None or not b.is_open:
            continue
        if b.disposition == "candidate":
            blockers.transition(store, bid, "reviewing", actor=CONTROLLER, reason="technical review started", now=now)
        blockers.set_review_state(store, bid, "in_flight", now=now)


def mark_review_unavailable(store: Store, blocker_ids, *, next_retry_at: float, now: float) -> None:
    job_ids = set()
    for bid in blocker_ids:
        b = blockers.get(store, bid)
        if b is None or not b.is_open:
            continue
        job_ids.add(b.job_id)
        if b.disposition == "reviewing":
            blockers.transition(store, bid, "candidate", actor=CONTROLLER, reason="technical review unavailable",
                                now=now)
        blockers.set_review_state(store, bid, "unavailable", now=now, next_review_epoch=next_retry_at)
    for jid in job_ids:
        _close_review_intent(store, jid, now=now, reason="technical review unavailable")


def _close_review_intent(store: Store, job_id: str, *, now: float, receipt: dict | None = None,
                         reason: str | None = None) -> None:
    key = store.kv_get(job_id, REVIEW_INTENT_KEY)
    it = intents.get(store, key) if key else None
    if it is None or it.state in intents.FINAL:
        return
    if receipt is not None:
        intents.complete(store, key, receipt=receipt, now=now, owner=it.owner)
    else:
        intents.fail(store, key, reason=reason or "review did not complete", now=now, owner=it.owner, retryable=True)


def record_review_outcome(store: Store, job_id: str, inflight: dict, proposal, results: list, *, now: float,
                          request_strategy: bool = True) -> None:
    """Persist what the review concluded on each blocker it covered (reused while evidence is unchanged)."""
    acts = [[a.type, (r or {}).get("outcome") if isinstance(r, dict) else None]
            for a, r in zip(proposal.actions, results)]
    for bid in inflight.get("blocker_ids") or []:
        b = blockers.get(store, bid)
        if b is None or not b.is_open:
            continue
        blockers.record_diagnosis(store, bid, {"lead": inflight.get("lead"), "model": inflight.get("model"),
                                               "proposal_id": proposal.proposal_id,
                                               "summary": proposal.rationale[:400], "actions": acts}, now=now)
        if b.disposition == "reviewing":
            blockers.transition(store, bid, "candidate", actor=CONTROLLER, reason="technical review answered", now=now)
        blockers.set_review_state(store, bid, "done", now=now)
    _close_review_intent(store, job_id, now=now,
                         receipt={"proposal": proposal.proposal_id, "actions": acts})
    executed = any(o in ("executed", "started") for _, o in acts)
    asked = any(isinstance(r, dict) and (r.get("strategy_review_due") or r.get("strategy_review_requested"))
                for r in results)
    if request_strategy and not executed and not asked:
        first = next((bid for bid in inflight.get("blocker_ids") or []
                      for b in [blockers.get(store, bid)] if b is not None and b.is_open
                      and b.disposition == "candidate"), None)
        if first is not None:
            request_strategy_review(store, job_id, blocker_id=first, reason="the review found no executable next step",
                                    prefer_other_than=inflight.get("lead"), now=now)


def _control_owned(store: Store, job) -> bool:
    """Jobs the control pane's lead reviews (no runner owns them)."""
    if job is None:
        return False
    if job.state == JobState.NEEDS_HUMAN:
        return True
    return job.state == JobState.RUNNING and bool((store.kv_get(job.job_id, LAUNCH_KEY) or {}).get("blocked"))


def _reconcile_due(store: Store, job_id: str, now: float) -> bool:
    if store.kv_get(job_id, CLASSIFIED_KEY) != _last_seq(store, job_id):
        return True
    last = store.kv_get(job_id, RECONCILED_AT_KEY)
    return last is None or now - float(last) >= RECONCILE_EVERY_S


def _review_key(store: Store, job_id: str, blocker_ids) -> str:
    parts = []
    for bid in sorted(blocker_ids):
        b = blockers.get(store, bid)
        if b is not None:
            parts.append(f"{bid}@{b.evidence_digest}#{(b.diagnosis or {}).get('at', 0)}")
    return intents.intent_key(job_id, REVIEW_KIND, *parts)


def _may_poll(store: Store, job_id: str, owner: str, now: float) -> bool:
    key = store.kv_get(job_id, REVIEW_INTENT_KEY)
    it = intents.get(store, key) if key else None
    return it is None or it.state in intents.FINAL or it.owner == owner or it.lease_expires <= now


def _lead_event(job_lead, job_id: str, *, now: float, **kwargs):
    """Keep lead-side leases and wake times on the sweep's clock, including deterministic tests."""
    old_clock = getattr(job_lead, "clock", None)
    executor = getattr(job_lead, "executor", None)
    old_executor_clock = getattr(executor, "clock", None)
    if callable(old_clock):
        job_lead.clock = lambda: now
    if callable(old_executor_clock):
        executor.clock = lambda: now
    try:
        return job_lead.on_event(job_id, **kwargs)
    finally:
        if callable(old_clock):
            job_lead.clock = old_clock
        if callable(old_executor_clock):
            executor.clock = old_executor_clock


def sweep(store: Store, *, now: float, job_lead=None, notifier=None, head_fn=None, outage=None, policy=None,
          launch_fn=None, owner: str = intents.PROCESS_OWNER, alive_fn=None) -> dict:
    """One control-pane pass: reconcile, poll in-flight reviews, start due reviews. Cheap and nonblocking (no
    model call runs here); one broken job never stops the others."""
    out: dict = {"reconciled": [], "polled": [], "reviews": [], "unavailable": [], "relaunched": [], "errors": []}
    if not enabled():
        return out
    decision_trace.maybe_purge(store, now=now)
    out["recovered"] = _recover_intents(store, now=now, owner=owner, throttle=True, errors=out["errors"])
    pol = policy if policy is not None else _policy()
    coalesce = float(_pv(pol, "ownership_coalesce_s", COALESCE_S))
    aging = float(_pv(pol, "ownership_aging_min", AGING_MIN))
    max_reviews = int(_pv(pol, "ownership_max_reviews", MAX_CONCURRENT_REVIEWS))
    ids = set(blockers.jobs_with_open_blockers(store)) | {j.job_id for j in store.list_jobs({JobState.NEEDS_HUMAN})}
    for jid in sorted(ids):
        try:
            if _reconcile_due(store, jid, now):
                head = head_fn(jid) if head_fn and _needs_head(store, jid) else None
                res = reconcile_job(store, jid, now=now, head=head, notifier=notifier)
                store.kv_set(jid, RECONCILED_AT_KEY, now)
                out["reconciled"].append({"job": jid, "status": res.status, "cleared": res.cleared})
        except Exception as exc:  # noqa: BLE001 - one broken job never blocks the others
            out["errors"].append({"job": jid, "error": f"{type(exc).__name__}: {exc}"})
    ids |= {j.job_id for j in store.list_jobs({JobState.RUNNING})
            if (store.kv_get(j.job_id, LAUNCH_KEY) or {}).get("blocked")}
    busy: set[str] = set()
    if job_lead is not None:
        for jid in sorted(ids):
            try:
                if (job_lead.state(jid) or {}).get("in_flight") and _may_poll(store, jid, owner, now):
                    busy.add(jid)
                    _lead_event(job_lead, jid, now=now, sweep=True, reason="ownership:poll")
                    out["polled"].append(jid)
            except Exception as exc:  # noqa: BLE001
                out["errors"].append({"job": jid, "error": f"{type(exc).__name__}: {exc}"})
    budget = max(0, max_reviews - len(busy))
    for req in due_reviews(store, now=now, coalesce_s=coalesce, aging_min=aging):
        if req.job_id in busy:
            continue
        try:
            job = store.get_job(req.job_id)
            if job is not None and job.state == JobState.RUNNING and not _control_owned(store, job):
                for bid in req.blocker_ids:
                    b = blockers.get(store, bid)
                    if b is not None:
                        submit_review_fact(store, b, reason=req.reason, now=now)
                out["reviews"].append({"job": req.job_id, "reason": req.reason, "via": "arbiter",
                                       "blockers": list(req.blocker_ids)})
                continue
            if not _control_owned(store, job):
                continue
            if job_lead is None:
                mark_review_unavailable(store, req.blocker_ids, next_retry_at=now + NO_LEAD_RETRY_S, now=now)
                out["unavailable"].append({"job": req.job_id, "reason": "no tech lead configured"})
                continue
            if outage is not None and not outage.may_attempt(req.job_id, now):
                mark_review_unavailable(store, req.blocker_ids, next_retry_at=outage.next_retry_at() or now + 60.0,
                                        now=now)
                out["unavailable"].append({"job": req.job_id, "reason": "reviewer outage"})
                continue
            if budget <= 0:
                break
            key = _review_key(store, req.job_id, req.blocker_ids)
            if intents.claim(store, key=key, job_id=req.job_id, kind=REVIEW_KIND, lease_s=REVIEW_LEASE_S, now=now,
                             owner=owner, blocker_id=req.blocker_ids[0], reclaimable=True,
                             authority_rev=authority_rev(store, req.job_id),
                             params={"blockers": list(req.blocker_ids), "reason": req.reason,
                                     "after_seq": _last_seq(store, req.job_id)}) is None:
                continue
            store.kv_set(req.job_id, REVIEW_INTENT_KEY, key)
            res = _lead_event(job_lead, req.job_id, now=now, sweep=True, reason=f"ownership:{req.reason}",
                              blocker_ids=list(req.blocker_ids))
            if (res or {}).get("status") != "review_started":
                intents.fail(store, key, reason=f"lead did not start a review: {(res or {}).get('status')}",
                             now=now, owner=owner, retryable=True)
                continue
            budget -= 1
            out["reviews"].append({"job": req.job_id, "reason": req.reason, "via": "control_lead",
                                   "blockers": list(req.blocker_ids), "status": (res or {}).get("status")})
        except Exception as exc:  # noqa: BLE001
            out["errors"].append({"job": req.job_id, "error": f"{type(exc).__name__}: {exc}"})
    _relaunch_pass(store, now=now, launch_fn=launch_fn, out=out, alive_fn=alive_fn, owner=owner)
    return out


# -- lead actions on blockers (Task 9) ---------------------------------------------------------------------------

STRATEGY_EVENT = "ownership_strategy_review"
MAX_STRATEGY_REVIEWS = 2  # automatic strategy reviews per blocker episode; then wait for new evidence (Plan 3 owns more)
AVOID_KEY = "ownership_avoid_lead"
_UNREVIEWABLE = {JobState.STOPPING: "owner_stop", JobState.STOPPED: "owner_stop", JobState.PAUSED: "job_paused",
                 JobState.ACCEPTED: "job_accepted", JobState.RELEASED: "job_released", JobState.FAILED: "job_failed"}
_STRATEGY_REVIEWERS: list[Callable[[Store, Blocker, dict], bool]] = []


def _conf(params: dict, default: float = 0.0) -> float:
    c = params.get("confidence", default)
    return float(c) if isinstance(c, (int, float)) and not isinstance(c, bool) else 0.0


def owner_decision_pending(store: Store, job_id: str) -> str | None:
    """The first open owner decision on this job (exact text), else None."""
    for b in blockers.open_blockers(store, job_id):
        if b.disposition == "confirmed_human":
            return str((b.human_request or {}).get("decision") or b.claim)
    return None


def reopens_since_strategy_review(store: Store, job_id: str) -> int:
    n = 0
    for e in reversed(store.events_of(job_id, (STRATEGY_EVENT, "tech_lead_reopen"))):
        if e.what == STRATEGY_EVENT:
            break
        if e.what == "tech_lead_reopen":
            n += 1
    return n


def blocker_action_allowed(store: Store, job_id: str, action_type: str, params: dict, *, policy,
                           proposal_confidence: float, lead_id: str = "") -> tuple[bool, str, dict]:
    """Per-blocker eligibility of reopen / resolve_blocker / wait (replaces the stuck-only, fix_rounds-to-owner rule)."""
    job = store.get_job(job_id)
    if job is None:
        return False, "unknown job", {}
    if job.state in _UNREVIEWABLE:
        return False, f"the job is {job.state.value}: the owner's stop/decision binds", {}
    ensure_classified(store, job_id)
    open_ = blockers.open_blockers(store, job_id)
    if action_type == "wait":
        from steward.tech_lead_actions import _wait_blocker_id
        if _wait_blocker_id(params) is None:  # a job with no blocker may still be told to wait
            return True, "", {}
    if action_type in ("resolve_blocker", "wait"):
        b = next((x for x in open_ if x.blocker_id == params.get("blocker_id")), None)
        if b is None:
            return False, "no such open blocker on this job", {}
        if action_type == "wait":
            return True, "", {"blocker_id": b.blocker_id}
        if blockers.spec(b.kind).owner_only:
            return False, f"{b.kind} is the owner's decision", {}
        if b.disposition == "confirmed_human" and b.origin_actor != f"tech_lead:{lead_id}":
            return False, "a confirmed owner request is closed by the owner or withdrawn by the lead that raised it", {}
        conf = _conf(params, proposal_confidence)
        if conf < policy.judgment_confidence:
            return False, (f"resolve at confidence {conf:.2f} is below the policy's "
                           f"{policy.judgment_confidence:.2f}"), {}
        return True, "", {"blocker_id": b.blocker_id}
    if action_type != "reopen":
        return False, f"unknown blocker action {action_type}", {}
    if job.state != JobState.NEEDS_HUMAN:
        return False, "reopen: the job is not parked (NEEDS_HUMAN)", {}
    if not policy.parked_reopen:
        return False, "reopen off in the policy ([parked] reopen = false)", {}
    conf = _conf(params)
    if conf < policy.judgment_confidence:
        return False, f"reopen at confidence {conf:.2f} is below the policy's {policy.judgment_confidence:.2f}", {}
    from steward import sendback

    if sendback.rework_pending(store, job_id):
        return False, "reopen: rework pending (a new attempt is already launching)", {}
    binding = [b for b in open_ if b.disposition == "confirmed_human" and {"progress", "all"} & set(b.affected)]
    if binding:
        text = str((binding[0].human_request or {}).get("decision") or binding[0].claim)
        return False, f"waiting for the owner: {text}"[:300], {"owner_pending": binding[0].blocker_id}
    technical = [b for b in open_ if not blockers.spec(b.kind).owner_only and b.disposition in ("candidate", "reviewing")]
    if not technical:
        return False, "reopen: no open technical blocker (what remains is the owner's decision)", {}
    done = reopens_since_strategy_review(store, job_id)
    if done >= policy.fix_rounds:
        return False, (f"reopen: {done} reopens since the last strategy review (fix_rounds {policy.fix_rounds}); "
                       "a strategy review is due"), {"strategy_review": True}
    return True, "", {"blocker_ids": [b.blocker_id for b in technical]}


def park_seq(store: Store, job_id: str) -> int:
    """Identity of the current park (the park event's seq; else the newest open blocker basis)."""
    park = park_event(_park_events(store, job_id))
    if park is not None:
        return int(park.seq)
    return max((b.basis_seq for b in blockers.open_blockers(store, job_id)), default=0)


def review_basis(store: Store, job_id: str, blocker_ids) -> dict:
    job = store.get_job(job_id)
    return {"state": job.state.value if job else None, "authority_rev": authority_rev(store, job_id),
            "digests": {bid: b.evidence_digest for bid in blocker_ids or []
                        for b in [blockers.get(store, bid)] if b is not None}}


def stale_answer(store: Store, job_id: str, inflight: dict) -> str | None:
    """Why a finished review's answer must not act (newer evidence, authority or an owner stop), else None."""
    job = store.get_job(job_id)
    if job is None:
        return "job_missing"
    if job.state in _UNREVIEWABLE:
        return _UNREVIEWABLE[job.state]
    basis = inflight.get("basis") or {}
    if not inflight.get("blocker_ids") or not basis:
        return None  # a runner review of a live job: only an owner stop/pause/decision binds (legacy behaviour)
    if job.state.value != basis.get("state"):
        return "job_state_changed"
    # Plan 2 revalidates authority-dependent actions at the execution seam. A
    # changed revision alone does not discard the whole review; its proposal
    # must reach that gate so it can be safely refused and recorded.
    for bid, digest in (basis.get("digests") or {}).items():
        b = blockers.get(store, bid)
        if b is None or not b.is_open:
            return "blocker_resolved"
        if b.evidence_digest != digest:
            return "evidence_changed"
    return None


def release_review(store: Store, inflight: dict, *, reason: str, now: float) -> None:
    """A review that will not act (discarded, lost): its blockers return to the queue without a diagnosis."""
    for bid in inflight.get("blocker_ids") or []:
        b = blockers.get(store, bid)
        if b is None or not b.is_open:
            continue
        if b.disposition == "reviewing":
            blockers.transition(store, bid, "candidate", actor=CONTROLLER, reason=f"review released: {reason}",
                                now=now)
        blockers.set_review_state(store, bid, "queued", now=now)


def register_strategy_reviewer(fn: Callable[[Store, Blocker, dict], bool]) -> None:
    """Plan 3: an independent-diagnosis service; returning True means it took the request over."""
    _STRATEGY_REVIEWERS.append(fn)


def avoid_lead(store: Store, job_id: str, blocker_ids) -> str | None:
    pref = store.kv_get(job_id, AVOID_KEY) or {}
    return next((pref[b] for b in blocker_ids or [] if pref.get(b)), None)


def request_strategy_review(store: Store, job_id: str, *, blocker_id: str | None, reason: str, signature: str = "",
                            prefer_other_than: str | None = None, now: float) -> Blocker:
    """Repeated failure or a round threshold asks for a different approach (spec §7: never proof the owner is
    needed). Plan 1: a bounded re-review preferring another lead; Plan 3's registered reviewer takes over."""
    if blocker_id is None:
        b, _ = blockers.raise_blocker(store, job_id, kind="repeated_failure", subject=signature or reason,
                                      claim=reason, evidence=[f"signature:{signature or 'none'}"],
                                      origin_actor=CONTROLLER, now=now, origin_rule="strategy_review")
    else:
        b = blockers.get(store, blocker_id)
        if b is None:
            raise blockers.BlockerError(f"unknown blocker {blocker_id}")
    n = sum(1 for e in b.evidence if e.startswith("strategy_review:"))
    request = {"reason": reason[:300], "signature": signature[:120], "prefer_other_than": prefer_other_than,
               "n": n + 1, "exhausted": n >= MAX_STRATEGY_REVIEWS}
    store.append_event(job_id, what=STRATEGY_EVENT, evidence={"blocker_id": b.blocker_id, **request},
                       rule="ownership", judgment=Judgment.code)
    for fn in list(_STRATEGY_REVIEWERS):
        try:
            if fn(store, b, request):
                _notify_strategy_hooks(store, b, reason=reason, signature=signature,
                                       prefer_other_than=prefer_other_than, now=now)
                return blockers.get(store, b.blocker_id)
        except Exception:  # noqa: BLE001 - a broken reviewer falls back to the built-in re-review
            continue
    if b.is_open and n < MAX_STRATEGY_REVIEWS:
        if prefer_other_than:
            pref = store.kv_get(job_id, AVOID_KEY) or {}
            store.kv_set(job_id, AVOID_KEY, {**pref, b.blocker_id: prefer_other_than})
        b = blockers.add_evidence(store, b.blocker_id, [f"strategy_review:{n + 1}"], actor=CONTROLLER, now=now,
                                  reason="strategy review requested")
    _notify_strategy_hooks(store, b, reason=reason, signature=signature, prefer_other_than=prefer_other_than, now=now)
    return b


def strategy_reviews_exhausted(store: Store, job_id: str, blocker_id: str) -> bool:
    """The newest strategy request for this blocker found no automatic review left (u3: the caller escalates)."""
    for e in reversed(store.events_of(job_id, (STRATEGY_EVENT,))):
        if (e.evidence or {}).get("blocker_id") == blocker_id:
            return bool((e.evidence or {}).get("exhausted"))
    return False


def _notify_strategy_hooks(store: Store, b: Blocker, *, reason: str, signature: str,
                           prefer_other_than: str | None, now: float) -> None:
    for hook in list(_STRATEGY_HOOKS):
        try:
            hook(store, b, reason=reason, issue_key=signature or b.issue_key,
                 prefer_other_than=prefer_other_than, now=now)
        except Exception:  # noqa: BLE001 - a hook failure never blocks the request
            pass


def raise_lead_escalation(store: Store, job_id: str, request: HumanRequest, *, kind: str, lead_id: str, now: float,
                          blocker_id: str | None = None, subject: str | None = None, notifier=None,
                          notify_fn=None) -> Blocker:
    """The lead confirms a genuine owner requirement: one blocker per issue episode, one approval, one notice."""
    actor = ActorRef(Actor.TECH_LEAD, lead_id)
    evidence = list(request.evidence)[:6] + ([f"blocker:{blocker_id}"] if blocker_id else [])
    b, _ = blockers.raise_blocker(store, job_id, kind=kind, subject=subject or request.decision,
                                  claim=request.decision, evidence=evidence or [f"lead:{lead_id}"],
                                  origin_actor=actor, now=now, origin_rule=f"lead_{kind}")
    if b.disposition == "confirmed_human":
        return b  # deduplicated: the same issue episode is already the owner's
    return confirm_human(store, b.blocker_id, request, actor=actor, now=now, notifier=notifier, notify_fn=notify_fn)


# -- signal sources, answers, reliance, relaunch (Task 11) ------------------------------------------------------

LAUNCH_KEY = "ownership_launch"
LAUNCH_KIND = "launch_worker"
_FLOW = ActorRef(Actor.MODEL_SUPERVISOR, "flow")


def raise_flow_finding(store: Store, episode: dict, result: dict, *, outcome: str, reason: str | None,
                       now: float) -> Blocker | None:
    """Flow could not act (job not running) or asked for human review: a candidate for technical review (G1, G2).
    Flow's own allowlist, cooldown and reserved decisions are unchanged."""
    if not enabled():
        return None
    job = store.get_job(episode["job_id"])
    if job is None or job.state in _DONE_STATES:
        return None
    ev = [f"flow_episode:{episode['episode_id']}"] + [str(r) for r in (result.get("evidence_refs") or [])][:8]
    if result.get("instruction"):
        ev.append(f"flow_instruction:{str(result['instruction'])[:100]}")
    b, _ = blockers.raise_blocker(store, job.job_id, kind="flow_finding", subject=f"flow:{episode.get('label')}",
                                  claim=f"flow ({episode.get('label')}): {result.get('root_cause') or outcome}",
                                  evidence=ev, origin_actor=_FLOW, now=now,
                                  origin_rule=f"flow_{outcome}:{reason or ''}"[:80], priority=1.0)
    decision_trace.safe_record(store, job_id=job.job_id, actor=_FLOW, stage="signal",
                               decision=f"flow:{episode.get('label')}", now=now, blocker_id=b.blocker_id,
                               evidence_refs=ev, signal={"label": episode.get("label"), "outcome": outcome,
                                                        "reason": reason})
    return b


def raise_signal_concern(store: Store, job_id: str, *, source: str, choice: str, text: str, confidence: float,
                         now: float, priority_hint: float = 1.0, refs=()) -> tuple[Blocker, bool]:
    """A Jev (or other model signal) concern: a candidate only (spec §13). A repeat of an unchanged finding touches
    the open episode (no new review, no notice); it is withdrawn when not seen for JEV_CONCERN_TTL_S."""
    subject = f"{source}:{choice}"
    key = blockers.issue_key("jev_concern", subject)
    current = next((b for b in blockers.open_blockers(store, job_id) if b.issue_key == key), None)
    evidence = [f"signal:{source}:{choice}"] + [str(r)[:120] for r in refs][:6]
    if current is not None and set(evidence) <= set(current.evidence):
        blockers.touch(store, current.blocker_id, now=now)
        return blockers.get(store, current.blocker_id), False
    b, created = blockers.raise_blocker(store, job_id, kind="jev_concern", subject=subject,
                                        claim=f"{text} (confidence {float(confidence):.2f})", evidence=evidence,
                                        origin_actor=ActorRef(Actor.JEV, source), now=now,
                                        origin_rule=f"{source}_{choice}"[:80], priority=max(0.0, priority_hint),
                                        recur_after_s=JEV_CONCERN_TTL_S, wake={"kind": "evidence_change",
                                                                              "last_seen": now})
    if created:
        decision_trace.safe_record(store, job_id=job_id, actor=ActorRef(Actor.JEV, source), stage="signal",
                                   decision=f"concern:{choice}", now=now, blocker_id=b.blocker_id,
                                   evidence_refs=evidence, signal={"choice": choice, "confidence": float(confidence)})
    return b, created


def reliance_hold(store: Store, job_id: str, claim_ref: str) -> list[Blocker]:
    """Open blockers that must be assessed before the controller relies on `claim_ref` (Plan 4: a completion
    claim). A concern never halts the job; it only holds the dependent reliance (spec §13 response policy)."""
    return [b for b in blockers.open_blockers(store, job_id) if any(claim_ref in e for e in b.evidence)]


def _note_answers(store: Store, job, cleared: list[str], now: float) -> None:
    """An answered owner question re-opens review of the remaining technical blockers, with the answer as evidence."""
    qids = [str((b.clearing.get("params") or {}).get("qid")) for bid in cleared
            for b in [blockers.get(store, bid)] if b is not None and b.kind == "owner_question"]
    if not qids or job.state != JobState.NEEDS_HUMAN:
        return
    for b in blockers.open_blockers(store, job.job_id):
        if not blockers.spec(b.kind).owner_only and b.disposition in ("candidate", "reviewing"):
            blockers.add_evidence(store, b.blocker_id, [f"answer:{q}" for q in qids], actor=CONTROLLER, now=now,
                                  reason="owner answered a question")


def _answers(store: Store, job_id: str) -> list[dict]:
    from steward import questions, questions_delivery

    return [{"qid": r.get("qid"), "question": str(r.get("question") or r.get("title") or "")[:200],
             "answer": questions_delivery._answer_text(r)[:300]}
            for r in questions.load(store, job_id) if r.get("status") == "answered"][-6:]


def _relaunch_pass(store: Store, *, now: float, launch_fn, out: dict, alive_fn=None,
                   owner: str = intents.PROCESS_OWNER) -> None:
    """A send-back whose launch never started a worker (spec §4: never a second writer)."""
    from steward import sendback

    for job in store.list_jobs({JobState.RUNNING}):
        jid = job.job_id
        try:
            if not sendback.rework_pending(store, jid):
                continue
            sent = store.last_event(jid, ("sent back for rework; new attempt pending",))
            if sent is None:
                continue
            if any(e.what == "worker attempt started" for e in store.events_since(jid, sent.seq)):
                store.kv_delete(jid, LAUNCH_KEY)
                continue
            st = store.kv_get(jid, LAUNCH_KEY) or {}
            if st.get("sent_seq") != sent.seq:
                st = {"sent_seq": sent.seq, "first_seen": now, "tries": 0, "next_at": now + REWORK_LAUNCH_GRACE_S,
                      "accepted": False, "blocked": False}
            if st["blocked"] or now < float(st["next_at"]):
                store.kv_set(jid, LAUNCH_KEY, st)
                continue
            if alive_fn is not None and alive_fn(jid):
                continue
            if st["accepted"] or st["tries"] >= len(LAUNCH_RETRY_S) or launch_fn is None:
                why = ("the launched attempt never started a worker" if st["accepted"] else
                       f"{st['tries']} launch(es) refused: {st.get('last_error') or 'no launcher wired'}")
                blockers.raise_blocker(store, jid, kind="worker_start_blocked", subject="rework_launch",
                                       claim=f"the send-back's new attempt did not start: {why}",
                                       evidence=[f"event:{sent.seq}", f"launch_tries:{st['tries']}"],
                                       origin_actor=CONTROLLER, now=now, origin_rule="rework_launch_failed",
                                       basis_seq=sent.seq)
                store.kv_set(jid, LAUNCH_KEY, {**st, "blocked": True})
                continue
            key = intents.intent_key(jid, LAUNCH_KIND, sent.seq, st["tries"] + 1)
            it = intents.claim(store, key=key, job_id=jid, kind=LAUNCH_KIND, lease_s=LAUNCH_LEASE_S, now=now,
                               owner=owner, params={"after_seq": sent.seq, "try": st["tries"] + 1})
            if it is None:
                continue
            try:
                res = launch_fn(jid) or {}
            except Exception as exc:  # noqa: BLE001 - a launcher failure is a refused launch
                res = {"accepted": False, "detail": f"{type(exc).__name__}: {exc}"}
            decision_trace.safe_record(store, job_id=jid, actor=CONTROLLER,
                                       stage="effect" if res.get("accepted") else "attempted",
                                       decision=f"launch_worker:{'accepted' if res.get('accepted') else 'refused'}",
                                       now=now, intent_key=key, evidence_refs=[f"event:{sent.seq}"])
            tries = st["tries"] + 1
            if res.get("accepted"):
                intents.complete(store, key, receipt={"tab_id": res.get("tab_id")}, now=now, owner=owner)
                st = {**st, "tries": tries, "accepted": True, "next_at": now + LAUNCH_RETRY_S[-1]}
            else:
                intents.fail(store, key, reason=str(res.get("detail") or "refused"), now=now, owner=owner)
                st = {**st, "tries": tries, "last_error": str(res.get("detail") or "refused")[:200],
                      "next_at": now + LAUNCH_RETRY_S[min(tries - 1, len(LAUNCH_RETRY_S) - 1)]}
            store.kv_set(jid, LAUNCH_KEY, st)
            out["relaunched"].append({"job": jid, "try": tries, "accepted": bool(res.get("accepted"))})
        except Exception as exc:  # noqa: BLE001
            out["errors"].append({"job": jid, "error": f"{type(exc).__name__}: {exc}"})

def _needs_head(store: Store, job_id: str) -> bool:
    return any(((b.clearing or {}).get("predicate") or blockers.spec(b.kind).predicate) == "verification_current_green"
               for b in blockers.open_blockers(store, job_id))


def _events_after(store: Store, it: intents.Intent):
    return store.events_since(it.job_id, int(it.params.get("after_seq") or 0))


def _check_reopen(store: Store, it: intents.Intent):
    sent = [e for e in _events_after(store, it) if e.what == "sent_back"]
    job = store.get_job(it.job_id)
    if sent and job is not None and job.state != JobState.NEEDS_HUMAN:
        return "done", {"steering_rev": (sent[-1].evidence or {}).get("steering_rev")}
    if sent:
        return "unknown", {}
    return "abandoned", {"reason": "no send-back was recorded after the claim"}


def _check_review(store: Store, it: intents.Intent):
    if any(e.what in _LEAD_EVENTS for e in _events_after(store, it)):
        return "done", {"reconciled": True, "via": "events"}
    return "unknown", {}


def _check_launch(store: Store, it: intents.Intent):
    if any(e.what == "worker attempt started" for e in _events_after(store, it)):
        return "done", {"worker_started": True}
    return "unknown", {}


def _check_notice(store: Store, it: intents.Intent):
    if it.blocker_id and store.kv_get(it.job_id, NOTIFIED_PREFIX + it.blocker_id):
        return "done", {"notified": True}
    return "unknown", {}


intents.register_checker("reopen", _check_reopen)
intents.register_checker(REVIEW_KIND, _check_review, reclaimable=True)
intents.register_checker(LAUNCH_KIND, _check_launch)
intents.register_checker("owner_notice", _check_notice)


def _settle_review(store: Store, it: intents.Intent, now: float) -> None:
    for bid in it.params.get("blockers") or []:
        b = blockers.get(store, bid)
        if b is None or not b.is_open:
            continue
        if b.disposition == "reviewing":
            blockers.transition(store, bid, "candidate", actor=CONTROLLER, now=now,
                                reason="review recovered after a controller restart")
        blockers.set_review_state(store, bid, "done", now=now)


def _recover_intents(store: Store, *, now: float, owner: str, throttle: bool = False, errors: list | None = None) -> dict:
    out = {"settled": [], "uncertain": [], "launches_held": []}
    errors = errors if errors is not None else []
    # Other integrations can register kind-specific checkers at runtime; restore ownership's
    # durable effect checkers before processing so an unrelated registration cannot weaken them.
    intents.register_checker("reopen", _check_reopen)
    intents.register_checker(REVIEW_KIND, _check_review, reclaimable=True)
    intents.register_checker(LAUNCH_KIND, _check_launch)
    intents.register_checker("owner_notice", _check_notice)
    last = store.kv_get("_ownership", "recovered_at") if throttle else None
    if last is not None and now - float(last) < RECOVER_EVERY_S:
        return out
    if throttle:
        store.kv_set("_ownership", "recovered_at", now)
    for fin in intents.reconcile_uncertain(store, now=now, owner=owner):
        if fin.state != "uncertain":
            out["settled"].append({"job": fin.job_id, "kind": fin.kind, "state": fin.state, "key": fin.key})
        if fin.kind == REVIEW_KIND and fin.state == "done":
            try:
                _settle_review(store, fin, now)
            except Exception as exc:  # noqa: BLE001
                errors.append({"job": fin.job_id, "error": f"{type(exc).__name__}: {exc}"})
    # An expired read-only review with no lead result can be safely handed to one new controller.
    rows = store._conn.execute("SELECT * FROM action_intents WHERE kind=? AND state='claimed' AND lease_expires<=? AND owner!=?",
                               (REVIEW_KIND, now, owner)).fetchall()
    for row in rows:
        it = intents._row(row)
        release_review(store, {"blocker_ids": it.params.get("blockers", [])},
                       reason="review lost (its controller died)", now=now)
        intents.fail(store, it.key, reason="review lost; safe to retry", now=now, owner=it.owner, retryable=True)
        out["settled"].append({"job": it.job_id, "kind": it.kind, "state": "released", "key": it.key})
    # A launch with no durable acknowledgement is held conservatively. Never make a second writer.
    from steward import sendback
    for job in store.list_jobs({JobState.RUNNING}):
        st = store.kv_get(job.job_id, LAUNCH_KEY)
        if not isinstance(st, dict) or st.get("accepted") or st.get("blocked") or not sendback.rework_pending(store, job.job_id):
            continue
        unsure = [i for i in intents.for_job(store, job.job_id, LAUNCH_KIND)
                  if i.state == "uncertain" and i.params.get("after_seq") == st.get("sent_seq")]
        if unsure:
            tries = max([int(st.get("tries") or 0)] + [int(i.params.get("try") or 0) for i in unsure])
            store.kv_set(job.job_id, LAUNCH_KEY, {**st, "tries": tries, "accepted": True, "unconfirmed": True,
                                                   "next_at": now + LAUNCH_RETRY_S[-1]})
            out["launches_held"].append(job.job_id)
    uncertain = store._conn.execute("SELECT * FROM action_intents WHERE state='uncertain'").fetchall()
    out["uncertain"] = [{"job": r["job_id"], "kind": r["kind"], "key": r["intent_key"], "reason": r["reason"]}
                         for r in uncertain]
    return out


def recover(store: Store, *, now: float, owner: str = intents.PROCESS_OWNER, notifier=None, head_fn=None) -> dict:
    out = {"intents": {}, "jobs": [], "errors": []}
    if not enabled():
        return out
    out["intents"] = _recover_intents(store, now=now, owner=owner, errors=out["errors"])
    ids = set(blockers.jobs_with_open_blockers(store)) | {j.job_id for j in store.list_jobs({JobState.NEEDS_HUMAN})}
    ids |= {j.job_id for j in store.list_jobs({JobState.RUNNING}) if store.kv_get(j.job_id, LAUNCH_KEY)}
    for jid in sorted(ids):
        try:
            head = head_fn(jid) if head_fn and _needs_head(store, jid) else None
            res = reconcile_job(store, jid, now=now, head=head, notifier=notifier)
            store.kv_set(jid, RECONCILED_AT_KEY, now)
            out["jobs"].append({"job": jid, "status": res.status, "cleared": res.cleared})
        except Exception as exc:  # noqa: BLE001
            out["errors"].append({"job": jid, "error": f"{type(exc).__name__}: {exc}"})
    return out


# Plan 2: authority revisions and grant-based blocker clearing are installed after this module is defined.
from steward import authority as _authority  # noqa: E402

_authority.install()
from steward import outcome_scope as _outcome_scope  # noqa: E402,F401 - registers outcome_unproven (Plan 4)
from steward import delivery as _delivery  # noqa: E402,F401 - registers delivery milestones and kinds (Plan 4)
