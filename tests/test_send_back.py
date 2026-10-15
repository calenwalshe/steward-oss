"""`steward send-back` (rework): NEEDS_HUMAN -> RUNNING with a fresh worker attempt in the same
worktree; all scratch store / fake runner / fake launch seam, nothing touches Herdr."""

from __future__ import annotations

import json

import pytest

from steward import approvals, cli, mcp_auth, mcp_server, phone, watchers
from steward.api import StewardAPI
from steward.contract import Contract, load_contract, store_contract
from steward.control_pane import ControlPane
from steward.models import JobState, Judgment
from steward.recovery import RecoveryPolicy
from steward.runner import Runner
from steward.runner_exec import CommandResult, FakeRunner
from steward.steering import Steering
from steward.store import Store
from steward.worker import FakeWorker, WorkerObservation

_DONE = WorkerObservation(lifecycle="idle", output_tail="STEWARD-DONE", pid_alive=True, ts="t")
_TAB = json.dumps({"result": {"tab": {"tab_id": "w3T:t9"}, "root_pane": {"pane_id": "w3T:p9"}}})


_HEAD = ["sha-1"]  # the worktree HEAD; a rework round that finishes commits something new, so a test moves it


def _git(argv, cwd):
    out = _HEAD[0] + "\n" if argv[:2] == ["git", "rev-parse"] else "base-0\n" if argv[:2] == ["git", "merge-base"] else ""
    return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=out, stderr="", duration_s=0.0)


class _Factory:
    def __init__(self):
        self.created: list[FakeWorker] = []

    def __call__(self, existing_ref):
        w = FakeWorker(script=[_DONE])
        self.created.append(w)
        return w


def _clock():
    t = [1000.0]

    def now():
        t[0] += 1.0
        return t[0]

    return now


@pytest.fixture(autouse=True)
def _human_acceptance(monkeypatch):
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "human")


def _needs_human(store):
    """A job that ran one attempt, reported done, and is parked at NEEDS_HUMAN."""
    _HEAD[0] = "sha-1"
    job = store.create_job("do the thing", "/repo")
    store_contract(store, job.job_id, Contract(goal="do the thing", done_when=["done"]))
    factory = _Factory()
    runner = Runner(store, factory, FakeRunner(_git), None, policy=RecoveryPolicy(), clock=_clock())
    runner.start(job.job_id)
    Steering(store).record(job.job_id, "focus", "prefer the small fix", origin="cli")
    report = runner.tick(job.job_id)
    assert report.state == "NEEDS_HUMAN"
    return job, runner, factory


def _pending_acceptance(store, jid):
    return [a for a in approvals.list_approvals(store, jid, status="pending") if a["kind"] == "acceptance"]


def _send(store, jid, text, *extra, origin="cli"):
    return cli.main(["send-back", jid, text, "--origin", origin, *extra])


def test_refuses_when_not_needs_human(steward_home, capsys):
    store = Store()
    job = store.create_job("g", "/repo")
    store_contract(store, job.job_id, Contract(goal="g", done_when=["d"]))
    rc = _send(store, job.job_id, "more")
    assert rc == 2
    assert "needs NEEDS_HUMAN" in capsys.readouterr().err
    assert store.get_job(job.job_id).state == JobState.CONTRACTED
    assert cli.main(["send-back", "nope", "x"]) == 1


def test_send_back_closes_acceptance_without_stopping_and_records_focus(steward_home, monkeypatch):
    store = Store()
    job, _, _ = _needs_human(store)
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w3T")
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: FakeRunner(lambda a, c: CommandResult(
        argv=a, cwd=str(c), exit_code=0, stdout=_TAB if a[:3] == ["herdr", "tab", "create"] else "", stderr="", duration_s=0.0)))
    assert len(_pending_acceptance(store, job.job_id)) == 1

    assert _send(store, job.job_id, "also handle the empty case", origin="moshi") == 0

    j = store.get_job(job.job_id)
    assert j.state == JobState.RUNNING
    assert not _pending_acceptance(store, job.job_id)
    closed = approvals.list_approvals(store, job.job_id, status="denied")[0]
    assert closed["closed_reason"] == "sent back"
    events = store.events(job.job_id)
    assert not any(e.what == "acceptance rejected" for e in events)
    sb = [e for e in events if e.what == "sent_back"][0]
    assert sb.judgment == Judgment.human and sb.evidence["by"] == "moshi"
    assert sb.evidence["instructions"] == "also handle the empty case"
    rev = Steering(store).all_instructions(job.job_id)[-1]
    assert (rev["verb"], rev["text"], rev["origin"]) == ("focus", "also handle the empty case", "moshi")
    assert load_contract(store, job.job_id).goal == "do the thing"  # focus does not touch the goal
    assert store.kv_get(job.job_id, "rework_pending")["rev"] == rev["rev"]


