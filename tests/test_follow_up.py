"""Follow-up jobs, session-driven reads (jobs/wait/result), MCP get_result."""

from __future__ import annotations

import json
import subprocess

import pytest

from steward import cli, followup, mcp_auth, mcp_server, phone, session
from steward.api import StewardAPI
from steward.contract import load_contract
from steward.control_pane import ControlPane
from steward.models import JobState, Judgment
from steward.store import Store


def _git(cwd, *argv):
    return subprocess.run(["git", *argv], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("a\n")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "init")
    return r


def _new(store, repo, goal="do the thing. Finish line: a.txt exists", *extra):
    assert cli.main(["new", goal, "--repo", str(repo), "--no-baseline", *extra]) == 0
    return store.list_jobs()[-1]


_PATHS = {
    JobState.RUNNING: [JobState.RUNNING],
    JobState.NEEDS_HUMAN: [JobState.RUNNING, JobState.NEEDS_HUMAN],
    JobState.ACCEPTED: [JobState.RUNNING, JobState.NEEDS_HUMAN, JobState.ACCEPTED],
    JobState.STOPPED: [JobState.RUNNING, JobState.NEEDS_HUMAN, JobState.STOPPED],
}


def _set(store, jid, state):
    if store.get_job(jid).state == JobState.CREATED:
        store.set_state(jid, JobState.CONTRACTED, what="t", evidence={}, rule="t", judgment=Judgment.code)
    for step in _PATHS[state]:
        if store.get_job(jid).state != step:
            store.set_state(jid, step, what="test", evidence={}, rule="t", judgment=Judgment.human)


def _finished_parent(store, repo, state=JobState.ACCEPTED):
    parent = _new(store, repo)
    wt = store.home / "worktrees" / parent.job_id
    (wt / "b.txt").write_text("secret sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789\n")
    _git(wt, "add", ".")
    _git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "add b")
    _set(store, parent.job_id, state)
    return parent, wt


def test_follow_up_refused_for_running_parent(steward_home, repo, capsys):
    store = Store()
    parent = _new(store, repo)
    _set(store, parent.job_id, JobState.RUNNING)
    rc = cli.main(["new", "more", "--follow-up", parent.job_id])
    assert rc == 2
    assert "ACCEPTED or RELEASED" in capsys.readouterr().err
    assert len(store.list_jobs()) == 1


def test_follow_up_unknown_parent(steward_home, repo, capsys):
    assert cli.main(["new", "more", "--follow-up", "nope", "--repo", str(repo)]) == 2
    assert "unknown follow-up parent" in capsys.readouterr().err


def test_worktree_starts_from_parent_branch_when_not_incorporated(steward_home, repo):
    store = Store()
    parent, _ = _finished_parent(store, repo)
    assert cli.main(["new", "next step", "--follow-up", parent.job_id[:8], "--no-baseline"]) == 0
    child = store.list_jobs()[-1]
    assert child.repo == str(repo)  # defaults to the parent's repo
    cwt = store.home / "worktrees" / child.job_id
    assert (cwt / "b.txt").exists()
    assert _git(cwt, "branch", "--show-current") == f"steward/{child.job_id}"
    assert _git(repo, "rev-parse", "HEAD") != _git(cwt, "rev-parse", "HEAD")


def test_worktree_starts_from_head_when_incorporated(steward_home, repo):
    store = Store()
    parent, _ = _finished_parent(store, repo)
    _git(repo, "merge", "--no-ff", "-qm", "merge", f"steward/{parent.job_id}")
    store.kv_set(parent.job_id, "incorporated_commit", _git(repo, "rev-parse", "HEAD"))
    assert followup.start_point(store, parent) is None
    assert cli.main(["new", "next", "--follow-up", parent.job_id, "--no-baseline"]) == 0
    child = store.list_jobs()[-1]
    cwt = store.home / "worktrees" / child.job_id
    assert _git(cwt, "rev-parse", "HEAD") == _git(repo, "rev-parse", "HEAD")
    assert "merge commit" in store.kv_get(child.job_id, "parent_brief")


