"""Deterministic trigger detectors + cadence/debounce (TRJ-04, TRJ-05).

Every function here is pure: no I/O, no clock reads (`now` is always passed
in), no model call. `evaluate()` is the outer loop's only decision point for
"is a review due right now?" — everything it needs comes from a
`TrajectoryLedger.snapshot()` dict, a `TrajectoryLedger.since(marker)` deltas
dict, the previous `TriggerState`, and a `TriggerConfig` (12-CONTEXT D5).

`recoveries_used` is not itself a ledger section (it lives in
`steward.recovery`'s kv keys); the caller is expected to fold it into the
snapshot dict as `snapshot["recoveries_used"]` before calling `evaluate()` so
`recovery_budget_low` stays a pure function of its inputs like every other
detector (12-03 wires this; nothing here reaches into the store).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

# -- config / state -----------------------------------------------------


@dataclass(frozen=True)
class TriggerConfig:
    cadence_s: float = 2700
    steady_cadence_s: float = 3600
    min_gap_s: float = 600
    settle_s: float = 1800
    review_timeout_s: float = 600
    diff_growth_ratio: float = 2.0
    diff_growth_min_lines: float = 300
    expected_diff_lines: float | None = None
    budget_usd: float | None = None
    time_marks_s: tuple[float, ...] = (10800, 14400)
    plan_jaccard: float = 0.5
    structural_renames: int = 5
    # Not in the D5 table (which only defines the ledger-facing detectors):
    # `recovery_budget_low` needs the same denominator `steward.recovery`
    # uses, so the caller can pass its `RecoveryPolicy.max_recoveries` here.
    max_recoveries: int = 2


@dataclass(frozen=True)
class TriggerState:
    last_review_at: float | None = None
    last_review_diff_lines: float = 0.0
    fired_bands: tuple[str, ...] = ()
    last_plan: tuple[str, ...] = ()
    seen_manifests: tuple[str, ...] = ()
    settle_until: float = 0.0
    settle_satisfied: bool = True
    # D5/TRJ-05 (chair-authorized fix, 12-03): the settle window only clears
    # via evidence that arrives *after* the redirect's ack, not merely after
    # the window opened. `None` means "the ack has not been observed yet" —
    # no amount of evidence can satisfy the window early. The caller
    # (`steward.trajectory.TrajectorySupervisor.step`) sets this once it
    # observes the redirect's steering instruction reach `acknowledged`.
    settle_evidence_after: float | None = None
    in_flight_review_id: str | None = None
    in_flight_started_at: float = 0.0
    pending: tuple[dict, ...] = ()  # [{kind, detail, urgent}], deduped by kind
    last_verdict: str | None = None
    # 12-04 gate finding: `public_api_changes`/`interface_files` entries are a
    # structural comparison against a FIXED base commit, so a file that
    # diverged from base stays "changed" in every future diff sample until
    # it is fixed or the base moves — without a seen-set this trigger would
    # re-fire every review from now on, the same already-reported fact,
    # unlike every other D5 detector (which reads deltas *since the last
    # review*). Mirrors `seen_manifests`' "was not seen before" semantics.
    seen_interface_changes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "last_review_at": self.last_review_at,
            "last_review_diff_lines": self.last_review_diff_lines,
            "fired_bands": list(self.fired_bands),
            "last_plan": list(self.last_plan),
            "seen_manifests": list(self.seen_manifests),
            "settle_until": self.settle_until,
            "settle_satisfied": self.settle_satisfied,
            "settle_evidence_after": self.settle_evidence_after,
            "in_flight_review_id": self.in_flight_review_id,
            "in_flight_started_at": self.in_flight_started_at,
            "pending": [dict(p) for p in self.pending],
            "last_verdict": self.last_verdict,
            "seen_interface_changes": list(self.seen_interface_changes),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TriggerState":
        return cls(
            last_review_at=data.get("last_review_at"),
            last_review_diff_lines=data.get("last_review_diff_lines", 0.0),
            fired_bands=tuple(data.get("fired_bands", ())),
            last_plan=tuple(data.get("last_plan", ())),
            seen_manifests=tuple(data.get("seen_manifests", ())),
            settle_until=data.get("settle_until", 0.0),
            settle_satisfied=data.get("settle_satisfied", True),
            settle_evidence_after=data.get("settle_evidence_after"),
            in_flight_review_id=data.get("in_flight_review_id"),
            in_flight_started_at=data.get("in_flight_started_at", 0.0),
            pending=tuple(data.get("pending", ())),
            last_verdict=data.get("last_verdict"),
            seen_interface_changes=tuple(data.get("seen_interface_changes", ())),
        )


@dataclass(frozen=True)
class Trigger:
    kind: str
    detail: dict = field(default_factory=dict)
    urgent: bool = False


@dataclass(frozen=True)
class Evaluation:
    fired: list[Trigger]
    review_due: bool
    urgent: bool
    reason: str


def manual_evaluation(kind: str, detail: dict, reason: str) -> Evaluation:
    """An on-demand (non-urgent) review request, e.g. Jev's looping/off_track escalation."""
    return Evaluation(fired=[Trigger(kind=kind, detail=detail)], review_due=True, urgent=False, reason=reason)


