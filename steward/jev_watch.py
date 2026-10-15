"""Jev as the always-on, free, 10-second triage layer.

Every `INTERVAL_S` seconds, for each RUNNING job whose observer mode is not
`off`, the runner builds a compact redacted context (`build_context`), asks Jev
one bounded question (`ok | stalled | hung_call | looping | off_track |
risky_action`) and gates the answer:

  non-ok choice, confidence >= 0.7, the SAME choice on 2 consecutive checks,
  and outside that choice's 5-minute cooldown  ->  a flag.

* shadow: the flag is only recorded (`jev_flag` with `would_escalate_to`).
* active: the flag is also routed to the existing supervisors
  (`ROUTES`): the doctor, the permission re-relay/re-open fix path, a custom
  watcher run (else an advisor review), or a NEEDS_YOU escalation. Jev never
  steers the worker and never decides anything itself.

v2.3 Flow (FLOW-01/04): when the job's independent Flow mode is not `off`, the typed Flow question rides the SAME
request. A qualifying friction answer becomes one bounded, redacted, deduplicated episode (`jev_observer.
record_episode`). Flow never feeds the triage gate, the legacy diagnosis request or any route; in shadow it stops
at the record. In active (phase 24) a newly committed active episode gets its ONE structured investigation
(`steward.flow_investigator`) off the observer thread; the outcome is stored. Back on the observer thread, an
accepted result is handed once to the deterministic controller (`steward.flow_actions`, phase 25), which may only
use existing guarded mechanisms.

`score` measures whether a flag was independently corroborated within
`AGREE_WINDOW_S` (a watcher/advisor/human found a problem too) so the owner can
decide from real precision when to go active.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from steward import approvals as approvals_module
from steward import config as config_module
from steward import eta_jev, hung_call, supervisors, transcript
from steward import flow_actions as flow_actions_module
from steward import flow_investigator as flow_investigator_module
from steward import jev_observer as jev_observer_module
from steward.contract import load_contract
from steward.diagnosis import redact
from steward.jev_observer import CHOICES, FLAG_CONFIDENCE, RISK_CHOICES, MAX_STATE_CHARS, JevObserver, _redact
from steward.masking import mask
from steward.models import JobState, Judgment
from steward.steering import Steering, _is_non_human
from steward.store import Store

INTERVAL_S = 10.0
STREAK_N = 2
COOLDOWN_S = 300.0
BACKOFF_AFTER = 3
BACKOFF_MAX_S = 300.0
AGREE_WINDOW_S = 600.0
STREAK_KEY = "jev_streak"
# Latency #6: a stalled/looping flag at >= 0.8 requests a diagnosis at once (no streak), at most once per job per
# 60 s, in shadow and active mode alike. A diagnosis only ever steers or records; it never interrupts.
DIAGNOSE_CHOICES = ("stalled", "looping")
DIAGNOSE_CONFIDENCE = 0.8
DIAGNOSE_EVERY_S = 60.0
DIAGNOSE_KEY = "jev_diagnosis_requested_at"
COOLDOWN_KEY = "jev_cooldown"

ROUTES = {
    "stalled": "doctor",
    "hung_call": "doctor+permission",
    "looping": "watcher",
    "off_track": "watcher",
    "risky_action": "needs_you",
    # v2.6 (SUP-05): general flags go to the Arbiter as tech-lead facts, in shadow and active alike (PRD D-03:
    # judgment-based fixes are active; the tech lead decides within policy and the hard floor)
    "permission_confusion": "tech_lead",
    "thrash": "tech_lead",
}
TECH_LEAD_CHOICES = ("permission_confusion", "thrash")


RISK_MIN_CONFIDENCE = 0.5
RISK_AFTER_S = 30.0  # asked once a held approval has waited this long (with the resolver's own start delay)


def risk_choice(observer: JevObserver, store: Store, job_id: str, held: str, rule_reason: str) -> str | None:
    """Jev's `risk: low|medium|high` for one held call (steward.resolver bumps a tier UP with it,
    never down), or None when Jev is off/unavailable/unsure. Advisory, authority none."""
    if supervisors.jev_mode(store, job_id) == "off":
        return None
    got = observer.ask_risk(store, job_id, held, rule_reason)
    if got is None or got["choice"] not in RISK_CHOICES or got["confidence"] < RISK_MIN_CONFIDENCE:
        return None
    return got["choice"]


class OutageAlert:
    """SUP-08 (v2.6): Jev is one shared service, so an outage is global. The first `jev_unavailable` of an outage
    (any code but the not-configured ones: disabled, no key, unregistered; e.g. TypeSafe's HTTP 402 billing error on job 14305aeb, which was recorded once and
    alerted nobody) sends ONE notice through `send_text` (production: `tg send` to the Notifications bot); it is
    deduped until Jev answers again, then ONE recovery notice. An undelivered alert is retried every `RETRY_S`,
    never dropped. The record lives in kv under the pseudo-job `KV_JOB` (status reads it: "unavailable since")."""

    KV_JOB = "_jev"
    KEY = "outage"
    RETRY_S = 300.0
    NOT_CONFIGURED = frozenset({"disabled", "no_api_key", "supervisor_model_unregistered"})

    def __init__(self, store: Store, send_text: Callable[[str], bool], *, clock: Callable[[], float] = time.time):
        self.store, self.send_text, self.clock = store, send_text, clock

    def _send(self, text: str) -> bool:
        try:
            return bool(self.send_text(text))
        except Exception:  # noqa: BLE001 - an alert failure is recorded as undelivered, never fatal
            return False

    def check(self, job_id: str, result: dict | None) -> None:
        now = self.clock()
        rec = self.store.kv_get(self.KV_JOB, self.KEY) or {}
        if result is not None:
            if rec.get("state") == "down":
                delivered = self._send(f"Steward: Jev is back (was unavailable since "
                                       f"{_hhmm(rec.get('since'))}, {rec.get('code')}).")
                self.store.kv_set(self.KV_JOB, self.KEY, {**rec, "state": "up", "recovered_at": now,
                                                          "recovery_delivered": delivered})
                self.store.append_event(job_id, what="jev_outage_recovered",
                                        evidence={"since": rec.get("since"), "code": rec.get("code"),
                                                  "delivered": delivered},
                                        rule="jev_outage_alert", judgment=Judgment.code)
            return
        status = str(self.store.kv_get(job_id, jev_observer_module.STATUS_KEY) or "")
        if not status.startswith("unavailable:"):
            return
        code = status.split(":", 1)[1]
        if code in self.NOT_CONFIGURED:
            return  # switched off on purpose / never configured here: status says so; not an outage
        if rec.get("state") == "down":
            if rec.get("alert_delivered") or now - float(rec.get("last_attempt") or 0) < self.RETRY_S:
                return
            rec = {**rec, "last_attempt": now, "alert_delivered": self._send(self._text(rec))}
            self.store.kv_set(self.KV_JOB, self.KEY, rec)
            return
        rec = {"state": "down", "since": now, "code": code, "job": job_id, "last_attempt": now}
        rec["alert_delivered"] = self._send(self._text(rec))
        self.store.kv_set(self.KV_JOB, self.KEY, rec)
        self.store.append_event(job_id, what="jev_outage_alert",
                                evidence={"code": code, "since": now, "delivered": rec["alert_delivered"]},
                                rule="jev_outage_alert", judgment=Judgment.code)

    @staticmethod
    def _text(rec: dict) -> str:
        return (f"Steward: Jev unavailable since {_hhmm(rec.get('since'))} ({rec.get('code')}). Triage is down; "
                "the code-fact floor keeps routing. One notice per outage.")


def _hhmm(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%H:%M")
    except (TypeError, ValueError, OSError):
        return "?"


def outage_status(store: Store) -> dict | None:
    """The current Jev outage record when Jev is down, else None."""
    rec = store.kv_get(OutageAlert.KV_JOB, OutageAlert.KEY)
    return rec if isinstance(rec, dict) and rec.get("state") == "down" else None


class Actions(Protocol):
    """What an active-mode flag may trigger; the runner implements it. Each returns a short
    human detail (or None when it had nothing to do)."""

    def doctor(self, job_id: str) -> str | None: ...
    def permission(self, job_id: str) -> str | None: ...
    def watcher(self, job_id: str, choice: str) -> str | None: ...
    def needs_you(self, job_id: str, text: str) -> str | None: ...
    def concern(self, job_id: str, choice: str, confidence: float) -> str | None: ...
    def request_diagnosis(self, job_id: str, evidence: dict) -> str | None: ...
    def tech_lead_fact(self, job_id: str, choice: str, evidence: dict) -> str | None: ...


# -- context ---------------------------------------------------------------------------


def _clean(text: object, cap: int) -> str:
    t = mask(redact(_redact(" ".join(str(text or "").split()))))
    return t if len(t) <= cap else t[: cap - 1] + "…"


def _epoch(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _age(ts: str | None, now: float) -> int | None:
    e = _epoch(ts)
    return None if e is None else max(0, int(now - e))


# Same-job events a Flow episode may cite as its source window (never Flow's own events).
_FLOW_SOURCE_EVENTS = ("watcher_verdict", "watcher_nudged", "watcher_escalated", "jev_flag", "jev_escalated",
                       "stall diagnosis produced", "urgent_steer_sent", "instruction acknowledged",
                       "trajectory_advice_received", "finish_line_not_met")
FLOW_SOURCE_EVENTS_MAX = 8


def _flow_source(e) -> bool:
    return e.what in _FLOW_SOURCE_EVENTS or e.what.endswith(" instruction recorded") or e.what.startswith("recovery")


def build_context(store: Store, job_id: str, sig, now: float, *, projects_dir: Path | None = None,
                  activity_fn: Callable | None = None, eta: dict | None = None, refs: list | None = None) -> dict:
    """The compact, redacted state Jev sees. Always <= MAX_STATE_CHARS when serialised. With `eta` (the stored
    ETA dict) it carries the `eta` digest line (counts, open unit ids, the last worker sentence), which `_fit`
    keeps until every other part has been shrunk. A `refs` list is filled with the source window (`turn:<i>`,
    `event:<seq>`) a Flow episode cites; the context itself is unchanged by it."""
    job = store.get_job(job_id)
    contract = load_contract(store, job_id)
    ctx: dict = {
        "goal": _clean(contract.goal if contract else "", 120),
        "state": job.state.value if job is not None else "?",
        "hb": store.kv_get(job_id, "last_class"),
        "idle_s": int(getattr(sig, "since_last_change_s", 0) or 0) if sig is not None else None,
    }
    try:
        act = activity_fn(store, job_id) if activity_fn else \
            transcript.worker_activity(store, job_id, 40, None, projects_dir=projects_dir)
    except Exception:  # noqa: BLE001 - a transcript read failure must never stop the observer
        act = {"turns": []}
    turns = act.get("turns") or []
    if turns:
        ctx["act_s"] = _age(turns[-1].get("ts"), now)
        res = next((t for t in reversed(turns) if t.get("kind") in ("result", "error")), None)
        ctx["res_s"] = _age(res.get("ts"), now) if res else None
        ctx["last"] = turns[-1].get("kind")
        last_tool_i = max((i for i, t in enumerate(turns) if t.get("kind") == "tool"), default=-1)
        last_res_i = max((i for i, t in enumerate(turns) if t.get("kind") in ("result", "error")), default=-1)
        if last_tool_i > last_res_i:
            ctx["tool"] = _clean(turns[last_tool_i].get("text"), 80)
            ctx["tool_s"] = _age(turns[last_tool_i].get("ts"), now)
    if sig is not None and getattr(sig, "running_process", None) and "tool" not in ctx:
        ctx["proc"] = _clean(sig.running_process, 60)

    perms = []
    for a in approvals_module.list_approvals(store, job_id):
        if a.get("kind") != "permission":
            continue
        if a["status"] == "pending" or (a["status"] == "stale" and (_age(a.get("decided_at"), now) or 0) < 900):
            perms.append({"st": a["status"], "age_s": _age(a.get("created_at"), now),
                          "what": _clean(a.get("question", "").replace("worker permission: ", ""), 50)})
        elif a["status"] in ("approved", "denied") and not a.get("relayed_at"):
            perms.append({"st": "unrelayed", "age_s": _age(a.get("decided_at"), now)})
        elif approvals_module.answer_in_flight(a):
            perms.append({"st": "unacked", "age_s": _age(a.get("created_at"), now)})
    if store.kv_get(job_id, "finish_line_unproven"):
        ctx["fin"] = 1  # a prose finish line with no runnable check to prove it
    ctx["perms"] = perms[-3:]
    # The two ages a hung call shows up in (lost-approval hang retro): how long the current tool call has run,
    # and how long the oldest permission has waited (undecided, or answered but not acked by the shim).
    ctx["tool_s"] = ctx.get("tool_s")
    held = hung_call.pending_permission(store, job_id, now)
    ctx["perm_wait_s"] = held["age_s"] if held else None
    ctx["perm_stale"] = sum(1 for p in perms if p["st"] == "stale")

    ctx["turns"] = [[t.get("kind"), _clean(t.get("text"), 100)] for t in turns[-5:]]
    if refs is not None:
        refs.extend(f"turn:{t.get('i', t.get('ts'))}" for t in turns[-5:])
        refs.extend(f"event:{e.seq}" for e in [e for e in store.events(job_id) if _flow_source(e)]
                    [-FLOW_SOURCE_EVENTS_MAX:])
    ctx["steers"] = [{"rev": i["rev"], "st": i["status"], "age_s": _age(i.get("received_at"), now)}
                     for i in Steering(store).all_instructions(job_id)[-3:] if i.get("origin") != "contract"]
    ctx["verdicts"] = _recent_verdicts(store, job_id, now)
    if eta:
        said = next((t for t in reversed(turns) if t.get("kind") == "said"), None)
        ctx["eta"] = eta_jev.digest(eta, said.get("text") if said else "")
    return _fit(ctx)


def _recent_verdicts(store: Store, job_id: str, now: float) -> list[dict]:
    out = []
    for e in reversed(store.events(job_id)):
        if e.what == "watcher_verdict":
            out.append({"w": _clean(e.evidence.get("name"), 20), "s": _clean(e.evidence.get("status"), 12),
                        "age_s": _age(e.ts, now)})
        if len(out) == 3:
            break
    return list(reversed(out))


def _size(ctx: dict) -> int:
    return len(json.dumps(ctx, ensure_ascii=False, separators=(",", ":")))


def _fit(ctx: dict) -> dict:
    """Shrink the least important parts until the serialised context fits MAX_STATE_CHARS."""
    for _ in range(200):
        if _size(ctx) <= MAX_STATE_CHARS:
            return ctx
        turns = ctx.get("turns") or []
        if turns and max(len(t[1]) for t in turns) > 40:
            ctx["turns"] = [[k, x if len(x) <= 40 else x[:39] + "…"] for k, x in turns]
        elif ctx.get("verdicts"):
            ctx["verdicts"] = ctx["verdicts"][1:]
        elif len(turns) > 1:
            ctx["turns"] = turns[1:]
        elif ctx.get("steers"):
            ctx["steers"] = ctx["steers"][1:]
        elif len(ctx.get("goal", "")) > 40:
            ctx["goal"] = ctx["goal"][:39] + "…"
        elif ctx.get("perms"):
            ctx["perms"] = ctx["perms"][1:]
        elif ctx.get("turns") or ctx.get("goal"):
            for k in ("turns", "goal"):
                ctx.pop(k, None)
        elif ctx.get("eta"):  # the digest is cut only after every other part is gone
            over = _size(ctx) - MAX_STATE_CHARS
            keep = len(ctx["eta"]) - over - 1
            ctx["eta"] = ctx["eta"][:keep] + "…" if keep > 0 else ""
        else:
            break
    return ctx


# -- the watch -------------------------------------------------------------------------


class JevWatch:
    def __init__(self, store: Store, observer: JevObserver, actions: Actions, clock=time.time,
                 *, projects_dir: Path | None = None, activity_fn: Callable | None = None):
        self.store = store
        self.observer = observer
        self.actions = actions
        self.clock = clock
        self.projects_dir = projects_dir
        self.activity_fn = activity_fn
        self._last: dict[str, float] = {}
        self._fails: dict[str, int] = {}
        self._hold_until: dict[str, float] = {}
        # v2.3 Flow investigation seams: None builds the configured supervisor-model investigator on demand; the
        # executor runs it off the observer thread (tests inject synchronous fakes).
        self.flow_investigator = None
        self.flow_executor: Callable = flow_investigator_module.run_in_thread
        self._flow_swept = False
        self.outage: OutageAlert | None = None  # SUP-08: the runner wires one (tests inject a recording sender)

    def enabled(self, job_id: str) -> bool:
        return supervisors.jev_mode(self.store, job_id) != "off"

    def due(self, job_id: str) -> bool:
        """True when this job's own 10s timer has elapsed (independent of the runner tick)."""
        now = self.clock()
        return now - self._last.get(job_id, float("-inf")) >= INTERVAL_S and now >= self._hold_until.get(job_id, 0.0)

    def until_due(self, job_id: str) -> float:
        now = self.clock()
        return max(0.0, INTERVAL_S - (now - self._last.get(job_id, float("-inf"))), self._hold_until.get(job_id, 0.0) - now)

    def _note_result(self, job_id: str, ok: bool, now: float) -> None:
        """A dead endpoint must not block every 10s tick for a full request timeout: after `BACKOFF_AFTER`
        consecutive failures, wait exponentially longer (capped) before the next call."""
        if ok:
            self._fails.pop(job_id, None)
            self._hold_until.pop(job_id, None)
            return
        n = self._fails[job_id] = self._fails.get(job_id, 0) + 1
        if n >= BACKOFF_AFTER:
            self._hold_until[job_id] = now + min(BACKOFF_MAX_S, INTERVAL_S * 2 ** (n - BACKOFF_AFTER + 1))

    def step(self, job_id: str, sig, *, force: bool = False) -> dict | None:
        """One check for a RUNNING job. Returns the observer result, or None (off / not due / unavailable)."""
        job = self.store.get_job(job_id)
        if job is None or job.state != JobState.RUNNING:
            self._last[job_id] = self.clock()  # not running: back off a full period, don't spin
            return None
        mode = supervisors.jev_mode(self.store, job_id)
        if mode == "off" or not (force or self.due(job_id)):
            return None
        now = self.clock()
        self._last[job_id] = now
        # The ETA questions ride this one triage call (G12): never a call of their own.
        state = self.store.kv_get(job_id, eta_jev.STATE_KEY)
        eta = state.get("eta") if isinstance(state, dict) else None
        eta_qs = eta_jev.questions(eta)
        try:  # v2.2: deadline choice labels ride the same body when they fit the budget; never a call of their own
            from steward import deadline as deadline_module

            dl_qs = deadline_module.jev_questions(self.store, job_id, len(eta_qs or {}))
        except Exception:  # noqa: BLE001 - labels are optional
            dl_qs = {}
        extra = {**(eta_qs or {}), **dl_qs} or None
        try:  # v2.3: Flow has its own mode and its own reserved rider slot; it never costs the ETA/deadline budget
            flow_mode = config_module.flow_mode(self.store, job_id)
        except Exception:  # noqa: BLE001 - a broken Flow config is Flow off, never a broken observer
            flow_mode = "off"
        refs: list = []
        ctx = build_context(self.store, job_id, sig, now, projects_dir=self.projects_dir, activity_fn=self.activity_fn,
                            eta=eta if eta_qs else None, refs=refs)
        result = self.observer.observe(self.store, job_id, ctx, "", force=True, extra_questions=extra,
                                       flow=flow_mode != "off")
        self._note_result(job_id, result is not None, self.clock())
        if self.outage is not None:
            try:
                self.outage.check(job_id, result)
            except Exception:  # noqa: BLE001 - the alert never breaks the observer
                pass
        if result is None:
            self.store.kv_set(job_id, STREAK_KEY, {"choice": None, "n": 0})
            return None
        if eta_qs and result.get("eta"):
            eta_answers = {k: v for k, v in result["eta"].items() if k in eta_qs}
            if eta_answers:
                eta_jev.record(self.store, job_id, eta_answers, now)
        if dl_qs and result.get("eta"):
            dl_answers = {k: v for k, v in result["eta"].items() if k in dl_qs}
            if dl_answers:
                deadline_module.record_jev(self.store, job_id, dl_answers, now)
        self._gate(job_id, result, mode, now)
        if flow_mode != "off":
            self._flow(job_id, result.get("flow"), ctx, refs, flow_mode, sig, now)
        if flow_mode == "active":
            self._flow_decide(job_id, flow_mode)
        return result

    def _flow_decide(self, job_id: str, flow_mode: str) -> None:
        """Accepted investigations without a decision go to the controller, here on the observer thread (the same
        thread as every other steering/watcher write). Never raises."""
        try:
            settings = config_module.flow_settings()
            for inv_rec in self.store.flow_investigations(job_id):
                if inv_rec["state"] != "interrupted":
                    continue
                episode = self.store.flow_episode(inv_rec["episode_id"])
                if episode is None or episode.get("mode") != "active":
                    continue
                inv = self.flow_investigator or flow_investigator_module.default_investigator(settings)
                store, now = self.store, self.clock()
                self.flow_executor(lambda e=episode, i=inv: flow_investigator_module.continue_investigation(
                    store, e, i, now=now, model=getattr(i, "model", None),
                    prompt_chars=int(settings["investigation_prompt_chars"]),
                    max_allowance_usd=config_module.FLOW_MAX_BUDGET_USD))
            decided = {d["episode_id"] for d in self.store.flow_decisions(job_id)}
            for inv in self.store.flow_investigations(job_id):
                if inv["state"] != "accepted" or inv["episode_id"] in decided:
                    continue
                episode = self.store.flow_episode(inv["episode_id"])
                if episode is not None:
                    flow_actions_module.process(self.store, episode, inv, mode=flow_mode, now=self.clock())
            flow_actions_module.record_outcomes(self.store, job_id, self.clock())
        except Exception as exc:  # noqa: BLE001 - a controller outage defers; it never stops the observer
            self._flow_unavailable(job_id, f"controller_error:{type(exc).__name__}")

    def _flow(self, job_id: str, flow, ctx: dict, refs: list, flow_mode: str, sig, now: float) -> dict | None:
        """Record a qualifying Flow episode. Separate from `_gate`: no streak, no route, no diagnosis request, no
        actions. Shadow stops at the record (FLOW-04). Never raises."""
        try:
            got = jev_observer_module.record_episode(self.store, job_id, flow, ctx, refs, mode=flow_mode, now=now,
                                                     head_sha=getattr(sig, "head_sha", None) or None)
        except Exception as exc:  # noqa: BLE001 - Flow is evidence only; a failure never stops the observer
            self._flow_unavailable(job_id, f"record_error:{type(exc).__name__}")
            return None
        if got is None:
            return None
        record, _created = got
        if flow_mode == "active" and record.get("mode") == "active":
            self._investigate(job_id, record, now)  # shadow episodes stay record-only, even after a mode switch
        return record

    def _investigate(self, job_id: str, record: dict, now: float) -> None:
        """Hand a committed active episode to its one investigation (FLOW-06). The claim inside
        `run_investigation` is the at-most-once gate; this only avoids a pointless hand-off. Never raises."""
        try:
            if not self._flow_swept:  # dead process attempts are checkpointed before a justified continuation
                from steward import call_allowance

                for rec in self.store.interrupt_flow_investigations(owner=flow_investigator_module.PROCESS_OWNER):
                    call_allowance.checkpoint(self.store, rec["job_id"],
                                              call_site=flow_investigator_module.CALL_SITE,
                                              subject_key=rec["episode_id"],
                                              result=call_allowance.CallResult("interrupted", None, None, None,
                                                                               "process_restarted"),
                                              partial=None, now=now)
                self._flow_swept = True
            if self.store.flow_investigation(record["episode_id"]) is not None:
                return
            settings = config_module.flow_settings()
            inv = self.flow_investigator
            if inv is None:
                inv = flow_investigator_module.default_investigator(settings)
            store, chars = self.store, int(settings["investigation_prompt_chars"])
            self.flow_executor(lambda: flow_investigator_module.run_investigation(
                store, record, inv, now=now, model=getattr(inv, "model", None), prompt_chars=chars))
        except Exception as exc:  # noqa: BLE001 - Flow never stops the observer
            self._flow_unavailable(job_id, f"investigation_error:{type(exc).__name__}")

    def _flow_unavailable(self, job_id: str, code: str) -> None:
        try:
            if self.store.kv_get(job_id, "flow_status") == code:
                return
            self.store.kv_set(job_id, "flow_status", code)
            self.store.append_event(job_id, what="flow_unavailable", evidence={"code": code, "authority": "none"},
                                    rule="flow_unavailable", judgment=Judgment.code)
        except Exception:  # noqa: BLE001 - nothing about Flow may break the observer
            pass

    def _gate(self, job_id: str, result: dict, mode: str, now: float) -> None:
        choice, conf = result["choice"], float(result["confidence"])
        if choice == "ok" or conf < FLAG_CONFIDENCE:
            self.store.kv_set(job_id, STREAK_KEY, {"choice": None, "n": 0})
            return
        if choice in DIAGNOSE_CHOICES and conf >= DIAGNOSE_CONFIDENCE:
            self._request_diagnosis(job_id, choice, conf, mode, now)
        streak = self.store.kv_get(job_id, STREAK_KEY) or {}
        n = int(streak.get("n", 0)) + 1 if streak.get("choice") == choice else 1
        self.store.kv_set(job_id, STREAK_KEY, {"choice": choice, "n": n})
        if n < STREAK_N:
            return
        cooldowns = self.store.kv_get(job_id, COOLDOWN_KEY) or {}
        if now - float(cooldowns.get(choice, float("-inf"))) < COOLDOWN_S:
            return
        cooldowns[choice] = now
        self.store.kv_set(job_id, COOLDOWN_KEY, cooldowns)
        self.store.kv_set(job_id, STREAK_KEY, {"choice": None, "n": 0})
        route = ROUTES[choice]
        base = {"choice": choice, "confidence": conf, "mode": mode, "authority": "none"}
        if choice == "hung_call":
            # Job d3477a2a: a verdict alone (shadow or active) never routes. Only the measured ages do — a tool
            # running > 30 min, or a permission undecided/undelivered > 5 min (hung_call.findings).
            ages = hung_call.measured(self.store, job_id, now, activity_fn=self.activity_fn)
            if not ages:
                self.store.append_event(job_id, what="jev_flag",
                                        evidence={**base, "would_escalate_to": route,
                                                  "held": "no measured hung tool call or stuck permission"},
                                        rule="jev_flag_unmeasured", judgment=Judgment.model)
                return
            base["measured"] = [{"kind": f["kind"], "age_s": f["age_s"]} for f in ages]
        if choice in TECH_LEAD_CHOICES:
            submit = getattr(self.actions, "tech_lead_fact", None)
            try:
                detail = submit(job_id, choice, base) if submit is not None else "no tech-lead seam"
            except Exception as exc:  # noqa: BLE001 - a routing failure is recorded, never fatal (SP-6)
                detail = f"error: {type(exc).__name__}"
            self.store.append_event(job_id, what="jev_flag", evidence={**base, "routed_to": route, "detail": detail},
                                    rule="jev_flag_tech_lead", judgment=Judgment.model)
            return
        # HOTFIX 2026-10-01: a shadow-mode hung_call routed on a 0-second-old, already-relayed permission and the
        # doctor then closed a live worker (job d3477a2a). Shadow is record-only for every choice, a measured
        # hung_call included; the measured-age gate above governs active mode.
        if mode != "active":
            self.store.append_event(job_id, what="jev_flag", evidence={**base, "would_escalate_to": route},
                                    rule="jev_flag_shadow", judgment=Judgment.model)
            return
        self.store.append_event(job_id, what="jev_flag", evidence={**base, "escalated_to": route},
                                rule="jev_flag_active", judgment=Judgment.model)
        details = self._escalate(job_id, choice, conf)
        self.store.append_event(job_id, what="jev_escalated",
                                evidence={"choice": choice, "route": route, "details": details},
                                rule="jev_escalation", judgment=Judgment.code)

    def _request_diagnosis(self, job_id: str, choice: str, conf: float, mode: str, now: float) -> None:
        last = self.store.kv_get(job_id, DIAGNOSE_KEY)
        if last is not None and now - float(last) < DIAGNOSE_EVERY_S:
            return
        request = getattr(self.actions, "request_diagnosis", None)
        if request is None:
            return
        self.store.kv_set(job_id, DIAGNOSE_KEY, now)
        try:
            detail = request(job_id, {"choice": choice, "confidence": conf})
        except Exception as exc:  # noqa: BLE001 - a diagnosis failure is recorded, never fatal (SP-6)
            detail = f"error: {type(exc).__name__}"
        self.store.append_event(job_id, what="jev_diagnosis_requested",
                                evidence={"choice": choice, "confidence": conf, "mode": mode, "detail": detail},
                                rule="jev_flag_requests_diagnosis", judgment=Judgment.model_informed_code_authorized)

    def _escalate(self, job_id: str, choice: str, conf: float) -> list[str]:
        out: list[str] = []
        try:
            if choice in ("stalled", "hung_call"):
                if choice == "hung_call":
                    out.append("permission: " + str(self.actions.permission(job_id)))
                out.append("doctor: " + str(self.actions.doctor(job_id)))
            elif choice in ("looping", "off_track"):
                out.append("watcher: " + str(self.actions.watcher(job_id, choice)))
            elif choice == "risky_action":
                concern = getattr(self.actions, "concern", None)
                if concern is not None:
                    out.append("concern: " + str(concern(job_id, choice, conf)))
                else:  # compatibility for older action adapters
                    out.append("needs_you: " + str(self.actions.needs_you(
                        job_id, f"observer flagged a risky action ({conf:.2f}); please look")))
        except Exception as exc:  # noqa: BLE001 - an escalation failure is recorded, never fatal (SP-6)
            out.append(f"error: {type(exc).__name__}")
        return out


