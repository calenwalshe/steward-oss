"""Bounded, read-only investigations for owner-flagged runs (spec §14).

A flag captures available supervision-chain evidence and an optional independent assessment. This module never stops a
job, changes authority/policy, or executes a proposed remedy; a supported handoff must use the existing tech-lead path.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from steward import blockers, decision_trace, judgment_ledger, masking
from steward.models import JobState, Judgment
from steward.store import Store

MAX_EVENTS = 80
MAX_DECISIONS = 80
MAX_JEV_RECEIPTS = 20
MAX_REPORT_CHARS = 12000
RETRY_AFTER_S = 300.0
CAUSES = {"bad_input", "model_judgment", "controller_routing", "downstream_execution", "multiple", "unknown"}


def _bounded(value, limit=MAX_REPORT_CHARS):
    clean = masking.mask_obj(value)
    raw = json.dumps(clean, ensure_ascii=False, sort_keys=True, default=str)
    if len(raw) <= limit:
        return clean
    return {"truncated": True, "digest": hashlib.sha256(raw.encode()).hexdigest()[:20],
            "excerpt": masking.mask(raw)[:limit - 100]}


def _flag(store: Store, flag_id: str) -> dict:
    row = store._conn.execute("SELECT * FROM run_flags WHERE flag_id=?", (flag_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown run flag {flag_id}")
    return dict(row)


def evidence_snapshot(store: Store, flag: dict) -> dict:
    """Build a bounded snapshot from available records; missing history stays explicitly missing."""
    jid, run = flag["job_id"], flag["run_id"]
    all_events = [e for e in store.events(jid) if e.what not in
                  ("run_investigation_completed", "run_investigation_failed")]
    # run_id's aN is the count of started attempts. Bound causal event citations
    # to the interval between that attempt's start and the next attempt start.
    try:
        attempt = int(run.rsplit(":a", 1)[1])
    except (IndexError, ValueError):
        attempt = -1
    starts = [i for i, e in enumerate(all_events) if e.what == "worker attempt started"]
    if attempt < 0 or attempt > len(starts):
        event_scope = []
    else:
        start_index = starts[attempt - 1] if attempt else -1
        end_index = starts[attempt] if attempt < len(starts) else len(all_events)
        event_scope = all_events[start_index + 1:end_index]
    events = event_scope[-MAX_EVENTS:]
    decisions = decision_trace.trace(store, jid, run=run)[-MAX_DECISIONS:]
    scoped_decision_ids = {d["decision_id"] for d in decisions}
    linked_refs = {str(ref) for d in decisions for ref in d.get("evidence", [])}
    event_rows = []
    for e in events[-MAX_EVENTS:]:
        ev = e.evidence or {}
        ref = f"event:{e.seq}"
        # Time-window membership is context only. A citation needs a durable link to this run's flag,
        # one of its decisions, or an explicit run id in the event itself.
        citable = (ref in linked_refs or ev.get("flag_id") == flag["flag_id"]
                   or ev.get("run_id") == run or ev.get("decision_id") in scoped_decision_ids)
        event_rows.append({"seq": e.seq, "at": e.ts, "what": e.what, "rule": e.rule,
                           "judgment": e.judgment.value, "evidence": e.evidence, "citable": citable})
    flag_notes = [dict(r) for r in store._conn.execute(
        "SELECT rowid AS note_id,actor_kind,actor_id,note,created_epoch FROM run_flag_notes WHERE flag_id=? "
        "ORDER BY rowid", (flag["flag_id"],)).fetchall()]
    all_jev = judgment_ledger.receipts(store, job_id=jid)
    jev = [r for r in all_jev if r.get("decision_id") in scoped_decision_ids][-MAX_JEV_RECEIPTS:]
    intent_rows = store._conn.execute("SELECT intent_key,kind,blocker_id,decision_id,params_json,state,attempt,"
                                      "authority_rev,evidence_digest,receipt_json,reason,updated_epoch "
                                      "FROM action_intents WHERE job_id=? ORDER BY updated_epoch DESC LIMIT ?",
                                      (jid, 40)).fetchall()
    intents = []
    for r in intent_rows:
        d = dict(r)
        d["params"] = json.loads(d.pop("params_json") or "{}")
        d["receipt"] = json.loads(d.pop("receipt_json")) if d.get("receipt_json") else None
        intents.append(d)
    all_decisions = decision_trace.trace(store, jid)
    blockers_list = blockers.for_job(store, jid)
    return _bounded({
        "flag": {k: flag[k] for k in ("flag_id", "job_id", "run_id", "actor_kind", "actor_id", "description", "created_epoch")},
        "job_state": store.get_job(jid).state.value if store.get_job(jid) else "unknown",
        "events": event_rows,
        "flag_notes": flag_notes,
        "decisions_for_run": decisions,
        "other_run_decision_count": max(0, len(all_decisions) - len(decisions)),
        "unattributed_jev_receipt_count": sum(1 for r in all_jev if r.get("decision_id") not in scoped_decision_ids),
        "intents": intents,
        "blockers": [{"blocker_id": b.blocker_id, "kind": b.kind, "disposition": b.disposition,
                      "claim": b.claim, "review_state": b.review_state, "authority_rev": b.authority_rev,
                      "evidence": list(b.evidence)} for b in blockers_list],
        "jev_receipts": [{"receipt_id": r["receipt_id"], "lane": r["lane"], "pack_id": r["pack_id"],
                          "status": r["status"], "model": r["model"], "state": r["state"],
                          "response": r["response"], "latency_ms": r["latency_ms"],
                          "decision_id": r["decision_id"]} for r in jev],
        "missing": ["events and Jev receipts without a current-run link are not causal evidence",
                    "raw transcripts are not retained here", "expired or never-recorded inputs cannot be reconstructed",
                    "causal links absent from decision/outcome records remain unknown"],
    })


def _assess(snapshot: dict, assessor=None) -> dict:
    if assessor is None:
        return {"status": "not_assessed", "causes": [{"type": "unknown", "evidence": [],
                                                         "why": "no independent assessor supplied"}],
                "remedy": None}
    try:
        result = assessor(snapshot)
    except Exception as exc:  # noqa: BLE001 - assessment outage is visible and does not stop the job
        return {"status": "assessor_unavailable", "causes": [{"type": "unknown", "evidence": [],
                                                                  "why": type(exc).__name__}], "remedy": None}
    if not isinstance(result, dict):
        return {"status": "invalid_assessment", "causes": [{"type": "unknown", "evidence": [],
                                                                "why": "assessment was not an object"}],
                "remedy": None}
    allowed_refs = {f"event:{e['seq']}" for e in snapshot.get("events", []) if e.get("citable") is True}
    causes = []
    for cause in result.get("causes", [])[:6]:
        kind = cause.get("type")
        refs = [str(r)[:160] for r in cause.get("evidence", [])[:8]]
        if kind not in CAUSES or not refs or not set(refs) <= allowed_refs:
            continue
        causes.append({"type": kind, "evidence": refs, "claim": str(cause.get("claim") or "")[:300],
                       "confidence": cause.get("confidence") if isinstance(cause.get("confidence"), (int, float))
                       and 0 <= cause.get("confidence") <= 1 else None})
    return {"status": "assessed" if causes else "insufficient_evidence",
            "causes": causes or [{"type": "unknown", "evidence": [],
                                   "why": "no supported causal claim cites captured evidence"}],
            "remedy": (str(result.get("remedy") or "")[:400] or None) if causes else None,
            "assessor": str(result.get("assessor") or "independent")[:120],
            "limitations": ["assessment is not human ground truth", "replay does not establish the full live counterfactual"]}


def investigate(store: Store, flag_id: str, *, assessor=None, now: float) -> dict:
    """Capture and assess once per exact evidence digest; duplicate unchanged flags reuse the result."""
    flag = _flag(store, flag_id)
    snapshot = evidence_snapshot(store, flag)
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with store.immediate() as tx:
        old = tx.conn.execute("SELECT * FROM run_investigations WHERE flag_id=? AND evidence_digest=?",
                              (flag_id, digest)).fetchone()
        if old is not None and old["state"] == "done":
            result = json.loads(old["report_json"] or "{}")
            return {"investigation_id": old["investigation_id"], "state": old["state"], "report": result,
                    "duplicate": True}
        if old is not None and now - float(old["created_epoch"]) < RETRY_AFTER_S:
            result = json.loads(old["report_json"] or "{}") if old["report_json"] else {}
            return {"investigation_id": old["investigation_id"], "state": old["state"], "report": result,
                    "duplicate": True}
        if old is None:
            investigation_id = "investigation-" + uuid.uuid4().hex[:20]
            tx.conn.execute("INSERT INTO run_investigations(investigation_id,flag_id,job_id,evidence_digest,state,"
                            "created_epoch) VALUES(?,?,?,?,?,?)",
                            (investigation_id, flag_id, flag["job_id"], digest, "running", now))
        else:
            investigation_id = old["investigation_id"]
            tx.conn.execute("UPDATE run_investigations SET state='running',created_epoch=?,finished_epoch=NULL "
                            "WHERE investigation_id=?", (now, investigation_id))
    assessment = _assess(snapshot, assessor)
    report = _bounded({"evidence": snapshot, "assessment": assessment,
                       "causal_claims": assessment.get("causes"), "missing_evidence": snapshot.get("missing", [])})
    report_json = json.dumps(report, ensure_ascii=False, sort_keys=True)
    state = "done" if assessment["status"] in ("assessed", "insufficient_evidence", "not_assessed") else "failed"
    with store._conn:
        store._conn.execute("UPDATE run_investigations SET state=?,report_json=?,remedy_json=?,finished_epoch=? "
                            "WHERE investigation_id=? AND state='running'",
                            (state, report_json, json.dumps({"proposed": assessment.get("remedy"),
                                                             "executed": False}), now, investigation_id))
    store.append_event(flag["job_id"], what="run_investigation_completed" if state == "done" else "run_investigation_failed",
                       evidence={"flag_id": flag_id, "investigation_id": investigation_id,
                                 "evidence_digest": digest[:20], "assessment_status": assessment["status"],
                                 "causal_claim_count": len(assessment.get("causes", [])), "remedy_executed": False},
                       rule="run_investigation", judgment=Judgment.code)
    return {"investigation_id": investigation_id, "state": state, "report": report, "duplicate": False}


def handoff_remedy(store: Store, investigation_id: str, blocker_id: str, *, actor, now: float) -> dict:
    """Queue a supported diagnosis through the existing tech-lead strategy path after current-state revalidation."""
    from steward import ownership

    row = store._conn.execute("SELECT job_id,state,report_json FROM run_investigations WHERE investigation_id=?",
                              (investigation_id,)).fetchone()
    if row is None or row["state"] != "done":
        return {"accepted": False, "reason": "investigation is not complete"}
    job = store.get_job(row["job_id"])
    b = blockers.get(store, blocker_id)
    if job is None or job.state not in (JobState.RUNNING, JobState.NEEDS_HUMAN) or b is None or b.job_id != row["job_id"]:
        return {"accepted": False, "reason": "job or blocker changed; refresh evidence first"}
    if b.disposition not in ("candidate", "reviewing") or blockers.spec(b.kind).owner_only:
        return {"accepted": False, "reason": "blocker is no longer eligible for recovery"}
    report = json.loads(row["report_json"] or "{}")
    assessment = report.get("assessment") or {}
    if assessment.get("status") != "assessed" or not assessment.get("remedy"):
        return {"accepted": False, "reason": "no supported remedy is available"}
    remedy = assessment["remedy"]
    result = ownership.request_strategy_review(store, job_id=job.job_id, blocker_id=b.blocker_id,
                                               reason=f"flagged-run diagnosis proposes: {remedy}",
                                               signature=f"flag-investigation:{investigation_id}", now=now)
    return {"accepted": True, "review": result, "executed": False, "actor": actor.label()}
