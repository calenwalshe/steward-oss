"""Structured Antigravity (`agy`) lane worker over stream-json print mode (Phase 30, FLEET-25).

Verified against agy 1.2.16 (PRD §6, live traces 2026-10-04):
- `agy --input-format stream-json --output-format stream-json -p=` reads one NDJSON message per stdin line and runs a
  turn for each; the only input event is `{"event": "user", "message": {"role": "user", "content": <text>}}` (a
  message without `"event"` is refused; an unknown event is ignored with a warning).
- Output opens with `{"event": "init", "conversation_id": ...}`, streams `step_update` records (a `user_input` step in
  state DONE is the runtime ingesting a message) and closes each turn with `{"event": "result", "result": {status,
  usage, denied_actions?, error?}}`; usage is cumulative for the conversation.
- SIGINT to the process group ends the running turn with a typed ERROR result (`error: interrupted`) and the process
  exits; `--conversation <id>` on a fresh process reattaches (its init arrives at once).
- The model is not in the stream; the CLI's own `--log-file` records `Propagating selected model override to
  backend: label="..."`, which is the observed native model.
- Headless mode CANNOT prompt for a tool permission: it auto-denies and lists `denied_actions` in the result
  (`--dangerously-skip-permissions` is never used). Denials are surfaced, never hidden.
- The hook gate (`steward.agy_gate`, audit "Hook-gate route") is how agy relays: the process runs with HOME set to a
  Steward-owned agy home whose broad allow rule stops the auto-deny and whose PreToolUse hook asks Steward about
  EVERY tool call over a socket; agy runs in a bubblewrap sandbox in which that config is read-only. Each becomes the shared `permission_request` frame; `answer_prompt`
  releases the blocked hook with allow or deny; anything unanswered, and everything open at `stop()`, is denied.
  Without a gate (`gate_dir=None`) the lane is the old auto-denying one and cannot relay.
Never invoke `agy --model list` (or anything needing a TTY).
"""

from __future__ import annotations

import json
import os
import re
import signal
import tempfile
import time
from pathlib import Path
from typing import Callable

from steward.agy_gate import AgyGate, gate_root
from steward.fabric import Frame
from steward.store import now_iso
from steward.worker import (PROBE_BRIEF, DeliveryResult, NdjsonProcess, WorkerObservation,
                            structured_capability_probes)

AGY_BASE_ARGV = ("agy", "--input-format", "stream-json", "--output-format", "stream-json", "-p=")
FORBIDDEN_FLAGS = ("--dangerously-skip-permissions",)
_LABEL = re.compile(r'Propagating selected model override to backend: label="([^"]+)"')


class AgyError(RuntimeError):
    pass


def agy_argv(*, model: str | None = None, effort: str | None = None, conversation: str | None = None,
             log_file: str | None = None) -> list[str]:
    argv = list(AGY_BASE_ARGV)
    if conversation:
        argv += ["--conversation", conversation]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    if log_file:
        argv += ["--log-file", log_file]
    assert not set(argv) & set(FORBIDDEN_FLAGS)
    return argv


def user_message(text: str) -> dict:
    return {"event": "user", "message": {"role": "user", "content": text}}


