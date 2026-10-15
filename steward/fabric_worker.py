"""FabricWorker — the runner-side `WorkerAdapter` over the Phase 9/10 message
fabric (WRK-03, SP-35).

Peer of `steward/shim.py` (10-01). Every send/receive is a typed `Frame`
through the injected `api`/`mailbox` (SP-31) — never a scraped pane read.
`spawn_pane` is the one seam that touches Herdr pane *lifecycle* (split +
run), kept separate from message passing on purpose: it is ordinary pane
management (same shape as `steward.herdr_worker.HerdrWorker.start`), not a
frame conversation.

Ack ambiguity (carried over from `steward/shim.py`'s module docstring, 10-01):
an inbound frame of type `ack` means one of two unrelated things depending on
its body shape — a low-level transport ack (`{"acked_seq": N}`, FAB-02) or a
business-level steer receipt (`{"rev": N, "stage": "delivered"|"acknowledged"}`,
WRK-02). `ack_kind()` is the one helper that disambiguates by shape; it is
called exactly once, here, on every inbound `ack` frame. Transport acks are
consumed internally (they clear this worker's own `Outbox` and are never
surfaced); steer-receipt acks are transport-acked back to the shim in turn
and are the only `ack`-typed frames ever placed in a `WorkerObservation`'s
`events` — so `steward.runner` can trust every "ack" it sees there is a steer
receipt, never a transport ack, without re-checking the shape itself.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from steward.fabric import Frame, Outbox
from steward.herdr_transport import HerdrApiError, HerdrUnavailable
from steward.herdr_worker import WorkerStartBlocked
from steward.store import now_iso
from steward.worker import DeliveryResult, WorkerObservation

_REV_PREFIX = re.compile(r"^\[rev=(\d+)\]\s?")

# 09-SPIKE Q3 (`spikes/frame_echo.py`): the measured p99 round trip for a
# frame + its ack over the mailbox-pane transport (local sockets, 200 frames
# incl. 8 KiB) was 5.5 ms. This default is set well above that — high enough
# that a healthy ack is always drained before its frame's retransmit timeout
# elapses, so a healthy run's logs never show a retry (10-03 finding 5) —
# while still being short enough to recover a genuinely lost frame quickly.
_DEFAULT_ACK_TIMEOUT_S = 2.0


def ack_kind(body: dict) -> str:
    """Disambiguate an inbound `ack` frame's body by shape.

    Returns `"transport"` for a low-level transport ack (`acked_seq` — FAB-02,
    every frame type acks this way at the wire level) or `"steer_receipt"` for
    a business-level steer delivery/acknowledgement ack (`rev` + `stage` —
    WRK-02, `steward/shim.py`'s own outbound `ack` frames). Raises
    `ValueError` for a body that is neither shape — a real protocol
    violation, not something to silently guess at.
    """
    if "acked_seq" in body:
        return "transport"
    if "rev" in body:
        return "steer_receipt"
    raise ValueError(f"ack frame body is neither a transport ack nor a steer receipt: {body!r}")


@dataclass
class FabricWorker:
    api: Any
    mailbox: Any
    # The RUNNER's own pane (where *this* process's `mailbox` reads its
    # stdin from, and where the shim sends frames *to us*). Not a send
    # target for `FabricWorker` itself — kept mainly so callers/tests have
    # it at hand and so a future `spawn_pane` implementation can wire the
    # shim's `--peer` without a second constructor argument. Every frame
    # `FabricWorker` sends goes into `self.pane_id` instead (the worker's
    # own pane, once `spawn_pane` has created it) — the mailbox model means
    # you always write into the *peer's* pane, and the peer here is the shim.
    runner_pane: str
    job: str
    spawn_pane: Callable[[Path], str]
    lifecycle: Any = None  # optional LifecycleWatcher: pane_exited for the worker's own pane
    timeout_s: float = _DEFAULT_ACK_TIMEOUT_S
    max_retries: int = 5
    stop_grace_s: float = 5.0
    # 10-03 finding 1: how long `start()` waits for the shim's own `ready`
    # event before giving up (real production default; unit tests use a
    # tiny value so a deliberate timeout test doesn't block).
    ready_timeout_s: float = 20.0
    # 10-04 (fabric resume after controller restart): how long `attach()`
    # waits for the transport ack of its `cmd {op: rebind}` frame.
    rebind_timeout_s: float = 5.0
    clock: Callable[[], float] | None = None
    print_fn: Callable[[str], None] = print
    # Duck-typed marker `steward.runner` uses to route to the typed-frame path
    # instead of legacy marker/output_tail parsing (WRK-03) — mirrors
    # `HerdrWorker.kind` (`"claude"`/`"codex"`), which the runner already
    # reads the same way for phone deep links (`_record_worker_location`).
    kind: str = "fabric"
    # Fleet lane identity (protocol v2, FLEET-09). All unset = the legacy v1 single-worker route. Set by the
    # JobController only; `lease_check(frame)` asks the Store whether the frame's identity is the active
    # lease's, so a late frame from a fenced attempt is dropped before it can touch any state.
    task: str | None = None
    lane: str | None = None
    attempt: int | None = None
    epoch: int | None = None
    sender: str | None = None
    lease_check: Callable[[Frame], bool] | None = None

    def __post_init__(self) -> None:
        self._now = self.clock or time.time
        self.outbox = Outbox(timeout_s=self.timeout_s, max_retries=self.max_retries)
        self._seq = 0
        self.pane_id: str | None = None
        self.worktree: Path | None = None
        self._lifecycle_state = "unknown"
        self._exited = False
        self._exited_via_send_failure = False
        # Every outstanding permission request, oldest first: parallel tool calls
        # ask several at once, and each must get its own answer.
        self._pending_permissions: list[str] = []
        self._stopping = False
        self._stop_deadline = 0.0
        self._pane_created_by_me = False
        self._pane_closed = False
        # Frames already drained by `pump()` between runner ticks (latency fast path): the runner acted on the
        # urgent ones on arrival; the next `observe()` still hands every one of them to the tick, in order.
        self._held: list[Frame] = []
        self.stale_dropped = 0

    # ------------------------------------------------------------ outbound

    def _ident(self) -> tuple:
        if self.lane is None:
            return ()
        return (self.task, self.lane, self.attempt, self.sender or f"controller:{self.lane}", self.epoch)

    def _frame(self, type_: str, body: dict) -> Frame:
        if self.lane is None:
            return Frame(v=1, seq=self._next_seq(), job=self.job, type=type_, body=body)
        return Frame(v=2, seq=self._next_seq(), job=self.job, type=type_, body=body, task=self.task, lane=self.lane,
                     attempt=self.attempt, sender=self.sender or f"controller:{self.lane}", epoch=self.epoch)

    def _admit(self, frame: Frame) -> bool:
        """Whether an inbound frame belongs to this worker's own route: legacy workers take only v1; lane
        workers take only v2 frames for their exact task/lane/attempt/epoch and (if wired) the active lease."""
        if self.lane is None:
            return frame.v < 2
        if frame.v < 2 or (frame.task, frame.lane, frame.attempt, frame.epoch) != (
                self.task, self.lane, self.attempt, self.epoch):
            return False
        if frame.sender != f"shim:{self.lane}":
            return False
        return self.lease_check(frame) if self.lease_check is not None else True

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit(self, line: str) -> None:
        """One human-readable log line, written with `\\r\\n` (10-03 finding
        2): this process's own mailbox puts its tty in raw mode (OPOST off),
        so a bare `\\n` from `print_fn`'s own trailing newline doesn't return
        the carriage — every subsequent line drifts one column right,
        producing diagonal "staircase" output in the pane."""
        self.print_fn(line + "\r")

    def _send(self, type_: str, body: dict, *, track: bool = True) -> Frame:
        # Mailbox model (09-SPIKE): to reach a peer endpoint you write into
        # *its own* pane. The shim reads its stdin in `self.pane_id` (the
        # pane `spawn_pane` just split+ran it in), never `self.runner_pane`
        # (that's *our own* pane — where the shim sends frames *to us*,
        # wired via `--peer` when `spawn_pane` builds its command line).
        frame = self._frame(type_, body)
        try:
            self.api.send(self.pane_id, frame)
        except (HerdrApiError, HerdrUnavailable) as exc:
            # A send to a pane that is gone must never raise out of a runner
            # tick (11-01 carried fix, 10-04 open issue): treat it the same
            # as an observed exit, but flag it as *unconfirmed* — `stop()`
            # verifies via `pane.process_info` before trusting it, since a
            # single failed send could also be transient.
            self._emit(f"-> {frame.type} seq={frame.seq} FAILED ({exc})")
            self._exited = True
            self._exited_via_send_failure = True
            self._lifecycle_state = "exited"
            return frame
        self._emit(f"-> {frame.type} seq={frame.seq}")
        if track:
            self.outbox.add(frame, self._now())
        return frame

    def _send_transport_ack(self, acked_seq: int) -> None:
        frame = self._frame("ack", {"acked_seq": acked_seq})
        self.api.send(self.pane_id, frame)
        self._emit(f"-> ack seq={frame.seq} for={acked_seq}")

    def _tick_outbox(self) -> None:
        for frame in self.outbox.due(self._now()):
            self.api.send(self.pane_id, frame)
            self._emit(f"-> (retry) {frame.type} seq={frame.seq}")

    # ------------------------------------------------------------ WorkerAdapter

    def start(self, brief: str, worktree: Path) -> str:
        """Split a worker pane (cwd=worktree, --no-focus), run `steward shim`
        in it (both via the injected `spawn_pane`, ordinary pane management —
        not a frame send), wait for the shim's `ready` event, then send the
        brief frame.

        10-03 finding 1: sending the brief (or anything) before the shim has
        put its own stdin into raw mode risks the frame landing on the bare
        shell that ran `steward shim` a moment earlier — retransmits used to
        paper over the loss, but that is a correctness/safety hazard, not a
        fix. If `ready` never arrives within `ready_timeout_s`, the pane this
        call just created is closed and `WorkerStartBlocked` is raised — the
        brief was NOT delivered, and the runner turns this into NEEDS_HUMAN
        without ever having sent one."""
        self.worktree = Path(worktree)
        self.pane_id = self.spawn_pane(self.worktree)
        self._pane_created_by_me = True
        self._lifecycle_state = "working"
        self._await_ready()
        self._send("brief", {"text": brief})
        return self.pane_id

    def _await_ready(self) -> None:
        poll_s = 0.2
        elapsed = 0.0
        while elapsed < self.ready_timeout_s:
            step = min(poll_s, self.ready_timeout_s - elapsed)
            for frame in self._drain(timeout=step):
                if frame.type == "event" and frame.body.get("kind") == "ready":
                    return
            elapsed += step
        self.close_pane()
        raise WorkerStartBlocked(
            f"shim in pane {self.pane_id} did not report ready within {self.ready_timeout_s}s; brief not delivered"
        )

    def close_pane(self) -> bool:
        """Close this worker's own pane — but only a pane `spawn_pane` itself
        created here, and only once (SP-14; 10-03 finding 3). Returns whether
        a close was actually attempted (callers use this to decide whether a
        `worker pane closed` event is warranted)."""
        if not self._pane_created_by_me or self._pane_closed or self.pane_id is None:
            return False
        self._pane_closed = True
        try:
            self.api.call("pane.close", {"pane_id": self.pane_id})
        except (HerdrApiError, HerdrUnavailable):
            pass
        return True

    def attach(self, pane_id: str) -> bool:
        """Reattach to an existing worker pane after a controller restart
        (10-04, SP-9, SP-14): this is a brand-new `FabricWorker` instance (a
        fresh controller process), so liveness is verified against Herdr's
        own signal, never this object's memory. On success, the shim's peer
        is rebound to *this* runner's own pane and its unacked outbox is
        resent there, so nothing sent while the old runner pane was down is
        lost. Returns `False` (never raises) for a gone pane, a shim that
        isn't there, or a rebind that never acks — the caller then treats
        this worker as dead and the existing bounded-recovery path enforces
        single writer (a fresh pane, never touching whatever is at
        `pane_id`) rather than this call inventing a second writer itself."""
        self.pane_id = pane_id
        if not self._shim_is_foreground():
            self._exited = True
            self._lifecycle_state = "exited"
            return False
        # This is a fresh object with its own `_seq` counter starting back
        # at 1 — but the shim's `FrameReader` dedupes by `(job, seq)` for its
        # whole process lifetime, and it has already seen this job's low
        # seq values from the now-dead runner instance. Reusing them would
        # make the shim silently drop the rebind frame as a duplicate
        # (measured live, 10-04). Seeding well past any realistic prior
        # count avoids the collision for every future send too.
        self._seq = max(self._seq, int(self._now() * 1000) % 1_000_000_000)
        frame = self._send("cmd", {"op": "rebind", "peer": self.runner_pane})
        if not self._await_transport_ack(frame.seq, self.rebind_timeout_s):
            self._exited = True
            self._lifecycle_state = "exited"
            return False
        self._pane_created_by_me = True
        self._lifecycle_state = "working"
        # This fresh object has no idea what the worker is blocked on; ask the shim (it also volunteers a
        # snapshot on rebind, an older shim only answers this query).
        self.query_permissions()
        return True

    def query_permissions(self) -> None:
        """Ask the shim for `event {kind: pending_permissions}`: the authoritative list of worker requests
        still waiting on an answer. The reply arrives through `observe()` like every other frame."""
        if self.pane_id is not None and not self._exited:
            self._send("cmd", {"op": "list_permissions"})

    def _shim_is_foreground(self) -> bool:
        """Best-effort liveness check via `pane.process_info` (the same
        signal the 10-02 live gate used to confirm a crashed worker's pane
        had returned to its own shell) — true when something other than the
        bare shell currently owns the pane's foreground."""
        try:
            result = self.api.call("pane.process_info", {"pane_id": self.pane_id})
        except (HerdrApiError, HerdrUnavailable):
            return False
        info = result.get("process_info", result)
        fg = info.get("foreground_process_group_id")
        shell = info.get("shell_pid")
        return fg is not None and shell is not None and fg != shell

    def _await_transport_ack(self, seq: int, timeout_s: float) -> bool:
        poll_s = 0.2
        elapsed = 0.0
        while elapsed < timeout_s:
            step = min(poll_s, timeout_s - elapsed)
            self._drain(timeout=step)
            if not self.outbox.pending(self.job, seq, self._ident()):
                return True
            elapsed += step
        return not self.outbox.pending(self.job, seq, self._ident())

    def deliver(self, text: str, *, urgent: bool = False) -> DeliveryResult:
        """Send a steer frame. `text` is the runner's own `[rev=N] ...`
        convention (steward.runner); split it back into `{rev, text}` for the
        typed frame body the shim expects."""
        match = _REV_PREFIX.match(text)
        if match:
            rev = int(match.group(1))
            body_text = text[match.end():]
        else:
            rev = 0
            body_text = text
        body = {"rev": rev, "text": body_text}
        if urgent:
            # Extra body key: an older shim ignores it (plain steer).
            body["urgent"] = True
        self._send("steer", body)
        return DeliveryResult(ok=True, detail="delivered")

    def interrupt_waits(self, *, gen: int, call_ids: list[str], rev: int, rule_version: str) -> None:
        """Latency #7: ask the shim to cancel the in-flight batch, which the runner judged to be pure waits.
        The shim re-checks `gen` and the call set before acting and reports `waits_interrupted` or
        `interrupt_refused`."""
        self._send("cmd", {"op": "interrupt_waits", "gen": gen, "call_ids": list(call_ids), "rev": rev,
                           "rule_version": rule_version})

    def interrupt(self) -> None:
        """No typed interrupt frame exists (FAB-02's FRAME_TYPES); `stop()`
        is the bounded control primitive for this adapter. Kept as a no-op
        to satisfy the `WorkerAdapter` protocol without inventing an
        unspecified frame type."""

    def answer_prompt(self, approve: bool, request_id: str | None = None, message: str | None = None) -> DeliveryResult:
        """Answer one permission request: the named one, else the oldest this object knows of.

        A named request is sent even when this object has never seen it: after a runner restart its memory is
        empty, and only the shim knows what the worker is blocked on. The shim confirms with
        `permission_answered` (delivered / already delivered) or `permission_unknown` (no such pending
        request) — the only evidence the runner may use to call an answer stale (lost-approval hang)."""
        if request_id is None:
            if not self._pending_permissions:
                return DeliveryResult(ok=False, detail="no_pending_permission")
            request_id = self._pending_permissions[0]
        body = {"request_id": request_id}
        if message and not approve:
            body["message"] = message  # the deny reason the worker sees (an older shim ignores it)
        self._send("approve" if approve else "deny", body)
        if self._exited_via_send_failure:
            return DeliveryResult(ok=False, detail="send_failed")
        if request_id in self._pending_permissions:
            self._pending_permissions.remove(request_id)
        return DeliveryResult(ok=True, detail="approved" if approve else "denied")

    def stop(self) -> bool:
        """Send a stop frame, then wait (across repeated calls, one per tick
        — the same polling shape `steward.runner._handle_stopping` already
        drives every adapter with) for the error/exit frame or a
        `pane_exited` lifecycle event. Once the grace period elapses with
        neither observed, force-close the worker's own pane (mirrors
        `HerdrWorker.stop()`'s hard fallback) so a replacement is never
        blocked behind a hung shim (SP-14)."""
        if not self._stopping:
            self._stopping = True
            self._stop_deadline = self._now() + self.stop_grace_s
            self._send("stop", {})
        self._drain(timeout=0.2)
        if self._exited and not self._exited_via_send_failure:
            return True
        if self._exited and self._exited_via_send_failure and self._confirm_pane_gone():
            return True
        if self._now() < self._stop_deadline:
            return False
        self.close_pane()
        self._exited = True
        self._lifecycle_state = "exited"
        return True

    def _confirm_pane_gone(self) -> bool:
        """Only reached after a failed `_send` (pane possibly gone, not yet
        confirmed — a single send failure could be transient). Confirmed
        stopped when `pane.process_info` itself fails (the pane is truly
        gone) or succeeds but shows no shim in the foreground (it already
        exited back to a bare shell). Otherwise the shim is still there and
        this falls back to the ordinary grace-period wait."""
        try:
            result = self.api.call("pane.process_info", {"pane_id": self.pane_id})
        except (HerdrApiError, HerdrUnavailable):
            return True
        info = result.get("process_info", result)
        fg = info.get("foreground_process_group_id")
        shell = info.get("shell_pid")
        if fg is None or shell is None:
            return True
        return fg == shell

    def pump(self, timeout: float = 0.0) -> list[Frame]:
        """Drain the mailbox between ticks (latency fast path, 2026-10-02): returns the frames that arrived so
        the runner can answer a permission request or record a steer receipt at once, and keeps them for the
        next `observe()` so the tick still sees every frame exactly once."""
        frames = self._drain(timeout=timeout)
        self._held.extend(frames)
        return frames

    def observe(self) -> WorkerObservation:
        events, self._held = self._held + self._drain(timeout=0.2), []
        lifecycle = (
            "exited" if self._exited
            else "blocked" if self._pending_permissions
            else self._lifecycle_state
        )
        return WorkerObservation(
            lifecycle=lifecycle,
            output_tail="",
            pid_alive=(not self._exited),
            ts=now_iso(),
            events=tuple(events),
        )

    # ------------------------------------------------------------- inbound

    def _drain(self, timeout: float) -> list[Frame]:
        # Read the mailbox (applying any already-arrived ack) BEFORE checking
        # for due retransmits (10-03 finding 5): `observe()` runs only once
        # per runner tick, which can be tens of seconds — an ack that arrived
        # in the meantime must be drained first, or a healthy run logs a
        # spurious retry every single tick even though nothing was lost.
        surfaced: list[Frame] = []
        for frame in self.mailbox.read(timeout=timeout):
            if not self._admit(frame):
                # Stale/foreign identity: complete transport delivery so the sender stops retrying, but never
                # surface it or let it change worker state.
                self.stale_dropped += 1
                if frame.type != "ack":
                    try:
                        self._send_transport_ack(frame.seq)
                    except (HerdrApiError, HerdrUnavailable):
                        pass
                continue
            if frame.type == "ack":
                kind = ack_kind(frame.body)
                if kind == "transport":
                    self.outbox.ack(job=frame.job, seq=frame.body["acked_seq"], ident=self._ident())
                    continue
                # steer_receipt: business data, not just a transport ack —
                # transport-ack it back and surface it for the runner.
                self._send_transport_ack(frame.seq)
                surfaced.append(frame)
                continue

            self._send_transport_ack(frame.seq)
            if frame.type == "permission_request":
                rid = frame.body.get("request_id")
                if rid and rid not in self._pending_permissions:
                    self._pending_permissions.append(rid)
            elif frame.type == "event" and frame.body.get("kind") == "pending_permissions":
                # the shim's own list replaces whatever this object believed
                self._pending_permissions = [
                    p.get("request_id") for p in frame.body.get("pending") or [] if p.get("request_id")]
            elif frame.type == "event" and frame.body.get("kind") in ("permission_answered", "permission_unknown"):
                rid = frame.body.get("request_id")
                if rid in self._pending_permissions:
                    self._pending_permissions.remove(rid)
            elif frame.type == "event" and frame.body.get("kind") == "activity":
                self._lifecycle_state = "working"
            elif frame.type == "done":
                self._lifecycle_state = "idle"
            elif frame.type == "error":
                self._exited = True
                self._lifecycle_state = "exited"
            surfaced.append(frame)

        self._tick_outbox()

        if self.lifecycle is not None and self.pane_id is not None:
            for event in self.lifecycle.poll(0):
                if event.kind == "pane_exited" and event.pane_id == self.pane_id:
                    self._exited = True
                    self._lifecycle_state = "exited"

        return surfaced