# -- scoring ---------------------------------------------------------------------------

_BAD_VERDICTS = ("off_track", "drifting", "concern", "concerned", "stuck", "bad", "problem", "flag", "escalate")


def _corroborates(e, escalated: bool) -> bool:
    ev = e.evidence or {}
    if escalated:
        # The flag itself called the doctor / watcher / advisor, so everything they then produced is downstream of
        # the flag and proves nothing (d3477a2a: the false hung_call "agreed" with its own doctor). Only a human
        # steer, which Jev cannot cause, is independent ground truth.
        return e.what.endswith(" instruction recorded") and not _is_non_human(str(ev.get("origin") or "cli"))
    if e.what in ("watcher_escalated", "trajectory_escalated", "watcher_nudged", "recovery escalated to human"):
        return True
    if e.what == "watcher_verdict":
        return str(ev.get("status", "")).lower() not in ("ok", "on_track", "fine", "")
    if e.what == "trajectory_advice_received":
        v = (ev.get("verdict") or {}).get("trajectory")
        return v not in (None, "on_track", "ok")
    if e.what == "finish_line_not_met":
        return True  # code found the same thing independently
    if e.what == "stall diagnosis produced":
        return True
    if e.what.endswith(" instruction recorded"):  # a human steer counts as a human finding a problem
        return not _is_non_human(str(ev.get("origin") or "cli"))
    return False


