"""The operator control pane (OPX-01/02, SP-31/33).

`ControlPane` is the cockpit control pane 11-CONTEXT describes: a Herdr
pane that reads `cmd` frames off its own mailbox and executes each one
through injected op handlers (built by `steward.cli._build_control_handlers`,
which wire the real `StewardAPI`/`steward.control.Controller`). It never
sends a cmd frame to itself — `execute()` is a plain synchronous call, not
a relay — so there is no loop between the control pane and its own
handlers.

`relay()` / `send_cmd()` / `find_control_pane()` are the OTHER side: what
`steward say` and every CLI write verb use to reach a control pane over
Herdr and read its result back from the DB receipt (SP-32), since many
senders (a chat UI, an MCP service) are not themselves mailbox endpoints
that could receive a `reply` frame.

Deliberately a separate module from `steward/control.py` (the Phase-1
idempotent pause/resume/stop `Controller`, unrelated and unchanged by this
phase) — same "control" vocabulary, different concept; keeping them apart
avoids overloading one module with both meanings.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from steward import actor as actor_module, coordinator_guard, steer_agent, unblock_agent
from steward.fabric import Frame
from steward.herdr_transport import HerdrApiError, HerdrUnavailable
from steward.store import Store

# Discovery token (11-CONTEXT): the control pane sets this via
# `pane.report_metadata` so `find_control_pane` (used by `say` and every
# relayed CLI write verb) can pick it out of `pane.list` without any
# configured/well-known pane id.
CONTROL_ROLE_TOKEN = "steward_role"
CONTROL_ROLE_VALUE = "control"

# kv namespace for control-request receipts (CORE-04, SP-20). Not a real
# job row — `kv` has no foreign key into `jobs`, unlike `events` — just a
# fixed key under which every cmd's result is stashed by `request_id`, so a
# retried request (or a different process polling for the same one) never
# re-executes and always sees the same answer.
_CONTROL_KV_JOB = "_control"
_CONTROL_KV_PREFIX = "control_receipt:"

# Backoff for a receipt write that meets a locked database (issue Q: one "database is locked" killed the control
# pane on 2026-10-06). Each attempt itself waits up to the store's 30 s busy timeout, so the retries are bounded by a
# total deadline, not a count (issue W): a locked DB never holds the control loop for minutes. A receipt that still
# cannot be saved stays in the control journal and is saved on a later pass.
_RECEIPT_RETRY_DELAYS = (0.2, 0.5, 1.0, 2.0)
RECEIPT_DEADLINE_S = 45.0


class NoControlPane(Exception):
    """No live control pane was found, or it could not be reached."""


class ControlTimeout(Exception):
    """The control pane never answered within the timeout."""


def _receipt_key(request_id: str) -> str:
    return f"{_CONTROL_KV_PREFIX}{request_id}"


def get_receipt(store: Store, request_id: str) -> dict | None:
    """The stored result of a cmd, by `request_id` — read by `relay()`'s
    poll loop and by `ControlPane.execute()`'s own duplicate check."""
    return store.kv_get(_CONTROL_KV_JOB, _receipt_key(request_id))


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _put_receipt(store: Store, request_id: str, result: dict, *, sleep: Callable[[float], Any] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, deadline_s: float = RECEIPT_DEADLINE_S) -> None:
    """Store a cmd's receipt, retrying a locked/busy database with backoff until `deadline_s` has passed in total;
    the last error is raised."""
    start = clock()
    for delay in (*_RECEIPT_RETRY_DELAYS, None):
        try:
            store.kv_set(_CONTROL_KV_JOB, _receipt_key(request_id), result)
            return
        except sqlite3.OperationalError as exc:
            if delay is None or not _is_lock_error(exc) or clock() - start + delay > deadline_s:
                raise
            sleep(delay)


class TickSteps:
    """A control tick made of steps run in order (calling it runs them all; the first failure ends the tick, as one
    callable did). `ControlPane.run_forever` answers waiting cmd frames between the steps, on the same thread (one
    writer), so a ping waits at most for one step, never for a whole tick (TUI speed plan 12)."""

    def __init__(self, *steps: Callable[[], Any]):
        self.steps = steps

    def __call__(self) -> None:
        for step in self.steps:
            step()


