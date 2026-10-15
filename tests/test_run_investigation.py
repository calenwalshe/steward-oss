import json
import threading

from steward import blockers, decision_trace, ownership, run_investigation
from steward.actor import Actor, ActorRef
from steward.models import JobState, Judgment
from steward.contract import Contract, store_contract
from steward.store import Store

OWNER = ActorRef(Actor.OWNER, "fixture")


def _flagged(store):
    job = store.create_job("investigate a flagged run", "/repo")
    store_contract(store, job.job_id, Contract(goal=job.goal, done_when=["the flagged outcome is investigated"]))
    store.set_state(job.job_id, JobState.RUNNING, what="worker started", evidence={}, rule="test",
                    judgment=Judgment.code)
    flag, _ = decision_trace.flag_run(store, job.job_id, description="the controller may have routed badly",
                                      actor=OWNER, now=100.0)
    return job.job_id, flag


def test_flagged_run_investigation_is_read_only_bounded_and_idempotent_continuous_ownership_recovery_acceptance(steward_home):
    store = Store(steward_home)
    jid, flag = _flagged(store)
    calls = []

    first = run_investigation.investigate(store, flag["flag_id"],
                                          assessor=lambda packet: calls.append(packet) or {}, now=101.0)
    second = run_investigation.investigate(store, flag["flag_id"],
                                           assessor=lambda _packet: (_ for _ in ()).throw(AssertionError()), now=102.0)

    assert first["state"] == "done" and second["duplicate"] is True
    assert first["investigation_id"] == second["investigation_id"] and len(calls) == 1
    assert first["report"]["assessment"]["status"] == "insufficient_evidence"
    assert first["report"]["missing_evidence"]
    assert store.get_job(jid).state == JobState.RUNNING
    assert not [e for e in store.events(jid) if e.what == "owner_request_notified"]


