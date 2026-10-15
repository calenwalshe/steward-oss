"""Plan 3/4 acceptance seams: separate evaluation lanes and end-to-end evidence attribution."""
from __future__ import annotations

import json
import time

from steward import decision_trace, jev_outcomes, judgment_ledger, qualification, run_investigation
from steward.actor import Actor, ActorRef
from steward.contract import Contract, store_contract
from steward.jev_observer import FakeTransport, TransportResponse
from steward.models import JobState, Judgment
from steward.store import Store


OWNER = ActorRef(Actor.OWNER, "fixture")
INDEPENDENT = ActorRef(Actor.INDEPENDENT_REVIEWER, "assessment-fixture")


def _job(store):
    job = store.create_job("audit supervision evidence", "/repo")
    store_contract(store, job.job_id, Contract(goal=job.goal, done_when=["the evidence is reviewed"]))
    store.set_state(job.job_id, JobState.RUNNING, what="worker attempt started", evidence={}, rule="fixture",
                    judgment=Judgment.code)
    return job.job_id


def _record_lane(store, job_id, lane, *, mode=""):
    body = json.dumps({"model": "pinned-test-model", "state": {"revision": "r1"},
                       "questions": {lane: {"criteria": {"ok": "ok", "miss": "miss"}}}}).encode()
    transport = judgment_ledger.RecordingTransport(
        FakeTransport(TransportResponse(200, json.dumps({"answers": {lane: {"choice": "ok", "confidence": 0.8,
                                                                             "probabilities": {"ok": 0.8}}}}).encode())))
    with judgment_ledger.call(store, job_id, lane, mode=mode):
        transport.post("https://offline.invalid", body, {}, 1.0)
    return judgment_ledger.receipts(store, job_id=job_id, lane=lane)[-1]


def test_continuous_ownership_recovery_acceptance_j9_skill_selection_measurement_cannot_promote_supervision(
        steward_home):
    store = Store(steward_home)
    jid = _job(store)
    triage = _record_lane(store, jid, "triage")
    skills = _record_lane(store, jid, "interactive_skill_selection", mode="interactive")
    now = time.time()
    jev_outcomes.label(store, triage["receipt_id"], "ok", source="owner_correction", actor=OWNER,
                       question="triage", now=now)
    jev_outcomes.label(store, skills["receipt_id"], "ok", source="independent_model", actor=INDEPENDENT,
                       question="interactive_skill_selection", uncertainty="benchmark-only", now=now)

    report = jev_outcomes.report(store, [jid])

    assert report["lanes"]["triage"]["labels"] == {"owner_correction": 1}
    assert report["lanes"]["interactive_skill_selection"]["labels"] == {"independent_model": 1}
    assert report["lanes"]["triage"]["activation_gate"]["promotion"] == "not_authorized_by_measurement"
    assert report["lanes"]["interactive_skill_selection"]["activation_gate"]["requires_explicit_owner_activation"]
    assert "not_authorized" in report["promotion"]


def test_continuous_ownership_recovery_acceptance_j16_outcome_evidence_is_receipt_bound_and_lane_scoped(
        steward_home):
    store = Store(steward_home)
    jid = _job(store)
    receipts = [_record_lane(store, jid, lane, mode="mode-a") for lane in ("triage", "lane_triage")]
    for r in receipts:
        decision = store._conn.execute("SELECT run_id FROM decision_records WHERE decision_id=?",
                                       (r["decision_id"],)).fetchone()
        event = store.append_event(jid, what="worker reported done",
                                   evidence={"marker": "STEWARD-DONE", "run_id": decision["run_id"]},
                                   rule="fixture_marker", judgment=Judgment.code)
        jev_outcomes.label(store, r["receipt_id"], "finished", source="code_outcome",
                           actor=ActorRef(Actor.CONTROLLER, "fixture"), evidence=[f"seq:{event.seq}"], now=time.time())

    report = jev_outcomes.report(store, [jid])

    for r in receipts:
        lane = report["lanes"][r["lane"]]
        (evidence,) = lane["evaluation_evidence"]
        assert evidence["receipt_id"] == r["receipt_id"] and evidence["decision_id"] == r["decision_id"]
        assert evidence["source"] == "code_outcome" and len(evidence["refs"]) == 1
        assert lane["activation_gate"]["rollback"] == "evidence_report_only"
    assert report["lanes"]["triage"]["evaluation_evidence"] != report["lanes"]["lane_triage"]["evaluation_evidence"]


