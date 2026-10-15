# tests/test_parked_pass.py
"""The control pane's parked pass (spec 2026-10-04 §4): stuck NEEDS_HUMAN jobs get a tech-lead review on the
[parked] cadence; acceptance/question/floor holds, RUNNING and ACCEPTED jobs never do."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy_ownership_off  # Plan 1 Task 8: the ownership sweep replaces this pass when enabled

from steward import questions_delivery as qd
from steward import tech_lead_policy as tpol
from steward.models import JobState, Judgment
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store
from test_runner import _contracted_job


def _git(argv, cwd):
    return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="sha-1\n", stderr="", duration_s=0.0)


class _Lead:
    def __init__(self):
        self.calls = []
        self._state = {}

    def state(self, job_id):
        return self._state.get(job_id, {})

    def on_event(self, job_id, *, sweep=False, reason=""):
        self.calls.append((job_id, sweep, reason))
        return {"status": "skipped"}


def _park(store, marker_rule):
    jid = _contracted_job(store).job_id
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    store.set_state(jid, JobState.WORKER_FINISHED, what="t", evidence={"marker": marker_rule},
                    rule=marker_rule, judgment=Judgment.code)
    store.set_state(jid, JobState.NEEDS_HUMAN, what="acceptance unresolved", evidence={}, rule="acceptance_unresolved",
                    judgment=Judgment.code)
    return jid


def test_only_stuck_parked_jobs_are_reviewed(steward_home):
    store = Store(steward_home)
    stuck = _park(store, "idle_after_final_message")
    done = _park(store, "steward_done_marker")
    running = _contracted_job(store).job_id
    store.set_state(running, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    lead = _Lead()
    out = qd.tick(store, FakeRunner(_git), now=1000.0, job_lead=lead, policy_fn=lambda: tpol.Policy())
    assert [c[0] for c in lead.calls] == [stuck]
    assert lead.calls[0][1:] == (True, "parked_stuck")
    assert out["parked"] == [{"job": stuck, "reason": "due"}]
    assert store.kv_get(stuck, qd.PARKED_AT_KEY) == 1000.0


def test_cadence_is_respected(steward_home):
    store = Store(steward_home)
    stuck = _park(store, "idle_after_final_message")
    lead = _Lead()
    pol = lambda: tpol.Policy(parked_cadence_min=10)
    qd.tick(store, FakeRunner(_git), now=1000.0, job_lead=lead, policy_fn=pol)
    qd.tick(store, FakeRunner(_git), now=1000.0 + 9 * 60, job_lead=lead, policy_fn=pol)
    assert len(lead.calls) == 1
    qd.tick(store, FakeRunner(_git), now=1000.0 + 10 * 60, job_lead=lead, policy_fn=pol)
    assert len(lead.calls) == 2


def test_in_flight_review_is_polled_every_tick_without_a_new_slot(steward_home):
    """Review focus 4."""
    store = Store(steward_home)
    stuck = _park(store, "idle_after_final_message")
    lead = _Lead()
    pol = lambda: tpol.Policy(parked_cadence_min=10)
    qd.tick(store, FakeRunner(_git), now=1000.0, job_lead=lead, policy_fn=pol)
    lead._state[stuck] = {"in_flight": {"lead": "astra-tech-lead", "started": 1000.0}}
    qd.tick(store, FakeRunner(_git), now=1005.0, job_lead=lead, policy_fn=pol)
    qd.tick(store, FakeRunner(_git), now=1010.0, job_lead=lead, policy_fn=pol)
    assert len(lead.calls) == 3
    assert store.kv_get(stuck, qd.PARKED_AT_KEY) == 1000.0  # polling did not move the cadence slot


def test_rework_pending_and_no_lead_are_skipped(steward_home):
    from steward import sendback
    store = Store(steward_home)
    stuck = _park(store, "idle_after_final_message")
    store.kv_set(stuck, sendback.REWORK_KEY, {"rev": 1, "text": "x", "goal_append": False, "by": "cli"})
    lead = _Lead()
    qd.tick(store, FakeRunner(_git), now=1000.0, job_lead=lead, policy_fn=lambda: tpol.Policy())
    assert not lead.calls
    out = qd.tick(store, FakeRunner(_git), now=1000.0)  # no job_lead wired: the pass is a no-op
    assert out["parked"] == []


def test_a_raising_lead_does_not_stop_other_jobs(steward_home):
    store = Store(steward_home)
    a = _park(store, "idle_after_final_message")
    b = _park(store, "idle_after_final_message")

    class Boom(_Lead):
        def on_event(self, job_id, **kw):
            super().on_event(job_id, **kw)
            if job_id == a:
                raise RuntimeError("boom")
            return {"status": "skipped"}
    lead = Boom()
    out = qd.tick(store, FakeRunner(_git), now=1000.0, job_lead=lead, policy_fn=lambda: tpol.Policy())
    assert {c[0] for c in lead.calls} == {a, b}
    assert any(e["job"] == a for e in out["errors"])


def test_control_lead_is_none_without_a_roster(steward_home, tmp_path, monkeypatch):
    from steward import cli
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "no-config"))
    assert cli._control_lead(Store(steward_home), {}) is None
