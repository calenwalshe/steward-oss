"""Steward governor: readings, limits, room check. Fakes only."""
from __future__ import annotations

import pytest

from steward import governor, sequencing
from steward.models import JobState
from steward.store import Store
from governor_fakes import FakeSys


@pytest.fixture
def store(steward_home):
    return Store(steward_home)


def _job(store, state):
    job = store.create_job("g", "/tmp/repo")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state.value, job.job_id))
    return store.get_job(job.job_id)


def test_calm_machine_has_room(store):
    r = governor.read(store, sys_reader=FakeSys())
    assert not r.wait and not r.critical and r.breaches == []


@pytest.mark.parametrize("kw,signal,level", [
    ({"tmp": 74.0}, "tmp_used_pct", "wait"),
    ({"tmp": 91.0}, "tmp_used_pct", "critical"),
    ({"mem": 3.5}, "mem_available_gb", "wait"),
    ({"mem": 1.5}, "mem_available_gb", "critical"),
    ({"disk": 86.0}, "disk_used_pct", "wait"),
    ({"disk": 96.0}, "disk_used_pct", "critical"),
    ({"load": 25.0}, "load5", "wait"),
])
def test_each_threshold(store, kw, signal, level):
    r = governor.read(store, sys_reader=FakeSys(**kw))
    assert [(b.signal, b.level) for b in r.breaches] == [(signal, level)]
    assert r.wait and r.critical == (level == "critical")
    assert signal.split("_")[0] in r.reason()


def test_running_jobs_counts_in_flight_launches(store):
    for _ in range(4):
        _job(store, JobState.RUNNING)
    _job(store, JobState.STOPPING)
    staged = _job(store, JobState.CONTRACTED)
    store.kv_set(staged.job_id, sequencing.K_LAUNCHED, True)
    _job(store, JobState.CONTRACTED)          # not launched: does not count
    _job(store, JobState.PAUSED)              # paused: does not count
    assert governor.running_jobs(store) == 6
    assert not governor.read(store, sys_reader=FakeSys()).wait
    _job(store, JobState.RUNNING)
    r = governor.read(store, sys_reader=FakeSys())
    assert [b.signal for b in r.breaches] == ["running_jobs"]


def test_unreadable_signal_is_skipped(store):
    r = governor.read(store, sys_reader=FakeSys(mem=None, tmp=None, disk=None, load=None))
    assert r.breaches == [] and r.values["tmp_used_pct"] is None


def test_toml_overrides_and_corrupt_toml(store, tmp_path, monkeypatch):
    from steward.store import resolve_config_dir
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "cfg"))  # never the real ~/.config/steward
    cfg = resolve_config_dir() / "steward.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("[governor]\nmax_running_jobs = 1\ntmp_max_pct = 50\n")
    lim = governor.limits()
    assert lim.max_running_jobs == 1 and lim.tmp_max_pct == 50 and lim.disk_max_pct == 85
    cfg.write_text("[governor\nnot toml")
    assert governor.limits() == governor.Limits()