@dataclass
class ControlPane:
    """Executes framed `cmd` requests via injected op handlers (OPX-01).

    Idempotent by `request_id` (CORE-04): a request already answered never
    re-executes — the stored result is returned again with
    `duplicate=True`. Handlers receive the full cmd dict (`request_id`,
    `op`, `args`, `origin`[, `reply_to`]) and return a plain JSON-able
    result dict; `ControlPane` never inspects `args` itself — op-specific
    shape belongs to the handler (built by
    `steward.cli._build_control_handlers`).
    """

    store: Store
    handlers: dict[str, Callable[[dict], dict]]
    print_fn: Callable[[str], None] = print
    reexec_fn: Callable[[], None] | None = None  # `self_exec` (steward deploy-self): exec this pane onto new code
    on_change: Callable[[], Any] | None = None  # runs after every executed cmd: the event-driven queue sweep
    journal: Any = None  # `control_journal.Journal`: durable received/done log (issue W); None = memory only
    _unsaved: dict[str, dict] = field(default_factory=dict, init=False, repr=False)  # receipts the DB refused (locked)

    def execute(self, cmd: dict) -> dict:
        request_id = cmd["request_id"]
        op = cmd.get("op")
        home = cmd.get("home")
        if home and Path(home).resolve() != Path(self.store.home).resolve():
            # A sender pointed at another store (e.g. a scratch STEWARD_HOME in a test) must never
            # write into this one. No receipt either: it would land in the wrong store.
            self.print_fn(f"cmd {op} request_id={request_id} ignored: sent for STEWARD_HOME {home}")
            return {"accepted": False, "detail": f"ignored: control pane serves {self.store.home}, not {home}"}
        cached = self._unsaved.get(request_id) or self._stored_receipt(request_id)
        if cached is None and self.journal is not None:
            cached = self.journal.result(request_id)  # answered by an earlier process; its receipt is not saved yet
        if cached is not None:
            self.print_fn(f"cmd {op} request_id={request_id} duplicate")
            result = dict(cached)
            result["duplicate"] = True
            return result

        self.print_fn(f"cmd {op} request_id={request_id} origin={cmd.get('origin')}")
        self._journal(lambda j: j.received(cmd))
        handler = self.handlers.get(op)
        refused = steer_agent.guard(self.store, cmd) or unblock_agent.guard(self.store, cmd) or coordinator_guard.guard(self.store, cmd)
        if refused:
            result: dict[str, Any] = {"accepted": False, "detail": f"refused: {refused}", "error_kind": "steer_agent_refused"}
        elif handler is None:
            result: dict[str, Any] = {"accepted": False, "detail": f"unknown control op: {op!r}"}
        else:
            try:
                with actor_module.bind(actor_module.label_for_cmd(cmd)):
                    result = dict(handler(cmd))
            except Exception as exc:  # noqa: BLE001 - a bad handler must never kill the control loop
                result = {"accepted": False, "detail": f"error executing {op}: {exc}"}
        result.setdefault("duplicate", False)
        self._journal(lambda j: j.done(request_id, result))
        try:
            steer_agent.record_action(self.store, cmd, result)
        except Exception:  # noqa: BLE001 - the feed line is best-effort
            pass
        try:
            coordinator_guard.record_action(self.store, cmd, result)
        except Exception:  # noqa: BLE001 - the feed line is best-effort
            pass
        try:
            _put_receipt(self.store, request_id, result)
        except sqlite3.OperationalError as exc:  # never let one receipt write kill the loop: save it next pass
            self._unsaved[request_id] = result
            self.print_fn(f"receipt {request_id} not saved yet ({exc}); retrying next pass")
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception as exc:  # noqa: BLE001 - the queue sweep must never fail a cmd
                self.print_fn(f"on_change error: {exc}")
        self.print_fn(f"result {op} request_id={request_id} accepted={result.get('accepted')}")
        return result

    def step(self, mailbox: Any, herdr_api: Any, *, timeout: float = 0.5) -> list[dict]:
        """One pass over whatever `cmd` frames are already waiting on
        `mailbox`. A `reply` frame is sent back only when the cmd names a
        `reply_to` pane (11-CONTEXT: most senders are not mailbox endpoints
        and read the DB receipt via `relay()` instead)."""
        self._save_unsaved()
        results = []
        for frame in mailbox.read(timeout=timeout):
            if frame.type != "cmd":
                self.print_fn(f"ignoring non-cmd frame type={frame.type}")
                continue
            result = self.execute(frame.body)
            results.append(result)
            reply_to = frame.body.get("reply_to")
            if reply_to:
                self._send_reply(herdr_api, reply_to, frame.body["request_id"], result)
        if self.reexec_fn is not None and any(r.get("reexec") and not r.get("duplicate") for r in results):
            self.reexec_fn()  # after the receipt + reply are out; in production this never returns
        return results

    def _stored_receipt(self, request_id: str) -> dict | None:
        try:
            return get_receipt(self.store, request_id)
        except sqlite3.OperationalError:
            return None

    def _journal(self, fn: Callable[[Any], Any]) -> None:
        if self.journal is None:
            return
        try:
            fn(self.journal)
        except OSError as exc:  # a full disk must not stop commands; the DB receipt still records the result
            self.print_fn(f"journal write failed: {exc}")

    def _save_unsaved(self) -> None:
        if self.journal is not None:
            try:
                self.journal.reconcile(lambda rid: self._stored_receipt(rid) is not None,
                                       lambda rid, res: _put_receipt(self.store, rid, res, sleep=lambda _s: None))
                self._unsaved.clear()
            except (sqlite3.OperationalError, OSError):
                pass  # still locked: the journal keeps them, the next pass tries again
            return
        for request_id, result in list(self._unsaved.items()):
            try:
                _put_receipt(self.store, request_id, result, sleep=lambda _s: None)
            except sqlite3.OperationalError:
                return  # still locked: the next pass tries again
            del self._unsaved[request_id]

    def _safe_step(self, mailbox: Any, herdr_api: Any, *, timeout: float) -> None:
        try:
            self.step(mailbox, herdr_api, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - one bad pass (e.g. "database is locked") must never end the loop
            self.print_fn(f"step error: {type(exc).__name__}: {exc}")

    def run_forever(
        self,
        mailbox: Any,
        herdr_api: Any,
        *,
        own_pane: str | None = None,
        poll_s: float = 0.5,
        running: Callable[[], bool] = lambda: True,
        tick: Callable[[], Any] | None = None,
        tick_every_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        beat_fn: Callable[[], Any] | None = None,
    ) -> None:
        """`steward control`'s own loop. Reports the `steward_role=control`
        token once at startup (best-effort — a Herdr hiccup here never
        blocks serving cmds), then calls `step()` until `running()` is
        false (production default: forever).

        `tick` (Phase 17-03: the question-answer flusher) runs at most once
        per `tick_every_s`, whether or not any cmd frame arrived; an
        exception inside it is printed and never ends the loop. A `TickSteps`
        tick yields to waiting cmd frames between its steps. `beat_fn` (the
        heartbeat file the watchdog reads, issue W) runs every pass and
        between tick steps; it never raises into the loop."""
        if own_pane:
            _report_control_role(herdr_api, own_pane)
        last_tick: float | None = None
        def beat() -> None:
            if beat_fn is not None:
                try:
                    beat_fn()
                except Exception:  # noqa: BLE001 - a heartbeat failure never ends the loop
                    pass

        while running():
            beat()
            self._safe_step(mailbox, herdr_api, timeout=poll_s)
            if tick is None:
                continue
            now = clock()
            if last_tick is not None and now - last_tick < tick_every_s:
                continue
            last_tick = now
            for i, step in enumerate(getattr(tick, "steps", None) or (tick,)):
                if i:
                    beat()
                    self._safe_step(mailbox, herdr_api, timeout=0.0)  # frames that arrived during the previous step
                try:
                    step()
                except Exception as exc:  # noqa: BLE001 - a flusher failure must never stop the control pane
                    self.print_fn(f"tick error: {exc}")
                    break

    def _send_reply(self, herdr_api: Any, reply_to: str, request_id: str, result: dict) -> None:
        reply = Frame(v=1, seq=1, job=request_id, type="reply", body={"request_id": request_id, "result": result})
        try:
            herdr_api.send(reply_to, reply)
        except (HerdrApiError, HerdrUnavailable):
            pass


def _report_control_role(herdr_api: Any, pane_id: str) -> None:
    try:
        herdr_api.call(
            "pane.report_metadata",
            {
                "pane_id": pane_id,
                "source": "steward",
                "tokens": {CONTROL_ROLE_TOKEN: CONTROL_ROLE_VALUE},
                "title": "steward control",
            },
        )
    except (HerdrApiError, HerdrUnavailable):
        pass


def find_control_pane(herdr_api: Any) -> str | None:
    """The pane whose token `steward_role=control` was set by a live
    `steward control` (11-CONTEXT). `None` (no silent fallback) when Herdr
    is unreachable or no pane carries the token."""
    try:
        result = herdr_api.call("pane.list", {})
    except (HerdrApiError, HerdrUnavailable):
        return None
    for pane in result.get("panes", []):
        if pane.get("tokens", {}).get(CONTROL_ROLE_TOKEN) == CONTROL_ROLE_VALUE:
            return pane.get("pane_id")
    return None


def send_cmd(
    herdr_api: Any,
    pane_id: str,
    request_id: str,
    op: str,
    args: dict,
    origin: str,
    *,
    reply_to: str | None = None,
    home: str | None = None,
    actor: str | None = None,
) -> None:
    """Write one `cmd` frame into the control pane (OPX-02, SP-31): every
    operator/ChatGPT/Moshi command is visible there. `job` is set to
    `request_id`, not the Steward job id — it only needs to be globally
    unique for `FrameReader`'s `(job, seq)` dedupe, and a short-lived sender
    process's own seq counter restarting at 1 every time would otherwise
    risk colliding with another request against the very same job (the
    same pitfall 10-04 hit and fixed in `FabricWorker.attach`)."""
    body = {"request_id": request_id, "op": op, "args": args, "origin": origin}
    if home:
        body["home"] = home  # the sender's STEWARD_HOME; the control pane refuses any other store's
    if actor:
        body["actor"] = actor
    if reply_to:
        body["reply_to"] = reply_to
    frame = Frame(v=1, seq=1, job=request_id, type="cmd", body=body)
    herdr_api.send(pane_id, frame)


def relay(
    store: Store,
    herdr_api: Any,
    op: str,
    args: dict,
    *,
    origin: str = "cli",
    request_id: str | None = None,
    reply_to: str | None = None,
    actor: str | None = None,
    timeout_s: float = 30.0,
    poll_s: float = 0.3,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> dict:
    """`steward say` and every CLI write verb (OPX-02): find the control
    pane, send it a `cmd` frame, then poll the DB receipt (keyed by
    `request_id`, SP-32) until it appears or `timeout_s` elapses. Never
    calls `StewardAPI` itself — only the control pane executes commands
    (SP-2); this function only ever sends a frame and reads memory back."""
    request_id = request_id or str(uuid.uuid4())
    pane_id = find_control_pane(herdr_api)
    if pane_id is None:
        raise NoControlPane("no control pane — start one: `steward control`")
    try:
        send_cmd(herdr_api, pane_id, request_id, op, args, origin, reply_to=reply_to, home=str(store.home),
                 actor=actor)
    except (HerdrApiError, HerdrUnavailable) as exc:
        raise NoControlPane(f"control pane unreachable: {exc}") from exc

    deadline = clock() + timeout_s
    while True:
        cached = get_receipt(store, request_id)
        if cached is not None:
            return cached
        if clock() >= deadline:
            raise ControlTimeout(
                "control pane did not answer (it ignores commands from a different STEWARD_HOME)"
            )
        sleep(poll_s)
