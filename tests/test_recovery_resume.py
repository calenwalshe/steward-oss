"""Approving "recovery budget exhausted after diagnosis" resumes the job (seen live on d22d6633, 2026-10-02: it left
the job in NEEDS_HUMAN, steers were recorded but never delivered, and only a send-back got it out). Fakes only."""

from __future__ import annotations

from steward import approvals as approvals_module
from steward import cli, heartbeat, sendback
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.recovery import RecoveryPolicy
from steward.runner import Runner
from steward.steering import Steering
from steward.store import Store
from test_runner_fabric import _Clock, _contracted_job, _FabricFakeWorker, _git_runner, _obs


def _exhausted(steward_home, monkeypatch):
    """Drive a fabric job through bounded recovery until it escalates with the recovery-budget approval."""
    monkeypatch.setattr(heartbeat, "classify", lambda sig, policy: heartbeat.Class.suspect_stall)
    store = Store(steward_home)
    job = _contracted_job(store)
    worker = _FabricFakeWorker(script=[_obs()] * 10)
    diagnoser = FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="try again", hypothesis="h"))
    runner = Runner(store, lambda ref: worker, _git_runner(), diagnoser, clock=_Clock(),
                    policy=RecoveryPolicy(max_recoveries=0))
    runner.start(job.job_id)
    report = runner.tick(job.job_id)
    assert report.state == "NEEDS_HUMAN"
    (rec,) = [a for a in approvals_module.list_approvals(store, job.job_id, status="pending")]
    assert rec["question"] == "recovery budget exhausted after diagnosis"
    assert rec["recovery_rule"] == approvals_module.RECOVERY_RULE
    return store, job, rec


def test_approving_returns_the_job_to_running_with_a_fresh_budget(steward_home, monkeypatch):
    store, job, rec = _exhausted(steward_home, monkeypatch)
    store.kv_set(job.job_id, "recoveries_used", 2)
    receipt = approvals_module.decide(store, job.job_id, rec["id"], "approve")
    assert "job resumed" in receipt.detail
    assert store.get_job(job.job_id).state.value == "RUNNING"
    assert store.kv_get(job.job_id, "recoveries_used") == 0
    assert store.kv_get(job.job_id, "stall_episode_diagnosed") is False
    assert store.kv_get(job.job_id, approvals_module.RESUME_RELAUNCH_KEY) is True
    ev = [e for e in store.events(job.job_id) if e.rule == "recovery_approval_resume"]
    assert ev and ev[0].evidence["reattach"] is True


def test_denying_keeps_todays_behaviour(steward_home, monkeypatch):
    store, job, rec = _exhausted(steward_home, monkeypatch)
    receipt = approvals_module.decide(store, job.job_id, rec["id"], "deny")
    assert "resumed" not in receipt.detail
    assert store.get_job(job.job_id).state.value == "NEEDS_HUMAN"
    assert store.kv_get(job.job_id, approvals_module.RESUME_RELAUNCH_KEY) is None


def test_other_approvals_are_unchanged(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    rec = approvals_module.open_approval(store, job.job_id, "watcher coverage: please look")
    assert "resumed" not in approvals_module.decide(store, job.job_id, rec["id"], "approve").detail
    assert store.get_job(job.job_id).state.value == "CONTRACTED"


def test_queued_steers_are_delivered_by_the_resumed_runner(steward_home, monkeypatch):
    store, job, rec = _exhausted(steward_home, monkeypatch)
    Steering(store).record(job.job_id, "nudge", "the GPU run is done; collect the results", origin="cli")
    assert Steering(store).pending(job.job_id)  # recorded while NEEDS_HUMAN, not delivered
    approvals_module.decide(store, job.job_id, rec["id"], "approve")
    monkeypatch.setattr(heartbeat, "classify", lambda sig, policy: heartbeat.Class.progressing)
    worker = _FabricFakeWorker(script=[_obs()] * 3)
    runner = Runner(Store(steward_home), lambda ref: worker, _git_runner(),
                    FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="g", hypothesis="h")),
                    clock=_Clock())
    assert runner.resume_all([job.job_id]) == [job.job_id]  # the live attempt is reattached
    report = runner.tick(job.job_id)
    assert report.state == "RUNNING"
    assert worker.delivered == ["[rev=1] the GPU run is done; collect the results"]


def test_no_live_attempt_means_a_fresh_attempt_with_the_steers_in_its_brief(steward_home, monkeypatch):
    store, job, rec = _exhausted(steward_home, monkeypatch)
    attempts = store.kv_get(job.job_id, "attempts")
    for a in attempts:
        a.update(status="stopped", confirmed_stopped=True)
    store.kv_set(job.job_id, "attempts", attempts)
    Steering(store).record(job.job_id, "focus", "finish the parser first", origin="cli")
    approvals_module.decide(store, job.job_id, rec["id"], "approve")
    assert sendback.rework_pending(store, job.job_id)
    worker = _FabricFakeWorker(script=[_obs()] * 3)
    runner = Runner(store, lambda ref: worker, _git_runner(),
                    FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="g", hypothesis="h")),
                    clock=_Clock())
    runner.start_rework(job.job_id)
    assert "finish the parser first" in worker.started_brief
    assert store.get_job(job.job_id).state.value == "RUNNING"


def test_control_pane_relaunches_the_runner_after_an_approved_recovery(steward_home, monkeypatch):
    store, job, rec = _exhausted(steward_home, monkeypatch)
    store.kv_set(job.job_id, "launch_args", {"workspace": "w3T", "worker": "fabric", "kind": "claude",
                                             "agent_args": [], "interval": 15.0, "sock": None, "keep_pane": False})
    launched = []
    from steward import launch

    monkeypatch.setattr(launch, "launch_tab", lambda runner_exec, j, **kw: launched.append((j.job_id, kw)) or (
        "w3T:t9", "w3T:p9"))
    handlers = cli._build_control_handlers(store)
    result = handlers["approve"]({"op": "approve", "args": {"job_id": job.job_id, "approval_id": rec["id"]},
                                  "request_id": "req-1"})
    assert result["accepted"] and "runner relaunched" in result["detail"]
    assert launched and launched[0][0] == job.job_id and launched[0][1]["workspace"] == "w3T"
    assert store.kv_get(job.job_id, approvals_module.RESUME_RELAUNCH_KEY) is None
    assert store.get_job(job.job_id).state.value == "RUNNING"
