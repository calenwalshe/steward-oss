"""The trajectory supervisor: closes the outer loop (TRJ-09, TRJ-11, 12-CONTEXT D2/D8/D9).

`TrajectorySupervisor.step(job_id)` is the outer loop's only entrypoint,
called once per `Runner.tick` (12-03 wires that call). It holds **no worker
reference** and never calls `worker.*` — its only side effects are kv/ledger/
events writes, one `request_review` cmd frame to the advisor pane (via the
injected `AdvisorLink`), phone notifications, and `Steering.record(origin=
"trajectory")`. Delivery to the worker happens only in the runner's own
tick step (2); the supervisor never sees a worker adapter.

Each `step()`:
1. `off` (kill switch or per-job/config mode) -> discard any arrived advice
   for an in-flight review, and return without detecting or requesting.
2. An in-flight review whose advice has arrived is converted via
   `hysteresis.decide` into at most one `Steering.record` (a redirect, active
   mode only; shadow mode records `trajectory_would_redirect` instead and
   changes no hysteresis state). A human steer since the review was
   requested (`expected_revision` mismatch) discards the advice
   (`stale_after_human_steer`); any instruction still unacknowledged defers
   the decision until `advice_ttl_s` elapses, never stacking a new revision
   on top of one the worker hasn't seen yet. No advice yet: a timeout clears
   the in-flight marker.
3. `triggers.evaluate` decides whether a fresh review is due; if so, the
   review/supervision-budget caps are enforced, the advisor is looked up
   (missing -> `trajectory_review_skipped`), a packet is built and recorded,
   and `AdvisorLink.request` sends the one frame this module ever sends.
4. Any supervision cost carried by newly arrived advice is folded into the
   ledger, regardless of what the decision was — money was already spent.

Config (`TrajectoryConfig`) is loaded from `~/.config/steward/trajectory.toml`
via `TrajectoryConfigSource`, re-read whenever the file's mtime changes;
`STEWARD_TRAJECTORY` overrides `mode` on every read, file or not.
`effective_mode` layers the global kill switch (kv `_trajectory`/`mode`, set
by `steward trajectory off`) over the per-job override (kv `trajectory_mode`,
`steward trajectory mode M --job J`) over that config default.

DEVIATION (documented, see 12-03-SUMMARY): D5's settle-window text says the
window lasts "until at least one phase_boundary or test_result arrives
*after the redirect's ack*". `triggers.evaluate`'s own pure signature
(established in 12-01) has no ack timestamp to compare against — it treats
`settle_satisfied` purely as "has evidence arrived in the ledger deltas since
the last review". This module does not separately model the ack timing: the
settle window is exactly `triggers.start_settle`/`evaluate()`'s own
mechanism, called right after an accepted redirect. In practice the gap is
small (an accepted redirect's instruction is delivered the very next tick),
and 12-01/12-02 already fixed the evaluate()/ledger contract as the one both
modules share — reopening it here was out of this plan's file list
(`steward/triggers.py` is not in 12-03's `files_modified`).
"""

from __future__ import annotations

import os
import time
import tomllib
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from steward import hysteresis
from steward import triggers as triggers_module
from steward.contract import load_contract
from steward.ledger import TrajectoryLedger
from steward.models import Judgment
from steward.notify import Notifier, PhoneEvent, deep_link, emit
from steward.review import build_packet, parse_verdict, verdict_to_dict
from steward.steering import Steering
from steward.store import Store, resolve_config_dir

_GLOBAL_KV_JOB = "_trajectory"
_GLOBAL_MODE_KEY = "mode"
_JOB_MODE_KEY = "trajectory_mode"

_TRIGGER_STATE_KEY = "trajectory_trigger_state"
_HYSTERESIS_STATE_KEY = "trajectory_hysteresis_state"
_LAST_REDIRECT_REV_KEY = "trajectory_last_redirect_rev"
_BUDGET_EXHAUSTED_KEY = "trajectory_budget_exhausted"