# -- helpers over a ledger snapshot dict ----------------------------------


def _lists(snapshot: dict) -> dict:
    return snapshot.get("lists", {})


def _worker_spend(snapshot: dict) -> float:
    by_attempt: dict[int, list[float]] = {}
    for point in _lists(snapshot).get("cost_points", []):
        if point.get("source") != "worker":
            continue
        by_attempt.setdefault(point["attempt_no"], []).append(point["cost_usd"])
    total = 0.0
    for values in by_attempt.values():
        non_decreasing = all(values[i] <= values[i + 1] for i in range(len(values) - 1))
        total += max(values) if non_decreasing else sum(values)
    return total


def _supervision_spend(snapshot: dict) -> float:
    return sum(p["cost_usd"] for p in _lists(snapshot).get("cost_points", []) if p.get("source") == "supervision")


def _elapsed_s(snapshot: dict, now: float) -> float:
    start = snapshot.get("started_at")
    if start is None:
        return 0.0
    return max(0.0, now - start)


def _spend_rate(snapshot: dict, window_s: float, now: float) -> float:
    if window_s <= 0:
        return 0.0
    cutoff = now - window_s
    total = sum(p["cost_usd"] for p in _lists(snapshot).get("cost_points", []) if p.get("ts", 0) >= cutoff)
    return total / window_s


def _jaccard_distance(a: set, b: set) -> float:
    union = a | b
    if not union:
        return 0.0
    return 1 - len(a & b) / len(union)


# -- the 10 D5 detectors ----------------------------------------------------


def _detect_phase_boundary(deltas: dict, snapshot: dict, state: TriggerState, cfg: TriggerConfig, now: float) -> Trigger | None:
    hits = [m for m in deltas.get("milestones", []) if m.get("kind") in ("done", "closeout_round", "summary_commit")]
    if not hits:
        return None
    return Trigger(kind="phase_boundary", detail={"count": len(hits), "kinds": sorted({m["kind"] for m in hits})})


def _detect_acceptance_changed(deltas: dict, snapshot: dict, state: TriggerState, cfg: TriggerConfig, now: float) -> Trigger | None:
    contract_revisions = [m for m in deltas.get("milestones", []) if m.get("kind") == "contract_revision"]
    acceptance_revisions = deltas.get("acceptance_revisions", [])
    weakening_hits = [d for d in deltas.get("diff_samples", []) if d.get("weakening")]
    if not (contract_revisions or acceptance_revisions or weakening_hits):
        return None
    return Trigger(
        kind="acceptance_changed",
        detail={
            "contract_revisions": len(contract_revisions),
            "acceptance_revisions": len(acceptance_revisions),
            "weakening_samples": len(weakening_hits),
        },
    )


def _detect_redesign_proposed(deltas: dict, snapshot: dict, state: TriggerState, cfg: TriggerConfig, now: float) -> Trigger | None:
    detail: dict = {}
    fired = False

    plan_snapshots = deltas.get("plan", [])
    if plan_snapshots:
        latest_items = set(plan_snapshots[-1].get("items", []))
        previous_items = set(state.last_plan)
        if len(latest_items) >= 3 and len(previous_items) >= 3:
            distance = _jaccard_distance(previous_items, latest_items)
            if distance >= cfg.plan_jaccard:
                fired = True
                detail["plan_jaccard_distance"] = distance

    diff_samples = deltas.get("diff_samples", [])
    total_deleted_or_renamed = sum(d.get("deleted_or_renamed", 0) for d in diff_samples)
    new_dirs: set = set()
    for d in diff_samples:
        new_dirs |= set(d.get("new_top_level_dirs", []))

    if total_deleted_or_renamed >= cfg.structural_renames:
        fired = True
        detail["deleted_or_renamed"] = total_deleted_or_renamed
    if new_dirs:
        fired = True
        detail["new_top_level_dirs"] = sorted(new_dirs)

    if not fired:
        return None
    return Trigger(kind="redesign_proposed", detail=detail)


