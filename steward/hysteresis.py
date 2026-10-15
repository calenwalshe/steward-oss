"""The hysteresis policy: record first, redirect only on confirmation (TRJ-08).

`decide` is a pure function (no I/O, no clock, no model call) implementing
12-CONTEXT D7's rule order exactly, first match wins:

1. `sound_hold` — `trajectory == "sound"` or `concern == "none"` -> hold.
2. `mode_off` — the effective mode is `"off"` -> discard.
3. `oscillation_guard` — a `change_approach` back into the discarded set ->
   escalate_human, never a revision (SP-19). Applies even to an "obvious"
   verdict.
4. `obvious_problem` — `off_track` and `confidence >= obvious_confidence` and
   `urgency == "high"` -> redirect, even on a first sighting.
5. `settle_window` — inside the post-redirect settle window -> record_concern
   only.
6. `first_sighting` — a concern key not currently open -> record_concern
   (stores this review's confidence and evidence score).
7. `concern_confirmed` / `concern_not_confirmed` — a repeat of an open key:
   confirmed (higher confidence AND a worse evidence score AND confidence >=
   `min_redirect_confidence`) -> redirect; otherwise -> update the concern and
   hold.
8. `escalate_requested` — the model's own recommendation asked for
   `escalate_human`, but nothing stronger (3/4) already fired and the
   hysteresis outcome above would only have held or recorded a concern (6/7):
   escalate anyway rather than silently sitting on an explicit ask (DEVIATION,
   see 12-02-SUMMARY: this is the only way rule 8, listed last in D7, is ever
   reachable — every other concern-bearing path is already claimed by rule 6
   or 7 before rule 8's literal position in the list is reached).

`evidence_score` is the D7 formula, computed from ledger numbers only (never
the model): `2*failed_approaches_total + 3*worker_test_regressions +
2*recoveries_used + min(diff_growth_ratio, 5) + 3*weakening_findings +
budget_band`. A concern not re-raised for `concern_expiry_reviews` consecutive
reviews closes (removed from `open_concerns`, reported in `Decision.detail`).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from steward.review import TrajectoryVerdict


@dataclass(frozen=True)
class HysteresisConfig:
    min_redirect_confidence: float = 0.6
    obvious_confidence: float = 0.9
    concern_expiry_reviews: int = 2


@dataclass(frozen=True)
class HysteresisState:
    open_concerns: dict = field(default_factory=dict)
    current_approach: str | None = None
    discarded: tuple[str, ...] = ()
    review_idx: int = 0

    def to_dict(self) -> dict:
        return {
            "open_concerns": {k: dict(v) for k, v in self.open_concerns.items()},
            "current_approach": self.current_approach,
            "discarded": list(self.discarded),
            "review_idx": self.review_idx,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HysteresisState":
        return cls(
            open_concerns={k: dict(v) for k, v in data.get("open_concerns", {}).items()},
            current_approach=data.get("current_approach"),
            discarded=tuple(data.get("discarded", ())),
            review_idx=data.get("review_idx", 0),
        )


@dataclass(frozen=True)
class Decision:
    kind: str  # hold | record_concern | redirect | escalate_human | discard
    rule: str
    detail: dict = field(default_factory=dict)


# -- evidence_score -----------------------------------------------------------


def _worker_test_regressions(test_points: list[dict]) -> int:
    points = sorted(
        (p for p in test_points if p.get("source") == "worker_observed"),
        key=lambda p: p.get("ts", 0),
    )
    regressions = 0
    for prev, cur in zip(points, points[1:]):
        if cur.get("passed", 0) < prev.get("passed", 0) or cur.get("failed", 0) > prev.get("failed", 0):
            regressions += 1
    return regressions


def _diff_growth_ratio(diff_samples: list[dict]) -> float:
    if len(diff_samples) < 2:
        return 0.0
    latest = diff_samples[-1].get("changed_lines", 0)
    previous = diff_samples[-2].get("changed_lines", 0)
    return latest / max(previous, 1)


def _budget_band(cost_points: list[dict], budget_usd: float | None) -> float:
    if not budget_usd:
        return 0.0
    total = sum(p.get("cost_usd", 0.0) for p in cost_points)
    pct = (total / budget_usd) * 100
    if pct >= 100:
        return 3.0
    if pct >= 80:
        return 2.0
    if pct >= 50:
        return 1.0
    return 0.0


def evidence_score(snapshot: dict, *, budget_usd: float | None = None) -> float:
    """Pure, code-only score (D7): never computed by the model."""
    lists = snapshot.get("lists", {})
    failed_total = len(lists.get("failed_approaches", []))
    regressions = _worker_test_regressions(lists.get("test_points", []))
    recoveries_used = snapshot.get("recoveries_used", 0)
    diff_ratio = min(_diff_growth_ratio(lists.get("diff_samples", [])), 5.0)
    weakening_total = sum(len(d.get("weakening", [])) for d in lists.get("diff_samples", []))
    budget_band = _budget_band(lists.get("cost_points", []), budget_usd)

    return (
        2 * failed_total
        + 3 * regressions
        + 2 * recoveries_used
        + diff_ratio
        + 3 * weakening_total
        + budget_band
    )


# -- decide -------------------------------------------------------------------


def _touch_concern(open_concerns: dict, key: str, confidence: float, score: float, review_idx: int) -> None:
    prior = open_concerns.get(key)
    sightings = prior["sightings"] + 1 if prior else 1
    open_concerns[key] = {
        "confidence": confidence,
        "evidence_score": score,
        "sightings": sightings,
        "last_review_idx": review_idx,
    }


def _apply_redirect(state: HysteresisState, verdict: TrajectoryVerdict) -> tuple[str | None, tuple[str, ...]]:
    rec = verdict.recommendation
    # 12-04 gate finding: on the very FIRST redirect ever, `state.current_approach`
    # is still `None` (hysteresis has never tracked one) — falling back to the
    # verdict's own `current_approach` slug (D6 carries it for exactly this)
    # is the only way the approach being moved AWAY from ever lands in
    # `discarded`, which the oscillation guard (rule 3) depends on to block a
    # future A->B->A. Once hysteresis has its own tracked value, that wins
    # (it reflects any prior redirect, which the verdict's own field cannot).
    prior = state.current_approach or (rec.current_approach or None)
    discarded = state.discarded
    if prior and prior not in discarded:
        discarded = discarded + (prior,)
    new_current = rec.recommended_approach or prior
    return new_current, discarded


def _expire(open_concerns: dict, exclude_key: str | None, new_review_idx: int, cfg: HysteresisConfig) -> list[str]:
    closed = []
    for key, info in list(open_concerns.items()):
        if key == exclude_key:
            continue
        if new_review_idx - info["last_review_idx"] >= cfg.concern_expiry_reviews:
            del open_concerns[key]
            closed.append(key)
    return sorted(closed)


def decide(
    state: HysteresisState,
    verdict: TrajectoryVerdict | None,
    score: float,
    *,
    in_settle: bool,
    mode: str,
    cfg: HysteresisConfig | None = None,
) -> tuple[Decision, HysteresisState]:
    cfg = cfg or HysteresisConfig()
    new_review_idx = state.review_idx + 1
    open_concerns = dict(state.open_concerns)

    if verdict is None:
        closed = _expire(open_concerns, None, new_review_idx, cfg)
        new_state = replace(state, open_concerns=open_concerns, review_idx=new_review_idx)
        return Decision("hold", "no_verdict", {"closed_concerns": closed}), new_state

    concern_key = verdict.concern
    action = verdict.recommendation.action

    # rule 1: sound_hold
    if verdict.trajectory == "sound" or verdict.concern == "none":
        closed = _expire(open_concerns, None, new_review_idx, cfg)
        new_state = replace(state, open_concerns=open_concerns, review_idx=new_review_idx)
        return Decision("hold", "sound_hold", {"closed_concerns": closed}), new_state

    # rule 2: mode_off
    if mode == "off":
        closed = _expire(open_concerns, concern_key, new_review_idx, cfg)
        new_state = replace(state, open_concerns=open_concerns, review_idx=new_review_idx)
        return Decision("discard", "mode_off", {"closed_concerns": closed}), new_state

    # rule 3: oscillation_guard
    if action == "change_approach" and verdict.recommendation.recommended_approach in state.discarded:
        closed = _expire(open_concerns, concern_key, new_review_idx, cfg)
        new_state = replace(state, open_concerns=open_concerns, review_idx=new_review_idx)
        detail = {"closed_concerns": closed, "recommended_approach": verdict.recommendation.recommended_approach}
        return Decision("escalate_human", "oscillation_guard", detail), new_state

    # rule 4: obvious_problem
    if verdict.trajectory == "off_track" and verdict.confidence >= cfg.obvious_confidence and verdict.urgency == "high":
        closed = _expire(open_concerns, concern_key, new_review_idx, cfg)
        open_concerns.pop(concern_key, None)
        new_current, new_discarded = _apply_redirect(state, verdict)
        new_state = replace(
            state, open_concerns=open_concerns, current_approach=new_current, discarded=new_discarded,
            review_idx=new_review_idx,
        )
        return Decision("redirect", "obvious_problem", {"closed_concerns": closed}), new_state

    # rule 5: settle_window
    if in_settle:
        closed = _expire(open_concerns, concern_key, new_review_idx, cfg)
        _touch_concern(open_concerns, concern_key, verdict.confidence, score, new_review_idx)
        new_state = replace(state, open_concerns=open_concerns, review_idx=new_review_idx)
        decision = Decision("record_concern", "settle_window", {"closed_concerns": closed})
        return _apply_escalate_requested_override(decision, action), new_state

    # rules 6/7: first sighting or repeat
    closed = _expire(open_concerns, concern_key, new_review_idx, cfg)
    prior = state.open_concerns.get(concern_key)
    if prior is None:
        _touch_concern(open_concerns, concern_key, verdict.confidence, score, new_review_idx)
        new_state = replace(state, open_concerns=open_concerns, review_idx=new_review_idx)
        decision = Decision("record_concern", "first_sighting", {"closed_concerns": closed})
        return _apply_escalate_requested_override(decision, action), new_state

    confirmed = (
        verdict.confidence > prior["confidence"]
        and score > prior["evidence_score"]
        and verdict.confidence >= cfg.min_redirect_confidence
    )
    if confirmed:
        open_concerns.pop(concern_key, None)
        new_current, new_discarded = _apply_redirect(state, verdict)
        new_state = replace(
            state, open_concerns=open_concerns, current_approach=new_current, discarded=new_discarded,
            review_idx=new_review_idx,
        )
        return Decision("redirect", "concern_confirmed", {"closed_concerns": closed}), new_state

    _touch_concern(open_concerns, concern_key, verdict.confidence, score, new_review_idx)
    new_state = replace(state, open_concerns=open_concerns, review_idx=new_review_idx)
    decision = Decision("hold", "concern_not_confirmed", {"closed_concerns": closed})
    return _apply_escalate_requested_override(decision, action), new_state


def _apply_escalate_requested_override(decision: Decision, action: str) -> Decision:
    """Rule 8 (D7, last in the written order): an explicit `escalate_human`
    recommendation always wins over a mere hold/record_concern outcome from
    rules 6/7 — see the module docstring's DEVIATION note."""
    if decision.kind in ("hold", "record_concern") and action == "escalate_human":
        return Decision("escalate_human", "escalate_requested", decision.detail)
    return decision
