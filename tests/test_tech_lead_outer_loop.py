"""SUP-16 / PRD D-07 + v2.7 SRD-12: the adaptive outer loop, for every job.

A code snapshot (no model call) every 30 min while the job is active or flagged, every 120 min when quiet and healthy; a
review starts only on a fact the snapshot derives (e.g. `no_progress_for`); a routed fact still wakes the lead
immediately (injectable clock). The fleet tech lead's cadence is the same adaptive one."""

from __future__ import annotations

import pytest

from steward import approvals, code_facts
from steward import fleet_tech_lead as tl
from steward import tech_lead_policy as tpol
from steward.models import JobState, Judgment
from steward.store import Store
from test_runner import _contracted_job
from test_tech_lead_no_clog import Clock, Gate, make_lead, send_back_proposal, wait_done


@pytest.fixture
def env(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store, "fix it")
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    (store.home / "worktrees" / job.job_id).mkdir(parents=True)
    return store, job.job_id


def answers(n=10):
    return {"astra-tech-lead": [send_back_proposal() for _ in range(n)], "fable-tech-lead": []}


def settle(lead, jid):
    wait_done(lead, jid)
    return lead.on_event(jid)


def test_quiet_and_healthy_job_is_snapshotted_every_120_minutes_without_a_model_call(env):
    store, jid = env
    clock = Clock()
    gate = Gate(answers())
    lead, _ = make_lead(store, gate, clock)
    store.kv_set(jid, "last_class", "idle")
    assert lead.on_event(jid)["status"] == "skipped"  # the loop starts its timer
    clock.t += 119 * 60
    store.append_event(jid, what="verification recorded", evidence={}, rule="r", judgment=Judgment.code)  # it did work
    assert lead.on_event(jid)["status"] == "skipped"
    assert lead.outer_interval_s(jid) == 120 * 60
    clock.t += 2 * 60
    res = lead.on_event(jid)
    assert res["status"] == "skipped" and res["reason"] == "sweep_no_facts"
    assert not gate.calls  # a sweep with no facts costs nothing
    sweep = [e for e in store.events(jid) if e.what == "tech_lead_sweep"]
    assert len(sweep) == 1 and sweep[0].evidence["model_calls"] == 0 and sweep[0].evidence["facts"] == []
    assert not [e for e in store.events(jid) if e.what == "tech_lead_review_started"]


def test_a_job_with_no_progress_for_a_cadence_gets_a_review_from_its_snapshot_fact(env):
    store, jid = env
    clock = Clock()
    lead, _ = make_lead(store, Gate(answers()), clock)
    store.kv_set(jid, "last_class", "idle")
    lead.on_event(jid)
    clock.t += 121 * 60  # nothing happened for longer than the 120-minute quiet cadence
    assert lead.on_event(jid)["status"] == "review_started"
    started = [e for e in store.events(jid) if e.what == "tech_lead_review_started"][-1]
    assert started.evidence["facts"] == ["no_progress_for"]
    settle(lead, jid)


def test_active_job_is_snapshotted_every_30_minutes(env):
    store, jid = env
    clock = Clock()
    lead, _ = make_lead(store, Gate(answers()), clock)
    store.kv_set(jid, "last_class", "progressing")
    lead.on_event(jid)
    assert lead.outer_interval_s(jid) == 30 * 60
    clock.t += 29 * 60
    assert lead.on_event(jid)["status"] == "skipped"
    assert not [e for e in store.events(jid) if e.what == "tech_lead_sweep"]
    clock.t += 61
    lead.on_event(jid)
    assert len([e for e in store.events(jid) if e.what == "tech_lead_sweep"]) == 1
    settle(lead, jid)


def test_flagged_job_is_snapshotted_every_30_minutes(env):
    store, jid = env
    clock = Clock()
    lead, _ = make_lead(store, Gate(answers()), clock)
    store.kv_set(jid, "last_class", "idle")
    approvals.open_approval(store, jid, "worker permission: Write(x)", kind="permission")  # flagged
    assert lead.outer_interval_s(jid) == 30 * 60


def test_routed_facts_wake_it_immediately(env):
    store, jid = env
    clock = Clock()
    lead, arb = make_lead(store, Gate(answers()), clock)
    store.kv_set(jid, "last_class", "idle")
    lead.on_event(jid)
    arb.submit(jid, code_facts.SOURCE, "finish_check_unverified", key="a")
    assert lead.on_event(jid)["status"] == "review_started"  # no waiting for the 60-minute timer


def test_a_review_resets_the_timer_and_policy_cadence_is_used(env):
    store, jid = env
    clock = Clock()
    gate = Gate(answers())
    lead, arb = make_lead(store, gate, clock)
    lead.policy_fn = lambda: tpol.Policy(cadence_active_min=5, cadence_quiet_min=30)
    store.kv_set(jid, "last_class", "idle")
    lead.on_event(jid)
    clock.t += 20 * 60
    arb.submit(jid, code_facts.SOURCE, "finish_check_unverified", key="a")
    lead.on_event(jid)
    settle(lead, jid)
    # the review acted, so the job is flagged for the next hour: the 5-minute (active) cadence, timed from the review
    assert lead.outer_interval_s(jid) == 5 * 60
    clock.t += 4 * 60
    assert lead.on_event(jid)["status"] == "skipped"
    assert not [e for e in store.events(jid) if e.what == "tech_lead_sweep"]
    clock.t += 61  # 5 minutes after the review: the snapshot runs; the job acted a moment ago, so no fact, no model call
    res = lead.on_event(jid)
    assert res["status"] == "skipped" and res["reason"] == "sweep_no_facts" and len(gate.calls) == 1
    clock.t += 61 * 60  # an hour without an action, idle: back to the 30-minute quiet cadence
    assert lead.outer_interval_s(jid) == 30 * 60


def test_runner_drives_the_outer_loop_every_tick(steward_home):
    from test_stability import ScriptedRunner, _runner

    from steward.runner import wire_job_lead
    from steward.worker import WorkerObservation
    from test_tech_lead_no_clog import ASTRA, FABLE

    store = Store(steward_home)
    job = _contracted_job(store, "fix it")
    (store.home / "worktrees" / job.job_id).mkdir(parents=True)
    clock = Clock()
    idle = WorkerObservation(lifecycle="idle", output_tail="x", pid_alive=True, ts="t")
    runner = _runner(store, ScriptedRunner(), [[idle] * 20], clock=clock)
    gate = Gate(answers())
    wire_job_lead(runner, [ASTRA, FABLE], invoke=gate, policy_fn=lambda: tpol.Policy())
    runner.job_lead.available = lambda e: None
    runner.start(job.job_id)
    runner.tick(job.job_id)
    clock.t += 121 * 60  # past the 120-minute quiet cadence: the runner's tick drives the snapshot
    runner.tick(job.job_id)
    sweeps = [e for e in store.events(job.job_id) if e.what == "tech_lead_sweep"]
    assert sweeps and sweeps[-1].evidence["model_calls"] == 0
    assert not gate.calls  # the tick itself was activity: the snapshot derived no fact, so no model call


def test_fleet_cadence_is_adaptive_not_thirty_minutes(steward_home, tmp_path):
    from test_fleet_tech_lead import JOB, TL

    w = TL(tmp_path, steward_home)
    sup = tl.TechLeadSupervisor(w.store, w.ctl, None, clock=w.clock, facts=w.ctl.arbiter)
    assert sup.cadence_s(JOB) == 120 * 60  # no live lane: quiet
    w.admit()
    assert sup.cadence_s(JOB) == 30 * 60  # live lanes: active
