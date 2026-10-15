from steward import cleanup
from steward.models import JobState
from steward.store import Store
from steward.train_housekeeping import MERGE_GOAL_PREFIX, stop_marked_fixups


def _mk(store, state):
    j = store.create_job(MERGE_GOAL_PREFIX + "x`", "/tmp/r")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state.value, j.job_id))
    store.kv_set(j.job_id, cleanup.FIXUP_STOP_KEY, {"cause": "RELEASED"})
    return j


def _events(store, jid):
    return [e for e in store.list_events(jid) if e.what == "fixup_stopped_parent_done"] if hasattr(store, "list_events") else []


def test_refused_stop_keeps_mark(steward_home):
    store = Store()
    j = _mk(store, JobState.RUNNING)
    assert stop_marked_fixups(store, lambda _id: {"accepted": False}) == []
    assert cleanup.pending_fixup_stops(store) == [(j.job_id, "RELEASED")]
    assert stop_marked_fixups(store, lambda _id: {"accepted": True}) == [j.job_id]
    assert cleanup.pending_fixup_stops(store) == []


def test_accepted_state_not_stopped(steward_home):
    store = Store()
    j = _mk(store, JobState.ACCEPTED)
    calls = []
    assert stop_marked_fixups(store, lambda i: calls.append(i)) == []
    assert calls == [] and cleanup.pending_fixup_stops(store) == []