def test_goal_append_updates_contract_and_uses_goal_verb(steward_home, monkeypatch, capsys):
    store = Store()
    job, _, _ = _needs_human(store)
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w3T")
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: FakeRunner(_git))
    _send(store, job.job_id, "cover the CLI too", "--goal-append")
    c = load_contract(store, job.job_id)
    assert "REWORK (" in c.goal and c.goal.startswith("do the thing") and "cover the CLI too" in c.goal
    assert Steering(store).all_instructions(job.job_id)[-1]["verb"] == "goal"
    assert "REWORK (" in capsys.readouterr().out  # re-rendered contract is printed


def test_dispatches_new_tab_via_launch_seam_and_closes_old_tab(steward_home, monkeypatch, capsys):
    store = Store()
    job, _, _ = _needs_human(store)
    store.kv_set(job.job_id, "herdr_tab", "w3T:t2")
    store.kv_set(job.job_id, "launch_args", {"worker": "fabric", "kind": "claude", "agent_args": ["--model", "sonnet"],
                                              "workspace": "w3T", "interval": 15.0, "sock": "/tmp/x.sock", "keep_pane": False})
    runner = FakeRunner(lambda a, c: CommandResult(argv=a, cwd=str(c), exit_code=0,
                        stdout=_TAB if a[:3] == ["herdr", "tab", "create"] else "", stderr="", duration_s=0.0))
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: runner)
    monkeypatch.delenv("HERDR_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)

    assert _send(store, job.job_id, "redo it") == 0

    argvs = [c["argv"] for c in runner.calls]
    create = next(a for a in argvs if a[:3] == ["herdr", "tab", "create"])
    assert create[create.index("--workspace") + 1] == "w3T"
    assert create[create.index("--cwd") + 1] == "/repo"
    run = next(a for a in argvs if a[:3] == ["herdr", "pane", "run"] and " run " in a[4])
    assert f"run {job.job_id}" in run[4] and "--agent-arg=--model" in run[4]
    assert ["herdr", "tab", "close", "w3T:t2"] in argvs
    assert store.kv_get(job.job_id, "herdr_tab") == "w3T:t9"
    assert "tab=w3T:t9" in capsys.readouterr().out


def test_model_flag_sets_agent_args_for_the_new_attempt(steward_home, monkeypatch, capsys):
    store = Store()
    job, _, _ = _needs_human(store)
    store.kv_set(job.job_id, "launch_args", {"worker": "fabric", "kind": "claude", "agent_args": ["--model", "sonnet"],
                                              "workspace": "w3T", "interval": 15.0, "sock": "/tmp/x.sock", "keep_pane": False})
    runner = FakeRunner(lambda a, c: CommandResult(argv=a, cwd=str(c), exit_code=0,
                        stdout=_TAB if a[:3] == ["herdr", "tab", "create"] else "", stderr="", duration_s=0.0))
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: runner)
    ns = cli._build_parser().parse_args(["send-back", job.job_id, "redo", "--model", "opus"])
    assert cli._op_and_args_for(ns) == ("send_back", {"job_id": job.job_id, "instructions": "redo", "goal_append": False, "model": "opus"})
    assert _send(store, job.job_id, "redo", "--model", "opus") == 0
    run = next(c["argv"] for c in runner.calls if c["argv"][:3] == ["herdr", "pane", "run"] and " run " in c["argv"][4])
    assert "--agent-arg=--model" in run[4] and "--agent-arg=opus" in run[4] and "sonnet" not in run[4]


