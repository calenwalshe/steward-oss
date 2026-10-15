from steward import cleanup
from steward.models import JobState
from steward.store import Store


def test_set_state_survives_cleanup_and_event_failure(steward_home, monkeypatch):
    store = Store()
    j = store.create_job("g", "/tmp/r")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (JobState.CONTRACTED.value, j.job_id))
    called = []
    monkeypatch.setattr(cleanup, "_close_approvals", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    real = store.append_event

    def bad_event(job_id, **kw):
        if kw.get("what") == "cleanup_failed":
            raise RuntimeError("event write down")
        return real(job_id, **kw)
    monkeypatch.setattr(store, "append_event", bad_event)
    from steward import check_runs
    monkeypatch.setattr(check_runs, "cancel_job_runs", lambda *a, **k: called.append(1))
    from steward.models import Judgment
    out = store.set_state(j.job_id, JobState.STOPPED, what="stop", evidence={}, rule="t", judgment=Judgment.code)
    assert out.state == JobState.STOPPED
    assert called == [1]


def test_on_resolved_never_raises_when_get_job_raises(steward_home, monkeypatch):
    store = Store()
    monkeypatch.setattr(store, "get_job", lambda *_: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(store, "append_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("y")))
    assert cleanup.on_resolved(store, "nope", cause="STOPPED")["approvals"] == []