_ADVICE_KEY_PREFIX = "trajectory_advice:"
_PACKET_KEY_PREFIX = "trajectory_review:"  # matches steward.advisor_pane's own key shape
_REVIEW_META_KEY_PREFIX = "trajectory_review_meta:"

_MODES = ("off", "shadow", "active")

_REFOCUS_VERBS = frozenset({"refocus", "narrow_scope"})


# -- config ------------------------------------------------------------------


@dataclass(frozen=True)
class TrajectoryConfig:
    mode: str = "shadow"
    triggers: triggers_module.TriggerConfig = field(default_factory=triggers_module.TriggerConfig)
    hysteresis: hysteresis.HysteresisConfig = field(default_factory=hysteresis.HysteresisConfig)
    advice_ttl_s: float = 900
    max_reviews_per_job: int = 12
    supervision_budget_usd: float = 5.0


def load_trajectory_config(path: Path | str | None = None) -> TrajectoryConfig:
    """Reads `path` (default `~/.config/steward/trajectory.toml`); a missing
    file yields plain defaults. `STEWARD_TRAJECTORY` overrides `mode` on top
    (checked fresh by `TrajectoryConfigSource.current()` too, so an env
    change is picked up without touching the file)."""
    cfg = TrajectoryConfig()
    p = Path(path) if path is not None else (resolve_config_dir() / "trajectory.toml")
    if p.exists():
        with p.open("rb") as fh:
            data = tomllib.load(fh)
        trig_data = data.get("triggers", {})
        hyst_data = data.get("hysteresis", {})
        trig_kwargs = {k: v for k, v in trig_data.items() if k in triggers_module.TriggerConfig.__dataclass_fields__}
        if "time_marks_s" in trig_kwargs:
            trig_kwargs["time_marks_s"] = tuple(trig_kwargs["time_marks_s"])
        hyst_kwargs = {k: v for k, v in hyst_data.items() if k in hysteresis.HysteresisConfig.__dataclass_fields__}
        cfg = TrajectoryConfig(
            mode=data.get("mode", cfg.mode),
            triggers=triggers_module.TriggerConfig(**trig_kwargs),
            hysteresis=hysteresis.HysteresisConfig(**hyst_kwargs),
            advice_ttl_s=data.get("advice_ttl_s", cfg.advice_ttl_s),
            max_reviews_per_job=data.get("max_reviews_per_job", cfg.max_reviews_per_job),
            supervision_budget_usd=data.get("supervision_budget_usd", cfg.supervision_budget_usd),
        )
    env_mode = os.environ.get("STEWARD_TRAJECTORY")
    if env_mode:
        cfg = replace(cfg, mode=env_mode)
    return cfg


