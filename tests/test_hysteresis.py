from steward.hysteresis import (
    Decision,
    HysteresisConfig,
    HysteresisState,
    decide,
    evidence_score,
)
from steward.review import Recommendation, TrajectoryVerdict


def _verdict(
    trajectory="questionable",
    confidence=0.7,
    urgency="normal",
    concern="wrong_approach",
    action="change_approach",
    recommended_approach="plan-b",
    current_approach="plan-a",
    instruction="switch",
    observation="obs",
):
    return TrajectoryVerdict(
        trajectory=trajectory,
        confidence=confidence,
        urgency=urgency,
        concern=concern,
        observation=observation,
        recommendation=Recommendation(
            action=action,
            instruction=instruction,
            current_approach=current_approach,
            recommended_approach=recommended_approach,
        ),
    )


def _snapshot(**lists) -> dict:
    base = {
        "acceptance_revisions": [], "plan": [], "decisions": [], "discarded_approaches": [],
        "milestones": [], "diff_samples": [], "test_points": [], "cost_points": [],
        "failed_approaches": [], "reviews": [],
    }
    base.update(lists)
    return {"started_at": 0, "intent": None, "lists": base, "dropped": {}}


# ------------------------------------------------------------------- rule 1


def test_sound_trajectory_holds():
    decision, state = decide(HysteresisState(), _verdict(trajectory="sound", concern="none"), 0.0, in_settle=False, mode="active")
    assert decision.kind == "hold"
    assert decision.rule == "sound_hold"
    assert state.review_idx == 1


def test_concern_none_holds_even_if_trajectory_is_questionable():
    decision, _ = decide(HysteresisState(), _verdict(trajectory="questionable", concern="none"), 0.0, in_settle=False, mode="active")
    assert decision.kind == "hold"
    assert decision.rule == "sound_hold"


# ------------------------------------------------------------------- rule 2


def test_mode_off_discards():
    decision, _ = decide(HysteresisState(), _verdict(), 5.0, in_settle=False, mode="off")
    assert decision.kind == "discard"
    assert decision.rule == "mode_off"


# ------------------------------------------------------------------- rule 3


def test_oscillation_guard_escalates_instead_of_redirecting_to_a_discarded_approach():
    state = HysteresisState(current_approach="plan-b", discarded=("plan-a",))
    verdict = _verdict(
        trajectory="off_track", confidence=0.95, urgency="high",
        action="change_approach", recommended_approach="plan-a",
    )
    decision, new_state = decide(state, verdict, 10.0, in_settle=False, mode="active")
    assert decision.kind == "escalate_human"
    assert decision.rule == "oscillation_guard"
    assert new_state.current_approach == "plan-b"  # never actually redirected


# ------------------------------------------------------------------- rule 4


def test_obvious_problem_redirects_on_first_sighting():
    verdict = _verdict(trajectory="off_track", confidence=0.95, urgency="high", recommended_approach="plan-b")
    decision, new_state = decide(HysteresisState(current_approach="plan-a"), verdict, 3.0, in_settle=False, mode="active")
    assert decision.kind == "redirect"
    assert decision.rule == "obvious_problem"
    assert new_state.current_approach == "plan-b"
    assert new_state.discarded == ("plan-a",)


def test_first_ever_redirect_discards_the_verdicts_own_current_approach():
    """12-04 gate finding: `HysteresisState()` (no redirect has ever
    happened yet) has `current_approach=None`; the approach being moved AWAY
    from can only come from the verdict's own `recommendation.current_approach`
    field the first time — otherwise nothing is ever added to `discarded`,
    and the oscillation guard (rule 3) could never catch a later attempt to
    go back to it."""
    verdict = _verdict(
        trajectory="off_track", confidence=0.95, urgency="high",
        current_approach="plan-a", recommended_approach="plan-b",
    )
    decision, new_state = decide(HysteresisState(), verdict, 3.0, in_settle=False, mode="active")
    assert decision.kind == "redirect"
    assert new_state.current_approach == "plan-b"
    assert new_state.discarded == ("plan-a",)


def test_not_obvious_when_urgency_is_not_high():
    verdict = _verdict(trajectory="off_track", confidence=0.95, urgency="normal")
    decision, _ = decide(HysteresisState(), verdict, 3.0, in_settle=False, mode="active")
    assert decision.kind == "record_concern"
    assert decision.rule == "first_sighting"


# ------------------------------------------------------------------- rule 5


def test_settle_window_only_records_a_concern():
    verdict = _verdict(trajectory="questionable", confidence=0.7, urgency="normal")
    decision, state = decide(HysteresisState(), verdict, 3.0, in_settle=True, mode="active")
    assert decision.kind == "record_concern"
    assert decision.rule == "settle_window"
    assert "wrong_approach" in state.open_concerns


# --------------------------------------------------------------- rules 6/7


