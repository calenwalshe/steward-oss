"""Tests for steward/herdr_transport.py (FAB-03, task 09-02.2).

No real Herdr: `HerdrApi`/`LifecycleWatcher` are exercised against a small
threaded fake Unix-socket server (`FakeHerdrServer`); `Mailbox` is exercised
against an `os.pipe()`.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import threading
import time
import uuid
from pathlib import Path

import pytest

from steward.fabric import Frame, encode
from steward.herdr_transport import (
    Endpoint,
    HerdrApi,
    HerdrApiError,
    HerdrUnavailable,
    LifecycleWatcher,
    Mailbox,
    FleetMailbox,
)


# ---------------------------------------------------------------- fake server

# Sockets live in a short, unique directory inside the checkout, never in $TMPDIR: a deep TMPDIR (e.g. pytest's
# basetemp inside a Steward job worktree) overflows the ~108-byte AF_UNIX path limit ("AF_UNIX path too long").
SOCKET_ROOT = Path(__file__).resolve().parent.parent / ".herdr-test-sockets"
AF_UNIX_MAX = 100  # bytes; sun_path is 108 on Linux, 104 on macOS


def short_socket_path(directory: Path, name: str = "h.sock") -> str:
    """The shorter of the cwd-relative and absolute spellings of `directory/name`; server and clients use this one
    string (no test here changes the cwd while a server is up)."""
    full = os.path.abspath(directory / name)
    try:
        rel = os.path.relpath(full)
    except ValueError:
        rel = full
    path = min((rel, full), key=lambda p: len(p.encode()))
    if len(path.encode()) > AF_UNIX_MAX:
        raise RuntimeError(f"socket path too long even when shortened: {path!r}")
    return path


class FakeHerdrServer:
    """Threaded AF_UNIX server. `handler(conn, request) -> None` decides what
    (if anything) to write back; it owns the connection until it returns."""

    def __init__(self, handler):
        self.dir = SOCKET_ROOT / uuid.uuid4().hex[:8]
        self.dir.mkdir(parents=True)
        self.path = short_socket_path(self.dir)
        self.handler = handler
        self.requests: list[dict] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(8)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            self._server.settimeout(0.2)
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            request = json.loads(buf.split(b"\n", 1)[0]) if buf.strip() else {}
            self.requests.append(request)
            try:
                self.handler(conn, request)
            finally:
                conn.close()

    def stop(self):
        self._stop = True
        self._server.close()
        self._thread.join(timeout=1)
        shutil.rmtree(self.dir, ignore_errors=True)
        try:
            SOCKET_ROOT.rmdir()
        except OSError:  # not empty (a concurrent server) or already gone
            pass


def reply_with(result):
    def handler(conn, request):
        conn.sendall((json.dumps({"id": request.get("id", ""), "result": result}) + "\n").encode())

    return handler


# ---------------------------------------------------------------- HerdrApi


def test_api_call_returns_result():
    server = FakeHerdrServer(reply_with({"ok": True}))
    try:
        api = HerdrApi(server.path)
        result = api.call("pane.list", {})
        assert result == {"ok": True}
        assert server.requests[0]["method"] == "pane.list"
    finally:
        server.stop()


def test_api_call_raises_typed_error():
    def handler(conn, request):
        conn.sendall((json.dumps({"id": request["id"], "error": {"code": "invalid_request", "message": "nope"}}) + "\n").encode())

    server = FakeHerdrServer(handler)
    try:
        api = HerdrApi(server.path)
        with pytest.raises(HerdrApiError) as exc_info:
            api.call("pane.split", {})
        assert exc_info.value.code == "invalid_request"
        assert exc_info.value.message == "nope"
    finally:
        server.stop()


def test_fleet_mailbox_routes_frames_to_lane_endpoints_without_stealing():
    class Source:
        def __init__(self):
            self.frames = []

        def read(self, timeout):
            out, self.frames = self.frames, []
            return out

    source = Source()
    mux = FleetMailbox(source)
    a = mux.endpoint("job", "a", "lane-a", 1, 3)
    b = mux.endpoint("job", "b", "lane-b", 1, 8)
    source.frames = [
        Frame(v=2, seq=1, job="job", type="event", body={"kind": "ready"}, task="b", lane="lane-b",
              attempt=1, sender="shim:lane-b", epoch=8),
        Frame(v=2, seq=1, job="job", type="event", body={"kind": "ready"}, task="a", lane="lane-a",
              attempt=1, sender="shim:lane-a", epoch=3),
    ]

    assert [f.task for f in a.read(0)] == ["a"]
    assert [f.task for f in b.read(0)] == ["b"]


def test_api_call_raises_unavailable_when_socket_missing(tmp_path):
    api = HerdrApi(str(tmp_path / "no-such.sock"))
    with pytest.raises(HerdrUnavailable):
        api.call("pane.list", {})


def test_api_connect_is_injectable():
    calls = []

    class FakeSock:
        def sendall(self, data):
            calls.append(data)

        def recv(self, n):
            return (json.dumps({"id": "x", "result": {"pong": True}}) + "\n").encode()

        def close(self):
            calls.append("closed")

    api = HerdrApi("/unused", connect=lambda: FakeSock())
    result = api.call("ping", {})
    assert result == {"pong": True}
    assert calls[-1] == "closed"


def test_api_connect_wraps_os_error_as_unavailable():
    def bad_connect():
        raise OSError("boom")

    api = HerdrApi("/unused", connect=bad_connect)
    with pytest.raises(HerdrUnavailable):
        api.call("pane.list", {})


def test_api_send_uses_pane_send_input_with_enter_key():
    server = FakeHerdrServer(reply_with({"type": "ok"}))
    try:
        api = HerdrApi(server.path)
        frame = Frame(v=1, seq=1, job="jobA", type="brief", body={"text": "hi"})
        api.send("w1:p2", frame)
        req = [r for r in server.requests if r["method"] == "pane.send_input"][0]  # after the shell-prompt probe
        assert req["method"] == "pane.send_input"
        assert req["params"] == {"pane_id": "w1:p2", "text": encode(frame), "keys": ["enter"]}
    finally:
        server.stop()


# ---------------------------------------------------------------- Mailbox


def test_mailbox_reads_frame_from_pipe():
    read_fd, write_fd = os.pipe()
    try:
        frame = Frame(v=1, seq=1, job="jobA", type="brief", body={"text": "hi"})
        os.write(write_fd, (encode(frame) + "\n").encode())
        mailbox = Mailbox(fd=read_fd)
        frames = mailbox.read(timeout=1.0)
        assert frames == [frame]
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_mailbox_read_times_out_with_no_data():
    read_fd, write_fd = os.pipe()
    try:
        mailbox = Mailbox(fd=read_fd)
        assert mailbox.read(timeout=0.1) == []
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_mailbox_handles_partial_write_across_calls():
    read_fd, write_fd = os.pipe()
    try:
        frame = Frame(v=1, seq=7, job="jobA", type="brief", body={"text": "partial"})
        line = encode(frame) + "\n"
        mailbox = Mailbox(fd=read_fd)
        os.write(write_fd, line[:5].encode())
        assert mailbox.read(timeout=0.2) == []
        os.write(write_fd, line[5:].encode())
        assert mailbox.read(timeout=1.0) == [frame]
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_mailbox_close_is_safe_on_non_tty_fd():
    read_fd, write_fd = os.pipe()
    try:
        mailbox = Mailbox(fd=read_fd)
        mailbox.close()  # must not raise on a pipe (no termios state to restore)
    finally:
        os.close(write_fd)
        os.close(read_fd)


# ---------------------------------------------------------------- LifecycleWatcher


def test_lifecycle_watcher_subscribes_and_yields_pane_exited():
    def handler(conn, request):
        conn.sendall((json.dumps({"id": request["id"], "result": {"type": "subscription_started"}}) + "\n").encode())
        conn.sendall((json.dumps({"event": "pane_exited", "data": {"pane_id": "w1:p2", "workspace_id": "w1"}}) + "\n").encode())
        time.sleep(2)  # keep the connection open past the test

    server = FakeHerdrServer(handler)
    try:
        api = HerdrApi(server.path)
        watcher = LifecycleWatcher(api, panes=["w1:p2"])
        events = watcher.poll(timeout=1.0)
        assert len(events) == 1
        assert events[0].kind == "pane_exited"
        assert events[0].pane_id == "w1:p2"
        watcher.close()
    finally:
        server.stop()


def test_lifecycle_watcher_reconnects_on_eof_and_yields_reconnected():
    state = {"n": 0}

    def handler(conn, request):
        state["n"] += 1
        conn.sendall((json.dumps({"id": request["id"], "result": {"type": "subscription_started"}}) + "\n").encode())
        if state["n"] == 1:
            return  # first connection: close immediately (simulated session restart -> EOF)
        conn.sendall((json.dumps({"event": "pane_exited", "data": {"pane_id": "w1:p1"}}) + "\n").encode())
        time.sleep(2)

    server = FakeHerdrServer(handler)
    try:
        api = HerdrApi(server.path)
        watcher = LifecycleWatcher(api, backoffs=(0.05,))
        events = watcher.poll(timeout=2.0)
        assert any(e.kind == "reconnected" for e in events)
        watcher.close()
    finally:
        server.stop()


# ---------------------------------------------------------------- Endpoint


class FakeApi:
    def __init__(self):
        self.sent: list[tuple[str, Frame]] = []

    def send(self, pane_id: str, frame: Frame) -> dict:
        self.sent.append((pane_id, frame))
        return {"type": "ok"}


def test_endpoint_send_prints_and_tracks_outbox():
    printed = []
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        mailbox = Mailbox(fd=read_fd)
        endpoint = Endpoint(api, mailbox, "w1:p2", "jobA", print_fn=printed.append)
        frame = endpoint.send("brief", {"text": "go"}, now=0.0)
        assert api.sent == [("w1:p2", frame)]
        assert any("-> brief seq=1" in line for line in printed)
        # unacked and past the default timeout: due() would resend it
        assert endpoint.outbox.due(now=10.0) == [frame]
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_endpoint_receive_autoacks_and_delivers():
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        mailbox = Mailbox(fd=read_fd)
        endpoint = Endpoint(api, mailbox, "w1:p2", "jobA")
        incoming = Frame(v=1, seq=9, job="jobA", type="brief", body={"text": "hi"})
        os.write(write_fd, (encode(incoming) + "\n").encode())
        delivered = endpoint.receive(timeout=1.0)
        assert delivered == [incoming]
        assert len(api.sent) == 1
        ack_pane, ack_frame = api.sent[0]
        assert ack_pane == "w1:p2"
        assert ack_frame.type == "ack"
        assert ack_frame.body == {"acked_seq": 9}
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_endpoint_receive_clears_outbox_on_incoming_ack():
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        mailbox = Mailbox(fd=read_fd)
        endpoint = Endpoint(api, mailbox, "w1:p2", "jobA", timeout_s=1.0, max_retries=3)
        endpoint.send("brief", {"text": "go"}, now=0.0)
        assert endpoint.tick(now=5.0) == 1  # unacked -> retransmitted once

        ack = Frame(v=1, seq=99, job="jobA", type="ack", body={"acked_seq": 1})
        os.write(write_fd, (encode(ack) + "\n").encode())
        delivered = endpoint.receive(timeout=1.0)
        assert delivered == []  # acks are never delivered as data

        assert endpoint.tick(now=100.0) == 0  # cleared: nothing left to resend
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_endpoint_tick_retransmits_due_frames():
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        mailbox = Mailbox(fd=read_fd)
        endpoint = Endpoint(api, mailbox, "w1:p2", "jobA", timeout_s=1.0, max_retries=3)
        frame = endpoint.send("brief", {"text": "go"}, now=0.0)
        assert len(api.sent) == 1
        resent = endpoint.tick(now=5.0)
        assert resent == 1
        assert api.sent[-1] == ("w1:p2", frame)
    finally:
        os.close(write_fd)
        os.close(read_fd)


def _pty():
    import pty

    master, slave = pty.openpty()
    return master, slave


def test_mailbox_keeps_input_queued_before_it_went_raw(monkeypatch):
    """Job f257aa7b: `tty.setraw` defaulted to TCSAFLUSH, so a cmd frame relayed while the control pane was being
    re-exec'd was discarded by the new process's Mailbox and its sender timed out."""
    import termios

    monkeypatch.delenv("STEWARD_MAILBOX_TTY", raising=False)
    master, slave = _pty()
    try:
        first = Mailbox(fd=slave)
        frame = Frame(v=1, seq=1, job="r1", type="cmd", body={"op": "ping", "request_id": "r1"})
        first.handoff_for_exec()  # what the control pane does right before os.execv
        os.write(master, (encode(frame) + "\r").encode())  # arrives "during the exec"
        assert not termios.tcgetattr(slave)[3] & termios.ICANON  # the tty stayed raw: no cooked window
        second = Mailbox(fd=slave)  # the re-exec'd process
        assert second.read(timeout=1.0) == [frame]
    finally:
        os.close(master)
        os.close(slave)


def test_mailbox_handoff_restores_the_original_tty_mode_on_final_close(monkeypatch):
    import termios

    monkeypatch.delenv("STEWARD_MAILBOX_TTY", raising=False)
    master, slave = _pty()
    try:
        cooked = termios.tcgetattr(slave)
        first = Mailbox(fd=slave)
        first.handoff_for_exec()
        assert "STEWARD_MAILBOX_TTY" in os.environ
        second = Mailbox(fd=slave)
        assert "STEWARD_MAILBOX_TTY" not in os.environ  # consumed: children never inherit it
        second.close()
        assert termios.tcgetattr(slave) == cooked
    finally:
        os.close(master)
        os.close(slave)


# ------------------------------------------------- never type a frame into a bare shell (2026-10-07)


def _process_info(*names):
    return {"process_info": {"foreground_processes": [{"name": n, "argv": [n]} for n in names]}}


def _routing_server(process_info):
    def handler(conn, request):
        result = process_info if request.get("method") == "pane.process_info" else {"type": "ok"}
        conn.sendall((json.dumps({"id": request.get("id", ""), "result": result}) + "\n").encode())
    return FakeHerdrServer(handler)


def test_send_refuses_a_pane_at_a_shell_prompt():
    server = _routing_server(_process_info("bash"))
    try:
        api = HerdrApi(server.path)
        out = api.send("w1:p2", Frame(v=1, seq=1, job="jobA", type="brief", body={"text": "$(touch /tmp/pwn)"}))
        assert out.get("refused")
        assert [r["method"] for r in server.requests] == ["pane.process_info"]  # nothing was typed
    finally:
        server.stop()


def test_send_delivers_when_a_program_runs_in_the_pane():
    server = _routing_server(_process_info("python3"))
    try:
        api = HerdrApi(server.path)
        frame = Frame(v=1, seq=1, job="jobA", type="brief", body={"text": "hi"})
        api.send("w1:p2", frame)
        sent = [r for r in server.requests if r["method"] == "pane.send_input"]
        assert len(sent) == 1 and sent[0]["params"]["text"] == encode(frame)
    finally:
        server.stop()


def test_send_still_delivers_when_herdr_cannot_report_processes():
    server = _routing_server({})  # older Herdr / no process list: unknown never blocks delivery
    try:
        api = HerdrApi(server.path)
        api.send("w1:p2", Frame(v=1, seq=1, job="jobA", type="brief", body={"text": "hi"}))
        assert any(r["method"] == "pane.send_input" for r in server.requests)
    finally:
        server.stop()


def test_login_shell_names_count_as_shells():
    from steward.herdr_transport import _is_shell_process

    assert _is_shell_process({"name": "-bash"}) and _is_shell_process({"argv": ["/usr/bin/fish"]})
    assert not _is_shell_process({"name": "python3"}) and not _is_shell_process({"name": "claude"})
