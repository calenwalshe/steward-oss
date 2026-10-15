"""Final-review fixes for coordinator Phase A (C1, I1-I5, minors a-c)."""
import json
import time

import pytest

from steward import approvals, cli, coordinator_guard as cg, coordinator_link as cl, push
from steward.models import JobState, Judgment
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store

SESSION = "37355ed2-dc4b-43b0-9456-08c53ed13bd3"
PANE = "w4Q:p1"


def _set_state(store, job_id, state):
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state.value, job_id))


def _parked(store, goal="g"):
    j = store.create_job(goal, "/tmp/r")
    _set_state(store, j.job_id, JobState.NEEDS_HUMAN)
    return store.get_job(j.job_id)


def _cmd(op, **args):
    return {"op": op, "origin": "coordinator", "args": args, "request_id": "r1"}


def _frame(op, **args):
    return {"op": op, "origin": "coordinator", "request_id": "r", "args": args}


def _runner():
    def respond(argv, cwd):
        out = json.dumps({"result": {"panes": [{"pane_id": PANE, "agent_session": {"agent": "claude", "kind": "id", "value": SESSION}}]}}) \
            if argv[:3] == ["herdr", "pane", "list"] else ""
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=out, stderr="", duration_s=0.0)
    return FakeRunner(respond)


def _texts(r):
    return [c["argv"][4] for c in r.calls if c["argv"][:3] == ["herdr", "pane", "send-text"]]


def _coord_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_COORDINATOR_STATE", str(tmp_path / "coord"))


# -- C1 ---------------------------------------------------------------------------------------------------------------


def test_c1_guard_refuses_send_back_over_acceptance_approval(steward_home):
    store = Store(); j = _parked(store)
    assert cg.guard(store, _cmd("send_back", job_id=j.job_id, instructions="x")) is None
    approvals.open_approval(store, j.job_id, "Accept?", kind="acceptance")
    assert "acceptance" in cg.guard(store, _cmd("send_back", job_id=j.job_id, instructions="x"))


def test_c1_guard_refuses_send_back_over_floor_general_approval(steward_home):
    store = Store(); j = _parked(store)
    floor = approvals.open_approval(store, j.job_id, "git push origin main", kind="general")
    store.kv_set(j.job_id, f"hold:{floor['id']}", {"tool": "Bash", "input": {"command": "git push origin main"}})
    assert "floor" in cg.guard(store, _cmd("send_back", job_id=j.job_id, instructions="x"))


def test_c1_send_back_by_coordinator_is_not_recorded_as_human(steward_home, monkeypatch):
    store = Store(); j = _parked(store)
    approvals.open_approval(store, j.job_id, "recovery budget exhausted", kind="general")
    seen = {}

    def fake(store_, job_id, instructions, **kw):
        seen.update(kw)
        return {"accepted": True, "detail": "ok", "live": True}

    monkeypatch.setattr(cli.sendback_module, "send_back", fake)
    h = cli._build_control_handlers(store)
    h["send_back"](_frame("send_back", job_id=j.job_id, instructions="x", workspace="w"))
    assert seen["judgment"] != Judgment.human
    seen.clear()
    h["send_back"]({"op": "send_back", "origin": "cli", "request_id": "r", "args": {"job_id": j.job_id, "instructions": "x", "workspace": "w"}})
    assert seen["judgment"] == Judgment.human


def test_c1_real_send_back_does_not_deny_as_human(steward_home):
    store = Store(); j = _parked(store)
    rec = approvals.open_approval(store, j.job_id, "recovery budget exhausted", kind="general")
    from steward import sendback
    sendback.send_back(store, j.job_id, "do x", origin="coordinator", judgment=Judgment.model_informed_code_authorized)
    assert approvals.get_approval(store, j.job_id, rec["id"])["decided_by"] == "coordinator"


# -- I1 ---------------------------------------------------------------------------------------------------------------


def test_i1_park_reason_uses_park_event_not_newest_detail(steward_home):
    store = Store(); j = _parked(store)
    store.append_event(j.job_id, what="verify_rounds_exhausted", evidence={"detail": "tests fail after 3 rounds"},
                       rule="verify_rounds_exhausted", judgment=Judgment.code)
    store.append_event(j.job_id, what="creator_notify_skipped", evidence={"reason": "duplicate"}, rule="push",
                       judgment=Judgment.code)
    store.append_event(j.job_id, what="coordinator_hold_opened", evidence={"detail": "hold opened for ep"},
                       rule="coordinator", judgment=Judgment.code)
    assert cl._park_reason(store, j.job_id) == "tests fail after 3 rounds"