def score(store: Store, job_id: str, now: float | None = None) -> dict:
    """Precision of the flags so far: a flag AGREES when a watcher/advisor/doctor/human also found a
    problem within AGREE_WINDOW_S after it; `pending` flags are still inside the window."""
    now = time.time() if now is None else now
    events = store.events(job_id)
    flags = [e for e in events if e.what == "jev_flag"]
    agree = disagree = pending = 0
    by_choice: dict[str, dict] = {}
    for f in flags:
        t0 = _epoch(f.ts) or 0.0
        escalated = any(x.what == "jev_escalated" and 0 <= (_epoch(x.ts) or 0) - t0 <= AGREE_WINDOW_S for x in events)
        hit = any(t0 < (_epoch(x.ts) or 0) <= t0 + AGREE_WINDOW_S and _corroborates(x, escalated) for x in events)
        c = by_choice.setdefault(f.evidence.get("choice", "?"), {"agree": 0, "disagree": 0, "pending": 0})
        if hit:
            agree += 1
            c["agree"] += 1
        elif now < t0 + AGREE_WINDOW_S:
            pending += 1
            c["pending"] += 1
        else:
            disagree += 1
            c["disagree"] += 1
    scored = agree + disagree
    return {"job_id": job_id, "flags": len(flags), "agree": agree, "disagree": disagree, "pending": pending,
            "precision": (agree / scored) if scored else None, "by_choice": by_choice}


