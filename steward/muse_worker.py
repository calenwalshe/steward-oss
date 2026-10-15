"""Structured Muse (Meta) lane worker over `muse serve` - the MSP JSON-RPC session host (Phase 30, FLEET-24).

Verified against muse 1.4.2 (PRD §6, live traces 2026-10-04): every message carries `"jsonrpc": "2.0"`; every command
carries a client-minted UUIDv7 `commandId`; `session/start` must select approvalMode `promptUnmatched` (the only mode
that both runs headless and still asks); approvals arrive as the server REQUEST `approval/request`, which is answered
at once with an empty receipt `{}` and decided separately with `approval/decide` (approvalId + choiceId +
requirementId). The deny choice muse offers is `abort` (decision "abort"); there is not always a "denied" choice.

The adapter never answers a permission itself: each request becomes the SAME `permission_request` frame the Claude
fabric and Codex emit (tool + input in the Claude tool vocabulary), so one authority policy decides. Approval is only
ever the exact once-scoped `approved` choice - never a session-wide or policy-amending choice. `META_API_KEY` never
reaches the child (an exported key overrides the subscription and the server answers HTTP 402).
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Callable

from steward.fabric import Frame
from steward.store import now_iso
from steward.worker import (AppServerError, AppServerTransport, DeliveryResult, StdioAppServerTransport,
                            WorkerObservation, structured_capability_probes)

MUSE_APPROVAL_MODE = "promptUnmatched"
MUSE_DEFAULT_MODEL = "muse-spark-1.3-contributor"
STRIPPED_ENV = ("META_API_KEY",)


def uuid7(now_ms: int | None = None) -> str:
    """A RFC 9562 UUIDv7 (48-bit unix-ms timestamp, version 7, variant 10) - muse rejects any other commandId."""
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))


def is_uuid7(text) -> bool:
    try:
        u = uuid.UUID(str(text))
    except ValueError:
        return False
    return u.version == 7 and u.variant == uuid.RFC_4122


def muse_env(base: dict | None = None) -> dict:
    """The child environment: the caller's, minus META_API_KEY (and anything else in STRIPPED_ENV)."""
    env = dict(os.environ if base is None else base)
    for k in STRIPPED_ENV:
        env.pop(k, None)
    return env


def muse_transport(argv: list[str] | None = None, **kw) -> StdioAppServerTransport:
    return StdioAppServerTransport(list(argv or ["muse", "serve"]), jsonrpc=True, env=muse_env(), **kw)


def _permission_view(subject: dict, params: dict) -> tuple[str, dict] | None:
    """Express a muse approval subject in the Claude tool vocabulary `steward.permissions.decide` judges.
    None = not expressible (the adapter then denies; it never guesses consent)."""
    kind = str(subject.get("kind") or "")
    if kind == "shell" or subject.get("command"):
        return "Bash", {"command": str(subject.get("command") or "")}
    if subject.get("path"):
        return "Edit", {"file_path": str(subject["path"])}
    if subject.get("host") or kind in ("network", "net"):
        return "WebFetch", {"url": str(subject.get("host") or subject.get("target") or "*")}
    return None


