"""Worker adapter protocol (SP-1, SP-3).

`WorkerAdapter` is the one seam through which the controller talks to a
worker process, whatever hosts it. Herdr hosts the pane the worker lives in
but its idle/done states and `--wait` are not completion receipts (SP-3) —
`observe()` only ever reports what was actually seen, and the controller
decides what it means.
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from steward.fabric import Frame
from steward.store import now_iso

Lifecycle = str  # "working" | "idle" | "blocked" | "exited" | "unknown"


@dataclass(frozen=True)
class WorkerObservation:
    lifecycle: Lifecycle
    output_tail: str
    pid_alive: bool | None
    ts: str
    # Typed frames observed since the previous `observe()` call (WRK-03,
    # SP-35). Empty for every adapter that predates the fabric (`HerdrWorker`,
    # `FakeWorker`) — added backward-compatibly with a default so existing
    # `WorkerObservation(...)` call sites never need to change. Only
    # `FabricWorker` populates this; its own `ack_kind()` helper has already
    # filtered out low-level transport acks, so every frame here is one the
    # runner is meant to act on (steer receipt, permission_request, done,
    # error, activity event) — never a transport ack.
    events: tuple[Frame, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    detail: str


class WorkerAdapter(Protocol):
    def start(self, brief: str, worktree: Path) -> str: ...

    def deliver(self, text: str) -> DeliveryResult: ...

    def interrupt(self) -> None: ...

    def stop(self) -> bool: ...

    def observe(self) -> WorkerObservation: ...

    def answer_prompt(self, approve: bool, request_id: str | None = None) -> DeliveryResult: ...


class FakeWorker:
    """Scripted worker for tests. Never spawns a process, never calls herdr.

    `script` is a list of `WorkerObservation` or `callable(worker) ->
    WorkerObservation` items consumed in order by `observe()`; a callable may
    mutate files under `self.worktree` before returning its observation, so
    tests can simulate a worker actually editing the repo. Once the script is
    exhausted, `observe()` settles into a steady idle observation.

    `ack_revs=True` makes `deliver(text)` look for `rev=<N>` in the delivered
    text and queue a `STEWARD-ACK rev=<N>` observation to be returned by the
    *next* `observe()` call — mirroring a real worker replying to a steering
    instruction before continuing.
    """

    def __init__(
        self,
        script: list[WorkerObservation | Callable[["FakeWorker"], WorkerObservation]]
        | None = None,
        *,
        ack_revs: bool = False,
    ):
        self.script = list(script) if script is not None else []
        self._step = 0
        self._pending_ack: int | None = None
        self.ack_revs = ack_revs

        self.started_brief: str | None = None
        self.worktree: Path | None = None
        self.worker_ref: str | None = None
        self.delivered: list[str] = []
        self.urgent_delivered: list[str] = []
        self.interrupted = False
        self.stopped = False
        self.answered: list[bool] = []
        self.answered_ids: list[str | None] = []

    def start(self, brief: str, worktree: Path) -> str:
        self.started_brief = brief
        self.worktree = Path(worktree)
        self.worker_ref = f"fake-{uuid.uuid4()}"
        return self.worker_ref

    def deliver(self, text: str, *, urgent: bool = False) -> DeliveryResult:
        self.delivered.append(text)
        if urgent:
            self.urgent_delivered.append(text)
        if self.ack_revs:
            match = re.search(r"rev=(\d+)", text)
            if match:
                self._pending_ack = int(match.group(1))
        return DeliveryResult(ok=True, detail="delivered")

    def interrupt(self) -> None:
        self.interrupted = True

    def stop(self) -> bool:
        self.stopped = True
        return True

    def answer_prompt(self, approve: bool, request_id: str | None = None, message: str | None = None) -> DeliveryResult:
        self.answered.append(approve)
        if message is not None:
            self.answer_messages = [*getattr(self, "answer_messages", []), message]
        self.answered_ids.append(request_id)
        return DeliveryResult(ok=True, detail="approved" if approve else "denied")

    def observe(self) -> WorkerObservation:
        if self._pending_ack is not None:
            rev = self._pending_ack
            self._pending_ack = None
            return WorkerObservation(
                lifecycle="working",
                output_tail=f"STEWARD-ACK rev={rev}",
                pid_alive=True,
                ts=now_iso(),
            )
        if self._step < len(self.script):
            item = self.script[self._step]
            self._step += 1
            if callable(item):
                return item(self)
            return item
        return WorkerObservation(
            lifecycle="idle", output_tail="", pid_alive=True, ts=now_iso()
        )


# -- structured Codex lifecycle (Phase 29, FLEET-18) -----------------------------------------------------------------

CODEX_APPROVAL_POLICY = "on-request"   # fixed: the adapter can never launch a thread with "never" or full access
CODEX_SANDBOX = "workspace-write"
_APPROVAL_TOOLS = {
    "item/commandExecution/requestApproval": "Bash",
    "item/fileChange/requestApproval": "Edit",
    "item/permissions/requestApproval": "Permissions",
}


class AppServerError(RuntimeError):
    """A JSON-RPC error or transport failure from the app server. `error` is the server's JSON-RPC error object when
    there was one (None for a timeout or transport failure)."""

    def __init__(self, message: str, error: dict | None = None):
        super().__init__(message)
        self.error = error


class AppServerTransport(Protocol):
    """What the adapter needs from `codex app-server` (JSON-RPC 2.0, one message per line). Injected so tests use a
    scripted fake; `StdioAppServerTransport` is the process-backed implementation."""

    def request(self, method: str, params: dict, timeout: float = 30.0) -> dict: ...

    def notify(self, method: str, params: dict | None = None) -> None: ...

    def drain(self) -> list[dict]: ...  # notifications and server->client requests (those carry an "id")

    def respond(self, request_id, result: dict | None = None, error: dict | None = None) -> None: ...

    def pid(self) -> int | None: ...

    def alive(self) -> bool: ...

    def terminate_tree(self) -> bool: ...  # True only once the whole process group is confirmed gone


class StdioAppServerTransport:
    """A line-delimited JSON-RPC server (`codex app-server`, `muse serve`) over stdio in its own process group.
    `popen`/`killpg` are injectable. Reader thread routes responses to waiting requests and everything else to
    `drain()`. `jsonrpc=True` adds the `"jsonrpc": "2.0"` envelope to every message (muse requires it; codex's wire
    format is unchanged without it); `env` replaces the child environment (None = inherit)."""

    def __init__(self, argv: list[str], *, cwd: str | None = None, popen=subprocess.Popen, killpg=os.killpg,
                 clock: Callable[[], float] = time.monotonic, grace_s: float = 3.0, jsonrpc: bool = False,
                 env: dict | None = None):
        self.argv, self.cwd, self._killpg, self._clock, self.grace_s = argv, cwd, killpg, clock, grace_s
        self.jsonrpc, self.env = jsonrpc, env
        self._popen = popen
        self._proc = None
        self._next = 0
        self._waiting: dict = {}
        self._eof = False
        self._inbox: "queue.Queue[dict]" = queue.Queue()
        self._lock = threading.Lock()

    def start(self) -> None:
        self._eof = False
        kw = {"env": self.env} if self.env is not None else {}
        self._proc = self._popen(self.argv, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True, **kw)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self._proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if "method" not in msg and "id" in msg:
                box = self._waiting.get(msg["id"])
                if box is not None:
                    box.put(msg)
            else:
                self._inbox.put(msg)
        self._eof = True  # the server closed stdout: nobody can answer a waiting request any more
        for box in list(self._waiting.values()):
            box.put({"error": {"code": -32000, "message": "server exited"}})

    def _send(self, msg: dict) -> None:
        if self.jsonrpc:
            msg = {"jsonrpc": "2.0", **msg}
        with self._lock:
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()

    def request(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        if self._proc is None or self._eof or self._proc.poll() is not None:
            raise AppServerError(f"{method}: server process is not running")
        self._next += 1
        rid = self._next
        box: "queue.Queue[dict]" = queue.Queue()
        self._waiting[rid] = box
        try:
            self._send({"id": rid, "method": method, "params": params})
            try:
                msg = box.get(timeout=timeout)
            except queue.Empty:
                raise AppServerError(f"{method}: timed out") from None
        finally:
            self._waiting.pop(rid, None)
        if "error" in msg:
            raise AppServerError(f"{method}: {msg['error']}", msg["error"] if isinstance(msg["error"], dict) else None)
        return msg.get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"method": method, **({"params": params} if params is not None else {})})

    def drain(self) -> list[dict]:
        out = []
        while True:
            try:
                out.append(self._inbox.get_nowait())
            except queue.Empty:
                return out

    def respond(self, request_id, result: dict | None = None, error: dict | None = None) -> None:
        self._send({"id": request_id, **({"error": error} if error is not None else {"result": result or {}})})

    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def terminate_tree(self) -> bool:
        if self._proc is None:
            return True
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if self._proc.poll() is not None:
                break
            try:
                self._killpg(self._proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            deadline = self._clock() + self.grace_s
            while self._proc.poll() is None and self._clock() < deadline:
                time.sleep(0.05)
        return self._proc.poll() is not None


class NdjsonProcess:
    """A CLI speaking one JSON object per line on stdin/stdout (agy stream-json, pi rpc), in its own process group.
    `popen`/`killpg` are injectable so tests use a scripted fake. Every stdout record goes to `drain()`; the last
    stderr lines are kept for evidence (never parsed as protocol)."""

    def __init__(self, argv: list[str], *, cwd: str | None = None, env: dict | None = None, popen=subprocess.Popen,
                 killpg=os.killpg, clock: Callable[[], float] = time.monotonic, grace_s: float = 3.0):
        self.argv, self.cwd, self.env = list(argv), cwd, env
        self._popen, self._killpg, self._clock, self.grace_s = popen, killpg, clock, grace_s
        self._proc = None
        self._inbox: "queue.Queue[dict]" = queue.Queue()
        self._lock = threading.Lock()
        self.stderr_tail: list[str] = []

    def start(self) -> None:
        kw = {"env": self.env} if self.env is not None else {}
        self._proc = self._popen(self.argv, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True, **kw)
        threading.Thread(target=self._read, daemon=True).start()
        if getattr(self._proc, "stderr", None) is not None:
            threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self) -> None:
        for line in self._proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                self._inbox.put(msg)

    def _read_err(self) -> None:
        for line in self._proc.stderr:
            self.stderr_tail = (self.stderr_tail + [line.rstrip()[:300]])[-20:]

    def send(self, msg: dict) -> bool:
        if not self.alive():
            return False
        try:
            with self._lock:
                self._proc.stdin.write(json.dumps(msg) + "\n")
                self._proc.stdin.flush()
        except (OSError, ValueError):
            return False
        return True

    def drain(self) -> list[dict]:
        out = []
        while True:
            try:
                out.append(self._inbox.get_nowait())
            except queue.Empty:
                return out

    def wait_for(self, pred: Callable[[dict], bool], timeout: float, held: list) -> dict | None:
        """Block (bounded) until a record matching `pred` arrives; every record read meanwhile is appended to
        `held` in order, so nothing is lost to the caller's next `drain`."""
        deadline = self._clock() + timeout
        while True:
            try:
                msg = self._inbox.get(timeout=0.05)
            except queue.Empty:
                if self._clock() >= deadline or not self.alive():
                    for m in self.drain():
                        held.append(m)
                        if pred(m):
                            return m
                    return None
                continue
            held.append(msg)
            if pred(msg):
                return msg

    def signal(self, sig) -> bool:
        if not self.alive():
            return False
        try:
            self._killpg(self._proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def terminate_tree(self) -> bool:
        if self._proc is None:
            return True
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if self._proc.poll() is not None:
                break
            try:
                self._killpg(self._proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            deadline = self._clock() + self.grace_s
            while self._proc.poll() is None and self._clock() < deadline:
                time.sleep(0.05)
        return self._proc.poll() is not None


def _permission_view(tool: str, params: dict) -> tuple[str, dict] | None:
    """Express a Codex approval request in the Claude tool vocabulary `steward.permissions.decide` already judges,
    so one authority policy covers both runtimes. None = not expressible (the adapter then refuses)."""
    if tool == "Bash":
        return "Bash", {"command": params.get("command") or ""}
    if tool == "Edit":
        root = params.get("grantRoot")
        return "Edit", ({"file_path": root} if root else {"reason": params.get("reason") or ""})
    prof = params.get("permissions") or {}
    fs_ = prof.get("fileSystem") or prof.get("file_system") or {}
    writes = fs_.get("write") if isinstance(fs_, dict) else None
    if isinstance(writes, list) and writes and isinstance(writes[0], str):
        return "Edit", {"file_path": writes[0]}
    if prof.get("network"):
        return "WebFetch", {"url": "*"}
    return None


class CodexAppServerAdapter:
    """Structured `WorkerAdapter` over the Codex app-server protocol (WORK-01, FLEET-18).

    thread/start (exact requested vs native model identity checked) + turn/start launch; `turn/steer(expectedTurnId)`
    is a typed, acknowledged instruction append; server approval requests become the SAME `permission_request`
    frames the Claude fabric emits (tool + input), so one authority policy decides and the adapter never answers one
    itself; `turn/completed` becomes a typed done/error; `turn/interrupt` + a confirmed process-group kill is the
    stop; `thread/resume` reattaches. Launch parameters are fixed constants (on-request approvals, workspace-write
    sandbox): nothing the controller or a steer sends can change sandbox or approval policy (SP-10).
    Token usage is reported only when the server sends it; cost is always None (the protocol carries none)."""

    runtime = "codex"
    adapter_version = "1"
    kind = "codex-structured"

    def __init__(self, transport: AppServerTransport | None = None, *, model: str | None = None,
                 job_id: str = "", task: str | None = None, lane: str | None = None, attempt: int | None = None,
                 epoch: int | None = None, sender: str | None = None, lease_check: Callable[[Frame], bool] | None = None,
                 head_fn: Callable[[Path], str | None] | None = None):
        if transport is None:
            raise NotImplementedError(
                "CodexAppServerAdapter needs an AppServerTransport (StdioAppServerTransport or a test fake)")
        self.transport, self.model = transport, model
        self.job, self.task, self.lane, self.attempt, self.epoch = job_id, task, lane, attempt, epoch
        self.sender = sender or f"codex:{lane}"
        self.lease_check, self.head_fn = lease_check, head_fn
        self.thread_id: str | None = None
        self.native_model: str | None = None
        self.turn_id: str | None = None
        self.worktree: Path | None = None
        self._base_head: str | None = None
        self._seq = 0
        self._held: list[Frame] = []
        self._perm: dict[str, tuple] = {}   # request_id -> (rpc id, method, params)
        self._usage: dict | None = None
        self._stopped = False
        self._initialized = False
        self._interrupt_turn: str | None = None   # an interrupt is owed to this turn until it completes
        self.stale_dropped = 0

    # -- frames ---------------------------------------------------------------------------------------------------------
    def _frame(self, type_: str, body: dict) -> Frame:
        self._seq += 1
        if self.lane is None:
            return Frame(v=1, seq=self._seq, job=self.job, type=type_, body=body)
        return Frame(v=2, seq=self._seq, job=self.job, type=type_, body=body, task=self.task, lane=self.lane,
                     attempt=self.attempt, sender=self.sender, epoch=self.epoch)

    # -- lifecycle ------------------------------------------------------------------------------------------------------
    def _ensure_transport(self) -> None:
        """A process-backed transport is spawned on first use; test fakes have no `start`."""
        starter = getattr(self.transport, "start", None)
        if callable(starter) and not self.transport.alive():
            starter()

    def _connect(self) -> None:
        """Spawn the process if needed and perform the `initialize` handshake exactly once per transport: the server
        refuses every other request on a connection that has not shaken hands (seen live, codex 0.157)."""
        self._ensure_transport()
        if not self._initialized:
            self.transport.request("initialize", {"clientInfo": {"name": "steward", "title": None, "version": "1"}})
            self.transport.notify("initialized")
            self._initialized = True

    def start(self, brief: str, worktree: Path) -> str:
        self.worktree = Path(worktree)
        self._connect()
        params = {"cwd": str(self.worktree), "approvalPolicy": CODEX_APPROVAL_POLICY, "sandbox": CODEX_SANDBOX}
        if self.model:
            params["model"] = self.model
        res = self.transport.request("thread/start", params)
        self.thread_id = (res.get("thread") or {}).get("id")
        self.native_model = res.get("model")
        if not self.thread_id:
            raise AppServerError("thread/start returned no thread id")
        if self.model and str(self.native_model or "").lower() != self.model.lower():
            self.stop()
            raise AppServerError(f"model_identity_mismatch: requested {self.model!r}, got {self.native_model!r}")
        if self.head_fn is not None:
            self._base_head = self.head_fn(self.worktree)
        self._start_turn(brief)
        return f"codex:{self.thread_id}"

    def _start_turn(self, text: str) -> None:
        res = self.transport.request("turn/start", {"threadId": self.thread_id,
                                                    "input": [{"type": "text", "text": text}]})
        self.turn_id = (res.get("turn") or {}).get("id")

    def process_identity(self) -> dict:
        return {"thread_id": self.thread_id, "native_model": self.native_model, "requested_model": self.model,
                "pid": self.transport.pid()}

    def attach(self, ref) -> bool:
        tid = str(ref).removeprefix("codex:")
        try:
            self._connect()
            res = self.transport.request("thread/resume", {"threadId": tid})
        except AppServerError:
            return False
        if (res.get("thread") or {}).get("id") != tid:
            return False
        self.thread_id, self.native_model = tid, res.get("model") or self.native_model
        return True

    def deliver(self, text: str, *, urgent: bool = False) -> DeliveryResult:
        if self.thread_id is None:
            return DeliveryResult(ok=False, detail="not_started")
        rev = re.search(r"rev=(\d+)", text)
        try:
            if self.turn_id is None:
                self._start_turn(text)
                got = self.turn_id
            else:
                res = self.transport.request("turn/steer", {"threadId": self.thread_id, "expectedTurnId": self.turn_id,
                                                            "input": [{"type": "text", "text": text}]})
                got = res.get("turnId")
        except AppServerError as exc:
            return DeliveryResult(ok=False, detail=f"steer_failed: {exc}"[:200])
        if got != self.turn_id:
            return DeliveryResult(ok=False, detail="steer_not_acknowledged")
        if rev:  # the server accepted the steer into the expected turn: a typed receipt
            self._held.append(self._frame("ack", {"rev": int(rev.group(1)), "stage": "acknowledged",
                                                  "via": "turn/steer"}))
        return DeliveryResult(ok=True, detail="delivered")

    def interrupt(self) -> None:
        if self.thread_id and self.turn_id:
            self._interrupt_turn = self.turn_id
            self._send_interrupt()

    def _send_interrupt(self) -> None:
        """An interrupt that lands before the server has really begun the turn is accepted but has no effect (seen
        live, codex 0.157), so `observe` re-sends it until that turn completes."""
        if self.thread_id and self.turn_id:
            try:
                self.transport.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id})
            except AppServerError:
                pass

    def stop(self) -> bool:
        """Interrupt, then kill the whole process group; True only when the tree is confirmed gone."""
        self.interrupt()
        self._stopped = True
        return bool(self.transport.terminate_tree())

    def answer_prompt(self, approve: bool, request_id: str | None = None, message: str | None = None) -> DeliveryResult:
        if request_id is None and len(self._perm) == 1:
            request_id = next(iter(self._perm))
        entry = self._perm.pop(str(request_id), None) if request_id is not None else None
        if entry is None:
            return DeliveryResult(ok=False, detail="unknown_request")
        rpc_id, method, params = entry
        if method == "item/permissions/requestApproval":
            result = {"permissions": params.get("permissions") if approve else {}, "scope": "turn"}
        else:
            result = {"decision": "accept" if approve else "decline"}
        self.transport.respond(rpc_id, result)
        return DeliveryResult(ok=True, detail="approved" if approve else "denied")

    def usage(self) -> dict | None:
        total = ((self._usage or {}).get("total") or {}).get("totalTokens")
        if not isinstance(total, int):
            return None
        return {"tokens": total, "cost_usd": None, "source": "codex_app_server", "confidence": "reported"}

    # -- observation ----------------------------------------------------------------------------------------------------
    def _current(self, frame: Frame) -> bool:
        if self.lease_check is not None and not self.lease_check(frame):
            self.stale_dropped += 1
            return False
        return True

    def observe(self) -> WorkerObservation:
        events = self._held
        self._held = []
        for m in self.transport.drain():
            method, params = m.get("method"), m.get("params") or {}
            if "id" in m and method:  # server -> client request
                tool = _APPROVAL_TOOLS.get(method)
                if tool is None:  # anything we do not understand is refused, never auto-answered with consent
                    self.transport.respond(m["id"], error={"code": -32601, "message": "unsupported"})
                    continue
                view = _permission_view(tool, params)
                if view is None:  # cannot be expressed in the shared authority policy: refuse, never guess consent
                    self.transport.respond(m["id"], {"permissions": {}, "scope": "turn"}
                                           if method == "item/permissions/requestApproval" else
                                           {"decision": "decline"})
                    continue
                rid = str(m["id"])
                self._perm[rid] = (m["id"], method, params)
                events.append(self._frame("permission_request", {"request_id": rid, "tool": view[0],
                                                                 "input": view[1]}))
            elif method == "turn/started":
                self.turn_id = (params.get("turn") or {}).get("id") or self.turn_id
            elif method == "thread/tokenUsage/updated":
                self._usage = params.get("tokenUsage")
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                status = turn.get("status")
                self.turn_id = None
                self._interrupt_turn = None
                if status == "completed":
                    body = {"turn_id": turn.get("id"), "status": status,
                            "evidence_ref": f"codex:{self.thread_id}:{turn.get('id')}"}
                    head = self.head_fn(self.worktree) if self.head_fn and self.worktree else None
                    if head and head != self._base_head:
                        body["candidate_sha"] = head
                    events.append(self._frame("done", body))
                else:
                    events.append(self._frame("error", {"turn_id": turn.get("id"), "status": status,
                                                        "detail": str(turn.get("error") or status)[:300]}))
            elif method == "error":
                events.append(self._frame("error", {"detail": str(params.get("error") or params)[:300]}))
        if self._interrupt_turn and self._interrupt_turn == self.turn_id and not self._stopped:
            self._send_interrupt()
        kept = tuple(e for e in events if self._current(e))
        alive = self.transport.alive()
        life = "exited" if not alive else ("working" if self.turn_id else "idle")
        return WorkerObservation(lifecycle=life, output_tail="", pid_alive=alive, ts=now_iso(), events=kept)


