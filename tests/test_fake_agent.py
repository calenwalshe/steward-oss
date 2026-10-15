"""Tests for steward/fake_agent.py (WRK-04).

`FakeAgent` is exercised directly (pure state machine, no subprocess) for
every step type; one subprocess test drives the real CLI end to end to prove
the wire format matches what `steward.shim` will actually read.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from steward.fake_agent import Crash, FakeAgent, Sleep

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _subprocess_env() -> dict:
    """`steward` isn't pip-installed here; the child's cwd is a scratch dir
    (mirrors the real shim spawning it in a job worktree), so it needs the
    repo root on PYTHONPATH to resolve `-m steward.fake_agent`."""
    return {**os.environ, "PYTHONPATH": str(_REPO_ROOT)}


def user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def control_response(request_id: str, *, allow: bool) -> dict:
    behavior = {"behavior": "allow", "updatedInput": {}} if allow else {"behavior": "deny", "message": "no"}
    return {"type": "control_response", "response": {"subtype": "success", "request_id": request_id, "response": behavior}}


# ---------------------------------------------------------------- FakeAgent


def test_start_emits_system_init():
    agent = FakeAgent(steps=[])
    events = agent.start()
    assert events == [{"type": "system", "subtype": "init", "session_id": agent.session_id}]


def test_user_message_is_replayed_with_string_content():
    agent = FakeAgent(steps=[])
    events = agent.feed(user("hello"))
    assert events[0] == {"type": "user", "message": {"role": "user", "content": "hello"}}


def test_say_step_emits_assistant_text():
    agent = FakeAgent(steps=[{"say": "on it"}])
    events = agent.feed(user("go"))
    assert events[1] == {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "on it"}]},
    }


def test_tool_use_without_permission_does_not_block():
    agent = FakeAgent(steps=[{"tool_use": {"name": "Read", "input": {"path": "x"}}}, {"say": "done reading"}])
    events = agent.feed(user("go"))
    # replay + tool_use + say, no control_request, no pause
    assert [e["type"] for e in events] == ["user", "assistant", "assistant"]
    tool_use_block = events[1]["message"]["content"][0]
    assert tool_use_block == {"type": "tool_use", "id": "tu-1", "name": "Read", "input": {"path": "x"}}


def test_tool_use_with_permission_blocks_until_control_response():
    agent = FakeAgent(
        steps=[{"tool_use": {"name": "Bash", "input": {"command": "touch x"}, "permission": True}}, {"say": "after"}]
    )
    events = agent.feed(user("go"))
    types = [e["type"] for e in events]
    assert types == ["user", "assistant", "control_request"]
    request = events[2]
    assert request["request"]["subtype"] == "can_use_tool"
    assert request["request"]["tool_name"] == "Bash"
    assert request["request"]["tool_use_id"] == events[1]["message"]["content"][0]["id"]

    resumed = agent.feed(control_response(request["request_id"], allow=True))
    assert resumed == [
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "after"}]}}
    ]


def test_denied_permission_is_recorded_and_turn_continues():
    agent = FakeAgent(
        steps=[
            {"tool_use": {"name": "Bash", "input": {}, "permission": True}},
            {"result": {"subtype": "success"}},
        ]
    )
    events = agent.feed(user("go"))
    request_id = events[2]["request_id"]
    resumed = agent.feed(control_response(request_id, allow=False))
    assert resumed[-1]["type"] == "result"
    assert resumed[-1]["permission_denials"] == ["Bash"]


def test_tool_result_step_emits_user_event_with_list_content():
    agent = FakeAgent(steps=[{"tool_result": "3 passed, 1 failed"}, {"result": {}}])
    events = agent.feed(user("go"))
    tool_result_event = events[1]
    assert tool_result_event == {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "3 passed, 1 failed"}]},
    }


def test_commit_step_invokes_commit_fn():
    calls = []
    agent = FakeAgent(steps=[{"commit": "wip"}, {"result": {}}], commit_fn=calls.append)
    agent.feed(user("go"))
    assert calls == ["wip"]


def test_result_step_defaults_and_ends_turn():
    agent = FakeAgent(steps=[{"result": {}}, {"say": "never reached this turn"}])
    events = agent.feed(user("go"))
    result = events[-1]
    assert result == {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 1,
        "session_id": agent.session_id,
        "total_cost_usd": 0.0,
        "permission_denials": [],
    }
    # a second user message starts the next turn where the script left off
    more = agent.feed(user("continue"))
    assert more[1] == {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "never reached this turn"}]},
    }


def test_result_step_honors_overrides():
    agent = FakeAgent(steps=[{"result": {"subtype": "error", "is_error": True, "cost": 0.5, "num_turns": 3}}])
    result = agent.feed(user("go"))[-1]
    assert result["subtype"] == "error"
    assert result["is_error"] is True
    assert result["total_cost_usd"] == 0.5
    assert result["num_turns"] == 3


def test_crash_step_emits_crash_sentinel_mid_turn():
    agent = FakeAgent(steps=[{"say": "before"}, {"crash": 7}, {"say": "never"}])
    events = agent.feed(user("go"))
    assert events[-1] == Crash(7)
    assert events[1]["type"] == "assistant"  # "before" was emitted first


def test_sleep_step_emits_sleep_sentinel():
    agent = FakeAgent(steps=[{"sleep": 0.01}, {"result": {}}])
    events = agent.feed(user("go"))
    assert Sleep(0.01) in events


def test_unknown_step_raises():
    agent = FakeAgent(steps=[{"nonsense": True}])
    with pytest.raises(ValueError):
        agent.feed(user("go"))


# ---------------------------------------------------------------- CLI (real subprocess)


def _write_script(tmp_path: Path, steps: list[dict]) -> Path:
    path = tmp_path / "script.json"
    path.write_text(json.dumps({"steps": steps}))
    return path


def test_cli_speaks_stream_json_over_a_real_subprocess(tmp_path):
    script = _write_script(
        tmp_path,
        [
            {"say": "hi"},
            {"result": {"subtype": "success"}},
        ],
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "steward.fake_agent", "--script", str(script)],
        cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=_subprocess_env(),
    )
    try:
        init_line = proc.stdout.readline()
        assert json.loads(init_line)["type"] == "system"

        proc.stdin.write(json.dumps(user("go")) + "\n")
        proc.stdin.flush()

        events = []
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            events.append(json.loads(line))
            if events[-1]["type"] == "result":
                break
        assert [e["type"] for e in events] == ["user", "assistant", "result"]

        proc.stdin.close()
        rc = proc.wait(timeout=5)
        assert rc == 0
    finally:
        proc.stdout.close()
        proc.stderr.close()
        if proc.poll() is None:
            proc.kill()


def test_cli_exits_zero_on_stdin_eof_with_no_input(tmp_path):
    script = _write_script(tmp_path, [])
    proc = subprocess.Popen(
        [sys.executable, "-m", "steward.fake_agent", "--script", str(script)],
        cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=_subprocess_env(),
    )
    proc.stdout.readline()  # system/init
    proc.stdin.close()
    rc = proc.wait(timeout=5)
    assert rc == 0
    proc.stdout.close()
    proc.stderr.close()


def test_cli_crash_step_exits_with_code(tmp_path):
    script = _write_script(tmp_path, [{"crash": 9}])
    proc = subprocess.Popen(
        [sys.executable, "-m", "steward.fake_agent", "--script", str(script)],
        cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=_subprocess_env(),
    )
    proc.stdout.readline()  # system/init
    proc.stdin.write(json.dumps(user("go")) + "\n")
    proc.stdin.flush()
    proc.stdin.close()
    rc = proc.wait(timeout=5)
    assert rc == 9
    proc.stdout.close()
    proc.stderr.close()