# -- fleet lanes (v2.5, FLEET-32/33/35) ---------------------------------------------------------------------------
#
# For a fleet job each live lane is triaged on its own: its own bounded, redacted, runtime-agnostic context (from the
# Store's lane records, the controller's last adapter observation and the lane worktree's changed paths), its own
# streak and per-choice cooldown under the SAME gate as above. A gated flag goes to the controller's Arbiter as a
# `jev_fleet` fact; the Arbiter's narrow amendment decides whether it wakes the tech lead. Jev itself never acts.

LANE_STREAK_KEY = "jev_lane_streak"
LANE_COOLDOWN_KEY = "jev_lane_cooldown"
LANE_TOUCHED_MAX = 20
LANE_RECENT_MAX = 6
_LANE_ERROR_EVENTS = ("frame_rejected", "capability_denied", "lane_not_dispatched")
_LANE_CORROBORATING = ("integration conflict", "integration blocked", "frame_rejected", "capability_denied")


def _git_lines(worktree, *args) -> list[str]:
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True, timeout=10,
                           check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln for ln in r.stdout.splitlines() if ln.strip()] if r.returncode == 0 else []


def lane_touched_paths(store: Store, job_id: str, lane_id: str) -> list[str]:
    """Paths the lane changed since the pinned fleet base: committed (`base...HEAD`) and uncommitted."""
    row = store._conn.execute("SELECT worktree_path FROM fleet_lanes WHERE job_id=? AND lane_id=?",
                              (job_id, lane_id)).fetchone()
    if row is None or not row["worktree_path"] or not Path(row["worktree_path"]).is_dir():
        return []
    wt = row["worktree_path"]
    base = store.kv_get(job_id, "fleet_base_sha")
    paths = set(_git_lines(wt, "diff", "--name-only", f"{base}...HEAD")) if base else set()
    paths |= {ln[3:].split(" -> ")[-1].strip() for ln in _git_lines(wt, "status", "--porcelain")}
    return sorted(p for p in paths if p)


