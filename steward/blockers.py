"""Durable blockers (continuous job ownership §4, §5, §8; Plan 1).

One row per issue EPISODE. `issue_key` is the stable identity (kind + normalised subject); `episode` is the evidence
generation. A repeat of the same evidence changes nothing; new evidence on an open blocker updates it in place (one
pending escalation per episode); an issue that recurs after resolution opens episode + 1 — only with changed evidence,
or after `recur_after_s` for signal kinds. The row is the current view; every change of disposition is also an
insert-only `blocker_history` row and a masked `blocker_*` event, so history is never lost.

Rights (spec §5): Jev, Flow/watchers, workers and unknown actors may only RAISE. Only the owner or the controller
(deterministic predicates, owner-only kinds) closes an owner-only blocker; a lead may withdraw a confirmation it raised
itself; nobody reopens a resolved row.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

from steward import masking, sanitize
from steward.actor import Actor, ActorRef
from steward.models import Judgment
from steward.store import Store

DISPOSITIONS = ("candidate", "reviewing", "confirmed_human", "waiting_external", "resolved")
OPEN = DISPOSITIONS[:4]
REVIEW_STATES = ("none", "queued", "in_flight", "done", "unavailable")
RESPONSIBLE = ("controller", "tech_lead", "owner", "external", "worker")
CLAIM_MAX = 400
REF_MAX = 120
REFS_MAX = 24
REQUEST_FIELD_MAX = 400
REQUEST_EVIDENCE_MAX = 8
REQUEST_OPTIONS_MAX = 6
_RAISE_ONLY = frozenset({Actor.JEV, Actor.MODEL_SUPERVISOR, Actor.WORKER, Actor.UNKNOWN})
_REVIEWERS = frozenset({Actor.TECH_LEAD, Actor.INDEPENDENT_REVIEWER})


class BlockerError(ValueError):
    """A refused blocker transition (actor lacks the right, missing request, resolved row)."""


@dataclass(frozen=True)
class KindSpec:
    owner_only: bool
    impact: int                    # 3 blocks all work, 2 blocks completion, 1 advisory
    affects: tuple[str, ...]
    predicate: str | None          # ownership clearing predicate name
    responsible: str
    notify_on_confirm: bool = False
    approval_kind: str | None = None


_T = ("progress", "completion")
KIND_SPECS: dict[str, KindSpec] = {
    "worker_parked_unfinished": KindSpec(False, 3, _T, "job_resumed", "tech_lead"),
    "finish_line_unmet": KindSpec(False, 3, _T, "job_resumed", "tech_lead"),
    "verification_failing": KindSpec(False, 3, _T, "verification_current_green", "tech_lead"),
    "verification_unverified": KindSpec(False, 3, ("completion",), "verification_current_green", "tech_lead"),
    "baseline_pending": KindSpec(False, 2, ("completion",), "baseline_reattributed", "controller"),
    "baseline_failures_remaining": KindSpec(False, 2, ("completion",), "verification_current_green", "tech_lead"),
    "test_weakening": KindSpec(False, 2, ("completion",), "job_resumed", "tech_lead"),
    "contract_incomplete": KindSpec(False, 2, ("completion",), "job_resumed", "tech_lead"),
    "deadline_evidence_pending": KindSpec(False, 2, ("completion",), "job_resumed", "tech_lead"),
    "recovery_exhausted": KindSpec(False, 3, _T, "job_resumed", "tech_lead"),
    "worker_start_blocked": KindSpec(False, 3, _T, "worker_started", "tech_lead"),
    "fleet_blocked": KindSpec(False, 3, _T, "job_resumed", "tech_lead"),
    "unclassified_hold": KindSpec(False, 2, _T, "job_resumed", "tech_lead"),
    "flow_finding": KindSpec(False, 1, ("progress",), "job_resumed", "tech_lead"),
    "jev_concern": KindSpec(False, 1, ("progress",), "signal_expired", "tech_lead"),
    "repeated_failure": KindSpec(False, 2, ("progress",), "checks_passed_since", "tech_lead"),
    "acceptance_decision": KindSpec(True, 2, ("completion",), "job_left_needs_human", "owner", True),
    "hard_constraint": KindSpec(True, 3, _T, "job_left_needs_human", "owner", True, "general"),
    "review_constraint": KindSpec(True, 2, ("completion",), "job_left_needs_human", "owner", True),
    "owner_question": KindSpec(True, 1, ("progress",), "question_answered", "owner"),
    "lead_escalation": KindSpec(False, 2, ("progress",), "approval_decided", "owner", True, "tech_lead"),
    "lead_reserved_action": KindSpec(True, 2, ("progress",), "approval_decided", "owner", True, "tech_lead"),
    # issue W: the watchdog's runner breaker opened (cooldown, then it retries); cleared once healthy again
    "watchdog_breaker": KindSpec(False, 3, _T, "watchdog_target_healthy", "tech_lead"),
    # spec 2026-10-08 §5.1 "The ask": the liveness ladder is exhausted; the owner says retry / stop / release
    "liveness_stuck": KindSpec(True, 2, ("progress", "completion"), "job_resumed", "owner", True, "general"),
}


def spec(kind: str) -> KindSpec:
    return KIND_SPECS.get(kind) or KIND_SPECS["unclassified_hold"]


def register_kind(kind: str, kind_spec: KindSpec) -> None:
    """Plans 2 and 4 add kinds (e.g. `authorization_missing`, `delivery_pending`); a kind is never redefined."""
    if kind in KIND_SPECS and KIND_SPECS[kind] != kind_spec:
        raise ValueError(f"blocker kind {kind!r} is already registered differently")
    KIND_SPECS[kind] = kind_spec


@dataclass(frozen=True)
class HumanRequest:
    decision: str
    why_authority_insufficient: str
    evidence: tuple[str, ...] = ()
    continuing: str = ""
    options: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"decision": self.decision, "why_authority_insufficient": self.why_authority_insufficient,
                "evidence": list(self.evidence), "continuing": self.continuing, "options": list(self.options)}


def _s(v, n: int) -> str:
    return sanitize.text(" ".join(str(v or "").split()), max_chars=n)


def validate_human_request(raw: dict) -> HumanRequest:
    """Spec §5: a confirmed human request states the exact decision, why existing authority is insufficient, the
    evidence and the independent work that continues. Missing decision/why is a ValueError (a malformed answer)."""
    if not isinstance(raw, dict):
        raise ValueError("human request must be an object")
    decision, why = _s(raw.get("decision"), REQUEST_FIELD_MAX), _s(raw.get("why_authority_insufficient"),
                                                                    REQUEST_FIELD_MAX)
    if not decision or not why:
        raise ValueError("a human request needs `decision` and `why_authority_insufficient`")
    ev = [_s(e, 200) for e in (raw.get("evidence") or []) if _s(e, 200)][:REQUEST_EVIDENCE_MAX]
    opts = [_s(o, 120) for o in (raw.get("options") or []) if _s(o, 120)][:REQUEST_OPTIONS_MAX]
    return HumanRequest(decision, why, tuple(ev), _s(raw.get("continuing"), REQUEST_FIELD_MAX), tuple(opts))


@dataclass(frozen=True)
class Blocker:
    blocker_id: str
    job_id: str
    issue_key: str
    episode: int
    kind: str
    claim: str
    affected: tuple[str, ...]
    disposition: str
    responsible: str
    review_state: str
    clearing: dict
    wake: dict | None
    evidence: tuple[str, ...]
    evidence_digest: str
    authority_rev: int
    basis_seq: int
    origin_actor: str
    origin_rule: str
    human_request: dict | None
    diagnosis: dict | None
    priority: float
    created_epoch: float
    updated_epoch: float
    next_review_epoch: float | None
    resolved_epoch: float | None
    resolution: dict | None

    @property
    def is_open(self) -> bool:
        return self.disposition in OPEN


def issue_key(kind: str, subject: str) -> str:
    norm = " ".join(str(subject or "").lower().split())
    return f"{kind}:{hashlib.sha256(norm.encode()).hexdigest()[:10]}"


def evidence_digest(evidence: Iterable[str]) -> str:
    canon = json.dumps(sorted({str(e) for e in evidence}))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _j(v):
    return None if v is None else json.loads(v)


def _row(r) -> Blocker:
    return Blocker(r["blocker_id"], r["job_id"], r["issue_key"], int(r["episode"]), r["kind"], r["claim"],
                   tuple(json.loads(r["affected_json"] or "[]")), r["disposition"], r["responsible"],
                   r["review_state"], json.loads(r["clearing_json"] or "{}"), _j(r["wake_json"]),
                   tuple(json.loads(r["evidence_json"] or "[]")), r["evidence_digest"], int(r["authority_rev"]),
                   int(r["basis_seq"]), r["origin_actor"], r["origin_rule"], _j(r["human_request_json"]),
                   _j(r["diagnosis_json"]), float(r["priority"]), float(r["created_epoch"]),
                   float(r["updated_epoch"]), r["next_review_epoch"], r["resolved_epoch"], _j(r["resolution_json"]))


def get(store: Store, blocker_id: str) -> Blocker | None:
    r = store._conn.execute("SELECT * FROM blockers WHERE blocker_id = ?", (blocker_id,)).fetchone()
    return None if r is None else _row(r)


def for_job(store: Store, job_id: str, *, open_only: bool = False) -> list[Blocker]:
    q = "SELECT * FROM blockers WHERE job_id = ?"
    if open_only:
        q += " AND disposition != 'resolved'"
    return [_row(r) for r in store._conn.execute(q + " ORDER BY created_epoch, rowid", (job_id,)).fetchall()]


def open_blockers(store: Store, job_id: str) -> list[Blocker]:
    return for_job(store, job_id, open_only=True)


def jobs_with_open_blockers(store: Store) -> list[str]:
    rows = store._conn.execute("SELECT DISTINCT job_id FROM blockers WHERE disposition != 'resolved'").fetchall()
    return sorted(r[0] for r in rows)


def history(store: Store, blocker_id: str) -> list[dict]:
    rows = store._conn.execute("SELECT * FROM blocker_history WHERE blocker_id = ? ORDER BY seq",
                               (blocker_id,)).fetchall()
    return [{**dict(r), "evidence": json.loads(r["evidence_json"] or "[]")} for r in rows]


def _judgment(a: ActorRef) -> Judgment:
    if a.kind == Actor.OWNER:
        return Judgment.human
    if a.kind in (Actor.TECH_LEAD, Actor.INDEPENDENT_REVIEWER, Actor.OPERATOR_AGENT):
        return Judgment.model_informed_code_authorized
    if a.kind in (Actor.JEV, Actor.MODEL_SUPERVISOR):
        return Judgment.model
    return Judgment.code


def _refs(store: Store, job_id: str, evidence: Iterable[str]) -> list[str]:
    masking.arm_job(store, job_id)
    out: list[str] = []
    for e in evidence:
        m = sanitize.text(e, max_chars=REF_MAX)
        if m and m not in out:
            out.append(m)
    return out[-REFS_MAX:]


def _history(tx, b_id: str, job_id: str, now: float, frm, to: str, actor: ActorRef, reason: str, evidence) -> None:
    tx.conn.execute("INSERT INTO blocker_history (blocker_id, job_id, at_epoch, from_disposition, to_disposition,"
                    " actor, reason, evidence_json) VALUES (?,?,?,?,?,?,?,?)",
                    (b_id, job_id, now, frm, to, actor.label(), masking.mask(reason)[:400], json.dumps(list(evidence))))


def raise_blocker(store: Store, job_id: str, *, kind: str, subject: str, claim: str, evidence: Iterable[str],
                  origin_actor: ActorRef, now: float, origin_rule: str = "", affected: Iterable[str] | None = None,
                  responsible: str | None = None, disposition: str = "candidate", clearing: dict | None = None,
                  wake: dict | None = None, authority_rev: int = 0, basis_seq: int = 0, priority: float = 0.0,
                  recur_after_s: float | None = None) -> tuple[Blocker, bool]:
    """Upsert one issue episode in one IMMEDIATE transaction. Returns (row, created)."""
    if disposition not in OPEN:
        raise ValueError(f"a blocker is raised open, not {disposition!r}")
    ks = spec(kind)
    key = issue_key(kind, subject)
    refs = _refs(store, job_id, evidence)
    digest = evidence_digest(refs)
    masking.arm_job(store, job_id)
    claim_m = sanitize.text(" ".join(str(claim).split()), max_chars=CLAIM_MAX)
    with store.immediate() as tx:
        latest = tx.conn.execute("SELECT * FROM blockers WHERE job_id = ? AND issue_key = ? ORDER BY episode DESC"
                                 " LIMIT 1", (job_id, key)).fetchone()
        if latest is not None and latest["disposition"] != "resolved":
            existing_refs = json.loads(latest["evidence_json"] or "[]")
            if latest["evidence_digest"] == digest or set(refs).issubset(existing_refs):
                return _row(latest), False
            merged = list(dict.fromkeys(json.loads(latest["evidence_json"]) + refs))[-REFS_MAX:]
            new_digest = evidence_digest(merged)
            tx.conn.execute("UPDATE blockers SET evidence_json = ?, evidence_digest = ?, updated_epoch = ?,"
                            " basis_seq = MAX(basis_seq, ?), priority = MAX(priority, ?) WHERE blocker_id = ?",
                            (json.dumps(merged), new_digest, now, basis_seq, float(priority), latest["blocker_id"]))
            _history(tx, latest["blocker_id"], job_id, now, latest["disposition"], latest["disposition"],
                     origin_actor, "evidence updated", refs)
            tx.append_event(job_id, what="blocker_evidence_updated",
                            evidence={"blocker_id": latest["blocker_id"], "kind": kind, "evidence": refs[:6]},
                            rule="ownership", judgment=_judgment(origin_actor))
            row = tx.conn.execute("SELECT * FROM blockers WHERE blocker_id = ?", (latest["blocker_id"],)).fetchone()
            return _row(row), False
        if latest is not None and latest["evidence_digest"] == digest and (
                recur_after_s is None or now - float(latest["resolved_epoch"] or 0) < recur_after_s):
            return _row(latest), False
        episode = int(latest["episode"]) + 1 if latest is not None else 1
        b_id = "blk-" + hashlib.sha256(f"{job_id}|{key}|{episode}".encode()).hexdigest()[:12]
        tx.conn.execute(
            "INSERT INTO blockers (blocker_id, job_id, issue_key, episode, kind, claim, affected_json, disposition,"
            " responsible, review_state, clearing_json, wake_json, evidence_json, evidence_digest, authority_rev,"
            " basis_seq, origin_actor, origin_rule, priority, created_epoch, updated_epoch)"
            " VALUES (?,?,?,?,?,?,?,?,?,'none',?,?,?,?,?,?,?,?,?,?,?)",
            (b_id, job_id, key, episode, kind, claim_m, json.dumps(list(affected or ks.affects)), disposition,
             responsible or ks.responsible, json.dumps(clearing or {"predicate": ks.predicate}),
             json.dumps(wake) if wake is not None else None, json.dumps(refs), digest, int(authority_rev),
             int(basis_seq), origin_actor.label(), origin_rule[:80], max(0.0, float(priority)), now, now))
        _history(tx, b_id, job_id, now, None, disposition, origin_actor, f"raised: {origin_rule or kind}", refs)
        tx.append_event(job_id, what="blocker_raised",
                        evidence={"blocker_id": b_id, "kind": kind, "episode": episode, "disposition": disposition,
                                  "claim": claim_m[:200], "origin": origin_actor.label(), "rule": origin_rule},
                        rule="ownership", judgment=_judgment(origin_actor))
        row = tx.conn.execute("SELECT * FROM blockers WHERE blocker_id = ?", (b_id,)).fetchone()
    return _row(row), True


def _refuse_reason(b: Blocker, to: str, actor: ActorRef, human_request) -> str | None:
    ks = spec(b.kind)
    if to not in DISPOSITIONS:
        return f"unknown disposition {to!r}"
    if not b.is_open:
        return "a resolved blocker is history; a recurrence opens a new episode"
    if actor.kind in _RAISE_ONLY:
        return f"{actor.kind.value} may raise blockers, never decide them"
    if to == "resolved":
        if actor.kind in (Actor.OWNER, Actor.CONTROLLER):
            return None
        if ks.owner_only:
            return f"{b.kind} is the owner's decision"
        if b.disposition == "confirmed_human" and not (actor.kind in _REVIEWERS and b.origin_actor == actor.label()):
            return "a confirmed owner request is closed by the owner, or withdrawn by the reviewer that raised it"
        return None
    if to == "confirmed_human":
        if actor.kind == Actor.CONTROLLER:
            return None if ks.owner_only or human_request is not None else "the controller confirms owner-only kinds"
        if actor.kind in _REVIEWERS | {Actor.OWNER}:
            return None if human_request is not None or actor.kind == Actor.OWNER else \
                "confirming needs the exact decision and why authority is insufficient"
        return "only a reviewer, the controller or the owner confirms a human requirement"
    if actor.kind in frozenset({Actor.CONTROLLER, Actor.OWNER}) | _REVIEWERS:
        return None
    return f"{actor.kind.value} may not move a blocker to {to}"


def transition(store: Store, blocker_id: str, to: str, *, actor: ActorRef, reason: str, now: float,
               evidence: Iterable[str] = (), human_request: HumanRequest | None = None,
               resolution: dict | None = None, wake: dict | None = None) -> Blocker:
    b = get(store, blocker_id)
    if b is None:
        raise BlockerError(f"unknown blocker {blocker_id}")
    why = _refuse_reason(b, to, actor, human_request)
    if why:
        raise BlockerError(why)
    refs = _refs(store, b.job_id, evidence)
    res = None
    if to == "resolved":
        res = {"actor": actor.label(), "reason": sanitize.text(reason, max_chars=400), "evidence": refs, **sanitize.obj(resolution or {})}
    with store.immediate() as tx:
        cur = tx.conn.execute("SELECT disposition FROM blockers WHERE blocker_id = ?", (blocker_id,)).fetchone()
        if cur is None or cur["disposition"] == "resolved":
            raise BlockerError("blocker was resolved concurrently")
        tx.conn.execute(
            "UPDATE blockers SET disposition = ?, updated_epoch = ?, resolved_epoch = ?, resolution_json = ?,"
            " human_request_json = COALESCE(?, human_request_json), wake_json = COALESCE(?, wake_json),"
            " responsible = CASE WHEN ? = 'confirmed_human' THEN 'owner' ELSE responsible END,"
            " review_state = CASE WHEN ? IN ('resolved', 'confirmed_human') THEN 'done' ELSE review_state END"
            " WHERE blocker_id = ?",
            (to, now, now if to == "resolved" else None, json.dumps(res) if res else None,
             json.dumps(human_request.to_dict()) if human_request else None,
             json.dumps(wake) if wake is not None else None, to, to, blocker_id))
        _history(tx, blocker_id, b.job_id, now, cur["disposition"], to, actor, reason, refs)
        tx.append_event(b.job_id, what="blocker_transition",
                        evidence={"blocker_id": blocker_id, "kind": b.kind, "from": cur["disposition"], "to": to,
                                  "by": actor.label(), "reason": sanitize.text(reason, max_chars=200),
                                  **({"decision": human_request.decision} if human_request else {})},
                        rule="ownership", judgment=_judgment(actor))
    if to == "resolved":
        from steward import decision_trace

        observed = {"resolved_by": actor.label(), "reason": sanitize.text(reason, max_chars=200)}
        if b.kind in decision_trace.SIGNAL_KINDS:
            observed["signal_use"] = ("overruled" if actor.kind in
                                      (Actor.TECH_LEAD, Actor.INDEPENDENT_REVIEWER, Actor.OWNER) else "expired")
        decision_trace.safe_outcomes(store, blocker_id, observed=observed, now=now)
    return get(store, blocker_id)


def set_review_state(store: Store, blocker_id: str, state: str, *, now: float,
                     next_review_epoch: float | None = None) -> Blocker:
    if state not in REVIEW_STATES:
        raise ValueError(f"unknown review state {state!r}")
    with store._conn:
        store._conn.execute("UPDATE blockers SET review_state = ?, next_review_epoch = COALESCE(?, next_review_epoch)"
                            " WHERE blocker_id = ? AND disposition != 'resolved'", (state, next_review_epoch, blocker_id))
    return get(store, blocker_id)


def record_diagnosis(store: Store, blocker_id: str, diagnosis: dict, *, now: float) -> Blocker:
    """Persist a review outcome keyed by the evidence/authority it was made on (spec §5: reuse when unchanged)."""
    b = get(store, blocker_id)
    if b is None:
        raise BlockerError(f"unknown blocker {blocker_id}")
    masking.arm_job(store, b.job_id)
    merged = {**(b.diagnosis or {}), **sanitize.obj(diagnosis), "evidence_digest": b.evidence_digest,
              "authority_rev": b.authority_rev, "at": now}
    with store._conn:
        store._conn.execute("UPDATE blockers SET diagnosis_json = ? WHERE blocker_id = ?",
                            (json.dumps(merged)[:8000], blocker_id))
    return get(store, blocker_id)


def reusable_diagnosis(b: Blocker) -> dict | None:
    d = b.diagnosis or {}
    if d and d.get("evidence_digest") == b.evidence_digest and d.get("authority_rev") == b.authority_rev:
        return d
    return None


def touch(store: Store, blocker_id: str, *, now: float) -> None:
    """A repeat sighting of an unchanged signal: refresh `wake.last_seen` only (no new episode, no new review)."""
    b = get(store, blocker_id)
    if b is None or not b.is_open:
        return
    with store._conn:
        store._conn.execute("UPDATE blockers SET wake_json = ? WHERE blocker_id = ?",
                            (json.dumps({**(b.wake or {}), "last_seen": now}), blocker_id))


def add_evidence(store: Store, blocker_id: str, evidence: Iterable[str], *, actor: ActorRef, now: float,
                 reason: str = "evidence added") -> Blocker:
    """New evidence on an open blocker, addressed by id (answers, strategy-review markers, signals). Changes the
    evidence digest, so a previous diagnosis is no longer reused (spec §5)."""
    b = get(store, blocker_id)
    if b is None or not b.is_open:
        raise BlockerError(f"no open blocker {blocker_id}")
    refs = _refs(store, b.job_id, evidence)
    merged = list(dict.fromkeys(list(b.evidence) + refs))[-REFS_MAX:]
    digest = evidence_digest(merged)
    if digest == b.evidence_digest:
        return b
    with store.immediate() as tx:
        cur = tx.conn.execute("UPDATE blockers SET evidence_json = ?, evidence_digest = ?, updated_epoch = ?"
                              " WHERE blocker_id = ? AND disposition != 'resolved'",
                              (json.dumps(merged), digest, now, blocker_id))
        if cur.rowcount != 1:
            raise BlockerError("blocker was resolved concurrently")
        _history(tx, blocker_id, b.job_id, now, b.disposition, b.disposition, actor, reason, refs)
        tx.append_event(b.job_id, what="blocker_evidence_updated",
                        evidence={"blocker_id": blocker_id, "kind": b.kind, "evidence": refs[:6]},
                        rule="ownership", judgment=_judgment(actor))
    return get(store, blocker_id)
