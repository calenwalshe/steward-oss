"""Tests for `steward.trajectory.TrajectorySupervisor` (TRJ-09/11, 12-CONTEXT
D2/D8/D9). A `FakeAdvisorLink` wraps a real `AdvisorPane` (over a real
`Store`) with a `FakeTrajectoryReviewer`, so `.request()` answers
synchronously — the supervisor still only *sees* the advice on its next
`step()` call (step 2 always runs before step 3 within one tick), matching
the one-round-trip-per-tick shape the runner drives in production.
"""

from __future__ import annotations

from dataclasses import replace

from steward import hysteresis, trajectory
from steward import triggers as triggers_module
from steward.advisor_pane import AdvisorPane
from steward.models import Judgment
from steward.review import FakeTrajectoryReviewer
from steward.steering import Steering
from steward.store import Store


class FakeClock:
    def __init__(self, start: float = 1_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeNotifier:
    def __init__(self):
        self.sent: list = []

    def send(self, event) -> bool:
        self.sent.append(event)
        return True


class FakeAdvisorLink:
    """`.request()` answers synchronously through a real `AdvisorPane` unless
    `answer=False` (simulates "review in flight, no advice yet" for the
    timeout test). `.find()` returns `None` when `present=False`."""

    def __init__(self, store: Store, reviewer, *, present: bool = True, answer: bool = True):
        self.pane = AdvisorPane(store, reviewer, print_fn=lambda _l: None)
        self.present = present
        self.answer = answer
        self.requests: list[tuple[str, str, str]] = []

    def find(self) -> str | None:
        return "advisor-pane" if self.present else None

    def request(self, pane_id: str, job_id: str, review_id: str) -> None:
        self.requests.append((pane_id, job_id, review_id))
        if not self.answer:
            return
        self.pane.execute(
            {
                "request_id": review_id,
                "op": "trajectory_review",
                "args": {"job_id": job_id, "review_id": review_id},
                "origin": "trajectory",
            }
        )


def _cfg(**overrides) -> trajectory.TrajectoryConfig:
    trig = triggers_module.TriggerConfig(cadence_s=0, steady_cadence_s=0, min_gap_s=0, settle_s=1800, review_timeout_s=300)
    base = trajectory.TrajectoryConfig(
        mode="active", triggers=trig, hysteresis=hysteresis.HysteresisConfig(),
        advice_ttl_s=900, max_reviews_per_job=12, supervision_budget_usd=5.0,
    )
    return replace(base, **overrides)


def _verdict(
    trajectory_="off_track",
    confidence=0.95,
    urgency="high",
    concern="wrong_approach",
    action="change_approach",
    recommended_approach="plan-b",
    current_approach="plan-a",
    instruction="switch to plan b",
    observation="drifting",
    cost_usd=0.0,
) -> dict:
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


def _setup(store: Store, *, mode="active", reviewer_steps=None, present=True, answer=True, cfg=None):
    job = store.create_job("goal", "/repo")
    clock = FakeClock()
    # In production `Runner.start()` seeds the ledger's intent before the
    # supervisor ever ticks (12-03 task 3); tests seed it directly since
    # `TrajectorySupervisor` is exercised standalone here.
    trajectory.TrajectoryLedger(store, job.job_id).set_intent(job.goal, "", clock.t)
    reviewer = FakeTrajectoryReviewer(reviewer_steps or [])
    link = FakeAdvisorLink(store, reviewer, present=present, answer=answer)
    notifier = FakeNotifier()
    supervisor = trajectory.TrajectorySupervisor(store, link, notifier, clock, cfg or _cfg(mode=mode))
    return job, supervisor, link, notifier, clock


def _events(store: Store, job_id: str, what: str) -> list:
    return [e for e in store.events(job_id) if e.what == what]


# ------------------------------------------------------------- effective_mode


def test_effective_mode_config_default(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    eff = trajectory.effective_mode(store, job.job_id, trajectory.TrajectoryConfig(mode="shadow"))
    assert eff.mode == "shadow"
    assert eff.source == "config"


def test_effective_mode_job_override_beats_config(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    trajectory.set_job_mode(store, job.job_id, "active")
    eff = trajectory.effective_mode(store, job.job_id, trajectory.TrajectoryConfig(mode="shadow"))
    assert eff.mode == "active"
    assert eff.source == "job"


def test_effective_mode_kill_switch_beats_everything(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    trajectory.set_job_mode(store, job.job_id, "active")
    trajectory.set_global_mode(store, "off")
    eff = trajectory.effective_mode(store, job.job_id, trajectory.TrajectoryConfig(mode="active"))
    assert eff.mode == "off"
    assert eff.source == "kill_switch"


# ------------------------------------------------------------- decision branches


def test_active_mode_redirect_records_exactly_one_steering_revision(steward_home):
    store = Store(steward_home)
    job, supervisor, link, notifier, clock = _setup(store, mode="active", reviewer_steps=[_verdict()])

    supervisor.step(job.job_id)  # requests + (synchronously) answers
    report = supervisor.step(job.job_id)  # processes the advice

    steering = Steering(store)
    instructions = steering.all_instructions(job.job_id)
    assert len(instructions) == 1
    instr = instructions[0]
    assert instr["origin"] == "trajectory"
    assert instr["verb"] == "nudge"  # change_approach -> nudge

    events = _events(store, job.job_id, "nudge instruction recorded")
    assert events[0].judgment == Judgment.model_informed_code_authorized

    authorized = _events(store, job.job_id, "trajectory_redirect_authorized")
    assert len(authorized) == 1
    assert authorized[0].judgment == Judgment.model_informed_code_authorized
    review_id = list(link.requests)[0][2]
    assert instr.get("job_revision")  # request_id itself lives in the receipt, not the row
    receipts_req = f"trajectory-{review_id}"
    receipt = store.get_receipt(receipts_req)
    assert receipt is not None
    assert receipt.accepted is True


def test_refocus_and_narrow_scope_map_to_focus_verb(steward_home):
    store = Store(steward_home)
    job, supervisor, *_ = _setup(
        store, mode="active", reviewer_steps=[_verdict(action="refocus", recommended_approach="")]
    )
    supervisor.step(job.job_id)
    supervisor.step(job.job_id)
    instr = Steering(store).all_instructions(job.job_id)[0]
    assert instr["verb"] == "focus"


def test_shadow_mode_records_would_redirect_and_never_touches_steering_or_hysteresis(steward_home):
    store = Store(steward_home)
    job, supervisor, *_ = _setup(store, mode="shadow", reviewer_steps=[_verdict()])

    supervisor.step(job.job_id)
    supervisor.step(job.job_id)

    assert Steering(store).all_instructions(job.job_id) == []
    would = _events(store, job.job_id, "trajectory_would_redirect")
    assert len(would) == 1

    hstate_data = store.kv_get(job.job_id, "trajectory_hysteresis_state")
    hstate = hysteresis.HysteresisState.from_dict(hstate_data) if hstate_data else hysteresis.HysteresisState()
    assert hstate.current_approach is None
    assert hstate.discarded == ()


def test_escalate_human_notifies_and_records_event(steward_home):
    store = Store(steward_home)
    verdict = _verdict(
        trajectory_="questionable", confidence=0.5, urgency="normal", concern="thrash", action="escalate_human"
    )
    job, supervisor, link, notifier, clock = _setup(store, mode="active", reviewer_steps=[verdict])

    supervisor.step(job.job_id)
    supervisor.step(job.job_id)

    escalated = _events(store, job.job_id, "trajectory_escalated")
    assert len(escalated) == 1
    assert escalated[0].judgment == Judgment.model
    assert len(notifier.sent) == 1
    assert notifier.sent[0].kind == "NEEDS_YOU"


def test_record_concern_on_first_sighting(steward_home):
    store = Store(steward_home)
    verdict = _verdict(
        trajectory_="questionable", confidence=0.5, urgency="normal", concern="scope_creep", action="continue"
    )
    job, supervisor, *_ = _setup(store, mode="active", reviewer_steps=[verdict])

    supervisor.step(job.job_id)
    supervisor.step(job.job_id)

    recorded = _events(store, job.job_id, "trajectory_concern_recorded")
    assert len(recorded) == 1
    hstate = hysteresis.HysteresisState.from_dict(store.kv_get(job.job_id, "trajectory_hysteresis_state"))
    assert "scope_creep" in hstate.open_concerns
    assert Steering(store).all_instructions(job.job_id) == []


def test_hold_when_sound(steward_home):
    store = Store(steward_home)
    verdict = _verdict(trajectory_="sound", concern="none", action="continue")
    job, supervisor, *_ = _setup(store, mode="active", reviewer_steps=[verdict])

    supervisor.step(job.job_id)
    supervisor.step(job.job_id)

    held = _events(store, job.job_id, "trajectory_hold")
    assert len(held) == 1
    assert held[0].judgment == Judgment.model


# ------------------------------------------------------------- modes / kill switch


def test_off_mode_never_requests_a_review(steward_home):
    store = Store(steward_home)
    job, supervisor, *_ = _setup(store, mode="off", reviewer_steps=[_verdict()])

    for _ in range(3):
        supervisor.step(job.job_id)

    assert _events(store, job.job_id, "trajectory_review_requested") == []


def test_kill_switch_discards_in_flight_advice(steward_home):
    store = Store(steward_home)
    job, supervisor, link, notifier, clock = _setup(store, mode="active", reviewer_steps=[_verdict()])

    supervisor.step(job.job_id)  # requests + synchronously answers

    trajectory.set_global_mode(store, "off")
    supervisor.step(job.job_id)  # kill switch: discard, no decision

    assert Steering(store).all_instructions(job.job_id) == []
    discarded = _events(store, job.job_id, "trajectory_advice_discarded")
    assert any(e.evidence.get("reason") == "mode_off" for e in discarded)


# ------------------------------------------------------------- human wins (D8)


def test_conflict_discards_advice_after_a_human_steer(steward_home):
    store = Store(steward_home)
    job, supervisor, link, notifier, clock = _setup(store, mode="active", reviewer_steps=[_verdict()])

    supervisor.step(job.job_id)  # requests + synchronously answers

    # a human steers after the review was requested, bumping the job revision;
    # already fully acknowledged, so it does NOT itself count as "unacked"
    # (that's a separate rule, covered by its own test below).
    steering = Steering(store)
    steering.record(job.job_id, "focus", "human wants this instead", "human-req-1")
    steering.mark_acknowledged(job.job_id, 1, "acknowledged")

    supervisor.step(job.job_id)  # processes the (now-stale) advice

    instructions = Steering(store).all_instructions(job.job_id)
    assert len(instructions) == 1
    assert instructions[0]["origin"] == "cli"  # only the human's

    discarded = _events(store, job.job_id, "trajectory_advice_discarded")
    assert any(e.evidence.get("reason") == "stale_after_human_steer" for e in discarded)

    hstate_data = store.kv_get(job.job_id, "trajectory_hysteresis_state")
    hstate = hysteresis.HysteresisState.from_dict(hstate_data) if hstate_data else hysteresis.HysteresisState()
    assert hstate.current_approach is None
    assert hstate.discarded == ()


def test_deferred_advice_charges_its_cost_once(steward_home):
    store = Store(steward_home)
    trig = triggers_module.TriggerConfig(cadence_s=0, steady_cadence_s=0, min_gap_s=100_000, review_timeout_s=300)
    cfg = _cfg(mode="active", advice_ttl_s=100, triggers=trig)
    job, supervisor, link, notifier, clock = _setup(
        store, mode="active", reviewer_steps=[_verdict(cost_usd=0.5)], cfg=cfg
    )
    Steering(store).record(job.job_id, "focus", "already pending", "pending-req")

    for _ in range(5):  # advice waits on the unacked instruction, re-read every tick
        supervisor.step(job.job_id)
        clock.advance(15)

    assert trajectory.TrajectoryLedger(store, job.job_id).supervision_spend_usd() == 0.5


def test_unacknowledged_instruction_defers_then_ttl_discards(steward_home):
    store = Store(steward_home)
    trig = triggers_module.TriggerConfig(cadence_s=0, steady_cadence_s=0, min_gap_s=100_000, review_timeout_s=300)
    cfg = _cfg(mode="active", advice_ttl_s=100, triggers=trig)
    job, supervisor, link, notifier, clock = _setup(store, mode="active", reviewer_steps=[_verdict()], cfg=cfg)

    # an existing, never-delivered instruction blocks stacking a new one
    Steering(store).record(job.job_id, "focus", "already pending", "pending-req")

    supervisor.step(job.job_id)  # requests + synchronously answers
    supervisor.step(job.job_id)  # advice arrived, but deferred (unacked pending)

    assert len(Steering(store).all_instructions(job.job_id)) == 1  # no new revision stacked
    assert _events(store, job.job_id, "trajectory_redirect_authorized") == []
    trig = triggers_module.TriggerState.from_dict(store.kv_get(job.job_id, "trajectory_trigger_state"))
    assert trig.in_flight_review_id is not None  # still in flight

    clock.advance(200)  # past advice_ttl_s
    supervisor.step(job.job_id)

    discarded = _events(store, job.job_id, "trajectory_advice_discarded")
    assert any(e.evidence.get("reason") == "stale_pending_ack" for e in discarded)
    trig = triggers_module.TriggerState.from_dict(store.kv_get(job.job_id, "trajectory_trigger_state"))
    assert trig.in_flight_review_id is None


# ------------------------------------------------------------- duplicate advice


def test_duplicate_advice_after_simulated_restart_adds_no_second_revision(steward_home):
    store = Store(steward_home)
    job, supervisor, link, notifier, clock = _setup(store, mode="active", reviewer_steps=[_verdict()])

    supervisor.step(job.job_id)  # requests + synchronously answers
    review_id = link.requests[0][2]
    supervisor.step(job.job_id)  # processes -> one revision, resolves the review

    assert len(Steering(store).all_instructions(job.job_id)) == 1

    # Simulate a crash between applying the redirect and persisting the
    # resolved trigger state: force the review back "in flight". The advice
    # (and the packet/meta) are still sitting in kv, untouched.
    trig = triggers_module.TriggerState.from_dict(store.kv_get(job.job_id, "trajectory_trigger_state"))
    stale = replace(trig, in_flight_review_id=review_id, in_flight_started_at=clock.t)
    store.kv_set(job.job_id, "trajectory_trigger_state", stale.to_dict())

    # A brand-new supervisor instance (a restarted controller).
    notifier2 = FakeNotifier()
    supervisor2 = trajectory.TrajectorySupervisor(store, link, notifier2, clock, _cfg(mode="active"))
    supervisor2.step(job.job_id)

    instructions = Steering(store).all_instructions(job.job_id)
    assert len(instructions) == 1  # still just one revision (request_id dedupe)
    hstate = hysteresis.HysteresisState.from_dict(store.kv_get(job.job_id, "trajectory_hysteresis_state"))
    assert hstate.current_approach == "plan-b"
    # discarded once (the verdict's own current_approach="plan-a", 12-04
    # gate finding: `_apply_redirect` falls back to it on the very first
    # redirect, when `state.current_approach` has never been set) — and only
    # once, not mutated a second time by the replayed duplicate advice.
    assert hstate.discarded == ("plan-a",)


# ------------------------------------------------------------- budget caps


def test_max_reviews_per_job_stops_further_requests(steward_home):
    store = Store(steward_home)
    sound = _verdict(trajectory_="sound", concern="none", action="continue")
    cfg = _cfg(mode="active", max_reviews_per_job=1)
    job, supervisor, link, notifier, clock = _setup(store, mode="active", reviewer_steps=[sound, sound], cfg=cfg)

    supervisor.step(job.job_id)  # request #1 + synchronous answer
    supervisor.step(job.job_id)  # processes -> hold, resolves

    supervisor.step(job.job_id)  # would request #2, but budget is spent

    requested = _events(store, job.job_id, "trajectory_review_requested")
    assert len(requested) == 1
    exhausted = _events(store, job.job_id, "trajectory_budget_exhausted")
    assert len(exhausted) == 1  # once, not every tick

    supervisor.step(job.job_id)
    assert len(_events(store, job.job_id, "trajectory_budget_exhausted")) == 1


def test_supervision_budget_usd_stops_further_requests(steward_home):
    store = Store(steward_home)
    sound = _verdict(trajectory_="sound", concern="none", action="continue", cost_usd=1.0)
    cfg = _cfg(mode="active", supervision_budget_usd=0.5)
    job, supervisor, link, notifier, clock = _setup(store, mode="active", reviewer_steps=[sound, sound], cfg=cfg)

    supervisor.step(job.job_id)
    supervisor.step(job.job_id)  # advice cost 1.0 folded into the ledger

    supervisor.step(job.job_id)  # over the 0.5 supervision budget now

    assert len(_events(store, job.job_id, "trajectory_review_requested")) == 1
    assert len(_events(store, job.job_id, "trajectory_budget_exhausted")) == 1


# ------------------------------------------------------------- advisor availability / timeout


def test_no_advisor_pane_skips_the_review(steward_home):
    store = Store(steward_home)
    job, supervisor, link, notifier, clock = _setup(store, mode="active", reviewer_steps=[_verdict()], present=False)

    supervisor.step(job.job_id)

    skipped = _events(store, job.job_id, "trajectory_review_skipped")
    assert len(skipped) == 1
    assert skipped[0].evidence["reason"] == "no_advisor_pane"


def test_review_times_out_without_advice(steward_home):
    store = Store(steward_home)
    trig = triggers_module.TriggerConfig(cadence_s=0, steady_cadence_s=0, min_gap_s=100_000, review_timeout_s=100)
    cfg = _cfg(mode="active", triggers=trig)
    job, supervisor, link, notifier, clock = _setup(store, mode="active", reviewer_steps=[_verdict()], answer=False, cfg=cfg)

    supervisor.step(job.job_id)  # requests; no synchronous answer
    trig = triggers_module.TriggerState.from_dict(store.kv_get(job.job_id, "trajectory_trigger_state"))
    assert trig.in_flight_review_id is not None

    clock.advance(150)
    supervisor.step(job.job_id)

    timed_out = _events(store, job.job_id, "trajectory_review_timed_out")
    assert len(timed_out) == 1
    trig = triggers_module.TriggerState.from_dict(store.kv_get(job.job_id, "trajectory_trigger_state"))
    assert trig.in_flight_review_id is None


# ------------------------------------------------------------- restart durability


def test_restart_continues_the_in_flight_review(steward_home):
    store = Store(steward_home)
    job, supervisor, link, notifier, clock = _setup(store, mode="active", reviewer_steps=[_verdict()])

    supervisor.step(job.job_id)  # requests + synchronously answers (advice sits in kv)

    # A fresh supervisor instance (post-restart) on the SAME store picks up
    # the persisted trigger state and processes the already-arrived advice.
    notifier2 = FakeNotifier()
    supervisor2 = trajectory.TrajectorySupervisor(store, link, notifier2, clock, _cfg(mode="active"))
    supervisor2.step(job.job_id)

    authorized = _events(store, job.job_id, "trajectory_redirect_authorized")
    assert len(authorized) == 1
    assert len(Steering(store).all_instructions(job.job_id)) == 1


# ------------------------------------------------------------- settle window (post-ack evidence)


def test_settle_evidence_after_stays_none_until_the_redirect_is_acknowledged(steward_home):
    store = Store(steward_home)
    job, supervisor, link, notifier, clock = _setup(store, mode="active", reviewer_steps=[_verdict()])

    supervisor.step(job.job_id)  # requests + synchronously answers
    supervisor.step(job.job_id)  # applies the redirect; starts the settle window

    trig = triggers_module.TriggerState.from_dict(store.kv_get(job.job_id, "trajectory_trigger_state"))
    assert trig.settle_until > 0
    assert trig.settle_evidence_after is None  # ack not observed yet

    rev = store.kv_get(job.job_id, "trajectory_last_redirect_rev")
    assert rev == 1
    instr = Steering(store).all_instructions(job.job_id)[0]
    assert instr["status"] != "acknowledged"

    # still unacknowledged a tick later: `settle_evidence_after` stays None
    clock.advance(5)
    supervisor.step(job.job_id)
    trig = triggers_module.TriggerState.from_dict(store.kv_get(job.job_id, "trajectory_trigger_state"))
    assert trig.settle_evidence_after is None


def test_settle_evidence_after_is_set_from_the_ack_observed_this_tick(steward_home):
    store = Store(steward_home)
    job, supervisor, link, notifier, clock = _setup(store, mode="active", reviewer_steps=[_verdict()])

    supervisor.step(job.job_id)
    supervisor.step(job.job_id)  # applies the redirect; starts the settle window

    rev = store.kv_get(job.job_id, "trajectory_last_redirect_rev")
    Steering(store).mark_acknowledged(job.job_id, rev, "acknowledged")

    clock.advance(5)
    supervisor.step(job.job_id)

    trig = triggers_module.TriggerState.from_dict(store.kv_get(job.job_id, "trajectory_trigger_state"))
    assert trig.settle_evidence_after == clock.t  # this tick's `now` (D8: never the wall-clock ack timestamp)


# ------------------------------------------------------------- config loading


def test_load_trajectory_config_defaults_when_missing(tmp_path):
    cfg = trajectory.load_trajectory_config(tmp_path / "does-not-exist.toml")
    assert cfg == trajectory.TrajectoryConfig()


def test_load_trajectory_config_reads_toml(tmp_path):
    path = tmp_path / "trajectory.toml"
    path.write_text(
        "mode = \"active\"\n"
        "advice_ttl_s = 123\n"
        "max_reviews_per_job = 3\n"
        "supervision_budget_usd = 1.5\n"
        "[triggers]\n"
        "cadence_s = 60\n"
        "[hysteresis]\n"
        "min_redirect_confidence = 0.8\n"
    )
    cfg = trajectory.load_trajectory_config(path)
    assert cfg.mode == "active"
    assert cfg.advice_ttl_s == 123
    assert cfg.max_reviews_per_job == 3
    assert cfg.supervision_budget_usd == 1.5
    assert cfg.triggers.cadence_s == 60
    assert cfg.hysteresis.min_redirect_confidence == 0.8


def test_env_override_beats_the_config_file(tmp_path, monkeypatch):
    path = tmp_path / "trajectory.toml"
    path.write_text("mode = \"shadow\"\n")
    monkeypatch.setenv("STEWARD_TRAJECTORY", "active")
    cfg = trajectory.load_trajectory_config(path)
    assert cfg.mode == "active"


def test_config_source_reloads_on_mtime_change(tmp_path):
    path = tmp_path / "trajectory.toml"
    path.write_text("mode = \"shadow\"\n")
    source = trajectory.TrajectoryConfigSource(path)
    assert source.current().mode == "shadow"

    import os
    import time

    time.sleep(0.01)
    path.write_text("mode = \"active\"\n")
    os.utime(path, None)
    assert source.current().mode == "active"


# ------------------------------------------------------------- repeat suppression (audit rec 3a)


def test_identical_repeat_concern_is_suppressed(steward_home):
    store = Store(steward_home)
    verdict = _verdict(
        trajectory_="questionable", confidence=0.5, urgency="normal", concern="scope_creep", action="continue"
    )
    job, supervisor, _l, _n, clock = _setup(store, mode="active", reviewer_steps=[verdict, verdict])

    for _ in range(3):
        supervisor.request_review_now(job.job_id, "test")
        supervisor.step(job.job_id)
        clock.advance(4000)

    assert len(_events(store, job.job_id, "trajectory_concern_recorded")) == 1
    assert _events(store, job.job_id, "trajectory_hold") == []
    assert len(_events(store, job.job_id, "trajectory_repeat_suppressed")) == 2


def test_repeat_sound_verdicts_are_still_recorded_as_holds(steward_home):
    store = Store(steward_home)
    verdict = _verdict(trajectory_="sound", concern="none", action="continue")
    job, supervisor, _l, _n, clock = _setup(store, mode="active", reviewer_steps=[verdict, verdict])

    for _ in range(2):
        supervisor.request_review_now(job.job_id, "test")
        supervisor.step(job.job_id)
        clock.advance(4000)

    assert len(_events(store, job.job_id, "trajectory_hold")) == 2
    assert _events(store, job.job_id, "trajectory_repeat_suppressed") == []
