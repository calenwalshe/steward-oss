"""Tests for steward/triggers.py (TRJ-04, TRJ-05).

Every detector is pure over a ledger snapshot dict + a deltas dict; `evaluate`
adds cadence, debounce, the settle window, in-flight tracking and band
once-only semantics on top (12-CONTEXT D5).
"""

from __future__ import annotations

from dataclasses import replace

from steward.triggers import (
    Trigger,
    TriggerConfig,
    TriggerState,
    begin_review,
    evaluate,
    resolve_review,
    start_settle,
)

_SECTIONS = (
    "acceptance_revisions", "plan", "decisions", "discarded_approaches", "milestones",
    "diff_samples", "test_points", "cost_points", "failed_approaches", "reviews",
)


def _snapshot(started_at=0.0, recoveries_used=0, **lists):
    base = {section: [] for section in _SECTIONS}
    base.update(lists)
    return {"started_at": started_at, "intent": None, "lists": base, "dropped": {}, "recoveries_used": recoveries_used}


def _deltas(**lists):
    base = {section: [] for section in _SECTIONS}
    base.update(lists)
    return base


def _kinds(ev):
    return {t.kind for t in ev.fired}


# ============================================================ detectors


# -- phase_boundary ----------------------------------------------------


def test_phase_boundary_fires_on_a_done_or_closeout_or_summary_milestone():
    cfg = TriggerConfig()
    state = TriggerState()
    snapshot = _snapshot()
    deltas = _deltas(milestones=[{"kind": "done", "ref": "x", "ts": 1}])
    ev, _ = evaluate(snapshot, deltas, state, cfg, now=1.0)
    assert "phase_boundary" in _kinds(ev)


def test_phase_boundary_does_not_fire_on_unrelated_milestone():
    cfg = TriggerConfig()
    state = TriggerState()
    snapshot = _snapshot()
    deltas = _deltas(milestones=[{"kind": "contract_revision", "ref": "x", "ts": 1}])
    ev, _ = evaluate(snapshot, deltas, state, cfg, now=1.0)
    assert "phase_boundary" not in _kinds(ev)


# -- acceptance_changed --------------------------------------------------


def test_acceptance_changed_fires_on_a_new_acceptance_revision():
    cfg = TriggerConfig()
    state = TriggerState()
    snapshot = _snapshot()
    deltas = _deltas(acceptance_revisions=[{"text": "scope narrowed", "source": "goal_steer", "ts": 1}])
    ev, _ = evaluate(snapshot, deltas, state, cfg, now=1.0)
    assert "acceptance_changed" in _kinds(ev)


def test_acceptance_changed_fires_on_new_weakening_findings():
    cfg = TriggerConfig()
    state = TriggerState()
    snapshot = _snapshot()
    deltas = _deltas(diff_samples=[{"weakening": ["deleted_test_file:tests/test_x.py"]}])
    ev, _ = evaluate(snapshot, deltas, state, cfg, now=1.0)
    assert "acceptance_changed" in _kinds(ev)


def test_acceptance_changed_does_not_fire_with_no_signal():
    cfg = TriggerConfig()
    state = TriggerState()
    snapshot = _snapshot()
    deltas = _deltas(diff_samples=[{"weakening": []}])
    ev, _ = evaluate(snapshot, deltas, state, cfg, now=1.0)
    assert "acceptance_changed" not in _kinds(ev)


# -- redesign_proposed ----------------------------------------------------


def test_redesign_proposed_fires_on_high_plan_jaccard_distance():
    cfg = TriggerConfig()
    state = TriggerState(last_plan=("a", "b", "c"))
    snapshot = _snapshot()
    deltas = _deltas(plan=[{"items": ["x", "y", "z"], "ts": 1}])
    ev, _ = evaluate(snapshot, deltas, state, cfg, now=1.0)
    assert "redesign_proposed" in _kinds(ev)


def test_redesign_proposed_does_not_fire_on_unchanged_plan():
    cfg = TriggerConfig()
    state = TriggerState(last_plan=("a", "b", "c"))
    snapshot = _snapshot()
    deltas = _deltas(plan=[{"items": ["a", "b", "c"], "ts": 1}])
    ev, _ = evaluate(snapshot, deltas, state, cfg, now=1.0)
    assert "redesign_proposed" not in _kinds(ev)