class AgyStreamAdapter:
    """`WorkerAdapter` over one `agy` stream-json process per conversation. `make_process(argv, cwd)` builds the
    `NdjsonProcess` (injectable: tests use a scripted fake)."""

    runtime = "agy"
    adapter_version = "2"   # 2: the hook gate relays permission requests (1 auto-denied them)
    kind = "agy-structured"

    def __init__(self, make_process: Callable[[list[str], str], NdjsonProcess] | None = None, *,
                 model: str | None = None, effort: str | None = None, job_id: str = "", task: str | None = None,
                 lane: str | None = None, attempt: int | None = None, epoch: int | None = None,
                 sender: str | None = None, lease_check: Callable[[Frame], bool] | None = None,
                 head_fn: Callable[[Path], str | None] | None = None, log_dir: str | Path | None = None,
                 start_timeout_s: float = 90.0, label_timeout_s: float = 10.0, gate_dir: str | Path | None = None,
                 gate_token_home: str | Path | None = None, gate_answer_timeout_s: float = 3600.0):
        self.make_process = make_process or (lambda argv, cwd, env=None: NdjsonProcess(argv, cwd=cwd, env=env))
        self.gate_dir = Path(gate_dir) if gate_dir else None
        self.gate_token_home, self.gate_answer_timeout_s = gate_token_home, gate_answer_timeout_s
        self.gate: AgyGate | None = None
        self.model, self.effort = model, effort
        self.job, self.task, self.lane, self.attempt, self.epoch = job_id, task, lane, attempt, epoch
        self.sender = sender or f"agy:{lane}"
        self.lease_check, self.head_fn = lease_check, head_fn
        self.log_dir = Path(log_dir) if log_dir else None
        self.start_timeout_s, self.label_timeout_s = start_timeout_s, label_timeout_s
        self.transport: NdjsonProcess | None = None
        self.conversation_id: str | None = None
        self.native_model: str | None = None
        self.turn_id: str | None = None
        self.worktree: Path | None = None
        self.log_file: Path | None = None
        self.denied_actions: list = []
        self._base_head: str | None = None
        self._seq = 0
        self._turns = 0
        self._outstanding = 0
        self._inputs_seen = 0
        self._skip_inputs = 0          # user_input steps that are not Steward deliveries (the brief)
        self._pending_revs: list[int | None] = []
        self._held_raw: list[dict] = []
        self._held: list[Frame] = []
        self._usage: dict | None = None
        self._stopped = False
        self._interrupted = False
        self.stale_dropped = 0
        self._perm: set[str] = set()

    # -- frames ---------------------------------------------------------------------------------------------------
    def _frame(self, type_: str, body: dict) -> Frame:
        self._seq += 1
        if self.lane is None:
            return Frame(v=1, seq=self._seq, job=self.job, type=type_, body=body)
        return Frame(v=2, seq=self._seq, job=self.job, type=type_, body=body, task=self.task, lane=self.lane,
                     attempt=self.attempt, sender=self.sender, epoch=self.epoch)

    # -- process ----------------------------------------------------------------------------------------------------
    def _spawn(self, conversation: str | None = None) -> None:
        base = self.log_dir or Path(tempfile.mkdtemp(prefix="steward-agy-"))
        base.mkdir(parents=True, exist_ok=True)
        self.log_file = base / f"agy-{int(time.time() * 1000)}.log"
        argv = agy_argv(model=self.model, effort=self.effort, conversation=conversation, log_file=str(self.log_file))
        cwd = str(self.worktree) if self.worktree else None
        if self.gate_dir is None:
            self.transport = self.make_process(argv, cwd)
        else:  # every tool call is asked of Steward through the gate; the config is rewritten for every process
            if self.gate is None:
                self.gate = AgyGate(self.gate_dir, token_home=self.gate_token_home,
                                    answer_timeout_s=self.gate_answer_timeout_s)
            env = {k: v for k, v in os.environ.items() if k != "META_API_KEY"}
            env.update(self.gate.materialize())
            argv = self.gate.sandbox_argv(self.worktree) + argv
            self.gate.start()
            self.transport = self.make_process(argv, cwd, env=env)
        self.transport.start()
        self._interrupted = False

    def _read_label(self) -> str | None:
        try:
            text = self.log_file.read_text(errors="replace") if self.log_file else ""
        except OSError:
            return None
        found = _LABEL.findall(text)
        return found[-1] if found else None

    def _await_label(self) -> None:
        deadline = time.monotonic() + self.label_timeout_s
        while self.native_model is None:
            self.native_model = self._read_label()
            if self.native_model or time.monotonic() >= deadline:
                return
            time.sleep(0.1)

    def _send_user(self, text: str) -> bool:
        if not self.transport.send(user_message(text)):
            return False
        self._outstanding += 1
        if self.turn_id is None:
            self._turns += 1
            self.turn_id = f"t{self._turns}"
        return True

    def start(self, brief: str, worktree: Path) -> str:
        self.worktree = Path(worktree)
        self._spawn()
        if self.head_fn is not None:
            self._base_head = self.head_fn(self.worktree)
        self._skip_inputs = 1
        if not self._send_user(brief):
            raise AgyError("agy refused the brief (process not running)")
        init = self.transport.wait_for(lambda m: m.get("event") == "init", self.start_timeout_s, self._held_raw)
        if init is None or not init.get("conversation_id"):
            self.stop()
            raise AgyError("agy printed no init record: " + " | ".join(self.transport.stderr_tail[-3:]))
        self.conversation_id = init["conversation_id"]
        self._await_label()
        if self.model and str(self.native_model or "").lower() != self.model.lower():
            self.stop()
            raise AgyError(f"model_identity_mismatch: requested {self.model!r}, got {self.native_model!r}")
        return f"agy:{self.conversation_id}"

    def process_identity(self) -> dict:
        return {"conversation_id": self.conversation_id, "native_model": self.native_model,
                "requested_model": self.model, "pid": self.transport.pid() if self.transport else None}

    def attach(self, ref, worktree: Path | None = None) -> bool:
        conv = str(ref).removeprefix("agy:")
        if worktree is not None:
            self.worktree = Path(worktree)
        self._spawn(conversation=conv)
        init = self.transport.wait_for(lambda m: m.get("event") == "init", self.start_timeout_s, self._held_raw)
        if init is None or init.get("conversation_id") != conv:
            self.transport.terminate_tree()
            return False
        self.conversation_id = conv
        self._await_label()
        return True

    def deliver(self, text: str, *, urgent: bool = False) -> DeliveryResult:
        if self.conversation_id is None:
            return DeliveryResult(ok=False, detail="not_started")
        if not self.transport.alive():  # the turn was interrupted (process exited): continue the conversation
            if not self.attach(self.conversation_id):
                return DeliveryResult(ok=False, detail="reattach_failed")
        rev = re.search(r"rev=(\d+)", text)
        if not self._send_user(text):
            return DeliveryResult(ok=False, detail="send_failed")
        self._pending_revs.append(int(rev.group(1)) if rev else None)
        return DeliveryResult(ok=True, detail="delivered")

    def interrupt(self) -> None:
        if self.turn_id and self.transport and self.transport.alive():
            self._interrupted = True
            self.transport.signal(signal.SIGINT)

    def stop(self) -> bool:
        self.interrupt()
        self._stopped = True
        gone = bool(self.transport.terminate_tree()) if self.transport else True
        if self.gate is not None:  # whatever is still waiting is denied; a later attach opens a fresh gate
            self.gate.close()
            self.gate = None
        return gone

    def answer_prompt(self, approve: bool, request_id: str | None = None, message: str | None = None) -> DeliveryResult:
        if self.gate_dir is None:  # ungated agy never asks: it auto-denies (see `denied_actions`)
            return DeliveryResult(ok=False, detail="unsupported: agy headless cannot relay permission requests")
        if self.gate is None:
            return DeliveryResult(ok=False, detail="stopped")
        if request_id is None and len(self._perm) == 1:
            request_id = next(iter(self._perm))
        if request_id is None or str(request_id) not in self._perm:
            return DeliveryResult(ok=False, detail="unknown_request")
        self._perm.discard(str(request_id))
        if not self.gate.answer(str(request_id), approve, message):
            return DeliveryResult(ok=False, detail="expired: the hook already denied it (no answer in time)")
        return DeliveryResult(ok=True, detail="approved" if approve else "denied")

    def usage(self) -> dict | None:
        total = (self._usage or {}).get("total_tokens")
        if not isinstance(total, int) or isinstance(total, bool):
            return None
        return {"tokens": total, "cost_usd": None, "source": "agy_stream_json", "confidence": "reported"}

    # -- observation ------------------------------------------------------------------------------------------------
    def _current(self, frame: Frame) -> bool:
        if self.lease_check is not None and not self.lease_check(frame):
            self.stale_dropped += 1
            return False
        return True

    def _on(self, m: dict, events: list) -> None:
        ev = m.get("event")
        if ev == "init":
            self.conversation_id = m.get("conversation_id") or self.conversation_id
        elif ev == "step_update":
            st = m.get("step_update") or {}
            if st.get("step_type") == "user_input" and st.get("state") == "DONE":
                self._inputs_seen += 1
                if self._skip_inputs:
                    self._skip_inputs -= 1
                elif self._pending_revs:
                    rev = self._pending_revs.pop(0)
                    if rev is not None:  # the runtime ingested the delivered instruction: a typed receipt
                        events.append(self._frame("ack", {"rev": rev, "stage": "acknowledged",
                                                          "via": "agy_user_input_step"}))
        elif ev == "result":
            res = m.get("result") or {}
            if isinstance(res.get("usage"), dict):
                self._usage = res["usage"]
            denied = res.get("denied_actions") or []
            if denied:
                self.denied_actions += [d for d in denied if isinstance(d, dict)]
            if self._stopped and not self._interrupted:
                return  # the shutdown record of a process Steward stopped, not a turn outcome
            self._outstanding = max(0, self._outstanding - 1)
            tid = self.turn_id
            if self._outstanding:
                self._turns += 1
                self.turn_id = f"t{self._turns}"
            else:
                self.turn_id = None
            status = str(res.get("status") or "").upper()
            if status == "SUCCESS":
                body = {"turn_id": tid, "status": "completed",
                        "evidence_ref": f"agy:{self.conversation_id}:{tid}"}
                if denied:
                    body["denied_actions"] = denied[:10]
                head = self.head_fn(self.worktree) if self.head_fn and self.worktree else None
                if head and head != self._base_head:
                    body["candidate_sha"] = head
                events.append(self._frame("done", body))
            else:
                err = str(res.get("error") or status or "error")
                interrupted = self._interrupted or "interrupt" in err.lower()
                body = {"turn_id": tid, "status": "interrupted" if interrupted else (status.lower() or "error"),
                        "detail": err[:300]}
                if denied:
                    body["denied_actions"] = denied[:10]
                events.append(self._frame("error", body))
                if interrupted:
                    self._outstanding, self.turn_id, self._interrupted = 0, None, False

    def observe(self) -> WorkerObservation:
        raw = self._held_raw + (self.transport.drain() if self.transport else [])
        self._held_raw = []
        events = self._held
        self._held = []
        for m in raw:
            self._on(m, events)
        for req in self.gate.pending() if self.gate is not None else []:
            self._perm.add(req.request_id)
            events.append(self._frame("permission_request", {"request_id": req.request_id, "tool": req.tool,
                                                             "input": req.input}))
        kept = tuple(e for e in events if self._current(e))
        alive = bool(self.transport and self.transport.alive())
        life = "exited" if not alive else ("working" if self.turn_id else "idle")
        return WorkerObservation(lifecycle=life, output_tail="", pid_alive=alive, ts=now_iso(), events=kept)