def test_flagged_investigation_survives_store_reopen_and_is_exposed_by_cli_continuous_ownership_recovery_acceptance(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    _jid, flag = _flagged(store)
    first = run_investigation.investigate(store, flag["flag_id"], now=101.0)
    store.close()
    reopened = Store(steward_home)
    second = run_investigation.investigate(reopened, flag["flag_id"], now=102.0)
    assert second["investigation_id"] == first["investigation_id"] and second["duplicate"]

    from steward import cli
    assert cli.main(["investigate", flag["flag_id"], "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["investigation_id"] == first["investigation_id"]
    assert payload["report"]["assessment"]["status"] == "not_assessed"


def test_concurrent_flag_investigations_claim_one_assessor_call_continuous_ownership_recovery_acceptance(steward_home):
    store = Store(steward_home)
    _jid, flag = _flagged(store)
    entered, release = threading.Event(), threading.Event()
    calls, results = [], []

    def assessor(_packet):
        calls.append(True)
        entered.set()
        assert release.wait(2)
        return {}

    worker = threading.Thread(target=lambda: results.append(
        run_investigation.investigate(store, flag["flag_id"], assessor=assessor, now=101.0)))
    worker.start()
    assert entered.wait(2)
    duplicate = run_investigation.investigate(store, flag["flag_id"], assessor=assessor, now=102.0)
    release.set()
    worker.join(2)

    assert not worker.is_alive() and len(calls) == 1
    assert duplicate["state"] == "running" and duplicate["duplicate"] is True
    assert len(results) == 1 and results[0]["state"] == "done"


def test_flagged_run_investigation_distinguishes_supported_cause_classes_continuous_ownership_recovery_acceptance(steward_home):
    store = Store(steward_home)
    _jid, flag = _flagged(store)
    event = store.events(flag["job_id"])[-1]
    ref = f"event:{event.seq}"
    result = run_investigation.investigate(store, flag["flag_id"], assessor=lambda _p: {
        "causes": [{"type": name, "claim": f"classified {name}", "evidence": [ref]}
                   for name in ("bad_input", "model_judgment", "controller_routing", "downstream_execution")]},
        now=101.0)
    causes = result["report"]["assessment"]["causes"]
    assert {cause["type"] for cause in causes} == {
        "bad_input", "model_judgment", "controller_routing", "downstream_execution"}


def test_investigation_cannot_cite_events_from_another_run_continuous_ownership_recovery_acceptance(steward_home):
    store = Store(steward_home)
    jid, _first_flag = _flagged(store)
    old = store.append_event(jid, what="old run failure", evidence={"source": "prior attempt"}, rule="old",
                             judgment=Judgment.code)
    store.append_event(jid, what="worker attempt started", evidence={"attempt_no": 1}, rule="dispatch",
                       judgment=Judgment.code)
    store.append_event(jid, what="current run progress", evidence={}, rule="current", judgment=Judgment.code)
    flag, _ = decision_trace.flag_run(store, jid, description="review current attempt", actor=OWNER, now=100.0)
    result = run_investigation.investigate(store, flag["flag_id"], assessor=lambda _packet: {
        "causes": [{"type": "downstream_execution", "claim": "old failure caused this run",
                    "evidence": [f"event:{old.seq}"]}]}, now=101.0)
    assert result["report"]["assessment"]["status"] == "insufficient_evidence"


def test_investigation_cannot_cite_an_unlinked_event_inside_its_run_window(steward_home):
    store = Store(steward_home)
    job = store.create_job("ignore unrelated same-run events", "/repo")
    store_contract(store, job.job_id, Contract(goal=job.goal, done_when=["investigate the flagged outcome"]))
    store.append_event(job.job_id, what="worker attempt started", evidence={}, rule="dispatch",
                       judgment=Judgment.code)
    unrelated = store.append_event(job.job_id, what="unrelated controller heartbeat", evidence={"state": "idle"},
                                   rule="controller", judgment=Judgment.code)
    flag, _ = decision_trace.flag_run(store, job.job_id, description="check a separate concern", actor=OWNER,
                                      now=100.0, run=f"{job.job_id}:a1")
    result = run_investigation.investigate(store, flag["flag_id"], assessor=lambda _packet: {
        "causes": [{"type": "controller_routing", "claim": "heartbeat caused the failure",
                    "evidence": [f"event:{unrelated.seq}"]}]}, now=101.0)
    assert result["report"]["assessment"]["status"] == "insufficient_evidence"


def test_investigation_snapshot_includes_duplicate_flag_notes(steward_home):
    store = Store(steward_home)
    _jid, flag = _flagged(store)
    decision_trace.add_flag_evidence(store, flag["flag_id"], note="owner observed a repeated stall", actor=OWNER,
                                     now=101.0)
    snapshot = run_investigation.evidence_snapshot(store, flag)
    assert snapshot["flag_notes"][0]["note"] == "owner observed a repeated stall"


def test_assessment_requires_captured_citations_and_supported_remedy_uses_existing_review_path_continuous_ownership_recovery_acceptance(steward_home):
    store = Store(steward_home)
    jid, flag = _flagged(store)
    blocker, _ = blockers.raise_blocker(store, jid, kind="worker_parked_unfinished", subject="attempt",
                                        claim="worker is parked", evidence=["event:1"],
                                        origin_actor=ActorRef(Actor.WORKER, "fake"), now=100.0)
    event = store.events(jid)[-1]
    decision_trace.record(store, job_id=jid, actor=ActorRef(Actor.CONTROLLER, "fixture"), stage="proposal",
                          decision="reviewed parked worker evidence", blocker_id=blocker.blocker_id,
                          evidence_refs=[f"event:{event.seq}"], now=100.5)
    assessment_calls = []

    def assess(packet):
        assessment_calls.append(packet)
        return {"assessor": "independent-reviewer:fixture", "causes": [{"type": "controller_routing",
                "claim": "The controller kept a completed check while a steer was pending.",
                "evidence": [f"event:{event.seq}"], "confidence": .8}],
                "remedy": "Reconcile verification state before retrying the steer."}

    result = run_investigation.investigate(store, flag["flag_id"], assessor=assess, now=101.0)
    assert result["report"]["assessment"]["status"] == "assessed"
    handoff = run_investigation.handoff_remedy(store, result["investigation_id"], blocker.blocker_id,
                                                actor=ActorRef(Actor.CONTROLLER, "investigation"), now=102.0)
    assert handoff["accepted"] and handoff["executed"] is False
    assert store.get_job(jid).state == JobState.RUNNING
    assert len(assessment_calls) == 1


def test_flagged_run_investigation_rejects_unsupported_causal_claim_continuous_ownership_recovery_acceptance(steward_home):
    store = Store(steward_home)
    _jid, flag = _flagged(store)
    result = run_investigation.investigate(store, flag["flag_id"], assessor=lambda _packet: {
        "causes": [{"type": "bad_input", "evidence": ["event:999999"], "claim": "unsupported"}],
        "remedy": "change policy"}, now=101.0)
    assessment = result["report"]["assessment"]
    assert assessment["status"] == "insufficient_evidence"
    assert assessment["causes"] == [{"type": "unknown", "evidence": [],
                                      "why": "no supported causal claim cites captured evidence"}]
    assert not assessment.get("remedy")


def test_run_flag_cannot_handoff_to_confirmed_owner_or_stopped_work_continuous_ownership_recovery_acceptance(steward_home):
    store = Store(steward_home)
    jid, flag = _flagged(store)
    event = store.events(jid)[-1]
    result = run_investigation.investigate(store, flag["flag_id"], assessor=lambda _p: {
        "causes": [{"type": "downstream_execution", "evidence": [f"event:{event.seq}"]}],
        "remedy": "review"}, now=101.0)
    owner_blocker, _ = blockers.raise_blocker(store, jid, kind="owner_question", subject="stop",
                                              claim="The owner must choose.", evidence=["question"],
                                              origin_actor=ActorRef(Actor.CONTROLLER, "test"), now=100.0)
    # Owner-only decisions cannot be converted into a tech-lead recovery by an investigation.
    denied = run_investigation.handoff_remedy(store, result["investigation_id"], owner_blocker.blocker_id,
                                               actor=ActorRef(Actor.CONTROLLER, "investigation"), now=102.0)
    assert denied["accepted"] is False
    store.set_state(jid, JobState.NEEDS_HUMAN, what="owner decision pending", evidence={}, rule="test", judgment=Judgment.code)
    store.set_state(jid, JobState.STOPPED, what="owner stopped", evidence={}, rule="test", judgment=Judgment.human)
    assert run_investigation.handoff_remedy(store, result["investigation_id"], owner_blocker.blocker_id,
                                             actor=ActorRef(Actor.CONTROLLER, "investigation"), now=103.0)["accepted"] is False
