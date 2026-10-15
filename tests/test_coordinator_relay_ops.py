import json
import time

from steward import cli, coordinator_link as cl, health, push
from steward.models import Judgment
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store

SESSION = "37355ed2-dc4b-43b0-9456-08c53ed13bd3"
PANE = "w4Q:p1"


def _runner():
    def respond(argv, cwd):
        out = json.dumps({"result": {"panes": [{"pane_id": PANE, "agent_session": {"agent": "claude", "kind": "id", "value": SESSION}}]}}) \
            if argv[:3] == ["herdr", "pane", "list"] else ""
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=out, stderr="", duration_s=0.0)
    return FakeRunner(respond)


def _frame(op, **args):
    return {"op": op, "origin": "coordinator", "request_id": "r", "args": args}


def _ep(job, outcome="resolved", **kw):
    base = {"job_id": job.job_id, "episode_id": "ep1", "trigger": "needs_human:x", "outcome": outcome,
            "diagnosis": "d", "actions": [], "rule_proposal_id": None, "model_used": False}
    return _frame("coordinator_episode", **{**base, **kw})


def test_episode_writes_event(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r")
    h = cli._build_control_handlers(store)
    out = h["coordinator_episode"](_ep(j, diagnosis="x" * 400, actions=["a"] * 12, rule_proposal_id="T1"))
    assert out["accepted"]
    ev = store.events_of(j.job_id, ("coordinator_episode",))[-1]
    assert ev.rule == "coordinator" and ev.judgment == Judgment.code
    assert len(ev.evidence["diagnosis"]) == 300 and len(ev.evidence["actions"]) == 8


def test_episode_model_used_and_unknown_job(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r")
    h = cli._build_control_handlers(store)
    h["coordinator_episode"](_ep(j, model_used=True))
    assert store.events_of(j.job_id, ("coordinator_episode",))[-1].judgment == Judgment.model_informed_code_authorized
    out = h["coordinator_episode"](_frame("coordinator_episode", job_id="nope", episode_id="e", trigger="t", outcome="resolved"))
    assert out["accepted"] is False and out["error_kind"] == "unknown_job"
    assert h["coordinator_hold"](_frame("coordinator_hold", job_id="nope", episode_id="e", trigger="t"))["error_kind"] == "unknown_job"


def test_hold_open_duplicate_and_resolved_closes(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r")
    h = cli._build_control_handlers(store)
    a = _frame("coordinator_hold", job_id=j.job_id, episode_id="ep1", trigger="needs_human:x")
    assert h["coordinator_hold"](a)["accepted"]
    rec = cl.hold(store, j.job_id)
    assert rec["episode_id"] == "ep1" and rec["state"] == "open" and rec["job_state"] == j.state.value
    assert h["coordinator_hold"](a).get("duplicate")
    assert [e.what for e in store.events(j.job_id)].count("coordinator_hold_opened") == 1
    h["coordinator_episode"](_ep(j, "resolved"))
    assert cl.hold(store, j.job_id) is None
    assert store.kv_get(j.job_id, "coordinator_hold")["how"] == "resolved"
    assert "coordinator_hold_closed" in [e.what for e in store.events(j.job_id)]


def test_asked_delivers_line_and_closes(steward_home, monkeypatch):
    store = Store(); j = store.create_job("g", "/tmp/r")
    store.kv_set(j.job_id, "created_by", {"session": SESSION, "herdr_pane": PANE, "cwd": "/x"})
    r = _runner()
    monkeypatch.setattr(cli, "SubprocessRunner", lambda *a, **k: r)
    h = cli._build_control_handlers(store)
    h["coordinator_hold"](_frame("coordinator_hold", job_id=j.job_id, episode_id="ep1", trigger="t"))
    h["coordinator_episode"](_ep(j, "asked", ask="need your call on X"))
    rec = store.kv_get(j.job_id, "coordinator_hold")
    assert rec["state"] == "delivered" and rec["how"] == "asked" and rec["delivery"] == "delivered"


def test_asked_without_hold_is_event_only(steward_home, monkeypatch):
    store = Store(); j = store.create_job("g", "/tmp/r")
    calls = []
    monkeypatch.setattr(cl, "deliver_ask", lambda *a, **k: calls.append(1) or "delivered")
    h = cli._build_control_handlers(store)
    assert h["coordinator_episode"](_ep(j, "gave_up"))["accepted"]
    assert not calls and store.events_of(j.job_id, ("coordinator_episode",))


def test_asked_skipped_delivery_recorded(steward_home, monkeypatch):
    store = Store(); j = store.create_job("g", "/tmp/r")  # no creator
    monkeypatch.setattr(cli, "SubprocessRunner", lambda *a, **k: _runner())
    h = cli._build_control_handlers(store)
    h["coordinator_hold"](_frame("coordinator_hold", job_id=j.job_id, episode_id="ep1", trigger="t"))
    h["coordinator_episode"](_ep(j, "gave_up"))
    rec = store.kv_get(j.job_id, "coordinator_hold")
    assert rec["state"] == "closed" and rec["delivery"] == "skipped:no_creator"


def test_needs_you_push_event(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r")
    store.kv_set(j.job_id, "created_by", {"session": SESSION, "herdr_pane": PANE, "cwd": "/x"})
    assert "needs_you" in push.EVENTS
    r = _runner()
    assert push.notify_creator(store, j.job_id, "needs_you", r, facts={"reason": "pick A or B"}) == "delivered"
    text = json.dumps(r.calls)
    assert "needs you: pick A or B" in text and "your call; see the dash" in text


def test_coordinator_revision_is_exempt():
    from steward import notify
    assert "coordinator:" in notify.OWNERSHIP_EXEMPT_REVISIONS


def test_health_extras_only_when_asked(tmp_path, monkeypatch, steward_home):
    monkeypatch.setenv("STEWARD_COORDINATOR_STATE", str(tmp_path))
    store = Store()
    kw = dict(ping=False, head_fn=lambda _r: "deadbeef")
    base = health.check_all(store, **kw)
    assert "coordinator" not in [c.name for c in base] and [c.name for c in base] == list(health.COMPONENTS)
    comps = {c.name: c for c in health.check_all(store, extras=True, **kw)}
    assert comps["coordinator"].ok is None
    cl.write_heartbeat(cursor=1, head="deadbeef", root="/r")
    assert {c.name: c for c in health.check_all(store, extras=True, **kw)}["coordinator"].ok is True
    cl.write_heartbeat(cursor=1, head="deadbeef", root="/r", now=time.time() - 500)
    c = {c.name: c for c in health.check_all(store, extras=True, **kw)}["coordinator"]
    assert c.ok is False and "silent" in c.detail


def test_cli_parser_maps_hold_and_episode():
    ns = cli._build_parser().parse_args(["coordinator", "episode", "J", "--episode", "e", "--trigger", "t",
                                        "--outcome", "asked", "--diagnosis", "d", "--action", "a", "--action", "b",
                                        "--rule-task", "T9", "--model-used", "--ask", "q"])
    assert cli._op_and_args_for(ns) == ("coordinator_episode", {
        "job_id": "J", "episode_id": "e", "trigger": "t", "outcome": "asked", "diagnosis": "d",
        "actions": ["a", "b"], "rule_proposal_id": "T9", "model_used": True, "ask": "q"})
    ns = cli._build_parser().parse_args(["coordinator", "hold", "J", "--episode", "e", "--trigger", "t"])
    assert cli._op_and_args_for(ns) == ("coordinator_hold", {"job_id": "J", "episode_id": "e", "trigger": "t"})


def test_status_without_package_exits_2(steward_home, capsys, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "steward.coordinator.__main__", None)
    assert cli.main(["coordinator", "status"]) == 2
    assert "coordinator package not installed" in capsys.readouterr().out
