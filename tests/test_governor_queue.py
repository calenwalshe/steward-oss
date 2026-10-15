"""Room gate + sweep release. Fakes only."""
from __future__ import annotations

import pytest

from steward import governor, sequencing, tabnames
from steward.models import JobState
from steward.store import Store
from governor_fakes import FakeSys


@pytest.fixture
def store(steward_home):
    return Store(steward_home)


def _job(store, name, state=JobState.CONTRACTED):
    job = store.create_job(f"{name} goal", "/tmp/repo")
    store.kv_set(job.job_id, tabnames.NAME_KEY, name)
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state.value, job.job_id))
    return store.get_job(job.job_id)


class Launcher:
    """Mirrors _h_launch: the gate first, then 'launch' (state RUNNING)."""
    def __init__(self, store, sys_reader):
        self.store, self.sys, self.calls = store, sys_reader, []
    def __call__(self, job_id, args):
        why = governor.gate(self.store, job_id, args, reading=governor.read(self.store, sys_reader=self.sys))
        if why:
            return {"accepted": False, "detail": f"waiting for room: {why}"}
        self.calls.append((job_id, args))
        sequencing.on_manual_launch(self.store, job_id)
        with self.store._conn:
            self.store._conn.execute("UPDATE jobs SET state='RUNNING' WHERE job_id=?", (job_id,))
        return {"accepted": True}


def test_full_machine_parks_launch_with_args(store):
    full = FakeSys(tmp=75.0)
    j = _job(store, "a")
    args = {"workspace": "w9", "kind": "codex", "agent_args": ["--model", "x"]}
    why = governor.gate(store, j.job_id, args, reading=governor.read(store, sys_reader=full))
    assert why and "tmp_used_pct" in why
    assert store.kv_get(j.job_id, governor.K_ROOM) == why
    assert store.kv_get(j.job_id, sequencing.K_ARGS) == args
    assert sequencing.is_queued(store, j.job_id)
    ev = [e for e in store.events(j.job_id) if e.rule == "governor_wait"]
    assert len(ev) == 1


def test_manual_launch_while_full_stays_queued(store):
    full = FakeSys(tmp=75.0)
    j = _job(store, "a")
    launcher = Launcher(store, full)
    assert launcher(j.job_id, {"workspace": "w9"})["accepted"] is False
    assert launcher(j.job_id, {"workspace": "w9"})["accepted"] is False   # second try: still parked, one event
    assert len([e for e in store.events(j.job_id) if e.rule == "governor_wait"]) == 1
    assert store.kv_get(j.job_id, sequencing.K_ARGS) == {"workspace": "w9"}


def test_sweep_releases_oldest_first_one_per_tick(store):
    full, calm = FakeSys(tmp=75.0), FakeSys()
    a, b = _job(store, "a"), _job(store, "b")
    for j in (a, b):
        governor.gate(store, j.job_id, {"workspace": "w1"}, reading=governor.read(store, sys_reader=full))
    st = sequencing.status(store, a.job_id)
    assert st["state"] == "waiting" and "waiting for room" in st["text"]
    launcher = Launcher(store, calm)
    reading = lambda: governor.read(store, sys_reader=calm)
    assert sequencing.sweep(store, launcher, reading_fn=reading) == [a.job_id]
    assert launcher.calls[0][1]["workspace"] == "w1"
    assert store.kv_get(a.job_id, governor.K_ROOM) is None
    assert any(e.rule == "governor_released" for e in store.events(a.job_id))
    assert sequencing.sweep(store, launcher, reading_fn=reading) == [b.job_id]


def test_sweep_keeps_waiting_while_full(store):
    full = FakeSys(tmp=75.0)
    a = _job(store, "a")
    governor.gate(store, a.job_id, {"workspace": "w1"}, reading=governor.read(store, sys_reader=full))
    launcher = Launcher(store, full)
    assert sequencing.sweep(store, launcher, reading_fn=lambda: governor.read(store, sys_reader=full)) == []
    assert launcher.calls == []