def _detect_failed_approaches(deltas: dict, snapshot: dict, state: TriggerState, cfg: TriggerConfig, now: float) -> Trigger | None:
    count = len(deltas.get("failed_approaches", []))
    if count < 2:
        return None
    return Trigger(kind="failed_approaches", detail={"count": count})


def _detect_diff_growth(deltas: dict, snapshot: dict, state: TriggerState, cfg: TriggerConfig, now: float) -> Trigger | None:
    diff_samples = _lists(snapshot).get("diff_samples", [])
    if not diff_samples:
        return None
    lines = diff_samples[-1].get("changed_lines", 0)
    prev = state.last_review_diff_lines
    fired = (lines - prev >= cfg.diff_growth_min_lines) and (lines >= cfg.diff_growth_ratio * max(prev, cfg.diff_growth_min_lines))
    if not fired and cfg.expected_diff_lines:
        fired = lines > 1.5 * cfg.expected_diff_lines
    if not fired:
        return None
    return Trigger(kind="diff_growth", detail={"lines": lines, "prev": prev})


def _detect_new_dependency(deltas: dict, snapshot: dict, state: TriggerState, cfg: TriggerConfig, now: float) -> Trigger | None:
    manifests: set = set()
    for d in deltas.get("diff_samples", []):
        manifests |= set(d.get("manifests_changed", []))
    new_ones = manifests - set(state.seen_manifests)
    if not new_ones:
        return None
    return Trigger(kind="new_dependency", detail={"manifests": sorted(new_ones)})


def _detect_public_interface_change(deltas: dict, snapshot: dict, state: TriggerState, cfg: TriggerConfig, now: float) -> Trigger | None:
    hits = [d for d in deltas.get("diff_samples", []) if d.get("public_api_changes") or d.get("interface_files")]
    if not hits:
        return None
    api_changes = sorted({c for d in hits for c in d.get("public_api_changes", [])})
    interface_files = sorted({f for d in hits for f in d.get("interface_files", [])})
    seen = set(state.seen_interface_changes)
    new_api_changes = [c for c in api_changes if c not in seen]
    new_interface_files = [f for f in interface_files if f not in seen]
    if not new_api_changes and not new_interface_files:
        return None
    return Trigger(
        kind="public_interface_change",
        detail={"public_api_changes": new_api_changes, "interface_files": new_interface_files},
    )


def _detect_recovery_budget_low(deltas: dict, snapshot: dict, state: TriggerState, cfg: TriggerConfig, now: float) -> Trigger | None:
    recoveries_used = snapshot.get("recoveries_used", 0)
    if recoveries_used < cfg.max_recoveries - 1:
        return None
    band = f"recovery_budget_low:{recoveries_used}"
    if band in state.fired_bands:
        return None
    return Trigger(kind="recovery_budget_low", detail={"recoveries_used": recoveries_used, "max_recoveries": cfg.max_recoveries}, urgent=True)


def _detect_spend_time_anomaly(
    deltas: dict, snapshot: dict, state: TriggerState, cfg: TriggerConfig, now: float
) -> tuple[Trigger | None, tuple[str, ...]]:
    elapsed = _elapsed_s(snapshot, now)
    total_spend = _worker_spend(snapshot) + _supervision_spend(snapshot)
    reasons: dict = {}
    new_bands: list[str] = []
    urgent = False

    if elapsed >= 3600:
        job_avg_rate = total_spend / elapsed if elapsed > 0 else 0.0
        recent_rate = _spend_rate(snapshot, 1800, now)
        if job_avg_rate > 0 and recent_rate > 3 * job_avg_rate:
            reasons["rate_anomaly"] = {"recent_rate": recent_rate, "job_avg_rate": job_avg_rate}

    if cfg.budget_usd:
        pct = (total_spend / cfg.budget_usd) * 100
        for band_pct in (50, 80, 100):
            band_key = f"budget:{band_pct}"
            if pct >= band_pct and band_key not in state.fired_bands:
                new_bands.append(band_key)
                reasons.setdefault("budget_bands", []).append(band_pct)
                if band_pct == 100:
                    urgent = True

    for mark in cfg.time_marks_s:
        band_key = f"time:{mark}"
        if elapsed >= mark and band_key not in state.fired_bands:
            new_bands.append(band_key)
            reasons.setdefault("time_marks", []).append(mark)

    if not reasons:
        return None, ()
    return Trigger(kind="spend_time_anomaly", detail=reasons, urgent=urgent), tuple(new_bands)


