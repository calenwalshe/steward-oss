import time

from steward import coordinator_link as cl
from steward import ownership
from steward.models import JobState
from steward.store import Store


def _parked(store):
    j = store.create_job("g", "/tmp/r")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (JobState.NEEDS_HUMAN.value, j.job_id))
    return j


class _Runner:
    def run(self, *a, **k):  # pragma: no cover - never reached; delivery is patched
        raise AssertionError


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_COORDINATOR_STATE", str(tmp_path / "coord"))


def _hb(now):
    cl.write_heartbeat(cursor=1, head="h", root="r", now=now)


def _capture(monkeypatch):
    calls = []

    def fake(store, job_id, runner, *, reason, episode_id, now):
        calls.append((job_id, reason, episode_id))
        return "delivered"

    monkeypatch.setattr(cl, "deliver_ask", fake)
    return calls


def test_gate_holds_needs_you_while_holding(steward_home, tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    store = Store(); j = _parked(store); now = time.time()
    _hb(now); cl.open_hold(store, j.job_id, episode_id="e", trigger="needs_human:x", now=now)
    assert cl.holding(store, j.job_id, now=now + 60)
    assert ownership.owner_notice_allowed(store, j.job_id, "NEEDS_YOU") == (False, "awaiting_coordinator")
    assert ownership.owner_notice_allowed(store, j.job_id, "READY")[1] != "awaiting_coordinator"


def test_hold_ignored_when_heartbeat_stale(steward_home, tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    store = Store(); j = _parked(store); now = time.time()
    _hb(now - 300); cl.open_hold(store, j.job_id, episode_id="e", trigger="needs_human:x", now=now)
    assert not cl.holding(store, j.job_id, now=now)
    assert ownership.owner_notice_allowed(store, j.job_id, "NEEDS_YOU")[1] != "awaiting_coordinator"


def test_not_holding_when_killed_or_closed(steward_home, tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    store = Store(); j = _parked(store); now = time.time()
    _hb(now); cl.open_hold(store, j.job_id, episode_id="e", trigger="needs_human:x", now=now)
    cl.kill_file().parent.mkdir(parents=True, exist_ok=True); cl.kill_file().write_text("")
    assert not cl.holding(store, j.job_id, now=now)
    cl.kill_file().unlink()
    cl.close_hold(store, j.job_id, "resolved", now=now)
    assert not cl.holding(store, j.job_id, now=now)


def test_still_stuck_rules(steward_home, tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    store = Store(); j = _parked(store)
    rec = cl.open_hold(store, j.job_id, episode_id="e", trigger="needs_human:x", now=1.0)
    assert cl.still_stuck(store, store.get_job(j.job_id), rec)
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (JobState.RUNNING.value, j.job_id))
    assert not cl.still_stuck(store, store.get_job(j.job_id), rec)
    other = {**rec, "trigger": "orphan:RUNNING", "job_updated_at": store.get_job(j.job_id).updated_at}
    assert cl.still_stuck(store, store.get_job(j.job_id), other)
    other["job_updated_at"] = "older"
    assert not cl.still_stuck(store, store.get_job(j.job_id), other)
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (JobState.FAILED.value, j.job_id))
    assert not cl.still_stuck(store, store.get_job(j.job_id),
                              {**other, "job_updated_at": store.get_job(j.job_id).updated_at})


def test_sweep_leaves_fresh_young_hold(steward_home, tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch); calls = _capture(monkeypatch)
    store = Store(); j = _parked(store); now = time.time()
    _hb(now); cl.open_hold(store, j.job_id, episode_id="e", trigger="needs_human:x", now=now)
    assert cl.sweep(store, _Runner(), now=now + 100) == 0
    assert calls == [] and cl.hold(store, j.job_id)


def test_sweep_delivers_at_expiry(steward_home, tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch); calls = _capture(monkeypatch)
    store = Store(); j = _parked(store); now = time.time()
    store.append_event(j.job_id, what="park", evidence={"detail": "tests red"}, rule="r",
                       judgment=__import__("steward.models", fromlist=["Judgment"]).Judgment.code)
    cl.open_hold(store, j.job_id, episode_id="e", trigger="needs_human:x", now=now)
    _hb(now + 1250)
    assert cl.sweep(store, _Runner(), now=now + 1300) == 1
    assert len(calls) == 1 and "20 min" in calls[0][1] and calls[0][2] == "e"
    assert cl.hold(store, j.job_id) is None
    assert store.kv_get(j.job_id, cl.HOLD_KEY)["how"] == "expired"
    assert cl.sweep(store, _Runner(), now=now + 1400) == 0 and len(calls) == 1


def test_sweep_delivers_when_heartbeat_stale_and_when_killed(steward_home, tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch); calls = _capture(monkeypatch)
    store = Store(); a = _parked(store); b = _parked(store); now = time.time()
    _hb(now - 500)
    cl.open_hold(store, a.job_id, episode_id="ea", trigger="needs_human:x", now=now)
    assert cl.sweep(store, _Runner(), now=now) == 1
    assert store.kv_get(a.job_id, cl.HOLD_KEY)["how"] == "coordinator_down"
    _hb(now); cl.open_hold(store, b.job_id, episode_id="eb", trigger="needs_human:x", now=now)
    cl.kill_file().parent.mkdir(parents=True, exist_ok=True); cl.kill_file().write_text("")
    assert cl.sweep(store, _Runner(), now=now) == 1
    assert store.kv_get(b.job_id, cl.HOLD_KEY)["how"] == "killed"
    assert "KILL" in calls[1][1]


def test_sweep_closes_superseded_silently(steward_home, tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch); calls = _capture(monkeypatch)
    store = Store(); j = _parked(store); now = time.time()
    _hb(now); cl.open_hold(store, j.job_id, episode_id="e", trigger="needs_human:x", now=now)
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (JobState.RUNNING.value, j.job_id))
    assert cl.sweep(store, _Runner(), now=now + 5000) == 0
    assert calls == [] and store.kv_get(j.job_id, cl.HOLD_KEY)["how"] == "superseded"


def test_sweep_closes_hold_of_missing_job(steward_home, tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch); calls = _capture(monkeypatch)
    store = Store(); j = _parked(store)
    cl.open_hold(store, j.job_id, episode_id="e", trigger="needs_human:x", now=1.0)
    monkeypatch.setattr(store, "get_job", lambda jid: None)
    assert cl.sweep(store, _Runner(), now=2.0) == 0 and calls == []
    assert store.kv_get(j.job_id, cl.HOLD_KEY)["how"] == "superseded"


def test_held_notice_is_sent_by_sweep_after_release(steward_home, tmp_path, monkeypatch):
    """E2: nothing retries a held notice; the sweep delivers the line."""
    _env(tmp_path, monkeypatch); calls = _capture(monkeypatch)
    store = Store(); j = _parked(store); now = time.time()
    _hb(now); cl.open_hold(store, j.job_id, episode_id="e", trigger="needs_human:x", now=now)
    assert ownership.owner_notice_allowed(store, j.job_id, "NEEDS_YOU")[1] == "awaiting_coordinator"
    _hb(now + 1300)
    assert cl.sweep(store, _Runner(), now=now + 1300) == 1 and len(calls) == 1


def test_tick_step_registered():
    import inspect
    from steward import cli
    src = inspect.getsource(cli)
    i = src.index("push_module.flush_all_due(store, SubprocessRunner())")
    assert "coordinator_link.sweep(store, SubprocessRunner())" in src[i:i + 400]
