"""Plan 3 Task 8: exhausted recovery rounds ask for an independent strategy review, never an owner by default."""
from __future__ import annotations

import pytest
from test_closeout import _finished_job
from test_runner import _contracted_job, _Factory, _fake_diagnoser, _git_runner, _Clock
from test_stability import ScriptedRunner, _done_obs, _finish_job, _runner

from steward import attempts, recovery
from steward.closeout import closeout
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.heartbeat import Class
from steward.models import JobState, Judgment
from steward.recovery import RecoveryPolicy
from steward.runner import Runner
from steward.store import Store
from steward.verify import Verification
from steward.worker import WorkerObservation


class Sent:
    def __init__(self):
        self.events = []

    def send(self, event):
        self.events.append(event)
        return True


@pytest.fixture
def strategy_calls(monkeypatch):
    calls = []

    def fake(store, job_id, **kw):
        calls.append((job_id, kw))
        return None

    monkeypatch.setattr("steward.ownership.request_strategy_review", fake)
    return calls


def test_recovery_rounds_trigger_independent_diagnosis_not_owner(steward_home, strategy_calls):
    store = Store(steward_home)
    job = _contracted_job(store)
    dead = WorkerObservation(lifecycle="exited", output_tail="gone", pid_alive=False, ts="t")
    sent = Sent()
    runner = Runner(store, _Factory(scripts=[[dead], [dead], [dead]]), _git_runner(), _fake_diagnoser(),
                    clock=_Clock(), policy=RecoveryPolicy(max_recoveries=2), notifier=sent)
    runner.start(job.job_id)
    for _ in range(3):
        report = runner.tick(job.job_id)
    assert report.state == "NEEDS_HUMAN"
    assert [(c[0], c[1]["signature"]) for c in strategy_calls] == [(job.job_id, "recovery:worker")]
    tried = attempts.for_issue(store, job.job_id, recovery.ISSUE)
    assert [a.outcome for a in tried] == ["failed", "failed"]
    assert all(a.hypothesis and a.change and a.success_observation for a in tried)
    assert "NEEDS_YOU" not in [e.kind for e in sent.events]
    ev = [e for e in store.events(job.job_id) if e.what == "recovery_strategy_review_requested"]
    assert ev and ev[0].evidence["rounds"] == 2


def test_progress_resets_rounds(steward_home, strategy_calls):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    recovery.decide(store, job.job_id, Class.dead, None, context={}, now=1.0)
    recovery.decide(store, job.job_id, Class.progressing, None, context={}, now=2.0)
    assert store.kv_get(job.job_id, "recoveries_used") == 0
    assert attempts.unproductive_rounds(store, job.job_id, recovery.ISSUE) == 0
    assert recovery.decide(store, job.job_id, Class.dead, None, context={}, now=3.0).kind == "replace"
    assert recovery.decide(store, job.job_id, Class.dead, None, context={}, now=4.0).kind == "replace"
    assert strategy_calls == []


@pytest.mark.legacy_ownership_off
def test_legacy_budget_never_resets_on_progress(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    recovery.decide(store, job.job_id, Class.dead, None, context={})
    recovery.decide(store, job.job_id, Class.progressing, None, context={})
    assert store.kv_get(job.job_id, "recoveries_used") == 1


def test_model_needs_human_verdict_is_a_claim_not_an_owner_fact(steward_home, strategy_calls):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    diagnoser = FakeDiagnoser(Diagnosis(verdict="needs_human", guidance="", hypothesis="it needs a human"))
    action = recovery.decide(store, job.job_id, Class.suspect_stall, diagnoser, context={}, now=1.0)
    assert action.kind == "needs_human" and action.rule == "diagnosis_verdict_needs_human"
    assert strategy_calls == []


def test_no_remaining_attempts_notice_when_rounds_are_triggers(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    sent = Sent()
    runner = Runner(store, _Factory(scripts=[[]]), _git_runner(), _fake_diagnoser(), notifier=sent)
    runner.start(job.job_id)
    sent.events.clear()
    store.kv_set(job.job_id, "recoveries_used", 1)
    runner._notify_budget_if_low(job.job_id)
    assert [e.kind for e in sent.events] == []


def test_finish_line_exhaustion_requests_strategy_review(steward_home, strategy_calls):
    store = Store(steward_home)
    job = _finish_job(store)
    sr = ScriptedRunner(lambda line: (1, "FAIL: still broken") if "check_finish" in line else (0, ""))
    runner = _runner(store, sr, [[_done_obs()] * 6])
    runner.start(job.job_id)
    for _ in range(4):
        last = runner.tick(job.job_id)
    assert last.state == "NEEDS_HUMAN"
    (call,) = strategy_calls
    assert call[1]["signature"].startswith("finish_line:")
    tried = attempts.for_issue(store, job.job_id, call[1]["signature"])
    assert len(tried) == 3 and [a.outcome for a in tried] == ["failed"] * 3
    assert "FAIL: still broken" in tried[0].hypothesis


def test_verify_rounds_exhausted_requests_strategy_review(steward_home, strategy_calls):
    store = Store(steward_home)
    job, contract = _finished_job(store)
    for _ in range(2):
        closeout(store, job.job_id, Verification(commit_sha="s", dirty=True), [], contract)
        store.set_state(job.job_id, JobState.WORKER_FINISHED, what="retry", evidence={}, rule="r",
                        judgment=Judgment.code)
    assert closeout(store, job.job_id, Verification(commit_sha="s", dirty=True), [], contract) == JobState.NEEDS_HUMAN
    assert [c[1]["signature"] for c in strategy_calls] == ["verification:rounds"]
    assert len(attempts.for_issue(store, job.job_id, "verification:rounds")) == 2


def test_checks_passing_marks_verification_attempts_progressed(steward_home):
    store = Store(steward_home)
    job, contract = _finished_job(store)
    closeout(store, job.job_id, Verification(commit_sha="s", dirty=True), [], contract)
    store.set_state(job.job_id, JobState.WORKER_FINISHED, what="retry", evidence={}, rule="r", judgment=Judgment.code)
    closeout(store, job.job_id, Verification(commit_sha="s2"), [], contract)
    assert [a.outcome for a in attempts.for_issue(store, job.job_id, "verification:rounds")] == ["progressed"]
