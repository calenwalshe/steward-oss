from steward import autopilot, delivery
from steward.store import Store
from test_incorporate import mkjob, scratch  # noqa: F401


def test_autopilot_steps_aside_when_train_owns_repo(scratch, tmp_path, monkeypatch):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    monkeypatch.setenv("STEWARD_TRAIN", "1")
    monkeypatch.setenv("STEWARD_TRAIN_REPO", str(scratch))
    out = autopilot.try_merge(store, job.job_id, runner=None)
    assert out["outcome"] == "skipped" and "landing train" in out["detail"]
    assert store.kv_get(job.job_id, "incorporated_commit") is None


def test_start_fix_merge_refuses_when_train_owns_repo(scratch, tmp_path, monkeypatch):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    monkeypatch.setenv("STEWARD_TRAIN", "1")
    monkeypatch.setenv("STEWARD_TRAIN_REPO", str(scratch))
    started = []
    out = autopilot.start_fix_merge(store, job.job_id, None, start_followup=lambda *a, **k: started.append(a))
    assert started == [] and "landing train" in str(out)


def test_delivery_sweep_skips_train_repo(scratch, tmp_path, monkeypatch):
    store = Store()
    mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    monkeypatch.setenv("STEWARD_TRAIN", "1")
    monkeypatch.setenv("STEWARD_TRAIN_REPO", str(scratch))
    spawned = []
    delivery.sweep(store, now=10_000.0, spawn=lambda *a, **k: spawned.append(a))
    assert spawned == []


def test_run_delivery_noop_when_train_owns_repo(scratch, tmp_path, monkeypatch):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    monkeypatch.setenv("STEWARD_TRAIN", "1")
    monkeypatch.setenv("STEWARD_TRAIN_REPO", str(scratch))
    out = delivery.run_delivery(store, job.job_id, None)
    assert out["step"] == "none" and "landing train" in out["reason"]
    assert store.kv_get(job.job_id, "incorporated_commit") is None


def test_ensure_attempted_does_not_consume_attempt_when_train_owns_repo(scratch, tmp_path, monkeypatch):
    from steward import autopilot_bg
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    monkeypatch.setenv("STEWARD_TRAIN", "1")
    monkeypatch.setenv("STEWARD_TRAIN_REPO", str(scratch))
    monkeypatch.setattr(autopilot_bg, "start", lambda *a, **k: (_ for _ in ()).throw(AssertionError("started")))
    assert autopilot.ensure_attempted(store, job.job_id, None) is None
    assert not store.kv_get(job.job_id, autopilot.ATTEMPT_KEY)