def test_redesign_proposed_ignored_below_three_items():
    cfg = TriggerConfig()
    state = TriggerState(last_plan=("a", "b"))
    snapshot = _snapshot()
    deltas = _deltas(plan=[{"items": ["x", "y"], "ts": 1}])
    ev, _ = evaluate(snapshot, deltas, state, cfg, now=1.0)
    assert "redesign_proposed" not in _kinds(ev)


def test_redesign_proposed_fires_on_structural_renames():
    cfg = TriggerConfig(structural_renames=5)
    state = TriggerState()
    snapshot = _snapshot()
    deltas = _deltas(diff_samples=[{"deleted_or_renamed": 5}])
    ev, _ = evaluate(snapshot, deltas, state, cfg, now=1.0)
    assert "redesign_proposed" in _kinds(ev)


def test_redesign_proposed_fires_on_new_top_level_dir():
    cfg = TriggerConfig()
    state = TriggerState()
    snapshot = _snapshot()
    deltas = _deltas(diff_samples=[{"new_top_level_dirs": ["newdir"]}])
    ev, _ = evaluate(snapshot, deltas, state, cfg, now=1.0)
    assert "redesign_proposed" in _kinds(ev)


# -- failed_approaches -----------------------------------------------------


def test_failed_approaches_fires_at_two():
    cfg = TriggerConfig()
    state = TriggerState()
    snapshot = _snapshot()
    deltas = _deltas(failed_approaches=[{"kind": "guide", "ref": "r1", "ts": 1}, {"kind": "replace", "ref": "r2", "ts": 2}])
    ev, _ = evaluate(snapshot, deltas, state, cfg, now=1.0)
    assert "failed_approaches" in _kinds(ev)


def test_failed_approaches_does_not_fire_at_one():
    cfg = TriggerConfig()
    state = TriggerState()
    snapshot = _snapshot()
    deltas = _deltas(failed_approaches=[{"kind": "guide", "ref": "r1", "ts": 1}])
    ev, _ = evaluate(snapshot, deltas, state, cfg, now=1.0)
    assert "failed_approaches" not in _kinds(ev)


# -- diff_growth -------------------------------------------------------


def test_diff_growth_fires_past_min_lines_and_ratio():
    cfg = TriggerConfig(diff_growth_min_lines=300, diff_growth_ratio=2.0)
    state = TriggerState(last_review_diff_lines=0.0)
    snapshot = _snapshot(diff_samples=[{"changed_lines": 700}])
    ev, _ = evaluate(snapshot, _deltas(), state, cfg, now=1.0)
    assert "diff_growth" in _kinds(ev)


def test_diff_growth_does_not_fire_below_min_lines():
    cfg = TriggerConfig(diff_growth_min_lines=300, diff_growth_ratio=2.0)
    state = TriggerState(last_review_diff_lines=0.0)
    snapshot = _snapshot(diff_samples=[{"changed_lines": 250}])
    ev, _ = evaluate(snapshot, _deltas(), state, cfg, now=1.0)
    assert "diff_growth" not in _kinds(ev)


def test_diff_growth_fires_on_expected_diff_lines_overrun():
    cfg = TriggerConfig(diff_growth_min_lines=10_000, expected_diff_lines=100)
    state = TriggerState(last_review_diff_lines=0.0)
    snapshot = _snapshot(diff_samples=[{"changed_lines": 200}])  # > 1.5x expected
    ev, _ = evaluate(snapshot, _deltas(), state, cfg, now=1.0)
    assert "diff_growth" in _kinds(ev)


# -- new_dependency -----------------------------------------------------


def test_new_dependency_fires_on_an_unseen_manifest():
    cfg = TriggerConfig()
    state = TriggerState(seen_manifests=())
    snapshot = _snapshot()
    deltas = _deltas(diff_samples=[{"manifests_changed": ["pyproject.toml"]}])
    ev, _ = evaluate(snapshot, deltas, state, cfg, now=1.0)
    assert "new_dependency" in _kinds(ev)


def test_new_dependency_does_not_refire_for_a_seen_manifest():
    cfg = TriggerConfig()
    state = TriggerState(seen_manifests=("pyproject.toml",))
    snapshot = _snapshot()
    deltas = _deltas(diff_samples=[{"manifests_changed": ["pyproject.toml"]}])
    ev, _ = evaluate(snapshot, deltas, state, cfg, now=1.0)
    assert "new_dependency" not in _kinds(ev)


