import pytest

from steward import exceptions
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.heartbeat import Class
from steward.models import Judgment
from steward.recovery import Action, RecoveryPolicy, decide
from steward.store import Store


def _job(store):
    return store.create_job("goal", "/repo")


# -- dead -----------------------------------------------------------------


def test_dead_replaces_worker_without_consulting_diagnoser(steward_home):
    store = Store(steward_home)
    job = _job(store)
    diagnoser = FakeDiagnoser(Diagnosis(verdict="needs_human", guidance="", hypothesis="unused"))

    action = decide(store, job.job_id, Class.dead, diagnoser, context={})

    assert action.kind == "replace"
    assert action.judgment == Judgment.code
    assert action.rule == "worker_exited"
    assert diagnoser.contexts == []  # dead never calls the diagnoser
    assert store.kv_get(job.job_id, "recoveries_used") == 1


def test_dead_after_budget_exhausted_yields_needs_human_with_evidence(steward_home):
    store = Store(steward_home)
    job = _job(store)
    diagnoser = FakeDiagnoser(Diagnosis(verdict="needs_human", guidance="", hypothesis=""))
    policy = RecoveryPolicy(max_recoveries=2)

    decide(store, job.job_id, Class.dead, diagnoser, context={}, policy=policy)
    decide(store, job.job_id, Class.dead, diagnoser, context={}, policy=policy)
    third = decide(
        store, job.job_id, Class.dead, diagnoser,
        context={"signals": {"a": 1}, "handoff": {"h": "x"}}, policy=policy,
    )

    assert third.kind == "needs_human"
    assert store.kv_get(job.job_id, "recoveries_used") == 2
    events = store.events(job.job_id)
    escalation = [e for e in events if e.what == "recovery escalated to human"]
    assert len(escalation) == 1
    assert escalation[0].evidence["signals"] == {"a": 1}
    assert escalation[0].evidence["handoff"] == {"h": "x"}


# -- suspect_stall ---------------------------------------------------------


def test_suspect_stall_produces_exactly_one_diagnosis_per_episode(steward_home):
    store = Store(steward_home)
    job = _job(store)
    diagnoser = FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="add timeout", hypothesis="hang"))

    first = decide(store, job.job_id, Class.suspect_stall, diagnoser, context={})
    second = decide(store, job.job_id, Class.suspect_stall, diagnoser, context={})

    assert first.kind == "guide"
    assert first.detail == "add timeout"
    assert second.kind == "none"
    assert len(diagnoser.contexts) == 1  # only one diagnosis for this episode
    assert store.kv_get(job.job_id, "diagnoses_used") == 1


def _yield(store, job, failed, sha="abc", said="all done"):
    store.append_event(job.job_id, what="yield_classified",
                       evidence={"lane": "STOPPED", "sha": sha, "failed_preconditions": failed, "last_said": said},
                       rule="yield_classification", judgment=Judgment.code)


def test_suspect_stall_skips_diagnosis_when_last_yield_claims_done(steward_home):
    store = Store(steward_home)
    job = _job(store)
    _yield(store, job, ["same_as_last_ruled"])
    diagnoser = FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="redo step 1", hypothesis="h"))

    action = decide(store, job.job_id, Class.suspect_stall, diagnoser, context={})

    assert action.kind == "none" and action.rule == "ruling_pending"
    assert diagnoser.contexts == []
    assert not store.kv_get(job.job_id, "stall_episode_diagnosed", default=False)  # no episode slot spent
    _yield(store, job, ["dirty_worktree", "same_as_last_ruled"])  # scratch at the ruled SHA is still awaiting it
    assert decide(store, job.job_id, Class.suspect_stall, diagnoser, context={}).rule == "ruling_pending"


def test_suspect_stall_skips_diagnosis_while_a_tl_send_back_is_unacknowledged(steward_home):
    from steward.steering import Steering

    store = Store(steward_home)
    job = _job(store)
    _yield(store, job, ["dirty_worktree"])
    Steering(store).record(job.job_id, "focus", "fix the flaky test", origin="tech_lead")
    store.append_event(job.job_id, what="sent_back", evidence={"steering_rev": 1, "by": "tech_lead"},
                       rule="tech_lead_send_back", judgment=Judgment.model_informed_code_authorized)
    diagnoser = FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="redo step 1", hypothesis="h"))

    held = decide(store, job.job_id, Class.suspect_stall, diagnoser, context={})
    assert held.rule == "ruling_pending" and diagnoser.contexts == []

    Steering(store).scan_acks(job.job_id, "STEWARD-ACK rev=1")
    assert Steering(store).all_instructions(job.job_id)[0]["status"] == "acknowledged"
    assert decide(store, job.job_id, Class.suspect_stall, diagnoser, context={}).kind == "guide"


def test_a_stopped_yield_at_an_unruled_sha_is_still_diagnosed(steward_home):
    store = Store(steward_home)
    job = _job(store)
    _yield(store, job, ["dirty_worktree", "no_new_commit"])
    diagnoser = FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="commit it", hypothesis="h"))
    assert decide(store, job.job_id, Class.suspect_stall, diagnoser, context={}).kind == "guide"