def agy_lane_factory(*, model: str | None = None, model_for: Callable[[str, str], str | None] | None = None,
                     head_fn: Callable[[Path], str | None] | None = None, make_process=None, log_dir=None,
                     gate_home: str | Path | None = None):
    """`gate_home` (Steward's state dir) puts every lane behind its own hook gate under `<gate_home>/agy-gate/`."""
    def factory(*, job_id, lane_id, task_id, attempt_no, epoch, lease_check=None):
        return AgyStreamAdapter(make_process, model=model_for(job_id, lane_id) if model_for else model,
                                job_id=job_id, task=task_id, lane=lane_id, attempt=attempt_no, epoch=epoch,
                                lease_check=lease_check, head_fn=head_fn, log_dir=log_dir,
                                gate_dir=gate_root(gate_home, job_id, lane_id) if gate_home else None)
    return factory


# The permission probe needs a side effect to prove what agy did with the answer, not just that an answer was sent.
AGY_MARKER = "steward-approval-marker"
AGY_APPROVAL_BRIEF = (f"Run the shell command `touch {AGY_MARKER}` in the current directory with run_command and report "
                      "whether it succeeded. Do not use any other tool.")


def agy_capability_probes(make_adapter, worktree: Path, *, wait: Callable[[], None] = lambda: None, polls: int = 20,
                          approval_brief: str = AGY_APPROVAL_BRIEF, **kw):
    """The shared lifecycle probes, with agy's reattach (fresh process in the same worktree) and a permission_relay
    probe that drives BOTH answers through the hook gate and checks the side effect: after a deny the marker file
    must be absent, after an approve it must exist. Ungated agy auto-denies instead of asking, and fails the probe."""
    probes = structured_capability_probes(make_adapter, worktree, wait=wait, polls=polls,
                                          approval_brief=approval_brief, **kw)
    marker = Path(worktree) / AGY_MARKER

    def _relay(approve: bool):
        """One run: answer the marker command with `approve`, deny anything else, until the turn ends."""
        a = make_adapter()
        a.start(approval_brief, worktree)
        relayed, other, end = [], [], []
        try:
            for _ in range(polls):
                for e in a.observe().events:
                    if e.type == "permission_request":
                        mine = e.body.get("tool") == "Bash" and AGY_MARKER in str((e.body.get("input") or {})
                                                                                 .get("command"))
                        r = a.answer_prompt(approve if mine else False, e.body.get("request_id"))
                        (relayed if mine else other).append(f"{e.body.get('tool')} "
                                                            f"{json.dumps(e.body.get('input'))[:80]}: {r.detail}")
                    elif e.type in ("done", "error"):
                        end.append(e)
                if end:
                    break
                wait()
        finally:
            a.stop()
        return a, relayed, other, end

    def permission_relay():
        marker.unlink(missing_ok=True)
        a, relayed, other, end = _relay(False)
        if not relayed:
            denied = [d.get("action") for d in a.denied_actions]
            return False, ("no approval request observed; agy headless auto-denied "
                           f"{denied or 'nothing'} instead of asking (permission prompts cannot be relayed in "
                           "stream-json print mode without the hook gate; --dangerously-skip-permissions is never "
                           "used: docs/fleet-evidence/agy-permission-audit.md)")
        deny_ok = bool(end) and not marker.exists()
        deny_ev = f"deny: relayed {relayed[0]}; turn ended {[e.type for e in end]}; marker absent={not marker.exists()}"
        _, relayed2, other2, end2 = _relay(True)
        approve_ok = bool(relayed2) and bool(end2) and marker.exists()
        approve_ev = (f"approve: relayed {relayed2[0] if relayed2 else 'nothing'}; turn ended {[e.type for e in end2]}; "
                      f"marker present={marker.exists()}")
        marker.unlink(missing_ok=True)
        extra = f"; also denied {len(other) + len(other2)} other request(s)" if other or other2 else ""
        return deny_ok and approve_ok, f"{deny_ev} | {approve_ev}{extra} (hook gate: agy-permission-audit.md)"

    def reattach():
        a = make_adapter()
        ref = a.start(PROBE_BRIEF, worktree)
        a.stop()
        b = make_adapter()
        ok = b.attach(ref, worktree)
        return ok, f"attach {ref} via --conversation: {ok}"

    probes["permission_relay"] = permission_relay
    probes["reattach"] = reattach
    return probes