# -- public_interface_change ------------------------------------------------


def test_public_interface_change_fires_on_public_api_removal():
    cfg = TriggerConfig()
    state = TriggerState()
    snapshot = _snapshot()
    deltas = _deltas(diff_samples=[{"public_api_changes": ["a.py:foo"]}])
    ev, _ = evaluate(snapshot, deltas, state, cfg, now=1.0)
    assert "public_interface_change" in _kinds(ev)


def test_public_interface_change_fires_on_interface_file():
    cfg = TriggerConfig()
    state = TriggerState()
    snapshot = _snapshot()
    deltas = _deltas(diff_samples=[{"interface_files": ["api.proto"]}])
    ev, _ = evaluate(snapshot, deltas, state, cfg, now=1.0)
    assert "public_interface_change" in _kinds(ev)


def test_public_interface_change_does_not_fire_with_no_signal():
    cfg = TriggerConfig()
    state = TriggerState()
    snapshot = _snapshot()
    deltas = _deltas(diff_samples=[{"public_api_changes": [], "interface_files": []}])
    ev, _ = evaluate(snapshot, deltas, state, cfg, now=1.0)
    assert "public_interface_change" not in _kinds(ev)


def test_public_interface_change_does_not_refire_for_an_already_seen_change():
    """12-04 gate finding: `public_api_changes` is a base-vs-worktree
    structural comparison, so a file that diverged from base keeps showing
    up in every future diff sample until it's fixed — without a seen-set
    this would refire every review from now on (unlike every other D5
    detector's "since the last review" semantics). Mirrors
    `test_new_dependency_does_not_refire_for_a_seen_manifest`."""
    cfg = TriggerConfig()
    state = TriggerState()
    snapshot = _snapshot()
    deltas = _deltas(diff_samples=[{"public_api_changes": ["a.py:foo"]}])
    ev1, state = evaluate(snapshot, deltas, state, cfg, now=1.0)
    assert "public_interface_change" in _kinds(ev1)

    # A later sample still shows the SAME already-reported change (the file
    # never reverted) plus nothing new: must not refire.
    ev2, state = evaluate(snapshot, deltas, state, cfg, now=2.0)
    assert "public_interface_change" not in _kinds(ev2)

    # A genuinely NEW api change in the same file set does fire.
    deltas2 = _deltas(diff_samples=[{"public_api_changes": ["a.py:foo", "b.py:bar"]}])
    ev3, _ = evaluate(snapshot, deltas2, state, cfg, now=3.0)
    assert "public_interface_change" in _kinds(ev3)


# -- recovery_budget_low ----------------------------------------------------


def test_recovery_budget_low_fires_near_budget_exhaustion():
    cfg = TriggerConfig(max_recoveries=2)
    state = TriggerState()
    snapshot = _snapshot(recoveries_used=1)
    ev, _ = evaluate(snapshot, _deltas(), state, cfg, now=1.0)
    assert "recovery_budget_low" in _kinds(ev)
    assert ev.urgent is True


def test_recovery_budget_low_does_not_fire_with_budget_remaining():
    cfg = TriggerConfig(max_recoveries=2)
    state = TriggerState()
    snapshot = _snapshot(recoveries_used=0)
    ev, _ = evaluate(snapshot, _deltas(), state, cfg, now=1.0)
    assert "recovery_budget_low" not in _kinds(ev)


def test_recovery_budget_low_fires_once_per_value_then_again_on_a_new_value():
    cfg = TriggerConfig(max_recoveries=2)
    state = TriggerState(last_review_at=0.0, last_verdict="sound")

    ev1, state = evaluate(_snapshot(recoveries_used=1), _deltas(), state, cfg, now=10.0)
    assert "recovery_budget_low" in _kinds(ev1)

    ev2, state = evaluate(_snapshot(recoveries_used=1), _deltas(), state, cfg, now=20.0)
    assert "recovery_budget_low" not in _kinds(ev2)

    ev3, state = evaluate(_snapshot(recoveries_used=2), _deltas(), state, cfg, now=30.0)
    assert "recovery_budget_low" in _kinds(ev3)


# -- spend_time_anomaly ---------------------------------------------------


