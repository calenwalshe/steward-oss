from steward import landing_queue as q
from steward.models import JobState
from steward.store import Store
from test_incorporate import git, mkjob, scratch  # noqa: F401


def test_sync_enqueues_accepted_only(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    b = mkjob(store, scratch, tmp_path, {"y.txt": "y"}, state=JobState.NEEDS_HUMAN)
    added = q.sync_accepted(store, str(scratch))
    assert added == [a.job_id]
    assert q.sync_accepted(store, str(scratch)) == []          # idempotent
    assert [e.job_id for e in q.queued(store, str(scratch), 10)] == [a.job_id]
    assert q.get(store, b.job_id) is None


def test_already_incorporated_is_not_enqueued(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    store.kv_set(a.job_id, "incorporated_commit", "abc")
    assert q.sync_accepted(store, str(scratch)) == []


def test_drop_three_times_parks(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.enqueue(store, a.job_id, str(scratch))
    assert q.mark_dropped(store, a.job_id, "red").status == "queued"
    q.mark_dropped(store, a.job_id, "red")
    e = q.mark_dropped(store, a.job_id, "conflict: x.txt")
    assert e.status == "parked" and e.attempts == 3 and e.last_reason == "conflict: x.txt"
    assert q.queued(store, str(scratch), 10) == []
    assert [p.job_id for p in q.parked(store, str(scratch))] == [a.job_id]


def test_mark_landed(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.enqueue(store, a.job_id, str(scratch))
    q.mark_landed(store, a.job_id, "deadbeef")
    e = q.get(store, a.job_id)
    assert e.status == "landed" and e.landed_commit == "deadbeef"
    assert q.queued(store, str(scratch), 10) == []


def test_park_is_immediate(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.enqueue(store, a.job_id, str(scratch))
    e = q.park(store, a.job_id, "deploy red; reverted abc")
    assert e.status == "parked" and e.last_reason == "deploy red; reverted abc"
    assert q.queued(store, str(scratch), 10) == []
    assert [p.job_id for p in q.parked(store, str(scratch))] == [a.job_id]


def test_remove_takes_job_out_and_reaccept_requeues(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.sync_accepted(store, scratch)
    e = q.remove(store, a.job_id, "job is needs_human")
    assert e.status == "removed" and e.last_reason == "job is needs_human"
    assert q.queued(store, scratch, 10) == []
    assert q.sync_accepted(store, scratch) == [a.job_id]       # still ACCEPTED (re-accepted): back in the queue
    assert q.get(store, a.job_id).status == "queued"


def test_prune_removes_stale_entries(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    b = mkjob(store, scratch, tmp_path, {"y.txt": "y"})
    c = mkjob(store, scratch, tmp_path, {"z.txt": "z"})
    q.sync_accepted(store, scratch)
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.NEEDS_HUMAN.value, a.job_id))
    store.kv_set(b.job_id, "incorporated_commit", "abc123")
    out = q.prune(store, scratch)
    assert set(out) == {a.job_id, b.job_id}
    assert q.get(store, a.job_id).status == "removed" and "NEEDS_HUMAN" in q.get(store, a.job_id).last_reason
    assert q.get(store, b.job_id).status == "landed" and q.get(store, b.job_id).landed_commit == "abc123"
    assert [e.job_id for e in q.queued(store, scratch, 10)] == [c.job_id]