def test_no_workspace_refuses_before_changing_anything(steward_home, monkeypatch, capsys):
    store = Store()
    job, _, _ = _needs_human(store)
    monkeypatch.delenv("HERDR_WORKSPACE_ID", raising=False)
    assert _send(store, job.job_id, "x") == 2
    assert store.get_job(job.job_id).state == JobState.NEEDS_HUMAN
    assert len(_pending_acceptance(store, job.job_id)) == 1


def test_new_attempt_in_same_worktree_gets_all_prior_steering_and_cycles_twice(steward_home):
    store = Store()
    job, runner, factory = _needs_human(store)
    jid = job.job_id
    first_worktree = factory.created[0].worktree
    assert len(store.kv_get(jid, "attempts")) == 1

    for n, text in enumerate(["round two fixes", "round three fixes"], start=2):
        res = _direct_send_back(store, jid, text)
        assert res["accepted"] and store.get_job(jid).state == JobState.RUNNING
        _HEAD[0] = f"sha-{n}"  # the reworked attempt commits new work
        runner.start_rework(jid)  # what `steward run` does in the new tab

        worker = factory.created[-1]
        assert worker.worktree == first_worktree  # same worktree/branch
        brief = worker.started_brief
        assert "do the thing" in brief and "prefer the small fix" in brief  # goal + prior steering
        assert f"REWORK: SENT BACK BY SAM" in brief and text in brief
        attempts = store.kv_get(jid, "attempts")
        assert len(attempts) == n and attempts[-1]["status"] == "running" and attempts[-2]["status"] == "stopped"
        assert store.kv_get(jid, "rework_pending") is None
        # nothing is re-sent as a steer: the brief carried it, and it is acknowledged
        assert all(i["status"] == "acknowledged" for i in Steering(store).all_instructions(jid))
        assert any(e.what == "worker dispatch started" and e.evidence.get("rework") for e in store.events(jid))

        report = runner.tick(jid)  # worker reports done again -> verification -> closeout
        assert report.action == "closeout" and report.state == "NEEDS_HUMAN"
        assert len(_pending_acceptance(store, jid)) == 1
        assert worker.delivered == []


def test_resume_all_skips_a_job_awaiting_rework(steward_home):
    store = Store()
    job, runner, factory = _needs_human(store)
    _direct_send_back(store, job.job_id, "again")
    assert runner.resume_all([job.job_id]) == []


def test_exhausted_recovery_budget_restarts_on_rework(steward_home):
    store = Store()
    job, runner, _ = _needs_human(store)
    store.kv_set(job.job_id, "recoveries_used", 3)
    _direct_send_back(store, job.job_id, "again")
    runner.start_rework(job.job_id)
    assert store.kv_get(job.job_id, "recoveries_used") == 0


def test_cmd_run_starts_rework_for_a_sent_back_job(steward_home, monkeypatch):
    store = Store()
    job, _, _ = _needs_human(store)
    _direct_send_back(store, job.job_id, "again")
    (steward_home / "worktrees" / job.job_id).mkdir(parents=True)
    assert cli.main(["run", job.job_id, "--worker", "fake", "--once"]) == 0
    assert len(store.kv_get(job.job_id, "attempts")) == 2
    assert store.kv_get(job.job_id, "rework_pending") is None


def _direct_send_back(store, jid, text, **kw):
    from steward import sendback
    return sendback.send_back(store, jid, text, **kw)


def test_relay_op_and_origin_parse(steward_home):
    ns = cli._build_parser().parse_args(["send-back", "J", "do x", "--goal-append", "--origin", "moshi"])
    assert cli._op_and_args_for(ns) == ("send_back", {"job_id": "J", "instructions": "do x", "goal_append": True})
    assert cli._origin_of(ns) == "moshi"
    ns = cli._build_parser().parse_args(["send-back", "J", "do x"])
    assert cli._op_and_args_for(ns)[1]["goal_append"] is False
    assert "send_back" in cli._build_control_handlers(Store())


def test_feed_line(steward_home):
    store = Store()
    job, _, _ = _needs_human(store)
    _direct_send_back(store, job.job_id, "please\nadd tests")
    ev = [e for e in store.events(job.job_id) if e.what == "sent_back"][0]
    assert watchers.format_event(ev) == ("job", "SENT BACK: please add tests")