def test_spend_time_anomaly_fires_on_budget_band_crossing():
    cfg = TriggerConfig(budget_usd=10.0)
    state = TriggerState()
    snapshot = _snapshot(cost_points=[{"cost_usd": 6.0, "attempt_no": 1, "source": "worker", "ts": 1}])
    ev, state = evaluate(snapshot, _deltas(), state, cfg, now=10.0)
    assert "spend_time_anomaly" in _kinds(ev)
    assert "budget:50" in state.fired_bands


def test_spend_time_anomaly_100_percent_budget_is_urgent():
    cfg = TriggerConfig(budget_usd=10.0)
    state = TriggerState()
    snapshot = _snapshot(cost_points=[{"cost_usd": 10.0, "attempt_no": 1, "source": "worker", "ts": 1}])
    ev, _ = evaluate(snapshot, _deltas(), state, cfg, now=10.0)
    assert "spend_time_anomaly" in _kinds(ev)
    assert ev.urgent is True


def test_spend_time_anomaly_fires_on_a_time_mark():
    cfg = TriggerConfig(time_marks_s=(100,))
    state = TriggerState()
    snapshot = _snapshot(started_at=0.0)
    ev, _ = evaluate(snapshot, _deltas(), state, cfg, now=150.0)
    assert "spend_time_anomaly" in _kinds(ev)


def test_spend_time_anomaly_fires_on_a_spend_rate_spike():
    cfg = TriggerConfig()
    state = TriggerState()
    snapshot = _snapshot(
        started_at=0.0,
        cost_points=[
            {"cost_usd": 0.0001, "attempt_no": 1, "source": "supervision", "ts": 5},
            {"cost_usd": 10.0, "attempt_no": 1, "source": "supervision", "ts": 7150},
        ],
    )
    ev, _ = evaluate(snapshot, _deltas(), state, cfg, now=7200.0)
    assert "spend_time_anomaly" in _kinds(ev)


def test_spend_time_anomaly_does_not_fire_on_steady_spend():
    cfg = TriggerConfig()
    state = TriggerState()
    snapshot = _snapshot(
        started_at=0.0,
        cost_points=[{"cost_usd": 1.0, "attempt_no": 1, "source": "supervision", "ts": 100}],
    )
    ev, _ = evaluate(snapshot, _deltas(), state, cfg, now=7200.0)
    assert "spend_time_anomaly" not in _kinds(ev)


def test_budget_band_fires_once():
    cfg = TriggerConfig(budget_usd=10.0)
    state = TriggerState()
    snapshot = _snapshot(cost_points=[{"cost_usd": 6.0, "attempt_no": 1, "source": "worker", "ts": 1}])

    ev1, state = evaluate(snapshot, _deltas(), state, cfg, now=10.0)
    assert "spend_time_anomaly" in _kinds(ev1)

    ev2, state = evaluate(snapshot, _deltas(), state, cfg, now=20.0)
    assert "spend_time_anomaly" not in _kinds(ev2)


# -- cadence -------------------------------------------------------------


def test_cadence_fires_at_60_minutes_when_steady():
    cfg = TriggerConfig()
    state = TriggerState(last_review_at=0.0)
    snapshot = _snapshot(started_at=0.0)
    ev, _ = evaluate(snapshot, _deltas(), state, cfg, now=3600.0)
    assert "cadence" in _kinds(ev)


def test_cadence_does_not_fire_at_45_minutes_when_steady():
    cfg = TriggerConfig()
    state = TriggerState(last_review_at=0.0)
    snapshot = _snapshot(started_at=0.0)
    ev, _ = evaluate(snapshot, _deltas(), state, cfg, now=2700.0)
    assert "cadence" not in _kinds(ev)


# ============================================================ debounce/cadence/settle scenarios


def test_steady_progress_over_three_hours_reviews_only_every_60_minutes():
    cfg = TriggerConfig()
    state = TriggerState()
    now = 0.0
    due_times = []
    while now <= 3 * 3600:
        snapshot = _snapshot(started_at=0.0)
        ev, state = evaluate(snapshot, _deltas(), state, cfg, now)
        if ev.review_due:
            due_times.append(now)
            state = resolve_review(state, now, diff_lines=0, verdict="sound")
        now += 60
    assert due_times == [3600.0, 7200.0, 10800.0]