class TrajectoryConfigSource:
    """Re-reads `load_trajectory_config(path)` only when the file's mtime
    changes; `STEWARD_TRAJECTORY` is re-applied on every `current()` call
    regardless (cheap, and env changes don't touch the file's mtime)."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else (resolve_config_dir() / "trajectory.toml")
        self._mtime: float | None = None
        self._cfg = TrajectoryConfig()
        self._reload()

    def _file_mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    def _reload(self) -> None:
        self._cfg = load_trajectory_config(self.path)
        self._mtime = self._file_mtime()

    def current(self) -> TrajectoryConfig:
        if self._file_mtime() != self._mtime:
            self._reload()
        env_mode = os.environ.get("STEWARD_TRAJECTORY")
        if env_mode and env_mode != self._cfg.mode:
            return replace(self._cfg, mode=env_mode)
        return self._cfg


# -- effective mode (D9) -------------------------------------------------


@dataclass(frozen=True)
class EffectiveMode:
    mode: str
    source: str  # "kill_switch" | "job" | "config"


def effective_mode(store: Store, job_id: str, cfg: TrajectoryConfig) -> EffectiveMode:
    """Global kill switch (kv `_trajectory`/`mode`) wins, then the per-job
    override (kv `trajectory_mode`), then `cfg.mode`. Re-read every call —
    never cached — so `steward trajectory off` is effective next tick (D9,
    TRJ-11)."""
    global_mode = store.kv_get(_GLOBAL_KV_JOB, _GLOBAL_MODE_KEY)
    if global_mode is not None:
        return EffectiveMode(global_mode, "kill_switch")
    job_mode = store.kv_get(job_id, _JOB_MODE_KEY)
    if job_mode is not None:
        return EffectiveMode(job_mode, "job")
    return EffectiveMode(cfg.mode, "config")


def set_global_mode(store: Store, mode: str) -> None:
    if mode not in _MODES:
        raise ValueError(f"unknown trajectory mode: {mode!r}")
    store.kv_set(_GLOBAL_KV_JOB, _GLOBAL_MODE_KEY, mode)


def set_job_mode(store: Store, job_id: str, mode: str) -> None:
    if mode not in _MODES:
        raise ValueError(f"unknown trajectory mode: {mode!r}")
    store.kv_set(job_id, _JOB_MODE_KEY, mode)


# -- state loaders (shared by the supervisor and the CLI's status read) ----


def load_trigger_state(store: Store, job_id: str) -> triggers_module.TriggerState:
    data = store.kv_get(job_id, _TRIGGER_STATE_KEY)
    return triggers_module.TriggerState.from_dict(data) if data else triggers_module.TriggerState()


def load_hysteresis_state(store: Store, job_id: str) -> hysteresis.HysteresisState:
    data = store.kv_get(job_id, _HYSTERESIS_STATE_KEY)
    return hysteresis.HysteresisState.from_dict(data) if data else hysteresis.HysteresisState()


# -- status read (TRJ-12, `steward trajectory status`) -----------------------


def review_history(store: Store, job_id: str, limit: int = 5) -> list[dict]:
    """The last `limit` reviews, each joined with its advice (verdict, if
    any answered) and the last `trajectory_*` decision event that named it —
    a direct read, never mutates anything."""
    ledger = TrajectoryLedger(store, job_id)
    reviews = ledger.snapshot().get("lists", {}).get("reviews", [])[-limit:]
    events = store.events_prefixed(job_id, "trajectory_")  # the only events the join below can match
    rows = []
    for entry in reviews:
        review_id = entry["review_id"]
        advice = store.kv_get(job_id, _advice_key(review_id)) or {}
        decision = next(
            (e.what for e in reversed(events) if e.evidence.get("review_id") == review_id and e.what.startswith("trajectory_")),
            None,
        )
        rows.append(
            {
                "review_id": review_id,
                "ts": entry["ts"],
                "triggers": entry["triggers"],
                "verdict": advice.get("verdict"),
                "decision": decision,
            }
        )
    return rows


def status_snapshot(store: Store, job_id: str, cfg: TrajectoryConfig, now: float) -> dict:
    """Everything `steward trajectory status` prints, as a plain dict (also
    the `--json` payload). Never mutates anything (D9: status is a direct
    read)."""
    eff = effective_mode(store, job_id, cfg)
    ledger = TrajectoryLedger(store, job_id)
    snapshot = ledger.snapshot()
    trig = load_trigger_state(store, job_id)
    hstate = load_hysteresis_state(store, job_id)

    reference = trig.last_review_at if trig.last_review_at is not None else snapshot.get("started_at")
    next_due_in = max(0.0, (reference + cfg.triggers.cadence_s) - now) if reference is not None else None

    return {
        "job_id": job_id,
        "mode": eff.mode,
        "mode_source": eff.source,
        "reviews": review_history(store, job_id),
        "open_concerns": hstate.open_concerns,
        "current_approach": hstate.current_approach,
        "discarded_approaches": list(hstate.discarded),
        "next_review_due_in_s": next_due_in,
        "settle_until": trig.settle_until,
        "in_settle": _in_settle(trig, now),
        "worker_spend_usd": ledger.worker_spend_usd(),
        "supervision_spend_usd": ledger.supervision_spend_usd(),
        "elapsed_s": ledger.elapsed_s(now),
    }


# -- advisor link (D1) --------------------------------------------------


class AdvisorLink(Protocol):
    def find(self) -> str | None: ...

    def request(self, pane_id: str, job_id: str, review_id: str) -> None: ...


@dataclass
class HerdrAdvisorLink:
    """The production `AdvisorLink`: wraps `steward.advisor_pane.find_advisor_pane`
    / `request_review` over a real Herdr socket."""

    herdr_api: Any

    def find(self) -> str | None:
        from steward.advisor_pane import find_advisor_pane

        return find_advisor_pane(self.herdr_api)

    def request(self, pane_id: str, job_id: str, review_id: str) -> None:
        from steward.advisor_pane import request_review

        request_review(self.herdr_api, pane_id, job_id, review_id)


class NoAdvisorLink:
    """A degenerate `AdvisorLink` for a run with no Herdr socket at all
    (e.g. `--worker fake`): `find()` always reports no advisor, so the
    supervisor takes the same fail-open `trajectory_review_skipped` path a
    real, merely-absent advisor pane would (D1)."""

    def find(self) -> str | None:
        return None

    def request(self, pane_id: str, job_id: str, review_id: str) -> None:  # pragma: no cover - never reached
        pass


# -- the supervisor -------------------------------------------------------


@dataclass(frozen=True)
class StepReport:
    job_id: str
    mode: str
    detail: str


class TrajectorySupervisor:
    def __init__(
        self,
        store: Store,
        advisor_link: AdvisorLink,
        notifier: Notifier,
        clock=time.time,
        cfg: TrajectoryConfig | TrajectoryConfigSource = None,
    ):
        self.store = store
        self.advisor_link = advisor_link
        self.notifier = notifier
        self.clock = clock
        self.cfg = cfg if cfg is not None else TrajectoryConfig()

    # -- config / state plumbing ------------------------------------------

    def _current_cfg(self) -> TrajectoryConfig:
        current = getattr(self.cfg, "current", None)
        return current() if callable(current) else self.cfg

    def _load_trigger_state(self, job_id: str) -> triggers_module.TriggerState:
        return load_trigger_state(self.store, job_id)

    def _save_trigger_state(self, job_id: str, state: triggers_module.TriggerState) -> None:
        self.store.kv_set(job_id, _TRIGGER_STATE_KEY, state.to_dict())

    def _load_hysteresis_state(self, job_id: str) -> hysteresis.HysteresisState:
        return load_hysteresis_state(self.store, job_id)

    def _save_hysteresis_state(self, job_id: str, state: hysteresis.HysteresisState) -> None:
        self.store.kv_set(job_id, _HYSTERESIS_STATE_KEY, state.to_dict())

    def _notify(self, job_id: str, kind: str, title: str, body: str) -> None:
        workspace = self.store.kv_get(job_id, "herdr_workspace")
        tab = self.store.kv_get(job_id, "herdr_tab")
        pane = self.store.kv_get(job_id, "herdr_pane")
        event = PhoneEvent(
            kind=kind,
            job_id=job_id,
            title=f"{title} ({job_id[:8]})"[:40],
            body=body[:140],
            deep_link=deep_link(workspace, tab, pane),
            revision=self.store.get_revision(job_id),
        )
        emit(self.store, self.notifier, event)

    def _event(self, job_id: str, what: str, evidence: dict, rule: str, judgment: Judgment) -> None:
        self.store.append_event(job_id, what=what, evidence=evidence, rule=rule, judgment=judgment)

    # -- the tick -----------------------------------------------------------

    def step(self, job_id: str) -> StepReport:
        cfg = self._current_cfg()
        now = self.clock()
        eff = effective_mode(self.store, job_id, cfg)
        state = self._load_trigger_state(job_id)

        if eff.mode == "off":
            state = self._discard_if_off(job_id, state, now)
            self._save_trigger_state(job_id, state)
            return StepReport(job_id, eff.mode, "off: no detection, no requests, advice discarded")

        state = self._process_in_flight(job_id, state, cfg, eff.mode, now)
        state = self._observe_redirect_ack(job_id, state, now)

        ledger = TrajectoryLedger(self.store, job_id)
        snapshot = ledger.snapshot()
        snapshot["recoveries_used"] = self.store.kv_get(job_id, "recoveries_used", default=0)
        deltas = ledger.since(state.last_review_at or 0.0)

        evaluation, new_state = triggers_module.evaluate(snapshot, deltas, state, cfg.triggers, now)

        detail = "no review due"
        if evaluation.review_due:
            new_state, detail = self._request_review(job_id, ledger, snapshot, evaluation, cfg, new_state, now)

        self._save_trigger_state(job_id, new_state)
        return StepReport(job_id, eff.mode, detail)

    # -- step 1: off -----------------------------------------------------

    def _discard_if_off(
        self, job_id: str, state: triggers_module.TriggerState, now: float
    ) -> triggers_module.TriggerState:
        if state.in_flight_review_id is None:
            return state
        review_id = state.in_flight_review_id
        advice = self.store.kv_get(job_id, _advice_key(review_id))
        if advice is None:
            return state
        self._add_supervision_cost(job_id, review_id, advice, now)
        self._event(
            job_id,
            "trajectory_advice_discarded",
            {"review_id": review_id, "reason": "mode_off"},
            "mode_off",
            Judgment.code,
        )
        return triggers_module.resolve_review(state, now, diff_lines=state.last_review_diff_lines, verdict=None)

    def _observe_redirect_ack(
        self, job_id: str, state: triggers_module.TriggerState, now: float
    ) -> triggers_module.TriggerState:
        """D5/TRJ-05 (chair-authorized fix): while a settle window is open
        and no ack has been observed yet (`settle_evidence_after is None`),
        check whether the last redirect's steering instruction has reached
        `acknowledged`; if so, record `settle_evidence_after` so
        `triggers.evaluate` can start counting evidence that arrives from
        here on. Uses `now` (this tick's clock reading, the same time system
        every other timestamp in this subsystem uses) rather than the
        instruction's own `acknowledged_at`, which is a wall-clock ISO
        string from `Store.now_iso()` — a different clock than the injected
        `self.clock` (a `FakeClock` in tests, `time.time` in production);
        parsing it would silently disagree with (or, worse, be
        incomparable to) every other `ts` this module ever compares against.
        """
        if state.settle_until <= 0 or state.settle_evidence_after is not None:
            return state
        rev = self.store.kv_get(job_id, _LAST_REDIRECT_REV_KEY)
        if rev is None:
            return state
        instructions = Steering(self.store).all_instructions(job_id)
        instr = next((i for i in instructions if i["rev"] == rev), None)
        if instr is None or instr["status"] != "acknowledged":
            return state
        return replace(state, settle_evidence_after=now)

    # -- step 2: in-flight review -----------------------------------------

    def _process_in_flight(
        self, job_id: str, state: triggers_module.TriggerState, cfg: TrajectoryConfig, mode: str, now: float
    ) -> triggers_module.TriggerState:
        if state.in_flight_review_id is None:
            return state
        review_id = state.in_flight_review_id
        advice = self.store.kv_get(job_id, _advice_key(review_id))

        if advice is None:
            if now - state.in_flight_started_at >= cfg.triggers.review_timeout_s:
                self._event(
                    job_id,
                    "trajectory_review_timed_out",
                    {"review_id": review_id},
                    "review_timeout",
                    Judgment.code,
                )
                return triggers_module.resolve_review(
                    state, now, diff_lines=state.last_review_diff_lines, verdict=None
                )
            return state

        self._add_supervision_cost(job_id, review_id, advice, now)

        steering = Steering(self.store)
        unacked = [
            i for i in steering.all_instructions(job_id) if i["status"] in ("received", "delivered")
        ]
        if unacked:
            if now - state.in_flight_started_at >= cfg.advice_ttl_s:
                self._event(
                    job_id,
                    "trajectory_advice_discarded",
                    {"review_id": review_id, "reason": "stale_pending_ack"},
                    "advice_ttl_expired",
                    Judgment.code,
                )
                return triggers_module.resolve_review(
                    state, now, diff_lines=state.last_review_diff_lines, verdict=None
                )
            return state  # still deferred: never stack on an unacked instruction

        verdict = parse_verdict(advice.get("verdict")) if advice.get("verdict") else None
        ledger = TrajectoryLedger(self.store, job_id)
        snapshot = ledger.snapshot()
        snapshot["recoveries_used"] = self.store.kv_get(job_id, "recoveries_used", default=0)
        score = hysteresis.evidence_score(snapshot, budget_usd=cfg.triggers.budget_usd)
        hstate = self._load_hysteresis_state(job_id)
        in_settle = _in_settle(state, now)
        decision, new_hstate = hysteresis.decide(hstate, verdict, score, in_settle=in_settle, mode=mode, cfg=cfg.hysteresis)

        diff_samples = snapshot.get("lists", {}).get("diff_samples", [])
        diff_lines = diff_samples[-1].get("changed_lines", state.last_review_diff_lines) if diff_samples else state.last_review_diff_lines
        verdict_label = verdict.trajectory if verdict is not None else None

        if verdict is not None:
            sig = verdict_signature(verdict)
            repeat = self.store.kv_get(job_id, "trajectory_last_verdict_sig") == sig
            self.store.kv_set(job_id, "trajectory_last_verdict_sig", sig)
            noise = decision.kind in ("record_concern", "escalate_human") or (
                decision.kind == "hold" and verdict.trajectory != "sound"
            )
            if repeat and noise:
                # The same concern again adds nothing and must not re-page the owner (8a641f9b: 11 identical
                # reviews). State still advances; a verdict that would redirect is never suppressed.
                self._save_hysteresis_state(job_id, new_hstate)
                self._event(
                    job_id,
                    "trajectory_repeat_suppressed",
                    {"review_id": review_id, "trajectory": verdict.trajectory,
                     "action": verdict.recommendation.action, "would_have": decision.kind},
                    "identical_repeat_verdict",
                    Judgment.code,
                )
                return triggers_module.resolve_review(state, now, diff_lines=diff_lines, verdict=verdict_label)

        after_decision = self._apply_decision(
            job_id, review_id, decision, verdict, score, hstate, new_hstate, steering, mode, now, state
        )
        return triggers_module.resolve_review(after_decision, now, diff_lines=diff_lines, verdict=verdict_label)

    def _apply_decision(
        self,
        job_id: str,
        review_id: str,
        decision: hysteresis.Decision,
        verdict,
        score: float,
        hstate: hysteresis.HysteresisState,
        new_hstate: hysteresis.HysteresisState,
        steering: Steering,
        mode: str,
        now: float,
        state: triggers_module.TriggerState,
    ) -> triggers_module.TriggerState:
        if decision.kind == "redirect":
            return self._apply_redirect(
                job_id, review_id, decision, verdict, score, hstate, new_hstate, steering, mode, state, now
            )

        # every other decision kind: hysteresis state' is committed as-is —
        # only a redirect's own CONFLICT/shadow path leaves it untouched.
        self._save_hysteresis_state(job_id, new_hstate)

        if decision.kind == "escalate_human":
            observation = verdict.observation[:100] if verdict is not None else "trajectory escalation"
            self._notify(job_id, "NEEDS_YOU", "Steward: trajectory", observation)
            self._event(
                job_id,
                "trajectory_escalated",
                {"review_id": review_id, "rule": decision.rule, "detail": decision.detail},
                decision.rule,
                Judgment.model,
            )
        elif decision.kind == "record_concern":
            self._event(
                job_id,
                "trajectory_concern_recorded",
                {"review_id": review_id, "rule": decision.rule, "concern": verdict.concern if verdict else None},
                decision.rule,
                Judgment.model,
            )
        elif decision.kind == "discard":
            self._event(
                job_id,
                "trajectory_advice_discarded",
                {"review_id": review_id, "reason": decision.rule},
                decision.rule,
                Judgment.code,
            )
        else:  # hold
            self._event(
                job_id,
                "trajectory_hold",
                {"review_id": review_id, "rule": decision.rule},
                decision.rule,
                Judgment.model,
            )
        return state

    def _apply_redirect(
        self, job_id, review_id, decision, verdict, score, hstate, new_hstate, steering, mode, state, now
    ) -> triggers_module.TriggerState:
        rec = verdict.recommendation
        verb = "focus" if rec.action in _REFOCUS_VERBS else "nudge"
        text = f"[trajectory {review_id[:8]}] {rec.instruction}"
        meta = self.store.kv_get(job_id, _review_meta_key(review_id)) or {}
        expected_revision = meta.get("expected_revision")

        if mode != "active":
            # shadow: record the would-be redirect, never touch hysteresis
            # state or Steering (must-have: shadow leaves approach/discarded
            # sets unchanged).
            self._event(
                job_id,
                "trajectory_would_redirect",
                {
                    "review_id": review_id,
                    "rule": decision.rule,
                    "verb": verb,
                    "instruction": rec.instruction,
                    "evidence_score": score,
                },
                decision.rule,
                Judgment.model,
            )
            return state

        receipt = steering.record(
            job_id,
            verb,
            text,
            request_id=f"trajectory-{review_id}",
            origin="trajectory",
            human_instruction="",
            interpreted_instruction=rec.instruction,
            expected_revision=expected_revision,
        )

        if not receipt.accepted:
            # CONFLICT: a human steered since the review was requested.
            # Hysteresis state' is NOT committed (the redirect never applied).
            self._event(
                job_id,
                "trajectory_advice_discarded",
                {"review_id": review_id, "reason": "stale_after_human_steer", "detail": receipt.detail},
                "human_wins_conflict",
                Judgment.code,
            )
            return state

        if receipt.duplicate:
            # Already applied by an earlier (pre-restart) pass: no second
            # mutation of hysteresis state, no second event — just let the
            # (re-opened) review resolve so the in-flight marker clears.
            return triggers_module.start_settle(state, now, self._current_cfg().triggers)

        self._save_hysteresis_state(job_id, new_hstate)
        rev = steering.all_instructions(job_id)[-1]["rev"]
        self.store.kv_set(job_id, _LAST_REDIRECT_REV_KEY, rev)
        self._event(
            job_id,
            "trajectory_redirect_authorized",
            {
                "review_id": review_id,
                "rule": decision.rule,
                "verb": verb,
                "rev": rev,
                "verdict": verdict_to_dict(verdict),
                "evidence_score": score,
            },
            decision.rule,
            Judgment.model_informed_code_authorized,
        )
        return triggers_module.start_settle(state, now, self._current_cfg().triggers)

    def _add_supervision_cost(self, job_id: str, review_id: str, advice: dict, now: float) -> None:
        # Once per review: deferred advice is re-read every tick while it waits on an ack.
        marker = f"trajectory_cost_recorded:{review_id}"
        if self.store.kv_get(job_id, marker):
            return
        cost_usd = advice.get("cost_usd", 0.0) or 0.0
        if cost_usd:
            TrajectoryLedger(self.store, job_id).add_cost_point(cost_usd, attempt_no=0, source="supervision", now=now)
        self.store.kv_set(job_id, marker, True)

    def request_review_now(self, job_id: str, reason: str) -> str:
        """An on-demand review (Jev's looping/off_track escalation when no custom watcher exists).
        Same guards as a scheduled one: mode off, a review already in flight, the budget cap and a
        missing advisor pane all decline it. Returns a short detail."""
        cfg = self._current_cfg()
        now = self.clock()
        if effective_mode(self.store, job_id, cfg).mode == "off":
            return "advisor is off; no review"
        state = self._load_trigger_state(job_id)
        if state.in_flight_review_id is not None:
            return "advisor review already in flight"
        ledger = TrajectoryLedger(self.store, job_id)
        snapshot = ledger.snapshot()
        snapshot["recoveries_used"] = self.store.kv_get(job_id, "recoveries_used", default=0)
        evaluation = triggers_module.manual_evaluation("observer_flag", {"reason": reason[:120]}, reason)
        new_state, detail = self._request_review(job_id, ledger, snapshot, evaluation, cfg, state, now)
        self._save_trigger_state(job_id, new_state)
        return detail

    # -- step 3: request a review -------------------------------------------

    def _request_review(
        self,
        job_id: str,
        ledger: TrajectoryLedger,
        snapshot: dict,
        evaluation: triggers_module.Evaluation,
        cfg: TrajectoryConfig,
        state: triggers_module.TriggerState,
        now: float,
    ) -> tuple[triggers_module.TriggerState, str]:
        reviews_so_far = len(snapshot.get("lists", {}).get("reviews", []))
        supervision_spend = ledger.supervision_spend_usd()
        over_budget = reviews_so_far >= cfg.max_reviews_per_job or (
            cfg.supervision_budget_usd is not None and supervision_spend >= cfg.supervision_budget_usd
        )
        if over_budget:
            already = self.store.kv_get(job_id, _BUDGET_EXHAUSTED_KEY, default=False)
            if not already:
                self._event(
                    job_id,
                    "trajectory_budget_exhausted",
                    {"reviews": reviews_so_far, "supervision_spend_usd": supervision_spend},
                    "review_budget_cap",
                    Judgment.code,
                )
                self.store.kv_set(job_id, _BUDGET_EXHAUSTED_KEY, True)
            return state, "budget exhausted; no review requested"

        pane_id = self.advisor_link.find()
        trigger_kinds = [t.kind for t in evaluation.fired]
        if pane_id is None:
            self._event(
                job_id,
                "trajectory_review_skipped",
                {"reason": "no_advisor_pane", "triggers": trigger_kinds},
                "no_advisor_pane",
                Judgment.code,
            )
            return state, "no advisor pane; review skipped"

        review_id = str(uuid.uuid4())
        contract = load_contract(self.store, job_id)
        hstate = self._load_hysteresis_state(job_id)
        packet = build_packet(
            snapshot, contract, evaluation.fired, hstate.open_concerns, hstate.current_approach, list(hstate.discarded)
        )
        self.store.kv_set(job_id, _packet_key(review_id), packet)
        self.store.kv_set(
            job_id,
            _review_meta_key(review_id),
            {"expected_revision": self.store.get_revision(job_id), "requested_at": now},
        )
        ledger.add_review(review_id, trigger_kinds, now)
        self.advisor_link.request(pane_id, job_id, review_id)
        self._event(
            job_id,
            "trajectory_review_requested",
            {"review_id": review_id, "triggers": trigger_kinds},
            "triggers_evaluate",
            Judgment.code,
        )
        new_state = triggers_module.begin_review(state, review_id, now)
        return new_state, f"review {review_id[:8]} requested"


def verdict_signature(verdict) -> str:
    """Stable identity of a verdict for repeat suppression; the free-text observation is left out."""
    rec = verdict.recommendation
    return "|".join((verdict.trajectory, verdict.concern, rec.action, rec.instruction,
                     rec.current_approach, rec.recommended_approach))


def _in_settle(state: triggers_module.TriggerState, now: float) -> bool:
    """Mirrors `triggers.evaluate`'s own settle-window predicate exactly, so
    the decision converting the CURRENT review's verdict sees the same
    "are we inside a post-redirect settle window" answer evaluate() would."""
    return (now < state.settle_until) or (not state.settle_satisfied and state.settle_until > 0)


def _advice_key(review_id: str) -> str:
    return f"{_ADVICE_KEY_PREFIX}{review_id}"


def _packet_key(review_id: str) -> str:
    return f"{_PACKET_KEY_PREFIX}{review_id}"


def _review_meta_key(review_id: str) -> str:
    return f"{_REVIEW_META_KEY_PREFIX}{review_id}"
