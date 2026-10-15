from steward import approvals, coordinator_guard as cg, resolver
from steward.models import JobState
from steward.store import Store


def _job(store):
    j = store.create_job("goal", "/tmp/r")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (JobState.NEEDS_HUMAN.value, j.job_id))
    return store.get_job(j.job_id)


def _cmd(op, **args):
    return {"op": op, "origin": "coordinator", "args": args, "request_id": "r1"}


def _perm(store, j, question, command):
    rec = approvals.open_approval(store, j.job_id, question, kind="permission")
    store.kv_set(j.job_id, f"hold:{rec['id']}", {"tool": "Bash", "input": {"command": command}})
    return rec


def test_floor_permission_deny_refused(steward_home):
    store = Store(); j = _job(store)
    rec = _perm(store, j, "git push origin main", "git push origin main")
    why = cg.guard(store, _cmd("deny", job_id=j.job_id, approval_id=rec["id"]))
    assert why and "floor" in why


def test_non_floor_permission_deny_allowed(steward_home):
    store = Store(); j = _job(store)
    rec = _perm(store, j, "ls", "ls -la")
    assert cg.guard(store, _cmd("deny", job_id=j.job_id, approval_id=rec["id"])) is None


def test_deny_classify_failure_refused(steward_home, monkeypatch):
    store = Store(); j = _job(store)
    rec = _perm(store, j, "ls", "ls -la")

    def boom(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(resolver, "classify", boom)
    why = cg.guard(store, _cmd("deny", job_id=j.job_id, approval_id=rec["id"]))
    assert why and "cannot classify" in why