def test_episode_resets_on_progressing_allowing_a_new_diagnosis(steward_home):
    store = Store(steward_home)
    job = _job(store)
    diagnoser = FakeDiagnoser(
        [
            Diagnosis(verdict="retry_with_guidance", guidance="g1", hypothesis="h1"),
            Diagnosis(verdict="retry_with_guidance", guidance="g2", hypothesis="h2"),
        ]
    )

    decide(store, job.job_id, Class.suspect_stall, diagnoser, context={})
    decide(store, job.job_id, Class.progressing, diagnoser, context={})
    second_stall = decide(store, job.job_id, Class.suspect_stall, diagnoser, context={})

    assert second_stall.kind == "guide"
    assert second_stall.detail == "g2"
    assert len(diagnoser.contexts) == 2


def test_suspect_stall_diagnosis_needs_human_verdict_escalates(steward_home):
    store = Store(steward_home)
    job = _job(store)
    diagnoser = FakeDiagnoser(Diagnosis(verdict="needs_human", guidance="", hypothesis="unrecoverable"))

    action = decide(store, job.job_id, Class.suspect_stall, diagnoser, context={})

    assert action.kind == "needs_human"
    assert action.diagnosis.hypothesis == "unrecoverable"
    # needs_human from a diagnosis does not itself spend a recovery
    assert store.kv_get(job.job_id, "recoveries_used", default=0) == 0


def test_suspect_stall_replace_worker_verdict_spends_a_recovery(steward_home):
    store = Store(steward_home)
    job = _job(store)
    diagnoser = FakeDiagnoser(Diagnosis(verdict="replace_worker", guidance="restart clean", hypothesis="wedged"))

    action = decide(store, job.job_id, Class.suspect_stall, diagnoser, context={})

    assert action.kind == "replace"
    assert action.judgment.value == "model_informed_code_authorized"
    assert store.kv_get(job.job_id, "recoveries_used") == 1


@pytest.mark.legacy_ownership_off
def test_suspect_stall_after_budget_exhausted_escalates_even_with_good_verdict(steward_home):
    store = Store(steward_home)
    job = _job(store)
    policy = RecoveryPolicy(max_recoveries=1)
    diagnoser = FakeDiagnoser(
        [
            Diagnosis(verdict="retry_with_guidance", guidance="g1", hypothesis="h1"),
            Diagnosis(verdict="retry_with_guidance", guidance="g2", hypothesis="h2"),
        ]
    )

    decide(store, job.job_id, Class.suspect_stall, diagnoser, context={}, policy=policy)
    decide(store, job.job_id, Class.progressing, diagnoser, context={}, policy=policy)
    second = decide(store, job.job_id, Class.suspect_stall, diagnoser, context={}, policy=policy)

    assert second.kind == "needs_human"


# -- budget persistence + no-op classes ------------------------------------


def test_recoveries_used_persists_across_a_brand_new_store_instance(steward_home):
    store_a = Store(steward_home)
    job = store_a.create_job("goal", "/repo")
    diagnoser = FakeDiagnoser(Diagnosis(verdict="needs_human", guidance="", hypothesis=""))
    decide(store_a, job.job_id, Class.dead, diagnoser, context={})

    store_b = Store(steward_home)
    assert store_b.kv_get(job.job_id, "recoveries_used") == 1


def test_progressing_waiting_and_quiet_known_process_never_call_diagnoser(steward_home):
    store = Store(steward_home)
    job = _job(store)
    diagnoser = FakeDiagnoser(Diagnosis(verdict="needs_human", guidance="", hypothesis=""))

    for cls in (Class.progressing, Class.waiting, Class.quiet_known_process, Class.idle):
        action = decide(store, job.job_id, cls, diagnoser, context={})
        assert action.kind == "none"

    assert diagnoser.contexts == []


# -- supervisor-model exception gate (12-02, D10) ---------------------------


def test_suspect_stall_uses_supervisor_model_exception_and_records_it(steward_home):
    store = Store(steward_home)
    job = _job(store)
    diagnoser = FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="g", hypothesis="h"))

    decide(store, job.job_id, Class.suspect_stall, diagnoser, context={})

    used = [e for e in store.events(job.job_id) if e.what == "exception_used" and e.evidence.get("name") == "supervisor-model"]
    assert len(used) == 1
    assert used[0].evidence["role"] == "sol"


def test_suspect_stall_unregistered_supervisor_model_fails_closed_to_needs_human(steward_home, tmp_path, monkeypatch):
    store = Store(steward_home)
    job = _job(store)
    registry_path = tmp_path / "no-supervisor-model.toml"
    registry_path.write_text('[git]\ndirection = "out"\npurpose = "x"\naudit = "event"\n')
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(registry_path))
    diagnoser = FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="g", hypothesis="h"))

    action = decide(store, job.job_id, Class.suspect_stall, diagnoser, context={})

    assert action.kind == "needs_human"
    assert action.diagnosis.hypothesis == "supervisor-model path unregistered"
    assert diagnoser.contexts == []  # never actually called the diagnoser
    violations = [e for e in store.events(job.job_id) if e.what == "exception_violation"]
    assert len(violations) == 1
    assert violations[0].evidence["name"] == "supervisor-model"


def test_action_is_a_frozen_dataclass_with_expected_fields():
    action = Action(kind="none", detail="d", judgment=Judgment.code, rule="r")
    assert action.kind == "none"
    assert action.diagnosis is None