def codex_lane_factory(argv: list[str] | None = None, *, model: str | None = None,
                       model_for: Callable[[str, str], str | None] | None = None,
                       head_fn: Callable[[Path], str | None] | None = None,
                       transport_factory: Callable[[list[str]], AppServerTransport] | None = None):
    """The JobController `adapter_factory` for Codex lanes: one process-group-isolated app-server per lane attempt,
    carrying the lane's full v2 identity so late frames from a fenced attempt are dropped by `lease_check`.
    `model_for(job, lane)` supplies the requested model (the lane's frozen assignment) so a native-model mismatch
    fails the start instead of silently running something else."""
    cmd = list(argv or ["codex", "app-server"])
    make = transport_factory or (lambda a: StdioAppServerTransport(a))

    def factory(*, job_id, lane_id, task_id, attempt_no, epoch, lease_check=None):
        return CodexAppServerAdapter(make(cmd), model=model_for(job_id, lane_id) if model_for else model, job_id=job_id, task=task_id, lane=lane_id,
                                     attempt=attempt_no, epoch=epoch, lease_check=lease_check, head_fn=head_fn)

    return factory


PROBE_LONG_BRIEF = "Count slowly from 1 to 500, one number per line, without using any tools."
# The brief of every probe that is not about permissions. A bare "probe" let a drifted model (Muse Code 1.4.2,
# 2026-10-04) explore with `ls`/`git status` and wait on that approval forever, so no typed result ever arrived.
PROBE_BRIEF = "Reply with exactly OK. Do not use any tools."


