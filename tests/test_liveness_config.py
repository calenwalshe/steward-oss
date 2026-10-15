from steward import config, liveness
from steward.store import Store


def test_defaults_and_env_off(monkeypatch):
    s = config.liveness_settings({})
    assert s == {"enabled": True, "every_s": 60.0, "orphan_s": 1800.0, "idle_s": 1800.0, "tries_per_day": 2,
                 "no_worker_s": 900.0}
    assert config.liveness_settings({"STEWARD_LIVENESS": "0"})["enabled"] is False


def test_record_runner_writes_kv(steward_home):
    store = Store()
    j = store.create_job("g", "/tmp/r")
    liveness.record_runner(store, j.job_id, turn_open=True, progress_ts=123.0, pid=42)
    rec = store.kv_get(j.job_id, liveness.KEY)
    assert rec["turn_open"] is True and rec["progress_ts"] == 123.0 and rec["pid"] == 42 and rec["at"] > 0
