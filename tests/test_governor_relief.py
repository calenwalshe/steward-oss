"""Critical relief. Fakes only."""
from __future__ import annotations

import pytest

from steward import governor
from steward.models import JobState
from steward.store import Store
from governor_fakes import FakeSys


@pytest.fixture
def store(steward_home):
    return Store(steward_home)


def _running(store, n):
    out = []
    for _ in range(n):
        j = store.create_job("g", "/tmp/repo")
        with store._conn:
            store._conn.execute("UPDATE jobs SET state='RUNNING' WHERE job_id=?", (j.job_id,))
        out.append(j.job_id)
    return out   # created in order: last is newest


class Ctl:
    def __init__(self, store):
        self.store, self.paused, self.resumed, self.cancelled = store, [], [], []
    def pause(self, jid):
        self.paused.append(jid)
        with self.store._conn:
            self.store._conn.execute("UPDATE jobs SET state='PAUSED' WHERE job_id=?", (jid,))
        return {"accepted": True}
    def resume(self, jid):
        self.resumed.append(jid)
        with self.store._conn:
            self.store._conn.execute("UPDATE jobs SET state='RUNNING' WHERE job_id=?", (jid,))
        return {"accepted": True}
    def cancel(self, jid):
        self.cancelled.append(jid)
        return {"cancelled": ["cr-1"], "killed": [], "kept": []}


def _relieve(store, ctl, sys_reader, now):
    return governor.relieve(store, pause=ctl.pause, resume=ctl.resume, cancel=ctl.cancel,
                            reading=governor.read(store, sys_reader=sys_reader), now=now)


def test_critical_pauses_newest_and_cancels_its_runs(store):
    a, b = _running(store, 2)
    ctl = Ctl(store)
    out = _relieve(store, ctl, FakeSys(tmp=95.0), 1000.0)
    assert out["paused"] == [b] and ctl.cancelled == [b]
    assert store.kv_get(governor.GKV, "paused") == [b]
    assert any(e.rule == "governor_paused" for e in store.events(b))


def test_one_pause_per_tick_while_critical(store):
    a, b = _running(store, 2)
    ctl = Ctl(store)
    _relieve(store, ctl, FakeSys(tmp=95.0), 1000.0)
    _relieve(store, ctl, FakeSys(tmp=95.0), 1015.0)
    assert ctl.paused == [b, a]


def test_resume_only_after_ten_calm_minutes(store):
    (a,) = _running(store, 1)
    ctl = Ctl(store)
    _relieve(store, ctl, FakeSys(tmp=95.0), 1000.0)
    _relieve(store, ctl, FakeSys(), 1100.0)                 # calm starts
    _relieve(store, ctl, FakeSys(tmp=75.0), 1200.0)         # wait-level breach resets calm
    _relieve(store, ctl, FakeSys(), 1300.0)                 # calm restarts
    assert ctl.resumed == []
    out = _relieve(store, ctl, FakeSys(), 1300.0 + 600.0)
    assert out["resumed"] == [a] and store.kv_get(governor.GKV, "paused") in (None, [])


def test_user_paused_job_is_never_resumed(store):
    (a,) = _running(store, 1)
    with store._conn:
        store._conn.execute("UPDATE jobs SET state='PAUSED' WHERE job_id=?", (a,))
    ctl = Ctl(store)
    _relieve(store, ctl, FakeSys(), 1000.0)
    _relieve(store, ctl, FakeSys(), 2000.0)
    assert ctl.resumed == []


def test_cancel_runs_for_spares_shared_run(store):
    from steward import check_runs
    a, b = _running(store, 2)
    now = 1000.0
    with store._conn:
        store._conn.execute(
            "INSERT INTO check_runs (run_id, equiv_key, tree_sha, command, env_digest, toolchain, heavy, host, state,"
            " queue_seq, requested_by, enqueued_epoch) VALUES ('cr-x','k','t','pytest','e','tc',1,?,'queued',1,?,?)",
            (check_runs.host_id(), a, now))
        for jid in (a, b):
            store._conn.execute("INSERT INTO check_run_waiters (run_id, job_id, purpose, how, attached_epoch)"
                " VALUES ('cr-x', ?, 'verify', 'queued', ?)", (jid, now))
    out = check_runs.cancel_runs_for(store, a, now=now, reason="governor critical", kill_fn=lambda pid: True)
    assert out["kept"] == ["cr-x"] and out["cancelled"] == []
