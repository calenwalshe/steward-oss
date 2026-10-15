"""Shim robustness sweep: no agent event, inbound frame or stream line may raise out of the shim loop.

Table-driven: every handler is fed unknown subtypes, missing fields, wrong types and garbage."""

from __future__ import annotations

import os
import threading

import pytest

from steward.fabric import Frame
from steward.shim import Shim, _batch_input, _extract_plan_items, _tool_result_text


class _Api:
    def __init__(self):
        self.sent = []

    def send(self, pane, frame):
        self.sent.append(frame)

    def call(self, *a, **k):
        return {}


class _Mailbox:
    def __init__(self, frames=()):
        self.frames = list(frames)

    def read(self, timeout):
        out, self.frames = self.frames, []
        return out


class _Proc:
    def __init__(self, fd):
        self.stdin = None
        self.stdout = os.fdopen(fd, "rb", buffering=0)
        self.stderr = None

    def poll(self):
        return None


def _shim(**kw):
    lines = []
    s = Shim(job="j", peer_pane="p", api=_Api(), mailbox=_Mailbox(), spawn=lambda a: None, agent_argv=[],
             print_fn=lines.append, **kw)
    s.lines = lines
    return s


_BAD = [None, 0, 1.5, True, "", "str", [], [1, "x", None], {}, {"x": 1}, [[]], [{}]]

_EVENTS = [
    [], "x", 7, None,
    {}, {"type": None}, {"type": ["list"]}, {"type": {"a": 1}}, {"type": "mystery"},
    {"type": "system"}, {"type": "system", "subtype": "status"}, {"type": "system", "subtype": "compact_boundary"},
    {"type": "system", "subtype": "init"}, {"type": "system", "subtype": "init", "session_id": ["x"]},
    {"type": "system", "subtype": "init", "session_id": "s", "model": 5},
    {"type": "system", "subtype": ["init"], "session_id": {"a": 1}},
    {"type": "user"}, {"type": "user", "message": None}, {"type": "user", "message": "str"},
    {"type": "user", "message": {"content": None}}, {"type": "user", "message": {"content": 5}},
    {"type": "user", "message": {"content": "[rev=notnum]"}},
    {"type": "user", "message": {"content": [None, 1, "x", {}, {"type": "tool_result"},
                                              {"type": "tool_result", "tool_use_id": ["x"], "content": 5},
                                              {"type": "tool_result", "tool_use_id": {}, "content": [None, {"type": "text", "text": 3}]}]}},
    {"type": "assistant"}, {"type": "assistant", "message": None}, {"type": "assistant", "message": "s"},
    {"type": "assistant", "message": {"content": "text"}}, {"type": "assistant", "message": {"content": {"a": 1}}},
    {"type": "assistant", "message": {"content": [None, 3, {"type": "tool_use"},
                                                   {"type": "tool_use", "name": 5, "id": ["x"], "input": "s"},
                                                   {"type": "tool_use", "name": ["Bash"], "id": {}, "input": []},
                                                   {"type": "tool_use", "name": "Bash", "id": "a", "input": "s"},
                                                   {"type": "tool_use", "name": "TodoWrite", "id": "b", "input": {"todos": 5}},
                                                   {"type": "tool_use", "name": "TodoWrite", "id": "c",
                                                    "input": {"todos": [None, 1, {"content": 5}, {"title": ["x"]}]}}]}},
    {"type": "control_request"}, {"type": "control_request", "request": None},
    {"type": "control_request", "request": "s"}, {"type": "control_request", "request": {"subtype": ["x"]}},
    {"type": "control_request", "request": {"subtype": "hook_callback"}},
    {"type": "control_request", "request_id": ["u"], "request": {"subtype": "can_use_tool"}},
    {"type": "control_request", "request_id": {}, "request": {"subtype": "can_use_tool", "input": 5}},
    {"type": "control_request", "request_id": "r", "request": {"subtype": "can_use_tool", "input": "s", "tool_name": 4}},
    {"type": "control_response"}, {"type": "control_response", "response": "s"},
    {"type": "control_response", "response": {"request_id": ["x"]}}, {"type": "control_response", "response": []},
    {"type": "result"}, {"type": "result", "usage": "s"}, {"type": "result", "usage": [1]},
    {"type": "result", "subtype": ["x"], "is_error": "yes", "total_cost_usd": "free", "usage": {"input_tokens": "x"}},
    {"type": "result", "subtype": {}, "total_cost_usd": [1], "usage": {"output_tokens": None}},
]


@pytest.mark.parametrize("event", _EVENTS, ids=lambda e: repr(e)[:70])
def test_agent_event_never_raises_via_guard(event):
    s = _shim()
    s.on_session = lambda sid: 1 / 0
    s._guard("agent event", s._handle_agent_event, event)
    # the strict claim: handlers themselves tolerate the input, the guard is the second line of defence
    s2 = _shim()
    s2._handle_agent_event(event)


@pytest.mark.parametrize("event", _EVENTS, ids=lambda e: repr(e)[:70])
def test_shim_still_works_after_bad_event(event):
    s = _shim()
    s._handle_agent_event(event)
    s._handle_agent_event({"type": "system", "subtype": "init", "session_id": "ok"})
    assert s._session_id == "ok"


