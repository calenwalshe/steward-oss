"""`steward shim` — the worker-pane fabric endpoint (WRK-01, WRK-02, SP-35).

The shim never reads the screen. Its input is frames on its own stdin
(`Mailbox`, injected); its output is frames sent into the runner pane via
`HerdrApi.send` (injected `api`), plus one readable line per frame (SP-31).
Between the two fabric endpoints sits the agent subprocess (`spawn`,
injected — `claude -p` or `steward.fake_agent`), speaking the stream-json
protocol measured in 09-SPIKE Q5.

Design note (deviation from a literal reading of the plan): `Endpoint` in
`steward/herdr_transport.py` is not reused here for outbound sends, because
its `receive()` treats *every* frame of type `ack` as a low-level transport
ack (`body.acked_seq`) and never delivers it — but this shim also needs to
*send* business-level `ack` frames (`{rev, stage}`) for steer delivery/
acknowledgement, which are a different, coexisting use of the same frame
type in the other direction. The shim implements its own thin send/receive
directly on `Frame`/`Outbox`/`HerdrApi`/`Mailbox` (all still Phase 9,
unmodified) so both meanings of `ack` coexist without colliding: inbound
`ack` frames (from the runner) are always the low-level transport kind here,
since the runner never receives a business ack back from itself; outbound
`ack` frames (to the runner) are always the business kind. The peer
(`FabricWorker`, phase 10-02) disambiguates the two by shape
(`"acked_seq" in body` vs `"rev" in body`).
"""

from __future__ import annotations

import json
import os
import re
import select
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from steward.fabric import Frame, Outbox
from steward.herdr_transport import HerdrApiError, HerdrUnavailable

_REV_PREFIX = re.compile(r"^\[rev=(\d+)\]")
# Steers queued while the model is busy (e.g. focus + authority at launch) are replayed as ONE user message, joined by
# newlines: "[rev=1] ...\n[rev=2] ..." (2026-10-13 lost ack). Each line-leading marker is its own consumed steer.
_REV_LINE = re.compile(r"^\[rev=(\d+)\]", re.MULTILINE)
_STDERR_TAIL_MAX = 4000

# TRJ-04: typed evidence for the trajectory ledger, extracted from the same
# stream-json events the shim already reads. Numbers only, never the pane
# text itself (12-CONTEXT D4).
_PLAN_MAX_ITEMS = 20
_PLAN_MAX_CHARS = 80

_PYTEST_PASSED_RE = re.compile(r"(\d+) passed")
_PYTEST_FAILED_RE = re.compile(r"(\d+) failed")
_PYTEST_ERROR_RE = re.compile(r"(\d+) error")
_JEST_PASSED_RE = re.compile(r"Tests:.*?(\d+) passed")
_GO_RESULT_RE = re.compile(r"^(ok|FAIL)\s", re.MULTILINE)

# 09-SPIKE Q3 (`spikes/frame_echo.py`): the measured p99 round trip for a
# frame + its ack over the mailbox-pane transport (local sockets, 200 frames
# incl. 8 KiB) was 5.5 ms. This default is set well above that — high enough
# that a healthy ack is always drained before its frame's retransmit timeout
# elapses, so a healthy run's logs never show a retry (10-03 finding 5) —
# while still being short enough to recover a genuinely lost frame quickly.
_DEFAULT_ACK_TIMEOUT_S = 2.0


class AgentProcess:
    """The subset of `subprocess.Popen` the shim needs (SP-1: injectable)."""

    stdin: Any
    stdout: Any
    stderr: Any

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


