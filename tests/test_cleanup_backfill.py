from steward import approvals, cleanup
from steward.models import JobState
from steward.store import Store
from test_incorporate import mkjob, scratch, git  # noqa: F401
from steward import incorporate as inc


def test_backfill_dry_run_then_apply(steward_home, scratch, tmp_path):
    store = Store()
    done = store.create_job("g", "/tmp/r")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state='RELEASED' WHERE job_id=?", (done.job_id,))
    approvals.open_approval(store, done.job_id, "x", kind="delivery")
    acc = mkjob(store, scratch, tmp_path, {"a.txt": "a"})
    inc.incorporate(store, acc.job_id)
    with store._conn:
        store._conn.execute("UPDATE jobs SET state='ACCEPTED' WHERE job_id=?", (acc.job_id,))
    dry = cleanup.backfill(store, dry_run=True)
    assert dry["approvals"] == 1 and acc.job_id in dry["would"]
    assert approvals.list_approvals(store, done.job_id, status="pending")
    real = cleanup.backfill(store, dry_run=False)
    assert real["approvals"] == 1 and real["released"] == [acc.job_id]
    assert not approvals.list_approvals(store, done.job_id, status="pending")
    assert store.get_job(acc.job_id).state == JobState.RELEASED