def _worker_test_points_non_decreasing(deltas: dict) -> bool:
    points = sorted(
        (p for p in deltas.get("test_points", []) if p.get("source") == "worker_observed"),
        key=lambda p: p.get("ts", 0),
    )
    passed_seq = [p["passed"] for p in points]
    return all(passed_seq[i] <= passed_seq[i + 1] for i in range(len(passed_seq) - 1))


def _is_steady(other_fired: list[Trigger], deltas: dict, state: TriggerState) -> bool:
    if other_fired:
        return False
    if not _worker_test_points_non_decreasing(deltas):
        return False
    if deltas.get("failed_approaches"):
        return False
    return state.last_verdict in (None, "sound")


def _detect_cadence(state: TriggerState, snapshot: dict, cfg: TriggerConfig, now: float, steady: bool) -> Trigger | None:
    reference = state.last_review_at if state.last_review_at is not None else snapshot.get("started_at", now)
    since_last = now - reference
    threshold = cfg.steady_cadence_s if steady else cfg.cadence_s
    if since_last < threshold:
        return None
    return Trigger(kind="cadence", detail={"since_last_review_s": since_last, "steady": steady})


# -- pending merge -----------------------------------------------------------


def _merge_pending(pending: tuple[dict, ...], triggers: list[Trigger]) -> tuple[dict, ...]:
    merged = {p["kind"]: p for p in pending}
    for t in triggers:
        merged[t.kind] = {"kind": t.kind, "detail": t.detail, "urgent": t.urgent}
    return tuple(merged.values())


def _as_triggers(dicts: tuple[dict, ...]) -> list[Trigger]:
    return [Trigger(kind=d["kind"], detail=d["detail"], urgent=d["urgent"]) for d in dicts]


# -- settle window control (called by the caller around a redirect) --------


def start_settle(state: TriggerState, now: float, cfg: TriggerConfig) -> TriggerState:
    """The caller invokes this once, right after converting a review into an
    authorized redirect. Non-urgent triggers are deferred until the window
    ends (time AND post-ack evidence, D5). `settle_evidence_after` resets to
    `None` — the ack has not been observed yet, so no evidence can satisfy
    the window until the caller records the ack (see `TriggerState`'s own
    docstring)."""
    return replace(state, settle_until=now + cfg.settle_s, settle_satisfied=False, settle_evidence_after=None)


def begin_review(state: TriggerState, review_id: str, now: float) -> TriggerState:
    """The caller invokes this when it actually sends a review request, so
    `evaluate()` knows one is in flight (at most one at a time, D5)."""
    return replace(state, in_flight_review_id=review_id, in_flight_started_at=now)


def resolve_review(state: TriggerState, now: float, *, diff_lines: float, verdict: str | None) -> TriggerState:
    """The caller invokes this when advice for `state.in_flight_review_id`
    arrives (or is discarded), clearing the in-flight marker and updating the
    `diff_growth` baseline / last verdict for the next evaluation."""
    return replace(
        state,
        in_flight_review_id=None,
        in_flight_started_at=0.0,
        last_review_at=now,
        last_review_diff_lines=diff_lines,
        last_verdict=verdict,
    )


def _settle_evidence_arrived(deltas: dict, after: float | None) -> bool:
    """D5: a `phase_boundary`-source fact (a `done`/`closeout_round`/
    `summary_commit` milestone) or a `test_result` point counts toward
    clearing the settle window only when its own `ts` is strictly after
    `after` (the observed ack time) — never merely "in these deltas"."""
    if after is None:
        return False
    if any(p.get("ts", 0) > after for p in deltas.get("test_points", [])):
        return True
    return any(
        m.get("ts", 0) > after and m.get("kind") in ("done", "closeout_round", "summary_commit")
        for m in deltas.get("milestones", [])
    )


# -- the outer evaluate() ----------------------------------------------------


