"""Structured pi lane worker: DeepSeek V4 Pro via the deepinfra provider in `pi --mode rpc` (Phase 30, FLEET-26, D-04).

Protocol per pi's docs/rpc.md + docs/rpc-commands.md (verified live 2026-10-04): JSONL commands on stdin carrying an
optional `id` that the matching `{"type": "response", ...}` repeats; session events (`agent_start` ... `agent_settled`)
on stdout; `prompt` starts a run, `steer` queues an instruction into a running one (an idle session only queues it,
so an idle deliver is a `prompt`), `abort` ends a run (its assistant message stops with `aborted`), `get_state` names
the exact model/provider/session, `get_session_stats` reports tokens and cost.

pi itself never asks before a tool call (docs/security.md). So that a lane's tool calls are judged by Steward's ONE
authority policy, every pi lane loads a Steward-owned gate extension (written beside the lane's session, never into
the worktree): each non-read-only tool call blocks on an RPC `confirm` dialog (`extension_ui_request`) which this
adapter relays as the shared `permission_request` frame and answers with `extension_ui_response`; a denial blocks the
tool. Other installed extensions are not loaded (`--no-extensions`), so nothing can sit in front of the gate.

The only model Steward ever asks pi for is `deepseek-ai/DeepSeek-V4-Pro` on `deepinfra`. `~/.pi/agent/models.json` is
JSONC owned by the operator: Steward never reads, parses or writes it.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path
from typing import Callable

from steward.fabric import Frame
from steward.store import now_iso
from steward.worker import PROBE_BRIEF, DeliveryResult, NdjsonProcess, WorkerObservation, structured_capability_probes

PI_PROVIDER = "deepinfra"
PI_MODEL = "deepseek-ai/DeepSeek-V4-Pro"
GATE_TITLE = "steward-permission"
READ_ONLY_TOOLS = ("read", "grep", "find", "ls")
GATE_EXTENSION = """// Steward permission gate for pi lanes (generated; do not edit). Every tool call that is not read-only blocks on
// Steward's authority policy through the RPC confirm dialog; anything but an explicit yes blocks the tool.
const READ_ONLY = new Set(%s);
export default function (pi: any) {
  pi.on("tool_call", async (event: any, ctx: any) => {
    if (READ_ONLY.has(event.toolName)) return undefined;
    const ok = await ctx.ui.confirm(%s, JSON.stringify({ tool: event.toolName, input: event.input }));
    if (ok !== true) return { block: true, reason: "Denied by Steward's authority policy" };
    return undefined;
  });
}
""" % (json.dumps(list(READ_ONLY_TOOLS)), json.dumps(GATE_TITLE))


class PiError(RuntimeError):
    pass


def write_gate(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "steward-gate.ts"
    path.write_text(GATE_EXTENSION)
    return path


def pi_argv(*, session_dir: str, gate: str, model: str = PI_MODEL, session: str | None = None) -> list[str]:
    argv = ["pi", "--mode", "rpc", "--provider", PI_PROVIDER, "--model", model, "--session-dir", session_dir,
            "--no-approve", "--no-extensions", "-e", gate]
    if session:
        argv += ["--session", session]
    return argv


def _permission_view(tool: str, args: dict) -> tuple[str, dict] | None:
    if tool == "bash":
        return "Bash", {"command": str(args.get("command") or "")}
    path = args.get("path") or args.get("file_path")
    if tool == "edit" and path:
        return "Edit", {"file_path": str(path)}
    if tool == "write" and path:
        return "Write", {"file_path": str(path), "content": str(args.get("content") or "")[:2000]}
    return None


class PiRpcAdapter:
    runtime = "pi"
    adapter_version = "1"
    kind = "pi-structured"

    def __init__(self, make_process: Callable[[list[str], str | None], NdjsonProcess] | None = None, *,
                 model: str | None = None, session_dir: str | Path | None = None, job_id: str = "",
                 task: str | None = None, lane: str | None = None, attempt: int | None = None,
                 epoch: int | None = None, sender: str | None = None, lease_check: Callable[[Frame], bool] | None = None,
                 head_fn: Callable[[Path], str | None] | None = None, request_timeout_s: float = 60.0):
        if model is not None and model != PI_MODEL:
            raise PiError(f"off_roster_model: pi lanes run only {PI_MODEL!r}, not {model!r}")
        self.make_process = make_process or (lambda argv, cwd: NdjsonProcess(argv, cwd=cwd))
        self.model = model or PI_MODEL
        self.session_dir = Path(session_dir) if session_dir else None
        self.job, self.task, self.lane, self.attempt, self.epoch = job_id, task, lane, attempt, epoch
        self.sender = sender or f"pi:{lane}"
        self.lease_check, self.head_fn = lease_check, head_fn
        self.request_timeout_s = request_timeout_s
        self.transport: NdjsonProcess | None = None
        self.session_id: str | None = None
        self.native_model: str | None = None
        self.native_provider: str | None = None
        self.turn_id: str | None = None
        self.worktree: Path | None = None
        self._base_head: str | None = None
        self._seq = 0
        self._rid = 0
        self._turns = 0
        self._running = False
        self._last_stop: dict = {}
        self._held_raw: list[dict] = []
        self._held: list[Frame] = []
        self._perm: dict[str, dict] = {}
        self._stats: dict | None = None
        self._streamed = {"tokens": 0, "cost": 0.0, "seen": False}   # per assistant message, as the run goes
        self._stopped = False
        self._aborting = False
        self.stale_dropped = 0

    # -- frames ---------------------------------------------------------------------------------------------------
    def _frame(self, type_: str, body: dict) -> Frame:
        self._seq += 1
        if self.lane is None:
            return Frame(v=1, seq=self._seq, job=self.job, type=type_, body=body)
        return Frame(v=2, seq=self._seq, job=self.job, type=type_, body=body, task=self.task, lane=self.lane,
                     attempt=self.attempt, sender=self.sender, epoch=self.epoch)

    # -- rpc --------------------------------------------------------------------------------------------------------
    def _request(self, type_: str, **fields) -> dict:
        self._rid += 1
        rid = f"st{self._rid}"
        if not self.transport.send({"id": rid, "type": type_, **fields}):
            raise PiError(f"{type_}: pi is not running")
        resp = self.transport.wait_for(lambda m: m.get("type") == "response" and m.get("id") == rid,
                                       self.request_timeout_s, self._held_raw)
        if resp is None:
            raise PiError(f"{type_}: no response")
        self._held_raw.remove(resp)
        if not resp.get("success"):
            raise PiError(f"{type_}: {resp.get('error')}")
        return resp.get("data") or {}

    def _spawn(self, session: str | None = None) -> None:
        sdir = self.session_dir or Path(tempfile.mkdtemp(prefix="steward-pi-"))
        self.session_dir = sdir
        gate = write_gate(sdir / ".steward")
        argv = pi_argv(session_dir=str(sdir), gate=str(gate), model=self.model, session=session)
        self.transport = self.make_process(argv, str(self.worktree) if self.worktree else None)
        self.transport.start()

    def _read_state(self) -> dict:
        state = self._request("get_state")
        model = state.get("model") or {}
        self.native_model, self.native_provider = model.get("id"), model.get("provider")
        self.session_id = state.get("sessionId") or self.session_id
        return state

    def start(self, brief: str, worktree: Path) -> str:
        self.worktree = Path(worktree)
        self._spawn()
        if self.head_fn is not None:
            self._base_head = self.head_fn(self.worktree)
        try:
            self._read_state()
        except PiError:
            self.stop()
            raise
        if self.native_model != self.model or self.native_provider != PI_PROVIDER:
            self.stop()
            raise PiError(f"model_identity_mismatch: requested {PI_PROVIDER}/{self.model}, "
                          f"got {self.native_provider}/{self.native_model}")
        if not self.session_id:
            self.stop()
            raise PiError("get_state returned no session id")
        self._prompt(brief)
        return f"pi:{self.session_id}"

    def _prompt(self, text: str) -> None:
        self._request("prompt", message=text)
        self._turns += 1
        self.turn_id = f"t{self._turns}"
        self._running = True

    def process_identity(self) -> dict:
        return {"session_id": self.session_id, "native_model": self.native_model, "provider": self.native_provider,
                "requested_model": self.model, "pid": self.transport.pid() if self.transport else None}

    def attach(self, ref, worktree: Path | None = None) -> bool:
        sid = str(ref).removeprefix("pi:")
        if worktree is not None:
            self.worktree = Path(worktree)
        try:
            self._spawn(session=sid)
            self._read_state()
        except PiError:
            if self.transport:
                self.transport.terminate_tree()
            return False
        return self.session_id == sid and self.native_model == self.model

    def deliver(self, text: str, *, urgent: bool = False) -> DeliveryResult:
        if self.session_id is None:
            return DeliveryResult(ok=False, detail="not_started")
        self._pump()
        rev = re.search(r"rev=(\d+)", text)
        try:
            if self._running:
                self._request("steer", message=text)
                via = "steer"
            else:
                self._prompt(text)
                via = "prompt"
        except PiError as exc:
            return DeliveryResult(ok=False, detail=f"steer_failed: {exc}"[:200])
        if rev:
            self._held.append(self._frame("ack", {"rev": int(rev.group(1)), "stage": "acknowledged", "via": via}))
        return DeliveryResult(ok=True, detail="delivered")

    def interrupt(self) -> None:
        if self._running and self.transport and self.transport.alive():
            self._aborting = True
            self._rid += 1
            self.transport.send({"id": f"st{self._rid}", "type": "abort"})

    def stop(self) -> bool:
        self.interrupt()
        self._stopped = True
        return bool(self.transport.terminate_tree()) if self.transport else True

    def answer_prompt(self, approve: bool, request_id: str | None = None, message: str | None = None) -> DeliveryResult:
        if request_id is None and len(self._perm) == 1:
            request_id = next(iter(self._perm))
        req = self._perm.pop(str(request_id), None) if request_id is not None else None
        if req is None:
            return DeliveryResult(ok=False, detail="unknown_request")
        if not self.transport.send({"type": "extension_ui_response", "id": req["id"], "confirmed": bool(approve)}):
            return DeliveryResult(ok=False, detail="send_failed")
        return DeliveryResult(ok=True, detail="approved" if approve else "denied")

    def usage(self) -> dict | None:
        """Session totals from `get_session_stats` while pi is up; after a stop (the controller records usage
        then - live finding, fleet proof d2cf45e8) the per-message usage pi streamed during the run."""
        if self.transport and self.transport.alive() and not self._running:
            try:
                self._stats = self._request("get_session_stats")
            except PiError:
                pass
        stats = self._stats or {}
        total = (stats.get("tokens") or {}).get("total")
        if isinstance(total, int) and not isinstance(total, bool) and total >= self._streamed["tokens"]:
            cost = stats.get("cost")
            return {"tokens": total, "cost_usd": float(cost) if isinstance(cost, (int, float))
                    and not isinstance(cost, bool) else None, "source": "pi_session_stats", "confidence": "reported"}
        if not self._streamed["seen"]:
            return None
        return {"tokens": self._streamed["tokens"], "cost_usd": self._streamed["cost"], "source": "pi_message_usage",
                "confidence": "reported"}

    # -- observation ------------------------------------------------------------------------------------------------
    def _current(self, frame: Frame) -> bool:
        if self.lease_check is not None and not self.lease_check(frame):
            self.stale_dropped += 1
            return False
        return True

    def _on(self, m: dict, events: list) -> None:
        t = m.get("type")
        if t == "extension_ui_request":
            method, rid = m.get("method"), str(m.get("id"))
            if method == "confirm" and m.get("title") == GATE_TITLE:
                try:
                    req = json.loads(m.get("message") or "{}")
                except ValueError:
                    req = {}
                view = _permission_view(str(req.get("tool") or ""), req.get("input") or {})
                self._perm[rid] = m
                if view is None:  # not expressible in the shared policy: deny, never guess consent
                    self.answer_prompt(False, rid)
                    return
                events.append(self._frame("permission_request", {"request_id": rid, "tool": view[0],
                                                                 "input": view[1]}))
            elif method in ("confirm", "select", "input", "editor"):  # nobody else may hold a lane open
                self.transport.send({"type": "extension_ui_response", "id": m.get("id"), "cancelled": True})
        elif t == "agent_start":
            self._running = True
            self._last_stop = {}
        elif t == "message_end":
            msg = m.get("message") or {}
            if msg.get("role") == "assistant":
                self._last_stop = {"stopReason": msg.get("stopReason"), "errorMessage": msg.get("errorMessage")}
                u = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
                tok, cost = u.get("totalTokens"), (u.get("cost") or {}).get("total") if isinstance(
                    u.get("cost"), dict) else None
                if isinstance(tok, int) and not isinstance(tok, bool):
                    self._streamed["tokens"] += tok
                    self._streamed["seen"] = True
                    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                        self._streamed["cost"] += float(cost)
        elif t == "agent_settled":
            if self._stopped and not self._aborting:
                return
            tid, self.turn_id, self._running = self.turn_id, None, False
            stop = self._last_stop.get("stopReason")
            if stop == "aborted" or self._aborting:
                self._aborting = False
                events.append(self._frame("error", {"turn_id": tid, "status": "interrupted",
                                                    "detail": "aborted"}))
            elif stop == "error":
                events.append(self._frame("error", {"turn_id": tid, "status": "failed",
                                                    "detail": str(self._last_stop.get("errorMessage") or "error")[:300]}))
            else:
                body = {"turn_id": tid, "status": "completed", "evidence_ref": f"pi:{self.session_id}:{tid}"}
                head = self.head_fn(self.worktree) if self.head_fn and self.worktree else None
                if head and head != self._base_head:
                    body["candidate_sha"] = head
                events.append(self._frame("done", body))

    def _pump(self) -> None:
        raw = self._held_raw + (self.transport.drain() if self.transport else [])
        self._held_raw = []
        for m in raw:
            self._on(m, self._held)

    def observe(self) -> WorkerObservation:
        self._pump()
        events, self._held = self._held, []
        kept = tuple(e for e in events if self._current(e))
        alive = bool(self.transport and self.transport.alive())
        life = "exited" if not alive else ("working" if self._running else "idle")
        return WorkerObservation(lifecycle=life, output_tail="", pid_alive=alive, ts=now_iso(), events=kept)


def pi_lane_factory(*, session_root: Path, head_fn: Callable[[Path], str | None] | None = None, make_process=None,
                    model_for: Callable[[str, str], str | None] | None = None):
    """JobController `adapter_factory` for pi lanes: one rpc process per lane attempt, sessions under
    `session_root/<job>/<lane>` (outside the worktree)."""

    def factory(*, job_id, lane_id, task_id, attempt_no, epoch, lease_check=None):
        return PiRpcAdapter(make_process, model=model_for(job_id, lane_id) if model_for else None,
                            session_dir=Path(session_root) / job_id / lane_id, job_id=job_id, task=task_id,
                            lane=lane_id, attempt=attempt_no, epoch=epoch, lease_check=lease_check, head_fn=head_fn)
    return factory


def pi_capability_probes(make_adapter, worktree: Path, **kw):
    """The shared lifecycle probes, with pi's reattach (a fresh process opening the same session)."""
    probes = structured_capability_probes(make_adapter, worktree, **kw)

    def reattach():
        a = make_adapter()
        ref = a.start(PROBE_BRIEF, worktree)
        a.stop()
        b = make_adapter()
        ok = b.attach(ref, worktree)
        return ok, f"attach {ref} via --session: {ok}"

    probes["reattach"] = reattach
    return probes
