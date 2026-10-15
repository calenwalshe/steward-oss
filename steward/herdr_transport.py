"""Herdr transport: the mailbox-pane model (FAB-03, SP-31, 09-SPIKE §Q1-Q3).

Sending a frame to a peer endpoint is one `pane.send_input` API call into
*that peer's* pane. Receiving is reading frames off *this* endpoint's own
stdin (`Mailbox`) — never `pane.read` / `output_matched` on the data path
(09-SPIKE headline decision). The event bus (`LifecycleWatcher`) carries only
lifecycle: `pane_exited`, `pane_closed`, `pane_agent_status_changed`.

`HerdrApi` always takes an explicit socket path; nothing here ever falls back
to a default/live Herdr session (SP-1, 09-SPIKE Q1).
"""

from __future__ import annotations

import json
import os
import select
import socket
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass, field
from typing import Callable, Protocol

from steward.fabric import Frame, FrameReader, Outbox, encode

_RECV_CHUNK = 65536
_SHELL_CHECK_TTL_S = 1.0
_SHELLS = frozenset({"bash", "sh", "zsh", "fish", "dash", "ksh", "mksh", "tcsh", "csh", "nu", "xonsh", "elvish"})


def _is_shell_process(proc) -> bool:
    if not isinstance(proc, dict):
        return False
    argv = proc.get("argv") or []
    name = str(proc.get("name") or (argv[0] if argv else "")).rsplit("/", 1)[-1].lstrip("-")
    return name in _SHELLS


class HerdrUnavailable(Exception):
    """The socket could not be reached at all (no server, wrong path, EOF)."""


