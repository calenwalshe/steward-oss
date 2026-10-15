"""Phase 12 TEST GATE (TRJ-13): a deterministic replayed 3-hour drifting
build, a steady control run, and kill-switch/restart variants. Uses
`tests/trajectory_replay.py`'s `run_replay` to drive the REAL `Runner.tick` +
`TrajectorySupervisor` + `AdvisorPane` + `hysteresis` + `Steering` against a
real `Store` — see that module's docstring for exactly what is faked.

Every scenario below is deterministic (a `FakeClock`, no sleep, no network)
and each run completes in well under 20s.

TIMING NOTE (recorded once, applies to every scenario): a review's `ts` is
recorded at REQUEST time, but the min-gap debounce measures elapsed time from
the PREVIOUS review's RESOLUTION time (one tick after its own request, since
the advisor's answer is only drained at the start of the NEXT tick — mirroring
the real fire-and-forget request + one-tick-later poll). That is an extra 0.5
tick per debounce-chained review, which compounds over a long chain of
back-to-back debounced requests. Scenario A's own gap-chained reviews (R2, R3,
R8, R9) therefore land 0.5-1 tick later than the idealized round numbers in
12-04-PLAN's table; every other review (driven by a fact becoming true, not
by the gap alone: R1's cadence, R4's diff jump, R6's acceptance change) lands
within one tick of its idealized minute. Shifted per the plan's own explicit
instruction ("shift the TIMELINE, never the assertion semantics"); recorded
here and in 12-04-SUMMARY.md, not re-litigated at every assertion site below.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from steward import hysteresis, trajectory as trajectory_module
from steward import triggers as triggers_module
from steward.ledger import TrajectoryLedger
from steward.steering import Steering

from trajectory_replay import Timeline, run_replay

_API_BASE = "def public_widget(a, b):\n    return a + b\n\n\ndef helper():\n    return 1\n"
_API_AFTER_REMOVAL = "def helper():\n    return 1\n"


def _verdict(
    trajectory_="sound",
    confidence=0.5,
    urgency="normal",
    concern="none",
    action="continue",
    instruction="keep going",
    current_approach="",
    recommended_approach="",
    observation="on track",
    cost_usd=0.0,
):
    return {
        "trajectory": trajectory_,
        "confidence": confidence,
        "urgency": urgency,
        "concern": concern,
        "observation": observation,
        "recommendation": {
            "action": action,
            "instruction": instruction,
            "current_approach": current_approach,
            "recommended_approach": recommended_approach,
        },
        "cost_usd": cost_usd,
    }


def _gate_cfg(**trig_overrides) -> trajectory_module.TrajectoryConfig:
    trig_kwargs = {"time_marks_s": (10800,)}
    trig_kwargs.update(trig_overrides)
    return trajectory_module.TrajectoryConfig(
        mode="active",
        triggers=triggers_module.TriggerConfig(**trig_kwargs),
        hysteresis=hysteresis.HysteresisConfig(),
    )


# =============================================================== Scenario B


def _steady_timeline() -> Timeline:
    tl = Timeline()
    lines = 0
    for minute in range(0, 181, 15):
        lines += 20
        tl.set_diff_lines(minute + 0.5, lines)
    passed = 0
    for minute in range(0, 181, 15):
        passed += 5
        tl.test_result(minute + 0.6, passed)
    return tl


def test_scenario_b_steady_reviews_only_at_cadence_all_sound():
    with tempfile.TemporaryDirectory() as tmp:
        sound_steps = [_verdict() for _ in range(6)]
        result = run_replay(
            _steady_timeline(),
            duration_s=10800 + 30,  # one trailing tick so the last review's advice resolves
            tick_s=30,
            cfg=_gate_cfg(),
            reviewer_steps=sound_steps,
            steward_home=Path(tmp) / "home",
        )

        reviews = result.reviews()
        minutes = [r["minute"] for r in reviews]
        assert len(reviews) == 3
        for minute in minutes:
            assert abs(minute - round(minute / 60) * 60) <= 1  # within 1 tick of 60/120/180
        assert sorted(round(m / 60) for m in minutes) == [1, 2, 3]
        for r in reviews:
            assert "cadence" in r["triggers"]
            assert r["verdict"]["trajectory"] == "sound"
            assert r["decision"] == "trajectory_hold"

        assert Steering(result.store).all_instructions(result.job_id) == []
        assert result.worker.delivered == []


# =============================================================== Scenario A


def _scenario_a_reviewer_steps() -> list[dict]:
    return [
        _verdict("sound", 0.9, "normal", "none", "continue"),  # R1 (60)
        _verdict("questionable", 0.6, "normal", "dependency_risk", "continue"),  # R2 (~70)
        _verdict(
            "questionable", 0.75, "normal", "wrong_approach", "change_approach",
            current_approach="approach-a", recommended_approach="approach-b",
        ),  # R3 (~80)
        _verdict(
            "off_track", 0.85, "normal", "wrong_approach", "change_approach",
            current_approach="approach-a", recommended_approach="approach-b",
        ),  # R4 (95): same key, higher confidence, worse evidence -> redirect
        _verdict(
            "off_track", 0.9, "normal", "wrong_approach", "change_approach",
            current_approach="approach-b", recommended_approach="approach-a",
        ),  # R5 (~125): recommends the discarded approach -> oscillation_guard
        _verdict(
            "off_track", 0.95, "high", "wrong_approach", "change_approach",
            current_approach="approach-b", recommended_approach="approach-c",
        ),  # R6 (~139:30): obvious_problem, but stale after the human's focus steer
        _verdict("questionable", 0.5, "normal", "dependency_risk", "continue", cost_usd=2.0),  # R7 (~160): cost jump
        _verdict("sound", 0.9, "normal", "none", "continue"),  # R8 (~170)
        _verdict("sound", 0.9, "normal", "none", "continue"),  # R9 (~180): time mark
    ]


def _scenario_a_timeline() -> Timeline:
    tl = Timeline()
    tl.suppress_auto_ack(1)  # R4's redirect (rev=1): ack timing is scripted by hand below

    # 0-60: steady climb (diff, worker-observed tests, a stable 5-item plan).
    tl.plan(1, ["item-a", "item-b", "item-c", "item-d", "item-e"])
    tl.set_diff_lines(1, 50)
    tl.set_diff_lines(15, 150)
    tl.set_diff_lines(30, 250)
    tl.set_diff_lines(45, 350)
    tl.set_diff_lines(60, 450)
    tl.test_result(5, 10)
    tl.test_result(20, 18)
    tl.test_result(35, 26)
    tl.test_result(50, 34)
    tl.test_result(60, 40)

    tl.add_manifest(65, "pyproject.toml")  # new_dependency

    tl.error_frame(78)
    tl.error_frame(79)  # failed_approaches (>= 2)
    tl.plan(80, ["item-f", "item-g", "item-h", "item-e", "item-i"])  # redesign_proposed (Jaccard vs P1)

    tl.set_diff_lines(95, 1300)
    tl.test_result(95, 35)  # diff_growth + a worker test regression (40 -> 35)

    tl.public_api_removed(100, "api_module.py", _API_BASE, _API_AFTER_REMOVAL)  # public_interface_change

    tl.done_frame(110, sha="abc123")  # phase_boundary; deferred by closeout (rev=1 still unacked)
    tl.ack(111, rev=1)  # R4's redirect finally acknowledged
    tl.test_result(112, 36)  # settle evidence: a test_result strictly AFTER the ack

    tl.human_steer(139.5, "goal", "target the new acceptance criteria instead")  # acceptance_changed
    tl.human_steer(139.75, "focus", "focus narrowly on the api layer")  # bumps the revision R6 was requested against

    tl.set_lifecycle(150, "idle")
    tl.freeze_activity(150, True)  # a real suspect_stall (no diff change, idle, for >= 600s)
    tl.set_lifecycle(162, "working")
    tl.freeze_activity(162, False)

    return tl


def _run_scenario_a(steward_home: Path, *, duration_s: float = 183 * 60, extra: "callable | None" = None, cfg=None):
    tl = _scenario_a_timeline()
    if extra is not None:
        extra(tl)
    return run_replay(
        tl,
        duration_s=duration_s,
        tick_s=30,
        cfg=cfg or _gate_cfg(),
        reviewer_steps=_scenario_a_reviewer_steps(),
        steward_home=steward_home,
    )


def test_scenario_a_drifting_build_fires_reviews_hysteresis_and_delivery():
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_scenario_a(Path(tmp) / "home")
        reviews = result.reviews()

        # -- fires on triggers: R1..R9, exact trigger sets -----------------
        assert len(reviews) == 9
        expected_triggers = [
            {"cadence"},
            {"new_dependency"},
            {"failed_approaches", "redesign_proposed"},
            {"diff_growth"},
            {"public_interface_change", "phase_boundary"},
            {"acceptance_changed"},
            {"failed_approaches", "recovery_budget_low"},
            {"spend_time_anomaly"},
            {"spend_time_anomaly"},
        ]
        for row, expected in zip(reviews, expected_triggers):
            assert set(row["triggers"]) == expected, row

        # -- stays asleep on steady progress -------------------------------
        r1 = reviews[0]
        assert abs(r1["minute"] - 60) <= 1
        assert set(r1["triggers"]) == {"cadence"}

        # -- expected verdicts / decisions, minute within ~1 tick of the plan's
        #    idealized number (module docstring: gap-chained reviews drift up
        #    to 1 extra tick; fact-driven ones land within 1 tick outright).
        expected = [
            (60, "sound", "trajectory_hold"),
            (70, "questionable", "trajectory_concern_recorded"),
            (80, "questionable", "trajectory_concern_recorded"),
            (95, "off_track", "trajectory_redirect_authorized"),
            (125, "off_track", "trajectory_escalated"),
            (139.5, "off_track", "trajectory_advice_discarded"),
            (160, "questionable", "trajectory_concern_recorded"),
            (170, "sound", "trajectory_hold"),
            (180, "sound", "trajectory_hold"),
        ]
        for row, (minute, verdict, decision) in zip(reviews, expected):
            assert abs(row["minute"] - minute) <= 1.0, row  # debounce-chain drift documented above (max 1 min observed)
            assert row["verdict"]["trajectory"] == verdict, row
            assert row["decision"] == decision, row

        # -- hysteresis prevents A -> B -> A ---------------------------------
        authorized = result.events("trajectory_redirect_authorized")
        assert len(authorized) == 1
        escalated = result.events("trajectory_escalated")
        assert len(escalated) == 1
        assert escalated[0].rule == "oscillation_guard"
        needs_you = [e for e in result.notifier.sent if e.kind == "NEEDS_YOU"]
        assert len(needs_you) == 1  # exactly the R5 oscillation-guard escalation

        hstate = hysteresis.HysteresisState.from_dict(result.store.kv_get(result.job_id, "trajectory_hysteresis_state"))
        assert hstate.current_approach == "approach-b"
        assert hstate.discarded == ("approach-a",)  # approach-a never comes back

        # -- exactly one steering revision per accepted redirect -------------
        trajectory_instructions = result.instructions_by_origin("trajectory")
        assert len(trajectory_instructions) == 1
        r4_id = reviews[3]["review_id"]
        assert trajectory_instructions[0]["human_instruction"] == ""
        receipt = result.store.get_receipt(f"trajectory-{r4_id}")
        assert receipt is not None and receipt.accepted

        # replaying R4's advice through supervisor.step again adds 0 revisions
        # (duplicate receipt) -- simulate by re-running the supervisor step
        # with the same advice still sitting in kv (nothing new arrives, but
        # the request_id-level dedupe is exercised directly here).
        dup = Steering(result.store).record(
            result.job_id, "nudge", "do it", request_id=f"trajectory-{r4_id}", origin="trajectory"
        )
        assert dup.duplicate is True
        assert len(result.instructions_by_origin("trajectory")) == 1

        # -- no worker contact except via the controller ---------------------
        assert result.worker.delivered == [
            f"[rev=1] [trajectory {r4_id[:8]}] keep going",
            "[rev=2] target the new acceptance criteria instead",
            "[rev=3] focus narrowly on the api layer",
            "[rev=4] try a different approach",
        ]
        assert result.herdr.violations == []
        assert len(result.herdr.sent) == 9

        # -- ledger: no output_tail sentinel ---------------------------------
        ledger = TrajectoryLedger(result.store, result.job_id)
        import json

        blob = json.dumps(ledger.snapshot())
        assert "output_tail" not in blob

        # -- cost: records the Astra cost from the fake results --------------
        assert ledger.supervision_spend_usd() >= 2.0


def test_scenario_a_the_supervisor_holds_no_worker_reference():
    """Structural guarantee (D1/D2): the `TrajectorySupervisor` class BODY
    (not the module's own docstring prose, which discusses the worker by
    name) never mentions `worker` anywhere -- no attribute, no call. Mirrors
    12-03-SUMMARY's own `grep -n "worker" steward/trajectory.py` check
    (docstring-only hits), scoped precisely here so the gate enforces it
    mechanically rather than by eyeballing grep output."""
    source = Path("steward/trajectory.py").read_text()
    start = source.index("class TrajectorySupervisor")
    end_match = re.search(r"\ndef _in_settle", source[start:])
    body = source[start : start + end_match.start()] if end_match else source[start:]
    assert "worker" not in body.lower()


# =============================================================== Scenario C (kill switch)


def test_scenario_c_kill_switch_suppresses_detection_and_discards_in_flight_advice():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _gate_cfg(cadence_s=89 * 60, steady_cadence_s=89 * 60, min_gap_s=0)

        tl = Timeline()
        tl.set_diff_lines(1, 50)
        tl.test_result(1, 5)
        # the diff-growth fact Scenario A's R4 relies on: still present, but
        # the kill switch (89:30-130) must suppress its detection entirely.
        tl.set_diff_lines(95, 1300)
        tl.test_result(95, 5)
        tl.trajectory_mode(89.5, "off")  # the cadence review requested at ~89 is still in flight here
        tl.trajectory_mode(130, "active")
        tl.add_manifest(140, "pyproject.toml")  # reviews resume: a fresh trigger after 130

        sound = _verdict("sound", 0.9, "normal", "none", "continue")
        result = run_replay(
            tl, duration_s=150 * 60, tick_s=30, cfg=cfg,
            reviewer_steps=[sound, sound, sound, sound],
            steward_home=Path(tmp) / "home",
        )

        reviews = result.reviews()
        # R4-equivalent (diff_growth) is never requested while off -- the
        # fact sits there undetected until mode resumes exactly at 130, when
        # the very next review picks it up (that IS the "reviews resume"
        # behaviour, not a violation of "never requested while off").
        assert all("diff_growth" not in r["triggers"] for r in reviews if r["minute"] < 130)
        resumed_review = next(r for r in reviews if r["minute"] >= 130)
        assert "diff_growth" in resumed_review["triggers"]

        discarded_mode_off = [
            e for e in result.events("trajectory_advice_discarded") if e.evidence.get("reason") == "mode_off"
        ]
        assert len(discarded_mode_off) == 1  # the review in flight at the kill-switch moment

        assert result.instructions_by_origin("trajectory") == []  # 0 trajectory revisions

        resumed = [r for r in reviews if r["minute"] > 130]
        assert len(resumed) >= 1  # reviews resume after 130

        ledger = TrajectoryLedger(result.store, result.job_id)
        snapshot = ledger.snapshot()
        # the ledger keeps recording throughout, including during the off window
        sample_ts = [d["ts"] for d in snapshot["lists"]["diff_samples"]]
        assert any(ts / 60 > 95 for ts in sample_ts) and any(ts / 60 < 89 for ts in sample_ts)


# =============================================================== Scenario D (restart)


def test_scenario_d_restart_mid_settle_gives_identical_r5_through_r9():
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_scenario_a(
            Path(tmp) / "home",
            extra=lambda tl: tl.restart_supervisor(96),
        )
        reviews = result.reviews()
        assert len(reviews) == 9
        expected_triggers = [
            {"public_interface_change", "phase_boundary"},
            {"acceptance_changed"},
            {"failed_approaches", "recovery_budget_low"},
            {"spend_time_anomaly"},
            {"spend_time_anomaly"},
        ]
        for row, expected in zip(reviews[4:], expected_triggers):
            assert set(row["triggers"]) == expected, row

        authorized = result.events("trajectory_redirect_authorized")
        assert len(authorized) == 1  # still exactly 1 trajectory revision, restart included

        hstate = hysteresis.HysteresisState.from_dict(result.store.kv_get(result.job_id, "trajectory_hysteresis_state"))
        assert hstate.current_approach == "approach-b"
        assert hstate.discarded == ("approach-a",)