def test_non_steady_progress_reviews_every_45_minutes():
    cfg = TriggerConfig()
    state = TriggerState(last_verdict="questionable")
    now = 0.0
    due_times = []
    while now <= 2 * 2700:
        snapshot = _snapshot(started_at=0.0)
        ev, state = evaluate(snapshot, _deltas(), state, cfg, now)
        if ev.review_due:
            due_times.append(now)
            state = resolve_review(state, now, diff_lines=0, verdict="questionable")
        now += 60
    assert due_times == [2700.0, 5400.0]


def test_min_gap_suppresses_then_releases_a_pending_trigger():
    cfg = TriggerConfig(min_gap_s=600)
    state = TriggerState(last_review_at=1000.0, last_verdict="sound")
    snapshot = _snapshot(started_at=0.0)

    ev1, state = evaluate(
        snapshot,
        _deltas(failed_approaches=[{"kind": "guide", "ref": "r1", "ts": 1050}, {"kind": "replace", "ref": "r2", "ts": 1060}]),
        state, cfg, now=1100.0,  # only 100s since last review
    )
    assert ev1.review_due is False
    assert ev1.reason == "min_gap_not_elapsed"
    assert any(p["kind"] == "failed_approaches" for p in state.pending)

    ev2, state = evaluate(snapshot, _deltas(), state, cfg, now=1650.0)  # 650s since last review
    assert ev2.review_due is True
    assert "failed_approaches" in _kinds(ev2)


def test_deferred_triggers_carried_into_the_next_due_review():
    cfg = TriggerConfig(min_gap_s=600, settle_s=1800)
    state = start_settle(TriggerState(last_review_at=0.0, last_verdict="sound"), now=0.0, cfg=cfg)
    snapshot = _snapshot(started_at=0.0)

    ev1, state = evaluate(
        snapshot,
        _deltas(failed_approaches=[{"kind": "guide", "ref": "r1", "ts": 100}, {"kind": "replace", "ref": "r2", "ts": 110}]),
        state, cfg, now=200.0,
    )
    assert ev1.review_due is False
    assert ev1.reason == "settle_window"
    assert any(p["kind"] == "failed_approaches" for p in state.pending)

    # the redirect's instruction gets acknowledged at t=300 (the caller,
    # TrajectorySupervisor, records this as `settle_evidence_after`) ...
    state = replace(state, settle_evidence_after=300.0)

    # ... and settle ends (time elapsed AND evidence arrived AFTER that ack);
    # no NEW trigger fires this tick, yet the previously deferred one still
    # surfaces.
    ev2, state = evaluate(
        snapshot,
        _deltas(test_points=[{"passed": 1, "failed": 0, "errors": 0, "source": "worker_observed", "ts": 1900}]),
        state, cfg, now=1900.0,
    )
    assert ev2.review_due is True
    assert "failed_approaches" in _kinds(ev2)


def test_settle_window_pre_ack_evidence_never_satisfies_it_even_past_settle_s():
    cfg = TriggerConfig(min_gap_s=0, settle_s=100)
    state = start_settle(TriggerState(last_review_at=0.0, last_verdict="sound"), now=0.0, cfg=cfg)
    snapshot = _snapshot(started_at=0.0)

    # a test_point arrives, but no ack has ever been observed
    # (`settle_evidence_after` stays `None`) — it must never count, no matter
    # how long after `settle_s` we check.
    ev, state = evaluate(
        snapshot,
        _deltas(test_points=[{"passed": 1, "failed": 0, "errors": 0, "source": "worker_observed", "ts": 50}]),
        state, cfg, now=10_000.0,
    )
    assert state.settle_satisfied is False
    assert ev.reason == "settle_window"


def test_settle_window_clears_once_evidence_arrives_after_the_observed_ack():
    cfg = TriggerConfig(min_gap_s=0, settle_s=100)
    state = start_settle(TriggerState(last_review_at=0.0, last_verdict="sound"), now=0.0, cfg=cfg)
    snapshot = _snapshot(started_at=0.0)
    # the caller observed the redirect's ack at t=60
    state = replace(state, settle_evidence_after=60.0)

    ev, state = evaluate(
        snapshot,
        _deltas(test_points=[{"passed": 1, "failed": 0, "errors": 0, "source": "worker_observed", "ts": 90}]),
        state, cfg, now=150.0,
    )
    assert state.settle_satisfied is True
    assert ev.reason != "settle_window"  # the window itself cleared


