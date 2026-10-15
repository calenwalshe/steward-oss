import pytest
import json
from pathlib import Path

from steward.herdr_worker import HerdrWorker
from steward.runner_exec import CommandResult, FakeRunner
from steward.worker import DeliveryResult, WorkerObservation

_SPLIT_OK = json.dumps({"result": {"pane": {"pane_id": "pane-2"}}})


def _result(argv, cwd, *, stdout="", exit_code=0, stderr=""):
    return CommandResult(
        argv=argv, cwd=str(cwd), exit_code=exit_code, stdout=stdout, stderr=stderr, duration_s=0.0
    )


def _worker(responses):
    runner = FakeRunner(responses)
    worker = HerdrWorker(
        runner, caller_pane="pane-1", kind="claude", direction="right", name="steward-abc",
        ready_timeout_s=4, poll_s=1, sleep=lambda s: None,
    )
    return worker, runner


def test_start_splits_pane_starts_agent_and_prompts_without_wait(tmp_path):
    def respond(argv, cwd):
        if argv[:3] == ["herdr", "pane", "split"]:
            return _result(argv, cwd, stdout=_SPLIT_OK)
        if argv[:3] == ["herdr", "agent", "get"]:
            return _result(argv, cwd, stdout=json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
        return _result(argv, cwd)

    worker, runner = _worker(respond)

    ref = worker.start("BRIEF TEXT", tmp_path)

    assert ref == "steward-abc"
    assert worker.pane_id == "pane-2"
    assert len(runner.calls) == 5
    assert runner.calls[0]["argv"] == [
        "herdr", "pane", "split",
        "--pane", "pane-1",
        "--direction", "right",
        "--ratio", "0.2",
        "--cwd", str(tmp_path),
        "--no-focus",
    ]
    assert runner.calls[1]["argv"] == ["herdr", "pane", "rename", "pane-2", "worker abc"]
    assert runner.calls[2]["argv"] == [
        "herdr", "agent", "start", "steward-abc", "--kind", "claude", "--pane", "pane-2",
    ]
    assert runner.calls[3]["argv"] == ["herdr", "agent", "get", "steward-abc"]
    assert runner.calls[4]["argv"] == ["herdr", "agent", "prompt", "steward-abc", "BRIEF TEXT"]
    assert "--wait" not in runner.calls[4]["argv"]


def test_deliver_checks_state_first_then_prompts_when_not_blocked(tmp_path):
    def respond(argv, cwd):
        if argv[:3] == ["herdr", "pane", "split"]:
            return _result(argv, cwd, stdout=_SPLIT_OK)
        if argv[:3] == ["herdr", "agent", "get"]:
            return _result(argv, cwd, stdout=json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
        return _result(argv, cwd)

    worker, runner = _worker(respond)
    worker.pane_id, worker.worktree = "pane-2", tmp_path

    result = worker.deliver("focus on X, rev=2")

    assert isinstance(result, DeliveryResult)
    assert result.ok is True
    last_call = runner.calls[-1]
    assert last_call["argv"] == ["herdr", "agent", "prompt", "steward-abc", "focus on X, rev=2"]


def test_deliver_rejects_when_agent_blocked(tmp_path):
    def respond(argv, cwd):
        if argv[:3] == ["herdr", "pane", "split"]:
            return _result(argv, cwd, stdout=_SPLIT_OK)
        if argv[:3] == ["herdr", "agent", "get"]:
            return _result(argv, cwd, stdout=json.dumps({"result": {"agent": {"agent_status": "blocked"}}}))
        return _result(argv, cwd)

    worker, runner = _worker(respond)
    worker.pane_id, worker.worktree = "pane-2", tmp_path

    result = worker.deliver("nudge")

    assert result.ok is False
    assert result.detail == "worker_blocked"
    # No prompt call was made after the blocked "get".
    assert runner.calls[-1]["argv"][:3] == ["herdr", "agent", "get"]


def test_observe_maps_state_to_lifecycle_and_reads_output(tmp_path):
    def respond(argv, cwd):
        if argv[:3] == ["herdr", "pane", "split"]:
            return _result(argv, cwd, stdout=_SPLIT_OK)
        if argv[:3] == ["herdr", "agent", "get"]:
            return _result(argv, cwd, stdout=json.dumps({"result": {"agent": {"agent_status": "working"}}}))
        if argv[:3] == ["herdr", "agent", "read"]:
            return _result(argv, cwd, stdout="model is thinking...")
        return _result(argv, cwd)

    worker, runner = _worker(respond)
    worker.pane_id, worker.worktree = "pane-2", tmp_path

    obs = worker.observe()

    assert isinstance(obs, WorkerObservation)
    assert obs.lifecycle == "working"
    assert obs.output_tail == "model is thinking..."
    read_call = [c for c in runner.calls if c["argv"][:3] == ["herdr", "agent", "read"]][0]
    assert read_call["argv"] == [
        "herdr", "agent", "read", "steward-abc", "--source", "recent-unwrapped", "--lines", "120",
    ]


def test_observe_maps_done_and_idle_to_idle_not_completion(tmp_path):
    for herdr_state in ("done", "idle"):
        def respond(argv, cwd, herdr_state=herdr_state):
            if argv[:3] == ["herdr", "pane", "split"]:
                return _result(argv, cwd, stdout=_SPLIT_OK)
            if argv[:3] == ["herdr", "agent", "get"]:
                return _result(argv, cwd, stdout=json.dumps({"result": {"agent": {"agent_status": herdr_state}}}))
            return _result(argv, cwd)

        worker, runner = _worker(respond)
        worker.start("brief", tmp_path)

        obs = worker.observe()

        assert obs.lifecycle == "idle"


def test_interrupt_sends_ctrl_c(tmp_path):
    def respond(argv, cwd):
        if argv[:3] == ["herdr", "pane", "split"]:
            return _result(argv, cwd, stdout=_SPLIT_OK)
        return _result(argv, cwd)

    worker, runner = _worker(respond)
    worker.pane_id, worker.worktree = "pane-2", tmp_path

    worker.interrupt()

    assert runner.calls[-1]["argv"] == ["herdr", "agent", "send-keys", "steward-abc", "ctrl+c"]


def test_stop_interrupts_closes_only_its_own_pane_and_confirms_via_agent_get(tmp_path):
    def respond(argv, cwd):
        if argv[:3] == ["herdr", "pane", "split"]:
            return _result(argv, cwd, stdout=_SPLIT_OK)
        if argv[:3] == ["herdr", "agent", "get"]:
            return _result(argv, cwd, exit_code=1, stdout="", stderr="agent not found")
        return _result(argv, cwd)

    worker, runner = _worker(respond)
    worker.pane_id, worker.worktree = "pane-2", tmp_path

    confirmed = worker.stop()

    assert confirmed is True
    close_calls = [c for c in runner.calls if c["argv"][:2] == ["herdr", "pane"] and c["argv"][2] == "close"]
    assert len(close_calls) == 1
    assert close_calls[0]["argv"] == ["herdr", "pane", "close", "pane-2"]
    send_keys_calls = [c for c in runner.calls if c["argv"][:3] == ["herdr", "agent", "send-keys"]]
    assert send_keys_calls[-1]["argv"] == ["herdr", "agent", "send-keys", "steward-abc", "ctrl+c"]


def test_stop_not_confirmed_when_agent_get_still_succeeds(tmp_path):
    def respond(argv, cwd):
        if argv[:3] == ["herdr", "pane", "split"]:
            return _result(argv, cwd, stdout=_SPLIT_OK)
        if argv[:3] == ["herdr", "agent", "get"]:
            return _result(argv, cwd, stdout=json.dumps({"result": {"agent": {"agent_status": "working"}}}))
        return _result(argv, cwd)

    worker, runner = _worker(respond)
    worker.pane_id, worker.worktree = "pane-2", tmp_path

    confirmed = worker.stop()

    assert confirmed is False


def test_agent_args_are_passed_after_double_dash(tmp_path):
    from steward.herdr_worker import HerdrWorker
    from steward.runner_exec import CommandResult

    calls = []

    def respond(argv, cwd=None, timeout=None, env=None):
        calls.append(argv)
        out = '{"result": {"pane": {"pane_id": "w9:p2"}}}' if argv[:3] == ["herdr", "pane", "split"] else '{"result": {"agent": {"agent_status": "idle"}}}'
        return CommandResult(argv, str(cwd), 0, out, "", 0.0)

    class R:
        run = staticmethod(respond)

    w = HerdrWorker(R(), caller_pane="w9:p1", kind="claude", name="demo",
                    agent_args=["--permission-mode", "acceptEdits"], sleep=lambda s: None)
    w.start("brief", tmp_path)
    start = next(c for c in calls if c[:3] == ["herdr", "agent", "start"])
    assert start[-3:] == ["--", "--permission-mode", "acceptEdits"]


def _staged(statuses):
    seq = iter(statuses)
    last = {"v": statuses[-1]}

    def respond(argv, cwd):
        if argv[:3] == ["herdr", "pane", "split"]:
            return _result(argv, cwd, stdout=_SPLIT_OK)
        if argv[:3] == ["herdr", "agent", "get"]:
            last["v"] = next(seq, last["v"])
            return _result(argv, cwd, stdout=json.dumps({"result": {"agent": {"agent_status": last["v"]}}}))
        return _result(argv, cwd)

    return respond


def test_start_waits_through_blocked_startup_dialog_then_delivers_brief(tmp_path):
    worker, runner = _worker(_staged(["unknown", "blocked", "blocked", "idle"]))
    seen = []
    worker.on_blocked = seen.append
    worker.start("BRIEF", tmp_path)
    assert len(seen) == 1  # operator told once, not per poll
    assert runner.calls[-1]["argv"] == ["herdr", "agent", "prompt", "steward-abc", "BRIEF"]


def test_start_never_ready_raises_and_never_prompts(tmp_path):
    from steward.herdr_worker import WorkerStartBlocked

    worker, runner = _worker(_staged(["blocked"]))
    with pytest.raises(WorkerStartBlocked):
        worker.start("BRIEF", tmp_path)
    assert not any(c["argv"][:3] == ["herdr", "agent", "prompt"] for c in runner.calls)


def test_agent_state_reads_real_herdr_agent_status_field(tmp_path):
    worker, _ = _worker(_staged(["working"]))
    assert worker._agent_state() == "working"


def test_answer_prompt_approve_sends_enter_when_blocked(tmp_path):
    def respond(argv, cwd):
        if argv[:3] == ["herdr", "pane", "split"]:
            return _result(argv, cwd, stdout=_SPLIT_OK)
        if argv[:3] == ["herdr", "agent", "get"]:
            return _result(argv, cwd, stdout=json.dumps({"result": {"agent": {"agent_status": "blocked"}}}))
        return _result(argv, cwd)

    worker, runner = _worker(respond)
    worker.pane_id, worker.worktree = "pane-2", tmp_path

    result = worker.answer_prompt(approve=True)

    assert result.ok is True
    assert runner.calls[-1]["argv"] == ["herdr", "agent", "send-keys", "steward-abc", "enter"]


def test_answer_prompt_deny_sends_esc_when_blocked(tmp_path):
    def respond(argv, cwd):
        if argv[:3] == ["herdr", "pane", "split"]:
            return _result(argv, cwd, stdout=_SPLIT_OK)
        if argv[:3] == ["herdr", "agent", "get"]:
            return _result(argv, cwd, stdout=json.dumps({"result": {"agent": {"agent_status": "blocked"}}}))
        return _result(argv, cwd)

    worker, runner = _worker(respond)
    worker.pane_id, worker.worktree = "pane-2", tmp_path

    result = worker.answer_prompt(approve=False)

    assert result.ok is True
    assert runner.calls[-1]["argv"] == ["herdr", "agent", "send-keys", "steward-abc", "esc"]


def test_answer_prompt_rejects_when_not_blocked(tmp_path):
    def respond(argv, cwd):
        if argv[:3] == ["herdr", "pane", "split"]:
            return _result(argv, cwd, stdout=_SPLIT_OK)
        if argv[:3] == ["herdr", "agent", "get"]:
            return _result(argv, cwd, stdout=json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
        return _result(argv, cwd)

    worker, runner = _worker(respond)
    worker.pane_id, worker.worktree = "pane-2", tmp_path

    result = worker.answer_prompt(approve=True)

    assert result.ok is False
    assert result.detail == "worker_not_blocked"
    assert not any(c["argv"][:3] == ["herdr", "agent", "send-keys"] for c in runner.calls)


def test_claude_preauthorized_args_come_only_from_contract_authority():
    from steward.herdr_worker import claude_preauthorized_args

    args = claude_preauthorized_args(
        ["edit files in job worktree", "run: pytest", "run: make test", "no deploy/publish/network"]
    )
    assert args[:3] == ["--permission-mode", "acceptEdits", "--allowedTools"]
    assert "Bash(pytest:*)" in args and "Bash(make test:*)" in args
    assert "Bash(git commit:*)" in args
    assert "Bash(python3 -m pytest:*)" in args
    assert not any("deploy" in a or "publish" in a for a in args)


def test_pretrust_adds_entry_without_touching_other_projects(tmp_path):
    from steward.herdr_worker import pretrust_claude_dir

    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({"projects": {"/other": {"hasTrustDialogAccepted": False, "x": 1}}, "k": 2}))
    pretrust_claude_dir(tmp_path / "wt", cfg)
    data = json.loads(cfg.read_text())
    assert data["projects"][str(tmp_path / "wt")]["hasTrustDialogAccepted"] is True
    assert data["projects"]["/other"] == {"hasTrustDialogAccepted": False, "x": 1} and data["k"] == 2


def test_start_pretrusts_worktree_before_launching_claude(tmp_path):
    cfg = tmp_path / "claude.json"
    cfg.write_text("{}")
    worker, runner = _worker(_staged(["idle"]))
    worker.pretrust, worker.claude_config_path = True, cfg
    worker.start("BRIEF", tmp_path)
    assert json.loads(cfg.read_text())["projects"][str(tmp_path)]["hasTrustDialogAccepted"] is True


def test_vanished_agent_is_exited_not_unknown(tmp_path):
    def respond(argv, cwd):
        if argv[:3] == ["herdr", "agent", "get"]:
            return _result(argv, cwd, exit_code=1, stderr="agent not found")
        return _result(argv, cwd)

    worker, _ = _worker(respond)
    worker.pane_id, worker.worktree = "pane-2", tmp_path
    assert worker.observe().lifecycle == "exited"


# -- observe(): markers read from the session log, not the screen (bug 2b) --


_SESSION_ID = "166d44c1-b365-4482-ad0d-60fa90bcd33f"


def _agent_get_with_session(session_id=_SESSION_ID, kind="claude"):
    return json.dumps(
        {
            "result": {
                "agent": {
                    "agent_status": "working",
                    "agent_session": {"agent": kind, "kind": "id", "value": session_id},
                }
            }
        }
    )


def _write_session_log(root, session_id, records):
    project_dir = root / "-home-agent--local-state-steward-worktrees-jobid"
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _assistant_record(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def test_observe_appends_session_log_tail_even_when_pane_read_is_truncated(tmp_path):
    projects_root = tmp_path / "claude_projects"
    _write_session_log(
        projects_root, _SESSION_ID,
        [
            {"type": "queue-operation", "operation": "enqueue"},  # non-assistant: ignored
            _assistant_record("working on it"),
            _assistant_record("STEWARD-DONE"),
        ],
    )

    def respond(argv, cwd):
        if argv[:3] == ["herdr", "agent", "get"]:
            return _result(argv, cwd, stdout=_agent_get_with_session())
        if argv[:3] == ["herdr", "agent", "read"]:
            return _result(argv, cwd, stdout="ST…")  # pane truncated the marker
        return _result(argv, cwd)

    worker, _ = _worker(respond)
    worker.pane_id, worker.worktree = "pane-2", tmp_path
    worker.claude_projects_root = projects_root

    obs = worker.observe()

    assert "STEWARD-DONE" in obs.output_tail
    assert "ST…" in obs.output_tail  # pane read is kept too


def test_observe_session_log_tolerates_malformed_lines(tmp_path):
    projects_root = tmp_path / "claude_projects"
    project_dir = projects_root / "-home-agent--local-state-steward-worktrees-jobid"
    project_dir.mkdir(parents=True)
    path = project_dir / f"{_SESSION_ID}.jsonl"
    path.write_text(
        "not json at all\n"
        + json.dumps(_assistant_record("STEWARD-ACK rev=2")) + "\n"
        + "{ broken\n"
    )

    def respond(argv, cwd):
        if argv[:3] == ["herdr", "agent", "get"]:
            return _result(argv, cwd, stdout=_agent_get_with_session())
        return _result(argv, cwd)

    worker, _ = _worker(respond)
    worker.pane_id, worker.worktree = "pane-2", tmp_path
    worker.claude_projects_root = projects_root

    obs = worker.observe()

    assert "STEWARD-ACK rev=2" in obs.output_tail


def test_observe_session_log_only_keeps_last_20_assistant_messages(tmp_path):
    projects_root = tmp_path / "claude_projects"
    records = [_assistant_record(f"msg-{i}") for i in range(25)]
    _write_session_log(projects_root, _SESSION_ID, records)

    def respond(argv, cwd):
        if argv[:3] == ["herdr", "agent", "get"]:
            return _result(argv, cwd, stdout=_agent_get_with_session())
        return _result(argv, cwd)

    worker, _ = _worker(respond)
    worker.pane_id, worker.worktree = "pane-2", tmp_path
    worker.claude_projects_root = projects_root

    obs = worker.observe()

    assert "msg-0" not in obs.output_tail
    assert "msg-5" in obs.output_tail
    assert "msg-24" in obs.output_tail


def test_observe_session_log_is_pane_only_for_codex(tmp_path):
    projects_root = tmp_path / "claude_projects"
    _write_session_log(projects_root, _SESSION_ID, [_assistant_record("STEWARD-DONE")])

    def respond(argv, cwd):
        if argv[:3] == ["herdr", "agent", "get"]:
            return _result(argv, cwd, stdout=_agent_get_with_session(kind="codex"))
        if argv[:3] == ["herdr", "agent", "read"]:
            return _result(argv, cwd, stdout="pane output")
        return _result(argv, cwd)

    runner = FakeRunner(respond)
    worker = HerdrWorker(
        runner, caller_pane="pane-1", kind="codex", name="steward-abc",
        ready_timeout_s=4, poll_s=1, sleep=lambda s: None, claude_projects_root=projects_root,
    )
    worker.pane_id, worker.worktree = "pane-2", tmp_path

    obs = worker.observe()

    assert obs.output_tail == "pane output"  # session log never consulted for codex


def test_observe_session_log_missing_file_is_pane_only(tmp_path):
    def respond(argv, cwd):
        if argv[:3] == ["herdr", "agent", "get"]:
            return _result(argv, cwd, stdout=_agent_get_with_session(session_id="no-such-id"))
        if argv[:3] == ["herdr", "agent", "read"]:
            return _result(argv, cwd, stdout="pane output")
        return _result(argv, cwd)

    worker, _ = _worker(respond)
    worker.pane_id, worker.worktree = "pane-2", tmp_path
    worker.claude_projects_root = tmp_path / "claude_projects"

    obs = worker.observe()

    assert obs.output_tail == "pane output"


_MCP_CARD = (
    'Allow Stew to run tool "stew.get_recent_events"?\n{"limit": 5}\n'
    "1. Allow\n2. Allow for this session\n3. Always allow\n4. Cancel\nenter to submit | esc to cancel\n"
)


def _blocked_worker(tmp_path, screen):
    def respond(argv, cwd):
        if argv[:3] == ["herdr", "agent", "get"]:
            return _result(argv, cwd, stdout=json.dumps({"result": {"agent": {"agent_status": "blocked"}}}))
        if argv[:3] == ["herdr", "agent", "read"]:
            return _result(argv, cwd, stdout=screen)
        return _result(argv, cwd)

    worker, runner = _worker(respond)
    worker.pane_id, worker.worktree = "pane-2", tmp_path
    return worker, runner


def _keys(runner):
    return [c["argv"][-1] for c in runner.calls if c["argv"][:3] == ["herdr", "agent", "send-keys"]]


def test_approve_codex_stew_readonly_card_picks_allow_for_this_session(tmp_path):
    worker, runner = _blocked_worker(tmp_path, _MCP_CARD)
    assert worker.answer_prompt(approve=True).ok is True
    assert _keys(runner) == ["down", "enter"]


def test_approve_codex_stew_write_tool_card_is_plain_enter(tmp_path):
    worker, runner = _blocked_worker(tmp_path, _MCP_CARD.replace("get_recent_events", "steer_job"))
    worker.answer_prompt(approve=True)
    assert _keys(runner) == ["enter"]


def test_deny_never_sends_down(tmp_path):
    worker, runner = _blocked_worker(tmp_path, _MCP_CARD)
    worker.answer_prompt(approve=False)
    assert _keys(runner) == ["esc"]