def test_after_successor_inherits_target_and_uses_its_tip(steward_home, repo):
    from steward import chain_branch

    store = Store()
    _git(repo, "checkout", "-qb", "chain-target")
    (repo / "target.txt").write_text("chain base\n")
    _git(repo, "add", "target.txt")
    _git(repo, "commit", "-qm", "chain base")
    predecessor = _new(store, repo, "first")
    target_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")

    assert cli.main(["new", "second", "--repo", str(repo), "--after", predecessor.job_id,
                     "--no-baseline"]) == 0
    child = store.list_jobs()[-1]
    cwt = store.home / "worktrees" / child.job_id
    assert chain_branch.target(store, child.job_id) == "chain-target"
    assert _git(cwt, "rev-parse", "HEAD") == target_head
    assert store.kv_get(child.job_id, "seq_after") == predecessor.job_id


def test_queued_worktree_fast_forwards_to_inherited_target_before_launch(steward_home, repo):
    from steward import chain_branch, sequencing
    from steward.runner_exec import SubprocessRunner

    store = Store()
    predecessor = _new(store, repo, "first")
    _git(repo, "checkout", "-qb", "chain-target")
    (repo / "target.txt").write_text("chain base\n")
    _git(repo, "add", "target.txt")
    _git(repo, "commit", "-qm", "chain base")
    chain_branch.record(store, predecessor.job_id, "chain-target")
    _git(repo, "checkout", "-q", "main")
    assert cli.main(["new", "second", "--repo", str(repo), "--no-baseline"]) == 0
    child = store.list_jobs()[-1]
    cwt = store.home / "worktrees" / child.job_id

    assert sequencing.set_after(store, child.job_id, predecessor.job_id)[0]
    assert chain_branch.align_staged_worktree(store, child, SubprocessRunner()) is None
    assert chain_branch.target(store, child.job_id) == "chain-target"
    assert _git(cwt, "rev-parse", "HEAD") == _git(repo, "rev-parse", "chain-target")


def test_follow_up_inherits_target_when_source_checkout_drifted(steward_home, repo):
    from steward import chain_branch

    store = Store()
    parent, _ = _finished_parent(store, repo)
    chain_branch.record(store, parent.job_id, "main")
    _git(repo, "checkout", "-qb", "other-target")
    assert cli.main(["new", "next", "--follow-up", parent.job_id, "--no-baseline"]) == 0
    child = store.list_jobs()[-1]
    assert chain_branch.target(store, child.job_id) == "main"


def test_autopilot_fixup_inherits_target_when_source_checkout_drifted(steward_home, repo, monkeypatch):
    from steward import autopilot, chain_branch

    store = Store()
    parent, parent_worktree = _finished_parent(store, repo)
    chain_branch.record(store, parent.job_id, "main")
    _git(repo, "checkout", "-qb", "other-target")

    handlers = cli._build_control_handlers(store)
    handlers["launch"] = lambda cmd: {"accepted": True, "tab_id": "test"}
    monkeypatch.setattr(cli, "_build_control_handlers", lambda _store: handlers)

    result = autopilot._default_followup(store, parent.job_id, "resolve the merge conflict")

    assert result["accepted"]
    child = store.get_job(result["job_id"])
    child_worktree = store.home / "worktrees" / child.job_id
    assert chain_branch.target(store, child.job_id) == "main"
    # Fix-ups intentionally start at the parent's worktree tip while retaining its merge target.
    assert _git(child_worktree, "rev-parse", "HEAD") == _git(parent_worktree, "rev-parse", "HEAD")


def test_links_events_contract_and_brief(steward_home, repo):
    from steward.brief import render_brief

    store = Store()
    parent, _ = _finished_parent(store, repo)
    assert cli.main(["new", "next", "--follow-up", parent.job_id, "--no-baseline"]) == 0
    child = store.list_jobs()[-1]
    assert store.kv_get(child.job_id, "parent_job") == parent.job_id
    assert store.kv_get(parent.job_id, "children") == [child.job_id]
    for jid in (child.job_id, parent.job_id):
        assert any(e.what == "follow_up_created" for e in store.events(jid))
    contract = load_contract(store, child.job_id)
    from steward.contract import render
    assert f"FOLLOWS: {parent.job_id[:8]} (ACCEPTED)" in render(contract)
    section = store.kv_get(child.job_id, "parent_brief")
    assert len(section) <= followup.BRIEF_CAP
    assert "sk-ant-api03" not in section
    assert "b.txt" in section and "do the thing" in section
    brief = render_brief(contract, [], None, None, parent=section)
    assert "PARENT JOB" in brief and parent.job_id[:8] in brief
    assert "PARENT JOB" not in render_brief(contract, [], None, None)
    assert phone.followup_module.follows_label(store, child.job_id).startswith("↳ follow-up of " + parent.job_id[:8])


