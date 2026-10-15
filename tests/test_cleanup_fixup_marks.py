from steward import cleanup
from steward.models import JobState
from steward.store import Store
from steward.train_housekeeping import MERGE_GOAL_PREFIX


def _job(store, goal, state):
    j = store.create_job(goal, "/tmp/r")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state.value, j.job_id))
    return j


def test_mark_pending_and_clear_fixup_stops(steward_home):
    store = Store()
    parent = _job(store, "parent", JobState.RELEASED)
    fix = _job(store, MERGE_GOAL_PREFIX + "x` conflicts", JobState.RUNNING)
    other = _job(store, "ordinary child", JobState.RUNNING)
    done = _job(store, MERGE_GOAL_PREFIX + "y`", JobState.STOPPED)
    store.kv_set(parent.job_id, "children", [fix.job_id, other.job_id, done.job_id])
    out = cleanup.on_resolved(store, parent.job_id, cause="RELEASED")
    assert out["fixups_marked"] == [fix.job_id]
    assert cleanup.pending_fixup_stops(store) == [(fix.job_id, "RELEASED")]
    cleanup.clear_fixup_stop(store, fix.job_id)
    assert cleanup.pending_fixup_stops(store) == []