def test_settle_window_ignores_evidence_at_or_before_the_ack_timestamp():
    cfg = TriggerConfig(min_gap_s=0, settle_s=100)
    state = start_settle(TriggerState(last_review_at=0.0, last_verdict="sound"), now=0.0, cfg=cfg)
    state = replace(state, settle_evidence_after=90.0)
    snapshot = _snapshot(started_at=0.0)

    ev, state = evaluate(
        snapshot,
        _deltas(test_points=[{"passed": 1, "failed": 0, "errors": 0, "source": "worker_observed", "ts": 90}]),
        state, cfg, now=150.0,
    )
    assert state.settle_satisfied is False  # ts == after, not strictly after
    assert ev.reason == "settle_window"


def test_settle_window_evidence_via_phase_boundary_milestone_after_the_ack():
    cfg = TriggerConfig(min_gap_s=0, settle_s=100)
    state = start_settle(TriggerState(last_review_at=0.0, last_verdict="sound"), now=0.0, cfg=cfg)
    state = replace(state, settle_evidence_after=60.0)
    snapshot = _snapshot(started_at=0.0)

    ev, state = evaluate(
        snapshot,
        _deltas(milestones=[{"kind": "done", "ref": "sha", "ts": 90}]),
        state, cfg, now=150.0,
    )
    assert state.settle_satisfied is True
    assert ev.review_due is True


def test_trigger_state_json_round_trips_settle_evidence_after():
    state = TriggerState(settle_evidence_after=123.5)
    restored = TriggerState.from_dict(state.to_dict())
    assert restored.settle_evidence_after == 123.5

    default = TriggerState.from_dict({})
    assert default.settle_evidence_after is None


def test_start_settle_resets_settle_evidence_after_to_none():
    cfg = TriggerConfig(settle_s=100)
    state = TriggerState(settle_evidence_after=999.0)
    new_state = start_settle(state, now=0.0, cfg=cfg)
    assert new_state.settle_evidence_after is None


def test_urgent_trigger_bypasses_min_gap():
    cfg = TriggerConfig(min_gap_s=600, max_recoveries=2)
    state = TriggerState(last_review_at=1000.0, last_verdict="sound")
    snapshot = _snapshot(started_at=0.0, recoveries_used=1)

    ev, _ = evaluate(snapshot, _deltas(), state, cfg, now=1050.0)  # only 50s since last review
    assert ev.review_due is True
    assert ev.urgent is True
    assert "recovery_budget_low" in _kinds(ev)


def test_urgent_trigger_bypasses_settle_window():
    cfg = TriggerConfig(settle_s=1800, max_recoveries=2)
    state = start_settle(TriggerState(last_review_at=0.0, last_verdict="sound"), now=0.0, cfg=cfg)
    snapshot = _snapshot(started_at=0.0, recoveries_used=1)

    ev, _ = evaluate(snapshot, _deltas(), state, cfg, now=100.0)  # well inside the settle window
    assert ev.review_due is True
    assert ev.urgent is True


def test_no_review_requested_while_one_is_in_flight():
    cfg = TriggerConfig(min_gap_s=0, max_recoveries=2)
    state = begin_review(TriggerState(last_review_at=0.0, last_verdict="sound"), review_id="r1", now=0.0)
    snapshot = _snapshot(started_at=0.0, recoveries_used=1)  # an urgent trigger; still must not bypass in-flight

    ev, state = evaluate(snapshot, _deltas(), state, cfg, now=10.0)
    assert ev.review_due is False
    assert ev.reason == "review_in_flight"
    assert state.in_flight_review_id == "r1"


def test_in_flight_review_times_out_and_clears():
    cfg = TriggerConfig(review_timeout_s=600, min_gap_s=0)
    state = begin_review(TriggerState(last_review_at=0.0, last_verdict="sound"), review_id="r1", now=0.0)
    snapshot = _snapshot(started_at=0.0)

    _, state = evaluate(snapshot, _deltas(), state, cfg, now=700.0)  # past the timeout
    assert state.in_flight_review_id is None


# ============================================================ small shapes


def test_trigger_and_evaluation_are_frozen_dataclasses():
    trigger = Trigger(kind="cadence", detail={"a": 1}, urgent=False)
    assert trigger.kind == "cadence"


def test_trigger_state_round_trips_through_dict():
    state = TriggerState(last_review_at=5.0, fired_bands=("budget:50",), pending=({"kind": "x", "detail": {}, "urgent": False},))
    restored = TriggerState.from_dict(state.to_dict())
    assert restored == state
