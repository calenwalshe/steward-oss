from steward import cleanup, sequencing, train_housekeeping as hk
from steward.models import JobState
from steward.store import Store


def _job(store, state, goal="Merge `main` into YOUR branch"):
    j = store.create_job(goal, "/tmp/r")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state.value, j.job_id))
    return j


def test_needs_human_fixup_of_incorporated_parent_is_stopped(steward_home):
    store = Store()
    parent = _job(store, JobState.RELEASED, goal="real work")
    store.kv_set(parent.job_id, "incorporated_commit", "abc")
    kid = _job(store, JobState.NEEDS_HUMAN)
    store.kv_set(kid.job_id, "autopilot_followup", parent.job_id)
    stopped = []
    assert hk.stop_superseded_fixups(store, stopped.append) == [kid.job_id]


def test_marked_fixups_are_stopped_and_cleared(steward_home):
    store = Store()
    parent = _job(store, JobState.STOPPED, goal="real work")
    kid = _job(store, JobState.RUNNING)
    store.kv_set(kid.job_id, "autopilot_followup", parent.job_id)
    cleanup.on_resolved(store, parent.job_id, cause="STOPPED")
    assert cleanup.pending_fixup_stops(store) == [(kid.job_id, "STOPPED")]
    stopped = []
    assert hk.stop_marked_fixups(store, stopped.append) == [kid.job_id]
    assert cleanup.pending_fixup_stops(store) == []
    assert any(e.what == "fixup_stopped_parent_done" for e in store.events(kid.job_id))


def test_done_includes_stopped_and_failed():
    assert {JobState.STOPPED, JobState.FAILED} <= sequencing._DONE