class HerdrApiError(Exception):
    """The Herdr API answered with a typed `{"error": {"code", "message"}}`."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class _Socket(Protocol):
    def sendall(self, data: bytes) -> None: ...

    def recv(self, n: int) -> bytes: ...

    def close(self) -> None: ...


class HerdrApi:
    """One request per connection, newline-delimited JSON, snake_case enums.

    `connect` is injectable: tests supply a fake that returns a socket-like
    object (real AF_UNIX socket, socketpair half, or anything with
    sendall/recv/close) instead of dialing a real Herdr server.
    """

    def __init__(self, socket_path: str, connect: Callable[[], _Socket] | None = None):
        self.socket_path = socket_path
        self._connect_fn = connect or self._dial
        self._next_id = 0
        self._shell_cache: dict[str, tuple[float, bool]] = {}

    def _dial(self) -> _Socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self.socket_path)
        except OSError as exc:
            raise HerdrUnavailable(f"cannot reach herdr at {self.socket_path}: {exc}") from exc
        return sock

    def connect(self) -> _Socket:
        try:
            return self._connect_fn()
        except HerdrUnavailable:
            raise
        except OSError as exc:
            raise HerdrUnavailable(str(exc)) from exc

    def call(self, method: str, params: dict) -> dict:
        self._next_id += 1
        request = {"id": f"fabric-{self._next_id}", "method": method, "params": params}
        sock = self.connect()
        try:
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(_RECV_CHUNK)
                if not chunk:
                    break
                buf += chunk
        finally:
            sock.close()
        if not buf.strip():
            raise HerdrUnavailable(f"no response from herdr api for {method}")
        response = json.loads(buf.split(b"\n", 1)[0])
        if "error" in response:
            err = response["error"]
            raise HerdrApiError(err["code"], err["message"])
        return response.get("result", {})

    def send(self, pane_id: str, frame: Frame) -> dict:
        """Deliver `frame` into `pane_id`'s pane: one `pane.send_input` call. Never into a bare shell prompt: when the
        peer process has exited (a runner killed by a locked database, 2026-10-07) its pane is an interactive shell,
        and a typed frame line would be EXECUTED (`⟦steward⟧: command not found`, with `$(...)` in a JSON string
        expanded). Such a frame is not sent; `{"refused": ...}` comes back, the outbox keeps any unacked frame and
        retries it once the peer is listening again."""
        if self._at_shell_prompt(pane_id):
            return {"refused": "peer pane is at a shell prompt", "pane_id": pane_id}
        return self.call("pane.send_input", {"pane_id": pane_id, "text": encode(frame), "keys": ["enter"]})

    def _at_shell_prompt(self, pane_id: str) -> bool:
        """True only when Herdr positively reports the pane's foreground is nothing but a shell. Unknown (older Herdr,
        API error, no process list) is False: this guard never blocks delivery it cannot prove unsafe."""
        now = time.monotonic()
        hit = self._shell_cache.get(pane_id)
        if hit is not None and now - hit[0] < _SHELL_CHECK_TTL_S:
            return hit[1]
        try:
            info = self.call("pane.process_info", {"pane_id": pane_id}).get("process_info") or {}
        except (HerdrApiError, HerdrUnavailable, ValueError):
            return False
        procs = info.get("foreground_processes") if isinstance(info, dict) else None
        at_shell = bool(procs) and all(_is_shell_process(p) for p in procs)
        self._shell_cache[pane_id] = (now, at_shell)
        return at_shell


# The tty mode a re-exec'd process must restore on exit (`Mailbox.handoff_for_exec`): the pre-exec process keeps the
# tty raw across the execv instead of restoring it, so it hands the ORIGINAL mode on through the environment.
TTY_HANDOFF_ENV = "STEWARD_MAILBOX_TTY"


def _encode_termios(attrs: list) -> str:
    *flags, cc = attrs
    return json.dumps([*flags, [c if isinstance(c, int) else {"b": c[0] if c else 0} for c in cc]])


def _decode_termios(text: str) -> list | None:
    try:
        *flags, cc = json.loads(text)
        return [*(int(f) for f in flags), [c if isinstance(c, int) else bytes([int(c["b"])]) for c in cc]]
    except (ValueError, TypeError, KeyError):
        return None


class Mailbox:
    """Raw, echo-off stdin frame reader for the local endpoint.

    Puts `fd` (default the process's own stdin) into raw/no-echo tty mode so
    frames longer than the canonical line cap can be read and nothing is
    echoed back into the pane; restores the prior mode on `close()`. Works on
    a non-tty fd too (tests use a pipe) — `termios` failures there are
    swallowed and reads happen as-is.

    Raw mode is entered with TCSANOW, never TCSAFLUSH: input already queued on the tty (a cmd frame relayed while this
    process was being exec'd) is read, not discarded (job f257aa7b: every frame sent during a control re-exec was
    silently lost and its sender timed out).
    """

    def __init__(self, fd: int | None = None):
        self.fd = fd if fd is not None else sys.stdin.fileno()
        self.reader = FrameReader()
        self._old_termios = None
        handed = os.environ.pop(TTY_HANDOFF_ENV, None)
        try:
            current = termios.tcgetattr(self.fd)
            self._old_termios = (_decode_termios(handed) if handed else None) or current
            tty.setraw(self.fd, termios.TCSANOW)
        except (termios.error, OSError):
            self._old_termios = None

    def read(self, timeout: float) -> list[Frame]:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return []
        try:
            data = os.read(self.fd, 1 << 20)
        except OSError:
            return []
        if not data:
            return []
        return self.reader.feed(data.decode("utf-8", "replace"))

    def handoff_for_exec(self) -> None:
        """Before an execv of this same program: leave the tty raw (a cooked window would echo, line-mangle and then
        flush frames arriving mid-exec) and hand the mode to restore on exit to the next process's `Mailbox`."""
        if self._old_termios is not None:
            os.environ[TTY_HANDOFF_ENV] = _encode_termios(self._old_termios)
            self._old_termios = None

    def close(self) -> None:
        if self._old_termios is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self._old_termios)
            except (termios.error, OSError):
                pass
            self._old_termios = None