def test_parent_section_is_capped(steward_home, repo):
    store = Store()
    parent, wt = _finished_parent(store, repo)
    for i in range(300):
        (wt / f"file_{i}_{'x' * 40}.txt").write_text("x")
    _git(wt, "add", ".")
    _git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "many")
    assert len(followup.parent_section(store, parent)) <= followup.BRIEF_CAP


def test_stopped_parent_warns(steward_home, repo, capsys):
    store = Store()
    parent, _ = _finished_parent(store, repo, JobState.STOPPED)
    assert cli.main(["new", "n", "--follow-up", parent.job_id, "--no-baseline"]) == 0
    assert "STOPPED" in capsys.readouterr().err


def test_created_by_recorded(steward_home, repo, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1234-abcd")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p2")
    store = Store()
    job = _new(store, repo)
    cb = store.kv_get(job.job_id, "created_by")
    assert cb["session"] == "sess-1234-abcd" and cb["herdr_pane"] == "w1:p2" and cb["cwd"]
    created = next(e for e in store.events(job.job_id) if e.what == "job created")
    assert created.evidence["created_by"] == cb


def test_jobs_mine_filtering(steward_home, monkeypatch, capsys):
    store = Store()
    a, b, c = (store.create_job(g, "/r") for g in ("alpha goal", "beta goal", "gamma"))
    store.kv_set(a.job_id, "created_by", {"session": "S1"})
    store.kv_set(b.job_id, "created_by", {"session": "S2"})
    store.kv_set(c.job_id, "created_by", {"herdr_pane": "w:p9"})
    _set(store, c.job_id, JobState.STOPPED)
    me = {"session": "S1"}
    assert [r["job_id"] for r in session.list_rows(store, mine=True, me=me)] == [a.job_id]
    assert len(session.list_rows(store)) == 2  # finished hidden by default
    assert len(session.list_rows(store, all_jobs=True)) == 3
    assert [r["job_id"] for r in session.list_rows(store, mine=True, all_jobs=True, me={"herdr_pane": "w:p9"})] == [c.job_id]
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S2")
    assert cli.main(["jobs", "--mine", "--json"]) == 0
    assert [r["job_id"] for r in json.loads(capsys.readouterr().out)] == [b.job_id]
    assert cli.main(["jobs"]) == 0
    assert "alpha goal" in capsys.readouterr().out


def test_wait_returns_on_decision_done_and_timeout(steward_home):
    store = Store()
    job = store.create_job("g", "/r")
    _set(store, job.job_id, JobState.RUNNING)
    now = [0.0]
    ticks = []

    def sleep(s):
        ticks.append(s)
        now[0] += s
        if len(ticks) == 3:
            _set(store, job.job_id, JobState.NEEDS_HUMAN)

    code, line = session.wait(store, job.job_id, "any", 100, sleep=sleep, clock=lambda: now[0])
    assert code == 0 and line.startswith(f"{job.job_id[:8]} NEEDS_HUMAN:") and len(ticks) == 3
    # until=done ignores the decision, times out
    now[0] = 0.0
    code, line = session.wait(store, job.job_id, "done", 25, sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])
    assert code == 2 and "timed out" in line
    _set(store, job.job_id, JobState.ACCEPTED)
    assert session.wait(store, job.job_id, "done", 5)[0] == 0
    code, line = session.wait(store, job.job_id, "decision", 0, sleep=lambda s: None)
    assert code == 2


def test_wait_cli_exit_codes(steward_home, capsys):
    store = Store()
    job = store.create_job("g", "/r")
    _set(store, job.job_id, JobState.ACCEPTED)
    assert cli.main(["wait", job.job_id[:8], "--until", "done"]) == 0
    assert capsys.readouterr().out.startswith(f"{job.job_id[:8]} ACCEPTED")
    assert cli.main(["wait", "zzzz"]) == 1