def build_lane_context(store: Store, job_id: str, lane_id: str, now: float, *, controller=None,
                       touched_fn: Callable | None = None) -> dict:
    """The compact, redacted state Jev sees for ONE fleet lane (<= MAX_STATE_CHARS serialised)."""
    from steward import fleet_lease as fl
    from steward.fleet_store import FleetStore

    fs = FleetStore(store)
    touched_fn = touched_fn or (lambda j, ln: lane_touched_paths(store, j, ln))
    lanes = {r["lane_id"]: r["task_id"] for r in store._conn.execute(
        "SELECT lane_id, task_id FROM fleet_lanes WHERE job_id=?", (job_id,))}
    task_id = lanes.get(lane_id)
    view = fs.graph_view(job_id) or {}
    task = next((t for t in view.get("tasks", []) if t["id"] == task_id), {})
    lease = fl.get(store, job_id, lane_id) or {}
    on = fs.lane_runtime(job_id, lane_id) or {}
    mine = list(touched_fn(job_id, lane_id) or [])
    live = sorted(ln["lane_id"] for ln in fl.live(store, job_id))
    others, overlap = [], []
    for other in live:
        if other == lane_id:
            continue
        theirs = list(touched_fn(job_id, other) or [])
        others.append({"lane": other, "touched": [_clean(p, 80) for p in theirs[:LANE_TOUCHED_MAX]]})
        shared = sorted(set(mine) & set(theirs))
        if shared:
            overlap.append({"lane": other, "paths": [_clean(p, 80) for p in shared[:LANE_TOUCHED_MAX]]})
    evs = [e for e in fs.fleet_events(job_id) if e["lane_id"] == lane_id]
    last = _epoch(evs[-1]["ts"]) if evs else None
    obs = (getattr(controller, "last_observation", {}) or {}).get((job_id, lane_id)) if controller else None
    ctx = {
        "lane": lane_id, "task": task_id, "objective": _clean(task.get("objective"), 160),
        "owns": list(task.get("owns", []))[:LANE_TOUCHED_MAX],
        "runtime": on.get("runtime"), "model": _clean(on.get("model"), 60),
        "state": lease.get("state"), "attempt": lease.get("attempt_no"),
        "lifecycle": (obs or {}).get("lifecycle"),
        "idle_s": None if last is None else max(0, int(now - last)),
        "touched": [_clean(p, 80) for p in mine[:LANE_TOUCHED_MAX]],
        "others": others, "overlap": overlap,
        "errors": sum(1 for e in evs if e["what"] in _LANE_ERROR_EVENTS),
        "recent": [[_clean(e["what"], 40), _age(e["ts"], now)] for e in evs[-LANE_RECENT_MAX:]],
    }
    for _ in range(50):  # shrink the least important parts until it fits
        if _size(ctx) <= MAX_STATE_CHARS:
            break
        if len(ctx["recent"]) > 1:
            ctx["recent"] = ctx["recent"][1:]
        elif any(len(o["touched"]) > 3 for o in ctx["others"]):
            ctx["others"] = [{**o, "touched": o["touched"][:3]} for o in ctx["others"]]
        elif len(ctx["touched"]) > 5:
            ctx["touched"] = ctx["touched"][:5]
        elif len(ctx["objective"]) > 60:
            ctx["objective"] = ctx["objective"][:59] + "…"
        else:
            ctx["others"] = ctx["others"][:1]
    return ctx


