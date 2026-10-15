import pytest

from steward import actor, approvals, coordinator_guard as cg
from steward.models import JobState
from steward.store import Store


def _job(store, state=JobState.NEEDS_HUMAN, goal="goal"):
    j = store.create_job(goal, "/tmp/r")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state.value, j.job_id))
    return store.get_job(j.job_id)


def _cmd(op, **args):
    return {"op": op, "origin": "coordinator", "args": args, "request_id": "r1"}


def test_origin_classifies_as_operator_agent_never_owner():
    assert actor.classify("coordinator").label() == "operator_agent:coordinator"
    assert actor.classify("coordinator", "owner:sam").kind != actor.Actor.OWNER


def test_other_origins_untouched(steward_home):
    assert cg.guard(Store(), {"op": "accept", "origin": "cli", "args": {}}) is None


@pytest.mark.parametrize("op", ["accept", "release", "incorporate", "launch", "new", "self_exec", "deadline", "grant"])
def test_guard_refuses_off_list_ops(steward_home, op):
    store = Store(); j = _job(store)
    assert cg.guard(store, _cmd(op, job_id=j.job_id))


def test_guard_allows_nudge_but_never_urgent_or_other_verbs(steward_home):
    store = Store(); j = _job(store, JobState.RUNNING)
    assert cg.guard(store, _cmd("steer", job_id=j.job_id, text="t", verb="nudge")) is None
    assert "urgent" in cg.guard(store, _cmd("steer", job_id=j.job_id, text="t", verb="nudge", urgent=True))
    assert cg.guard(store, _cmd("goal", job_id=j.job_id, text="t"))
    assert cg.guard(store, _cmd("priority", job_id=j.job_id, text="t"))
    assert cg.guard(store, _cmd("steer", job_id=j.job_id, text="t", verb="goal"))


def test_guard_approvals(steward_home):
    store = Store(); j = _job(store)
    acc = approvals.open_approval(store, j.job_id, "Accept?", kind="acceptance")
    rec = approvals.open_approval(store, j.job_id, "recovery budget exhausted", kind="general")
    odd = approvals.open_approval(store, j.job_id, "something else", kind="general")
    assert cg.guard(store, _cmd("approve", job_id=j.job_id, approval_id=acc["id"]))
    assert cg.guard(store, _cmd("approve", job_id=j.job_id, approval_id=rec["id"])) is None
    assert cg.guard(store, _cmd("approve", job_id=j.job_id, approval_id=odd["id"]))
    assert cg.guard(store, _cmd("deny", job_id=j.job_id, approval_id=rec["id"]))


def test_guard_refuses_floor_permission(steward_home):
    store = Store(); j = _job(store)
    floor = approvals.open_approval(store, j.job_id, "git push origin main", kind="permission")
    store.kv_set(j.job_id, f"hold:{floor['id']}", {"tool": "Bash", "input": {"command": "git push origin main"}})
    assert cg.guard(store, _cmd("approve", job_id=j.job_id, approval_id=floor["id"]))


def test_guard_answer_never_delivers(steward_home):
    store = Store(); j = _job(store)
    for k in ("then_accept", "write_source", "and_continue", "done"):
        assert cg.guard(store, _cmd("answer", job_id=j.job_id, **{k: True}))
    assert cg.guard(store, _cmd("answer", job_id=j.job_id, answers=[{"qid": "q", "defer": True}]))


def test_guard_answer_citation_rules(steward_home):
    store = Store(); j = _job(store)
    store.kv_set(j.job_id, "questions", [{"qid": "q", "recommended": ["a"], "options": [{"key": "a"}, {"key": "b"}]}])
    g = lambda **item: cg.guard(store, _cmd("answer", job_id=j.job_id, answers=[{"qid": "q", **item}]))
    assert g(option="recommended") is None
    assert g(option="a") is None
    assert g(option="b")
    assert g(option="b", note="cite: spec D3") is None
    assert g(other="free text")
    assert g(other="free text\ncite: plan T1") is None
    assert cg.guard(store, _cmd("answer", job_id=j.job_id, accept_recommended=True, qids=["q"])) is None


def test_guard_send_back_limits(steward_home):
    store = Store(); j = _job(store)
    assert cg.guard(store, _cmd("send_back", job_id=j.job_id, text="go")) is None
    assert cg.guard(store, _cmd("send_back", job_id=j.job_id, text="go", model="opus"))
    assert cg.guard(store, _cmd("send_back", job_id=j.job_id, text="go", goal_append="x"))


def test_guard_stop_needs_merge_fixup_with_done_parent(steward_home):
    store = Store()
    parent = _job(store, JobState.RELEASED)
    fix = _job(store, JobState.NEEDS_HUMAN, goal="Merge `x` into fleet-registry")
    store.kv_set(fix.job_id, "autopilot_followup", parent.job_id)
    assert cg.guard(store, _cmd("stop", job_id=fix.job_id)) is None
    other = _job(store, JobState.RUNNING)
    assert "merge fix-ups" in cg.guard(store, _cmd("stop", job_id=other.job_id))
    live_parent = _job(store, JobState.RUNNING)
    fix2 = _job(store, JobState.NEEDS_HUMAN, goal="Merge `y` into fleet-registry")
    store.kv_set(fix2.job_id, "autopilot_followup", live_parent.job_id)
    assert "parent" in cg.guard(store, _cmd("stop", job_id=fix2.job_id))


def test_guard_refuses_everything_while_kill_file_exists(steward_home, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_COORDINATOR_STATE", str(tmp_path))
    (tmp_path / "KILL").write_text("x")
    store = Store(); j = _job(store, JobState.RUNNING)
    assert "KILL" in cg.guard(store, _cmd("steer", job_id=j.job_id, text="t", verb="nudge"))


def test_record_action_writes_event(steward_home):
    store = Store(); j = _job(store, JobState.RUNNING)
    cg.record_action(store, _cmd("steer", job_id=j.job_id), {"accepted": False, "detail": "refused: x"})
    cg.record_action(store, {"op": "steer", "origin": "cli", "args": {"job_id": j.job_id}}, {"accepted": True})
    evs = [e for e in store.events(j.job_id) if e.what == "coordinator_action"]
    assert len(evs) == 1 and evs[0].evidence["accepted"] is False