class FleetMailbox:
    """One terminal reader which demultiplexes protocol-v2 frames to lane endpoints.

    Every lane adapter runs in the same Steward process and therefore cannot own a
    separate reader for stdin. Endpoints share this router; a read may collect
    frames for several lanes, but each frame is queued by its complete lane
    identity and can only be returned to the matching adapter.
    """

    def __init__(self, source=None):
        self.source = source if source is not None else Mailbox()
        self._pending: dict[tuple, list[Frame]] = {}
        self._known: set[tuple] = set()
        self._lock = threading.Lock()

    def endpoint(self, job: str, task: str, lane: str, attempt: int, epoch: int):
        identity = (job, task, lane, attempt, epoch)
        with self._lock:
            self._known.add(identity)
            self._pending.setdefault(identity, [])
        return _FleetMailboxEndpoint(self, identity)

    def _read(self, identity: tuple, timeout: float) -> list[Frame]:
        key = self._key(identity)
        with self._lock:
            waiting = self._pending.get(key, [])
            if waiting:
                self._pending[key] = []
                return waiting
            matching = []
            for frame in self.source.read(timeout):
                target = self._frame_key(frame)
                if target == key:
                    matching.append(frame)
                elif target in self._known:
                    self._pending[target].append(frame)
            return matching

    def close(self) -> None:
        close = getattr(self.source, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _key(identity: tuple) -> tuple:
        return tuple(identity)

    @staticmethod
    def _frame_key(frame: Frame) -> tuple | None:
        if frame.v < 2 or any(x is None for x in (frame.task, frame.lane, frame.attempt, frame.epoch)):
            return None
        return (frame.job, frame.task, frame.lane, frame.attempt, frame.epoch)


class _FleetMailboxEndpoint:
    def __init__(self, router: FleetMailbox, identity: tuple):
        self.router, self.identity = router, identity

    def read(self, timeout: float) -> list[Frame]:
        return self.router._read(self.identity, timeout)


@dataclass(frozen=True)
class LifecycleEvent:
    kind: str  # "pane_exited" | "pane_closed" | "pane_agent_status_changed" | "reconnected"
    pane_id: str | None
    workspace_id: str | None
    data: dict


class LifecycleWatcher:
    """Subscribes to lifecycle events over a persistent connection.

    On EOF (e.g. a test-session `server stop`), reconnects with backoff,
    re-subscribes, and yields a `reconnected` event so the owner knows to
    re-verify/relaunch its endpoints (09-SPIKE Q2).
    """

    def __init__(
        self,
        api: HerdrApi,
        panes: list[str] | None = None,
        *,
        backoffs: tuple[float, ...] = (0.05, 0.2, 0.5, 1.0),
    ):
        self._api = api
        self._panes = list(panes) if panes else []
        self._backoffs = backoffs
        self._sock: _Socket | None = None
        self._buf = b""
        self._connect_and_subscribe()

    def _subscriptions(self) -> list[dict]:
        # `pane.exited`/`pane.closed` cover every pane when `pane_id` is omitted, but the
        # live API rejects `pane.agent_status_changed` without one (measured, task 09-02.3)
        # — so that subscription is only added per explicitly-watched pane. Subscription
        # `type` values here are dotted; the events actually emitted on the wire spell
        # their `event` field snake_case (`pane_exited`, not `pane.exited` — also measured).
        subs = [{"type": "pane.exited"}, {"type": "pane.closed"}]
        subs.extend({"type": "pane.agent_status_changed", "pane_id": p} for p in self._panes)
        return subs

    def _connect_and_subscribe(self) -> None:
        sock = self._api.connect()
        req = {"id": "lifecycle-sub", "method": "events.subscribe", "params": {"subscriptions": self._subscriptions()}}
        sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
        self._sock = sock
        self._buf = b""
        self._read_line(1.0)  # subscription_started ack (or EOF, surfaced on the next poll)

    def _read_line(self, timeout: float) -> bytes | None:
        """One buffered line, or `b""` on EOF, or `None` if nothing arrived."""
        while b"\n" not in self._buf:
            ready, _, _ = select.select([self._sock], [], [], timeout)
            if not ready:
                return None
            chunk = self._sock.recv(_RECV_CHUNK)
            if not chunk:
                return b""
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    def _reconnect(self) -> list[LifecycleEvent]:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        for delay in self._backoffs:
            time.sleep(delay)
            try:
                self._connect_and_subscribe()
                return [LifecycleEvent(kind="reconnected", pane_id=None, workspace_id=None, data={})]
            except HerdrUnavailable:
                continue
        raise HerdrUnavailable("lifecycle watcher could not reconnect")

    def _to_event(self, msg: dict) -> LifecycleEvent | None:
        if "event" not in msg:
            return None
        data = msg.get("data", {})
        return LifecycleEvent(
            kind=msg["event"], pane_id=data.get("pane_id"), workspace_id=data.get("workspace_id"), data=data
        )

    def poll(self, timeout: float) -> list[LifecycleEvent]:
        """Drain whatever lifecycle events are available within `timeout`."""
        events: list[LifecycleEvent] = []
        remaining = timeout
        while True:
            line = self._read_line(remaining)
            remaining = 0.0  # only the first read in a batch waits
            if line is None:
                return events
            if line == b"":
                events.extend(self._reconnect())
                return events
            if not line.strip():
                continue
            event = self._to_event(json.loads(line))
            if event is not None:
                events.append(event)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None


class Endpoint:
    """One side of a job's mailbox conversation with a peer pane.

    `send()` transmits and (for non-ack types) tracks the frame in an
    `Outbox` for retransmit; `receive()` reads the mailbox, auto-acks
    non-ack frames, applies incoming acks to the outbox, and returns the
    delivered (non-ack) frames; `tick()` resends anything past its timeout.
    One readable line is printed per frame sent or received (SP-31).
    """

    def __init__(
        self,
        api: HerdrApi,
        mailbox: Mailbox,
        peer_pane: str,
        job: str,
        *,
        print_fn: Callable[[str], None] = print,
        timeout_s: float = 2.0,
        max_retries: int = 5,
    ):
        self.api = api
        self.mailbox = mailbox
        self.peer_pane = peer_pane
        self.job = job
        self._print = print_fn
        self.outbox = Outbox(timeout_s=timeout_s, max_retries=max_retries)
        self._seq = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def send(self, type_: str, body: dict, *, now: float | None = None) -> Frame:
        frame = Frame(v=1, seq=self._next_seq(), job=self.job, type=type_, body=body)
        self.api.send(self.peer_pane, frame)
        self._print(f"-> {frame.type} seq={frame.seq}")
        if frame.type != "ack":
            self.outbox.add(frame, now if now is not None else time.time())
        return frame

    def _send_ack(self, acked_seq: int) -> None:
        frame = Frame(v=1, seq=self._next_seq(), job=self.job, type="ack", body={"acked_seq": acked_seq})
        self.api.send(self.peer_pane, frame)
        self._print(f"-> ack seq={frame.seq} for={acked_seq}")

    def receive(self, timeout: float) -> list[Frame]:
        delivered: list[Frame] = []
        for frame in self.mailbox.read(timeout):
            self._print(f"<- {frame.type} seq={frame.seq}")
            if frame.type == "ack":
                acked_seq = frame.body.get("acked_seq")
                if acked_seq is not None:
                    self.outbox.ack(job=frame.job, seq=acked_seq)
                continue
            self._send_ack(frame.seq)
            delivered.append(frame)
        return delivered

    def tick(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        due = self.outbox.due(now)
        for frame in due:
            self.api.send(self.peer_pane, frame)
            self._print(f"-> (retry) {frame.type} seq={frame.seq}")
        return len(due)