@dataclass
class Shim:
    job: str
    peer_pane: str
    api: Any
    mailbox: Any
    spawn: Callable[[list[str]], AgentProcess]
    agent_argv: list[str]
    git: Callable[[], str | None] | None = None
    clock: Callable[[], float] | None = None
    print_fn: Callable[[str], None] = print
    timeout_s: float = _DEFAULT_ACK_TIMEOUT_S
    max_retries: int = 5
    activity_throttle_s: float = 5.0
    stop_grace_s: float = 5.0
    own_pane: str | None = None
    # "the worker's own model API calls" (OPX-04, SP-34): checked once, in
    # `start()`, right before spawning the real agent subprocess. `None`
    # (the default) skips the check — tests that construct a `Shim` with no
    # exceptions registry in play are unaffected.
    exceptions_use: Callable[[], None] | None = None
    # Called once with the agent's session id from its stream-json `init`
    # message, so `steward chat <job> worker` can fork the live session.
    on_session: Callable[[str], None] | None = None
    # Fleet lane identity (protocol v2, FLEET-09). All unset = the legacy v1 single-worker shim. Set: every frame
    # carries (task, lane, attempt, sender, epoch), inbound frames for any other identity are ignored, and `done`
    # names the lane's committed candidate (`candidate_sha`, HEAD when it moved since start) plus an evidence ref.
    task: str | None = None
    lane: str | None = None
    attempt: int | None = None
    epoch: int | None = None

    def __post_init__(self) -> None:
        self._now = self.clock or time.time
        self.outbox = Outbox(timeout_s=self.timeout_s, max_retries=self.max_retries)
        self._seq = 0
        self._agent: AgentProcess | None = None
        self._exited = False
        self.exit_code: int | None = None
        self._stopping = False
        self._terminated = False
        self._stop_deadline = 0.0
        self._kill_deadline = 0.0
        self._pending_permissions: dict[str, dict] = {}
        # request_id -> allow, for every answer already written to the agent: a runner that restarted re-sends
        # answers it never saw acked, and must hear "already delivered", not "unknown" (lost-approval hang).
        self._answered: dict[str, bool] = {}
        self._pending_ack_revs: set[int] = set()
        # The in-flight tool batch (latency #6/#7): every tool_use the model has issued whose result has not come
        # back yet. `_batch_gen` bumps when a new batch opens; the runner names it when it asks to interrupt, and
        # the shim re-checks it right before acting.
        self._inflight: dict[str, dict] = {}
        self._batch_gen = 0
        self._batch_since = 0.0
        self._max_rev = 0  # highest steer rev received: a done names it so the runner knows what the shim had seen
        self._last_activity_sent = float("-inf")
        self._stderr_tail = ""
        self._stdout_buf = ""
        # Urgent steer (interrupt): `_turn_active` is True from the moment a
        # user message is written until its `result`; `_interrupting` is set
        # while an interrupt we sent is still expected to produce a `result`
        # for the cancelled turn, which must never be reported as `done`.
        self._turn_active = False
        self._interrupting = False
        self._interrupt_ids: set[str] = set()
        self._start_head: str | None = None
        self._session_id: str | None = None
        self._turns = 0

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Spawn the agent process. Call once before `step()`/`run()`.

        Sends `event {kind: ready, pid}` first, before spawning the agent or
        reading anything from the mailbox (10-03 finding 1): the mailbox is
        already in raw mode by the time `Shim` exists (`Mailbox.__init__`
        sets it), so this is the earliest point at which a frame written
        into this pane is actually read as a frame rather than typed into
        the bare shell that ran `steward shim` a moment earlier.
        `FabricWorker.start()` withholds the brief until it observes this.

        `exceptions_use()` (OPX-04) runs right before the real spawn: an
        unregistered `model-api` path raises here and the agent is never
        spawned — the ready event has already gone out, but no model
        process ever starts."""
        self._send("event", {"kind": "ready", "pid": os.getpid()})
        if self.lane is not None and self.git is not None:
            self._start_head = self.git()  # the lane's pinned base: a candidate is only a commit made after it
        if self.exceptions_use is not None:
            self.exceptions_use()
        self._agent = self.spawn(self.agent_argv)
        self._report_agent("working")

    def step(self, timeout: float) -> None:
        """One event-loop tick: drain agent output, drain inbound frames,
        retransmit due outbox frames, and progress any pending stop."""
        if self._exited:
            return
        half = max(timeout / 2.0, 0.0)
        self._guard("drain_agent_stdout", self._drain_agent_stdout, half)
        self._guard("drain_agent_stderr", self._drain_agent_stderr)
        self._guard("check_agent_exit", self._check_agent_exit)
        if self._exited:
            return
        remaining = max(timeout - half, 0.0)
        frames = self._guard("mailbox_read", self.mailbox.read, timeout=remaining) or []
        for frame in frames:
            self._guard("inbound frame", self._handle_inbound, frame)
        self._guard("tick_outbox", self._tick_outbox)
        self._guard("check_stop_grace", self._check_stop_grace)
        self._guard("check_agent_exit", self._check_agent_exit)

    def _guard(self, what: str, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """Top-level guard: one bad event/frame is logged and dropped, never raised out of the shim loop (a dead
        shim kills the worker's whole job)."""
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            try:
                self._emit(f"shim error in {what}: {type(exc).__name__}: {str(exc)[:200]}")
            except Exception:
                pass
            return None

    def run(self, poll_s: float = 0.5) -> int:
        """Spawn (if needed) and loop `step()` until the agent exits."""
        if self._agent is None:
            self.start()
        while not self._exited:
            self.step(poll_s)
        return self.exit_code if self.exit_code is not None else 0

    # ------------------------------------------------------------ outbound

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit(self, line: str) -> None:
        """One human-readable log line, written with `\\r\\n` (10-03 finding
        2): the mailbox has this process's tty in raw mode (OPOST off), so a
        bare `\\n` from `print_fn`'s own trailing newline doesn't return the
        carriage — every line after the first drifts one column right,
        producing diagonal "staircase" output in the pane."""
        self.print_fn(line + "\r")

    def _send(self, type_: str, body: dict, *, track: bool = True) -> Frame:
        frame = self._frame(type_, body)
        self.api.send(self.peer_pane, frame)
        self._emit(f"-> {frame.type} seq={frame.seq}")
        if track:
            self.outbox.add(frame, self._now())
        return frame

    def _frame(self, type_: str, body: dict) -> Frame:
        identity = (self.task, self.lane, self.attempt, self.epoch)
        if all(value is None for value in identity):
            return Frame(v=1, seq=self._next_seq(), job=self.job, type=type_, body=body)
        if any(value is None for value in identity):
            raise ValueError("fleet shim requires task, lane, attempt and epoch")
        return Frame(v=2, seq=self._next_seq(), job=self.job, type=type_, body=body, task=self.task,
                     lane=self.lane, attempt=self.attempt, sender=f"shim:{self.lane}", epoch=self.epoch)

    def _inbound_identity_ok(self, frame: Frame) -> bool:
        if self.task is None:
            return frame.v < 2 and frame.job == self.job
        return (frame.v >= 2 and frame.job == self.job and frame.task == self.task and frame.lane == self.lane
                and frame.attempt == self.attempt and frame.epoch == self.epoch
                and frame.sender == f"controller:{self.lane}")

    def _own_ident(self) -> tuple:
        if self.task is None:
            return ()
        return (self.task, self.lane, self.attempt, f"shim:{self.lane}", self.epoch)

    _ident = _own_ident  # the name the fleet tests and FabricWorker use for the same outbox identity

    def _tick_outbox(self) -> None:
        for frame in self.outbox.due(self._now()):
            self.api.send(self.peer_pane, frame)
            self._emit(f"-> (retry) {frame.type} seq={frame.seq}")

    def _report_agent(self, state: str) -> None:
        """Best-effort: mirrors worker state to Herdr (sidebar/Moshi), never
        a data channel and never a hard dependency (09-SPIKE Q4)."""
        if not self.own_pane:
            return
        try:
            self.api.call(
                "pane.report_agent",
                {"pane_id": self.own_pane, "source": "steward", "agent": "steward-shim", "state": state},
            )
        except (HerdrApiError, HerdrUnavailable):
            pass

    # ------------------------------------------------------------- inbound

    def _handle_inbound(self, frame: Frame) -> None:
        if not self._inbound_identity_ok(frame):
            self._emit(f"dropping frame with foreign identity seq={frame.seq}")
            return
        if not isinstance(frame.body, dict):
            self._emit(f"ignoring frame type={frame.type} seq={frame.seq} with non-object body")
            return
        if frame.type == "ack":
            acked_seq = frame.body.get("acked_seq")
            if isinstance(acked_seq, int) and not isinstance(acked_seq, bool):
                self.outbox.ack(job=frame.job, seq=acked_seq, ident=self._own_ident())
            return
        self._emit(f"<- {frame.type} seq={frame.seq}")
        if frame.type == "cmd":
            # Handled before the transport ack below: a `rebind` must switch
            # `self.peer_pane` first, or the ack for this very frame would
            # still go to the stale (possibly gone) runner pane (10-04
            # fabric resume after controller restart).
            self._handle_cmd(frame.body)
        self._send("ack", {"acked_seq": frame.seq}, track=False)
        if frame.type == "brief":
            self._write_user_message(_as_text(frame.body.get("text")))
            self._report_agent("working")
        elif frame.type == "steer":
            rev = frame.body.get("rev")
            if not isinstance(rev, int) or isinstance(rev, bool):
                self._emit(f"ignoring steer seq={frame.seq} with bad rev={rev!r}")
                return
            self._pending_ack_revs.add(rev)
            self._max_rev = max(self._max_rev, rev)
            if frame.body.get("urgent") and self._turn_active:
                self._send_interrupt()
                self._send("event", {"kind": "interrupted", "rev": rev})
            self._write_user_message(f"[rev={rev}] {_as_text(frame.body.get('text'))}")
            self._send("ack", {"rev": rev, "stage": "delivered"})
        elif frame.type in ("approve", "deny"):
            request_id = frame.body.get("request_id")
            if not isinstance(request_id, str):
                self._emit(f"ignoring {frame.type} seq={frame.seq} with bad request_id={request_id!r}")
                return
            message = frame.body.get("message")
            self._answer_permission(request_id, allow=(frame.type == "approve"),
                                    message=message if isinstance(message, str) else None)
        elif frame.type == "stop":
            self._begin_stop()
        elif frame.type != "cmd":
            self._emit(f"ignoring unexpected inbound frame type={frame.type} seq={frame.seq}")

    def _handle_cmd(self, body: dict) -> None:
        op = body.get("op")
        if op == "list_permissions":
            self._send_permission_snapshot()
            return
        if op == "interrupt_waits":
            self._interrupt_waits(body)
            return
        if op != "rebind":
            self._emit(f"ignoring cmd op={op!r}")
            return
        new_peer = body.get("peer")
        if not new_peer or not isinstance(new_peer, str):
            self._emit("ignoring cmd op='rebind' with no peer")
            return
        self._rebind(new_peer)
        # A new runner has no memory of what the worker is waiting on: tell it (the shim is the authority).
        self._send_permission_snapshot()

    def _interrupt_waits(self, body: dict) -> None:
        """Latency #7: the runner decided (and persisted) that every in-flight call is a pure wait. Re-check right
        before acting: the same batch generation, exactly the same calls, each still a pure wait by the strict
        rule, a turn still running. Then cancel the turn and tell the worker its observer was cancelled and the
        task is still running. A refusal is reported; nothing is cancelled."""
        from steward import auto_interrupt, wait_policy

        gen = body.get("gen")
        raw_ids = body.get("call_ids")
        ids = {i for i in raw_ids if isinstance(i, str)} if isinstance(raw_ids, list) else set()
        reason = None
        if not self._turn_active or self._interrupting:
            reason = "no running turn"
        elif gen != self._batch_gen:
            reason = "batch generation changed"
        elif not self._inflight or set(self._inflight) != ids:
            reason = "in-flight calls changed"
        elif not all(wait_policy.is_pure_wait(c.get("name", ""), c.get("input") or {})
                     for c in self._inflight.values()):
            reason = "a call is not a pure wait"
        if reason is not None:
            self._send("event", {"kind": "interrupt_refused", "gen": gen, "rev": body.get("rev"), "reason": reason})
            return
        cancelled = sorted(self._inflight)
        self._send_interrupt()
        self._inflight.clear()
        self._write_user_message(auto_interrupt.note_text(wait_policy.cap_s()))
        self._send("event", {"kind": "waits_interrupted", "gen": gen, "rev": body.get("rev"), "call_ids": cancelled,
                             "rule_version": body.get("rule_version")})

    def _send_permission_snapshot(self) -> None:
        """`event {kind: pending_permissions}`: every request the agent is still blocked on, plus the ids already
        answered. The runner marks an approval stale only on this (or `permission_unknown`), never on its own
        empty memory after a restart."""
        pending = [
            {"request_id": rid, "tool": req.get("tool_name"), "input": req.get("input") or {}}
            for rid, req in self._pending_permissions.items()
        ]
        self._send("event", {"kind": "pending_permissions", "pending": pending, "answered": sorted(self._answered)})

    def _rebind(self, new_peer: str) -> None:
        """Controller restart reattach (10-04, SP-9/SP-14): the runner's own
        pane changed (a fresh controller process). All future sends switch
        to it, and everything still unacked is resent there so nothing sent
        while the old runner pane was down is lost."""
        old_peer = self.peer_pane
        self.peer_pane = new_peer
        self._emit(f"rebind peer {old_peer} -> {new_peer}")
        for entry in list(self.outbox._pending.values()):
            frame = entry["frame"]
            self.api.send(self.peer_pane, frame)
            self._emit(f"-> (rebind resend) {frame.type} seq={frame.seq}")

    def _answer_permission(self, request_id: str, *, allow: bool, message: str | None = None) -> None:
        pending = self._pending_permissions.pop(request_id, None)
        if pending is None:
            if request_id in self._answered:
                # a restarted runner re-sent an answer we already delivered: ack it again, write nothing
                self._send("event", {"kind": "permission_answered", "request_id": request_id,
                                     "allow": self._answered[request_id], "duplicate": True})
                return
            self._emit(f"ignoring {'approve' if allow else 'deny'} for unknown request_id={request_id}")
            self._send("event", {"kind": "permission_unknown", "request_id": request_id})
            return
        response = (
            {"behavior": "allow", "updatedInput": pending.get("input") or {}}
            if allow
            else {"behavior": "deny", "message": message or "denied by steward operator"}
        )
        self._write_json_line(
            {"type": "control_response", "response": {"subtype": "success", "request_id": request_id, "response": response}}
        )
        self._answered[request_id] = allow
        # delivery ack: the runner keeps the answer persisted as in flight until it sees this
        self._send("event", {"kind": "permission_answered", "request_id": request_id, "allow": allow})
        self._report_agent("working")

    def _begin_stop(self) -> None:
        self._stopping = True
        self._stop_deadline = self._now() + self.stop_grace_s
        if self._agent is not None and self._agent.stdin is not None:
            try:
                self._agent.stdin.close()
            except (OSError, ValueError):
                pass

    # ------------------------------------------------------- agent -> shim

    def _write_json_line(self, obj: dict) -> None:
        if self._agent is None or self._agent.stdin is None:
            return
        try:
            self._agent.stdin.write(json.dumps(obj) + "\n")
            self._agent.stdin.flush()
        except (OSError, ValueError, BrokenPipeError):
            pass

    def _send_interrupt(self) -> None:
        """Claude Code's stream-json interrupt control request: cancels the
        running tool call/turn. The cancelled turn ends with its own
        `result`, swallowed in `_handle_result` via `_interrupting`."""
        request_id = str(uuid.uuid4())
        self._interrupt_ids.add(request_id)
        self._interrupting = True
        self._write_json_line(
            {"type": "control_request", "request_id": request_id, "request": {"subtype": "interrupt"}}
        )

    def _write_user_message(self, text: str) -> None:
        self._turn_active = True
        self._write_json_line({"type": "user", "message": {"role": "user", "content": text}})

    def _drain_agent_stdout(self, timeout: float) -> None:
        """Reads the agent's stdout via the raw fd (`os.read`), never the
        buffered `.readline()`: mixing `select()` on the fd with a buffered
        `TextIOWrapper` is unsafe — the wrapper can over-read from the pipe
        in one OS-level read, after which `select()` correctly reports the
        (now-empty) fd as not-ready even though already-buffered lines are
        still waiting to be consumed, so they'd never be read. `Mailbox` in
        `steward/herdr_transport.py` avoids the same trap the same way."""
        if self._agent is None or self._agent.stdout is None:
            return
        try:
            fd = self._agent.stdout.fileno()
        except (OSError, ValueError):  # closed pipe
            return
        deadline = self._now() + timeout
        while True:
            remaining = deadline - self._now()
            if remaining <= 0:
                return
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
                if not ready:
                    return
                chunk = os.read(fd, 65536)
            except (OSError, ValueError):
                return
            if not chunk:
                return  # EOF
            self._stdout_buf += chunk.decode("utf-8", "replace")
            while "\n" in self._stdout_buf:
                line, self._stdout_buf = self._stdout_buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, RecursionError):  # JSONDecodeError is a ValueError
                    self._emit(f"non-json agent stdout: {line[:120]!r}")
                    continue
                self._guard("agent event", self._handle_agent_event, event)

    def _drain_agent_stderr(self) -> None:
        if self._agent is None or self._agent.stderr is None:
            return
        try:
            fd = self._agent.stderr.fileno()
        except (OSError, ValueError):
            return
        while True:
            try:
                ready, _, _ = select.select([fd], [], [], 0)
                if not ready:
                    return
                chunk = os.read(fd, 65536)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            self._stderr_tail = (self._stderr_tail + chunk.decode("utf-8", "replace"))[-_STDERR_TAIL_MAX:]

    def _handle_agent_event(self, event: dict) -> None:
        if not isinstance(event, dict):
            self._emit(f"ignoring non-object agent event: {str(event)[:120]!r}")
            return
        etype = event.get("type")
        if etype == "user":
            content = _dict(event.get("message")).get("content")
            if isinstance(content, str):
                self._handle_replay(content)
            elif isinstance(content, list):
                self._handle_tool_result_list(content)
        elif etype == "assistant":
            self._handle_assistant(event)
        elif etype == "control_request":
            self._handle_control_request(event)
        elif etype == "control_response":
            rid = _dict(event.get("response")).get("request_id")
            if isinstance(rid, str) and rid in self._interrupt_ids:
                self._interrupt_ids.discard(rid)
            else:
                self._emit(f"ignoring control_response request_id={rid!r}")
        elif etype == "result":
            self._handle_result(event)
        elif etype == "system":
            sid = event.get("session_id")
            if event.get("subtype") == "init" and isinstance(sid, str) and sid:
                self._session_id = sid
                identity = {"kind": "identity", "session_id": sid}
                if isinstance(event.get("model"), str) and event["model"]:
                    identity["model"] = event["model"]
                # Unit callers that only observe the legacy on_session callback may
                # construct a shim without a transport; live shims always have one.
                # Only the init event carries an identity: other system events
                # (status, compaction, hooks) must not crash the shim.
                if self.api is not None:
                    self._send("event", identity)
            if event.get("subtype") == "init" and isinstance(sid, str) and sid and self.on_session is not None:
                try:
                    self.on_session(sid)
                except Exception:  # best-effort; never fatal to the worker
                    pass
        else:
            self._emit(f"ignoring agent event type={str(etype)[:60]!r}")

    def _handle_replay(self, content: str) -> None:
        match = _REV_PREFIX.match(content)
        if not match:
            return
        revs = [int(match.group(1))]
        # Later lines count only for a rev still awaited, so steer text quoting `[rev=N]` cannot ack an unseen steer.
        revs += [int(m.group(1)) for m in _REV_LINE.finditer(content, match.end())
                 if int(m.group(1)) in self._pending_ack_revs and int(m.group(1)) not in revs]
        # `at`: when the model actually consumed it (the replay), so the receipt is not stamped at drain time
        at = self._now()
        for rev in revs:
            self._pending_ack_revs.discard(rev)
            self._send("ack", {"rev": rev, "stage": "acknowledged", "at": at})

    def _handle_tool_result_list(self, content: list) -> None:
        """`user` events whose content is a list (a tool_result being fed
        back to the model) are scanned for a pytest/jest/go test summary
        (TRJ-04). Numbers only ever leave this process — never the tail
        text itself."""
        closed = False
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            if isinstance(tool_use_id, str) and self._inflight.pop(tool_use_id, None) is not None:
                closed = True
            summary = _match_test_summary(_tool_result_text(block))
            if summary is not None:
                self._send("event", {"kind": "test_result", **summary, "source": "worker_observed"})
        if closed:
            self._send_batch()

    def _handle_assistant(self, event: dict) -> None:
        opened = False
        content = _dict(event.get("message")).get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            name = name if isinstance(name, str) else ""
            tool_input = _dict(block.get("input"))
            self._note_activity(name)
            if name == "TodoWrite":
                self._send_plan_event(tool_input)
            if isinstance(block.get("id"), str) and block["id"]:
                if not self._inflight:
                    self._batch_gen += 1
                    self._batch_since = self._now()
                self._inflight[block["id"]] = {"id": block["id"], "name": name,
                                               "input": _batch_input(name, tool_input)}
                opened = True
        if opened:
            self._send_batch()

    def _send_batch(self) -> None:
        """`event {kind: batch}`: the in-flight tool calls (empty when the batch has returned)."""
        self._send("event", {"kind": "batch", "gen": self._batch_gen, "since": self._batch_since,
                             "calls": list(self._inflight.values())})

    def _send_plan_event(self, tool_input: dict) -> None:
        """Not throttled by the activity throttle (TRJ-04): a redesign is
        exactly the kind of evidence the trajectory loop must never miss."""
        self._send("event", {"kind": "plan", "items": _extract_plan_items(tool_input)})

    def _note_activity(self, tool: str) -> None:
        now = self._now()
        if now - self._last_activity_sent >= self.activity_throttle_s:
            self._send("event", {"kind": "activity", "tool": tool})
            self._last_activity_sent = now
        self._report_agent("working")

    def _handle_control_request(self, event: dict) -> None:
        request_id = event.get("request_id")
        request = _dict(event.get("request"))
        if request.get("subtype") != "can_use_tool":
            self._emit(f"ignoring control_request subtype={str(request.get('subtype'))[:60]!r}")
            return
        if not isinstance(request_id, str) or not request_id:
            self._emit(f"ignoring can_use_tool control_request with bad request_id={str(request_id)[:60]!r}")
            return
        request = {**request, "input": _dict(request.get("input"))}
        self._pending_permissions[request_id] = request
        self._send(
            "permission_request",
            {"request_id": request_id, "tool": request.get("tool_name"), "input": request["input"]},
        )
        # Not "blocked": Steward auto-approves most permissions within a second, and a blocked
        # report makes Herdr raise a needs-input toast. The runner reports blocked itself, only
        # when an approval is routed to the owner (`Runner._sync_herdr_state`).
        self._report_agent("working")

    def _handle_result(self, event: dict) -> None:
        if self._interrupting:
            # The result of the turn we just cancelled: not "done". A new
            # turn (the urgent steer's message) is already queued behind it.
            self._interrupting = False
            self._emit(f"swallowing result of interrupted turn subtype={event.get('subtype')!r}")
            return
        self._turn_active = False
        if self._inflight:
            self._inflight.clear()
            self._send_batch()
        try:
            sha = self.git() if self.git else None
        except Exception as exc:  # a failing git probe must not swallow the turn's `done`
            self._emit(f"git probe failed: {type(exc).__name__}")
            sha = None
        self._turns += 1
        lane_body = {}
        raw_usage = event.get("usage")
        if isinstance(raw_usage, dict):
            lane_body["usage"] = {k: v for k, v in raw_usage.items()
                                  if k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                           "cache_creation_input_tokens")
                                  and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
        if self.lane is not None:
            # A claim only: the IntegrationWriter re-validates it against the lane branch tip in Git.
            lane_body["evidence_ref"] = f"lane:{self.lane}:attempt{self.attempt}:turn{self._turns}" + (
                f":session{self._session_id}" if self._session_id else "")
            if sha and sha != self._start_head:
                lane_body["candidate_sha"] = sha
        cost = event.get("total_cost_usd")
        self._send(
            "done",
            {
                **lane_body,
                "sha": sha,
                "result_subtype": event.get("subtype") if isinstance(event.get("subtype"), str) else None,
                "is_error": bool(event.get("is_error", False)),
                "cost": cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
                **lane_body.pop("usage", {}),
                # The shim is the authority on which steers the model has consumed (latency #2): the runner gates
                # this done on these, never on its own tick-lagged receipts.
                "unconsumed_revs": sorted(self._pending_ack_revs),
                "last_rev": self._max_rev,
            },
        )
        # Not "idle": a turn ending is not the job finishing; Herdr would read idle as done.
        self._report_agent("working")

    # -------------------------------------------------------------- exit

    def _check_agent_exit(self) -> None:
        if self._agent is None or self._exited:
            return
        rc = self._agent.poll()
        if rc is None:
            return
        self._drain_agent_stderr()
        self._exited = True
        self.exit_code = rc
        self._send("error", {"exit_code": rc, "stderr_tail": self._stderr_tail})
        self._report_agent("working" if rc == 0 else "blocked")

    def _check_stop_grace(self) -> None:
        if not self._stopping or self._exited or self._agent is None:
            return
        now = self._now()
        if not self._terminated and now >= self._stop_deadline:
            self._terminated = True
            self._kill_deadline = now + self.stop_grace_s
            try:
                self._agent.terminate()
            except (OSError, ProcessLookupError):
                pass
        elif self._terminated and now >= self._kill_deadline:
            try:
                self._agent.kill()
            except (OSError, ProcessLookupError):
                pass


