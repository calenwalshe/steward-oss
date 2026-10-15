"""Tests for steward/shim.py (WRK-01, WRK-02).

No real Herdr (`HerdrApi` is a `FakeApi` recording sends/calls) and no model
(the agent is a real `python -m steward.fake_agent` subprocess, driven
through actual pipes — this is the "unit tests use steward.fake_agent" case
from the plan). Inbound frames are written into an `os.pipe()` wrapped by the
real `steward.herdr_transport.Mailbox`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from steward import cli
from steward.contract import propose_contract, store_contract
from steward.discovery import Discovery
from steward.fabric import Frame, encode
from steward.herdr_transport import HerdrApiError, HerdrUnavailable, Mailbox
from steward.shim import Shim
from steward.store import Store

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _subprocess_env() -> dict:
    return {**os.environ, "PYTHONPATH": str(_REPO_ROOT)}


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    """A real git repo (the fake agent's `commit` step and the shim's `git`
    injectable both need one)."""
    _git("init", "-q", cwd=tmp_path)
    _git("config", "user.email", "t@example.com", cwd=tmp_path)
    _git("config", "user.name", "t", cwd=tmp_path)
    _git("commit", "--allow-empty", "-q", "-m", "root", cwd=tmp_path)
    return tmp_path


def _write_script(repo: Path, steps: list[dict]) -> Path:
    path = repo / "script.json"
    path.write_text(json.dumps({"steps": steps}))
    return path


class FakeApi:
    def __init__(self, *, call_raises: Exception | None = None):
        self.sent: list[tuple[str, Frame]] = []
        self.calls: list[tuple[str, dict]] = []
        self.call_raises = call_raises

    def send(self, pane_id: str, frame: Frame) -> dict:
        self.sent.append((pane_id, frame))
        return {"type": "ok"}

    def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if self.call_raises is not None:
            raise self.call_raises
        return {}


def _make_shim(
    repo: Path, script: Path, api: FakeApi, read_fd: int, *,
    own_pane: str | None = None, timeout_s: float = 0.5, max_retries: int = 3,
) -> Shim:
    def spawn(argv: list[str]):
        return subprocess.Popen(
            argv, cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=_subprocess_env(),
        )

    def git_head() -> str | None:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None

    agent_argv = [sys.executable, "-m", "steward.fake_agent", "--script", str(script)]
    return Shim(
        job="jobA", peer_pane="w1:p2", api=api, mailbox=Mailbox(fd=read_fd),
        spawn=spawn, agent_argv=agent_argv, git=git_head, own_pane=own_pane,
        timeout_s=timeout_s, max_retries=max_retries,
    )


def _send_frame(write_fd: int, seq: int, type_: str, body: dict) -> None:
    frame = Frame(v=1, seq=seq, job="jobA", type=type_, body=body)
    os.write(write_fd, (encode(frame) + "\n").encode())


def _run_until(shim: Shim, predicate, *, timeout: float = 10.0, poll: float = 0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        shim.step(poll)
        if predicate():
            return
    pytest.fail("condition not met before timeout")


def _sent_of_type(api: FakeApi, type_: str) -> list[Frame]:
    return [f for _, f in api.sent if f.type == type_]


# ---------------------------------------------------------------- ready handshake


def test_start_sends_ready_event_before_spawning_or_reading(repo):
    """10-03 finding 1: `ready` must be the very first frame out, before the
    agent is even spawned, so `FabricWorker` never sends the brief into a
    pane whose shell hasn't yet been replaced by this shim's raw-mode
    reader."""
    script = _write_script(repo, [])
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.start()
        assert api.sent[0][1].type == "event"
        assert api.sent[0][1].body["kind"] == "ready"
        assert isinstance(api.sent[0][1].body["pid"], int)
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_claude_init_and_result_carry_model_identity_and_reported_usage(repo):
    script = _write_script(repo, [])
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim._handle_agent_event({"type": "system", "subtype": "init", "session_id": "session-123",
                                  "model": "claude-sonnet-4-5"})
        identity = [f for f in _sent_of_type(api, "event") if f.body.get("kind") == "identity"]
        assert len(identity) == 1
        assert identity[0].body == {"kind": "identity", "session_id": "session-123",
                                    "model": "claude-sonnet-4-5"}

        shim._handle_agent_event({"type": "result", "subtype": "success", "usage": {
            "input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 3, "untrusted": "ignore"}})
        done = _sent_of_type(api, "done")[-1]
        assert done.body["input_tokens"] == 10 and done.body["output_tokens"] == 4
        assert done.body["cache_read_input_tokens"] == 3 and "untrusted" not in done.body
    finally:
        os.close(write_fd)
        os.close(read_fd)



def test_non_init_system_events_do_not_crash_the_shim(repo):
    """2026-10-06: a system event without subtype init (status, compaction, hooks) raised UnboundLocalError on
    `identity` and killed the worker shim (job 84bc6350)."""
    script = _write_script(repo, [])
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim._handle_agent_event({"type": "system", "subtype": "status", "session_id": "session-9"})
        shim._handle_agent_event({"type": "system", "subtype": "compact_boundary"})
        assert not [f for f in _sent_of_type(api, "event") if f.body.get("kind") == "identity"]
        shim._handle_agent_event({"type": "system", "subtype": "init", "session_id": "session-9"})
        assert len([f for f in _sent_of_type(api, "event") if f.body.get("kind") == "identity"]) == 1
    finally:
        os.close(write_fd)
        os.close(read_fd)

# ---------------------------------------------------------------- full scripted run


def test_full_scripted_run_brief_steer_permission_commit_done(repo):
    script = _write_script(
        repo,
        [
            {"say": "on it"},
            {"tool_use": {"name": "Bash", "input": {"command": "touch x"}, "permission": True}},
            {"commit": "did the thing"},
            {"result": {"subtype": "success", "cost": 0.02}},
        ],
    )
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.start()

        _send_frame(write_fd, 1, "brief", {"text": "do the thing"})
        _run_until(shim, lambda: _sent_of_type(api, "permission_request"))

        permission = _sent_of_type(api, "permission_request")[0]
        request_id = permission.body["request_id"]
        assert permission.body["tool"] == "Bash"

        # steer mid-turn: replayed immediately, folded into the running turn
        _send_frame(write_fd, 2, "steer", {"rev": 7, "text": "change of plan"})
        _run_until(shim, lambda: any(f.body.get("stage") == "delivered" for f in _sent_of_type(api, "ack")))
        _run_until(shim, lambda: any(f.body.get("stage") == "acknowledged" for f in _sent_of_type(api, "ack")))
        acks = [{k: v for k, v in f.body.items() if k != "at"} for f in _sent_of_type(api, "ack") if "rev" in f.body]
        assert {"rev": 7, "stage": "delivered"} in acks
        assert {"rev": 7, "stage": "acknowledged"} in acks  # (+ `at`, the replay time: latency #2)

        _send_frame(write_fd, 3, "approve", {"request_id": request_id})
        _run_until(shim, lambda: _sent_of_type(api, "done"))

        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
        done = _sent_of_type(api, "done")[0]
        assert done.body == {"sha": head, "result_subtype": "success", "is_error": False, "cost": 0.02,
                             "unconsumed_revs": [], "last_rev": 7}

        # low-level transport acks (acked_seq) were sent for every inbound business frame
        transport_acks = [f.body["acked_seq"] for f in _sent_of_type(api, "ack") if "acked_seq" in f.body]
        assert {1, 2, 3}.issubset(set(transport_acks))
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_deny_path_records_denial_and_turn_continues(repo):
    script = _write_script(
        repo,
        [
            {"tool_use": {"name": "Bash", "input": {}, "permission": True}},
            {"say": "ok, skipping that"},
            {"result": {"subtype": "success"}},
        ],
    )
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.start()
        _send_frame(write_fd, 1, "brief", {"text": "go"})
        _run_until(shim, lambda: _sent_of_type(api, "permission_request"))
        request_id = _sent_of_type(api, "permission_request")[0].body["request_id"]

        _send_frame(write_fd, 2, "deny", {"request_id": request_id})
        _run_until(shim, lambda: _sent_of_type(api, "done"))
        assert _sent_of_type(api, "done")[0].body["is_error"] is False
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_crash_mid_turn_sends_error_frame(repo):
    script = _write_script(repo, [{"say": "about to crash"}, {"crash": 3}])
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.start()
        _send_frame(write_fd, 1, "brief", {"text": "go"})
        _run_until(shim, lambda: _sent_of_type(api, "error"))
        error = _sent_of_type(api, "error")[0]
        assert error.body["exit_code"] == 3
        assert shim.exit_code == 3
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_stop_closes_agent_stdin_and_exit_becomes_error_frame(repo):
    script = _write_script(repo, [])
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.stop_grace_s = 2.0
        shim.start()
        _send_frame(write_fd, 1, "stop", {})
        _run_until(shim, lambda: _sent_of_type(api, "error"))
        error = _sent_of_type(api, "error")[0]
        assert error.body["exit_code"] == 0  # fake agent exits 0 on stdin EOF
    finally:
        os.close(write_fd)
        os.close(read_fd)


# ---------------------------------------------------------------- protocol edges


def test_duplicate_inbound_frame_is_ignored_once_delivered(repo):
    script = _write_script(repo, [])
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.start()
        frame = Frame(v=1, seq=1, job="jobA", type="brief", body={"text": "go"})
        line = (encode(frame) + "\n").encode()
        os.write(write_fd, line)
        os.write(write_fd, line)  # exact duplicate: same (job, seq)
        shim.step(0.3)
        # the FrameReader dedupes by (job, seq): only one low-level ack for seq=1
        transport_acks = [f for f in _sent_of_type(api, "ack") if f.body.get("acked_seq") == 1]
        assert len(transport_acks) == 1
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_unknown_frame_type_is_logged_and_ignored(repo):
    script = _write_script(repo, [])
    api = FakeApi()
    printed = []
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.print_fn = printed.append
        shim.start()
        _send_frame(write_fd, 1, "cmd", {"text": "unexpected"})
        shim.step(0.3)
        assert any("ignoring" in line and "cmd" in line for line in printed)
    finally:
        os.close(write_fd)
        os.close(read_fd)


# ---------------------------------------------------------------- controller-restart rebind


def test_cmd_rebind_switches_peer_and_resends_unacked_outbox(repo):
    """10-04: a `cmd {op: rebind, peer}` from the (new) runner switches this
    shim's send target and re-sends anything still unacked there — nothing
    sent while the old runner pane was down is lost."""
    script = _write_script(repo, [])
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.start()
        # an event sent to the OLD peer, never acked (the old runner pane is gone)
        unacked = shim._send("event", {"kind": "activity", "tool": "Bash"})

        _send_frame(write_fd, 5, "cmd", {"op": "rebind", "peer": "w2:p9"})
        shim.step(0.3)

        assert shim.peer_pane == "w2:p9"
        # the transport ack for the rebind frame itself went to the NEW peer
        acks_to_new_peer = [f for pane, f in api.sent if pane == "w2:p9" and f.type == "ack"]
        assert any(f.body.get("acked_seq") == 5 for f in acks_to_new_peer)
        # the unacked event got resent into the new peer's pane
        resent = [f for pane, f in api.sent if pane == "w2:p9" and f.type == "event"]
        assert any(f.seq == unacked.seq for f in resent)
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_cmd_rebind_with_no_peer_is_ignored(repo):
    script = _write_script(repo, [])
    api = FakeApi()
    printed = []
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.print_fn = printed.append
        shim.start()
        original_peer = shim.peer_pane

        _send_frame(write_fd, 1, "cmd", {"op": "rebind"})
        shim.step(0.3)

        assert shim.peer_pane == original_peer
        assert any("ignoring" in line and "rebind" in line for line in printed)
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_cmd_unknown_op_is_ignored(repo):
    script = _write_script(repo, [])
    api = FakeApi()
    printed = []
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.print_fn = printed.append
        shim.start()

        _send_frame(write_fd, 1, "cmd", {"op": "reboot"})
        shim.step(0.3)

        assert any("ignoring" in line and "cmd" in line for line in printed)
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_printed_lines_end_with_carriage_return_for_raw_tty(repo):
    """10-03 finding 2: while the mailbox has this process's tty in raw mode
    (OPOST off), a bare `\\n` doesn't return the carriage — every line must
    end `\\r\\n` or the pane shows diagonal "staircase" text."""
    script = _write_script(repo, [])
    api = FakeApi()
    printed = []
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.print_fn = printed.append
        shim.start()
        assert printed  # the ready-frame log line, at least
        assert all(line.endswith("\r") for line in printed)
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_peer_transport_ack_clears_outbox_and_stops_retransmit(repo):
    script = _write_script(repo, [])
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd, timeout_s=0.05, max_retries=5)
        shim.start()  # sends the "ready" event first; not the frame under test
        frame = shim._send("event", {"kind": "activity", "tool": "Bash"})
        assert shim.outbox.pending("jobA", frame.seq)

        time.sleep(0.1)
        shim.step(0.05)
        activity_sent = [f for _, f in api.sent if f.type == "event" and f.body.get("kind") == "activity"]
        assert len(activity_sent) == 2  # original + one retransmit

        _send_frame(write_fd, 99, "ack", {"acked_seq": frame.seq})
        shim.step(0.3)
        assert not shim.outbox.pending("jobA", frame.seq)
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_fleet_shim_emits_v2_identity_and_rejects_stale_control(repo):
    script = _write_script(repo, [])
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.task, shim.lane, shim.attempt, shim.epoch = "task-a", "lane-task-a", 2, 7
        shim.start()
        ready = _sent_of_type(api, "event")[0]
        assert (ready.v, ready.task, ready.lane, ready.attempt, ready.epoch, ready.sender) == (
            2, "task-a", "lane-task-a", 2, 7, "shim:lane-task-a")

        stale = Frame(v=2, seq=3, job="jobA", type="stop", body={}, task="task-a", lane="lane-task-a",
                      attempt=1, sender="controller:lane-task-a", epoch=6)
        shim._handle_inbound(stale)
        assert not [f for f in _sent_of_type(api, "ack") if f.body.get("acked_seq") == stale.seq]
        assert not shim._stopping
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_report_agent_best_effort_swallows_herdr_errors(repo):
    script = _write_script(repo, [])
    api = FakeApi(call_raises=HerdrUnavailable("no herdr"))
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd, own_pane="w1:p1")
        shim.start()  # must not raise even though report_agent fails
        assert api.calls  # attempted
        _send_frame(write_fd, 1, "brief", {"text": "go"})
        shim.step(0.3)  # still must not raise
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_report_agent_skipped_without_own_pane(repo):
    script = _write_script(repo, [])
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd, own_pane=None)
        shim.start()
        assert api.calls == []
    finally:
        os.close(write_fd)
        os.close(read_fd)


# ---------------------------------------------------------------- plan + test_result events (TRJ-04)


def _sent_events_of_kind(api: FakeApi, kind: str) -> list[dict]:
    return [f.body for _, f in api.sent if f.type == "event" and f.body.get("kind") == kind]


def test_todowrite_tool_use_sends_plan_event(repo):
    script = _write_script(
        repo,
        [
            {
                "tool_use": {
                    "name": "TodoWrite",
                    "input": {"todos": [{"content": "write tests"}, {"content": "implement"}]},
                }
            },
            {"result": {"subtype": "success"}},
        ],
    )
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.start()
        _send_frame(write_fd, 1, "brief", {"text": "go"})
        _run_until(shim, lambda: _sent_events_of_kind(api, "plan"))

        plans = _sent_events_of_kind(api, "plan")
        assert plans[0]["items"] == ["write tests", "implement"]
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_todowrite_plan_event_uses_title_fallback_and_is_not_throttled(repo):
    """Not throttled by the activity throttle: two TodoWrite tool_use calls
    in immediate succession both produce a plan event even with a large
    activity_throttle_s."""
    script = _write_script(
        repo,
        [
            {"tool_use": {"name": "TodoWrite", "input": {"todos": [{"title": "first plan"}]}}},
            {"tool_use": {"name": "TodoWrite", "input": {"todos": [{"title": "second plan"}]}}},
            {"result": {"subtype": "success"}},
        ],
    )
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.activity_throttle_s = 1000.0
        shim.start()
        _send_frame(write_fd, 1, "brief", {"text": "go"})
        _run_until(shim, lambda: len(_sent_events_of_kind(api, "plan")) >= 2)

        plans = _sent_events_of_kind(api, "plan")
        assert plans[0]["items"] == ["first plan"]
        assert plans[1]["items"] == ["second plan"]
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_todowrite_plan_event_caps_item_count_and_length(repo):
    todos = [{"content": f"item {i} " + ("x" * 100)} for i in range(25)]
    script = _write_script(
        repo,
        [
            {"tool_use": {"name": "TodoWrite", "input": {"todos": todos}}},
            {"result": {"subtype": "success"}},
        ],
    )
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.start()
        _send_frame(write_fd, 1, "brief", {"text": "go"})
        _run_until(shim, lambda: _sent_events_of_kind(api, "plan"))

        items = _sent_events_of_kind(api, "plan")[0]["items"]
        assert len(items) == 20
        assert all(len(item) <= 80 for item in items)
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_pytest_tool_result_summary_sends_test_result_event(repo):
    script = _write_script(
        repo,
        [
            {"tool_result": "===== 3 passed, 1 failed, 2 error in 1.23s ====="},
            {"result": {"subtype": "success"}},
        ],
    )
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.start()
        _send_frame(write_fd, 1, "brief", {"text": "go"})
        _run_until(shim, lambda: _sent_events_of_kind(api, "test_result"))

        result = _sent_events_of_kind(api, "test_result")[0]
        assert result == {"kind": "test_result", "passed": 3, "failed": 1, "errors": 2, "source": "worker_observed"}
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_jest_tool_result_summary_sends_test_result_event(repo):
    script = _write_script(
        repo,
        [
            {"tool_result": "Tests: 1 failed, 5 passed, 6 total"},
            {"result": {"subtype": "success"}},
        ],
    )
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.start()
        _send_frame(write_fd, 1, "brief", {"text": "go"})
        _run_until(shim, lambda: _sent_events_of_kind(api, "test_result"))

        result = _sent_events_of_kind(api, "test_result")[0]
        assert result["kind"] == "test_result"
        assert result["passed"] == 5
        assert result["source"] == "worker_observed"
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_go_ok_line_sends_test_result_event(repo):
    script = _write_script(
        repo,
        [
            {"tool_result": "ok  \tsteward/foo\t0.003s"},
            {"result": {"subtype": "success"}},
        ],
    )
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.start()
        _send_frame(write_fd, 1, "brief", {"text": "go"})
        _run_until(shim, lambda: _sent_events_of_kind(api, "test_result"))

        result = _sent_events_of_kind(api, "test_result")[0]
        assert result == {"kind": "test_result", "passed": 1, "failed": 0, "errors": 0, "source": "worker_observed"}
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_go_fail_line_sends_test_result_event(repo):
    script = _write_script(
        repo,
        [
            {"tool_result": "FAIL\tsteward/foo\t0.003s"},
            {"result": {"subtype": "success"}},
        ],
    )
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.start()
        _send_frame(write_fd, 1, "brief", {"text": "go"})
        _run_until(shim, lambda: _sent_events_of_kind(api, "test_result"))

        result = _sent_events_of_kind(api, "test_result")[0]
        assert result == {"kind": "test_result", "passed": 0, "failed": 1, "errors": 0, "source": "worker_observed"}
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_non_summary_tool_result_sends_no_test_result_frame(repo):
    script = _write_script(
        repo,
        [
            {"tool_result": "just some ordinary tool output, nothing to see here"},
            {"say": "done"},
            {"result": {"subtype": "success"}},
        ],
    )
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.start()
        _send_frame(write_fd, 1, "brief", {"text": "go"})
        _run_until(shim, lambda: _sent_of_type(api, "done"))

        assert _sent_events_of_kind(api, "test_result") == []
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_string_content_user_event_still_uses_replay_ack_path(repo):
    """Existing shim frames/tests are unchanged: a steer's `[rev=N]` replay
    still resolves through the string-content path, not the new list-content
    tool_result scan."""
    script = _write_script(repo, [{"result": {"subtype": "success"}}])
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.start()
        _send_frame(write_fd, 1, "steer", {"rev": 3, "text": "change of plan"})
        _run_until(shim, lambda: any(f.body.get("stage") == "acknowledged" for f in _sent_of_type(api, "ack")))
        acks = [{k: v for k, v in f.body.items() if k != "at"} for f in _sent_of_type(api, "ack") if "rev" in f.body]
        assert {"rev": 3, "stage": "acknowledged"} in acks
    finally:
        os.close(write_fd)
        os.close(read_fd)


# ---------------------------------------------------------------- CLI argv construction


def test_build_shim_agent_argv_fake(steward_home):
    store = Store()  # unused by the fake branch
    argv = cli._build_shim_agent_argv(
        store, "jobA", agent="fake", agent_script="/tmp/script.json", model=None, extra_args=[]
    )
    assert argv == [sys.executable, "-m", "steward.fake_agent", "--script", "/tmp/script.json"]


def test_build_shim_agent_argv_claude_has_required_flags(steward_home):
    store = Store()
    job = store.create_job("goal", "/repo")
    discovery = Discovery(commands=[], instruction_files=[], languages=[])
    contract = propose_contract(job.goal, discovery, baseline=None)
    store_contract(store, job.job_id, contract)

    argv = cli._build_shim_agent_argv(
        store, job.job_id, agent="claude", agent_script=None, model=None, extra_args=[]
    )

    assert argv[:4] == ["env", "-u", "ANTHROPIC_API_KEY", "claude"]
    assert "-p" in argv
    assert "--input-format" in argv and "stream-json" in argv
    assert "--output-format" in argv and "stream-json" in argv
    assert "--permission-prompt-tool" in argv
    assert argv[argv.index("--permission-prompt-tool") + 1] == "stdio"
    assert "--replay-user-messages" in argv
    assert "--setting-sources" in argv
    assert argv[argv.index("--setting-sources") + 1] == "project"
    # S1 (D5): Agent calls must reach Steward's permission hook so a background subagent can be denied
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert "Agent" in settings["permissions"]["ask"]
    assert argv.index("--settings") < argv.index("--allowedTools")
    # contract-derived one-time authorization (no network authority never becomes a tool)
    assert "--permission-mode" in argv
    assert "acceptEdits" in argv


def test_build_shim_agent_argv_claude_appends_model_and_extra_args(steward_home):
    store = Store()
    job = store.create_job("goal", "/repo")
    discovery = Discovery(commands=[], instruction_files=[], languages=[])
    store_contract(store, job.job_id, propose_contract(job.goal, discovery, baseline=None))

    argv = cli._build_shim_agent_argv(
        store, job.job_id, agent="claude", agent_script=None, model="haiku", extra_args=["--verbose"]
    )
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[-1] == "--verbose"


def test_build_shim_agent_argv_claude_with_no_contract_still_has_base_flags(steward_home):
    store = Store()
    argv = cli._build_shim_agent_argv(
        store, "no-such-job", agent="claude", agent_script=None, model=None, extra_args=[]
    )
    assert "--permission-prompt-tool" in argv
    assert "--setting-sources" in argv


def test_shim_run_command_passes_dash_prefixed_agent_args_through_argparse():
    """Chair fix (10-03): `--agent-arg --model` as two tokens breaks the shim's argparse."""
    import shlex
    from steward.cli import _build_parser, _shim_run_command

    line = _shim_run_command(
        "job1", sock="/s", peer_pane="w1:p1", agent="claude", agent_script=None,
        model=None, extra_args=["--model", "sonnet"], steward_home=None,
    )
    argv = shlex.split(line.split("&& ", 1)[1])
    args = _build_parser().parse_args(argv[argv.index("shim"):])
    assert args.agent_arg == ["--model", "sonnet"]


def test_shim_run_command_carries_complete_fleet_identity():
    import shlex
    from steward.cli import _build_parser, _shim_run_command

    line = _shim_run_command("jobA", sock="/sock", peer_pane="w:p1", agent="claude", agent_script=None,
                             model=None, extra_args=[], steward_home=None, task="t1", lane="lane-t1",
                             attempt=2, epoch=9)
    argv = shlex.split(line.split("&& ", 1)[1])
    args = _build_parser().parse_args(argv[argv.index("shim"):])
    assert (args.task, args.lane, args.attempt, args.epoch) == ("t1", "lane-t1", 2, 9)


def test_shim_run_command_rejects_partial_fleet_identity():
    from steward.cli import _shim_run_command

    with pytest.raises(ValueError, match="requires task, lane, attempt and epoch"):
        _shim_run_command("jobA", sock="/sock", peer_pane="w:p1", agent="claude", agent_script=None,
                          model=None, extra_args=[], steward_home=None, task="t1")