class FleetLaneTriage:
    """Per-lane Jev for fleet jobs. `observer.observe_lane(store, job_id, lane_id, signals)` -> {choice,
    confidence} | None (the real one is `JevObserver.observe_lane`). Records `jev_lane_flag` fleet events and hands
    gated flags to `controller.arbiter`; never touches a lane, a lease or the graph."""

    def __init__(self, store: Store, observer, controller, *, clock=time.time, touched_fn: Callable | None = None):
        self.store, self.observer, self.controller, self.clock = store, observer, controller, clock
        self.touched_fn = touched_fn
        self._last: dict[tuple[str, str], float] = {}

    def step(self, job_id: str, *, force: bool = False) -> dict:
        from steward import fleet_lease as fl
        from steward import fleet_runtime as rt

        if not rt.is_fleet_job(self.store, job_id):
            return {}
        mode = supervisors.jev_mode(self.store, job_id)
        if mode == "off":
            return {}
        out = {}
        for lease in fl.live(self.store, job_id):
            lane = lease["lane_id"]
            now = self.clock()
            if not force and now - self._last.get((job_id, lane), float("-inf")) < INTERVAL_S:
                continue
            self._last[(job_id, lane)] = now
            ctx = build_lane_context(self.store, job_id, lane, now, controller=self.controller,
                                     touched_fn=self.touched_fn)
            res = self.observer.observe_lane(self.store, job_id, lane, ctx)
            if res is None:
                self._streak(job_id, lane, None)
                continue
            out[lane] = res
            self._gate(job_id, lane, res, mode, now)
        return out

    def _streak(self, job_id: str, lane: str, choice: str | None) -> int:
        with self.store.immediate() as tx:
            doc = tx.kv_get(job_id, LANE_STREAK_KEY, {}) or {}
            cur = doc.get(lane) or {}
            n = int(cur.get("n", 0)) + 1 if choice is not None and cur.get("choice") == choice else (
                1 if choice is not None else 0)
            doc[lane] = {"choice": choice, "n": n}
            tx.kv_set(job_id, LANE_STREAK_KEY, doc)
        return n

    def _gate(self, job_id: str, lane: str, result: dict, mode: str, now: float) -> None:
        from steward import fleet_runtime as rt
        from steward.fleet_store import FleetStore

        choice, conf = result["choice"], float(result["confidence"])
        if choice == "ok" or conf < FLAG_CONFIDENCE:
            self._streak(job_id, lane, None)
            return
        if self._streak(job_id, lane, choice) < STREAK_N:
            return
        with self.store.immediate() as tx:
            cds = tx.kv_get(job_id, LANE_COOLDOWN_KEY, {}) or {}
            mine = cds.setdefault(lane, {})
            if now - float(mine.get(choice, float("-inf"))) < COOLDOWN_S:
                return
            mine[choice] = now
            tx.kv_set(job_id, LANE_COOLDOWN_KEY, cds)
        self._streak(job_id, lane, None)
        tmode = rt.jev_trigger_mode(self.store)
        fact = self.controller.arbiter.submit(job_id, rt.Arbiter.JEV_FLEET, choice, lane=lane, confidence=conf,
                                              trigger_mode=tmode, jev_mode=mode)
        route = fact["route"]
        would = "wake_tech_lead" if route == "outer" else ("doctor" if route == "doctor" else "none")
        FleetStore(self.store).append_event(
            job_id, what="jev_lane_flag", actor="jev", rule="jev_lane_gate", judgment=Judgment.model, lane_id=lane,
            evidence={"choice": choice, "confidence": conf, "route": route, "trigger_mode": tmode,
                      "would_act": would, "jev_mode": mode, "fact_id": fact["id"], "authority": "none",
                      "observer": str(result.get("observer") or "jev")[:120]})