# ---------------------------------------------------------------- TRJ-04 helpers


def _dict(value: Any) -> dict:
    """`value` when it is an object, else `{}`: agent/frame fields of an unexpected shape read as absent."""
    return value if isinstance(value, dict) else {}


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _batch_input(name: str, tool_input: dict) -> dict:
    """What the runner needs to classify an in-flight call (strict pure-wait rule): a Bash call's command and
    flags, nothing for other tools (their name decides)."""
    if name != "Bash" or not isinstance(tool_input, dict):
        return {}
    out = {"command": str(tool_input.get("command") or "")[:4000]}
    for key in ("run_in_background", "timeout"):
        if key in tool_input:
            out[key] = tool_input[key]
    return out


def _extract_plan_items(tool_input: dict) -> list[str]:
    """A TodoWrite call's `todos` (or `items`), each entry's `content` or
    `title` string, capped to `_PLAN_MAX_ITEMS` entries of `_PLAN_MAX_CHARS`
    chars each."""
    todos = tool_input.get("todos") or tool_input.get("items") or []
    if not isinstance(todos, list):
        return []
    items: list[str] = []
    for todo in todos:
        text = todo.get("content") or todo.get("title") or "" if isinstance(todo, dict) else str(todo)
        text = text.strip() if isinstance(text, str) else str(text)
        if text:
            items.append(text[:_PLAN_MAX_CHARS])
    return items[:_PLAN_MAX_ITEMS]


