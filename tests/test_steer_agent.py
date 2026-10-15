"""The quick-steer button and agent: button placement, briefing, allowed-tools scope, origin recording."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys

import pytest
from test_dash_polish import _REPO, _render, _running  # noqa: F401
from test_incorporate import scratch  # noqa: F401  (fixture)

from steward import approvals as approvals_module
from steward import chat as chat_module
from steward import cli, dashact, owner, steer_agent, watchers
from steward.control_pane import ControlPane
from steward.models import JobState
from steward.roles import ModelRoles, load_roles
from steward.runner_exec import CommandResult, FakeRunner
from steward.steering import Steering
from steward.store import Store

CLI = "Bash(python3 -m steward.cli "


def _set_state(store, job, state):
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job.job_id))


@pytest.fixture
def job(steward_home):
    store = Store(steward_home)
    return store, _running(store, goal="Ship the widget. FINISH LINE: tests green.")


# -- the button ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("state", [JobState.RUNNING, JobState.PAUSED, JobState.NEEDS_HUMAN])
def test_steer_button_on_header_row_and_click_map(steward_home, tmp_path, state):
    store = Store(steward_home)
    j = _running(store)
    _set_state(store, j, state)
    lines, cmap = _render(steward_home, tmp_path, 88)
    head = next(i for i, ln in enumerate(lines) if "[ steer ]" in ln)
    col = lines[head].index("[ steer ]") + 1
    m = next(re.fullmatch(rf"btn {head} (\d+) (\d+) steer {j.job_id} -", c) for c in cmap if f"btn {head} " in c and " steer " in c)
    assert (int(m.group(1)), int(m.group(2))) == (col, col + len("[ steer ]") - 1)


def test_steer_button_shown_when_folded(steward_home, tmp_path):
    store = Store(steward_home)
    j = _running(store)
    lines, cmap = _render(steward_home, tmp_path, 88, (dashact.FOLD_PREFIX + j.job_id,))
    head = next(i for i, ln in enumerate(lines) if "[ steer ]" in ln)
    assert "[ steer ]" in lines[head]
    assert any(c.startswith(f"btn {head} ") and f" steer {j.job_id} -" in c for c in cmap)
    assert f"btn {head} 1 3 fold {j.job_id} -" in cmap  # the fold cell is untouched


def test_no_steer_button_on_finished_jobs(steward_home, tmp_path):
    store = Store(steward_home)
    j = _running(store)
    _set_state(store, j, JobState.ACCEPTED)
    lines, cmap = _render(steward_home, tmp_path, 88, ("done:",))
    assert not any("[ steer ]" in ln for ln in lines) and not any(" steer " in c for c in cmap)


@pytest.mark.parametrize("cols", [60, 80, 120])
def test_header_with_button_fits_cols(steward_home, tmp_path, cols):
    _running(Store(steward_home))
    lines, _ = _render(steward_home, tmp_path, cols)
    assert all(len(ln) <= cols for ln in lines)


def test_button_runs_steer_chat_command():
    assert dashact.argv_for("steer", "J1") == ["python3", "-m", "steward.cli", "steer-chat", "J1"]
    ns = cli._build_parser().parse_args(["steer-chat", "J1", "--say", "all well?"])
    assert ns.job == "J1" and ns.say == "all well?"


# -- the agent's config and command -------------------------------------------------------------------------


def test_default_model_is_opus_high_and_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path))
    for v in ("STEWARD_MODEL_STEER", "STEWARD_STEER_EFFORT"):
        monkeypatch.delenv(v, raising=False)
    assert (ModelRoles().steer, ModelRoles().steer_effort) == ("claude-opus-5-5", "high")
    (tmp_path / "models.toml").write_text('steer = "sonnet"\nsteer_effort = "low"\n')
    assert (load_roles().steer, load_roles().steer_effort) == ("sonnet", "low")


def test_plan_command_and_label(job, monkeypatch, tmp_path):
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path))
    store, j = job
    plan = chat_module.build_chat(store, j.job_id, "steer", say="figure out if all is well and continue", runner=FakeRunner())
    assert chat_module.chat_label(plan.role, j.job_id) == f"steer chat {j.job_id[:8]}"
    argv = shlex.split(plan.command.split("&&", 1)[1])
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5" and argv[argv.index("--effort") + 1] == "high"
    assert f"STEWARD_FORCED_ORIGIN=steer-agent" in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[-1] == "figure out if all is well and continue"
    tools = argv[argv.index("--allowedTools") + 1:argv.index("--permission-mode")]
    assert tools == steer_agent.allowed_tools(j.job_id)
    assert shlex.split(chat_module.build_chat(store, j.job_id, "steer", runner=FakeRunner()).command.split("&&", 1)[1])[-1] == owner.fill(steer_agent.OPENER)


def test_system_prompt_states_the_method_and_limits(job):
    _, j = job
    text = steer_agent.system_prompt(j.job_id)
    for needle in ("short instructions", "INVESTIGATE first", "2-4 lines", "--urgent", "git push", "stop, release, accept or"):
        assert needle in text, needle


# -- the briefing -------------------------------------------------------------------------------------------


def test_briefing_contents(job):
    store, j = job
    Steering(store).record(j.job_id, "nudge", "check the flaky test", origin="cli")
    approvals_module.open_approval(store, j.job_id, "Bash: pytest -q", kind="permission")
    store.append_event(j.job_id, what="spend_rate_high", evidence={"usd_per_hour": 9, "limit": 5}, rule="t",
                       judgment=__import__("steward.models", fromlist=["Judgment"]).Judgment.code)
    runner = FakeRunner(lambda argv, cwd: CommandResult(argv, str(cwd), 0, " a.py | 2 +-\n 1 file changed", "", 0.0))
    (store.home / "worktrees" / j.job_id).mkdir(parents=True)
    text = steer_agent.build_briefing(store, j.job_id, runner=runner)
    for needle in ("Goal / finish line: Ship the widget. FINISH LINE: tests green.", "State: RUNNING", "Liveness:", "Worker now:",
                   "Worker's last turns", "Pending steers: rev 1 nudge [cli] check the flaky test", "Pending approvals:",
                   "[approval id ", "Questions waiting for Sam", "Watcher / Jev / steering feed", "SPEND $9",
                   "Efficiency findings:", "Recent events:", "Diff stat:", "a.py | 2 +-", "Spend:"):
        assert needle in text, needle
    assert len(text) <= steer_agent.PRIMER_MAX


# -- allowed-tools scope ------------------------------------------------------------------------------------


def test_allowed_tools_scope():
    tools = steer_agent.allowed_tools("JOB123")
    assert f"{CLI}steer JOB123 *)" in tools  # covers `steer JOB123 "x" --urgent`
    for v in ("pause", "resume", "runner-restart"):
        assert f"{CLI}{v} JOB123*)" in tools
    for v in ("send-back", "answer", "approve", "deny"):
        assert f"{CLI}{v} JOB123 *)" in tools
    for v in ("status", "transcript", "events", "approvals", "tokens", "watchers"):
        assert f"{CLI}{v} JOB123*)" in tools
    for v in ("incorporate", "stop", "accept", "release", "launch", "new", "watcher"):
        assert not any(t.startswith(f"{CLI}{v} ") for t in tools), v
    assert not any("push" in t or "OTHER" in t for t in tools)
    assert "Bash(git diff*)" in tools and "Bash(git push*)" not in tools
    assert all(("JOB123" in t) for t in tools if t.startswith(CLI))
    assert not any(t.startswith(f"{CLI}efficiency JOB123 set") for t in tools)


# -- origin recorded and enforced ---------------------------------------------------------------------------


def test_origin_parser_and_forced_env(monkeypatch):
    ns = cli._build_parser().parse_args(["steer", "J", "x", "--urgent", "--origin", "steer-agent"])
    assert ns.origin == "steer-agent" and ns.urgent
    monkeypatch.setenv("STEWARD_FORCED_ORIGIN", "steer-agent")
    ns = cli._build_parser().parse_args(["pause", "J", "--origin", "cli"])
    assert cli._origin_of(ns) == "steer-agent"  # the agent cannot relabel itself
    monkeypatch.delenv("STEWARD_FORCED_ORIGIN")
    assert cli._origin_of(ns) == "cli"


def test_urgent_steer_origin_recorded_and_in_feed(job):
    store, j = job
    rec = Steering(store).record(j.job_id, "nudge", "stop the loop", origin="steer-agent", urgent=True)
    assert rec.accepted
    ins = Steering(store).all_instructions(j.job_id)[-1]
    assert ins["origin"] == "steer-agent" and ins["urgent"]
    feed = [t for _, _, t in watchers.collect(store.events(j.job_id))]
    assert any("URGENT" in t and "[steer-agent]" in t for t in feed)


def _pane(store, ran):
    def ok(cmd):
        ran.append(cmd["op"])
        return {"accepted": True, "detail": "done"}

    ops = ("pause", "resume", "runner_restart", "approve", "deny", "accept", "stop", "incorporate", "release")
    return ControlPane(store, {op: ok for op in ops}, print_fn=lambda s: None)


def _cmd(op, job_id, n, **args):
    return {"request_id": f"r{n}", "op": op, "args": {"job_id": job_id, **args}, "origin": steer_agent.STEER_ORIGIN}


def test_pause_is_recorded_with_origin_and_shows_in_feed(job):
    store, j = job
    ran = []
    res = _pane(store, ran).execute(_cmd("pause", j.job_id, 1))
    assert res["accepted"] and ran == ["pause"]
    ev = [e for e in store.events(j.job_id) if e.what == "steer_agent_action"]
    assert ev and ev[-1].evidence["origin"] == "steer-agent" and ev[-1].evidence["op"] == "pause"
    assert any("steer-agent pause" in t for _, _, t in watchers.collect(store.events(j.job_id)))


@pytest.mark.parametrize("op", ["accept", "stop", "incorporate", "release"])
def test_control_pane_refuses_irreversible_ops_from_steer_agent(job, op):
    store, j = job
    ran = []
    res = _pane(store, ran).execute(_cmd(op, j.job_id, 2))
    assert not res["accepted"] and ran == [] and "Sam's" in res["detail"]
    assert [e for e in store.events(j.job_id) if e.what == "steer_agent_action"][-1].evidence["accepted"] is False
    assert _pane(store, ran).execute({**_cmd(op, j.job_id, 3), "origin": "dash"})["accepted"]  # humans still can


def test_floor_approval_refused_but_normal_one_approved(job):
    store, j = job
    floor = approvals_module.open_approval(store, j.job_id, "Bash: git push origin main", kind="permission")
    store.kv_set(j.job_id, f"hold:{floor['id']}", {"tool": "Bash", "input": {"command": "git push origin main"}, "reason": "git push"})
    easy = approvals_module.open_approval(store, j.job_id, "Bash: ls", kind="permission")
    store.kv_set(j.job_id, f"hold:{easy['id']}", {"tool": "Bash", "input": {"command": "ls"}, "reason": ""})
    ran = []
    pane = _pane(store, ran)
    assert not pane.execute(_cmd("approve", j.job_id, 4, approval_id=floor["id"]))["accepted"]
    assert pane.execute(_cmd("deny", j.job_id, 5, approval_id=floor["id"]))["accepted"]  # denying a floor hold is fine
    assert pane.execute(_cmd("approve", j.job_id, 6, approval_id=easy["id"]))["accepted"]
    assert ran == ["deny", "approve"]


def test_acceptance_approval_cannot_be_decided(job):
    store, j = job
    acc = approvals_module.open_approval(store, j.job_id, approvals_module.ACCEPTANCE_QUESTION, kind="acceptance")
    ran = []
    pane = _pane(store, ran)
    for n, op in enumerate(("approve", "deny"), 7):
        assert not pane.execute(_cmd(op, j.job_id, n, approval_id=acc["id"]))["accepted"]
    assert ran == []


def test_system_prompt_tells_the_agent_the_only_command_shape_that_is_allowed():
    # Regression (first live trial): the agent wrapped commands in `cd …; J=…; … $J` and every one was denied.
    from steward import owner, steer_agent
    text = steer_agent.system_prompt("abc123")
    assert "single plain command" in text and "no `cd`" in text and "full job id" in text