def evaluate(ledger_snapshot: dict, deltas: dict, state: TriggerState, cfg: TriggerConfig, now: float) -> tuple[Evaluation, TriggerState]:
    recovery_low = _detect_recovery_budget_low(deltas, ledger_snapshot, state, cfg, now)
    spend_anomaly, new_bands = _detect_spend_time_anomaly(deltas, ledger_snapshot, state, cfg, now)

    non_cadence_detectors = [
        _detect_phase_boundary(deltas, ledger_snapshot, state, cfg, now),
        _detect_acceptance_changed(deltas, ledger_snapshot, state, cfg, now),
        _detect_redesign_proposed(deltas, ledger_snapshot, state, cfg, now),
        _detect_failed_approaches(deltas, ledger_snapshot, state, cfg, now),
        _detect_diff_growth(deltas, ledger_snapshot, state, cfg, now),
        _detect_new_dependency(deltas, ledger_snapshot, state, cfg, now),
        _detect_public_interface_change(deltas, ledger_snapshot, state, cfg, now),
        recovery_low,
        spend_anomaly,
    ]
    non_cadence_fired = [t for t in non_cadence_detectors if t is not None]

    steady = _is_steady(non_cadence_fired, deltas, state)
    cadence = _detect_cadence(state, ledger_snapshot, cfg, now, steady)

    this_tick_fired = non_cadence_fired + ([cadence] if cadence else [])

    # -- state updates that always happen, independent of debounce --------
    new_last_plan = state.last_plan
    plan_snapshots = deltas.get("plan", [])
    if plan_snapshots:
        new_last_plan = tuple(plan_snapshots[-1].get("items", []))

    new_seen_manifests = set(state.seen_manifests)
    for d in deltas.get("diff_samples", []):
        new_seen_manifests |= set(d.get("manifests_changed", []))

    new_seen_interface_changes = set(state.seen_interface_changes)
    for d in deltas.get("diff_samples", []):
        new_seen_interface_changes |= set(d.get("public_api_changes", []))
        new_seen_interface_changes |= set(d.get("interface_files", []))

    new_fired_bands = set(state.fired_bands) | set(new_bands)
    if recovery_low is not None:
        new_fired_bands.add(f"recovery_budget_low:{recovery_low.detail['recoveries_used']}")

    # -- in-flight timeout -------------------------------------------------
    in_flight_review_id = state.in_flight_review_id
    in_flight_started_at = state.in_flight_started_at
    if in_flight_review_id is not None and now - in_flight_started_at >= cfg.review_timeout_s:
        in_flight_review_id = None
        in_flight_started_at = 0.0

    # -- settle window: time AND post-ack evidence ---------------------------
    # (D5/TRJ-05, chair-authorized fix, 12-03): evidence only counts when it
    # arrived strictly after `state.settle_evidence_after` — the ack
    # timestamp the caller records once it observes the redirect's
    # instruction acknowledged. Before that (`settle_evidence_after is
    # None`), no phase_boundary/test_result can satisfy the window early,
    # even if one arrives in these very deltas.
    new_evidence_arrived = _settle_evidence_arrived(deltas, state.settle_evidence_after)
    settle_satisfied = state.settle_satisfied or new_evidence_arrived
    in_settle = (now < state.settle_until) or (not settle_satisfied and state.settle_until > 0)

    urgent_fired = [t for t in this_tick_fired if t.urgent]
    non_urgent_fired = [t for t in this_tick_fired if not t.urgent]
    pending_candidate = _merge_pending(state.pending, non_urgent_fired)

    has_had_review = state.last_review_at is not None
    gap_elapsed = (not has_had_review) or (now - state.last_review_at >= cfg.min_gap_s)

    review_in_flight = in_flight_review_id is not None

    if review_in_flight:
        review_due = False
        reason = "review_in_flight"
        pending_out = _merge_pending(pending_candidate, urgent_fired)
        fired_out = this_tick_fired
    elif urgent_fired:
        review_due = True
        reason = "urgent"
        fired_out = _as_triggers(pending_candidate) + urgent_fired
        pending_out = ()
    elif in_settle:
        review_due = False
        reason = "settle_window"
        pending_out = pending_candidate
        fired_out = this_tick_fired
    elif pending_candidate and gap_elapsed:
        review_due = True
        reason = "debounced_review"
        fired_out = _as_triggers(pending_candidate)
        pending_out = ()
    elif pending_candidate:
        review_due = False
        reason = "min_gap_not_elapsed"
        pending_out = pending_candidate
        fired_out = this_tick_fired
    else:
        review_due = False
        reason = "steady" if steady else "no_trigger"
        pending_out = ()
        fired_out = this_tick_fired

    new_state = replace(
        state,
        last_plan=new_last_plan,
        seen_manifests=tuple(sorted(new_seen_manifests)),
        fired_bands=tuple(sorted(new_fired_bands)),
        in_flight_review_id=in_flight_review_id,
        in_flight_started_at=in_flight_started_at,
        settle_satisfied=settle_satisfied,
        pending=pending_out,
        seen_interface_changes=tuple(sorted(new_seen_interface_changes)),
    )

    evaluation = Evaluation(fired=fired_out, review_due=review_due, urgent=bool(urgent_fired), reason=reason)
    return evaluation, new_state