def test_i1_park_reason_falls_back_to_needs_human_state_change(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r")
    _set_state(store, j.job_id, JobState.RUNNING)
    store.set_state(j.job_id, JobState.NEEDS_HUMAN, what="worker gave up", evidence={}, rule="x", judgment=Judgment.code)
    store.append_event(j.job_id, what="creator_notify_skipped", evidence={"reason": "duplicate"}, rule="push",
                       judgment=Judgment.code)
    assert cl._park_reason(store, j.job_id) == "worker gave up"


# -- I2 / I4 ----------------------------------------------------------------------------------------------------------


def _ask_ep(job, outcome="asked"):
    return _frame("coordinator_episode", job_id=job.job_id, episode_id="ep1", trigger="needs_human:x", outcome=outcome,
                  diagnosis="d", actions=[], ask="which one?")


def test_i2_ask_after_superseded_hold_is_still_delivered(steward_home, monkeypatch):
    store = Store(); j = _parked(store)
    store.kv_set(j.job_id, "created_by", {"session": SESSION, "herdr_pane": PANE, "cwd": "/x"})
    r = _runner(); monkeypatch.setattr(cli, "SubprocessRunner", lambda *a, **k: r)
    h = cli._build_control_handlers(store)
    h["coordinator_hold"](_frame("coordinator_hold", job_id=j.job_id, episode_id="ep1", trigger="needs_human:x"))
    cl.close_hold(store, j.job_id, "superseded", now=time.time())
    h["coordinator_episode"](_ask_ep(j))
    rec = store.kv_get(j.job_id, cl.HOLD_KEY)
    assert rec["delivery"] == "delivered" and rec["how"] == "asked"
    assert len(_texts(r)) == 1 and "which one?" in _texts(r)[0]
    h["coordinator_episode"](_ask_ep(j, "gave_up"))  # no second line for the same episode
    assert len(_texts(r)) == 1
    assert store.kv_get(j.job_id, cl.HOLD_KEY)["how"] == "asked"


def test_i4_rehold_after_delivery_is_duplicate_and_supersede_emits_event(steward_home):
    store = Store(); j = _parked(store)
    h = cli._build_control_handlers(store)
    a = _frame("coordinator_hold", job_id=j.job_id, episode_id="ep1", trigger="needs_human:x")
    h["coordinator_hold"](a)
    cl.close_hold(store, j.job_id, "asked", now=time.time(), delivery="delivered")
    out = h["coordinator_hold"](a)
    assert out.get("duplicate") and cl.hold(store, j.job_id) is None
    # a different episode over an open hold closes the first one (event)
    b = _frame("coordinator_hold", job_id=j.job_id, episode_id="ep2", trigger="needs_human:x")
    h["coordinator_hold"](b)
    h["coordinator_hold"](_frame("coordinator_hold", job_id=j.job_id, episode_id="ep3", trigger="needs_human:x"))
    closed = [e for e in store.events(j.job_id) if e.what == "coordinator_hold_closed"
              and e.evidence.get("episode_id") == "ep2"]
    assert closed and closed[0].evidence["how"] == "superseded"


# -- I3 ---------------------------------------------------------------------------------------------------------------


def test_i3_orphan_hold_does_not_suppress_notice(steward_home, tmp_path, monkeypatch):
    _coord_env(tmp_path, monkeypatch)
    store = Store(); j = _parked(store); now = time.time()
    cl.write_heartbeat(cursor=1, head="h", root="r", now=now)
    cl.open_hold(store, j.job_id, episode_id="e", trigger="needs_human:x", now=now)
    assert cl.holding(store, j.job_id, now=now)
    with store._conn:
        store._conn.execute("UPDATE jobs SET updated_at=? WHERE job_id=?", ("2099-01-01T00:00:00+00:00", j.job_id))
    assert not cl.holding(store, j.job_id, now=now)


# -- I5 ---------------------------------------------------------------------------------------------------------------


def test_i5_needs_you_ask_text_not_persisted(steward_home):
    store = Store(); j = store.create_job("build", "/repo")
    store.kv_set(j.job_id, "created_by", {"session": SESSION, "herdr_pane": PANE, "cwd": "/x"})
    r = _runner()
    out = push.notify_creator(store, j.job_id, "needs_you", r, facts={"reason": "SECRET-ASK", "action": "SECRET-ACT"})
    assert out == "delivered"
    assert "SECRET-ASK" in _texts(r)[0]
    facts = json.dumps((store.kv_get(j.job_id, "notice") or {}).get("facts"))
    assert "SECRET-ASK" not in facts and "SECRET-ACT" not in facts
    push.notify_creator(store, j.job_id, "stopped", r, facts={})
    assert "SECRET" not in _texts(r)[-1]


# -- minors -----------------------------------------------------------------------------------------------------------


def test_minor_a_accept_recommended_needs_qids(steward_home):
    store = Store(); j = _parked(store)
    assert "qids" in cg.guard(store, _cmd("answer", job_id=j.job_id, accept_recommended=True))
    assert cg.guard(store, _cmd("answer", job_id=j.job_id, accept_recommended=True, qids=["q1"])) is None


def test_minor_b_dash_not_delivered_matches_episode(steward_home):
    store = Store(); j = _parked(store)
    store.append_event(j.job_id, what="coordinator_episode", rule="coordinator", judgment=Judgment.code,
                       evidence={"episode_id": "ep2", "outcome": "asked", "diagnosis": "d", "actions": []})
    store.kv_set(j.job_id, cl.HOLD_KEY, {"state": "closed", "episode_id": "ep1", "delivery": "skipped:pane_gone"})
    assert "not delivered" not in cl.dash_line(store, j.job_id)
    store.kv_set(j.job_id, cl.HOLD_KEY, {"state": "closed", "episode_id": "ep2", "delivery": "skipped:pane_gone"})
    assert "not delivered" in cl.dash_line(store, j.job_id)


def test_minor_c_only_missing_coordinator_package_is_not_installed(steward_home, monkeypatch, capsys):
    import builtins
    store = Store()
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "steward.coordinator.__main__":
            raise ModuleNotFoundError("No module named 'tomli_w'", name="tomli_w")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    args = cli._build_parser().parse_args(["coordinator", "status"])
    with pytest.raises(ModuleNotFoundError):
        cli._cmd_coordinator(store, args)

    def fake2(name, *a, **k):
        if name == "steward.coordinator.__main__":
            raise ModuleNotFoundError("No module named 'steward.coordinator'", name="steward.coordinator")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake2)
    assert cli._cmd_coordinator(store, args) == 2
    assert "not installed" in capsys.readouterr().out