def test_non_init_system_event_sends_no_identity():
    s = _shim()
    s._handle_agent_event({"type": "system", "subtype": "status", "session_id": "x"})
    assert s.api.sent == []


_FRAMES = [
    ("brief", {}), ("brief", {"text": None}), ("brief", {"text": ["x"]}),
    ("steer", {}), ("steer", {"rev": "1"}), ("steer", {"rev": None}), ("steer", {"rev": [1]}),
    ("steer", {"rev": True}), ("steer", {"rev": 1, "text": 5, "urgent": 1}),
    ("approve", {}), ("approve", {"request_id": ["x"]}), ("deny", {"request_id": 5, "message": 4}),
    ("deny", {"request_id": "nope", "message": ["m"]}),
    ("stop", {}), ("event", {}), ("done", {}), ("permission_request", {}),
    ("ack", {}), ("ack", {"acked_seq": "x"}), ("ack", {"acked_seq": [1]}), ("ack", {"acked_seq": None}),
    ("cmd", {}), ("cmd", {"op": None}), ("cmd", {"op": ["x"]}), ("cmd", {"op": "mystery"}),
    ("cmd", {"op": "rebind"}), ("cmd", {"op": "rebind", "peer": 5}), ("cmd", {"op": "rebind", "peer": ["p"]}),
    ("cmd", {"op": "list_permissions"}),
    ("cmd", {"op": "interrupt_waits"}), ("cmd", {"op": "interrupt_waits", "call_ids": 5, "gen": []}),
    ("cmd", {"op": "interrupt_waits", "call_ids": [[1], {}, "a"], "gen": {}}),
    ("steer", "not-a-dict"), ("brief", None), ("cmd", [1, 2]),
]


@pytest.mark.parametrize("ftype,body", _FRAMES, ids=lambda x: repr(x)[:50])
def test_inbound_frame_never_raises(ftype, body):
    s = _shim()
    s._handle_inbound(Frame(v=1, seq=1, job="j", type=ftype, body=body))


def test_step_guard_survives_raising_handlers():
    class Boom(_Mailbox):
        def read(self, timeout):
            return [Frame(v=1, seq=1, job="j", type="stop", body={})]

    s = _shim()
    s.mailbox = Boom()
    s._handle_inbound = lambda f: 1 / 0
    s._tick_outbox = lambda: 1 / 0
    s._check_stop_grace = lambda: 1 / 0
    s.step(0)  # must not raise
    assert any("shim error in inbound frame" in l for l in s.lines)


def test_mailbox_read_error_is_guarded():
    class Bad:
        def read(self, timeout):
            raise RuntimeError("tty gone")

    s = _shim()
    s.mailbox = Bad()
    s.step(0)
    assert any("mailbox_read" in l for l in s.lines)


_STREAMS = [
    b"\n\n", b"\xff\xfe\xfd\n", b"{not json\n", b"[1,2,3]\n", b"\"str\"\n", b"null\n", b"42\n",
    b'{"type":"assistant","message":{"content":"x"}}\n', b'{"type": "system", "subtype": "x"}\n',
    b"[" * 100000 + b"\n",  # recursion bomb
    b'{"type":"resu', # partial line, never completed
    b'{"type":"system","subtype":"init","session_id":"ok"}\n',
]


@pytest.mark.parametrize("data", _STREAMS, ids=lambda d: repr(d)[:40])
def test_drain_stdout_garbage_never_raises(data):
    r, w = os.pipe()
    s = _shim()
    s._agent = _Proc(r)
    def feed():  # off-thread: data may exceed the 64 KiB pipe buffer, which would block a same-thread write
        os.write(w, data)
        os.close(w)

    writer = threading.Thread(target=feed)
    writer.start()
    s._drain_agent_stdout(0.5)
    writer.join(5)
    assert not writer.is_alive()
    s._drain_agent_stdout(0.05)  # EOF'd pipe again


def test_drain_stdout_partial_line_then_rest():
    r, w = os.pipe()
    s = _shim()
    s._agent = _Proc(r)
    os.write(w, b'{"type":"system","subtype":"init",')
    s._drain_agent_stdout(0.1)
    assert s._session_id is None
    os.write(w, b'"session_id":"abc"}\n')
    s._drain_agent_stdout(0.1)
    assert s._session_id == "abc"
    os.close(w)


def test_drain_stdout_closed_pipe():
    r, w = os.pipe()
    s = _shim()
    s._agent = _Proc(r)
    s._agent.stdout.close()
    s._drain_agent_stdout(0.05)
    os.close(w)


def test_result_with_raising_git_still_sends_done():
    s = _shim()
    s.git = lambda: 1 / 0
    s._handle_result({"type": "result", "subtype": "success"})
    assert [f.type for f in s.api.sent] == ["done"]


@pytest.mark.parametrize("bad", _BAD, ids=repr)
def test_helpers_tolerate_odd_values(bad):
    _batch_input("Bash", bad)
    _batch_input(bad if isinstance(bad, str) else "Bash", {})
    _extract_plan_items({"todos": bad})
    _extract_plan_items({"items": bad})
    _tool_result_text({"content": bad})
    _tool_result_text({"content": [bad, {"type": "text", "text": bad}]})
