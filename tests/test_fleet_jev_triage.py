"""Phase 32 (FLEET-32/33/35): per-lane Jev triage for fleet jobs and the narrow Arbiter amendment.

Only `lanes_overlapping`, `runtime_misbehaving` and `lane_off_task` AFTER a watcher/advisor already acted on that
lane become tech-lead (outer) facts; `lane_stalled` keeps the doctor path exactly like a heartbeat stall; every
other Jev flag stays advisory. The existing gate applies per lane (confidence >= 0.7, same choice twice, per-choice
cooldown). New triggers ship in shadow. Real Store + temp Git (the Phase 29 TL world); Jev is a scripted fake."""

from __future__ import annotations

import json

import pytest
from test_fleet_lanes import JOB
from test_fleet_tech_lead import TL

from steward import fleet_lease as fl
from steward import fleet_runtime as rt
from steward import jev_watch
from steward.jev_observer import FLEET_CHOICES, JevObserver


@pytest.fixture
def w(steward_home, tmp_path):
    e = TL(tmp_path, steward_home)
    e.admit()
    return e


class ScriptedJev:
    """observe_lane returns the next scripted answer for that lane (default ok)."""

    def __init__(self, script=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls = []

    def observe_lane(self, store, job_id, lane_id, signals):
        self.calls.append((lane_id, signals))
        seq = self.script.get(lane_id) or []
        choice, conf = seq.pop(0) if seq else ("ok", 0.9)
        return {"choice": choice, "confidence": conf, "probabilities": {choice: conf}}


def triage(w, script=None, touched=None):
    jev = ScriptedJev(script)
    t = jev_watch.FleetLaneTriage(w.store, jev, w.ctl, clock=w.clock,
                                  touched_fn=(lambda job, lane: (touched or {}).get(lane, [])))
    return t, jev


def tick(t, w, n=1, dt=11.0):
    out = []
    for _ in range(n):
        w.clock.t += dt
        out.append(t.step(JOB))
    return out


# --------------------------------------------------------------------------------------------- the Arbiter amendment

def test_only_three_jev_fleet_flags_reach_the_tech_lead(w):
    arb = w.ctl.arbiter
    assert arb.submit(JOB, "jev_fleet", "lanes_overlapping", lane="lane-t1")["route"] == "outer"
    assert arb.submit(JOB, "jev_fleet", "runtime_misbehaving", lane="lane-t2")["route"] == "outer"
    assert arb.submit(JOB, "jev_fleet", "lane_off_task", lane="lane-t1")["route"] == "advisory"  # nobody acted yet
    assert arb.submit(JOB, "jev_fleet", "ok", lane="lane-t1")["route"] == "advisory"
    assert arb.submit(JOB, "jev_fleet", "something_new", lane="lane-t1")["route"] == "advisory"
    assert {f["kind"] for f in arb.pending_for_tech_lead(JOB)} == {"lanes_overlapping", "runtime_misbehaving"}


def test_lane_off_task_is_promoted_only_after_a_watcher_or_advisor_acted_on_that_lane(w):
    arb = w.ctl.arbiter
    arb.submit(JOB, "watcher", "nudged", lane="lane-t2")           # acted on ANOTHER lane
    assert arb.submit(JOB, "jev_fleet", "lane_off_task", lane="lane-t1")["route"] == "advisory"
    arb.submit(JOB, "advisor", "reviewed", lane="lane-t1")
    assert arb.submit(JOB, "jev_fleet", "lane_off_task", lane="lane-t1")["route"] == "outer"
    # the "acted" memory survives the bounded fact list rolling over
    for _ in range(rt.Arbiter.MAX_FACTS + 5):
        arb.submit(JOB, "heartbeat", "progressing")
    assert arb.submit(JOB, "jev_fleet", "lane_off_task", lane="lane-t1")["route"] == "outer"


def test_lane_stalled_keeps_the_doctor_path_exactly_like_a_stall(w):
    arb = w.ctl.arbiter
    assert arb.submit(JOB, "jev_fleet", "lane_stalled", lane="lane-t1")["route"] == "doctor"
    assert arb.pending_for_tech_lead(JOB) == []
    arb.submit(JOB, "jev_fleet", "lane_stalled", lane="lane-t1")
    assert [f["kind"] for f in arb.pending_for_tech_lead(JOB)] == ["repeated_stall"]   # today's escalation rule


def test_legacy_jev_and_efficiency_routes_are_unchanged(w):
    arb = w.ctl.arbiter
    assert arb.submit(JOB, "jev", "flag")["route"] == "advisory"
    assert arb.submit(JOB, "jev", "lanes_overlapping")["route"] == "advisory"   # only the fleet source is promoted
    assert arb.submit(JOB, "efficiency", "finding")["route"] == "advisory"
    assert jev_watch.ROUTES["stalled"] == "doctor" and "lanes_overlapping" not in jev_watch.ROUTES


# --------------------------------------------------------------------------------------------- per-lane triage + gate

def test_each_live_lane_is_triaged_with_its_own_context(w):
    t, jev = triage(w, touched={"lane-t1": ["src/a/x.py"], "lane-t2": ["src/b/y.py"]})
    tick(t, w)
    lanes = sorted(c[0] for c in jev.calls)
    assert lanes == ["lane-t1", "lane-t2"]
    ctx = dict(jev.calls)["lane-t1"]
    assert ctx["lane"] == "lane-t1" and ctx["task"] == "t1" and ctx["owns"] == ["src/a"]
    assert ctx["touched"] == ["src/a/x.py"] and ctx["others"] == [{"lane": "lane-t2", "touched": ["src/b/y.py"]}]
    assert ctx["overlap"] == [] and "runtime" in ctx and ctx["state"] == "running"
    assert len(json.dumps(ctx)) <= jev_watch.MAX_STATE_CHARS


def test_overlap_hint_names_the_shared_paths(w):
    t, jev = triage(w, touched={"lane-t1": ["NOTES.md", "src/a/x.py"], "lane-t2": ["NOTES.md"]})
    tick(t, w)
    ctx = dict(jev.calls)["lane-t1"]
    assert ctx["overlap"] == [{"lane": "lane-t2", "paths": ["NOTES.md"]}]


def test_the_gate_needs_confidence_the_same_choice_twice_and_respects_the_per_lane_cooldown(w):
    t, _ = triage(w, script={"lane-t1": [("lanes_overlapping", 0.6), ("lanes_overlapping", 0.9),
                                          ("lane_off_task", 0.9), ("lanes_overlapping", 0.9),
                                          ("lanes_overlapping", 0.9), ("lanes_overlapping", 0.9),
                                          ("lanes_overlapping", 0.9)]})
    flags = lambda: [e for e in w.fs.fleet_events(JOB) if e["what"] == "jev_lane_flag"]  # noqa: E731
    tick(t, w, 3)
    assert flags() == []                       # 0.6 never counts; a different choice resets the streak
    tick(t, w, 2)
    assert len(flags()) == 1                   # same choice twice at >= 0.7
    tick(t, w, 2)
    assert len(flags()) == 1                   # still inside that lane's 300 s cooldown for this choice
    ev = json.loads(flags()[0]["evidence"])
    assert flags()[0]["lane_id"] == "lane-t1" and ev["choice"] == "lanes_overlapping"
    assert ev["route"] == "outer" and ev["trigger_mode"] == "shadow" and ev["would_act"] == "wake_tech_lead"
    assert ev["observer"] == "jev"   # which observer produced the verdict is always on the record


def test_lanes_are_gated_independently(w):
    t, _ = triage(w, script={"lane-t1": [("runtime_misbehaving", 0.9)], "lane-t2": [("runtime_misbehaving", 0.9)]})
    tick(t, w, 2)
    assert [e for e in w.fs.fleet_events(JOB) if e["what"] == "jev_lane_flag"] == []   # one each: no streak


def test_a_flag_is_submitted_to_the_arbiter_and_wakes_the_tech_lead_as_a_shadow_fact(w):
    t, _ = triage(w, script={"lane-t1": [("lanes_overlapping", 0.9)] * 2})
    tick(t, w, 2)
    pending = w.ctl.arbiter.pending_for_tech_lead(JOB)
    assert [(f["source"], f["kind"], f["lane"]) for f in pending] == [("jev_fleet", "lanes_overlapping", "lane-t1")]
    assert pending[0]["data"]["trigger_mode"] == "shadow"


def test_a_non_promoted_flag_stays_advisory(w):
    t, _ = triage(w, script={"lane-t1": [("lane_off_task", 0.9)] * 2})
    tick(t, w, 2)
    assert w.ctl.arbiter.pending_for_tech_lead(JOB) == []
    ev = json.loads([e for e in w.fs.fleet_events(JOB) if e["what"] == "jev_lane_flag"][0]["evidence"])
    assert ev["route"] == "advisory" and ev["would_act"] == "none"


def test_triage_runs_on_the_interval_and_is_off_when_jev_is_off(w):
    t, jev = triage(w)
    tick(t, w, 1, dt=11)
    tick(t, w, 1, dt=2)            # not due again yet
    assert len(jev.calls) == 2      # two lanes, one round
    from steward import supervisors
    w.store.kv_set(JOB, supervisors.SWITCH_KEYS["jev"], "off")
    assert supervisors.jev_mode(w.store, JOB) == "off"
    tick(t, w, 1)
    assert len(jev.calls) == 2


def test_triage_never_mutates_lanes_or_the_graph(w):
    before = (json.dumps(w.fs.lanes_view(JOB), sort_keys=True), w.fs.current_graph(JOB),
              [fl.get(w.store, JOB, ln["lane"]) for ln in w.fs.lanes_view(JOB)])
    t, _ = triage(w, script={"lane-t1": [("lanes_overlapping", 0.95)] * 4,
                             "lane-t2": [("runtime_misbehaving", 0.95)] * 4})
    tick(t, w, 4)
    after = (json.dumps(w.fs.lanes_view(JOB), sort_keys=True), w.fs.current_graph(JOB),
             [fl.get(w.store, JOB, ln["lane"]) for ln in w.fs.lanes_view(JOB)])
    assert before == after and w.deliveries() == 0


def test_a_legacy_job_is_never_lane_triaged(w):
    w.store.create_job("legacy", "/repo", job_id="LEG")
    t, jev = triage(w)
    assert t.step("LEG") == {} and jev.calls == []


def test_the_real_observer_has_a_fleet_question_with_exactly_the_fleet_choices():
    assert FLEET_CHOICES == ("ok", "lane_stalled", "lane_off_task", "lanes_overlapping", "runtime_misbehaving")
    body = json.loads(JevObserver(enabled=True)._build_lane_body({"lane": "lane-a"}))
    q = body["questions"]["lane_triage"]
    assert set(q["criteria"]) == set(FLEET_CHOICES) and body["state"]["signals"] == {"lane": "lane-a"}
    from steward.jev_observer import CHOICES
    assert "lanes_overlapping" not in CHOICES   # the legacy question is untouched


def test_precision_scoring_counts_independent_corroboration_only(w):
    t, _ = triage(w, script={"lane-t1": [("lanes_overlapping", 0.9)] * 2})
    tick(t, w, 2)
    s = jev_watch.score_lanes(w.store, JOB, now=w.clock.t)
    assert s["flags"] == 1 and s["pending"] == 1 and s["precision"] is None
    w.fs.append_event(JOB, what="tech_lead_proposal_shadowed", actor="tech_lead", rule="x", judgment="code",
                      evidence={})   # downstream of the flag: proves nothing
    assert jev_watch.score_lanes(w.store, JOB, now=w.clock.t)["agree"] == 0
    w.fs.append_event(JOB, what="integration conflict", actor="integration_writer", rule="x", judgment="code",
                      lane_id="lane-t1", evidence={})
    s = jev_watch.score_lanes(w.store, JOB, now=w.clock.t)
    assert s["agree"] == 1 and s["by_choice"]["lanes_overlapping"]["agree"] == 1
    late = jev_watch.score_lanes(w.store, JOB, now=w.clock.t + 10_000)
    assert late["precision"] == 1.0


# --------------------------------------------------------------------------------------------- runner wiring

def test_the_runner_triages_fleet_lanes_on_the_jev_timer_and_legacy_jobs_keep_their_path(steward_home, tmp_path):
    from test_fleet_wiring import World, _runner, seeded

    from steward import fleet_live
    from steward.runner_exec import SubprocessRunner
    store, _ = seeded(tmp_path, steward_home)
    runner = _runner(store)
    world = World()
    fleet_live.wire(runner, "J1", adapter_factory=world.factory, probe=world.probe, runner_exec=SubprocessRunner())
    assert isinstance(runner.fleet_triage, jev_watch.FleetLaneTriage) and runner.fleet_triage.controller is \
        runner.fleet_controller
    calls = []
    runner.fleet_triage.step = lambda job_id, **kw: calls.append(job_id) or {}
    runner.jev_step("J1")
    assert calls == ["J1"]
    legacy = store.create_job("legacy", "/repo", job_id="LEG")
    runner.jev_step(legacy.job_id)   # no single worker registered: nothing happens, exactly as before
    assert calls == ["J1"]


def test_wire_uses_the_ordered_advisor_it_is_given(steward_home, tmp_path):
    from test_fleet_wiring import World, _runner, seeded

    from steward import fleet_live
    from steward import fleet_tech_lead as tl
    from steward.runner_exec import SubprocessRunner
    store, _ = seeded(tmp_path, steward_home)
    runner = _runner(store)
    adv = tl.OrderedTechLead([], invoke=lambda *a: None)
    fleet_live.wire(runner, "J1", adapter_factory=World().factory, probe=World().probe,
                    runner_exec=SubprocessRunner(), advisor=adv)
    assert runner.tech_lead.advisor is adv


def test_every_fleet_tick_gives_lane_triage_its_chance_whatever_the_tick_interval(steward_home, tmp_path):
    from test_fleet_wiring import World, _runner, seeded

    from steward import fleet_live
    from steward.runner_exec import SubprocessRunner
    store, _ = seeded(tmp_path, steward_home)
    runner = _runner(store)
    world = World()
    fleet_live.wire(runner, "J1", adapter_factory=world.factory, probe=world.probe, runner_exec=SubprocessRunner())
    runner.start("J1")
    calls = []
    runner.fleet_triage.step = lambda job_id, **kw: calls.append(job_id) or {}
    runner.tick("J1")
    assert calls == ["J1"]