def test_first_sighting_only_records_a_concern():
    decision, state = decide(HysteresisState(), _verdict(confidence=0.7), 4.0, in_settle=False, mode="active")
    assert decision.kind == "record_concern"
    assert decision.rule == "first_sighting"
    assert state.open_concerns["wrong_approach"] == {
        "confidence": 0.7, "evidence_score": 4.0, "sightings": 1, "last_review_idx": 1,
    }


def test_repeat_without_higher_confidence_or_worse_evidence_holds():
    state = HysteresisState(open_concerns={"wrong_approach": {"confidence": 0.7, "evidence_score": 4.0, "sightings": 1, "last_review_idx": 1}}, review_idx=1)
    decision, new_state = decide(state, _verdict(confidence=0.65, concern="wrong_approach"), 4.0, in_settle=False, mode="active")
    assert decision.kind == "hold"
    assert decision.rule == "concern_not_confirmed"
    assert new_state.open_concerns["wrong_approach"]["confidence"] == 0.65


def test_repeat_with_both_higher_confidence_and_worse_evidence_redirects():
    state = HysteresisState(
        current_approach="plan-a",
        open_concerns={"wrong_approach": {"confidence": 0.7, "evidence_score": 4.0, "sightings": 1, "last_review_idx": 1}},
        review_idx=1,
    )
    verdict = _verdict(confidence=0.8, concern="wrong_approach", recommended_approach="plan-b")
    decision, new_state = decide(state, verdict, 5.0, in_settle=False, mode="active")
    assert decision.kind == "redirect"
    assert decision.rule == "concern_confirmed"
    assert new_state.current_approach == "plan-b"
    assert new_state.discarded == ("plan-a",)
    assert "wrong_approach" not in new_state.open_concerns


def test_repeat_requires_confidence_above_min_redirect_confidence_too():
    """Higher than previous AND worse evidence, but under min_redirect_confidence -> hold, not redirect."""
    cfg = HysteresisConfig(min_redirect_confidence=0.9)
    state = HysteresisState(
        open_concerns={"wrong_approach": {"confidence": 0.3, "evidence_score": 1.0, "sightings": 1, "last_review_idx": 1}},
        review_idx=1,
    )
    decision, _ = decide(state, _verdict(confidence=0.5, concern="wrong_approach"), 2.0, in_settle=False, mode="active", cfg=cfg)
    assert decision.kind == "hold"
    assert decision.rule == "concern_not_confirmed"


# --------------------------------------------------------------------- rule 8


def test_escalate_requested_overrides_a_first_sighting_hold():
    verdict = _verdict(action="escalate_human", confidence=0.5)
    decision, _ = decide(HysteresisState(), verdict, 1.0, in_settle=False, mode="active")
    assert decision.kind == "escalate_human"
    assert decision.rule == "escalate_requested"


def test_escalate_requested_overrides_a_concern_not_confirmed_hold():
    state = HysteresisState(
        open_concerns={"wrong_approach": {"confidence": 0.7, "evidence_score": 4.0, "sightings": 1, "last_review_idx": 1}},
        review_idx=1,
    )
    verdict = _verdict(action="escalate_human", confidence=0.65, concern="wrong_approach")
    decision, _ = decide(state, verdict, 4.0, in_settle=False, mode="active")
    assert decision.kind == "escalate_human"
    assert decision.rule == "escalate_requested"


def test_escalate_requested_never_overrides_a_redirect():
    state = HysteresisState(
        current_approach="plan-a",
        open_concerns={"wrong_approach": {"confidence": 0.7, "evidence_score": 4.0, "sightings": 1, "last_review_idx": 1}},
        review_idx=1,
    )
    verdict = _verdict(action="escalate_human", confidence=0.8, concern="wrong_approach", recommended_approach="plan-b")
    decision, _ = decide(state, verdict, 5.0, in_settle=False, mode="active")
    assert decision.kind == "redirect"
    assert decision.rule == "concern_confirmed"


# ------------------------------------------------------------------- expiry


def test_a_concern_closes_after_two_non_repeating_reviews():
    state = HysteresisState(
        open_concerns={"wrong_approach": {"confidence": 0.7, "evidence_score": 4.0, "sightings": 1, "last_review_idx": 1}},
        review_idx=1,
    )
    sound = _verdict(trajectory="sound", concern="none")

    decision1, state = decide(state, sound, 0.0, in_settle=False, mode="active")  # review 2: diff=1, not closed
    assert decision1.detail["closed_concerns"] == []
    assert "wrong_approach" in state.open_concerns

    decision2, state = decide(state, sound, 0.0, in_settle=False, mode="active")  # review 3: diff=2, closes
    assert decision2.detail["closed_concerns"] == ["wrong_approach"]
    assert "wrong_approach" not in state.open_concerns


# ---------------------------------------------------- A -> B -> A scenario