def score_lanes(store: Store, job_id: str, now: float | None = None) -> dict:
    """Precision of the per-lane fleet flags (D-13): a flag AGREES when INDEPENDENT evidence on that lane (or job-wide)
    follows within AGREE_WINDOW_S - an integration conflict/block, a rejected frame, a capability denial, or a human
    steer. Anything the flag itself caused (the tech lead's packet or proposal) proves nothing."""
    from steward.fleet_store import FleetStore

    now = time.time() if now is None else now
    fev = FleetStore(store).fleet_events(job_id)
    human = [e for e in store.events(job_id) if e.what.endswith(" instruction recorded")
             and not _is_non_human(str((e.evidence or {}).get("origin") or "cli"))]
    agree = disagree = pending = 0
    by_choice: dict[str, dict] = {}
    for f in [e for e in fev if e["what"] == "jev_lane_flag"]:
        t0 = _epoch(f["ts"]) or 0.0
        ev = json.loads(f["evidence"] or "{}")
        hit = any(t0 < (_epoch(x["ts"]) or 0) <= t0 + AGREE_WINDOW_S and x["what"] in _LANE_CORROBORATING
                  and x["lane_id"] in (f["lane_id"], None) for x in fev)
        hit = hit or any(t0 < (_epoch(h.ts) or 0) <= t0 + AGREE_WINDOW_S for h in human)
        c = by_choice.setdefault(ev.get("choice", "?"), {"agree": 0, "disagree": 0, "pending": 0})
        key = "agree" if hit else ("pending" if now < t0 + AGREE_WINDOW_S else "disagree")
        c[key] += 1
        agree, disagree, pending = agree + (key == "agree"), disagree + (key == "disagree"), pending + (key == "pending")
    scored = agree + disagree
    return {"job_id": job_id, "flags": agree + disagree + pending, "agree": agree, "disagree": disagree,
            "pending": pending, "precision": (agree / scored) if scored else None, "by_choice": by_choice}