def test_result_sections_and_next_commands(steward_home, repo, capsys):
    store = Store()
    parent, _ = _finished_parent(store, repo)
    assert cli.main(["new", "next", "--follow-up", parent.job_id, "--no-baseline"]) == 0
    child = store.list_jobs()[-1]
    assert cli.main(["result", parent.job_id[:8]]) == 0
    out = capsys.readouterr().out
    for section in ("GOAL", "FINISH LINE", "STATE", "VERDICT / EVIDENCE", "WORKER'S FINAL MESSAGE", "OUTPUT FILES",
                    "BRANCH", "COST", "LINKS", "NEXT"):
        assert section in out
    assert "b.txt" in out and f"steward/{parent.job_id}" in out
    assert f"follow-up child {child.job_id}" in out
    assert "sk-ant-api03" not in out
    assert f"steward incorporate {parent.job_id}" in out and "--follow-up" in out
    assert cli.main(["result", child.job_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["parent_job"] == parent.job_id
    S = session._next_commands
    assert any("accept" in c for c in S(JobState.NEEDS_HUMAN, "j" * 36, False))
    assert any("send-back" in c for c in S(JobState.NEEDS_HUMAN, "j" * 36, False))
    assert not any("incorporate" in c for c in S(JobState.ACCEPTED, "j" * 36, True))
    assert any("wait" in c for c in S(JobState.RUNNING, "j" * 36, False))
    assert any("launch" in c for c in S(JobState.CONTRACTED, "j" * 36, False))


def _mcp(store, steward_home):
    def relay(op, args, *, origin, request_id):
        pane = ControlPane(store, cli._build_control_handlers(store), print_fn=lambda _l: None)
        return pane.execute({"request_id": request_id or "r1", "op": op, "args": args, "origin": origin})

    server = mcp_server.MCPServer(mcp_server.build_tools(StewardAPI(store, relay=relay)), home=steward_home)
    token, _ = mcp_auth.create_token(list(mcp_auth.SCOPES), home=steward_home)
    info = mcp_auth.authorize(f"Bearer {token}", home=steward_home)

    def call(name, arguments):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}).encode()
        return json.loads(server.handle_authenticated(body, info)[1])

    return server, call


def test_mcp_get_result_and_create_job_follow_up(steward_home, repo):
    store = Store()
    parent, _ = _finished_parent(store, repo)
    server, call = _mcp(store, steward_home)
    assert server.tools["get_result"].required_scope == "runner:read"
    res = call("get_result", {"job_id": parent.job_id})["result"]["structuredContent"]
    assert res["state"] == "ACCEPTED" and "b.txt" in res["files_changed"]
    assert "error" in call("get_result", {"job_id": "nope"})
    made = call("create_job", {"goal": "more", "repo": str(repo), "follow_up": parent.job_id})["result"]["structuredContent"]
    assert made["accepted"] and made["follow_up"] == parent.job_id
    assert store.kv_get(made["job_id"], "parent_job") == parent.job_id


def test_wait_ignores_resolver_deciding_and_debounces_live_asks(steward_home, monkeypatch):
    """Mechanics item 14: a transient ask on a RUNNING job (resolver / tech lead deciding) never wakes `wait`."""
    store = Store()
    job = store.create_job("g", "/r")
    _set(store, job.job_id, JobState.RUNNING)
    state = {"ask": {"question": "worker permission: Bash", "resolver_pending": True}}
    monkeypatch.setattr(session.evidence, "decision",
                        lambda st, jid: None if state["ask"] is None else {"kind": "approval", "approval": state["ask"]})
    monkeypatch.setattr(session.jobview, "reason", lambda *a, **k: "approve? Bash")
    now = [0.0]

    def sleep(s):
        now[0] += s
        if now[0] >= 20 and state["ask"] and state["ask"].get("resolver_pending"):
            state["ask"] = {"question": "worker permission: Bash"}  # resolver gave up: a real ask ...
        elif now[0] >= 35 and state["ask"] and now[0] < 60:
            state["ask"] = None  # ... which the tech lead cleared within the settle window
        elif now[0] >= 100 and state["ask"] is None:
            state["ask"] = {"question": "worker permission: Bash"}  # a real one that stays
    code, line = session.wait(store, job.job_id, "any", 500, sleep=sleep, clock=lambda: now[0])
    assert code == 0 and "RUNNING: approve?" in line and now[0] >= 100 + session.SETTLE_S


def test_wait_returns_at_once_for_a_parked_job(steward_home):
    store = Store()
    job = store.create_job("g", "/r")
    _set(store, job.job_id, JobState.RUNNING)
    _set(store, job.job_id, JobState.NEEDS_HUMAN)
    code, line = session.wait(store, job.job_id, "any", 100, sleep=lambda s: (_ for _ in ()).throw(AssertionError),
                              clock=lambda: 0.0)
    assert code == 0 and "NEEDS_HUMAN" in line