def test_a_to_b_to_a_is_escalated_never_redirected_even_at_high_confidence():
    state = HysteresisState(current_approach="A")

    # First sighting: recommend B.
    decision, state = decide(state, _verdict(confidence=0.6, recommended_approach="B", current_approach="A"), 3.0, in_settle=False, mode="active")
    assert decision.kind == "record_concern"

    # Repeat, confirmed: redirect A -> B.
    decision, state = decide(state, _verdict(confidence=0.8, recommended_approach="B", current_approach="A"), 4.0, in_settle=False, mode="active")
    assert decision.kind == "redirect"
    assert state.current_approach == "B"
    assert state.discarded == ("A",)

    # Now the model wants to go back to A (discarded) with an obvious-problem verdict: guard wins, not redirect.
    obvious_back_to_a = _verdict(
        trajectory="off_track", confidence=0.95, urgency="high",
        action="change_approach", recommended_approach="A", current_approach="B",
    )
    decision, state = decide(state, obvious_back_to_a, 10.0, in_settle=False, mode="active")
    assert decision.kind == "escalate_human"
    assert decision.rule == "oscillation_guard"
    assert state.current_approach == "B"

    # A second attempt to go back to A, still discarded: escalated again, never redirected.
    decision, state = decide(state, obvious_back_to_a, 10.0, in_settle=False, mode="active")
    assert decision.kind == "escalate_human"
    assert decision.rule == "oscillation_guard"
    assert state.current_approach == "B"


# -------------------------------------------------------------- flip-flop


def test_flip_flop_confidence_sequence_never_redirects():
    state = HysteresisState()

    decision, state = decide(state, _verdict(confidence=0.7), 4.0, in_settle=False, mode="active")
    assert decision.kind == "record_concern"

    decision, state = decide(state, _verdict(trajectory="sound", concern="none"), 0.0, in_settle=False, mode="active")
    assert decision.kind == "hold"
    assert decision.rule == "sound_hold"

    decision, state = decide(state, _verdict(confidence=0.65), 3.0, in_settle=False, mode="active")
    assert decision.kind == "hold"
    assert decision.rule == "concern_not_confirmed"


# --------------------------------------------------------------- no verdict


def test_no_verdict_holds_and_does_not_touch_open_concerns():
    state = HysteresisState(open_concerns={"wrong_approach": {"confidence": 0.7, "evidence_score": 4.0, "sightings": 1, "last_review_idx": 1}}, review_idx=1)
    decision, new_state = decide(state, None, 0.0, in_settle=False, mode="active")
    assert decision.kind == "hold"
    assert decision.rule == "no_verdict"
    assert new_state.open_concerns == state.open_concerns


# -------------------------------------------------------------- JSON round trip


def test_hysteresis_state_json_round_trip():
    state = HysteresisState(
        open_concerns={"wrong_approach": {"confidence": 0.7, "evidence_score": 4.0, "sightings": 2, "last_review_idx": 3}},
        current_approach="plan-b",
        discarded=("plan-a",),
        review_idx=3,
    )
    restored = HysteresisState.from_dict(state.to_dict())
    assert restored == state


def test_hysteresis_state_from_dict_defaults_for_a_fresh_job():
    restored = HysteresisState.from_dict({})
    assert restored == HysteresisState()


# ------------------------------------------------------------- evidence_score


def test_evidence_score_is_zero_for_an_empty_snapshot():
    assert evidence_score(_snapshot()) == 0.0


def test_evidence_score_counts_failed_approaches_and_recoveries():
    snapshot = _snapshot(failed_approaches=[{"kind": "x", "ref": "", "ts": 0}, {"kind": "y", "ref": "", "ts": 1}])
    snapshot["recoveries_used"] = 1
    assert evidence_score(snapshot) == 2 * 2 + 2 * 1


def test_evidence_score_counts_worker_test_regressions():
    snapshot = _snapshot(
        test_points=[
            {"passed": 10, "failed": 0, "source": "worker_observed", "ts": 0},
            {"passed": 8, "failed": 1, "source": "worker_observed", "ts": 1},
        ]
    )
    assert evidence_score(snapshot) == 3


def test_evidence_score_includes_diff_growth_ratio_capped_at_five():
    snapshot = _snapshot(diff_samples=[{"changed_lines": 100}, {"changed_lines": 5000}])
    assert evidence_score(snapshot) == 5.0


def test_evidence_score_counts_weakening_findings():
    snapshot = _snapshot(diff_samples=[{"changed_lines": 10, "weakening": ["skip_marker:x", "deleted_test:y"]}])
    assert evidence_score(snapshot) == 3 * 2


def test_evidence_score_budget_band_needs_budget_usd():
    snapshot = _snapshot(cost_points=[{"cost_usd": 90.0}])
    assert evidence_score(snapshot) == 0.0  # no budget configured -> band 0
    assert evidence_score(snapshot, budget_usd=100.0) == 2.0  # 90% -> band 2 (>=80, <100)
    assert evidence_score(snapshot, budget_usd=90.0) == 3.0  # 100% -> band 3
    assert evidence_score(snapshot, budget_usd=180.0) == 1.0  # 50% -> band 1
