import pytest

from steward import landing_queue as q
from steward.models import JobState, Judgment
from steward.store import Store


def test_priority_orders_head_first(steward_home, tmp_path):
    store = Store()
    a, b = store.create_job("a", str(tmp_path)), store.create_job("b", str(tmp_path))
    q.enqueue(store, a.job_id, tmp_path)
    q.enqueue(store, b.job_id, tmp_path, origin="fix-merge", priority=1)
    assert [e.job_id for e in q.queued(store, tmp_path, 10)] == [b.job_id, a.job_id]
    assert q.get(store, b.job_id).priority == 1


def test_migration_adds_columns_to_old_table(steward_home):
    store = Store()
    cols = {r[1] for r in store._conn.execute("PRAGMA table_info(landing_queue)")}
    assert {"priority", "last_target"} <= cols


def test_migration_is_idempotent_on_old_table(steward_home):
    store = Store()
    with store._conn:
        store._conn.execute("ALTER TABLE landing_queue DROP COLUMN last_target")
        store._conn.execute("ALTER TABLE landing_queue DROP COLUMN priority")
    store._migrate_landing_priority()
    store._migrate_landing_priority()
    cols = {r[1] for r in store._conn.execute("PRAGMA table_info(landing_queue)")}
    assert {"priority", "last_target"} <= cols


def test_already_queued_job_is_bumped_to_head(steward_home, tmp_path):
    store = Store()
    a, b = store.create_job("a", str(tmp_path)), store.create_job("b", str(tmp_path))
    q.enqueue(store, a.job_id, tmp_path)
    q.enqueue(store, b.job_id, tmp_path)
    assert q.enqueue(store, b.job_id, tmp_path, origin="incorporate", priority=1) is True
    assert [e.job_id for e in q.queued(store, tmp_path, 10)] == [b.job_id, a.job_id]
    ev = [e for e in store.events(b.job_id) if e.what == "landing_enqueued"]
    assert ev[-1].evidence["origin"] == "incorporate" and ev[-1].evidence["bumped"] is True
    assert q.enqueue(store, b.job_id, tmp_path, origin="incorporate", priority=1) is False  # no second bump


def test_route_to_train_reports_position(steward_home, tmp_path):
    store = Store()
    a, b = store.create_job("a", str(tmp_path)), store.create_job("b", str(tmp_path))
    q.enqueue(store, a.job_id, tmp_path)
    out = q.route_to_train(store, b.job_id, tmp_path, origin="fix-merge")
    assert out == {"accepted": True, "queued": True, "position": 1, "via": "landing train",
                   "detail": "queued for the next train (head of queue)"}


def _accept(store, j):
    with store._conn:
        store._conn.execute("UPDATE jobs SET state='CHECKS_PASSED' WHERE job_id=?", (j.job_id,))
    store.set_state(j.job_id, JobState.ACCEPTED, what="operator accepted", evidence={}, rule="op", judgment=Judgment.human)


def _train_env(monkeypatch, tmp_path):
    monkeypatch.setenv("STEWARD_TRAIN", "1")
    monkeypatch.setenv("STEWARD_TRAIN_REPO", str(tmp_path))


def test_enqueue_failure_does_not_block_accept(steward_home, monkeypatch, tmp_path):
    _train_env(monkeypatch, tmp_path)
    store = Store(); j = store.create_job("g", str(tmp_path))
    monkeypatch.setattr(q, "enqueue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    _accept(store, j)
    assert store.get_job(j.job_id).state == JobState.ACCEPTED
    ev = [e for e in store.events(j.job_id) if e.what == "cleanup_failed"]
    assert ev and ev[0].evidence["cause"] == "enqueue"


def test_failing_event_append_in_hook_does_not_propagate(steward_home, monkeypatch, tmp_path):
    _train_env(monkeypatch, tmp_path)
    store = Store(); j = store.create_job("g", str(tmp_path))
    monkeypatch.setattr(q, "enqueue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    real = store.append_event

    def flaky(job_id, *, what, **kw):
        if what == "cleanup_failed":
            raise RuntimeError("db gone")
        return real(job_id, what=what, **kw)

    monkeypatch.setattr(store, "append_event", flaky)
    _accept(store, j)  # must not raise
    assert store.get_job(j.job_id).state == JobState.ACCEPTED
