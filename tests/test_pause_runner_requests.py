# tests/test_pause_runner_requests.py
"""Spec 2026-10-13 §4.3/§9: every worker pause is ONE tech-lead review request (none for a background wait or a
permission prompt); no Jev ask_stop, no stop timer; unacknowledged steering does not block. Pause=off is unchanged."""
from __future__ import annotations

from pause_support import BASE, NEW, events, pause_facts, pause_runner, set_pause
from test_runner import _Factory
from test_runner_fabric import _StopJev, _contracted_job, _fake_diagnoser, _frame, _obs, _open_check_run_wait, _yield_git

from steward import pause_checkpoint
from steward.contract import load_contract
from steward.models import JobState
from steward.runner import Runner
from steward.steering import Steering
from steward.store import Store


def test_candidate_yield_is_one_review_request_not_worker_finished(steward_home):
    store, job, runner, _, _ = pause_runner(steward_home)
    report = runner.tick(job.job_id)
    assert report.state == "RUNNING" and store.get_job(job.job_id).state == JobState.RUNNING
    (fact,) = pause_facts(store, job.job_id)
    d = fact["data"]
    assert fact["route"] == "outer"
    assert (d["yield_lane"], d["sha"], d["unit_id"], d["next_unit"], d["done_claim"], d["pause_seq"]) == \
        ("CANDIDATE", NEW, "u1", "u2", False, 1)
    assert not [e for e in store.events(job.job_id) if e.rule == "fabric_done_frame"]
    cp = pause_checkpoint.load(store, job.job_id)
    assert cp["awaiting_review"] and cp["pause_seq"] == 1 and cp["last_pause_sha"] == NEW
    assert [u["id"] for u in cp["units_seen"]] == ["u1", "u2"]
    assert len(events(store, job.job_id, "pause_review_requested")) == 1
    runner.tick(job.job_id)
    assert len(pause_facts(store, job.job_id)) == 1            # one pause, one request, however many ticks


def test_stopped_yield_is_a_request_with_no_jev_ask_and_no_stop_timer(steward_home):
    jev = _StopJev({"choice": "stuck", "confidence": 0.99})
    store, job, runner, _, _ = pause_runner(steward_home, git=_yield_git(head=BASE), jev=jev)
    runner.tick(job.job_id)
    (fact,) = pause_facts(store, job.job_id)
    assert fact["data"]["yield_lane"] == "STOPPED" and fact["data"]["failed"] == ["no_new_commit"]
    assert jev.calls == [] and store.kv_get(job.job_id, "stop_fact_due") is None
    doc = store.kv_get(job.job_id, "fleet_arbiter") or {}
    assert not [f for f in doc.get("facts", []) if f["kind"] in ("worker_stopped", "worker_asking")]


def test_asking_yield_is_a_request(steward_home):
    store, job, runner, _, _ = pause_runner(steward_home)
    store.kv_set(job.job_id, "questions", [{"qid": "q1", "status": "open", "blocking": True}])
    runner.tick(job.job_id)
    (fact,) = pause_facts(store, job.job_id)
    assert fact["data"]["yield_lane"] == "ASKING"


def test_background_wait_is_no_request(steward_home):
    store, job, runner, _, _ = pause_runner(steward_home)
    _open_check_run_wait(store, job)
    runner.tick(job.job_id)
    assert pause_facts(store, job.job_id) == [] and not pause_checkpoint.load(store, job.job_id)["awaiting_review"]


def test_permission_prompt_is_no_request(steward_home):
    frames = (_frame(3, "permission_request", {"request_id": "r1", "tool": "Bash", "input": {"command": "ls"}}),)
    store, job, runner, _, _ = pause_runner(steward_home, frames=frames, lifecycle="blocked")
    runner.tick(job.job_id)
    assert pause_facts(store, job.job_id) == [] and not pause_checkpoint.load(store, job.job_id)["awaiting_review"]


def test_unacked_steering_does_not_defer_the_pause(steward_home):
    frames = (_frame(9, "done", {"sha": NEW, "unconsumed_revs": [1], "last_rev": 1}),)
    store, job, runner, _, _ = pause_runner(steward_home, frames=frames)
    Steering(store).record(job.job_id, "nudge", "document the fix", "req-unacked")
    runner.tick(job.job_id)
    (fact,) = pause_facts(store, job.job_id)
    assert "unacked_steering" in fact["data"]["failed"] and fact["data"]["unacked"] == [1]
    assert events(store, job.job_id, "worker done deferred: steering not yet acknowledged") == []


def test_a_pause_during_an_outstanding_review_is_folded(steward_home):
    first = _obs(lifecycle="idle", events=(_frame(9, "done", {"sha": NEW}),))
    second = _obs(lifecycle="idle", events=(_frame(12, "done", {"sha": NEW}),))
    store, job, runner, _, _ = pause_runner(steward_home, scripts=[[first, second]])
    runner.tick(job.job_id)
    runner.tick(job.job_id)
    assert len(pause_facts(store, job.job_id)) == 1
    assert len(events(store, job.job_id, "pause_review_folded")) == 1
    cp = pause_checkpoint.load(store, job.job_id)
    assert cp["pause_seq"] == 2 and cp["last_pause_fact"]["pause_seq"] == 2
    assert [f["kind"] for f in cp["pending_facts"]] == ["pause_folded"]


def test_the_next_pause_clears_a_wait_on_owner(steward_home):
    store, job, runner, _, _ = pause_runner(steward_home)
    pause_checkpoint.update(store, job.job_id, lambda c: c.update(waiting_on="owner"))
    runner.tick(job.job_id)
    assert pause_checkpoint.load(store, job.job_id)["waiting_on"] is None


def test_a_non_fabric_worker_runs_with_pause_off(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    set_pause(store, job.job_id, "unit")
    Runner(store, _Factory(), _yield_git(), _fake_diagnoser()).start(job.job_id)
    assert load_contract(store, job.job_id).pause == "off"
    assert len(events(store, job.job_id, "pause_review_unsupported")) == 1


def _trace(store, job_id):
    whats = [e.what for e in store.events(job_id)]
    steer_rows = [(i["origin"], i["verb"], i["text"]) for i in Steering(store).all_instructions(job_id)]
    keys = sorted(r["key"] for r in store._conn.execute("SELECT key FROM kv WHERE job_id = ?", (job_id,)).fetchall())
    return whats, steer_rows, keys


def test_pause_off_is_unchanged_against_a_contract_stored_before_the_fields(steward_home):
    """Review Focus 5 (mixed fleet): a pause=off job behaves exactly like a job whose stored contract predates the
    fields — same events, steers and kv keys, today's 120 s stop timer, and never a pause record."""
    traces = []
    for name, strip in (("legacy", True), ("off", False)):
        home = steward_home / name
        home.mkdir()
        store, job, runner, _, clock = pause_runner(home, mode="off", git=_yield_git(head=BASE), jev=_StopJev(None))
        if strip:
            raw = store.kv_get(job.job_id, "contract")
            store.kv_set(job.job_id, "contract", {k: v for k, v in raw.items() if k not in ("pause", "units")})
        runner.tick(job.job_id)
        assert store.kv_get(job.job_id, "stop_fact_due") is not None   # today's path: the stop timer runs
        for _ in range(2):
            clock.advance(130)
            runner.tick(job.job_id)
        assert not pause_checkpoint.exists(store, job.job_id) and pause_facts(store, job.job_id) == []
        traces.append(_trace(store, job.job_id))
    assert traces[0] == traces[1]
