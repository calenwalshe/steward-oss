"""Recovery attempts: hypotheses, concrete changes, and observable proof of progress."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from steward import masking
from steward.actor import ActorRef
from steward.models import Judgment
from steward.store import Store

OUTCOMES = ("pending", "progressed", "failed", "abandoned")
TEXT_MAX = 400
EVIDENCE_MAX, EVIDENCE_REF_MAX = 24, 120


class AttemptError(ValueError):
    pass


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    job_id: str
    issue_key: str
    blocker_id: str | None
    approach_key: str
    round: int
    actor: str
    hypothesis: str
    change: str
    success_observation: str
    evidence: tuple
    evidence_digest: str
    outcome: str
    outcome_detail: dict | None
    decision_id: str | None
    created_epoch: float
    resolved_epoch: float | None


def approach_key(text: str) -> str:
    norm = " ".join(re.sub(r"[^a-z0-9]+", " ", str(text).lower()).split())
    return hashlib.sha256(norm.encode()).hexdigest()[:10]


def _row(r) -> Attempt:
    return Attempt(r["attempt_id"], r["job_id"], r["issue_key"], r["blocker_id"], r["approach_key"],
                   int(r["round"]), r["actor"], r["hypothesis"], r["change"], r["success_observation"],
                   tuple(json.loads(r["evidence_json"] or "[]")), r["evidence_digest"], r["outcome"],
                   json.loads(r["outcome_json"]) if r["outcome_json"] else None, r["decision_id"],
                   float(r["created_epoch"]), r["resolved_epoch"])


def get(store: Store, attempt_id: str) -> Attempt | None:
    r = store._conn.execute("SELECT * FROM recovery_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
    return _row(r) if r else None


def for_issue(store: Store, job_id: str, issue_key: str) -> list[Attempt]:
    rows = store._conn.execute("SELECT * FROM recovery_attempts WHERE job_id = ? AND issue_key = ?"
                               " ORDER BY created_epoch, rowid", (job_id, issue_key)).fetchall()
    return [_row(r) for r in rows]


def unproductive_rounds(store: Store, job_id: str, issue_key: str) -> int:
    n = 0
    for a in reversed(for_issue(store, job_id, issue_key)):
        if a.outcome == "progressed":
            break
        if a.outcome in ("failed", "pending"):
            n += 1
    return n


def failed_approaches(store: Store, job_id: str, issue_key: str) -> set[str]:
    return {a.approach_key for a in for_issue(store, job_id, issue_key) if a.outcome == "failed"}


def unchanged_since_last_failure(store: Store, job_id: str, issue_key: str, evidence_digest: str) -> bool:
    failed = [a for a in for_issue(store, job_id, issue_key) if a.outcome == "failed"]
    return bool(failed) and failed[-1].evidence_digest == evidence_digest


def _clip(text: str) -> str:
    return masking.mask(" ".join(str(text or "").split()))[:TEXT_MAX]


def record(store: Store, job_id: str, *, issue_key: str, actor: ActorRef, hypothesis: str, change: str,
           success_observation: str, evidence=(), blocker_id: str | None = None, now: float) -> Attempt:
    from steward import blockers, decision_trace, ownership

    hyp, chg, obs = _clip(hypothesis), _clip(change), _clip(success_observation)
    if not hyp or not chg:
        raise AttemptError("an attempt needs a hypothesis and a concrete change")
    if not obs:
        raise AttemptError("an attempt needs an observable success condition")
    refs = [masking.mask(str(e))[:EVIDENCE_REF_MAX] for e in list(evidence)[:EVIDENCE_MAX]]
    digest = blockers.evidence_digest(refs)
    rnd = unproductive_rounds(store, job_id, issue_key) + 1
    aid = "att-" + hashlib.sha256(f"{job_id}|{issue_key}|{rnd}|{now}|{chg}".encode()).hexdigest()[:12]
    decision_id = decision_trace.record(store, job_id=job_id, actor=actor, stage="attempted",
                                        decision=f"attempt {rnd} on {issue_key}: {chg}"[:400], now=now,
                                        blocker_id=blocker_id, evidence_refs=refs,
                                        authority_rev=ownership.authority_rev(store, job_id))
    with store.immediate() as tx:
        tx.conn.execute(
            "INSERT INTO recovery_attempts (attempt_id, job_id, issue_key, blocker_id, approach_key, round, actor,"
            " hypothesis, change, success_observation, evidence_json, evidence_digest, outcome, decision_id,"
            " created_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (aid, job_id, issue_key, blocker_id, approach_key(chg), rnd, actor.label(), hyp, chg, obs,
             json.dumps(refs), digest, decision_id, now))
        tx.append_event(job_id, what="recovery_attempt_recorded",
                        evidence={"attempt_id": aid, "issue_key": issue_key, "round": rnd, "actor": actor.label(),
                                  "hypothesis": hyp, "change": chg, "success_observation": obs},
                        rule="recovery_attempts", judgment=Judgment.code)
    return get(store, aid)


def resolve(store: Store, attempt_id: str, outcome: str, *, detail: dict, now: float) -> Attempt:
    from steward import decision_trace

    if outcome not in OUTCOMES[1:]:
        raise AttemptError(f"unknown outcome {outcome!r}")
    a = get(store, attempt_id)
    if a is None or a.outcome != "pending":
        raise AttemptError(f"attempt {attempt_id} is not pending")
    body = masking.mask_obj(dict(detail))
    with store.immediate() as tx:
        tx.conn.execute("UPDATE recovery_attempts SET outcome = ?, outcome_json = ?, resolved_epoch = ?"
                        " WHERE attempt_id = ? AND outcome = 'pending'", (outcome, json.dumps(body), now, attempt_id))
        tx.append_event(a.job_id, what="recovery_attempt_resolved",
                        evidence={"attempt_id": attempt_id, "issue_key": a.issue_key, "outcome": outcome,
                                  "detail": body}, rule="recovery_attempts", judgment=Judgment.code)
    if a.decision_id:
        decision_trace.outcome(store, a.decision_id, observed={"outcome": outcome, **body}, now=now)
    return get(store, attempt_id)


def progressed(store: Store, job_id: str, issue_key: str, *, detail: dict, now: float) -> int:
    n = 0
    for a in for_issue(store, job_id, issue_key):
        if a.outcome == "pending":
            resolve(store, a.attempt_id, "progressed", detail=detail, now=now)
            n += 1
    return n


def fail_pending(store: Store, job_id: str, issue_key: str, *, detail: dict, now: float) -> int:
    n = 0
    for a in for_issue(store, job_id, issue_key):
        if a.outcome == "pending":
            resolve(store, a.attempt_id, "failed", detail=detail, now=now)
            n += 1
    return n
