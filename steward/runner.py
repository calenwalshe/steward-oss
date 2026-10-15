"""Controller loop: dispatch, watch, bounded recovery, restart resume.

`Runner` is the one component that decides what runs next and owns the
worker's input (SP-1, SP-2). Each `tick()` is a single bounded pass:
pause/stop are honoured before any dispatch (SP-11); pending steering is
delivered; heartbeat evidence is collected and classified, logged only on a
class change (no log spam per SP-12); `STEWARD-DONE` moves the job to
`WORKER_FINISHED`; and `steward.recovery.decide` (bounded, budgeted) decides
whether to replace the worker, deliver guidance, or escalate to
`NEEDS_HUMAN`. `resume_all` lets a brand-new `Runner` (a restarted
controller) reattach to whatever attempt was left running instead of
starting a second writer (SP-14).
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import random
import re
import shlex
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from steward import approvals as approvals_module
from steward import decision_trace, exceptions, masking
from steward import research as research_module
from steward import housekeeping, selfrestart
from steward import alerts, bg_baseline, bg_verify, code_facts, efficiency_policy, efficiency_watch, eta_prior, stability, supervisors, yield_lanes
from steward import check_runs, ownership, waits
from steward import questions as questions_module
from steward import questions_delivery
from steward import hung_call, liveness
from steward import deadline as deadline_module
from steward import deadline_evidence, deadline_plan, deadline_status
from steward import wait_policy
from steward import inflight
from steward import auto_interrupt as auto_interrupt_module
from steward.fabric import Frame
from steward import herdr_state
from steward import checkpoint, closeout, diffscan, fleet_drift, fleet_closeout as fleet_closeout_module, heartbeat, herdr_tabs, jobdiff, lease, notify, pause_checkpoint, pause_review, permissions, recovery, weakening
from steward.brief import render_brief
from steward.contract import load_contract, with_pause
from steward.diagnosis import Diagnoser, redact
from steward.discovery import discover as discover_repo
from steward.inner import inner_verdict, record_if_changed
from steward import autopilot as autopilot_module
from steward import authority as authority_module
from steward import push as push_module
from steward import watcher_authority
from steward.custom_watchers import CustomWatchers
from steward import custom_watchers as custom_watchers_module
from steward import jev_watch as jev_watch_module
from steward import resolver as resolver_module
from steward.jev_observer import JevObserver
from steward import tokens as tokens_module
from steward.ledger import TrajectoryLedger
from steward.models import JobState, Judgment
from steward.runner_exec import Runner as CommandRunner
from steward.runner_exec import SubprocessRunner
from steward.steering import Steering, provenance_judgment
from steward.store import Store
from steward.trajectory import TrajectorySupervisor
from steward.verify import verify as verify_worker
from steward.worker import DeliveryResult, WorkerAdapter

WorkerFactory = Callable[[str | None], WorkerAdapter]

# Claude Code echoes the operator's typed prompt as a `> text` line; Codex
# uses `› text`. Neither is a runner delivery unless its text matches
# something the runner itself sent (MOSHI-04, SP-24): a match means the
# terminal is simply replaying our own `deliver()`/`start()` call, not
# someone typing directly into the pane.
_PROMPT_ECHO_RE = re.compile(r"^(?:>|›)\s*(.+?)\s*$", re.MULTILINE)
_BREAK_GLASS_EXCERPT_CAP = 200

# Latency fast path (docs/diagnostics/2026-10-02-latency, #1/#2): between ticks the runner polls the shim mailbox
# every `FAST_POLL_S` and acts on arrival — a code-policy permission answer goes out in the same pass it is decided
# (it used to wait for the next tick's step 1.5: 15.2 s p50), a steer receipt is recorded when it lands, and a newly
# recorded steer is sent without waiting for the tick. Target: < 500 ms p95 for code-policy turnaround.
FAST_POLL_S = 0.2
FAST_PATH_ENV = "STEWARD_FAST_PATH"
# Frames the fast path acts on at once; everything else (done, activity, error, ...) waits for the tick as before.
_FAST_FRAME_KINDS = ("permission_answered", "permission_unknown", "pending_permissions", "batch",
                     "waits_interrupted", "interrupt_refused")


def fast_path_enabled(env=None) -> bool:
    """Kill switch: `STEWARD_FAST_PATH=0` restores tick-only relay and delivery."""
    return (os.environ if env is None else env).get(FAST_PATH_ENV, "1").strip().lower() not in ("0", "false", "off", "no")


def _is_fast_frame(frame) -> bool:
    if frame.type in ("permission_request", "ack"):
        return True
    return frame.type == "event" and frame.body.get("kind") in _FAST_FRAME_KINDS

_DISPATCHABLE_TERMINAL_STATES = frozenset(
    {
        JobState.STOPPED.value,
        JobState.NEEDS_HUMAN.value,
        JobState.WORKER_FINISHED.value,
        JobState.FAILED.value,
        JobState.CHECKS_PASSED.value,
        JobState.ACCEPTED.value,
        JobState.RELEASED.value,
    }
)


_DB_LOCK_RETRY_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)


def _is_db_lock_error(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


@dataclass(frozen=True)
class TickReport:
    job_id: str
    state: str
    cls: str | None
    action: str | None
    detail: str



_UNSET = object()
VERIFY_REQUEUES_KEY = "verify_requeues"  # kv: how many lost background verifications were re-queued (§5.5)
VERIFY_REQUEUE_MAX = 2                   # the third loss is the ladder's ask, never a silent pass


def record_class(store: Store, job_id: str, cls: heartbeat.Class) -> None:
    """Record this poll's heartbeat class. `last_class` (what recovery, the API and the phone read) follows every
    poll. The `heartbeat class changed` event has hysteresis (TUI speed plan 10c): a change is logged once the new
    class held for 2 consecutive polls, so a quiet_known_process <-> progressing flap is not logged every 15 s.
    Stall/dead transitions and the first class of a fresh attempt (`last_class` reset to None) are logged at once."""
    prev_cls = store.kv_get(job_id, "last_class")
    logged = None if prev_cls is None else store.kv_get(job_id, "logged_class", default=_UNSET)
    if logged is _UNSET:  # a job the pre-10c runner logged: its last event's class is last_class
        logged = prev_cls
        store.kv_set(job_id, "logged_class", logged)
    critical = {heartbeat.Class.suspect_stall.value, heartbeat.Class.dead.value}
    if cls.value != logged and (cls.value == prev_cls or prev_cls is None or {cls.value, logged} & critical):
        store.append_event(
            job_id,
            what="heartbeat class changed",
            evidence={"from": logged, "to": cls.value},
            rule="heartbeat_classifier",
            judgment=Judgment.code,
        )
        store.kv_set(job_id, "logged_class", cls.value)
    elif prev_cls is None:
        store.kv_set(job_id, "logged_class", cls.value)
    if cls.value != prev_cls:
        store.kv_set(job_id, "last_class", cls.value)

class Runner:
    def route_lane_candidate(self, job_id: str, task_id: str, *, candidate_sha: str, evidence_ref: str) -> dict:
        """Controller event: a lane produced a committed candidate. Admission and serial integration belong to
        the fleet IntegrationWriter (which re-validates everything against Git and the Store); this single-worker
        Runner only forwards the event and records that it did, never touching Git itself."""
        writer = self.integration_writer
        if writer is None:
            return {"status": "ignored", "reason": "no_integration_writer"}
        res = writer.submit_candidate(job_id, task_id, candidate_sha=candidate_sha, evidence_ref=evidence_ref)
        self.store.append_event(job_id, what="lane_candidate_routed",
                                evidence={"task": task_id, "status": res["status"], "reason": res.get("reason")},
                                rule="fleet_integration_routing", judgment=Judgment.code)
        if res["status"] == "admitted":
            res = {**res, "integrated": writer.integrate(job_id)}
        return res

    def __init__(
        self,
        store: Store,
        worker_factory: WorkerFactory,
        runner_exec: CommandRunner,
        diagnoser: Diagnoser,
        clock: Callable[[], float] = time.time,
        policy: recovery.RecoveryPolicy | None = None,
        heartbeat_policy: heartbeat.HeartbeatPolicy | None = None,
        jev_observer: JevObserver | None = None,
        notifier: notify.Notifier | None = None,
        trajectory: TrajectorySupervisor | None = None,
        custom_watchers: CustomWatchers | None = None,
        jev_watch: jev_watch_module.JevWatch | None = None,
        spend_fn: Callable | None = None,
        activity_fn: Callable | None = None,
        spend_clock: Callable[[], float] = time.monotonic,
        resolver: resolver_module.ResolverEngine | None = None,
        efficiency: efficiency_watch.EfficiencyWatcher | None = None,
        questions_runner: CommandRunner | None = None,
        hung_watch: "hung_call.HungCallWatch | None" = None,
        auto_interrupt: "auto_interrupt_module.AutoInterrupt | None" = None,
    ):
        self.store = store
        self.worker_factory = worker_factory
        self.runner_exec = runner_exec
        # Question detection (Phase 17) reads only committed git content; it has its own runner so the scripted
        # `runner_exec` of existing tests sees no new calls.
        self.questions_runner = questions_runner if questions_runner is not None else SubprocessRunner()
        self.diagnoser = diagnoser
        self.clock = clock
        self.policy = policy or recovery.RecoveryPolicy()
        self.heartbeat_policy = heartbeat_policy or heartbeat.HeartbeatPolicy()
        self.jev = jev_observer or JevObserver()
        self.notifier = notifier or notify.notifier_from_env(runner_exec)
        self.trajectory = trajectory
        self.custom_watchers = custom_watchers or CustomWatchers(store, notify=self._notify, clock=clock)
        # Jev, the free always-on triage layer: its own 10s timer, independent of the tick (steward.jev_watch).
        self.jev_watch = jev_watch or jev_watch_module.JevWatch(store, self.jev, _JevActions(self), clock,
                                                                  activity_fn=activity_fn)
        if getattr(self.jev_watch, "outage", None) is None:  # SUP-08: one `tg send` per Jev outage (+ recovery)
            self.jev_watch.outage = jev_watch_module.OutageAlert(
                store, lambda text: notify.tg_send_text(text, runner=self.runner_exec), clock=clock)

        # Risk-routed resolvers for held approvals (steward.resolver): background, polled each tick.
        self.resolver = resolver or resolver_module.ResolverEngine(
            store, notify=self._notify, clock=clock, jev=self.jev, activity_fn=activity_fn,
            resolve_after_s=resolver_module.LIVE_RESOLVE_AFTER_S)

        self.spend_fn = spend_fn or tokens_module.job_tokens
        self.spend_clock = spend_clock  # wall time, not the (possibly simulated) tick clock: transcripts cost real I/O
        self.activity_fn = activity_fn  # tests inject a transcript view; None reads the worker's session file
        self._code_recorded: set[str] = set()
        self.efficiency = efficiency or efficiency_watch.EfficiencyWatcher(
            store, notify=self._notify, spend_clock=spend_clock, tokens_fn=self.spend_fn)

        self._workers: dict[str, WorkerAdapter] = {}
        # Phase 28: optional fleet IntegrationWriter; lane candidates reach it only through route_lane_candidate.
        self.integration_writer = None
        self.fleet_closeout = None  # Phase 28-02: FleetCloseout; fleet jobs never use the single-worker closeout
        # Phase 29: the one JobController (its `arbiter` joins every supervisor's facts) and the TechLeadSupervisor.
        # Both optional; legacy jobs and unwired runners behave exactly as before.
        self.fleet_controller = None
        self.drift_watch = None  # fleet_drift.DriftWatch, built on first tick (STEWARD_RUNTIME_DRIFT=0 disables)
        self.fleet_triage = None  # v2.5: per-lane Jev for a fleet job (steward.fleet_live.wire)
        self.tech_lead = None
        # v2.6 (SUP-07): the Arbiter for every job (single-worker facts have no lane; a fleet job's controller has
        # its own instance over the same per-job store document)
        from steward import fleet_runtime as _rt
        self.arbiter = _rt.Arbiter(store, clock)
        # v2.6 (SUP-09): the tech lead for a single-worker job (steward.fleet_tech_lead.JobTechLead); None = unwired,
        # the job behaves exactly as before
        self.job_lead = None
        # v2.6 (SUP-02): how a background candidate verification is launched (tests inject a fake; None = detached)
        self.bg_verify_spawn = None
        self.check_spawn = None  # (run_id, job_id) -> start the detached worker-check executor; None = real spawn
        self._fleet_last_cls: dict[str, heartbeat.Class] = {}
        self._attempts: dict[str, int] = {}
        self._signals: dict[str, heartbeat.Signals] = {}
        self._last_change_at: dict[str, float] = {}
        self._delivered_texts: dict[str, set[str]] = {}
        self._seen_break_glass: dict[str, set[str]] = {}
        # Who relayed a permission answer: a later instance (after a restart) re-sends unacked ones.
        self._instance_id = uuid.uuid4().hex
        # (job, seq) of frames the between-tick fast path already acted on; the tick skips them once.
        self._fast_seen: dict[str, set[tuple[str, int]]] = {}
        self.hung_watch = hung_watch or hung_call.HungCallWatch(store, clock=clock, activity_fn=activity_fn)
        # Latency #7: code-initiated interrupt of pure waits (rule + Jev, both required; STEWARD_AUTO_INTERRUPT=0 off).
        self.auto_interrupt = auto_interrupt or auto_interrupt_module.AutoInterrupt(
            store, lambda st, jid, batch: auto_interrupt_module.jev_activity(self.jev, st, jid, batch), clock=clock)

    def _worktree(self, job_id: str) -> Path:
        return self.store.home / "worktrees" / job_id

    def _is_fleet_job(self, job_id: str) -> bool:
        """Treat an explicit but partially configured fleet opt-in as fleet too (fail closed)."""
        from steward import fleet_runtime
        return fleet_runtime.is_fleet_job(self.store, job_id) or bool(
            self.store.kv_get(job_id, "fleet_mode_requested", False))

    # -- phone notifications (MOSHI-02, MOSHI-03) ---------------------------

    def _notify(self, job_id: str, kind: str, title: str, body: str) -> bool:
        workspace = self.store.kv_get(job_id, "herdr_workspace")
        tab = self.store.kv_get(job_id, "herdr_tab")
        pane = self.store.kv_get(job_id, "herdr_pane")
        title, body = push_module.phone_event(self.store, job_id, kind, body)  # one grammar with the pane line
        event = notify.PhoneEvent(
            kind=kind,
            job_id=job_id,
            title=title,
            body=body,
            deep_link=notify.deep_link(workspace, tab, pane),
            revision=self.store.get_revision(job_id),
        )
        return notify.emit(self.store, self.notifier, event)  # u4: callers get the receipt, not just the attempt

    def _deadline_step(self, job_id: str) -> None:
        if self.store.kv_get(job_id, deadline_module.K_COMMIT) is None:
            return  # ordinary jobs: one kv read, nothing else
        try:
            deadline_status.sweep(self.store, job_id, now=datetime.fromtimestamp(self.clock(), timezone.utc),
                                  runner=self.runner_exec, worktree=self._worktree(job_id))
        except Exception:  # noqa: BLE001 - sweep never raises; this is belt and braces for the tick
            pass

    def _deadline_brief(self, job_id: str) -> list[str] | None:
        """The DEADLINE brief section (v2.2) for a job with a deadline in force; never breaks a start."""
        try:
            return deadline_plan.brief_lines(self.store, job_id) or None
        except Exception:  # noqa: BLE001
            return None

    def _sync_herdr_state(self, job_id: str) -> None:
        """Mirror the job to its worker pane in Herdr (see `steward.herdr_state`)."""
        herdr_state.sync(self.store, self.runner_exec, job_id)

    def _record_worker_location(self, job_id: str, worker: WorkerAdapter) -> None:
        """Best-effort, duck-typed: only `HerdrWorker` exposes these attrs.

        `FakeWorker` (and any adapter without them) leaves the kv keys unset,
        so `_notify`'s deep link simply omits the missing parts (SP-25) —
        this never requires changing `steward.herdr_worker`.
        """
        pane = getattr(worker, "pane_id", None)
        # Herdr pane ids are workspace-qualified ("w3T:p5"); the worker *name* is not a workspace.
        workspace = pane.split(":", 1)[0] if isinstance(pane, str) and ":" in pane else None
        kind = getattr(worker, "kind", None)
        if workspace is not None:
            self.store.kv_set(job_id, "herdr_workspace", workspace)
        if pane is not None:
            self.store.kv_set(job_id, "herdr_pane", pane)
        if kind is not None:
            self.store.kv_set(job_id, "worker_kind", kind)

    # -- break-glass detection (MOSHI-04, SP-24) -----------------------------

    def _track_delivery(self, job_id: str, text: str) -> None:
        """Remember text the runner itself sent this worker, for `_detect_break_glass`."""
        self._delivered_texts.setdefault(job_id, set()).add(text)

    def _detect_break_glass(self, job_id: str, output_tail: str) -> None:
        """Flag a prompt-echo line that doesn't match any runner delivery.

        Never blocks, never treated as an instruction revision — only
        recorded (`judgment=human`, since a human at the keyboard is the
        only thing that could have produced it). Each distinct excerpt is
        recorded once per job (the same line often stays in a rolling
        output tail across several ticks).
        """
        known = self._delivered_texts.get(job_id, set())
        already_seen = self._seen_break_glass.setdefault(job_id, set())
        for match in _PROMPT_ECHO_RE.finditer(output_tail):
            text = match.group(1).strip()
            if not text or text in already_seen:
                continue
            if any(text in delivered or delivered in text for delivered in known):
                continue
            already_seen.add(text)
            self.store.append_event(
                job_id,
                what="break_glass_input",
                evidence={"excerpt": text[:_BREAK_GLASS_EXCERPT_CAP]},
                rule="prompt_echo_without_matching_delivery",
                judgment=Judgment.human,
            )

    # -- worker permission-prompt relay (SP-2, SP-19, SP-24) ----------------

    def _relay_permission_decisions(self, job_id: str, worker: WorkerAdapter, *, only_request_id: str | None = None) -> None:
        """Send an already-decided `permission` approval's answer to the
        worker (SP-2: the runner alone touches worker input).

        Legacy adapters: exactly once; a decision that no longer applies because the worker is no longer
        blocked is marked stale instead of retried.

        Fabric (lost-approval hang, job 8a641f9b retro 2026-09-30): the answer stays persisted, with its worker
        request_id, until the shim acks delivery. A runner instance re-sends every answer an earlier instance
        (before a restart/reattach) sent but never saw acked, and never marks one stale from its own empty
        memory — only the shim's `permission_unknown` / `pending_permissions` can say the request is gone.
        """
        fabric = _is_fabric(worker)
        for record in approvals_module.list_approvals(self.store, job_id):
            if record["kind"] != "permission" or record["status"] not in ("approved", "denied"):
                continue
            if only_request_id is not None and record.get("worker_request_id") != only_request_id:
                continue
            if record.get("relayed_at"):
                resend = (fabric and record.get("ack_expected") and not record.get("delivery_acked_at")
                          and record.get("relayed_by") != self._instance_id)
                if not resend:
                    continue
                self.store.append_event(job_id, what="permission answer re-sent after restart",
                                        evidence={"approval_id": record["id"],
                                                  "request_id": record.get("worker_request_id"),
                                                  "decision": record["status"]},
                                        rule="permission_resend_unacked", judgment=Judgment.code)
            rid = record.get("worker_request_id")
            message = record.get("deny_message") if record["status"] == "denied" else None
            if rid and message:
                result = worker.answer_prompt(approve=False, request_id=rid, message=message)
            elif rid:
                result = worker.answer_prompt(approve=(record["status"] == "approved"), request_id=rid)
            else:
                result = worker.answer_prompt(approve=(record["status"] == "approved"))
            if result.ok:
                approvals_module.mark_relayed(self.store, job_id, record["id"], ack_expected=fabric and bool(rid),
                                              relayed_by=self._instance_id)
            elif fabric and rid:
                continue  # not sent (pane unreachable): stays in flight; the shim alone may call it stale
            else:
                approvals_module.mark_stale(self.store, job_id, record["id"], reason=result.detail)

    def _handle_permission_prompt(self, job_id: str, sig: heartbeat.Signals) -> None:
        """Open exactly one `permission` approval per prompt, or clear a
        pending one that a human answered directly in the pane (break-glass).

        `sig.permission_prompt` is the same evidence `heartbeat.classify`
        uses to reach `Class.waiting` (SP-12): it never re-derives its own
        notion of "blocked".
        """
        pending = [
            a
            for a in approvals_module.list_approvals(self.store, job_id, status="pending")
            if a["kind"] == "permission"
        ]

        if not sig.permission_prompt:
            for record in pending:
                approvals_module.mark_stale(self.store, job_id, record["id"], reason="worker no longer blocked")
                self.store.append_event(
                    job_id,
                    what="break_glass_input",
                    evidence={"approval_id": record["id"]},
                    rule="permission_answered_in_pane",
                    judgment=Judgment.human,
                )
            return

        if pending:
            return  # already have an open approval for this prompt

        request = heartbeat.extract_permission_request(sig.output_tail)
        record = approvals_module.open_approval(
            self.store, job_id, f"worker permission: {request}", kind="permission"
        )
        self.store.append_event(
            job_id,
            what="worker permission requested",
            evidence={"approval_id": record["id"], "request": request},
            rule="worker_permission_requested",
            judgment=Judgment.code,
        )
        self._notify(
            job_id,
            "NEEDS_YOU",
            "Steward: needs you",
            f"approval {record['id'][:8]}: worker permission: {request[:100]}",
        )

    # -- typed fabric events (WRK-03, SP-35): ack + permission_request -------
    #
    # No marker regex, no output_tail parsing decides anything here — every
    # frame in `events` already arrived pre-disambiguated from
    # `FabricWorker.ack_kind()` (an `ack`-typed frame here is always a steer
    # receipt, never a transport ack; see steward/fabric_worker.py).

    def handle_fleet_frame(self, job_id: str, frame, worker: WorkerAdapter | None) -> None:
        """Apply shared permission policy to a current, controller-validated lane frame."""
        lane_row = self.store._conn.execute(
            "SELECT worktree_path FROM fleet_lanes WHERE job_id=? AND lane_id=?", (job_id, frame.lane)
        ).fetchone()
        worktree = Path(lane_row["worktree_path"]) if lane_row and lane_row["worktree_path"] else None
        if frame.type == "permission_request":
            self._open_fabric_permission(job_id, frame, worktree=worktree)
            rid = str(frame.body.get("request_id") or "")
            if worker is not None and rid:
                self._relay_permission_decisions(job_id, worker, only_request_id=rid)
        elif frame.type == "event" and frame.body.get("kind") in ("permission_answered", "permission_unknown"):
            self._permission_receipt(job_id, frame)
        elif frame.type == "event" and frame.body.get("kind") == "pending_permissions":
            self._reconcile_permissions(job_id, frame, worktree=worktree)
            if worker is not None:
                for pending in frame.body.get("pending") or []:
                    rid = pending.get("request_id")
                    if rid:
                        self._relay_permission_decisions(job_id, worker, only_request_id=str(rid))
        elif frame.type == "event" and frame.body.get("kind") == "batch":
            inflight.record(self.store, job_id, frame.body)

    def relay_fleet_permission_decisions(self, job_id: str, controller) -> None:
        """Relay persisted answers only to the live lane attempt that created each request."""
        from steward import fleet_lease

        routes = self.store.kv_get(job_id, "fleet_permission_routes", {}) or {}
        for route in routes.values():
            lease = fleet_lease.get(self.store, job_id, route["lane"])
            if not lease or lease["state"] != "running" or (lease["attempt_no"], lease["epoch"]) != (
                    route["attempt"], route["epoch"]):
                continue
            worker = controller.adapter(job_id, route["lane"])
            if worker is not None:
                self._relay_permission_decisions(job_id, worker, only_request_id=route["request_id"])

    def _deliver_pending_steers(self, job_id: str, worker: WorkerAdapter) -> None:
        steering = Steering(self.store)
        fabric = _is_fabric(worker)
        for instruction in steering.pending(job_id):
            text = f"[rev={instruction['rev']}] {instruction['text']}"
            if instruction.get("urgent") and fabric:
                result = worker.deliver(text, urgent=True)
            else:
                result = worker.deliver(text)
            steering.mark_delivered(job_id, instruction["rev"], result)
            if instruction.get("origin") != "contract":
                watcher_authority.note_steer_delivered(self.store, job_id, self.clock())
            self._track_delivery(job_id, text)

    def _consume_fabric_events(self, job_id: str, steering: Steering, events: tuple) -> None:
        seen = self._fast_seen.get(job_id, set())
        opened = False
        for frame in events:
            key = (frame.job, frame.seq)
            if key in seen:
                seen.discard(key)  # the fast path already acted on it between ticks
                continue
            if frame.type == "permission_request":
                opened = True
            if frame.type == "ack":
                rev = frame.body.get("rev")
                stage = frame.body.get("stage")
                if rev is not None and stage in ("delivered", "acknowledged"):
                    at = frame.body.get("at")
                    steering.mark_acknowledged(job_id, rev, stage, at=at,
                                               evidence={"seq": frame.seq, **({"consumed_at": at} if at else {})})
            elif frame.type == "permission_request":
                self._open_fabric_permission(job_id, frame)
            elif frame.type == "event" and frame.body.get("kind") in ("permission_answered", "permission_unknown"):
                self._permission_receipt(job_id, frame)
            elif frame.type == "event" and frame.body.get("kind") == "pending_permissions":
                self._reconcile_permissions(job_id, frame)
            elif frame.type == "event" and frame.body.get("kind") == "batch":
                inflight.record(self.store, job_id, frame.body)
            elif frame.type == "event" and frame.body.get("kind") in ("waits_interrupted", "interrupt_refused"):
                applied = frame.body["kind"] == "waits_interrupted"
                self.store.append_event(
                    job_id, what="auto_interrupt_applied" if applied else "auto_interrupt_refused",
                    evidence={k: frame.body.get(k) for k in ("gen", "rev", "call_ids", "reason", "rule_version")
                              if frame.body.get(k) is not None} | {"seq": frame.seq},
                    rule=auto_interrupt_module.RULE_VERSION, judgment=Judgment.code)
            elif frame.type == "event" and frame.body.get("kind") == "interrupted":
                self.store.append_event(
                    job_id,
                    what="worker_interrupted",
                    evidence={"rev": frame.body.get("rev"), "seq": frame.seq},
                    rule="urgent_steer_interrupt",
                    judgment=Judgment.code,
                )
            elif frame.type == "error":
                self.store.append_event(
                    job_id,
                    what="worker error frame",
                    evidence={"exit_code": frame.body.get("exit_code"), "seq": frame.seq},
                    rule="fabric_error_frame",
                    judgment=Judgment.code,
                )
            # "event" (activity) frames need no runner action: heartbeat has
            # already folded them into `sig.turn_open`.
        if opened:
            self._relay_now(job_id)

    def _flush_deferred_nudges(self, job_id: str) -> None:
        """Latency #6: a watcher nudge held back only by an unconsumed steer goes out once that steer lands."""
        flush = getattr(self.custom_watchers, "flush_deferred", None)
        if flush is not None:
            try:
                flush(job_id)
            except Exception:  # noqa: BLE001 - supervisor failures never stop the controller (SP-6)
                pass

    def _relay_now(self, job_id: str) -> None:
        """Fast path #1a: relay whatever code policy just decided in this same pass, not at the next tick's step
        1.5. Same idempotent relay (request ids, resend-after-restart); only the timing changes."""
        worker = self._workers.get(job_id)
        if worker is not None and fast_path_enabled():
            self._relay_permission_decisions(job_id, worker)

    def pump(self, job_id: str, timeout: float = 0.0) -> int:
        """Between ticks (fast path #1b/#2): drain the shim mailbox and act on permission requests, permission
        receipts and steer receipts on arrival; send any steer recorded since the last pass. The drained frames
        stay queued in the worker for the next tick, which skips what was already handled here. Returns the
        number of frames acted on. Never raises: a failure is recorded and the tick handles the frames."""
        worker = self._workers.get(job_id)
        if worker is None or not _is_fabric(worker) or not fast_path_enabled():
            return 0
        job = self.store.get_job(job_id)
        if job is None or job.state != JobState.RUNNING:
            return 0
        try:
            self._deliver_pending_steers(job_id, worker)
            pump = getattr(worker, "pump", None)
            frames = [f for f in (pump(timeout) if pump is not None else []) if _is_fast_frame(f)]
            if frames:
                self._consume_fabric_events(job_id, Steering(self.store), tuple(frames))
                self._fast_seen.setdefault(job_id, set()).update((f.job, f.seq) for f in frames)
                if any(f.type == "ack" for f in frames):
                    self._flush_deferred_nudges(job_id)
            self._relay_permission_decisions(job_id, worker)  # also an answer a human/resolver gave since the tick
            self.auto_interrupt.step(job_id, worker)
            return len(frames)
        except Exception as exc:  # noqa: BLE001 - the tick is the backstop; the fast path must never stop the loop
            self.store.append_event(job_id, what="fast_path_error", evidence={"detail": f"{type(exc).__name__}: {exc}"[:200]},
                                    rule="latency_fast_path", judgment=Judgment.code)
            return 0

    def _permission_receipt(self, job_id: str, frame) -> None:
        """The shim's word on one answer: delivered (ack), or no such pending request (the only stale path)."""
        rid = frame.body.get("request_id")
        record = approvals_module.by_request_id(self.store, job_id, rid) if rid else None
        if record is None:
            return
        if frame.body["kind"] == "permission_answered":
            approvals_module.mark_delivery_acked(self.store, job_id, record["id"],
                                                 duplicate=bool(frame.body.get("duplicate")))
        elif record["status"] in ("approved", "denied") and not record.get("delivery_acked_at"):
            approvals_module.mark_stale(self.store, job_id, record["id"], reason="shim_confirmed_no_pending")

    def _reconcile_permissions(self, job_id: str, frame, *, worktree: Path | None = None) -> None:
        """Match persisted permission approvals against the shim's authoritative snapshot (sent on every
        rebind/reattach and on request): a still-pending request whose approval is unknown or stale is
        re-opened (a stale one keeps its answer and is re-relayed); an answer the shim already delivered is
        acked; an unacked answer for a request the shim does not have is stale — confirmed, not inferred."""
        pending = {p.get("request_id"): p for p in frame.body.get("pending") or [] if p.get("request_id")}
        answered = set(frame.body.get("answered") or [])
        for rid, req in pending.items():
            record = approvals_module.by_request_id(self.store, job_id, rid)
            if record is None:
                synthetic = Frame(v=1, seq=frame.seq, job=frame.job, type="permission_request",
                                  body={"request_id": rid, "tool": req.get("tool", ""), "input": req.get("input") or {}})
                self._open_fabric_permission(job_id, synthetic, worktree=worktree)
                continue
            if record["status"] == "stale":
                back = approvals_module.reopen(self.store, job_id, record["id"], rule="permission_reopened_on_reattach")
                if back is not None and back["status"] == "pending":
                    self._notify(job_id, "NEEDS_YOU", "Steward: needs you", f"reopened: {record['question'][:100]}")
            elif record["status"] in ("approved", "denied") and not record.get("delivery_acked_at") \
                    and record.get("relayed_at"):
                approvals_module.clear_relayed(self.store, job_id, record["id"], rule="permission_resend_unacked")
        for record in approvals_module.list_approvals(self.store, job_id):
            rid = record.get("worker_request_id")
            if record["kind"] != "permission" or not rid or rid in pending:
                continue
            if record["status"] not in ("approved", "denied") or record.get("delivery_acked_at") \
                    or not record.get("relayed_at"):
                continue
            if rid in answered:
                approvals_module.mark_delivery_acked(self.store, job_id, record["id"], duplicate=True)
            else:
                approvals_module.mark_stale(self.store, job_id, record["id"], reason="shim_confirmed_no_pending")

    def _open_fabric_permission(self, job_id: str, frame, *, worktree: Path | None = None) -> None:
        """The typed counterpart to `_handle_permission_prompt`'s opening
        half: one pending `permission` approval per outstanding
        `permission_request` frame, keyed by the frame's own `request_id`
        rather than a scraped prompt card (WRK-02).

        This is also how "Claude itself doesn't block" (NB-02): the shim
        spawns Claude with `--permission-mode acceptEdits
        --permission-prompt-tool stdio` (`steward.cli._build_shim_agent_argv`
        / `steward.herdr_worker.claude_preauthorized_args`), so file edits
        inside the worktree are accepted locally and every other tool call
        (Bash not already pre-authorized, WebFetch, ...) is handed to the
        stdio permission-prompt tool instead of prompting interactively in
        the pane. The shim turns that into exactly this typed
        `permission_request` frame, and `steward.permissions.decide` below
        answers it within the same tick for anything not on SP-36's
        outbound/irreversible list — no interactive wait, no pane scrape.
        """
        masking.arm_job(self.store, job_id)
        tool = frame.body.get("tool", "")
        request_id = frame.body.get("request_id", "")
        # One approval per worker request: parallel tool calls ask several at once, and an
        # approval that doesn't name its request gets answered against the wrong one.
        if request_id and any(
            a.get("worker_request_id") == request_id
            for a in approvals_module.list_approvals(self.store, job_id)
            if a["kind"] == "permission"
        ):
            return  # a retransmitted frame for a request we already opened
        question = redact(f"worker permission: {tool} {frame.body.get('input', {})}")[:300]
        # TUI speed plan 10b: the request's events are written only when code does not allow it by itself; an
        # auto-allowed call records one event (approvals.SINGLE_EVENT) once its answer's delivery is known.
        record = approvals_module.open_approval(
            self.store, job_id, question, kind="permission", worker_request_id=request_id or None, record_event=False
        )
        requested = {"approval_id": record["id"], "request_id": request_id, "tool": tool, "seq": frame.seq}
        chain = []

        def full_chain() -> None:
            if chain:
                return
            chain.append(True)
            approvals_module.record_opened(self.store, job_id, record)
            self.store.append_event(
                job_id,
                what="worker permission requested",
                evidence=requested,
                rule="worker_permission_requested_frame",
                judgment=Judgment.code,
            )

        contract = load_contract(self.store, job_id)
        tool_input = frame.body.get("input") or {}
        command = tool_input.get("command", "") if tool == "Bash" else ""
        if self._intercept_check_verbs(job_id, record, tool, tool_input, worktree=worktree or self._worktree(job_id),
                                       contract=contract, before_deny=full_chain, single=requested):
            return
        if self._deny_long_wait(job_id, record, tool, tool_input, before_deny=full_chain):
            return
        if self._deny_background_agent(job_id, record, tool, tool_input):
            return
        lane_worktree = worktree or self._worktree(job_id)
        if command and contract and in_contract_command(command, contract.authority, lane_worktree):
            # SP-19: an action the contract already authorizes is not a human
            # interruption. Approved by code, recorded, relayed like any other.
            self._auto_approve_permission(
                job_id,
                record,
                what="in-contract permission auto-approved",
                rule="in_contract_auto_approve",
                evidence={"approval_id": record["id"], "command": redact(command)[:300]},
                single=requested,
            )
            return

        verdict = authority_module.decide_worker(self.store, job_id, tool, tool_input if tool != "Bash" else command,
                                                 lane_worktree)
        if verdict.basis.startswith("prohibited:"):
            full_chain()
            approvals_module.decide(self.store, job_id, record["id"], "deny", decided_by="code")
            self.store.append_event(job_id, what="owner prohibition denied permission",
                                    evidence={"approval_id": record["id"], "tool": tool, **verdict.to_dict()},
                                    rule="owner_prohibition_denied", judgment=Judgment.code)
            return
        if verdict.allow and verdict.grant_id:
            self._auto_approve_permission(
                job_id, record, what="owner-granted permission auto-approved", rule="owner_grant_auto_approve",
                evidence={"approval_id": record["id"], "tool": tool, "grant_id": verdict.grant_id,
                          "authority_rev": verdict.rev, "basis": verdict.basis},
                single=requested)
            return

        if tool.startswith(research_module.TOOL_PREFIX):
            full_chain()  # a metered research call keeps its full record
            self._handle_research_call(job_id, record, tool, tool_input)
            return

        # SP-36 (14-01/NB-02): everything else defaults to allowed unless it
        # is on the outbound/irreversible list (`steward.permissions`) — a
        # job whose worker fetches a public URL or runs its own scripts
        # completes with zero human approvals.
        decision = permissions.decide(tool, tool_input if tool != "Bash" else command, lane_worktree)
        if decision.allow:
            self._auto_approve_permission(
                job_id,
                record,
                what="permission_auto_allowed",
                rule="permission_policy_allow",
                evidence={"approval_id": record["id"], "tool": tool, "policy_rule": decision.reason},
                single=requested,
            )
            return
        full_chain()
        try:
            deferred = self.resolver.record_hold(job_id, record["id"], tool, tool_input, decision.reason)
        except Exception:  # noqa: BLE001 - never let bookkeeping stop the ping below
            deferred = None
        if deferred is not None:
            return  # a resolver will look first; it pings the owner itself if it escalates
        self._notify(job_id, "NEEDS_YOU", "Steward: needs you", f"approval {record['id'][:8]}: {question[:100]}")

    def _deny_long_wait(self, job_id: str, record: dict, tool: str, tool_input: dict, *, before_deny=None) -> bool:
        """Latency #3: a foreground wait bounded above the cap (10 s) is denied with the replacement instruction
        (run_in_background / Monitor, poll in short calls, one wait per batch). Anything the rule does not
        understand is allowed (fail open). `STEWARD_WAIT_POLICY=shadow` only records `would_deny`."""
        mode = wait_policy.mode()
        if mode == "off":
            return False
        verdict = wait_policy.check(tool, tool_input)
        if not verdict.deny:
            return False
        evidence = {"approval_id": record["id"], "command": redact(str(tool_input.get("command", "")))[:300],
                    "bound_s": verdict.bound_s, "form": verdict.form, "cap_s": wait_policy.cap_s(),
                    "rule_version": verdict.rule_version, "mode": mode}
        if mode == "shadow":
            self.store.append_event(job_id, what="foreground_wait_would_deny", evidence=evidence,
                                    rule="foreground_wait_cap", judgment=Judgment.code)
            return False
        if before_deny is not None:
            before_deny()
        approvals_module.annotate(self.store, job_id, record["id"], deny_message=verdict.reason)
        approvals_module.decide(self.store, job_id, record["id"], "deny", decided_by="code")
        self.store.append_event(job_id, what="foreground_wait_denied", evidence=evidence,
                                rule="foreground_wait_cap", judgment=Judgment.code)
        return True

    # -- Steward-owned long processes (v2.7 Phase 42, SRD-13/14) ------------------------------------------------------

    def _intercept_check_verbs(self, job_id: str, record: dict, tool: str, tool_input: dict, *, worktree, contract,
                               before_deny=None, single: dict | None = None) -> bool:
        """`steward check run|await|wait` and a bare full-suite command, implemented through the permission hook so
        the worker has no new write path to the store. `run` / a full suite submit a Steward-owned `check_runs` row on
        the worker's committed tree and are denied with the run id; `await` parks a `check_run` wait and is denied with
        "end your turn now"; `wait` is a real CLI verb (read-only polling) and is auto-approved. Follows the wait
        policy's `off` switch. Returns True when the call was answered here."""
        if tool != "Bash" or wait_policy.mode() == "off" or not check_runs.enabled():
            return False
        parsed = wait_policy.parse_check_verb(tool, tool_input)
        command = str(tool_input.get("command") or "").strip()
        if parsed is None:
            if not command or not stability.is_full_suite(command) or wait_policy.CHECK_UNSAFE.search(command):
                return False
            verb, args = "run", [command]
        else:
            verb, args = parsed
        base = {"approval_id": record["id"], "verb": verb}

        def deny(reason: str, what: str, **evidence) -> bool:
            if before_deny is not None:
                before_deny()
            approvals_module.annotate(self.store, job_id, record["id"], deny_message=reason)
            approvals_module.decide(self.store, job_id, record["id"], "deny", decided_by="code")
            self.store.append_event(job_id, what=what, evidence={**base, **evidence}, rule="check_verbs",
                                    judgment=Judgment.code)
            return True

        if verb == "wait":
            self._auto_approve_permission(job_id, record, what="check_wait_auto_approved", rule="check_verbs",
                                          evidence={**base, "args": [redact(a)[:80] for a in args[:3]]}, single=single)
            return True
        if verb == "await":
            return self._check_await(job_id, args, deny)
        cmd = args[0] if len(args) == 1 else shlex.join(args)
        if not cmd.strip():
            return deny("usage: steward check run <command>", "check_run_refused", why="empty")
        python_env = ""
        try:
            discovery = discover_repo(worktree)
            python_env = discovery.python_env
            discovered = {c.line for c in discovery.commands if c.kind == "check"}
        except Exception:  # noqa: BLE001 - no discovery: only contract-authorized and full-suite commands run
            discovered = set()
        authorized = (stability.is_full_suite(cmd) or cmd in discovered
                      or bool(contract and in_contract_command(cmd, contract.authority, worktree)))
        if not authorized:  # SP-7: a discovered or named command authorizes only itself, never an arbitrary line
            return deny(f"`{cmd[:120]}` is not an authorized check command for this job (not a full suite, not in the "
                        f"contract's `run:` authority, not a discovered check): run it yourself, or submit a "
                        f"command the contract authorizes.", "check_run_refused", why="unauthorized",
                        command=redact(cmd)[:200])
        sub = check_runs.submit_worker_run(self.store, self.runner_exec, worktree, job_id, cmd, now=self.clock(),
                                           python_env=python_env)
        if sub.row is None:
            return deny(sub.error, "check_run_refused", why="dirty_tree", command=redact(cmd)[:200])
        run_id = sub.row.run_id
        if sub.row.state == "queued":
            try:
                (self.check_spawn or check_runs.spawn_worker_executor)(run_id, job_id)
            except Exception as exc:  # noqa: BLE001 - the run stays queued; the worker still gets its id
                self.store.append_event(job_id, what="check_run_spawn_error", evidence={
                    "run_id": run_id, "error": f"{type(exc).__name__}: {exc}"[:200]}, rule="check_verbs",
                                        judgment=Judgment.code)
        reason = (f"submitted run {run_id}; wait with: steward check wait {run_id} (zero-cost) or "
                  f"steward check await {run_id} (end your turn)")
        if sub.row.state in check_runs.FINAL:
            reason = f"run {run_id} already finished (reused receipt): {check_runs.result_summary(sub.row)}"
        return deny(reason, "check_run_submitted", run_id=run_id, how=sub.how, command=redact(cmd)[:200],
                    heavy=sub.row.heavy, state=sub.row.state, implicit=parsed is None)

    def _check_await(self, job_id: str, args: list[str], deny) -> bool:
        run_id = args[0] if args else ""
        row = check_runs.get(self.store, run_id) if run_id else None
        mine = row is not None and (row.requested_by == job_id
                                    or any(w["job_id"] == job_id for w in check_runs.waiters(self.store, run_id)))
        if not mine:
            return deny(f"unknown run {run_id or '(none given)'}: submit one first with steward check run <cmd>",
                        "check_await_refused", run_id=run_id)
        if row.state in check_runs.FINAL:
            return deny(f"run {run_id} already finished: {check_runs.result_summary(row)}", "check_await_refused",
                        run_id=run_id, state=row.state, why="already_final")
        waits.park(self.store, job_id, blocker_id=None, kind="check_run", params={"run_id": run_id},
                   reason=f"check run {run_id}: `{row.command[:60]}`", now=self.clock())
        return deny(f"parked on run {run_id}; end your turn now. Steward resumes you with the result.",
                    "check_await_parked", run_id=run_id)

    def _check_run_wake_step(self, job_id: str) -> None:
        """Each tick: wake this job's `check_run` waits whose run is final (one result steer each). Never raises."""
        try:
            waits.probe_job(self.store, job_id, now=self.clock(), kinds=("check_run",))
        except Exception as exc:  # noqa: BLE001 - SP-6: a wake failure never stops the tick; the sweep is the backstop
            self.store.append_event(job_id, what="check_run_wake_error", evidence={"error": str(exc)[:200]},
                                    rule="check_verbs", judgment=Judgment.code)

    def _deny_background_agent(self, job_id: str, record: dict, tool: str, tool_input: dict) -> bool:
        """S1 (D5): a background subagent outlives the worker's turn where Steward cannot see it, so it is denied
        with the replacement instruction (run it in the foreground). Follows the wait policy's off/shadow modes."""
        mode = wait_policy.mode()
        reason = wait_policy.background_agent_denial(tool, tool_input)
        if mode == "off" or reason is None:
            return False
        evidence = {"approval_id": record["id"], "tool": tool, "mode": mode,
                    "description": redact(str(tool_input.get("description", "")))[:200]}
        if mode == "shadow":
            self.store.append_event(job_id, what="background_agent_would_deny", evidence=evidence,
                                    rule="background_agent_denied", judgment=Judgment.code)
            return False
        approvals_module.annotate(self.store, job_id, record["id"], deny_message=reason)
        approvals_module.decide(self.store, job_id, record["id"], "deny", decided_by="code")
        self.store.append_event(job_id, what="background_agent_denied", evidence=evidence,
                                rule="background_agent_denied", judgment=Judgment.code)
        return True

    def _handle_research_call(self, job_id: str, record: dict, tool: str, tool_input: dict) -> None:
        """A sanctioned `mcp__research__*` call: metered against the job's cap.
        Allowed (and recorded as an `exception_used` research-mcp + a feed event)
        until the cap is crossed; then NEEDS_YOU once and every further call is
        denied until `steward research-cap` raises it."""
        allowed, info = research_module.charge(self.store, job_id, tool, tool_input)
        query = redact(research_module.query_of(tool_input))[:120]
        if allowed:
            try:
                exceptions.use(self.store, job_id, "research-mcp", {"tool": info["tool"]})
            except exceptions.UnregisteredPath:
                # SP-34: an unregistered path is blocked where Steward controls it
                # (`steward exceptions sync` adds the default section).
                self._deny_permission(job_id, record, what="research_call_denied",
                                      evidence={"approval_id": record["id"], "tool": info["tool"],
                                                "reason": "research-mcp exception path not registered"})
                return
            self.store.append_event(
                job_id, what="research_call",
                evidence={"approval_id": record["id"], "tool": info["tool"], "query": query,
                          "est_usd": info.get("est_usd"), "calls": info["calls"],
                          "spent_usd": info["spent_usd"], "cap_usd": info["cap_usd"]},
                rule="research_call_metered", judgment=Judgment.code)
            self._auto_approve_permission(
                job_id, record, what="permission_auto_allowed", rule="permission_policy_allow",
                evidence={"approval_id": record["id"], "tool": tool, "policy_rule": "research MCP (sanctioned, read-only)"})
            if info.get("over"):
                from steward import spend_policy

                spend_policy.advise(self.store, job_id, "research", observed_usd=info["spent_usd"],
                                    notify_fn=self._notify, now=self.clock())
        else:
            self._deny_permission(job_id, record, what="research_call_denied",
                                  evidence={"approval_id": record["id"], "tool": info["tool"], "query": query,
                                            "reason": info.get("reason", "denied")})
        if info["trip"]:
            self.store.append_event(
                job_id, what="research_cap_tripped",
                evidence={"spent_usd": info["spent_usd"], "cap_usd": info["cap_usd"], "calls": info["calls"]},
                rule="research_spend_cap", judgment=Judgment.code)
            self._notify(job_id, "NEEDS_YOU", "Steward: needs you",
                         f"research spend cap ${info['cap_usd']:.2f} reached "
                         f"(${info['spent_usd']:.2f}); raise: steward research-cap {job_id[:8]} <usd>")

    def _deny_permission(self, job_id: str, record: dict, *, what: str, evidence: dict) -> None:
        approvals_module.decide(self.store, job_id, record["id"], "deny", decided_by="code")
        self.store.append_event(job_id, what=what, evidence=evidence, rule="research_spend_cap", judgment=Judgment.code)

    def _auto_approve_permission(self, job_id: str, record: dict, *, what: str, rule: str, evidence: dict,
                                 single: dict | None = None) -> None:
        """Code allows the request. `single` (the request's own facts): the whole chain is ONE `what` event, written
        when the answer's delivery result is known (TUI speed plan 10b); without it the full chain is recorded."""
        if single is not None:
            approvals_module.decide(self.store, job_id, record["id"], "approve", decided_by="code",
                                    single_event={"what": what, "rule": rule, "evidence": {**single, **evidence}})
            return
        approvals_module.decide(self.store, job_id, record["id"], "approve", decided_by="code")
        self.store.append_event(job_id, what=what, evidence=evidence, rule=rule, judgment=Judgment.code)

    def _unconsumed_at_done(self, job_id: str, frame) -> list[int]:
        """Steering revisions the model had not consumed when it produced this `done`.

        Latency #2: a current shim names them itself (`unconsumed_revs`, plus `last_rev`, the highest rev it had
        received). That is authoritative and immediate, so the gate never waits on the runner's own tick-lagged
        `acknowledged` receipt: a rev the shim had received and replayed is consumed (and recorded so now); one it
        has not replayed, or never received (sent after the done was produced), or not yet sent at all, is not.
        An older shim (no `unconsumed_revs`) falls back to the recorded receipts."""
        steering = Steering(self.store)
        instructions = steering.all_instructions(job_id)
        if "unconsumed_revs" not in frame.body:
            return [i["rev"] for i in instructions if i["status"] in ("received", "delivered")]
        pending = {int(r) for r in frame.body.get("unconsumed_revs") or []}
        last_rev = int(frame.body.get("last_rev") or 0)
        unacked = []
        for i in instructions:
            if i["status"] == "acknowledged":
                continue
            if i["status"] == "received" or i["rev"] in pending or i["rev"] > last_rev:
                unacked.append(i["rev"])
            else:
                steering.mark_acknowledged(job_id, i["rev"], "acknowledged",
                                           evidence={"seq": frame.seq, "via": "done_frame_unconsumed_revs"})
        return unacked

    def _fabric_worker_finished(self, job_id: str, events: tuple) -> bool:
        """A `done` frame is a YIELD (the turn ended), never a completion by itself (SRD-01). Each one is classified
        by code (`steward.yield_lanes`); only a CANDIDATE sets WORKER_FINISHED, and the return value says whether
        it did, so the caller runs closeout in the same tick (SP-16, SP-17). An `error` frame is a dead worker and
        keeps its recovery path."""
        for frame in events:
            if frame.type not in ("done", "error"):
                continue
            subtype = frame.body.get("result_subtype") or frame.body.get("status")
            errored = bool(frame.type == "error" or frame.body.get("is_error")
                           or (subtype is not None and subtype != "success"))
            if errored:
                # A harness turn can end with a typed result while reporting an API/runtime error. The turn ended;
                # the job did not. Keep the worker in RUNNING so normal liveness/recovery can resume the unfinished
                # outcome, and never send an errored result to closeout (SRD-05).
                self.store.append_event(
                    job_id,
                    what="worker turn failed; completion remains open",
                    evidence={"sha": frame.body.get("sha") or frame.body.get("candidate_sha"),
                              "seq": frame.seq, "result_subtype": subtype,
                              "is_error": bool(frame.body.get("is_error"))},
                    rule="fabric_turn_error",
                    judgment=Judgment.code,
                )
            if frame.type == "error":  # a dead worker: liveness/recovery replaces it; not a yield
                return False
            unacked = [] if errored else self._unconsumed_at_done(job_id, frame)
            return self._classify_yield(job_id, seq=frame.seq, frame_sha=frame.body.get("sha"), errored=errored,
                                        unacked=unacked)
        return False

    STOP_FACT_DELAY_S = 120.0
    STOP_MIN_CONFIDENCE = 0.7
    _STOP_DUE_KEY = "stop_fact_due"
    _YIELD_FACT_SEQ_KEY = "yield_fact_seq"
    _LEGACY_YIELD_FP_KEY = "legacy_yield_fp"

    def _classify_yield(self, job_id: str, *, seq, frame_sha=None, errored: bool = False, unacked=(),
                        legacy: bool = False) -> bool:
        """Classify one worker turn end (SRD-01..05). True only for a CANDIDATE (WORKER_FINISHED was set)."""
        if not errored:
            # The committed-questions scan normally lands a tick later; classify must see the questions committed
            # at this HEAD or an unanswered blocking question reads as a CANDIDATE (live proof 2026-10-06).
            self._scan_questions_for_yield(job_id)
        cls = yield_lanes.classify(self.store, job_id, run=self.runner_exec.run, worktree=self._worktree(job_id),
                                   start_sha=self._diff_base_sha(job_id), unacked=unacked, errored=errored)
        if legacy and cls.lane != yield_lanes.CANDIDATE:  # the marker stays in the pane's tail: classify each state once
            fp = f"{cls.lane}|{cls.sha}|{','.join(cls.failed_preconditions)}"
            if self.store.kv_get(job_id, self._LEGACY_YIELD_FP_KEY) == fp:
                return False
            self.store.kv_set(job_id, self._LEGACY_YIELD_FP_KEY, fp)
        evidence = {"lane": cls.lane, "sha": cls.sha or frame_sha, "seq": seq,
                    "failed_preconditions": cls.failed_preconditions}
        if cls.untracked:  # RC-4: untracked files never block a done, but the TL can still see them
            evidence["untracked"] = cls.untracked
        if legacy:
            evidence["marker"] = "STEWARD-DONE"
        if cls.lane == yield_lanes.STOPPED:
            said = self._last_said_text(job_id)
            if said:
                evidence["last_said"] = " ".join(said.split())[:200]
        self.store.kv_set(job_id, self._STOP_DUE_KEY, None)  # a new yield supersedes any pending stop timer
        self.store.append_event(job_id, what="yield_classified", evidence=evidence, rule="yield_classification",
                                judgment=Judgment.code)
        if not legacy and pause_checkpoint.pause_mode(self.store, job_id) != "off":
            if cls.lane != yield_lanes.WAITING:  # spec 2026-10-13 §4.3: a background wait is code's; all else is a pause
                self._on_pause(job_id, seq, cls, unacked=list(unacked))
            return False
        if cls.lane == yield_lanes.CANDIDATE:
            self.store.set_state(
                job_id,
                JobState.WORKER_FINISHED,
                what="worker reported done (typed frame)" if not legacy else "worker reported done",
                evidence=({"sha": frame_sha or cls.sha, "seq": seq, "run_id": decision_trace.run_id(self.store, job_id)}
                          if not legacy else {"marker": "STEWARD-DONE",
                                              "run_id": decision_trace.run_id(self.store, job_id)}),
                rule="fabric_done_frame" if not legacy else "steward_done_marker",
                judgment=Judgment.code,
            )
            return True
        if cls.lane == yield_lanes.ASKING:
            self._yield_fact(job_id, seq, "worker_asking", sha=cls.sha)
        elif cls.lane == yield_lanes.STOPPED:
            self._on_stopped(job_id, seq, cls, unacked=list(unacked))
        return False

    def _scan_questions_for_yield(self, job_id: str) -> None:
        """Run the questions scan for the current HEAD, synchronously, before the yield is classified. Never raises."""
        try:
            if not self._worktree(job_id).is_dir():
                return
            result = questions_module.scan(self.store, job_id, self.questions_runner)
            questions_delivery.notify_queue(self.store, self.notifier, job_id, result, link_fn=self._questions_link)
        except Exception as exc:  # noqa: BLE001 - classification proceeds on whatever the store holds
            questions_module.record_error(self.store, job_id, f"{type(exc).__name__}: {exc}")

    def _on_stopped(self, job_id: str, seq, cls, *, unacked: list) -> None:
        if "unacked_steering" in cls.failed_preconditions:
            # The shim hands the steer to the agent, which starts a new turn and replays it (ack), then yields
            # again: nothing to ask or report yet (SP-9/SP-17; found in 11-03 acceptance).
            self.store.append_event(
                job_id, what="worker done deferred: steering not yet acknowledged",
                evidence={"sha": cls.sha, "seq": seq, "unacknowledged_revs": unacked},
                rule="no_closeout_with_unacknowledged_steering",
                judgment=Judgment.code)
            return
        if cls.label is not None:  # code already knows why (claims_done at a ruled SHA; errored): no model call
            self._yield_fact(job_id, seq, "worker_stopped", label=cls.label, sha=cls.sha,
                             failed=cls.failed_preconditions)
            return
        label = None
        if supervisors.jev_mode(self.store, job_id) != "off":
            got = self.jev.ask_stop(self.store, job_id, self._last_said_text(job_id) or "",
                                    {"failed_preconditions": cls.failed_preconditions, "seq": seq})
            if got is not None and got["confidence"] >= self.STOP_MIN_CONFIDENCE:
                label = got["choice"]
        if label is not None:
            self._yield_fact(job_id, seq, "worker_stopped", label=label, sha=cls.sha, failed=cls.failed_preconditions)
        else:  # Jev off / unavailable / unsure: the same fact after the timer, by code, never dropped (SRD-04)
            self.store.kv_set(job_id, self._STOP_DUE_KEY,
                              {"seq": seq, "due_at": self.clock() + self.STOP_FACT_DELAY_S, "sha": cls.sha,
                               "failed": cls.failed_preconditions})

    def _stop_fact_step(self, job_id: str) -> None:
        due = self.store.kv_get(job_id, self._STOP_DUE_KEY)
        if not isinstance(due, dict) or self.clock() < float(due.get("due_at") or 0):
            return
        self.store.kv_set(job_id, self._STOP_DUE_KEY, None)
        self._yield_fact(job_id, due.get("seq"), "worker_stopped", label="unlabeled", sha=due.get("sha"),
                         failed=due.get("failed") or [])

    def _yield_fact(self, job_id: str, seq, kind: str, **data) -> None:
        key = f"{kind}:{seq}"
        done = list(self.store.kv_get(job_id, self._YIELD_FACT_SEQ_KEY) or [])
        if seq is not None and key in done:
            return
        self.store.kv_set(job_id, self._YIELD_FACT_SEQ_KEY, (done + [key])[-50:])
        self.job_fact(job_id, "yield", kind, seq=seq, **data)

    def _last_said_text(self, job_id: str) -> str | None:
        try:
            if self.activity_fn is not None:
                activity = self.activity_fn(self.store, job_id)
            else:
                from steward import transcript
                activity = transcript.worker_activity(self.store, job_id, 40, None)
        except Exception:  # noqa: BLE001 - no transcript, no message: the stop question still goes out
            return None
        said = next((t for t in reversed(activity.get("turns") or []) if t.get("kind") == "said"), None)
        return str(said.get("text") or "") if said else None

    def _pause_init(self, job_id: str) -> dict:
        """The job's pause checkpoint, with the current unit defaulted to the contract's first unit."""
        cp = pause_checkpoint.load(self.store, job_id)
        if cp["unit_id"] is None:
            contract = load_contract(self.store, job_id)
            first = contract.units[0].id if contract is not None and contract.units else None
            if first:
                cp = pause_checkpoint.update(self.store, job_id, lambda c: c.update(unit_id=c["unit_id"] or first))
        return cp

    def _on_pause(self, job_id: str, seq, cls, *, unacked: list) -> None:
        """Spec 2026-10-13 §4.3/§5: a worker turn end (CANDIDATE, STOPPED for any reason, ASKING) is ONE tech-lead review
        request whose packet facts are the classifier's output. No Jev ask_stop, no stop timer; unacked steering does not
        block (the open instruction rides in the packet). A pause while a review is outstanding is folded into it."""
        now = self.clock()
        said = self._last_said_text(job_id) or ""
        report = pause_review.parse_report(said)
        _head, subject = pause_review.git_head_subject(self.runner_exec.run, self._worktree(job_id))
        cp = self._pause_init(job_id)
        unit_id, matched = pause_review.unit_for_pause(report, cp, subject)
        folded = bool(cp["awaiting_review"] or cp["review_in_flight"])
        data = {"seq": seq, "sha": cls.sha, "yield_lane": cls.lane, "failed": list(cls.failed_preconditions),
                "label": cls.label, "unit_id": unit_id, "unit_matched": matched,
                "done_claim": pause_review.is_done_claim(report, cls.label),
                "next_unit": report.next_unit if report else None,
                "next_title": report.next_title if report else "",
                "said": " ".join(said.split())[:1200], "pause_kind": "pause",
                "pause_seq": int(cp["pause_seq"]) + 1, "unacked": list(unacked)}

        def upd(c: dict) -> None:
            c.update(pause_seq=data["pause_seq"], last_pause_sha=cls.sha, last_pause_at=now, last_pause_fact=data,
                     done_claim=data["done_claim"], active_s=0.0, backstop_stage=0, unit_commit_sha=None,
                     unit_commit_at=None, waiting_on=None, owner_approval_id=None, owner_instruction=None,
                     owner_card_kind=None)
            if report is not None:
                seen = {u.get("id") for u in c["units_seen"]}
                for uid, title in ((report.unit, ""), (report.next_unit, report.next_title)):
                    if uid and uid not in seen:
                        c["units_seen"] = c["units_seen"] + [{"id": uid, "title": title}]
                        seen.add(uid)
            if not folded:
                c.update(awaiting_review=True, review_requested_at=now, force_provisional=False)

        pause_checkpoint.update(self.store, job_id, upd)
        if folded:
            pause_checkpoint.append_fact(self.store, job_id, source="runner", kind="pause_folded", at=now,
                                         text=f"worker paused again at {(cls.sha or '?')[:8]} ({cls.lane}) while a "
                                              f"review was outstanding: {data['said'][:200]}")
            self.store.append_event(job_id, what="pause_review_folded",
                                    evidence={"seq": seq, "sha": cls.sha, "pause_seq": data["pause_seq"]},
                                    rule="pause_review", judgment=Judgment.code)
            return
        self.job_fact(job_id, pause_review.SOURCE, "worker_paused", **data)
        self.store.append_event(job_id, what="pause_review_requested",
                                evidence={k: data[k] for k in pause_review.REQUEST_KEYS},
                                rule="pause_review", judgment=Judgment.code)

    def _pause_step(self, job_id: str, sig: heartbeat.Signals) -> bool:
        """Spec 2026-10-13 §4.3/§5/§7, once per tick: a `done` ruling at the current HEAD moves the job to WORKER_FINISHED
        (True: the caller runs closeout now); otherwise run the pause clock. pause=off: returns at once, writes nothing."""
        mode = pause_checkpoint.pause_mode(self.store, job_id)
        if mode == "off":
            return False
        now = self.clock()
        head, subject = pause_review.git_head_subject(self.runner_exec.run, self._worktree(job_id))
        cp = self._pause_init(job_id)
        if cp["done_sha"]:
            pause_checkpoint.update(self.store, job_id, lambda c: c.update(done_sha=None))
            if head == cp["done_sha"]:
                self.store.set_state(job_id, JobState.WORKER_FINISHED, what="pause review ruled done",
                                     evidence={"sha": head, "run_id": decision_trace.run_id(self.store, job_id)},
                                     rule="pause_review_done", judgment=Judgment.code)
                return True
            self.store.append_event(job_id, what="pause_done_stale", evidence={"ruled_sha": cp["done_sha"], "head": head},
                                    rule="pause_review", judgment=Judgment.code)
            pause_review.rerequest(self.store, job_id, now=now)
            return False
        if cp.get("waiting_on") == "owner" and pause_review.owner_decision_step(self.store, job_id, now=now):
            cp = pause_checkpoint.load(self.store, job_id)  # u5: the owner answered the card; the worker has its steer
        live = pause_review.review_live(self.store, job_id, self.arbiter)
        upd, actions = pause_review.timers(cp, now=now, turn_open=bool(sig.turn_open), head=head, subject=subject,
                                           mode=mode, review_live=live)
        cp = pause_checkpoint.update(self.store, job_id, lambda c: c.update(upd))
        for action in actions:
            self._pause_action(job_id, action, cp, head=head, now=now)
        return False

    def _pause_action(self, job_id: str, action: str, cp: dict, *, head: str | None, now: float) -> None:
        """One pause-clock action. Every steer here is origin `steward` and never urgent (spec §4.3)."""
        steering = Steering(self.store)
        seq = int(cp["pause_seq"])
        fact = cp["last_pause_fact"] or {}

        def ev(what: str, **evidence) -> None:
            self.store.append_event(job_id, what=what, evidence=evidence, rule="pause_review", judgment=Judgment.code)

        if action in ("provisional_continue", "provisional_abandon"):
            ruled = fact.get("unit_id") or cp["unit_id"] or "the current unit"
            nxt = fact.get("next_unit") or "the next unit you named"
            steering.record(job_id, "nudge", f"No tech-lead ruling yet on unit {ruled}. Continue provisionally with "
                            f"{nxt}; a later ruling can still send unit {ruled} back.",
                            request_id=f"pause-provisional-{job_id}-{seq}", origin="steward")
            abandon = action == "provisional_abandon"

            def upd(c: dict) -> None:
                if ruled not in c["provisional_units"]:
                    c["provisional_units"] = c["provisional_units"] + [ruled]
                c["unit_id"] = fact.get("next_unit") or c["unit_id"]
                if abandon:   # the review failed or was skipped: no late ruling will come
                    c["awaiting_review"] = False

            pause_checkpoint.update(self.store, job_id, upd)
            ev("pause_review_abandoned" if abandon else "pause_review_timeout", unit_id=ruled, pause_seq=seq,
               wait_s=round(now - float(cp["last_pause_at"] or now), 1))
        elif action == "soft_stop":
            sha = str(cp["unit_commit_sha"] or head or "")
            steering.record(job_id, "nudge", f"Unit {cp['unit_id'] or '(current)'} is committed ({sha[:8]}). Stop here "
                            "and report: end your turn with STEWARD-PAUSE unit=<id> next=<next id, or none when every "
                            "unit is done> <next title>.", request_id=f"pause-softstop-{job_id}-{sha[:12]}",
                            origin="steward")
            ev("pause_soft_stop", sha=sha, unit_id=cp["unit_id"])
        elif action == "nudge_commit":
            steering.record(job_id, "nudge", "20 active minutes without a pause: commit what is coherent and report at "
                            "your next safe point (STEWARD-PAUSE unit=<id> next=<id or none> <title>).",
                            request_id=f"pause-nudge-{job_id}-{seq}", origin="steward")
            ev("pause_backstop", stage=20, active_s=round(float(cp["active_s"]), 1))
        elif action == "snapshot_review":
            self.job_fact(job_id, pause_review.SOURCE, "pause_snapshot", sha=head, unit_id=cp["unit_id"],
                          pause_kind="snapshot", pause_seq=seq, done_claim=False, said="",
                          dirty=pause_review.git_dirty(self.runner_exec.run, self._worktree(job_id)))
            ev("pause_backstop", stage=35, active_s=round(float(cp["active_s"]), 1))
        elif action == "liveness_handoff":
            self.job_fact(job_id, pause_review.SOURCE, "urgent_fact", urgent=True,
                          text="no pause for 60 active minutes: liveness and stall recovery own the job now")
            ev("pause_backstop", stage=60, active_s=round(float(cp["active_s"]), 1))
        elif action == "rerequest_review":
            pause_review.rerequest(self.store, job_id, now=now)

    # -- starting / resuming -----------------------------------------------

    def start(self, job_id: str) -> None:
        """CONTRACTED -> RUNNING: dispatch the first attempt (SP-2)."""
        approvals_module.migrate(self.store, job_id)  # this runner owns the job now (TUI speed plan 10a)
        contract = load_contract(self.store, job_id)
        if contract is None:
            raise RuntimeError(f"job {job_id} has no working contract yet")
        if self._is_fleet_job(job_id):
            contract = self._pause_downgrade(job_id, contract, "fleet jobs are not pause-reviewed")
            if self.fleet_controller is None:  # fail closed: never the legacy single-worker path
                self.store.set_state(job_id, JobState.NEEDS_HUMAN, what="fleet runtime unavailable",
                                     evidence={"reason": "controller services not configured"},
                                     rule="fleet_runtime_unavailable", judgment=Judgment.code)
                return
            if self.trajectory is not None:
                TrajectoryLedger(self.store, job_id).set_intent(
                    contract.goal, "; ".join(contract.done_when), self.clock()
                )
            self._start_fleet(job_id)
            return

        if self.trajectory is not None:
            TrajectoryLedger(self.store, job_id).set_intent(
                contract.goal, "; ".join(contract.done_when), self.clock()
            )

        self.store.set_state(
            job_id,
            JobState.RUNNING,
            what="worker dispatch started",
            evidence={},
            rule="worker_dispatch",
            judgment=Judgment.code,
        )

        steering = Steering(self.store)
        worker = self.worker_factory(None)
        if not _is_fabric(worker):
            contract = self._pause_downgrade(job_id, contract,
                                             f"worker {type(worker).__name__} is not a fabric (Claude) worker")
        brief = render_brief(
            contract,
            steering.active_constraints(job_id),
            _handoff_dict(checkpoint.load(self.store, job_id)),
            None,
            fabric=_is_fabric(worker),
            parent=self.store.kv_get(job_id, "parent_brief"),
            efficiency_policy=efficiency_policy.brief_lines(self.store, job_id),
            grants=authority_module.render_lines(authority_module.effective(self.store, job_id)) or None,
            deadline=self._deadline_brief(job_id),
        )

        try:
            worker_ref = worker.start(brief, self._worktree(job_id))
        except Exception as exc:  # WorkerStartBlocked or a herdr failure: brief NOT delivered
            if type(exc).__name__ != "WorkerStartBlocked":
                raise
            pane = getattr(worker, "pane_id", None)
            self.store.set_state(
                job_id,
                JobState.NEEDS_HUMAN,
                what="worker startup blocked; brief not delivered",
                evidence={"detail": str(exc), "pane_id": pane},
                rule="worker_start_blocked",
                judgment=Judgment.code,
            )
            return
        attempt_no = lease.acquire(self.store, job_id, worker_ref)

        self._workers[job_id] = worker
        self._attempts[job_id] = attempt_no
        self._record_worker_location(job_id, worker)
        self._track_delivery(job_id, brief)
        approvals_module.close_tech_lead_superseded(self.store, job_id, "new worker attempt")
        self.store.append_event(
            job_id,
            what="worker attempt started",
            evidence={"attempt_no": attempt_no, "worker_ref": worker_ref},
            rule="worker_dispatch",
            judgment=Judgment.code,
        )
        self._notify(job_id, "RUNNING", "Steward: running", f"job {job_id[:8]} dispatched")

    def _pause_downgrade(self, job_id: str, contract, reason: str):
        """Pause review needs a single fabric (Claude) worker (plan decision): any other job runs with pause=off."""
        if contract.pause == "off":
            return contract
        contract = with_pause(contract, "off")
        self.store.kv_set(job_id, "contract", dataclasses.asdict(contract))
        self.store.append_event(job_id, what="pause_review_unsupported", evidence={"reason": reason},
                                rule="pause_review", judgment=Judgment.code)
        return contract

    def start_rework(self, job_id: str) -> None:
        """A sent-back job (`steward send-back`, NEEDS_HUMAN -> RUNNING) gets a NEW worker attempt in
        the SAME worktree/branch. The new session has no memory, so its brief carries the (updated)
        contract, every steering revision (`active_constraints` is all of them, including the one
        just recorded) and a REWORK section; those revisions are then marked acknowledged so the
        fabric "done deferred: steering not yet acknowledged" gate can never hold the new worker's
        `done` behind text it received in its brief rather than as a steer frame."""
        from steward import sendback

        approvals_module.migrate(self.store, job_id)  # this runner owns the job now (TUI speed plan 10a)

        marker = sendback.rework_pending(self.store, job_id)
        job = self.store.get_job(job_id)
        if marker is None or job is None or job.state != JobState.RUNNING:
            raise RuntimeError(f"job {job_id} has no pending rework")
        contract = load_contract(self.store, job_id)
        if contract is None:
            raise RuntimeError(f"job {job_id} has no working contract")

        # The old worker pane was closed when the job reached NEEDS_HUMAN (closeout / escalation);
        # release any still-held attempt so the single-writer lease lets the new one in.
        for attempt in self.store.kv_get(job_id, "attempts", default=[]):
            if attempt["status"] in ("running", "stopping") and not attempt["confirmed_stopped"]:
                lease.release(self.store, job_id, attempt["attempt_no"], confirmed=True)
        # Recovery budget stays as-is, unless it is already spent (the new attempt would be one
        # stall from escalating again): a human sending back is a fresh mandate, so restart it.
        if self.store.kv_get(job_id, "recoveries_used", default=0) >= self.policy.max_recoveries:
            self.store.kv_set(job_id, "recoveries_used", 0)
        self._signals.pop(job_id, None)
        self._last_change_at.pop(job_id, None)
        self.store.kv_set(job_id, "last_class", None)

        if self.trajectory is not None:
            TrajectoryLedger(self.store, job_id).set_intent(
                contract.goal, "; ".join(contract.done_when), self.clock()
            )
        self.store.append_event(
            job_id, what="worker dispatch started",
            evidence={"rework": True, "steering_rev": marker.get("rev")},
            rule="worker_dispatch", judgment=Judgment.code,
        )

        steering = Steering(self.store)
        worker = self.worker_factory(None)
        brief = render_brief(
            contract, steering.active_constraints(job_id),
            _handoff_dict(checkpoint.load(self.store, job_id)), None,
            fabric=_is_fabric(worker), rework=marker.get("text") or "(no text)",
            rework_by=marker.get("by"),
            grants=authority_module.render_lines(authority_module.effective(self.store, job_id)) or None,
            parent=self.store.kv_get(job_id, "parent_brief"),
            efficiency_policy=efficiency_policy.brief_lines(self.store, job_id),
            deadline=self._deadline_brief(job_id),
        )
        try:
            worker_ref = worker.start(brief, self._worktree(job_id))
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ != "WorkerStartBlocked":
                raise
            self.store.kv_set(job_id, sendback.REWORK_KEY, None)
            self.store.set_state(
                job_id, JobState.NEEDS_HUMAN, what="worker startup blocked; brief not delivered",
                evidence={"detail": str(exc), "pane_id": getattr(worker, "pane_id", None)},
                rule="worker_start_blocked", judgment=Judgment.code,
            )
            return
        attempt_no = lease.acquire(self.store, job_id, worker_ref)
        for instruction in steering.all_instructions(job_id):
            if instruction["status"] != "acknowledged":
                steering.mark_acknowledged(
                    job_id, instruction["rev"], "acknowledged", evidence={"via": "rework_brief"}
                )
        self.store.kv_set(job_id, sendback.REWORK_KEY, None)

        self._workers[job_id] = worker
        self._attempts[job_id] = attempt_no
        self._record_worker_location(job_id, worker)
        self._track_delivery(job_id, brief)
        approvals_module.close_tech_lead_superseded(self.store, job_id, "new worker attempt")
        self.store.append_event(
            job_id, what="worker attempt started",
            evidence={"attempt_no": attempt_no, "worker_ref": worker_ref, "rework": True},
            rule="worker_dispatch", judgment=Judgment.code,
        )
        self._notify(job_id, "RUNNING", "Steward: running", f"job {job_id[:8]} sent back; new attempt dispatched")

    _RESUMABLE_STATES = frozenset({JobState.RUNNING, JobState.PAUSED, JobState.STOPPING})

    def resume_all(self, job_ids: list[str] | None = None) -> list[str]:
        """Reattach to in-flight jobs after a controller restart (SP-9).

        `job_ids=None` (the default) discovers every RUNNING/PAUSED/STOPPING
        job via `Store.list_jobs` — a caller doesn't need to already know
        which jobs were in flight. Each candidate is only actually resumed if
        it has an attempt that isn't yet confirmed stopped. Resuming never
        starts a second writer (SP-14): if the last attempt IS confirmed
        stopped, resume is a no-op here and a fresh `start()` is the caller's
        job.
        """
        if job_ids is None:
            job_ids = [j.job_id for j in self.store.list_jobs(self._RESUMABLE_STATES)]

        resumed = []
        for job_id in job_ids:
            job = self.store.get_job(job_id)
            if job is None or job.state not in self._RESUMABLE_STATES:
                continue
            if self._is_fleet_job(job_id):
                # The fleet controller owns lane identities and recovery. Never ask the legacy worker factory to
                # reattach a job whose durable graph or opt-in marker selects fleet semantics.
                if self.fleet_controller is not None:
                    result = self.fleet_controller.reconcile(job_id)
                    if not result.get("ambiguous"):
                        resumed.append(job_id)
                continue
            if self.store.kv_get(job_id, "rework_pending"):
                continue  # sent back: its old attempt is dead; start_rework dispatches the new one
            attempts = self.store.kv_get(job_id, "attempts", default=[])
            current = next(
                (
                    a
                    for a in reversed(attempts)
                    if a["status"] in ("running", "stopping") and not a["confirmed_stopped"]
                ),
                None,
            )
            if current is None:
                continue
            worker = self.worker_factory(current["worker_ref"])
            approvals_module.migrate(self.store, job_id)  # this runner owns the job now (TUI speed plan 10a)
            self._resume_one(job_id, worker, current)
            self._workers[job_id] = worker
            self._attempts[job_id] = current["attempt_no"]
            resumed.append(job_id)
        return resumed

    def _resume_one(self, job_id: str, worker: WorkerAdapter, attempt: dict) -> None:
        """Records how a single attempt was reattached. Fabric workers use
        `attach()` (10-04): it verifies the shim is still alive at the
        recorded pane (Herdr's own signal, never this fresh object's
        memory), rebinds the shim's peer to this new runner's own pane, and
        resends its unacked outbox — never starting a second writer. On
        failure, the worker is left marked `exited` (set by `attach()`
        itself), so the very next `tick()`'s heartbeat classifies it `dead`
        and the existing bounded-recovery path replaces it (SP-14: a fresh
        pane, never a second writer at the old one). Every other adapter
        (`HerdrWorker`/`FakeWorker`) keeps the pre-10-04 behavior: just
        reattach and let the next `observe()` discover the real state."""
        if _is_fabric(worker):
            attached = worker.attach(attempt["worker_ref"])
            self.store.append_event(
                job_id,
                what="controller restart fabric reattach" if attached else "controller restart fabric reattach failed",
                evidence={"attempt_no": attempt["attempt_no"], "pane_id": attempt["worker_ref"], "attached": attached},
                rule="fabric_resume_attach",
                judgment=Judgment.code,
            )
            return
        from steward.herdr_worker import HerdrWorker

        if isinstance(worker, HerdrWorker):
            worker.worktree = self._worktree(job_id)
        self.store.append_event(
            job_id,
            what="controller restart resumed job",
            evidence={"attempt_no": attempt["attempt_no"], "worker_ref": attempt["worker_ref"]},
            rule="restart_resume",
            judgment=Judgment.code,
        )

    def _tick_fleet_job(self, job_id: str) -> TickReport:
        """Drive an opted-in job only through its fleet controller and closeout path. No controller wired: the job
        fails closed to the owner (never the legacy worker). Stop/pause/run use the one JobController."""
        job = self.store.get_job(job_id)
        if self.fleet_controller is None:
            if job.state not in (JobState.NEEDS_HUMAN, JobState.ACCEPTED, JobState.FAILED):
                self.store.set_state(job_id, JobState.NEEDS_HUMAN, what="fleet runtime unavailable",
                                     evidence={"reason": "controller services not configured"},
                                     rule="fleet_runtime_unavailable", judgment=Judgment.code)
            return TickReport(job_id, self.store.get_job(job_id).state.value, None, None,
                              "fleet controller runtime unavailable")
        if job.state == JobState.STOPPING:
            report = self._fleet_stop(job_id, job)
            self._record_fleet_outcomes(job_id)
            return report
        if job.state == JobState.PAUSED:
            self._fleet_pause(job_id)
            return TickReport(job_id, job.state.value, None, None, "paused; no dispatch")
        if job.state != JobState.RUNNING:
            return TickReport(job_id, job.state.value, None, None, f"no dispatch in state {job.state.value}")
        return self._fleet_tick(job_id)

    # -- the tick -----------------------------------------------------------

    def tick(self, job_id: str) -> TickReport:
        self._sweep_finished_check_runs()
        report = self._tick(job_id)
        housekeeping.maybe_sweep(self.store)  # Issue H: at most once a day (kv timestamp); never raises
        self._code_facts_step(job_id)  # SUP-06: the model-free floor runs after every tick, Jev up or down
        self._job_lead_step(job_id)    # SUP-09/13: the tech lead sees this tick's facts at once; never blocks
        return report

    def _sweep_finished_check_runs(self) -> None:
        """Cancel queued/running check runs whose waiting jobs are all finished (they would hold the heavy line)."""
        if not check_runs.enabled():
            return
        try:
            check_runs.sweep_finished(self.store, now=time.time())
        except Exception:  # noqa: BLE001 - SP-6: the sweep never stops the tick
            pass

    def _job_lead_step(self, job_id: str) -> None:
        if self.job_lead is None or self._fleet_wired(job_id):
            return
        try:
            self.job_lead.on_event(job_id)
        except Exception as exc:  # noqa: BLE001 - SP-6: the tech lead never stops the tick
            self.store.append_event(job_id, what="tech_lead_error", evidence={"error": f"{type(exc).__name__}: {exc}"[:200]},
                                    rule="tech_lead_exception", judgment=Judgment.code)

    def _worker_writing(self, job_id: str) -> bool:
        """The worker is a live writer: a turn open or a tool/process running, and its process alive."""
        sig = self._signals.get(job_id)
        return bool(sig is not None and sig.pid_alive is not False and (sig.turn_open or sig.running_process))

    def _code_facts_step(self, job_id: str) -> None:
        if self._fleet_wired(job_id):
            return  # a fleet job's facts are the controller's (v2.5 routes unchanged)
        try:
            try:
                code_facts.step(self.store, job_id, self.arbiter)
            except sqlite3.OperationalError as exc:
                if "database is locked" not in str(exc):
                    raise
                time.sleep(random.uniform(0.2, 0.8))  # issue O: another writer held the lock; retry once
                code_facts.step(self.store, job_id, self.arbiter)
        except Exception as exc:  # noqa: BLE001 - SP-6: a detector failure never stops the tick
            self.store.append_event(job_id, what="code_fact_error", evidence={"error": str(exc)[:200]},
                                    rule="code_fact_floor", judgment=Judgment.code)

    def job_fact(self, job_id: str, source: str, kind: str, **data) -> None:
        """Hand one job-level observation to the Arbiter (single-worker jobs; fleet jobs use `fleet_fact`).
        Never raises (a supervisor must not be able to break the tick)."""
        try:
            self.arbiter.submit(job_id, source, kind, **data)
        except Exception as exc:  # noqa: BLE001
            self.store.append_event(job_id, what="job_fact_error", evidence={"error": str(exc)[:200]},
                                    rule="arbiter_feed", judgment=Judgment.code)

    def _tick(self, job_id: str) -> TickReport:
        job = self.store.get_job(job_id)
        if job is None:
            raise KeyError(f"unknown job {job_id}")

        # (-1) v2.2 deadline supervision runs before every state early-return, so paused / waiting jobs still get
        # their plan, milestone and status facts recorded. Code only, never raises, never dispatches.
        self._deadline_step(job_id)

        # (0) A watcher's pause lease auto-resumes when it lapses (phase 15); a human's pause is never touched.
        if job.state == JobState.PAUSED and watcher_authority.expire_pause_lease(self.store, job_id, self.clock()):
            job = self.store.get_job(job_id)

        # (1) Pause/stop honoured at the top, before any dispatch (SP-11).
        if self._is_fleet_job(job_id):
            # A fleet job (graph or explicit opt-in) never falls through to the legacy single-worker path; without
            # a wired controller it fails closed to the owner.
            return self._tick_fleet_job(job_id)
        if job.state == JobState.STOPPING:
            return self._handle_stopping(job_id, job)
        if job.state == JobState.PAUSED:
            return TickReport(job_id, job.state.value, None, None, "paused; no dispatch")
        if job.state == JobState.WORKER_FINISHED and bg_verify.pending(self.store, job_id):
            return self._bg_verify_tick(job_id)  # SUP-02: consume (or keep waiting on) the background verification
        if job.state == JobState.CHECKS_PASSED and closeout.ruling_pending(self.store, job_id):
            return self._ruling_tick(job_id)  # SRD-06: green verification waits for ONE completion ruling
        if job.state != JobState.RUNNING:
            return TickReport(job_id, job.state.value, None, None, f"no dispatch in state {job.state.value}")

        worker = self._workers.get(job_id)
        if worker is None:
            return TickReport(
                job_id, job.state.value, None, None, "no attached worker; call start() or resume_all() first"
            )
        if lease.takeover_holder(self.store, job_id):
            # v2.6 (PRD D-02): a tech-lead takeover is the worktree's one writer; the worker is paused (no dispatch,
            # no delivery) until it releases. Jev keeps its own timer.
            self.jev_step(job_id)
            return TickReport(job_id, job.state.value, None, "takeover", "tech lead takeover in progress; worker paused")

        if job_id not in self._code_recorded:  # what code this runner loaded, for the outdated warning
            self._code_recorded.add(job_id)
            stability.record_runner_code(self.store, job_id)

        # (1.5) Relay any decided-but-unrelayed permission approval before
        # anything else this tick (SP-2: the runner alone touches worker
        # input; SP-19: only a human decision drives this, never a heuristic).
        self._relay_permission_decisions(job_id, worker)

        # (2) Deliver pending steering instructions. `mark_delivered` (from
        # adapter-send success) still runs for every adapter — it is what
        # takes an instruction out of `pending()`, so a fabric worker's
        # steer frame is sent exactly once per instruction rather than
        # resent every tick while waiting for the shim's own ack (measured
        # live 2026-09-27: without this, the agent saw the same steer twice).
        # The fabric path's `mark_acknowledged` (below, from typed ack
        # frames) still supersedes it for the "acknowledged" stage, which is
        # the receipt that actually matters (SP-9, SP-35) — "delivered" from
        # send success and "delivered" from the shim's own ack agree by the
        # time the agent has actually seen the frame either way.
        steering = Steering(self.store)
        fabric = _is_fabric(worker)
        self._check_run_wake_step(job_id)  # a finished Steward-owned check run becomes this tick's result steer
        self._deliver_pending_steers(job_id, worker)

        # (3) Collect signals, scan acks, check hard-constraint compliance.
        prev_sig = self._signals.get(job_id)
        sig, last_change_at = heartbeat.collect(
            worker,
            self._worktree(job_id),
            self.runner_exec,
            prev_sig,
            self.clock(),
            last_change_at=self._last_change_at.get(job_id),
        )
        if any(getattr(e, "v", 1) >= 2 for e in sig.events):
            # FLEET-09: this single-worker runner only speaks protocol v1; lane (v2) frames belong to the
            # fleet JobController and are never acted on here.
            self.store.append_event(job_id, what="v2_frame_ignored_by_legacy_runner",
                                    evidence={"count": sum(1 for e in sig.events if getattr(e, "v", 1) >= 2)},
                                    rule="v1_legacy_isolation", judgment=Judgment.code)
            sig = dataclasses.replace(sig, events=tuple(e for e in sig.events if getattr(e, "v", 1) < 2))
        self._signals[job_id] = sig
        self._last_change_at[job_id] = last_change_at
        watcher_authority.observe_tick(self.store, job_id, sig, self.clock())

        if not fabric:
            # Fabric's steer receipts come from typed ack frames (below), and
            # its `output_tail` is always empty — nothing here to scan.
            steering.scan_acks(job_id, sig.output_tail)
        steering.check_compliance(job_id, self._worktree(job_id), self.runner_exec)
        self._detect_break_glass(job_id, sig.output_tail)

        # (4) Classify; log only on a class change (no log spam, with hysteresis: `record_class`).
        cls = heartbeat.classify(sig, self.heartbeat_policy)
        record_class(self.store, job_id, cls)
        try:
            # u2: liveness counts only the worker's own changes, never this runner's first look after a (re)start
            prev_rec = self.store.kv_get(job_id, liveness.KEY)
            prev_progress = float(prev_rec.get("progress_ts") or 0.0) if isinstance(prev_rec, dict) else 0.0
            liveness.record_runner(self.store, job_id, turn_open=bool(sig.turn_open),
                                   progress_ts=liveness.worker_progress_ts(prev_sig, sig, now=self.clock(),
                                                                           previous=prev_progress),
                                   pid=os.getpid())
        except sqlite3.OperationalError:
            pass

        # Jev (SP-6) runs on its own 10s timer (`jev_step`, also driven from run_forever's sleep);
        # this is only a backstop for when the loop calls tick() directly.
        self.jev_step(job_id)
        self.hung_step(job_id)

        # Runtime qualification drift (CLI updated -> stale): rate-limited, one requalify per (runtime, version).
        if os.environ.get("STEWARD_RUNTIME_DRIFT", "1") != "0":
            try:
                if self.drift_watch is None:
                    self.drift_watch = fleet_drift.DriftWatch(
                        self.store, send_text=lambda text: notify.tg_send_text(text, runner=self.runner_exec),
                        clock=self.clock, background=True)
                self.drift_watch.check(job_id)
            except Exception as exc:  # noqa: BLE001 - a drift-check failure never breaks the tick
                self.store.append_event(job_id, what="runtime_drift_error", evidence={"detail": str(exc)[:200]},
                                        rule="runtime_drift", judgment=Judgment.code)

        # Held approvals: classify, hand to the tier's resolver, apply its answer (never blocks the tick).
        try:
            self.resolver.step(job_id)
        except Exception as exc:  # noqa: BLE001 - SP-6: a resolver failure leaves the approval for the owner
            self.store.append_event(job_id, what="resolver_error", evidence={"detail": str(exc)[:200]},
                                    rule="resolver_exception", judgment=Judgment.code)

        if self.trajectory is not None:
            self._feed_trajectory_ledger(job_id, sig)

        # (4.5) A permission-prompt wait opens a NEEDS_YOU approval the human
        # hasn't seen yet, once per prompt; the job stays RUNNING throughout
        # (SP-19: Steward keeps observing, this is not a NEEDS_HUMAN state).
        # (5) done -> WORKER_FINISHED -> closeout (verify + weakening).
        #
        # Fabric: both come from typed frames (`sig.events`) — no marker
        # regex, no output_tail parsing (WRK-03). Legacy: unchanged text path.
        self._stop_fact_step(job_id)
        if fabric:
            self._consume_fabric_events(job_id, steering, sig.events)
            self._flush_deferred_nudges(job_id)
            self.auto_interrupt.step(job_id, worker)
            if self._fabric_worker_finished(job_id, sig.events):
                return self._run_closeout(job_id, cls)
            if self._pause_step(job_id, sig):  # spec 2026-10-13: a TL `done` ruling hands the job to closeout
                return self._run_closeout(job_id, cls)
        else:
            self._handle_permission_prompt(job_id, sig)
            if "STEWARD-DONE" in sig.output_tail:
                # The legacy marker is a yield signal into the same classifier, never a direct completion (SRD-03).
                if self._classify_yield(job_id, seq=None, legacy=True):
                    return self._run_closeout(job_id, cls)

        # (6) Bounded recovery.
        contract = load_contract(self.store, job_id)
        context = {
            "goal": contract.goal if contract else "",
            "signals": _signals_context(sig),
            "output_tail": sig.output_tail,
            "handoff": _handoff_dict(checkpoint.load(self.store, job_id)),
            "cwd": str(self._worktree(job_id)),
        }
        action = self._stall_inflight_check(job_id, cls) or recovery.decide(
            self.store, job_id, cls,
            self.diagnoser if supervisors.sol_enabled(self.store, job_id) else None,  # sol off -> rules only
            context, self.policy, now=self.clock(),
        )
        detail = self._apply_action(job_id, worker, action, sig)

        if self.trajectory is not None and not self._tech_lead_owns_outer_loop(job_id):
            self._feed_trajectory_decision_facts(job_id, cls, action)
            try:
                self.trajectory.step(job_id)
            except Exception as exc:  # noqa: BLE001 - SP-6 Jev rule: supervisor failures never stop the tick
                self.store.append_event(
                    job_id,
                    what="trajectory_error",
                    evidence={"error": str(exc)},
                    rule="trajectory_supervisor_exception",
                    judgment=Judgment.code,
                )

        self._fleet_outer_step(job_id, cls, action)

        # Custom watchers (steward.custom_watchers): non-blocking; a failure is recorded, never fatal.
        try:
            self.custom_watchers.step(job_id)
        except Exception as exc:  # noqa: BLE001 - SP-6: supervisor failures never stop the tick
            self.store.append_event(
                job_id,
                what="watcher_error",
                evidence={"name": "*", "detail": str(exc)[:200]},
                rule="custom_watcher_exception",
                judgment=Judgment.code,
            )

        self._spend_step(job_id)
        self._questions_step(job_id)

        self._sync_herdr_state(job_id)
        final_job = self.store.get_job(job_id)
        return TickReport(job_id, final_job.state.value, cls.value, action.kind, detail)

    # -- fleet job (explicit `launch --fleet` only; see steward.fleet_live) -----------------------------------

    def _fleet_wired(self, job_id: str) -> bool:
        """A fleet job with a controller wired. Legacy runners (no controller) never touch the fleet tables."""
        return self.fleet_controller is not None and self._is_fleet_job(job_id)

    def _start_fleet(self, job_id: str) -> None:
        """CONTRACTED -> RUNNING for a fleet job: no single worker is dispatched; the controller admits lanes."""
        self.store.set_state(job_id, JobState.RUNNING, what="fleet dispatch started", evidence={},
                             rule="fleet_dispatch", judgment=Judgment.code)
        # Steering already pending at dispatch (e.g. the job's authority instruction from `new`) becomes a job-wide
        # constraint BEFORE any lane exists, so every lane carries it in its brief from birth instead of depending on
        # a live steer that a lane's runtime may not be qualified to acknowledge (the closeout waits on every receipt).
        self._fleet_deliver_steers(job_id)
        admitted = self.fleet_controller.admit(job_id)
        self.store.append_event(job_id, what="fleet lanes admitted",
                                evidence={"lanes": [{k: r.get(k) for k in ("task", "lane", "status", "reason")}
                                                    for r in admitted]},
                                rule="fleet_dispatch", judgment=Judgment.code)
        self._notify(job_id, "RUNNING", "Steward: running",
                     f"fleet job {job_id[:8]} dispatched ({sum(r.get('status') == 'started' for r in admitted)} lanes)")

    def resume_fleet(self, job_id: str) -> dict:
        """Controller restart: reattach live lanes, fence dead ones, block dispatch on ambiguity (never duplicate)."""
        out = self.fleet_controller.reconcile(job_id)
        self.store.append_event(job_id, what="fleet controller reconciled", evidence=out,
                                rule="fleet_restart_reconcile", judgment=Judgment.code)
        return out

    def _fleet_pause(self, job_id: str) -> None:
        ctl = self.fleet_controller
        if ctl.fence(job_id) is None:
            ctl.pause(job_id, f"pause-{uuid.uuid4().hex[:12]}")

    def _fleet_stop(self, job_id: str, job) -> TickReport:
        """Fence every lane and stop each; the parent is STOPPED only when no lane is left uncertain."""
        n = (self.store.kv_get(job_id, "fleet_stop_attempts") or 0) + 1
        self.store.kv_set(job_id, "fleet_stop_attempts", n)
        res = self.fleet_controller.stop(job_id, f"job-stop-{n}")
        if res["state"] != "stopped":
            return TickReport(job_id, job.state.value, None, None, f"fleet stop unconfirmed: {res['uncertain']}")
        self.store.set_state(job_id, JobState.STOPPED, what="fleet lanes stop confirmed",
                             evidence={"lanes": sorted(res["lanes"])}, rule="operator_stop_confirmed",
                             judgment=Judgment.code)
        push_module.notify_creator(self.store, job_id, "stopped", self.runner_exec)
        return TickReport(job_id, JobState.STOPPED.value, None, None, "stopped")

    def _fleet_deliver_steers(self, job_id: str) -> None:
        """Owner steering fans out to every live lane with its own receipt (a lane-less job-wide constraint is also
        given to lanes admitted later, through the lane brief)."""
        steering = Steering(self.store)
        for instruction in steering.pending(job_id):
            receipts = self.fleet_controller.steer(job_id, instruction["text"], request_id=f"rev-{instruction['rev']}",
                                                   rev=instruction["rev"])
            lanes = ",".join(f"{r['lane']}={r['stage']}" for r in receipts)
            steering.mark_delivered(job_id, instruction["rev"], DeliveryResult(
                ok=True, detail=f"fleet: {lanes or 'no live lane'}; job-wide constraint for every later lane"))

    def _fleet_integrate(self, job_id: str) -> list[dict]:
        """Serial integration through the one IntegrationWriter, once a candidate's lane stop is confirmed."""
        writer = self.integration_writer
        if writer is None:
            return []
        from steward import fleet_lease
        from steward.fleet_store import FleetStore
        fs = FleetStore(self.store)
        integrated = fs.integrated_tasks(job_id)
        for cand in fs.candidates(job_id):
            if cand["task_id"] in integrated:
                continue
            lease_row = fleet_lease.get(self.store, job_id, cand["lane_id"])
            if lease_row and lease_row["state"] == "stopped" and lease_row.get("stop_evidence"):
                return writer.integrate(job_id)
        return []

    _FINAL_BLOCKS = ("out_of_claim_paths", "candidate_not_based_on_dependencies", "task_not_in_graph")

    def _fleet_settled(self, job_id: str) -> str | None:
        """'integrated' when every required task is integrated and no lane is live; 'stuck' when a required task can
        no longer get there (exhausted/failed lane or a merge conflict, which no repair worker here resolves)."""
        from steward import fleet_lease
        from steward.fleet_store import FleetStore
        if fleet_lease.live(self.store, job_id):
            return None
        fs = FleetStore(self.store)
        view = fs.graph_view(job_id) or {}
        required = [t["id"] for t in view.get("tasks", []) if t["required"]]
        integrated = fs.integrated_tasks(job_id)
        if required and all(t in integrated for t in required):
            return "integrated"
        states = fleet_lease.task_states(self.store, job_id)
        hist = fs.integration_history(job_id)
        conflicted = {h["task_id"] for h in hist if h["outcome"] == "conflict"}
        # A block that the SAME candidate SHA can never get past (it wrote outside its claims, or is not based on its
        # dependencies) is final for that candidate: like a conflict, no repair worker here resolves it (v2.5 live
        # finding: the writer otherwise re-attempts the blocked candidate every tick and the job never settles).
        for h in hist:
            latest = fs.latest_candidate(job_id, h["task_id"])
            if (h["outcome"] == "blocked" and str(h["reason"]).startswith(self._FINAL_BLOCKS) and latest
                    and latest["candidate_sha"] == h["candidate_sha"]):
                conflicted.add(h["task_id"])
        if any(t not in integrated and (states.get(t) in ("exhausted", "failed") or t in conflicted) for t in required):
            return "stuck"
        return None

    def _fleet_tick(self, job_id: str) -> TickReport:
        """One fleet tick, all deterministic code: lanes (observe, frames, stop finished, bounded recovery, admit),
        serial integration, arbiter facts + the tech lead (the outer loop; the legacy trajectory redirect does not
        run for a fleet job), then the combined closeout once every required task is integrated."""
        ctl = self.fleet_controller
        fence = ctl.fence(job_id)
        if fence and fence.get("action") == "pause":  # the owner resumed the job: lift the controller fence
            ctl.resume(job_id, f"resume-{fence.get('request_id')}")
        if job_id not in self._code_recorded:  # what code this runner loaded, for the outdated warning
            self._code_recorded.add(job_id)
            stability.record_runner_code(self.store, job_id)
        self._fleet_deliver_steers(job_id)
        res = ctl.tick(job_id)
        admitted, recovered = res.get("admitted", []), res.get("recovered", [])
        for r in admitted:
            if r.get("status") == "started":
                self.fleet_fact(job_id, "lifecycle", "lane_started", lane=r.get("lane"))
        for r in recovered:  # each bounded recovery outcome, per lane
            self.fleet_fact(job_id, "recovery", str(r.get("status", "unknown")), lane=r.get("lane"),
                            reason=r.get("reason"))
        merged = self._fleet_integrate(job_id)
        self._record_fleet_outcomes(job_id)  # settled attempts only; idempotent
        self.relay_fleet_permission_decisions(job_id, ctl)  # owner answers for requests opened by the frame handler
        self.jev_step(job_id)  # per-lane Jev (its own 10 s per-lane timer), before the arbiter's facts are reviewed
        from steward import fleet_lease
        live = fleet_lease.live(self.store, job_id)
        observations = res.get("observations") or {}
        if observations:  # typed per-lane lifecycle/activity evidence; quiet time survives a runner restart
            cls, stalled = self._fleet_heartbeat(observations)
            stalled = self._fleet_stall_samples(job_id, stalled)
        else:
            cls, stalled = (heartbeat.Class.progressing if live else heartbeat.Class.idle), None
        action = recovery.Action(kind="recover" if recovered else "none",
                                 detail=f"{len(live)} live lane(s)", judgment=Judgment.code,
                                 rule="fleet_controller_tick")
        self._spend_step(job_id)
        self._fleet_outer_step(job_id, cls, action, stalled_lanes=stalled)
        detail = (f"fleet: {len(live)} live lane(s), {sum(r.get('status') == 'started' for r in admitted)} "
                  f"admitted, {sum(m.get('outcome') == 'integrated' for m in merged)} integrated")
        settled = self._fleet_settled(job_id)
        if settled == "integrated":
            self.store.set_state(job_id, JobState.WORKER_FINISHED, what="every required fleet task integrated",
                                 evidence={"integration_head": self.integration_writer and
                                           self.integration_writer.fleet.integration_head(job_id)},
                                 rule="fleet_all_required_integrated", judgment=Judgment.code)
            report = self._run_closeout(job_id, cls)
            if self.store.get_job(job_id).state == JobState.RUNNING and self.store.kv_get(job_id, "fleet_repair_active"):
                # FleetCloseout asked for a bounded repair; no repair worker is wired here, so the owner decides.
                self.fleet_closeout.repair_finished(job_id)
                self.store.set_state(job_id, JobState.NEEDS_HUMAN, what="fleet repair needs the owner",
                                     evidence={"reasons": self.store.kv_get(job_id, "fleet_closeout_last_block")},
                                     rule="fleet_repair_no_executor", judgment=Judgment.code)
                report = TickReport(job_id, JobState.NEEDS_HUMAN.value, cls.value, "closeout",
                                    "fleet closeout blocked; repair needs the owner")
            return report
        if settled == "stuck":
            self.store.set_state(job_id, JobState.NEEDS_HUMAN, what="fleet task cannot complete",
                                 evidence={"tasks": fleet_lease.task_states(self.store, job_id)},
                                 rule="fleet_required_task_stuck", judgment=Judgment.code)
            return TickReport(job_id, JobState.NEEDS_HUMAN.value, cls.value, action.kind, detail)
        return TickReport(job_id, self.store.get_job(job_id).state.value, cls.value, action.kind, detail)

    # -- fleet arbiter feed (Phase 29, FLEET-17) --------------------------------------------------------------

    def _tech_lead_owns_outer_loop(self, job_id: str) -> bool:
        if self.tech_lead is None or self.fleet_controller is None:
            return False
        from steward import fleet_runtime
        return fleet_runtime.is_fleet_job(self.store, job_id)

    def fleet_fact(self, job_id: str, source: str, kind: str, **data) -> None:
        """Hand one supervisor observation to the controller's arbiter. No-op unless a controller is wired and the
        job is a fleet job; never raises (a supervisor must not be able to break the tick)."""
        controller = self.fleet_controller
        if controller is None:
            return
        try:
            from steward import fleet_runtime
            if fleet_runtime.is_fleet_job(self.store, job_id):
                controller.arbiter.submit(job_id, source, kind, **data)
        except Exception as exc:  # noqa: BLE001
            self.store.append_event(job_id, what="fleet_fact_error", evidence={"error": str(exc)[:200]},
                                    rule="fleet_arbiter_feed", judgment=Judgment.code)

    def _record_fleet_outcomes(self, job_id: str) -> None:
        """Record any now-settled lane attempts; the writer is idempotent and leaves open attempts alone."""
        try:
            from steward import fleet_learning
            fleet_learning.record_job_outcomes(self.store, job_id)
        except Exception as exc:  # noqa: BLE001 - learning evidence must not break job control
            self.store.append_event(job_id, what="fleet_outcome_record_error",
                                    evidence={"error": f"{type(exc).__name__}: {exc}"[:200]},
                                    rule="fleet_learning_fail_open", judgment=Judgment.code)

    def _fleet_heartbeat(self, observations: dict) -> tuple[heartbeat.Class, list[str]]:
        """Classify only typed per-lane lifecycle/activity evidence; quiet process time survives Runner restart."""
        if not observations:
            return heartbeat.Class.idle, []
        now = self.clock()
        stall_s = self.heartbeat_policy.stall_s
        stalled = []
        for lane, obs in observations.items():
            if obs.get("lifecycle") != "working" or obs.get("activity"):
                continue
            try:
                last_activity_at = float(obs.get("last_activity_at", now))
            except (TypeError, ValueError):
                last_activity_at = now
            if now - last_activity_at >= stall_s:
                stalled.append(lane)
        stalled.sort()
        if any(obs.get("activity") for obs in observations.values()):
            return heartbeat.Class.progressing, []
        if any(obs.get("waiting") for obs in observations.values()):
            return heartbeat.Class.waiting, []
        if stalled:
            return heartbeat.Class.suspect_stall, stalled
        if any(obs.get("lifecycle") == "working" for obs in observations.values()):
            return heartbeat.Class.quiet_known_process, []
        if all(obs.get("lifecycle") == "exited" for obs in observations.values()):
            return heartbeat.Class.dead, []
        return heartbeat.Class.idle, []

    def _fleet_stall_samples(self, job_id: str, lanes: list[str]) -> list[str]:
        """Count a persistent quiet lane at most once per minute toward Arbiter's repeated-stall threshold."""
        key = "fleet_stall_sample_at"
        previous = self.store.kv_get(job_id, key, {})
        if not isinstance(previous, dict):
            previous = {}
        now = self.clock()
        selected = []
        for lane in lanes:
            try:
                last_sample = float(previous.get(lane, 0))
            except (TypeError, ValueError):
                last_sample = 0
            if now - last_sample >= 60.0:
                selected.append(lane)
        for lane in selected:
            previous[lane] = now
        for lane in list(previous):
            if lane not in lanes:
                previous.pop(lane, None)
        self.store.kv_set(job_id, key, previous)
        return selected

    def _fleet_outer_step(self, job_id: str, cls: heartbeat.Class, action: recovery.Action, *,
                          stalled_lanes: list[str] | None = None) -> None:
        """Inner-loop facts (heartbeat class, recovery decision), the efficiency finding and deadline risk go to the
        one arbiter; then the tech lead gets one chance to review on a material event. For a fleet job the tech
        lead IS the outer loop, so the legacy trajectory supervisor's redirect path is not run in parallel."""
        if self.fleet_controller is None:
            return
        from steward import fleet_runtime
        if not fleet_runtime.is_fleet_job(self.store, job_id):
            return
        if cls == heartbeat.Class.suspect_stall:
            if stalled_lanes is None:  # compatibility for direct/legacy callers without lane observations
                self.fleet_fact(job_id, "heartbeat", "stall")
            else:
                for lane in stalled_lanes:
                    self.fleet_fact(job_id, "heartbeat", "stall", lane=lane)
        elif self._fleet_last_cls.get(job_id) != cls:  # one fact per class CHANGE, not one per tick
            self._fleet_last_cls[job_id] = cls
            self.fleet_fact(job_id, "heartbeat", cls.value)
        if action.kind != "none":
            self.fleet_fact(job_id, "recovery", action.kind)
        try:
            dl = deadline_status.project(self.store, job_id)
        except Exception:  # noqa: BLE001
            dl = None
        if dl and dl.get("status") in ("unlikely", "at_risk") and dl.get("basis") not in (None, "none"):
            self.fleet_fact(job_id, "deadline", "deadline_risk", status=dl["status"])
        if self.tech_lead is not None:
            try:
                self.tech_lead.on_event(job_id)
            except Exception as exc:  # noqa: BLE001 - SP-6: the tech lead never stops the tick
                self.store.append_event(job_id, what="tech_lead_error", evidence={"error": str(exc)[:200]},
                                        rule="tech_lead_exception", judgment=Judgment.code)

    def _stall_inflight_check(self, job_id: str, cls: heartbeat.Class) -> recovery.Action | None:
        """Latency #6: `suspect_stall` goes straight to diagnosis (this same pass), but only after checking the
        in-flight batch. A legitimate wait, build/test or subagent call in flight is not a stall (d22d6633: an
        unblock steer said "no child process" during three legitimate wait loops): no diagnosis is spent, and
        the check is recorded once per batch. The hung-call detector still owns calls running far too long."""
        if cls != heartbeat.Class.suspect_stall:
            return None
        legit, labels = inflight.legitimate(self.store, job_id)
        if not legit:
            return None
        batch = inflight.current(self.store, job_id) or {}
        if self.store.kv_get(job_id, "stall_inflight_checked_gen") != batch.get("gen"):
            self.store.kv_set(job_id, "stall_inflight_checked_gen", batch.get("gen"))
            self.store.append_event(job_id, what="stall check: legitimate call in flight",
                                    evidence={"gen": batch.get("gen"), "labels": labels,
                                              "calls": [c.get("id") for c in batch.get("calls") or []]},
                                    rule="stall_inflight_legitimate", judgment=Judgment.code)
        return recovery.Action(kind="none", detail=f"suspect_stall with {', '.join(labels)} in flight; not a stall",
                               judgment=Judgment.code, rule="stall_inflight_legitimate")

    # -- stability checks (steward.stability) -------------------------------

    def _spend_step(self, job_id: str) -> None:
        """The efficiency watcher's step: the spend guard (a watcher costing more than twice the worker, or a job over
        the hourly limit; at most once a minute) plus the metrics and findings. Never raises."""
        finding = self.efficiency.step(job_id, self._signals.get(job_id))
        if finding:  # advisory only: evidence for the packet, never an action or a tech-lead trigger
            self.fleet_fact(job_id, "efficiency", "finding")

    def _questions_step(self, job_id: str, *, force: bool = False) -> None:
        """Record the questions the job has COMMITTED (steward.questions, G1): when the worker's HEAD moves, and at
        every closeout. A missing worktree or a scan failure never breaks the tick."""
        if not self._worktree(job_id).is_dir():
            return
        self._questions_flush(job_id)
        if not force:
            head = getattr(self._signals.get(job_id), "head_sha", "") or ""
            if not head or head == self.store.kv_get(job_id, questions_module.SCANNED_HEAD_KEY):
                return
        try:
            result = questions_module.scan(self.store, job_id, self.questions_runner, force=force)
            questions_delivery.notify_queue(self.store, self.notifier, job_id, result, link_fn=self._questions_link)
        except Exception as exc:  # noqa: BLE001 - scan() never raises, but a broken runner seam must not either
            questions_module.record_error(self.store, job_id, f"{type(exc).__name__}: {exc}")

    def _questions_link(self, job_id: str) -> str | None:
        """The phone tab's deep link for the questions notification (else the job's own tab)."""
        from steward import phone

        return phone.phone_link(self.runner_exec, self.store, job_id)

    def _questions_flush(self, job_id: str) -> None:
        """Fallback for the control pane's flusher (17-03, G7): a RUNNING or PAUSED job's own runner delivers the owner's
        pending answers as a steering revision once they are 60 s old, so they reach the worker whether or not a
        control pane tick runs. Both call the same idempotent `questions_delivery.flush`. Never raises."""
        try:
            job = self.store.get_job(job_id)
            if job is None or job.state not in (JobState.RUNNING, JobState.PAUSED):
                return
            if questions_delivery.pending(self.store, job_id):
                questions_delivery.flush(self.store, job_id, self.questions_runner, now=self.clock())
        except Exception as exc:  # noqa: BLE001 - a broken flush must not break the tick
            questions_module.record_error(self.store, job_id, f"flush {type(exc).__name__}: {exc}")

    MAX_COMMIT_NUDGES = 2

    def _commit_nudge(self, job_id: str, cls: heartbeat.Class) -> TickReport | None:
        """The worker said it is done but left uncommitted files: say "commit your work" and keep the job RUNNING
        instead of spending a verify round on a dirty-worktree bounce (3 of the audit's 11 bounces). At most
        `MAX_COMMIT_NUDGES` times; after that the normal verify path (and its round budget) decides."""
        if not self._worktree(job_id).is_dir():
            return None
        status = self.runner_exec.run(["git", "status", "--porcelain"], cwd=self._worktree(job_id))
        if status.exit_code != 0 or not status.stdout.strip():
            return None
        done = int(self.store.kv_get(job_id, "commit_nudges", default=0) or 0)
        if done >= self.MAX_COMMIT_NUDGES:
            return None
        files = [ln[3:].strip() for ln in status.stdout.splitlines() if ln.strip()]
        self.store.kv_set(job_id, "commit_nudges", done + 1)
        Steering(self.store).record(
            job_id, "nudge",
            "You declared done but have uncommitted changes (" + ", ".join(files[:6]) + "). Commit your work "
            "(git add + git commit) in this worktree, then end your turn.",
            origin="steward")
        self.store.set_state(
            job_id, JobState.RUNNING, what="uncommitted work at done; returning to running with a commit nudge",
            evidence={"nudge": done + 1, "files": files[:20]}, rule="commit_nudge_before_verify",
            judgment=Judgment.code)
        return TickReport(job_id, JobState.RUNNING.value, cls.value, "commit_nudge",
                          f"uncommitted work at done ({done + 1}/{self.MAX_COMMIT_NUDGES})")

    def _finish_line_gate(self, job_id: str, cls: heartbeat.Class) -> TickReport | None:
        """A done claim is checked against the goal's own finish check command. Not met -> ONE nudge and the job
        keeps RUNNING (at most `MAX_FINISH_BOUNCES` times, then NEEDS_HUMAN). No runnable check -> nothing here
        (Jev's `done_unproven` scores the prose finish line in shadow). Timed out / cannot run -> unverified:
        recorded, no bounce, and verification runs."""
        job = self.store.get_job(job_id)
        goal = job.goal if job else ""
        worktree = self._worktree(job_id)
        command = stability.finish_check_command(goal, worktree)
        if command is None:
            if stability.has_prose_finish_line(goal):
                self.store.kv_set(job_id, stability.FINISH_UNPROVEN_KEY, True)
            return None
        res = stability.run_finish_check(self.runner_exec, worktree, command)
        if res.unverified:
            # SUP-01: a check that cannot finish or cannot run proves nothing. No bounce, no NEEDS_HUMAN on that
            # alone: the real verification (verify_worker + closeout, in the background when long) decides.
            self.store.kv_set(job_id, stability.FINISH_UNPROVEN_KEY, True)
            if not res.runnable:
                self.store.append_event(
                    job_id, what="finish_check_unrunnable",
                    evidence={"command": command, "exit_code": res.exit_code, "output_tail": res.tail},
                    rule="finish_line_gate", judgment=Judgment.code)
            self.store.append_event(
                job_id, what="finish_check_unverified", evidence={"command": command, "reason": res.reason},
                rule="finish_line_gate", judgment=Judgment.code)
            return None
        if res.ok:
            self.store.append_event(job_id, what="finish_line_met", evidence={"command": command},
                                    rule="finish_line_gate", judgment=Judgment.code)
            if recovery._ownership_on():
                from steward import attempts

                attempts.progressed(self.store, job_id, _finish_issue(command), detail={"finish_line": "met"},
                                    now=self.clock())
            return None
        done = stability.finish_bounces(self.store, job_id)
        evidence = {"command": command, "exit_code": res.exit_code, "output_tail": res.tail,
                    "first_failing_line": res.first_failing_line, "bounce": done + 1,
                    "max": stability.MAX_FINISH_BOUNCES}
        self.store.append_event(job_id, what="finish_line_not_met", evidence=evidence,
                                rule="finish_line_gate", judgment=Judgment.code)
        if recovery._ownership_on():
            from steward import attempts

            issue = _finish_issue(command)
            attempts.fail_pending(self.store, job_id, issue,
                                  detail={"first_failing_line": res.first_failing_line}, now=self.clock())
        if done >= stability.MAX_FINISH_BOUNCES:
            reason = f"finish line still not met after {done} nudges: {res.first_failing_line}"
            self.store.set_state(
                job_id, JobState.NEEDS_HUMAN, what=reason[:200], evidence={"reason": reason, **evidence},
                rule="finish_line_bounces_exhausted", judgment=Judgment.code)
            if recovery._ownership_on():
                from steward import ownership

                ownership.request_strategy_review(self.store, job_id, blocker_id=None,
                                                  reason=f"finish line: {done} unproductive nudges "
                                                         f"({res.first_failing_line})",
                                                  signature=_finish_issue(command), now=self.clock())
            self._questions_step(job_id, force=True)
            self._notify_closeout_result(job_id, JobState.NEEDS_HUMAN)
            worker = self._workers.get(job_id)
            if worker is not None:
                self._close_fabric_worker_pane(job_id, worker)
            return TickReport(job_id, JobState.NEEDS_HUMAN.value, cls.value, "closeout", "finish line bounces exhausted")
        self.store.kv_set(job_id, stability.FINISH_BOUNCES_KEY, done + 1)
        Steering(self.store).record(job_id, "focus", stability.nudge_text(res.first_failing_line), origin="steward")
        if recovery._ownership_on():
            from steward import attempts
            from steward.actor import Actor, ActorRef

            attempts.record(self.store, job_id, issue_key=_finish_issue(command),
                            actor=ActorRef(Actor.CONTROLLER, "finish_line"),
                            hypothesis=f"the worker can make `{command}` pass; first failing line: "
                                       f"{res.first_failing_line}",
                            change="focus nudge naming the first failing line",
                            success_observation=f"`{command}` exits 0", evidence=[f"finish:{command}"],
                            now=self.clock())
        self.store.set_state(
            job_id, JobState.RUNNING, what="finish line not met; returning to running with a nudge",
            evidence={"bounce": done + 1, "first_failing_line": res.first_failing_line},
            rule="finish_line_bounced_to_running", judgment=Judgment.code)
        return TickReport(job_id, JobState.RUNNING.value, cls.value, "finish_bounce",
                          f"finish line not met ({done + 1}/{stability.MAX_FINISH_BOUNCES}): {res.first_failing_line}")

    # -- Jev: the 10s triage layer ------------------------------------------

    def jev_step(self, job_id: str) -> None:
        """One Jev check if this job's own 10s timer is due. Never raises, never blocks the tick. A fleet job has
        no single worker: each live lane is triaged on its own (v2.5, `jev_watch.FleetLaneTriage`)."""
        triage = getattr(self, "fleet_triage", None)
        if triage is not None and self._fleet_wired(job_id):
            try:
                triage.step(job_id)
            except Exception as exc:  # noqa: BLE001 - SP-6
                self.jev._unavailable(self.store, job_id, f"lane_watch_error:{type(exc).__name__}")
            return
        sig = self._signals.get(job_id)
        if sig is None or job_id not in self._workers:
            return
        try:
            self.jev_watch.step(job_id, sig)
        except Exception as exc:  # noqa: BLE001 - SP-6: an unavailable/buggy Jev never stops the controller
            self.jev._unavailable(self.store, job_id, f"watch_error:{type(exc).__name__}")

    def hung_step(self, job_id: str, *, force: bool = False) -> list[dict]:
        """Code-backed hung tool call / stuck permission detection (steward.hung_call). Never raises."""
        try:
            return self.hung_watch.step(job_id, _JevActions(self), force=force)
        except Exception as exc:  # noqa: BLE001 - SP-6: detection failure never stops the controller
            self.store.append_event(job_id, what="hung_call_error", evidence={"detail": f"{type(exc).__name__}: {exc}"[:200]},
                                    rule="hung_call_detector", judgment=Judgment.code)
            return []

    def _sleep_between_ticks(self, job_id: str, interval_s: float, sleep_fn: Callable[[float], None]) -> None:
        """Sleep `interval_s`, waking every time Jev's own timer is due so it runs every 10s
        regardless of the (default 15s) tick interval. Jev off -> one plain sleep.

        Fabric workers with the fast path on: wake every `FAST_POLL_S` to drain the shim mailbox (`pump`) and
        act on arrival, so neither a permission answer nor a steer waits for the next tick."""
        worker = self._workers.get(job_id)
        if worker is not None and _is_fabric(worker) and hasattr(worker, "pump") and fast_path_enabled():
            jev_on = interval_s > jev_watch_module.INTERVAL_S and self.jev_watch.enabled(job_id)
            elapsed = 0.0
            while elapsed < interval_s:
                self.pump(job_id)
                if jev_on:
                    self.jev_step(job_id)
                d = min(FAST_POLL_S, interval_s - elapsed)
                sleep_fn(d)
                elapsed += d
            return
        if interval_s <= jev_watch_module.INTERVAL_S or not self.jev_watch.enabled(job_id):
            sleep_fn(interval_s)
            return
        elapsed = 0.0
        while elapsed < interval_s:
            self.jev_step(job_id)
            d = min(max(self.jev_watch.until_due(job_id), 0.5), interval_s - elapsed)
            sleep_fn(d)
            elapsed += d

    # -- trajectory ledger feed (12-03, TRJ-09) -----------------------------

    def _feed_trajectory_ledger(self, job_id: str, sig: heartbeat.Signals) -> None:
        """Typed fabric frames (`sig.events`) feed the longitudinal ledger,
        independent of whatever else this tick does with them (approvals,
        acks, closeout) — empty for every non-fabric adapter, so this is a
        no-op there (12-CONTEXT D3/D4)."""
        ledger = TrajectoryLedger(self.store, job_id)
        now = self.clock()
        attempt_no = self._attempts.get(job_id, 0)
        for frame in sig.events:
            if frame.type == "done":
                ledger.add_milestone("done", ref=str(frame.body.get("sha") or ""), now=now)
                cost = frame.body.get("cost")
                if cost is not None:
                    ledger.add_cost_point(float(cost), attempt_no, source="worker", now=now)
            elif frame.type == "error":
                ledger.add_failed_approach("worker_error", ref=str(frame.body.get("exit_code", "")), now=now)
            elif frame.type == "event":
                kind = frame.body.get("kind")
                if kind == "plan":
                    ledger.add_plan(frame.body.get("items", []), now)
                elif kind == "test_result":
                    ledger.add_test_point(
                        frame.body.get("passed", 0),
                        frame.body.get("failed", 0),
                        frame.body.get("errors", 0),
                        frame.body.get("source", "worker_observed"),
                        now,
                    )
        self._maybe_sample_diff(job_id, ledger, sig, now)

    def _maybe_sample_diff(
        self, job_id: str, ledger: TrajectoryLedger, sig: heartbeat.Signals, now: float
    ) -> None:
        """Structural diff sample (12-CONTEXT D4/D5), only when the diff
        fingerprint actually moved since the last sample AND at least 60s
        have passed (heartbeat ticks are far more frequent than that)."""
        # `git diff` (the fingerprint) sees only uncommitted edits, so committed work never moved it and the
        # advisor was shown "0 files"; the head sha is part of the key so a commit triggers a sample.
        fp = f"{sig.diff_fingerprint}@{sig.head_sha}"
        last_fp = self.store.kv_get(job_id, "trajectory_last_sample_fingerprint")
        if fp == last_fp:
            return
        last_at = self.store.kv_get(job_id, "trajectory_last_sample_at")
        if last_at is not None and now - last_at < 60:
            return
        base_sha = self._diff_base_sha(job_id)
        if not base_sha:
            return
        sample = diffscan.scan(self._worktree(job_id), base_sha, self.runner_exec)
        ledger.add_diff_sample(sample, now)
        for path in sample.summary_commits:
            ledger.add_milestone("summary_commit", ref=path, now=now)
        if sample.weakening:
            ledger.add_acceptance_revision(
                "weakening detected: " + "; ".join(sample.weakening[:5]), "diff_sample", now
            )
        self.store.kv_set(job_id, "trajectory_last_sample_fingerprint", fp)
        self.store.kv_set(job_id, "trajectory_last_sample_at", now)

    def _diff_base_sha(self, job_id: str) -> str | None:
        """What the job's diff is measured against: the merge-base with the branch it will be incorporated into
        (jobdiff), so upstream commits merged into the job branch are not counted as the job's own change."""
        job = self.store.get_job(job_id)
        found = jobdiff.job_diff_base(self.store, job, self.runner_exec.run) if job is not None else None
        return found[0] if found else None

    def _feed_trajectory_decision_facts(self, job_id: str, cls: heartbeat.Class, action: recovery.Action) -> None:
        """The InnerVerdict (typed, recorded only on change), the D5
        `failed_approaches` facts a recovery action or a fresh stall
        diagnosis contributes, and a read-only fold of every Steering
        instruction (any origin) into the ledger's `decisions` section."""
        ledger = TrajectoryLedger(self.store, job_id)
        now = self.clock()

        diagnosed_this_tick = cls == heartbeat.Class.suspect_stall and action.rule != "stall_episode_diagnosis_cap"
        verdict = inner_verdict(cls, action, diagnosed_this_tick)
        record_if_changed(self.store, job_id, verdict)

        if action.kind in ("guide", "replace"):
            ledger.add_failed_approach(action.kind, ref=action.rule, now=now)
        if diagnosed_this_tick:
            ledger.add_failed_approach("stall_diagnosis", ref=action.rule, now=now)

        steering = Steering(self.store)
        last_rev = self.store.kv_get(job_id, "trajectory_last_decision_rev", default=0)
        max_rev = last_rev
        for instruction in steering.all_instructions(job_id):
            if instruction["rev"] <= last_rev:
                continue
            ledger.add_decision(
                "steering",
                instruction["text"],
                instruction["origin"],
                provenance_judgment(instruction["origin"]),
                f"rev={instruction['rev']}",
                now,
            )
            if instruction["verb"] == "goal":
                # 12-04 gate finding (D5 `acceptance_changed`: "a `goal` steer"
                # is itself one of the two ledger-facing signals this trigger
                # reads) — nothing fed `acceptance_revisions` from a human
                # goal steer before this; the trigger could never fire on one.
                ledger.add_acceptance_revision(instruction["text"], "human_goal_steer", now)
            max_rev = max(max_rev, instruction["rev"])
        if max_rev != last_rev:
            self.store.kv_set(job_id, "trajectory_last_decision_rev", max_rev)

    def _run_closeout(self, job_id: str, cls: heartbeat.Class) -> TickReport:
        """WORKER_FINISHED -> verify + weakening detect -> closeout decision.

        Discovery is re-run against the worktree (cheap, read-only) rather
        than persisted, since it never changes what a worker is authorized to
        run mid-job — only which check commands currently exist on disk.
        """
        if fleet_closeout_module.is_fleet(self.store, job_id):
            return self._run_fleet_closeout(job_id, cls)
        nudged = self._commit_nudge(job_id, cls)
        if nudged is not None:
            return nudged
        gate = self._finish_line_gate(job_id, cls)
        if gate is not None:
            return gate
        worktree = self._worktree(job_id)
        # a still-running background baseline would block this tick inside verify (bg_baseline.wait, up to hours):
        # verify in the background instead, so the runner stays responsive to steers (mechanics item 12)
        if (bg_verify.needed(self.store, job_id) or (bg_verify.enabled() and bg_baseline.running(self.store, job_id))) \
                and worktree.is_dir():
            started = self._start_bg_verify(job_id, cls)
            if started is not None:
                return started
        discovery = discover_repo(worktree)
        t0 = time.monotonic()
        try:
            verification = verify_worker(
                self.store, job_id, worktree, discovery, self.runner_exec,
                check_fn=check_runs.shared_check_fn(self.store, job_id, purpose="verify", wait=False, share=False,
                                                    predicted_s=bg_verify.predicted_s(self.store, job_id)))
        except check_runs.Queued as q:
            self.store.append_event(job_id, what="verification queued for the heavy slot; continuing in background",
                                    evidence={"run_id": q.run_id, "position": q.position, "eta_s": q.eta_s},
                                    rule="check_runs", judgment=Judgment.code)
            started = self._start_bg_verify(job_id, cls)
            if started is not None:
                return started
            verification = verify_worker(self.store, job_id, worktree, discovery, self.runner_exec)
        if not verification.dirty:
            self.store.kv_set(job_id, bg_verify.DURATION_KEY, round(time.monotonic() - t0, 1))
        return self._finish_closeout(job_id, cls, worktree, verification)

    def _start_bg_verify(self, job_id: str, cls: heartbeat.Class, *, requeue: bool = False) -> TickReport | None:
        """SUP-02: a candidate whose verification is predicted to exceed the tick budget is verified in the
        background at its exact SHA. A dirty worktree is left to the inline path (it returns at once, unrun).
        Any start that is not a lost-run re-queue opens a new loss streak: its re-queue allowance is reset (§5.5)."""
        worktree = self._worktree(job_id)
        status = self.runner_exec.run(["git", "status", "--porcelain"], cwd=worktree)
        if status.exit_code != 0 or status.stdout.strip():
            return None
        head = self.runner_exec.run(["git", "rev-parse", "HEAD"], cwd=worktree)
        sha = head.stdout.strip() if head.exit_code == 0 else ""
        if not sha:
            return None
        if not requeue and self.store.kv_get(job_id, VERIFY_REQUEUES_KEY):
            self.store.kv_set(job_id, VERIFY_REQUEUES_KEY, 0)
        bg_verify.start(self.store, job_id, worktree, sha, spawn=self.bg_verify_spawn)
        return TickReport(job_id, JobState.WORKER_FINISHED.value, cls.value, "verifying",
                          f"verifying in background at {sha[:12]}")

    def _set_running(self, job_id: str, *, what: str, evidence: dict, rule: str, judgment) -> None:
        """Return a job to RUNNING from the background-verification path. Idempotent: the job may already be
        RUNNING (a late baseline re-attribution or a resumed worker moved it first), and RUNNING -> RUNNING is not a
        legal transition -- record the event instead of crashing the runner."""
        from steward.models import JobState

        job = self.store.get_job(job_id)
        if job is not None and job.state == JobState.RUNNING:
            self.store.append_event(job_id, what=what, evidence={**evidence, "already_running": True}, rule=rule,
                                    judgment=judgment)
            return
        self.store.set_state(job_id, JobState.RUNNING, what=what, evidence=evidence, rule=rule, judgment=judgment)

    def _bg_verify_tick(self, job_id: str) -> TickReport:
        """WORKER_FINISHED with a background verification outstanding: never blocks. Done at the current HEAD ->
        closeout exactly as inline; the candidate moved -> discard and re-run; crashed -> unverified, NEEDS_HUMAN."""
        from steward.models import JobState

        cls = heartbeat.Class.idle
        worktree = self._worktree(job_id)
        head = self.runner_exec.run(["git", "rev-parse", "HEAD"], cwd=worktree)
        head_sha = head.stdout.strip() if head.exit_code == 0 else ""
        polled = bg_verify.record(self.store, job_id) or {}  # identifies the run this poll judges (lost re-queue)
        status, verification, reason = bg_verify.poll(self.store, job_id, head_sha)
        if status == "running":
            pending = Steering(self.store).pending(job_id)
            if pending:
                worker = self._workers.get(job_id)
                if worker is None:
                    # WORKER_FINISHED is not in resume_all's discovery set. Make the job explicitly resumable
                    # before trying to reattach; a restart can now recover its durable worker attempt.
                    self._set_running(job_id,
                                         what="verification pre-empted for worker recovery; reattach pending",
                                         evidence={"revisions": [i["rev"] for i in pending], "worker_attached": False},
                                         rule="verification_recovery_pending", judgment=Judgment.code)
                    resumed = self.resume_all([job_id])
                    worker = self._workers.get(job_id) if resumed else None
                    attach_events = [e for e in self.store.events(job_id) if e.rule == "fabric_resume_attach"]
                    attached = not attach_events or bool(attach_events[-1].evidence.get("attached"))
                    if worker is None or not attached:
                        # Keep the durable verification record and resumable lifecycle aligned. A later tick can
                        # retry this reattach; do not leave RUNNING with no worker or discard the check result.
                        self.store.set_state(job_id, JobState.WORKER_FINISHED,
                                             what="worker reattach failed during verification recovery",
                                             evidence={"revisions": [i["rev"] for i in pending],
                                                       "verification_preserved": True},
                                             rule="verification_recovery_reattach_failed",
                                             judgment=Judgment.code)
                        self._workers.pop(job_id, None)
                        return TickReport(job_id, JobState.WORKER_FINISHED.value, cls.value, "recovery_pending",
                                          "worker reattach failed; verification and recovery remain pending")
                self._deliver_pending_steers(job_id, worker)
                instructions = Steering(self.store).all_instructions(job_id)
                remaining = [i for i in instructions if i["rev"] in {p["rev"] for p in pending}
                             and i["status"] != "delivered"]
                if remaining:
                    return TickReport(job_id, JobState.WORKER_FINISHED.value, cls.value, "recovery_delivery_pending",
                                      "recovery instruction not yet delivered; background verification remains pending")
                revisions = [i["rev"] for i in pending]
                bg_verify.consume(self.store, job_id, "preempted_for_recovery")
                check_runs.withdraw_job(self.store, job_id, now=time.time())  # its queued verify is moot now
                self._set_running(job_id,
                                     what="worker recovery instruction delivered; work is resumable",
                                     evidence={"revisions": revisions, "worker_attached": True},
                                     rule="verification_recovery_delivered", judgment=Judgment.code)
                return TickReport(job_id, JobState.RUNNING.value, cls.value, "recovery_delivered",
                                  "recovery instruction delivered; worker acknowledgment and progress remain pending")
            line = check_runs.status_line(self.store, job_id, now=time.time()) or "verifying in background"
            return TickReport(job_id, JobState.WORKER_FINISHED.value, cls.value, "verifying", line)
        if status == "stale":
            bg_verify.consume(self.store, job_id, "discarded")
            self.store.append_event(job_id, what="background verification discarded", evidence={"reason": reason},
                                    rule="bg_verify_stale_sha", judgment=Judgment.code)
            started = self._start_bg_verify(job_id, cls)
            if started is not None:
                return started
            return self._run_closeout(job_id, cls)
        if status in ("crashed", "lost"):
            lost = (self._lost_requeue(job_id, head_sha, reason, started_at=polled.get("started_at"))
                    if status == "lost" else None)
            if lost == "raced":
                return TickReport(job_id, JobState.WORKER_FINISHED.value, cls.value, "verifying",
                                  "lost background verification already re-queued by another tick")
            if lost == "requeued":
                started = self._start_bg_verify(job_id, cls, requeue=True)
                if started is not None:
                    return started
                return self._run_closeout(job_id, cls)  # a dirty worktree: the inline path handles it, as on stale
            bg_verify.consume(self.store, job_id, "lost" if lost else "crashed")
            if lost == "exhausted":
                self.store.set_state(
                    job_id, JobState.NEEDS_HUMAN, what="background verification lost twice; tests unverified",
                    evidence={"reasons": [f"tests unverified: {reason}"], "commit_sha": head_sha,
                              "requeues": VERIFY_REQUEUE_MAX},
                    rule="bg_verify_lost", judgment=Judgment.code)  # reviewable; the liveness ladder then asks
            else:
                self.store.set_state(
                    job_id, JobState.NEEDS_HUMAN, what="background verification crashed; tests unverified",
                    evidence={"reasons": [f"tests unverified: {reason}"], "commit_sha": head_sha},
                    rule="bg_verify_unverified", judgment=Judgment.code)
            self._notify_closeout_result(job_id, JobState.NEEDS_HUMAN)
            worker = self._workers.get(job_id)
            if worker is not None:
                self._close_fabric_worker_pane(job_id, worker)
            return TickReport(job_id, JobState.NEEDS_HUMAN.value, cls.value, "closeout",
                              f"background verification unverified: {reason[:120]}")
        if status == "done" and verification is not None:
            pending = Steering(self.store).pending(job_id)
            if pending:
                worker = self._workers.get(job_id)
                if worker is None:
                    self._set_running(job_id,
                                         what="completed verification held for steering recovery",
                                         evidence={"revisions": [i["rev"] for i in pending]},
                                         rule="verification_recovery_pending", judgment=Judgment.code)
                    resumed = self.resume_all([job_id])
                    worker = self._workers.get(job_id) if resumed else None
                    attach_events = [e for e in self.store.events(job_id) if e.rule == "fabric_resume_attach"]
                    attached = not attach_events or bool(attach_events[-1].evidence.get("attached"))
                    if worker is None or not attached:
                        self._workers.pop(job_id, None)
                        self.store.set_state(job_id, JobState.WORKER_FINISHED,
                                             what="completed verification retained while reattach is pending",
                                             evidence={"revisions": [i["rev"] for i in pending],
                                                       "verification_preserved": True},
                                             rule="verification_recovery_reattach_failed",
                                             judgment=Judgment.code)
                        return TickReport(job_id, JobState.WORKER_FINISHED.value, cls.value, "recovery_pending",
                                          "verification finished, but pending steering was not delivered; retrying recovery")
                self._deliver_pending_steers(job_id, worker)
                remaining = [i for i in Steering(self.store).all_instructions(job_id)
                             if i["rev"] in {p["rev"] for p in pending} and i["status"] != "delivered"]
                if remaining:
                    return TickReport(job_id, JobState.WORKER_FINISHED.value, cls.value,
                                      "recovery_delivery_pending",
                                      "verification finished, but pending steering was not delivered")
                revisions = [i["rev"] for i in pending]
                bg_verify.consume(self.store, job_id, "preempted_for_recovery")
                self._set_running(job_id,
                                     what="completed verification pre-empted by delivered steering",
                                     evidence={"revisions": revisions, "worker_attached": True},
                                     rule="verification_recovery_delivered", judgment=Judgment.code)
                return TickReport(job_id, JobState.RUNNING.value, cls.value, "recovery_delivered",
                                  "verification completed, but recovery instructions were delivered; worker outcome remains pending")
            bg_verify.consume(self.store, job_id, "consumed")
            if self.store.kv_get(job_id, VERIFY_REQUEUES_KEY):
                self.store.kv_set(job_id, VERIFY_REQUEUES_KEY, 0)  # a verification landed: the loss budget resets
            return self._finish_closeout(job_id, cls, worktree, verification)
        return TickReport(job_id, JobState.WORKER_FINISHED.value, cls.value, None, "no background verification")

    def _lost_requeue(self, job_id: str, head_sha: str, reason: str, *, started_at=None) -> str | None:
        """A LOST background verification (zero-touch §5.5): "requeued" (re-run it), "raced" (another tick already
        handled this loss), "exhausted" (the third loss: NEEDS_HUMAN `bg_verify_lost`, a reviewable verification_unverified
        park whose orphan the liveness ladder asks about), or None when re-queueing
        is off (legacy: the crashed path). The count is recorded, and the lost record consumed, in ONE write
        transaction before anything acts, so two racing ticks re-queue a given loss once (`started_at` names the run
        the caller polled: a record another tick already replaced with a fresh run is never counted again)."""
        if not (check_runs.enabled() and ownership.enabled()):
            return None
        now = time.time()
        check_runs.reconcile(self.store, now=now)
        with self.store.immediate() as tx:
            rec = tx.kv_get(job_id, bg_verify.KEY)
            if (not isinstance(rec, dict) or rec.get("status") not in ("running", "failed")
                    or (started_at is not None and rec.get("started_at") != started_at)):
                return "raced"  # consumed, or replaced by the re-queued run, since this tick polled it
            n = int(tx.kv_get(job_id, VERIFY_REQUEUES_KEY) or 0) + 1
            tx.kv_set(job_id, VERIFY_REQUEUES_KEY, n)
            tx.kv_set(job_id, bg_verify.KEY, {**rec, "status": "consumed", "outcome": "lost", "consumed_at": now})
        if n > VERIFY_REQUEUE_MAX:
            return "exhausted"
        self.store.append_event(job_id, what="verify_requeued",
                                evidence={"reason": reason[:200], "n": n, "max": VERIFY_REQUEUE_MAX,
                                          "commit_sha": head_sha},
                                rule="bg_verify_lost_requeue", judgment=Judgment.code)
        return "requeued"

    def _finish_closeout(self, job_id: str, cls: heartbeat.Class, worktree: Path, verification) -> TickReport:
        """The closeout decision for one verification (inline or background): weakening, closeout, notify."""
        if verification.new_failures:
            self._notify(
                job_id,
                "CHECKS_FAILED",
                "Steward: checks failing",
                f"{len(verification.new_failures)} check(s) still failing",
            )

        findings = []
        if not verification.dirty and not verification.new_failures:
            baseline_checks = self.store.get_checks(job_id, label="baseline")
            base_sha = baseline_checks[0]["commit_sha"] if baseline_checks else verification.commit_sha
            findings = weakening.detect(worktree, base_sha, self.runner_exec)

        contract = load_contract(self.store, job_id)
        try:  # v2.2 (DL-06): per-item evidence at this exact SHA for a deadline job; closeout reads it for that SHA
            deadline_evidence.evaluate_candidate(self.store, job_id, verification=verification, worktree=worktree,
                                                 runner=self.runner_exec,
                                                 now=datetime.fromtimestamp(self.clock(), timezone.utc))
        except Exception:  # noqa: BLE001 - no verdict means `deadline verification pending`, never READY
            pass
        final_state = closeout.closeout(self.store, job_id, verification, findings, contract)
        if getattr(verification, "commit_sha", None):  # SP-20/O13: a yield at this SHA again is claims_done, no re-verify
            self.store.kv_set(job_id, yield_lanes.LAST_RULED_KEY, verification.commit_sha)
        try:  # G4: the ETA priors learn from every finished job; a rebuild failure never touches the closeout
            eta_prior.rebuild(self.store)
        except Exception as exc:  # noqa: BLE001
            try:
                self.store.append_event(job_id, what="eta_error", evidence={"error": f"prior rebuild: {exc}"[:200]},
                                        rule="eta", judgment=Judgment.code)
            except Exception:  # noqa: BLE001
                pass

        if self.trajectory is not None and final_state == JobState.RUNNING and verification.new_failures:
            ledger = TrajectoryLedger(self.store, job_id)
            now = self.clock()
            ledger.add_milestone("closeout_round", ref="", now=now)
            ledger.add_failed_approach(
                "closeout_new_failures", ref=str(len(verification.new_failures)), now=now
            )

        return self._closeout_tail(job_id, cls, final_state)

    def _closeout_tail(self, job_id: str, cls: heartbeat.Class, final_state: JobState) -> TickReport:
        """What follows a closeout decision, whether it came from `closeout()` or from `decide_after_ruling`."""
        self._questions_step(job_id, force=True)  # NEEDS_HUMAN and ACCEPTED: what the job committed last
        self._deadline_step(job_id)  # v2.2: candidate report / facts right after closeout, before the runner exits
        self._notify_closeout_result(job_id, final_state)
        self._sync_herdr_state(job_id)  # before the worker pane closes below

        # A resting terminal state (ACCEPTED, NEEDS_HUMAN) closes the worker
        # pane (10-03 finding 3); `RUNNING` (a verification round sent the
        # job back for more work) must not — the worker is still live — and neither must CHECKS_PASSED (it waits
        # for its completion ruling, which may send the worker back).
        if final_state not in (JobState.RUNNING, JobState.CHECKS_PASSED):
            worker = self._workers.get(job_id)
            if worker is not None:
                self._close_fabric_worker_pane(job_id, worker)

        job = self.store.get_job(job_id)
        return TickReport(job_id, job.state.value, cls.value, "closeout", f"closeout -> {final_state.value}"
                          + (" (awaiting completion ruling)" if final_state == JobState.CHECKS_PASSED else ""))

    RULING_WAIT_CAP_S = 45 * 60  # a ruling nobody answers in this long is "no completion ruling" (P4), not a hang

    def _ruling_tick(self, job_id: str) -> TickReport:
        """CHECKS_PASSED with a completion ruling outstanding: act on it when it is in, else wait (the tech lead's review
        runs on its own thread after this tick). Never blocks on the lead."""
        status = closeout.ruling_pending(self.store, job_id)
        if status in ("requested", "in_review"):
            rec = self.store.kv_get(job_id, "completion_ruling") or {}
            if self.job_lead is None:
                closeout.mark_ruling_unavailable(self.store, job_id, "no tech lead is wired to this runner")
            elif rec.get("seen_at") is None:  # the runner's own clock (the request may be written by another process)
                self.store.kv_set(job_id, "completion_ruling", {**rec, "seen_at": self.clock()})
            elif self.clock() - float(rec["seen_at"]) > self.RULING_WAIT_CAP_S:
                closeout.mark_ruling_unavailable(self.store, job_id, "no completion ruling within "
                                                 f"{self.RULING_WAIT_CAP_S // 60} min")
            status = closeout.ruling_pending(self.store, job_id)
        if status in ("ruled", "unavailable"):
            final = closeout.decide_after_ruling(self.store, job_id)
            if final is not None:
                return self._closeout_tail(job_id, heartbeat.Class.idle, final)
        job = self.store.get_job(job_id)
        return TickReport(job_id, job.state.value, None, "awaiting_ruling", "awaiting completion ruling")

    def _run_fleet_closeout(self, job_id: str, cls: heartbeat.Class) -> TickReport:
        """Fleet job: the combined final SHA is the only verification subject (FleetCloseout). Fails closed to
        the human when no coordinator is wired, rather than verifying a lane or the legacy worktree."""
        coordinator = self.fleet_closeout
        if coordinator is None:
            self.store.set_state(job_id, JobState.NEEDS_HUMAN, what="fleet closeout unavailable",
                                 evidence={"reasons": ["no fleet closeout coordinator configured"]},
                                 rule="fleet_closeout_unavailable", judgment=Judgment.code)
            final = JobState.NEEDS_HUMAN
            detail = "fleet closeout coordinator unavailable"
        else:
            res = coordinator.run(job_id, load_contract(self.store, job_id))
            final = JobState(res["state"])
            detail = f"fleet closeout {res['status']} -> {final.value}"
            if res["status"] == "candidate":
                self._notify_closeout_result(job_id, final)
        job = self.store.get_job(job_id)
        return TickReport(job_id, job.state.value, cls.value, "closeout", detail)

    def _notify_closeout_result(self, job_id: str, final_state: JobState) -> None:
        if final_state == JobState.RELEASED:  # closed as "no change": nothing to merge, so no autopilot/delivery
            push_module.notify_creator(self.store, job_id, "no_change", self.runner_exec,
                                       facts={"reason": closeout.worker_reason_excerpt(self.store, job_id)})
            return
        if final_state == JobState.ACCEPTED:
            # Terminal-state tidy-up (USERLOOP-LOG bug 2c): the tab gets a
            # checkmark and `steward run` is about to exit; the worker pane
            # itself stays open for review.
            job = self.store.get_job(job_id)
            herdr_tabs.mark_accepted(self.store, job_id, job.goal if job else "", self.runner_exec)
            push_module.notify_creator(self.store, job_id, "accepted", self.runner_exec)
            self._notify(job_id, "READY", "Steward: ready", "pre-authorized local acceptance")
            autopilot_module.ensure_attempted(self.store, job_id, self.runner_exec, notify=self._notify)
            return
        if final_state != JobState.NEEDS_HUMAN:
            return

        # The question scan (Phase 17) appends its own events just before this; they say nothing about why we stopped.
        events = [e for e in self.store.events(job_id) if not e.what.startswith(("question_", "questions_"))]
        last = events[-1] if events else None
        reason = last.what if last else "acceptance unresolved"
        rule = last.rule if last else ""

        if rule == "acceptance_unresolved":
            # Checks passed; this is a positive "come take a look", not an
            # alarm — open the same acceptance approval `steward.approvals`
            # recognizes so `api.approve(..., decision="approve")` can drive
            # it straight through the existing human-only accept path.
            approvals_module.open_approval(
                self.store, job_id, approvals_module.ACCEPTANCE_QUESTION, kind="acceptance"
            )
            if last is not None and last.what == closeout.UNVERIFIED_WHAT:
                self._notify(job_id, "NEEDS_YOU", "Steward: needs you", "tests unverified: the check command is broken")
            else:
                self._notify(job_id, "READY", "Steward: ready", "checks passed; acceptance unresolved")
        else:
            record = approvals_module.open_approval(self.store, job_id, reason)
            self._notify(
                job_id,
                "NEEDS_YOU",
                "Steward: needs you",
                f"approval {record['id'][:8]}: {reason[:100]}",
            )

    def _handle_stopping(self, job_id: str, job) -> TickReport:
        worker = self._workers.get(job_id)
        confirmed = worker.stop() if worker is not None else True
        attempt_no = self._attempts.get(job_id)
        if attempt_no is not None:
            lease.release(self.store, job_id, attempt_no, confirmed=confirmed)
        if not confirmed:
            return TickReport(job_id, job.state.value, None, None, "stop_unconfirmed")

        self.store.set_state(
            job_id,
            JobState.STOPPED,
            what="worker stop confirmed",
            evidence={"attempt_no": attempt_no},
            rule="operator_stop_confirmed",
            judgment=Judgment.code,
        )
        # `HerdrWorker.stop()` always closes its own pane already; a
        # `FabricWorker` only does that on the grace-expiry hard fallback
        # (confirmed via an error/pane_exited frame otherwise leaves the pane
        # open showing an idle shell — 10-03 finding 3). This call is
        # idempotent and duck-typed, so it is a no-op for every other adapter.
        if worker is not None:
            self._close_fabric_worker_pane(job_id, worker)
        self._workers.pop(job_id, None)
        # The job's tab is closed after a grace by the control pane's `herdr_tabs.sweep`.
        self._sync_herdr_state(job_id)
        push_module.notify_creator(self.store, job_id, "stopped", self.runner_exec)
        return TickReport(job_id, JobState.STOPPED.value, None, None, "stopped")

    def _close_fabric_worker_pane(self, job_id: str, worker: WorkerAdapter) -> None:
        """Duck-typed (`close_pane`; only `FabricWorker` has it): closes the
        worker's own pane once a job settles into a terminal resting state
        (10-03 finding 3), and only a pane this worker actually created
        (SP-14) — `close_pane()` itself is idempotent, so calling this more
        than once for the same worker is harmless."""
        close_pane = getattr(worker, "close_pane", None)
        if close_pane is None or not close_pane():
            return
        self.store.append_event(
            job_id,
            what="worker pane closed",
            evidence={"pane_id": getattr(worker, "pane_id", None)},
            rule="worker_pane_closed",
            judgment=Judgment.code,
        )

    def _apply_action(self, job_id: str, worker: WorkerAdapter, action: recovery.Action, sig: heartbeat.Signals) -> str:
        if action.kind == "none":
            return action.detail

        if action.kind == "needs_human":
            self.store.set_state(
                job_id,
                JobState.NEEDS_HUMAN,
                what="recovery escalated to human",
                evidence={"rule": action.rule, "detail": action.detail},
                rule=action.rule,
                judgment=Judgment.code,
            )
            record = approvals_module.open_approval(self.store, job_id, action.detail)
            if action.rule == approvals_module.RECOVERY_RULE:
                # approving it resumes the job with a fresh budget (approvals.is_recovery_approval)
                approvals_module.annotate(self.store, job_id, record["id"], recovery_rule=action.rule)
            self._notify(
                job_id,
                "NEEDS_YOU",
                "Steward: needs you",
                f"approval {record['id'][:8]}: {action.detail[:100]}",
            )
            # Job d3477a2a: escalating is telling the owner, not killing the worker. Only a dead worker's pane closes;
            # a live (or unknown) one keeps running for the owner to look at.
            if sig.pid_alive is False or sig.lifecycle == "exited":
                self._close_fabric_worker_pane(job_id, worker)
            else:
                self.store.append_event(job_id, what="worker pane kept open",
                                        evidence={"pane_id": getattr(worker, "pane_id", None),
                                                  "pid_alive": sig.pid_alive, "lifecycle": sig.lifecycle},
                                        rule="needs_human_live_worker", judgment=Judgment.code)
            return action.detail

        if action.kind in ("guide", "replace"):
            self._notify_budget_if_low(job_id)

        if action.kind == "guide":
            if pause_checkpoint.pause_mode(self.store, job_id) != "off":  # spec 2026-10-13 §4.5: Sol reports a fact
                pause_checkpoint.append_fact(self.store, job_id, source="diagnosis", kind="stall_guidance",
                                             text=action.detail or "reconsider approach", at=self.clock())
                self.store.append_event(job_id, what="steer_folded_to_fact",
                                        evidence={"origin": "steward", "via": "recovery_guide",
                                                  "text": (action.detail or "")[:400], "urgent": False},
                                        rule="pause_review_single_writer", judgment=Judgment.code)
                return action.detail
            Steering(self.store).record(job_id, "nudge", action.detail or "reconsider approach", origin="steward")
            self._notify(job_id, "RECOVERY", "Steward: recovery", (action.detail or "retrying with guidance")[:140])
            return action.detail

        if action.kind == "replace":
            detail = self._replace_worker(job_id, worker, action, sig)
            self._notify(job_id, "RECOVERY", "Steward: recovery", detail[:140])
            return detail

        raise ValueError(f"unknown action kind: {action.kind}")

    def _notify_budget_if_low(self, job_id: str) -> None:
        if recovery._ownership_on():
            return  # recovery rounds request a strategy review rather than counting down for an owner
        recoveries_used = self.store.kv_get(job_id, "recoveries_used", default=0)
        if recoveries_used == self.policy.max_recoveries - 1:
            self._notify(
                job_id,
                "BUDGET",
                "Steward: budget",
                "1 recovery attempt remaining",
            )

    def _replace_worker(self, job_id: str, worker: WorkerAdapter, action: recovery.Action, sig: heartbeat.Signals) -> str:
        attempt_no = self._attempts.get(job_id, 0)
        handoff = checkpoint.from_observation(
            sig.output_tail,
            hypothesis=(action.diagnosis.hypothesis if action.diagnosis else "worker process exited"),
            next_action=action.detail,
            attempt_no=attempt_no + 1,
        )
        checkpoint.save(self.store, job_id, handoff)

        confirmed = worker.stop()
        lease.release(self.store, job_id, attempt_no, confirmed=confirmed)
        if not confirmed:
            # SP-14: never start a replacement until the previous attempt and
            # its children are confirmed stopped.
            return "replacement deferred: previous attempt not confirmed stopped"

        contract = load_contract(self.store, job_id)
        steering = Steering(self.store)
        new_worker = self.worker_factory(None)
        brief = render_brief(
            contract, steering.active_constraints(job_id), _handoff_dict(handoff), None,
            fabric=_is_fabric(new_worker),
            parent=self.store.kv_get(job_id, "parent_brief"),
            efficiency_policy=efficiency_policy.brief_lines(self.store, job_id),
            grants=authority_module.render_lines(authority_module.effective(self.store, job_id)) or None,
            deadline=self._deadline_brief(job_id),
        )

        worker_ref = new_worker.start(brief, self._worktree(job_id))
        new_attempt_no = lease.acquire(self.store, job_id, worker_ref)

        self._workers[job_id] = new_worker
        self._attempts[job_id] = new_attempt_no
        self._record_worker_location(job_id, new_worker)
        self._track_delivery(job_id, brief)
        self._signals.pop(job_id, None)
        self._last_change_at.pop(job_id, None)
        self.store.kv_set(job_id, "stall_episode_diagnosed", False)
        self.store.kv_set(job_id, "last_class", None)
        self.store.append_event(
            job_id,
            what="worker replaced",
            evidence={"attempt_no": new_attempt_no, "worker_ref": worker_ref},
            rule="worker_replacement",
            judgment=Judgment.code,
        )
        return f"replaced worker (attempt {new_attempt_no})"

    def run_forever(
        self,
        job_id: str,
        interval_s: float = 30,
        *,
        sleep_fn: Callable[[float], None] | None = None,
        max_ticks: int | None = None,
        self_restart: Callable[[str], object] | None = None,
    ) -> TickReport:
        """`self_restart(job_id)` runs at the safe point after each tick (none in progress, no relay write in
        flight): it may re-exec this process onto newer code (steward.selfrestart). None = the default restarter."""
        sleep_fn = sleep_fn or time.sleep
        if self_restart is None and max_ticks is None:  # bounded (test/once) runs never re-exec their process
            self_restart = selfrestart.SelfRestarter(self.store).maybe_restart
        ticks = 0
        done = self._terminal_exit(job_id)  # started (or self-restarted) onto an already-finished job
        if done is not None:
            return done
        report = self._tick_surviving_lock(job_id, sleep_fn)
        ticks += 1
        self._alerts(job_id)
        while report.state not in _DISPATCHABLE_TERMINAL_STATES or self._verifying(job_id) or self._awaiting_ruling(job_id):
            if max_ticks is not None and ticks >= max_ticks:
                return report
            done = self._terminal_exit(job_id)  # finished behind our back (accepted/stopped elsewhere): exit, no re-exec
            if done is not None:
                return done
            if self_restart is not None:
                self_restart(job_id)
            self._sleep_between_ticks(job_id, interval_s, sleep_fn)
            report = self._tick_surviving_lock(job_id, sleep_fn)
            ticks += 1
            self._alerts(job_id)
        return report

    def _tick_surviving_lock(self, job_id: str, sleep_fn: Callable[[float], None]) -> TickReport:
        """One tick that a locked/busy database cannot kill (2026-10-07: `database is locked` in
        watcher_authority.observe_tick escaped run_forever twice and left the runner pane a bare shell). The tick is
        retried with backoff, which is what `steward runner-restart` did by hand: ticks are built to resume after a
        runner dies mid-tick. Any other error still propagates."""
        attempt = 0
        while True:
            try:
                report = self.tick(job_id)
            except sqlite3.OperationalError as exc:
                if not _is_db_lock_error(exc):
                    raise
                delay = _DB_LOCK_RETRY_DELAYS[min(attempt, len(_DB_LOCK_RETRY_DELAYS) - 1)]
                attempt += 1
                print(f"runner {job_id[:8]}: database locked during a tick (attempt {attempt}); retrying in {delay:g}s",
                      file=sys.stderr, flush=True)
                sleep_fn(delay)
                continue
            if attempt:
                try:
                    self.store.append_event(job_id, what="tick_db_lock_retried", evidence={"attempts": attempt},
                                            rule="runner_survives_db_lock", judgment=Judgment.code)
                except sqlite3.OperationalError:
                    pass  # the record is garnish; the tick itself went through
            return report

    def _terminal_exit(self, job_id: str) -> TickReport | None:
        """A terminal job needs no runner: ACCEPTED first gets its (once-only) autopilot attempt, then the runner exits."""
        job = self.store.get_job(job_id)
        if job is None or job.state.value not in _DISPATCHABLE_TERMINAL_STATES:
            return None
        if job.state == JobState.WORKER_FINISHED and self._verifying(job_id):
            return None  # SUP-02: a background verification is outstanding; the tick consumes it
        if self._awaiting_ruling(job_id):
            return None  # SRD-06: the completion ruling is outstanding; the tick consumes it
        if job.state == JobState.WORKER_FINISHED:
            # closeout always moves the job on, so WORKER_FINISHED here means a previous runner died mid-closeout
            # (or never got there): run it now rather than exit and leave the job parked forever
            self.store.append_event(job_id, what="closeout_resumed", evidence={},
                                    rule="worker_finished_without_closeout", judgment=Judgment.code)
            self._run_closeout(job_id, heartbeat.Class.idle)
            job = self.store.get_job(job_id)
            if job is None or job.state.value not in _DISPATCHABLE_TERMINAL_STATES:
                return None  # closeout sent the job back to RUNNING: keep supervising it
        if job.state == JobState.ACCEPTED:
            autopilot_module.ensure_attempted(self.store, job_id, self.runner_exec, notify=self._notify)
        return TickReport(job_id, job.state.value, None, None, f"job is {job.state.value}; runner exits")

    def _awaiting_ruling(self, job_id: str) -> bool:
        return closeout.ruling_pending(self.store, job_id) is not None

    def _verifying(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        return bool(job and job.state == JobState.WORKER_FINISHED and bg_verify.pending(self.store, job_id))

    def _alerts(self, job_id: str) -> None:
        """Telegram alerts (default off; `[alerts] telegram = true` in steward.toml)."""
        alerts.check(self.store, job_id, self.runner_exec)


class _JevActions:
    """What an ACTIVE-mode Jev flag may trigger. Every route is an existing supervisor path
    with its own budgets; none of them steers the worker (SP-10)."""

    def __init__(self, runner: Runner):
        self.r = runner

    def rerelay(self, job_id: str, approval_id: str) -> str | None:
        """Send a decided permission answer again (the worker never acked it), and ask the shim what it is
        really waiting on so a request it no longer has is confirmed stale."""
        r = self.r
        worker = r._workers.get(job_id)
        if worker is None:
            return "no worker"
        approvals_module.clear_relayed(r.store, job_id, approval_id, rule="hung_call_rerelay")
        r._relay_permission_decisions(job_id, worker)
        if hasattr(worker, "query_permissions"):
            worker.query_permissions()
        return "re-relayed the pending permission answer"

    def doctor(self, job_id: str, evidence: dict | None = None) -> str | None:
        r = self.r
        r.fleet_fact(job_id, "jev", "flag", route="doctor")
        sig, worker = r._signals.get(job_id), r._workers.get(job_id)
        if sig is None or worker is None:
            return "no worker"
        if not supervisors.sol_enabled(r.store, job_id):
            return "doctor is off; skipped"
        contract = load_contract(r.store, job_id)
        context = {"goal": contract.goal if contract else "", "signals": _signals_context(sig),
                   "output_tail": sig.output_tail, "handoff": _handoff_dict(checkpoint.load(r.store, job_id)),
                   "cwd": str(r._worktree(job_id))}
        if evidence:
            context["hung_call"] = evidence
        action = recovery.decide(r.store, job_id, heartbeat.Class.suspect_stall, r.diagnoser, context, r.policy,
                                 now=r.clock())
        return f"{action.kind}: {r._apply_action(job_id, worker, action, sig)}"[:200]

    def request_diagnosis(self, job_id: str, evidence: dict) -> str | None:
        """Latency #6: a confident Jev flag asks for a diagnosis now. It checks the in-flight batch first (a
        legitimate wait/build is not a stall), and its only possible act is a non-urgent nudge: a verdict to
        replace the worker or escalate is recorded as `diagnosis_would_act`, never applied. Never interrupts and
        spends no recovery budget; it skips while a TL ruling is pending and shares the stall-episode cap (RC-3)."""
        r = self.r
        r.fleet_fact(job_id, "jev", "flag", route="request_diagnosis")
        sig = r._signals.get(job_id)
        if sig is None or job_id not in r._workers:
            return "no worker"
        legit, labels = inflight.legitimate(r.store, job_id)
        if legit:
            return f"in-flight batch is legitimate ({', '.join(labels)}); no diagnosis"
        if not supervisors.sol_enabled(r.store, job_id):
            return "doctor is off; skipped"
        pending = recovery.ruling_pending(r.store, job_id)  # RC-3: never steer over a pending TL ruling
        if pending:
            return f"ruling pending ({pending}); no diagnosis"
        try:
            exceptions.use(r.store, job_id, "supervisor-model", {"role": "sol"})
        except exceptions.UnregisteredPath:
            return "supervisor-model path unregistered; skipped"
        capped = recovery.jev_diagnosis_capped(r.store, job_id)
        if capped:
            return f"{capped}; no diagnosis"
        contract = load_contract(r.store, job_id)
        context = {"goal": contract.goal if contract else "", "signals": _signals_context(sig),
                   "output_tail": sig.output_tail, "handoff": _handoff_dict(checkpoint.load(r.store, job_id)),
                   "cwd": str(r._worktree(job_id)), "observer_flag": evidence}
        diagnosis = r.diagnoser.diagnose(context)
        r.store.append_event(job_id, what="stall diagnosis produced",
                             evidence={"verdict": diagnosis.verdict, "hypothesis": diagnosis.hypothesis,
                                       "requested_by": "jev", **evidence},
                             rule="jev_requested_diagnosis", judgment=Judgment.model)
        if diagnosis.verdict == "retry_with_guidance" and diagnosis.guidance:
            Steering(r.store).record(job_id, "nudge", diagnosis.guidance, origin="diagnosis")
            return "nudged: " + diagnosis.guidance[:120]
        r.store.append_event(job_id, what="diagnosis_would_act",
                             evidence={"verdict": diagnosis.verdict, "hypothesis": diagnosis.hypothesis,
                                       "requested_by": "jev"},
                             rule="jev_diagnosis_never_interrupts", judgment=Judgment.code)
        return f"recorded only: {diagnosis.verdict}"

    def permission(self, job_id: str) -> str | None:
        r = self.r
        worker, sig = r._workers.get(job_id), r._signals.get(job_id)
        perms = [a for a in approvals_module.list_approvals(r.store, job_id) if a["kind"] == "permission"]
        if worker is None or not perms:
            return None
        rec, now = perms[-1], r.clock()
        blocked = bool(sig is not None and (sig.permission_prompt or sig.lifecycle == "blocked"))

        def age(ts):
            try:
                return time.time() - datetime.fromisoformat(ts).timestamp()
            except (TypeError, ValueError):
                return 0.0

        if rec["status"] == "pending":
            if age(rec.get("created_at")) >= 120:
                r._notify(job_id, "NEEDS_YOU", "Steward: needs you", f"still waiting on you: {rec['question'][:100]}")
                return "re-notified the pending permission"
            return None
        if rec["status"] in ("approved", "denied"):
            if not rec.get("relayed_at"):
                r._relay_permission_decisions(job_id, worker)
                return "relayed a decided permission that never went out"
            if blocked and age(rec["relayed_at"]) >= 60:
                approvals_module.clear_relayed(r.store, job_id, rec["id"])
                r._relay_permission_decisions(job_id, worker)
                return "re-relayed the decision; worker is still blocked"
            return None
        if rec["status"] == "stale" and blocked:
            back = approvals_module.reopen(r.store, job_id, rec["id"])
            if back is not None and back["status"] != "pending":
                r._relay_permission_decisions(job_id, worker)
                return "reopened a stale permission with its answer and re-relayed it; worker is still blocked"
            r._notify(job_id, "NEEDS_YOU", "Steward: needs you", f"reopened: {rec['question'][:100]}")
            return "reopened a stale permission; worker is still blocked"
        return None

    def watcher(self, job_id: str, choice: str) -> str | None:
        r = self.r
        pick = _pick_watcher(custom_watchers_module.list_watchers(r.store, job_id), choice)
        if pick is not None:
            custom_watchers_module.request_run(r.store, job_id, pick["name"])
            return f"ran watcher {pick['name']} now"
        if r.trajectory is None:
            return "no watcher and no advisor; nothing to run"
        return "advisor: " + r.trajectory.request_review_now(job_id, f"observer flagged {choice}")

    def needs_you(self, job_id: str, text: str) -> str | None:
        self.r._notify(job_id, "NEEDS_YOU", "Steward: needs you", text[:140])
        return "notified NEEDS_YOU"

    def concern(self, job_id: str, choice: str, confidence: float) -> str | None:
        from steward import ownership

        text = f"observer flagged a risky action ({confidence:.2f})"
        if not ownership.enabled():
            return self.needs_you(job_id, f"{text} ({confidence:.2f}); please look")
        blocker, created = ownership.raise_signal_concern(self.r.store, job_id, source="jev", choice=choice,
                                                            text=text, confidence=confidence, now=self.r.clock())
        return f"candidate blocker {'raised' if created else 'repeat sighting'}: {blocker.blocker_id}"

    def tech_lead_fact(self, job_id: str, choice: str, evidence: dict) -> str | None:
        """SUP-05/07: a gated general Jev flag (`permission_confusion`, `thrash`) becomes one Arbiter fact for the
        tech lead. Nothing is steered here."""
        self.r.job_fact(job_id, "jev", choice, confidence=evidence.get("confidence"), mode=evidence.get("mode"))
        return "tech-lead fact queued"


_WATCHER_HINTS = {
    "looping": ("loop", "repeat", "stuck", "spin", "same", "progress", "stall"),
    "off_track": ("track", "goal", "scope", "drift", "focus", "plan", "on-task", "task"),
}


def _pick_watcher(watchers: list[dict], choice: str) -> dict | None:
    """The enabled custom watcher whose name/ask best matches the flag; else the first enabled one."""
    on = [w for w in watchers if w.get("enabled", True)]
    if not on:
        return None
    hints = _WATCHER_HINTS.get(choice, ())
    scored = [(sum(h in f"{w['name']} {w.get('ask', '')}".lower() for h in hints), -i, w) for i, w in enumerate(on)]
    return max(scored, key=lambda t: t[:2])[2]


def _is_fabric(worker: WorkerAdapter) -> bool:
    """Duck-typed dispatch to the typed-frame path (WRK-03): `FabricWorker`
    sets `kind = "fabric"`, mirroring `HerdrWorker.kind`
    (`_record_worker_location` already reads that attribute the same way).
    Never true for `FakeWorker`/`HerdrWorker`, so every legacy code path this
    guards stays byte-for-byte unchanged."""
    return getattr(worker, "kind", None) == "fabric"


def _handoff_dict(handoff: checkpoint.Handoff | None) -> dict | None:
    if handoff is None:
        return None
    return {
        "hypothesis": handoff.hypothesis,
        "commands_tried": handoff.commands_tried,
        "results": handoff.results,
        "next_action": handoff.next_action,
    }


def _finish_issue(command: str) -> str:
    return "finish_line:" + hashlib.sha256(command.encode()).hexdigest()[:10]


def _signals_context(sig: heartbeat.Signals) -> dict:
    return {
        "lifecycle": sig.lifecycle,
        "pid_alive": sig.pid_alive,
        "head_sha": sig.head_sha,
        "diff_fingerprint": sig.diff_fingerprint,
        "last_check_exit": sig.last_check_exit,
        "error_fingerprint": sig.error_fingerprint,
        "same_error_count": sig.same_error_count,
        "permission_prompt": sig.permission_prompt,
        "running_process": sig.running_process,
        "since_last_change_s": sig.since_last_change_s,
    }


_SAFE_FILTERS = ("tail", "head", "grep", "wc", "sort", "uniq", "cat", "echo", "true", "ls", "pwd")
_SHELL_HAZARDS = ("$(", "`", "<(", ">(", " > ", ">>", " < ")


def in_contract_command(command: str, authority: list[str], worktree) -> bool:
    """True when a Bash command is made only of contract-authorized check
    commands, harmless read-only filters, and `cd` into the job worktree.
    Anything else (new executables, redirects to files, substitutions) stays
    a human approval (SP-7: discovering a command authorizes only that)."""
    import re
    import shlex
    from pathlib import Path

    checks = [line[len("run: "):].strip() for line in authority if line.startswith("run: ")]
    if "pytest" in checks or any(c.endswith(" -m pytest") for c in checks):
        checks += ["python -m pytest", "python3 -m pytest"]
    if not checks:
        return False
    cleaned = command.replace("2>&1", " ")
    if any(h in cleaned for h in _SHELL_HAZARDS) or re.search(r"\d?>\s*\S", cleaned):
        return False
    root = Path(worktree).resolve()
    saw_check = False
    for segment in re.split(r"&&|\|\||;|\|", cleaned):
        segment = segment.strip()
        if not segment:
            continue
        try:
            words = shlex.split(segment)
        except ValueError:
            return False
        if words[0] == "cd":
            if len(words) != 2:
                return False
            target = Path(words[1]).expanduser()
            target = (root / target if not target.is_absolute() else target).resolve()
            if target != root and root not in target.parents:
                return False
            continue
        if any(segment == c or segment.startswith(c + " ") for c in checks):
            saw_check = True
            continue
        if words[0] in _SAFE_FILTERS:
            continue
        return False
    return saw_check


def wire_job_lead(runner: Runner, leads, *, invoke=None, takeover_invoke=None, policy_fn=None, start_bg=None,
                  memory=None, outage=None):
    """Wire the v2.6 tech lead for single-worker jobs into `runner` (SUP-09): the ordered leads (Astra, then Fable),
    an executor whose worker hooks are the runner's own, and the runner's Arbiter. Returns the JobTechLead."""
    from steward import fleet_tech_lead, notify, review_service
    from steward import tech_lead_actions as tla

    executor = tla.ActionExecutor(
        runner.store, runner_exec=runner.runner_exec, notify_fn=runner._notify, notifier=runner.notifier,
        worker_fn=lambda job_id: runner._workers.get(job_id), worker_writing_fn=runner._worker_writing,
        takeover_invoke=takeover_invoke or tla.invoke_takeover_via_runtime, start_bg=start_bg or tla.Background,
        clock=runner.clock)
    kw = {"invoke": invoke} if invoke is not None else {}
    runner.job_lead = fleet_tech_lead.JobTechLead(
        runner.store, leads, executor, arbiter=runner.arbiter, policy_fn=policy_fn, clock=runner.clock,
        start_bg=start_bg, worker_model_fn=lambda job_id: runner.store.kv_get(job_id, "worker_model"),
        memory=memory, outage=outage or review_service.ReviewOutage(
            runner.store, lambda text: notify.tg_send_text(text, runner=runner.runner_exec), clock=runner.clock), **kw)
    return runner.job_lead


def wire_control_lead(store, leads, *, send_back_fn, notify_fn=None, notifier=None, policy_fn=None, memory=None,
                      runner_exec=None, invoke=None, start_bg=None, outage=None, clock=time.time):
    """The control pane's tech lead for PARKED jobs (spec 2026-10-04 §4): the same JobTechLead and executor as the
    runner's, with no worker hooks (a parked job has no worker), no Arbiter (the parked pass sweeps), and the pane's
    own send-back handler as the reopen seam."""
    from steward import fleet_tech_lead
    from steward import tech_lead_actions as tla
    from steward.runner_exec import SubprocessRunner

    executor = tla.ActionExecutor(store, runner_exec=runner_exec or SubprocessRunner(), notify_fn=notify_fn,
                                  notifier=notifier,
                                  start_bg=start_bg or tla.Background, send_back_fn=send_back_fn, clock=clock)
    kw = {"invoke": invoke} if invoke is not None else {}
    return fleet_tech_lead.JobTechLead(store, leads, executor, arbiter=None, policy_fn=policy_fn,
                                       start_bg=start_bg, memory=memory, outage=outage, clock=clock,
                                       worker_model_fn=lambda job_id: store.kv_get(job_id, "worker_model"), **kw)
