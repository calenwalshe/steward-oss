import time
from steward import approvals, blockers, cleanup, ownership
from steward.models import JobState, Judgment
from steward.store import Store


def _job(store, state):
    j = store.create_job("goal", "/tmp/r")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state.value, j.job_id))
    return j


def test_stale_approvals_closed_owner_decisions_carried(steward_home):
    store = Store()
    j = _job(store, JobState.ACCEPTED)
    a1 = approvals.open_approval(store, j.job_id, "Incorporate?", kind="general")
    a2 = approvals.open_approval(store, j.job_id, "ship it?", kind="acceptance")
    a3 = approvals.open_approval(store, j.job_id, "which colour?", kind="general")
    approvals.annotate(store, j.job_id, a3["id"], owner_decision=True)
    out = cleanup.on_resolved(store, j.job_id, cause="RELEASED")
    assert sorted(out["approvals"]) == sorted([a1["id"], a2["id"]]) and out["carried"] == [a3["id"]]
    assert approvals.get_approval(store, j.job_id, a3["id"])["status"] == "pending"
    assert approvals.get_approval(store, j.job_id, a1["id"])["status"] == "stale"
    whats = [e.what for e in store.events(j.job_id)]
    assert "approvals_superseded" in whats and "owner_decision_carried" in whats


def test_blockers_resolved_except_owner_questions(steward_home):
    store = Store()
    j = _job(store, JobState.STOPPED)
    b, _ = blockers.raise_blocker(store, j.job_id, kind="verification_unverified", subject="v", claim="c",
                                  evidence=["e1"], origin_actor=ownership.CONTROLLER, now=time.time())
    q, _ = blockers.raise_blocker(store, j.job_id, kind="owner_question", subject="q1", claim="which?",
                                  evidence=["e2"], origin_actor=ownership.CONTROLLER, now=time.time())
    out = cleanup.on_resolved(store, j.job_id, cause="STOPPED")
    assert out["blockers"] == [b.blocker_id] and blockers.get(store, b.blocker_id).disposition == "resolved"
    assert blockers.get(store, q.blocker_id).disposition != "resolved"


def test_set_state_hook_runs_cleanup(steward_home):
    store = Store()
    j = _job(store, JobState.ACCEPTED)
    a = approvals.open_approval(store, j.job_id, "x", kind="delivery")
    store.set_state(j.job_id, JobState.RELEASED, what="operator released", evidence={}, rule="op",
                    judgment=Judgment.human)
    assert approvals.get_approval(store, j.job_id, a["id"])["status"] == "stale"


def test_cleanup_exception_never_blocks_transition(steward_home, monkeypatch):
    store = Store()
    j = _job(store, JobState.ACCEPTED)
    monkeypatch.setattr(cleanup, "_close_approvals", lambda *a, **k: 1 / 0)
    store.set_state(j.job_id, JobState.RELEASED, what="operator released", evidence={}, rule="op",
                    judgment=Judgment.human)
    assert store.get_job(j.job_id).state == JobState.RELEASED
    assert any(e.what == "cleanup_failed" for e in store.events(j.job_id))


def test_set_state_hook_resolves_blockers_at_transition(steward_home):
    store = Store()
    j = _job(store, JobState.ACCEPTED)
    blockers.raise_blocker(store, j.job_id, kind="verification_unverified", subject="v", claim="c",
                           evidence=["e1"], origin_actor=ownership.CONTROLLER, now=time.time())
    store.set_state(j.job_id, JobState.RELEASED, what="operator released", evidence={}, rule="op",
                    judgment=Judgment.human)
    assert blockers.open_blockers(store, j.job_id) == []