def _tool_result_text(block: dict) -> str:
    """A `tool_result` block's content is either a plain string or a list of
    content blocks (only `type: text` ones are joined)."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item["text"] for item in content
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
        )
    return ""


def _match_test_summary(text: str) -> dict | None:
    """Numbers-only pytest/jest/go test summary, or `None` when the text
    doesn't look like one (TRJ-04, D4)."""
    pytest_passed = _PYTEST_PASSED_RE.search(text)
    pytest_failed = _PYTEST_FAILED_RE.search(text)
    pytest_error = _PYTEST_ERROR_RE.search(text)
    if pytest_passed or pytest_failed or pytest_error:
        return {
            "passed": int(pytest_passed.group(1)) if pytest_passed else 0,
            "failed": int(pytest_failed.group(1)) if pytest_failed else 0,
            "errors": int(pytest_error.group(1)) if pytest_error else 0,
        }

    jest_passed = _JEST_PASSED_RE.search(text)
    if jest_passed:
        return {"passed": int(jest_passed.group(1)), "failed": 0, "errors": 0}

    go_result = _GO_RESULT_RE.search(text)
    if go_result:
        ok = go_result.group(1) == "ok"
        return {"passed": 1 if ok else 0, "failed": 0 if ok else 1, "errors": 0}

    return None