# -- phone -----------------------------------------------------------------------------


def test_phone_S_prompts_and_runs_send_back(steward_home):
    store = Store()
    job, _, _ = _needs_human(store)
    calls = []
    p = phone.Phone(store, run_cli=lambda argv: (calls.append(argv), (0, "ok"))[1], tokens_fn=lambda s: None)
    p.handle("1")
    p.handle("S")
    assert p.mode == "input" and p.purpose == "sendback"
    for ch in "fix it":
        p.handle(ch)
    p.handle("enter")
    assert calls == [["send-back", job.job_id, "fix it", "--origin", "moshi"]]


def test_phone_S_only_on_needs_human(steward_home):
    store = Store()
    job = store.create_job("g", "/repo")
    p = phone.Phone(store, run_cli=lambda argv: (0, "ok"), tokens_fn=lambda s: None)
    p.handle("1")
    p.handle("S")
    assert p.mode is None and "NEEDS_HUMAN" in p.note


# -- MCP -------------------------------------------------------------------------------


def _mcp(store, steward_home):
    def relay(op, args, *, origin, request_id):
        pane = ControlPane(store, {"send_back": lambda cmd: {"accepted": True, "detail": "ok", "args": cmd["args"]}}, print_fn=lambda _l: None)
        return pane.execute({"request_id": request_id or "r1", "op": op, "args": args, "origin": origin})

    server = mcp_server.MCPServer(mcp_server.build_tools(StewardAPI(store, relay=relay)), home=steward_home)
    token, _ = mcp_auth.create_token(list(mcp_auth.SCOPES), home=steward_home)
    info = mcp_auth.authorize(f"Bearer {token}", home=steward_home)

    def call(name, arguments):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}).encode()
        return json.loads(server.handle_authenticated(body, info)[1])

    return server, call


def test_mcp_send_back_job_registered_with_approve_scope(steward_home):
    store = Store(steward_home)
    job, _, _ = _needs_human(store)
    server, call = _mcp(store, steward_home)
    spec = server.tools["send_back_job"]
    assert spec.required_scope == "runner:approve"
    assert "confirm" in spec.description.lower() and "Sam" in spec.description
    ok = call("send_back_job", {"job_id": job.job_id, "instructions": "redo", "goal_append": True})
    assert ok["result"]["structuredContent"]["args"] == {"job_id": job.job_id, "instructions": "redo", "goal_append": True}
    other = store.create_job("g", "/repo")
    assert call("send_back_job", {"job_id": other.job_id, "instructions": "x"})["error"]["code"] == mcp_server.INVALID_PARAMS
    assert "error" in call("send_back_job", {"job_id": job.job_id})


def test_send_back_records_the_passed_judgment_and_defaults_to_human(steward_home):
    from steward import sendback
    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    store.set_state(job.job_id, JobState.CONTRACTED, what="t", evidence={}, rule="t", judgment=Judgment.code)
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    store.set_state(job.job_id, JobState.WORKER_FINISHED, what="t", evidence={}, rule="t", judgment=Judgment.code)
    store.set_state(job.job_id, JobState.NEEDS_HUMAN, what="t", evidence={}, rule="t", judgment=Judgment.code)
    res = sendback.send_back(store, job.job_id, "retry", origin="tech_lead:astra-tech-lead",
                             judgment=Judgment.model_informed_code_authorized)
    assert res["accepted"]
    ev = [e for e in store.events(job.job_id) if e.what == "sent_back"][-1]
    assert ev.judgment == Judgment.model_informed_code_authorized and ev.evidence["by"] == "tech_lead:astra-tech-lead"
    assert store.kv_get(job.job_id, sendback.REWORK_KEY)["by"] == "tech_lead:astra-tech-lead"
    # the state transition event carries the same judgment
    assert store.events(job.job_id)[-1].judgment == Judgment.model_informed_code_authorized
    # default stays human
    store.set_state(job.job_id, JobState.NEEDS_HUMAN, what="t", evidence={}, rule="t", judgment=Judgment.code)
    store.kv_set(job.job_id, sendback.REWORK_KEY, None)
    sendback.send_back(store, job.job_id, "again")
    assert store.events(job.job_id)[-1].judgment == Judgment.human
