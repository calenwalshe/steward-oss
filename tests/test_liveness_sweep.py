import time
from steward import liveness
from steward.models import JobState
from steward.store import Store

S = {"enabled": True, "every_s": 60.0, "orphan_s": 1800.0, "idle_s": 1800.0, "tries_per_day": 2}


def _job(store, state):
    j = store.create_job("g", "/tmp/r")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state.value, j.job_id))
    return store.get_job(j.job_id)


def _sweep(store, now, calls=None):
    return liveness.sweep(store, now=now, settings=S, remedy=lambda st, job, **kw: (calls.append((job.job_id, kw["orphan_since"])) or {"rung": "x"}) if calls is not None else {},
                          heal_lock=lambda *a, **k: {}, runner_alive=lambda _: False, check_runs_live=lambda _: False)


def test_orphan_clock_starts_then_remedies_after_threshold(steward_home):
    store = Store(); j = _job(store, JobState.STOPPING); calls = []
    t0 = 10_000.0
    out = _sweep(store, t0, calls)
    assert out["orphans"] == [j.job_id] and calls == []          # clock started, no remedy yet
    assert [e.what for e in store.events(j.job_id)][-1] == "liveness_orphan"
    _sweep(store, t0 + 61, calls)
    assert calls == []
    out = _sweep(store, t0 + 1801, calls)
    assert calls == [(j.job_id, t0)] and out["remedied"] == [j.job_id]


def test_actor_return_clears_clock_and_writes_event(steward_home):
    store = Store(); j = _job(store, JobState.STOPPING)
    _sweep(store, 10_000.0)
    out = liveness.sweep(store, now=10_061.0, settings=S, remedy=lambda *a, **k: {}, heal_lock=lambda *a, **k: {},
                         runner_alive=lambda _: True, check_runs_live=lambda _: False)
    assert out["orphans"] == [] and store.kv_get(j.job_id, liveness.ORPHAN_KEY) is None
    assert [e.what for e in store.events(j.job_id)][-1] == "liveness_actor"


def test_throttle_and_disabled(steward_home):
    store = Store()
    assert liveness.sweep(store, now=1.0, settings={**S, "enabled": False})["skipped"] == "off"
    liveness.sweep(store, now=100.0, settings=S, remedy=lambda *a, **k: {}, heal_lock=lambda *a, **k: {},
                   runner_alive=lambda _: True, check_runs_live=lambda _: False)
    assert liveness.sweep(store, now=120.0, settings=S)["skipped"] == "throttle"


def test_health_component_red_when_stale_or_long_orphan(steward_home):
    store = Store()
    assert liveness.component(store, now=1000.0).ok is None            # never ran: unknown
    store.kv_set("_liveness", "tick_at", 1000.0)
    assert liveness.component(store, now=1100.0).ok is True
    assert liveness.component(store, now=1400.0).ok is False           # > 300 s stale
    j = _job(store, JobState.STOPPING)
    store.kv_set(j.job_id, liveness.ORPHAN_KEY, 1000.0 - 3601)
    assert liveness.component(store, now=1100.0).ok is False           # orphan > 60 min


def test_stop_fn_is_never_passed_to_remedy(steward_home):
    """Fix round 1: stop_fn is the sweep's fix-up stop only; finish_stop closes panes, it never re-sends a stop."""
    store = Store(); j = _job(store, JobState.STOPPING); seen = []
    kw_ = dict(settings={**S, "every_s": 0.0}, heal_lock=lambda *a, **k: {}, runner_alive=lambda _: False, check_runs_live=lambda _: False)
    liveness.sweep(store, now=10_000.0, remedy=None, **kw_)  # starts the orphan clock
    liveness.sweep(store, now=12_000.0, remedy=lambda *a, **k: seen.append(sorted(k)) or {}, **kw_)
    liveness.sweep(store, now=14_000.0, remedy=lambda *a, **k: seen.append(sorted(k)) or {}, stop_fn=lambda jid: None, **kw_)
    assert "stop_fn" not in seen[0] and "stop_fn" not in seen[1]


def test_lock_heal_is_recorded_under_the_oldest_live_job_of_the_repo(steward_home):
    store = Store(); j = _job(store, JobState.STOPPING); got = []
    out = liveness.sweep(store, now=1.0, settings=S, remedy=None, runner_alive=lambda _: True, check_runs_live=lambda _: False,
                         heal_lock=lambda st, repo, *, now, job_id=None: got.append((repo, job_id)) or {"healed": True})
    assert got == [("/tmp/r", j.job_id)] and out["healed_locks"] == [{"healed": True}]