def test_continuous_ownership_recovery_acceptance_l5_machine_outcomes_and_owner_corrections_remain_separate(
        steward_home):
    store = Store(steward_home)
    jid = _job(store)
    receipt = _record_lane(store, jid, "triage")
    decision = store._conn.execute("SELECT run_id FROM decision_records WHERE decision_id=?",
                                   (receipt["decision_id"],)).fetchone()
    event = store.append_event(jid, what="worker reported done",
                               evidence={"marker": "STEWARD-DONE", "run_id": decision["run_id"]},
                               rule="fixture_marker", judgment=Judgment.code)
    jev_outcomes.label(store, receipt["receipt_id"], "finished", source="code_outcome",
                       actor=ActorRef(Actor.CONTROLLER, "fixture"), evidence=[f"seq:{event.seq}"], now=time.time())
    jev_outcomes.label(store, receipt["receipt_id"], "missed", source="owner_correction", actor=OWNER,
                       evidence=[f"seq:{event.seq}"], now=time.time())

    lane = jev_outcomes.report(store, [jid])["lanes"]["triage"]

    assert lane["labels"] == {"code_outcome": 1, "owner_correction": 1}
    assert lane.get("observed_outcomes", {}).get("code_outcome", {}).get("misses", 0) == 0
    assert lane["observed_outcomes"]["owner_correction"]["misses"] == 1
    assert {r["source"] for r in lane["evaluation_evidence"]} == {"code_outcome", "owner_correction"}


def test_continuous_ownership_recovery_acceptance_f10_flag_investigation_retention_and_owner_surface_are_attributed(
        steward_home):
    store = Store(steward_home)
    jid = _job(store)
    started_at = time.time()
    ref_event = store.append_event(jid, what="controller routed an unclear result", evidence={}, rule="fixture",
                                   judgment=Judgment.code)
    flag, created = decision_trace.flag_run(store, jid, description="review the unclear result", actor=OWNER,
                                            now=started_at)
    assert created
    decision_trace.record(store, job_id=jid, actor=OWNER, stage="signal", decision="flagged_run",
                          now=started_at, evidence_refs=[f"flag:{flag['flag_id']}"])
    event = store.append_event(jid, what="flag evidence attached", evidence={"flag_id": flag["flag_id"]},
                               rule="fixture", judgment=Judgment.code)
    assessment = run_investigation.investigate(
        store, flag["flag_id"], now=started_at + 1,
        assessor=lambda snapshot: {"causes": [{"type": "controller_routing", "claim": "routing needs review",
                                                "evidence": [f"event:{event.seq}"]}],
                                  "remedy": "route the evidence to the technical lead"})
    assert assessment["state"] == "done" and assessment["report"]["assessment"]["status"] == "assessed"
    assert assessment["report"]["evidence"]["flag"]["actor_kind"] == "owner"
    assert assessment["report"]["evidence"]["events"][-1]["citable"] is True
    assert store.get_job(jid).state == JobState.RUNNING  # an assessment is evidence, not a stop or action

    far = started_at + 90 * 86400
    assert decision_trace.preserved(store, jid, now=far)
    decision_trace.maybe_purge(store, now=far)
    assert decision_trace.trace(store, jid)
    decision_trace.close_flag(store, flag["flag_id"], actor=OWNER, resolution="investigated", now=far + 1)
    decision_trace.purge(store, now=far + 31 * 86400)
    assert decision_trace.trace(store, jid) == []  # after closure, ordinary retention resumes
    metrics = qualification.observe(store, [jid], now=far + 31 * 86400)
    assert metrics["causal_attribution"] == "unknown_unless_linked_by_recorded_decision_and_outcome"
    assert ref_event.seq != event.seq
