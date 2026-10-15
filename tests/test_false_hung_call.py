"""False hung-call escalation (job d3477a2a, 2026-10-01 01:43:55Z): a worker permission was opened, auto-approved and
relayed in the same second; in that tick Jev flagged hung_call (0.87) and routed doctor+permission; the doctor found
the recovery budget spent, escalated NEEDS_HUMAN and the runner closed the live worker's pane. Fakes only."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from steward import approvals as approvals_module
from steward import hung_call, recovery
from steward.heartbeat import Signals
from steward.jev_observer import JevObserver
from steward.models import JobState, Judgment
from steward.runner import Runner
from steward.store import Store
from test_jev_watch import FAKE_KEY, ScriptedTransport, _resp
from test_runner_fabric import _contracted_job, _fake_diagnoser, _FabricFakeWorker, _frame, _git_runner, _obs


class _Notes:
    def __init__(self):
        self.events = []

    def send(self, event):
        self.events.append(event)
        return True


def _kinds(notes):
    return [getattr(e, "kind", None) for e in notes.events]


def _sig(**kw):
    base = dict(lifecycle="working", pid_alive=True, head_sha="a", diff_fingerprint="", last_check_exit=None,
                error_fingerprint="", same_error_count=0, permission_prompt=False, running_process=None,
                since_last_change_s=1.0)
    return Signals(**{**base, **kw})


def _setup(store, mode):
    job = _contracted_job(store)
    req = _frame(3, "permission_request", {"request_id": "r1", "tool": "WebSearch", "input": {"query": "x"}})
    worker = _FabricFakeWorker(script=[_obs(lifecycle="blocked", events=(req,))] + [_obs()] * 8)
    store.kv_set(job.job_id, "supervisor_jev", mode)
    transport = ScriptedTransport([_resp("hung_call", 0.87)])
    jev = JevObserver(transport=transport, enabled=True, api_key_fn=lambda: FAKE_KEY, clock=time.time)
    diag = _fake_diagnoser()
    notes = _Notes()
    runner = Runner(store, lambda ref: worker, _git_runner(), diag, clock=time.time, jev_observer=jev,
                    notifier=notes, activity_fn=lambda s, j: {"turns": []})
    runner.start(job.job_id)
    return job, runner, worker, diag, notes


@pytest.mark.parametrize("mode", ["active", "shadow"])
def test_replay_d3477a2a_no_doctor_no_escalation_pane_untouched(steward_home, mode):
    store = Store(steward_home)
    job, runner, worker, diag, notes = _setup(store, mode)
    store.kv_set(job.job_id, "recoveries_used", recovery.RecoveryPolicy().max_recoveries)  # budget already spent
    runner.tick(job.job_id)  # permission opened + auto-approved
    runner.tick(job.job_id)  # ... and relayed, same second
    (perm,) = [a for a in approvals_module.list_approvals(store, job.job_id) if a["kind"] == "permission"]
    assert perm["status"] == "approved" and perm.get("relayed_at")
    for _ in range(3):  # Jev: hung_call 0.87, enough consecutive flags to pass the streak gate
        runner.jev_watch.step(job.job_id, runner._signals[job.job_id], force=True)
    runner.hung_step(job.job_id, force=True)
    runner.tick(job.job_id)

    assert diag.contexts == []  # no doctor run
    assert store.get_job(job.job_id).state == JobState.RUNNING  # no escalation
    assert "NEEDS_YOU" not in _kinds(notes)
    assert not [e for e in store.events(job.job_id) if e.what in ("jev_escalated", "recovery escalated to human",
                                                                    "hung_call", "permission_stuck")]
    assert worker.close_pane_calls == 0 and worker.stopped is False  # pane untouched, worker not stopped
    held = [e for e in store.events(job.job_id) if e.rule == "jev_flag_unmeasured"]
    assert held and held[0].evidence["choice"] == "hung_call"


def test_permission_relayed_in_the_last_60s_is_never_stuck(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    rec = approvals_module.open_approval(store, job.job_id, "worker permission: Bash x", kind="permission",
                                         worker_request_id="r1")
    approvals_module.decide(store, job.job_id, rec["id"], "approve", decided_by="human")
    approvals_module.mark_relayed(store, job.job_id, rec["id"], ack_expected=True)
    relayed = datetime.fromisoformat(
        approvals_module.list_approvals(store, job.job_id)[0]["relayed_at"].replace("Z", "+00:00")).timestamp()
    store.kv_set(job.job_id, "hung_perm_s", 0)  # even a request older than any threshold
    assert hung_call.pending_permission(store, job.job_id, relayed + 30) is None
    assert hung_call.measured(store, job.job_id, relayed + 30, activity_fn=lambda s, j: {"turns": []}, env={}) == []
    late = hung_call.pending_permission(store, job.job_id, relayed + 120)
    assert late is not None and late["state"] == "unacked"


def test_undecided_permission_over_5_min_is_measured(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    approvals_module.open_approval(store, job.job_id, "worker permission: Bash x", kind="permission")
    now = datetime.now(timezone.utc).timestamp()
    empty = lambda s, j: {"turns": []}  # noqa: E731
    assert hung_call.measured(store, job.job_id, now + 4 * 60, activity_fn=empty, env={}) == []
    (f,) = hung_call.measured(store, job.job_id, now + 6 * 60, activity_fn=empty, env={})
    assert f["kind"] == "permission_stuck"


def _needs_human(runner, job_id, sig):
    action = recovery.Action(kind="needs_human", detail="recovery budget exhausted after diagnosis",
                             rule="recovery_budget_exhausted", judgment=Judgment.code)
    return runner._apply_action(job_id, runner._workers[job_id], action, sig)


@pytest.mark.legacy_ownership_off  # Plan 1 G23: ownership reviews this recovery park before notifying the owner
def test_budget_exhausted_escalation_keeps_a_live_worker_pane(steward_home):
    store = Store(steward_home)
    job, runner, worker, _, notes = _setup(store, "off")
    _needs_human(runner, job.job_id, _sig(pid_alive=True))
    assert store.get_job(job.job_id).state == JobState.NEEDS_HUMAN
    assert "NEEDS_YOU" in _kinds(notes)  # Sam is told
    assert worker.close_pane_calls == 0 and worker.stopped is False  # live worker: pane kept, process not stopped
    assert [e for e in store.events(job.job_id) if e.what == "worker pane kept open"]


@pytest.mark.parametrize("sig", [_sig(pid_alive=False, lifecycle="exited"), _sig(pid_alive=False)])
def test_budget_exhausted_escalation_closes_a_dead_worker_pane(steward_home, sig):
    store = Store(steward_home)
    job, runner, worker, _, _ = _setup(store, "off")
    _needs_human(runner, job.job_id, sig)
    assert store.get_job(job.job_id).state == JobState.NEEDS_HUMAN
    assert worker.close_pane_calls == 1
