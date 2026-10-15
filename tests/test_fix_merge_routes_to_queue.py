from steward import autopilot, cli, landing_queue as q
from steward.store import Store
from test_incorporate import mkjob, scratch  # noqa: F401


def _train(monkeypatch, repo):
    monkeypatch.setenv("STEWARD_TRAIN", "1")
    monkeypatch.setenv("STEWARD_TRAIN_REPO", str(repo))


def test_fix_merge_tap_queues_at_head(scratch, tmp_path, monkeypatch):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    _train(monkeypatch, scratch)
    out = autopilot.start_fix_merge(store, job.job_id, None, start_followup=lambda *a, **k: 1 / 0)
    assert out["accepted"] is True and out["queued"] is True and out["position"] == 1
    assert out["detail"] == "queued for the next train (head of queue)"
    assert q.get(store, job.job_id).priority == 1


def test_fix_merge_bumps_job_already_queued(scratch, tmp_path, monkeypatch):
    store = Store()
    first = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    second = mkjob(store, scratch, tmp_path, {"y.txt": "y"})
    _train(monkeypatch, scratch)
    q.enqueue(store, first.job_id, scratch)
    q.enqueue(store, second.job_id, scratch)
    out = autopilot.start_fix_merge(store, second.job_id, None)
    assert out["queued"] is True and out["position"] == 1
    assert [e.job_id for e in q.queued(store, scratch, 10)] == [second.job_id, first.job_id]


def test_incorporate_handler_queues_instead_of_refusing(scratch, tmp_path, monkeypatch):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    _train(monkeypatch, scratch)
    r = cli._build_control_handlers(store)["incorporate"]({"op": "incorporate", "args": {"job_id": job.job_id}})
    assert r["accepted"] is True and r["queued"] is True and r["position"] == 1
    assert q.get(store, job.job_id).priority == 1
    assert store.kv_get(job.job_id, "incorporated_commit") is None


def test_parked_row_is_requeued_at_head(scratch, tmp_path, monkeypatch):
    store = Store()
    first = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    parked = mkjob(store, scratch, tmp_path, {"y.txt": "y"})
    _train(monkeypatch, scratch)
    q.enqueue(store, first.job_id, scratch)
    q.enqueue(store, parked.job_id, scratch)
    q.park(store, parked.job_id, "dropped 3x")
    out = autopilot.start_fix_merge(store, parked.job_id, None)
    assert out["accepted"] is True and out["position"] == 1
    e = q.get(store, parked.job_id)
    assert e.status == "queued" and e.priority == 1
    ev = [x for x in store.events(parked.job_id) if x.what == "landing_enqueued"][-1]
    assert ev.evidence["origin"] == "fix-merge" and ev.evidence["requeued_from"] == "parked"


def test_landed_row_is_not_reported_as_queued(scratch, tmp_path, monkeypatch):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    _train(monkeypatch, scratch)
    q.enqueue(store, job.job_id, scratch)
    q.mark_landed(store, job.job_id, "abcdef123456")
    out = q.route_to_train(store, job.job_id, scratch, origin="fix-merge")
    assert out["accepted"] is False and "already landed" in out["detail"]
    assert q.get(store, job.job_id).status == "landed"


def test_unqueueable_status_is_refused(scratch, tmp_path, monkeypatch):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.enqueue(store, job.job_id, scratch)
    with store._conn:
        store._conn.execute("UPDATE landing_queue SET status='weird' WHERE job_id=?", (job.job_id,))
    assert q.route_to_train(store, job.job_id, scratch, origin="incorporate")["accepted"] is False


def test_no_merge_protection_blocks_train_route(scratch, tmp_path, monkeypatch):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    _train(monkeypatch, scratch)
    monkeypatch.setattr(autopilot, "no_merge_protection", lambda s, j: ("deadbeef00", "no merge"))
    out = autopilot.start_fix_merge(store, job.job_id, None)
    assert out["accepted"] is False and "protected" in out["detail"]
    assert q.get(store, job.job_id) is None


def test_incorporate_release_is_noted_as_after_landing(scratch, tmp_path, monkeypatch):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    _train(monkeypatch, scratch)
    r = cli._build_control_handlers(store)["incorporate"]({"op": "incorporate", "args": {"job_id": job.job_id, "release": True}})
    assert r["queued"] is True and r["release"] == "after landing"
