from steward import landing_queue as q
from steward.models import JobState, Judgment
from steward.store import Store


def test_accept_enqueues_in_train_repo(steward_home, monkeypatch, tmp_path):
    monkeypatch.setenv("STEWARD_TRAIN", "1"); monkeypatch.setenv("STEWARD_TRAIN_REPO", str(tmp_path))
    store = Store(); j = store.create_job("g", str(tmp_path))
    with store._conn:
        store._conn.execute("UPDATE jobs SET state='CHECKS_PASSED' WHERE job_id=?", (j.job_id,))
    store.set_state(j.job_id, JobState.ACCEPTED, what="operator accepted", evidence={}, rule="op", judgment=Judgment.human)
    e = q.get(store, j.job_id)
    assert e is not None and e.status == "queued"
    ev = [x for x in store.events(j.job_id) if x.what == "landing_enqueued"]
    assert ev and ev[0].evidence["origin"] == "accept"
    assert q.sync_accepted(store, tmp_path) == []          # nothing left for the safety net


def test_accept_outside_train_repo_does_not_enqueue(steward_home, monkeypatch, tmp_path):
    monkeypatch.setenv("STEWARD_TRAIN", "1"); monkeypatch.setenv("STEWARD_TRAIN_REPO", str(tmp_path / "other"))
    store = Store(); j = store.create_job("g", str(tmp_path))
    with store._conn:
        store._conn.execute("UPDATE jobs SET state='CHECKS_PASSED' WHERE job_id=?", (j.job_id,))
    store.set_state(j.job_id, JobState.ACCEPTED, what="operator accepted", evidence={}, rule="op", judgment=Judgment.human)
    assert q.get(store, j.job_id) is None