class MuseServeAdapter:
    """`WorkerAdapter` over `muse serve` (MSP). session/start (requested vs native model checked) + turn/start launch;
    `turn/steer(expectedTurnId)` is the acknowledged steer; `approval/request` -> shared `permission_request`;
    `turn/completed` terminal completed -> done, failed/cancelled -> error; `turn/interrupt` cancels (re-sent until the
    turn ends: an interrupt that lands before the run exists is rejected `missing_run`); stop = interrupt + confirmed
    process-group kill; attach = fresh process + `session/resume`. Tokens come from `session/tokenUsage.cumulative`;
    cost only when the server reports a complete (non-partial) USD figure."""

    runtime = "muse"
    adapter_version = "1"
    kind = "muse-structured"

    def __init__(self, transport: AppServerTransport | None = None, *, model: str | None = None,
                 job_id: str = "", task: str | None = None, lane: str | None = None, attempt: int | None = None,
                 epoch: int | None = None, sender: str | None = None, lease_check: Callable[[Frame], bool] | None = None,
                 head_fn: Callable[[Path], str | None] | None = None):
        if transport is None:
            raise NotImplementedError("MuseServeAdapter needs a transport (muse_transport() or a test fake)")
        self.transport, self.model = transport, model
        self.job, self.task, self.lane, self.attempt, self.epoch = job_id, task, lane, attempt, epoch
        self.sender = sender or f"muse:{lane}"
        self.lease_check, self.head_fn = lease_check, head_fn
        self.session_id: str | None = None
        self.native_model: str | None = None
        self.turn_id: str | None = None
        self.worktree: Path | None = None
        self._base_head: str | None = None
        self._seq = 0
        self._held: list[Frame] = []
        self._perm: dict[str, dict] = {}   # approvalId -> approval/request params
        # approvalId -> (verdict, judged subject): a compound shell command is ONE approval with one stage per part;
        # muse asks again per stage (terminal:false). Steward's policy judged the whole command, so later stages of
        # the same subject take the same verdict without a second relay (live: fleet proof 3d0441cf).
        self._decided: dict[str, tuple[bool, str]] = {}
        self._decided_reqs: set[tuple[str, object]] = set()   # (approvalId, sourceIndex) already decided
        self._usage: dict | None = None
        self._stopped = False
        self._initialized = False
        self._interrupt_turn: str | None = None
        self.stale_dropped = 0
        self.refused_user_input = 0

    # -- frames ---------------------------------------------------------------------------------------------------
    def _frame(self, type_: str, body: dict) -> Frame:
        self._seq += 1
        if self.lane is None:
            return Frame(v=1, seq=self._seq, job=self.job, type=type_, body=body)
        return Frame(v=2, seq=self._seq, job=self.job, type=type_, body=body, task=self.task, lane=self.lane,
                     attempt=self.attempt, sender=self.sender, epoch=self.epoch)

    # -- lifecycle --------------------------------------------------------------------------------------------------
    def _connect(self) -> None:
        starter = getattr(self.transport, "start", None)
        if callable(starter) and not self.transport.alive():
            starter()
        if not self._initialized:
            self.transport.request("initialize", {"clientInfo": {"name": "steward", "version": "1"}})
            self.transport.notify("initialized")
            self._initialized = True

    def start(self, brief: str, worktree: Path) -> str:
        self.worktree = Path(worktree)
        self._connect()
        params = {"commandId": uuid7(), "workspaceRoot": str(self.worktree.resolve()),
                  "approvalMode": MUSE_APPROVAL_MODE}
        if self.model:
            params["modelId"] = self.model
        res = self.transport.request("session/start", params)
        session = res.get("session") or {}
        self.session_id, self.native_model = session.get("sessionId"), session.get("modelId")
        if not self.session_id:
            raise AppServerError("session/start returned no session id")
        if self.model and str(self.native_model or "").lower() != self.model.lower():
            self.stop()
            raise AppServerError(f"model_identity_mismatch: requested {self.model!r}, got {self.native_model!r}")
        if self.head_fn is not None:
            self._base_head = self.head_fn(self.worktree)
        self._start_turn(brief)
        return f"muse:{self.session_id}"

    def _start_turn(self, text: str) -> None:
        res = self.transport.request("turn/start", {"commandId": uuid7(), "sessionId": self.session_id,
                                                    "input": [{"type": "text", "text": text}]})
        self.turn_id = res.get("turnId")

    def process_identity(self) -> dict:
        return {"session_id": self.session_id, "native_model": self.native_model, "requested_model": self.model,
                "pid": self.transport.pid()}

    def attach(self, ref) -> bool:
        sid = str(ref).removeprefix("muse:")
        try:
            self._connect()
            res = self.transport.request("session/resume", {"commandId": uuid7(), "sessionId": sid,
                                                            "excludeItems": True})
        except AppServerError:
            return False
        session = res.get("session") or {}
        if session.get("sessionId") != sid:
            return False
        self.session_id = sid
        self.native_model = session.get("modelId") or self.native_model
        self.turn_id = session.get("activeTurnId")
        return True

    def deliver(self, text: str, *, urgent: bool = False) -> DeliveryResult:
        if self.session_id is None:
            return DeliveryResult(ok=False, detail="not_started")
        rev = re.search(r"rev=(\d+)", text)
        self._pump()  # a turn that already completed is not steerable: the text starts the next turn instead
        try:
            if self.turn_id is None:
                self._start_turn(text)
                got = self.turn_id
            else:
                res = self.transport.request("turn/steer", {"commandId": uuid7(), "sessionId": self.session_id,
                                                            "expectedTurnId": self.turn_id,
                                                            "input": [{"type": "text", "text": text}]})
                got = res.get("turnId")
        except AppServerError as exc:
            return DeliveryResult(ok=False, detail=f"steer_failed: {exc}"[:200])
        if not got or got != self.turn_id:
            return DeliveryResult(ok=False, detail="steer_not_acknowledged")
        if rev:
            self._held.append(self._frame("ack", {"rev": int(rev.group(1)), "stage": "acknowledged",
                                                  "via": "turn/steer"}))
        return DeliveryResult(ok=True, detail="delivered")

    def interrupt(self) -> None:
        if self.session_id and self.turn_id:
            self._interrupt_turn = self.turn_id
            self._send_interrupt()

    def _send_interrupt(self) -> None:
        if self.session_id and self.turn_id:
            try:
                self.transport.request("turn/interrupt", {"commandId": uuid7(), "sessionId": self.session_id,
                                                          "turnId": self.turn_id})
            except AppServerError:
                pass  # e.g. `missing_run`: the turn is accepted but not running yet - `observe` re-sends

    def stop(self) -> bool:
        self.interrupt()
        self._stopped = True
        return bool(self.transport.terminate_tree())

    def answer_prompt(self, approve: bool, request_id: str | None = None, message: str | None = None) -> DeliveryResult:
        if request_id is None and len(self._perm) == 1:
            request_id = next(iter(self._perm))
        req = self._perm.pop(str(request_id), None) if request_id is not None else None
        if req is None:
            return DeliveryResult(ok=False, detail="unknown_request")
        choice = self._choice(req, approve)
        if choice is None:
            return DeliveryResult(ok=False, detail="no_matching_choice")
        params = {"commandId": uuid7(), "sessionId": self.session_id, "approvalId": req["approvalId"],
                  "choiceId": choice["choiceId"], "requirementId": req.get("currentRequirementId")}
        if not approve and message and choice.get("acceptsFeedback"):
            params["feedback"] = message[:500]
        try:
            self.transport.request("approval/decide", params)
        except AppServerError as exc:
            return DeliveryResult(ok=False, detail=f"decide_failed: {exc}"[:200])
        self._decided[str(req["approvalId"])] = (bool(approve), self._judged(req))
        self._decided_reqs.add(self._req_key(req))
        return DeliveryResult(ok=True, detail="approved" if approve else "denied")

    @staticmethod
    def _req_key(params: dict) -> tuple[str, object]:
        ref = params.get("currentRequirementId") or {}
        return str(params.get("approvalId")), ref.get("sourceIndex") if isinstance(ref, dict) else ref

    @staticmethod
    def _judged(params: dict) -> str:
        view = _permission_view(params.get("subject") or {}, params)
        return json.dumps(view, sort_keys=True) if view else ""

    def _next_stage(self, params: dict, events: list) -> None:
        """A further stage of an approval (updated request or `approval/updated`). The same subject Steward already
        judged gets the same verdict; anything else is relayed as a new permission request."""
        aid = str(params.get("approvalId"))
        if self._req_key(params) in self._decided_reqs:
            return   # an update about a stage already decided (e.g. its resolution): nothing to answer
        prior = self._decided.get(aid)
        if prior is not None and prior[1] and prior[1] == self._judged(params):
            self._perm[aid] = params
            self.answer_prompt(prior[0], aid)
            return
        if prior is None and aid in self._perm:   # still pending with Steward: keep the newest stage to decide
            self._perm[aid] = {**self._perm[aid], **params}
            return
        view = _permission_view(params.get("subject") or {}, params)
        self._perm[aid] = params
        if view is None:
            self.answer_prompt(False, aid)
            return
        events.append(self._frame("permission_request", {"request_id": aid, "tool": view[0], "input": view[1]}))

    @staticmethod
    def _choice(req: dict, approve: bool) -> dict | None:
        choices = [c for c in req.get("availableChoices") or [] if isinstance(c, dict)]
        if approve:  # once only: never a session-wide or policy-amending approval
            return next((c for c in choices if c.get("decision") == "approved"
                         and c.get("scope", "once") == "once"), None)
        for want in ("denied", "abort"):
            hit = next((c for c in choices if c.get("decision") == want), None)
            if hit is not None:
                return hit
        return None

    def usage(self) -> dict | None:
        cum = self._usage or {}
        total = cum.get("totalTokens")
        if not isinstance(total, int) or isinstance(total, bool):
            return None
        cost = cum.get("cost") if isinstance(cum.get("cost"), dict) else {}
        usd = cost.get("usd") if not cost.get("partial") else None
        return {"tokens": total, "cost_usd": usd if isinstance(usd, (int, float)) else None,
                "source": "muse_msp", "confidence": "reported"}

    # -- observation ------------------------------------------------------------------------------------------------
    def _current(self, frame: Frame) -> bool:
        if self.lease_check is not None and not self.lease_check(frame):
            self.stale_dropped += 1
            return False
        return True

    def _server_request(self, m: dict, events: list) -> None:
        method, params = m.get("method"), m.get("params") or {}
        if method == "approval/request" and str(params.get("approvalId")) in self._decided:
            self.transport.respond(m["id"], {})
            self._next_stage(params, events)
        elif method == "approval/request":
            self.transport.respond(m["id"], {})   # the receipt; the decision travels separately
            view = _permission_view(params.get("subject") or {}, params)
            rid = str(params.get("approvalId"))
            self._perm[rid] = params
            if view is None:
                self.answer_prompt(False, rid)   # not expressible in the shared policy: refuse, never guess
                return
            events.append(self._frame("permission_request", {"request_id": rid, "tool": view[0], "input": view[1]}))
        elif method == "userInput/request":
            # a lane cannot put questions to the owner mid-run; instructions reach it only through Steward steers
            self.transport.respond(m["id"], {})
            self.refused_user_input += 1
            try:
                self.transport.request("userInput/cancel", {"commandId": uuid7(), "sessionId": self.session_id,
                                                            "userInputId": params.get("userInputId"),
                                                            "reason": "steward lanes take instructions via steering"})
            except AppServerError:
                pass
        else:
            self.transport.respond(m["id"], error={"code": -32601, "message": "unsupported"})

    def _pump(self) -> None:
        """Process every server message received so far, in order, into held frames (turn state stays current)."""
        events = self._held
        for m in self.transport.drain():
            method, params = m.get("method"), m.get("params") or {}
            if "id" in m and method:
                self._server_request(m, events)
            elif method == "approval/updated":
                self._next_stage(params, events)
            elif method == "turn/started":
                self.turn_id = params.get("turnId") or self.turn_id
            elif method == "session/tokenUsage":
                if isinstance(params.get("cumulative"), dict):
                    self._usage = params["cumulative"]
            elif method == "turn/completed":
                terminal, tid = params.get("terminal"), params.get("turnId")
                if tid == self.turn_id or self.turn_id is None:
                    self.turn_id = None
                    self._interrupt_turn = None
                if terminal == "completed":
                    body = {"turn_id": tid, "status": terminal, "evidence_ref": f"muse:{self.session_id}:{tid}"}
                    head = self.head_fn(self.worktree) if self.head_fn and self.worktree else None
                    if head and head != self._base_head:
                        body["candidate_sha"] = head
                    events.append(self._frame("done", body))
                else:
                    err = params.get("error") if isinstance(params.get("error"), dict) else {}
                    events.append(self._frame("error", {"turn_id": tid, "status": terminal,
                                                        "detail": str(err.get("message") or params.get("reason")
                                                                      or terminal)[:300]}))

    def observe(self) -> WorkerObservation:
        self._pump()
        events = self._held
        self._held = []
        if self._interrupt_turn and self._interrupt_turn == self.turn_id and not self._stopped:
            self._send_interrupt()
        kept = tuple(e for e in events if self._current(e))
        alive = self.transport.alive()
        life = "exited" if not alive else ("working" if self.turn_id else "idle")
        return WorkerObservation(lifecycle=life, output_tail="", pid_alive=alive, ts=now_iso(), events=kept)


def muse_lane_factory(*, model: str | None = None, model_for: Callable[[str, str], str | None] | None = None,
                      head_fn: Callable[[Path], str | None] | None = None,
                      transport_factory: Callable[[], AppServerTransport] | None = None):
    """JobController `adapter_factory` for Muse lanes: one process-group-isolated `muse serve` per lane attempt."""
    make = transport_factory or muse_transport

    def factory(*, job_id, lane_id, task_id, attempt_no, epoch, lease_check=None):
        return MuseServeAdapter(make(), model=model_for(job_id, lane_id) if model_for else model, job_id=job_id,
                                task=task_id, lane=lane_id, attempt=attempt_no, epoch=epoch, lease_check=lease_check,
                                head_fn=head_fn)

    return factory


def muse_capability_probes(make_adapter, worktree: Path, **kw):
    """The Muse probe suite: the shared structured-lifecycle probes; a cancelled turn ends `cancelled`."""
    return structured_capability_probes(make_adapter, worktree, **kw)
