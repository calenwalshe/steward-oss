import time

import pytest

from steward import liveness, supervision_allowance as sa
from steward.models import JobState, Judgment
from steward.store import Store
from test_runner import _contracted_job
from test_tech_lead_no_clog import Clock, Gate, make_lead


@pytest.fixture(autouse=True)
def _fresh_cache():
    sa.reset_cache()
    yield
    sa.reset_cache()


def test_idle_reason(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r"); now = time.time()
    assert sa.idle_reason(store, j.job_id, now=now) is None            # no record: not idle
    liveness.record_runner(store, j.job_id, turn_open=False, progress_ts=now - 3600, pid=1)
    assert sa.idle_reason(store, j.job_id, now=now) == "idle"
    liveness.record_runner(store, j.job_id, turn_open=True, progress_ts=now - 3600, pid=1)
    assert sa.idle_reason(store, j.job_id, now=now) is None


def test_recent_progress_is_not_idle(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r"); now = time.time()
    liveness.record_runner(store, j.job_id, turn_open=False, progress_ts=now - 60, pid=1)
    assert sa.idle_reason(store, j.job_id, now=now) is None


def test_open_turn_clears_the_paused_marker(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r")
    store.kv_set(j.job_id, "tl_idle_paused_at", 5.0)
    liveness.record_runner(store, j.job_id, turn_open=False, progress_ts=1.0, pid=1)
    assert store.kv_get(j.job_id, "tl_idle_paused_at") == 5.0
    liveness.record_runner(store, j.job_id, turn_open=True, progress_ts=1.0, pid=1)
    assert store.kv_get(j.job_id, "tl_idle_paused_at") is None


def test_defer_review_pauses_an_idle_job_once(steward_home):
    from steward import code_facts

    store = Store(steward_home)
    job = _contracted_job(store, "fix it. Finish line: `pytest -q tests/test_a.py`")
    jid = job.job_id
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    (store.home / "worktrees" / jid).mkdir(parents=True)
    clock = Clock(time.time())
    lead, arb = make_lead(store, Gate({}), clock)
    liveness.record_runner(store, jid, turn_open=False, progress_ts=time.time() - 3600, pid=1)
    arb.submit(jid, code_facts.SOURCE, "finish_check_unverified", key="a")
    r1 = lead.on_event(jid)
    assert r1 == {"status": "deferred", "reason": "idle"}
    assert lead.state(jid).get("retry_at") is None and lead.state(jid).get("pending_facts")
    r2 = lead._defer_review(jid, [{"kind": "x"}], "t2", ())
    assert r2 == {"status": "deferred", "reason": "idle"}
    assert len([e for e in store.events(jid) if e.what == "tech_lead_idle_paused"]) == 1
    assert not [e for e in store.events(jid) if e.what == "tech_lead_review_deferred"]
    # a worker turn lifts the pause and re-arms the once-per-episode guard
    liveness.record_runner(store, jid, turn_open=True, progress_ts=time.time(), pid=1)
    assert store.kv_get(jid, "tl_idle_paused_at") is None


def test_synthetic_clock_progress_is_never_idle(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r")
    liveness.record_runner(store, j.job_id, turn_open=False, progress_ts=1001.0, pid=1)
    assert sa.idle_reason(store, j.job_id, now=time.time()) is None


def _idle_job_at_ruled_sha(store, sha="c0ffee"):
    job = _contracted_job(store, "fix it. Finish line: `pytest -q tests/test_a.py`")
    jid = job.job_id
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    (store.home / "worktrees" / jid).mkdir(parents=True)
    store.kv_set(jid, "last_ruled_sha", sha)
    liveness.record_runner(store, jid, turn_open=False, progress_ts=time.time() - 3600, pid=1)
    return jid


def test_idle_guard_does_not_fold_refused_yield_at_ruled_sha(steward_home):
    """RC-5 / F1: the worker is idle *because* its done was refused at the ruled SHA. That is "waiting on us", so the
    idle guard must not fold the worker_stopped fact into pending_facts and go dark."""
    store = Store(steward_home)
    jid = _idle_job_at_ruled_sha(store)
    clock = Clock(time.time())
    lead, arb = make_lead(store, Gate({}), clock)
    arb.submit(jid, "yield", "worker_stopped", seq=7, label="claims_done", sha="c0ffee",
               failed=["same_as_last_ruled"])
    r = lead.on_event(jid)
    assert r["status"] == "review_started"
    assert not [e for e in store.events(jid) if e.what == "tech_lead_idle_paused"]


def test_idle_guard_still_folds_refused_yield_at_another_sha(steward_home):
    store = Store(steward_home)
    jid = _idle_job_at_ruled_sha(store)
    lead, arb = make_lead(store, Gate({}), Clock(time.time()))
    arb.submit(jid, "yield", "worker_stopped", seq=7, label="stuck", sha="deadbeef", failed=["dirty_worktree"])
    assert lead.on_event(jid) == {"status": "deferred", "reason": "idle"}