PROBE_APPROVAL_BRIEF = "Run the shell command `echo steward-probe` and wait for approval."
CANCELLED_STATUSES = frozenset({"interrupted", "cancelled"})


def codex_capability_probes(make_adapter: Callable[[], "CodexAppServerAdapter"], worktree: Path, *,
                            wait: Callable[[], None] = lambda: None, polls: int = 20,
                            approval_brief: str = PROBE_APPROVAL_BRIEF,
                            long_brief: str = PROBE_LONG_BRIEF,
                            ) -> dict[str, Callable[[], tuple[bool, str]]]:
    """The Codex probe suite: the shared structured-lifecycle probes (`structured_capability_probes`)."""
    return structured_capability_probes(make_adapter, worktree, wait=wait, polls=polls, approval_brief=approval_brief,
                                        long_brief=long_brief)


def structured_capability_probes(make_adapter: Callable[[], object], worktree: Path, *,
                                 wait: Callable[[], None] = lambda: None, polls: int = 20,
                                 approval_brief: str = PROBE_APPROVAL_BRIEF,
                                 long_brief: str = PROBE_LONG_BRIEF,
                                 cancelled_statuses=CANCELLED_STATUSES,
                                 ) -> dict[str, Callable[[], tuple[bool, str]]]:
    """One probe per runtime capability, for any structured adapter (start/observe/deliver/interrupt/stop/attach/
    answer_prompt/usage, a `transport` with `alive()`, `turn_id`, `process_identity()`). Each builds a FRESH adapter,
    exercises the real behavior and returns (observed, evidence); a probe that cannot demonstrate the behavior
    returns False. Qualification is only ever recorded from these results (see `fleet_runtime.qualify_runtime`),
    never from installation or roster presence. `wait` is called between polls (a real run passes a short sleep).
    Every probe except permission_relay sends the no-tools `PROBE_BRIEF`; a permission request seen while such a
    probe waits is DENIED (never approved) and named in its evidence, so the turn can still end."""
    denials: dict[int, list[str]] = {}

    def _started(brief=PROBE_BRIEF):
        a = make_adapter()
        ref = a.start(brief, worktree)
        return a, ref

    def _events(a, pred, deny=True):
        seen = []
        for _ in range(polls):
            new = list(a.observe().events)
            seen += new
            hit = [e for e in seen if pred(e)]
            if hit:
                return hit, seen
            for e in new:
                if deny and e.type == "permission_request":
                    r = a.answer_prompt(False, e.body.get("request_id"))
                    denials.setdefault(id(a), []).append(
                        f"{e.body.get('tool')} {json.dumps(e.body.get('input'), sort_keys=True)[:120]}: {r.detail}")
            wait()
        return [], seen

    def _denied(a):
        d = denials.get(id(a))
        return f"; denied {len(d)} unrequested permission request(s): {' | '.join(d)}" if d else ""

    def launch():
        a, ref = _started()
        return bool(ref and a.turn_id), f"started {ref} turn {a.turn_id}"

    def identity():
        a, ref = _started()
        ident = a.process_identity()
        conv = ident.get("thread_id") or ident.get("session_id") or ident.get("conversation_id")
        ok = bool(conv and ident.get("native_model") and ident.get("pid") is not None)
        return ok, f"session={conv} model={ident.get('native_model')} pid={ident.get('pid')}"

    def poll():
        a, _ = _started()
        o = a.observe()
        return o.lifecycle in ("working", "idle") and o.pid_alive is True, f"lifecycle={o.lifecycle}"

    def steer_ack():
        a, _ = _started()
        r = a.deliver("[rev=1] probe steer")
        hit, _ = _events(a, lambda e: e.type == "ack" and e.body.get("rev") == 1)
        return r.ok and bool(hit), f"deliver={r.detail} acks={len(hit)}{_denied(a)}"

    def permission_relay():
        a, _ = _started(approval_brief)
        hit, _ = _events(a, lambda e: e.type == "permission_request", deny=False)
        if not hit:
            return False, "no approval request observed"
        r = a.answer_prompt(False, hit[0].body["request_id"])
        return r.ok, f"relayed {hit[0].body.get('tool')}: {r.detail}"

    def typed_result():
        a, _ = _started()
        hit, _ = _events(a, lambda e: e.type in ("done", "error"))
        return bool(hit), f"typed frames {[e.type for e in hit]}{_denied(a)}"

    def cancel():
        a, _ = _started(long_brief)  # a turn that is still running when the cancel lands
        a.interrupt()
        hit, _ = _events(a, lambda e: e.type == "error" and e.body.get("status") in cancelled_statuses)
        return bool(hit), "typed interrupted result observed" if hit else "no interrupted result"

    def stop_confirm():
        a, _ = _started()
        ok = a.stop()
        return bool(ok) and not a.transport.alive(), f"terminate_tree={ok} alive={a.transport.alive()}"

    def reattach():
        a, ref = _started()
        a.stop()  # the server refuses a resume while another live process is still the thread's writer
        b = make_adapter()
        ok = b.attach(ref)
        return ok, f"attach {ref}: {ok}"

    def usage():
        a, _ = _started()
        _events(a, lambda e: e.type == "done")
        u = a.usage()
        return bool(u and u.get("tokens") is not None), f"usage={u}{_denied(a)}"

    return {"launch": launch, "identity": identity, "poll": poll, "steer_ack": steer_ack,
            "permission_relay": permission_relay, "typed_result": typed_result, "cancel": cancel,
            "stop_confirm": stop_confirm, "reattach": reattach, "usage": usage}
