"""A13 (Plan 1 Task 14 partial): the Plan 1 qualification scenarios (spec §11 s9, §13 j1-j6/j15, §14 f1/f7/f9) that no
other acceptance file proves, each through an isolated real path (real Store, sweep, lead, guard, trace) with fake
clocks and scripted reviewers: no model, no network. `SCENARIO_TESTS` maps each scenario id to its test(s) for the
requirement-to-evidence matrix; scenarios other files prove are listed there too, unproven ones are not claimed."""
from __future__ import annotations

import time

import pytest

from steward import blockers, decision_trace as dt, intents, jev_watch, ownership, sendback
from steward import jev_observer
from steward import tech_lead_policy as tpol
from steward.actor import Actor, ActorRef
from steward.models import JobState, Judgment
from steward.steering import Steering
from steward.store import Store
from ownership_fixtures import FakeLead, Notes, contracted, control_lead, park_done, park_tracks, proposal

LEAD = ActorRef(Actor.TECH_LEAD, "astra-tech-lead")
OWNER = ActorRef(Actor.OWNER, "cli")
P = "tests/test_continuous_ownership_recovery_acceptance_"
SCENARIO_TESTS = {
    "s1": [P + "review_loop.py::s1_parked_unfinished_job_with_no_runner_is_reviewed_once_across_duplicate_ticks"],
    "s5": [P + "guards.py::s5_answers_resume_once_unanswered_and_stop_bind"],
    "s9": [P + "scenarios.py::s9_concurrent_and_restarted_controllers_do_not_duplicate_and_stale_answers_cannot_act"],
    "s14": [P + "recovery.py::s14_legacy_jobs_keep_grants_stops_caps_approvals_and_delivery"],
    "s15": [P + "review_outage.py::s15_all_reviewers_unavailable_acceptance"],
    "s17": [P + "lead_actions.py::s17_mistaken_blocker_is_corrected_and_the_worker_actually_resumes"],
    "s19": [P + "recovery.py::s19_skill_guided_and_direct_work_meet_the_same_evidence_bar",
            P + "recovery.py::s19_internal_workflow_stop_is_reviewed_and_explicit_owner_stop_binds"],
    "j1": [P + "scenarios.py::j1_low_consequence_jev_claim_does_not_interrupt_the_worker"],
    "j2": [P + "scenarios.py::j2_disputed_completion_claim_is_resolved_by_review_before_reliance"],
    "j3": [P + "scenarios.py::j3_independent_work_continues_while_a_concern_is_assessed"],
    "j4": [P + "scenarios.py::j4_repeated_unchanged_findings_reuse_one_episode_and_one_review"],
    "j5": [P + "scenarios.py::j5_jev_abstention_and_outage_cannot_starve_a_required_review"],
    "j6": [P + "scenarios.py::j6_a_jev_concern_cannot_accumulate_into_an_unwithdrawable_hold"],
    "j15": [P + "scenarios.py::j15_jev_labels_cannot_complete_or_halt_a_job"],
    "f1": [P + "scenarios.py::f1_a_flag_preserves_evidence_across_restart_and_retention_until_closed"],
    "f7": [P + "scenarios.py::f7_a_flag_alone_changes_nothing_about_the_run"],
    "f9": [P + "scenarios.py::f9_model_judgments_operator_agents_and_owner_keep_distinct_provenance"],
}


def _reopen(text="check the unit before trusting the failure"):
    return proposal({"type": "reopen", "params": {"text": text, "evidence": "x", "confidence": 0.9}})


def _running(store, goal="implement the feature"):
    jid = contracted(store, goal)
    store.set_state(jid, JobState.RUNNING, what="worker attempt started", evidence={}, rule="t",
                    judgment=Judgment.code)
    return jid


def _ready(store, jid):
    t = time.time()
    ownership.reconcile_job(store, jid, now=t)
    return t + 31


def _cycle(store, lead, t, notifier=None):
    ownership.sweep(store, now=t, job_lead=lead, notifier=notifier)
    return ownership.sweep(store, now=t + 1, job_lead=lead, notifier=notifier)


def test_continuous_ownership_recovery_acceptance_s9_concurrent_and_restarted_controllers_do_not_duplicate_and_stale_answers_cannot_act(
        steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    lead_a, scripted_a = control_lead(store, {"astra-tech-lead": [_reopen(), _reopen()]})
    lead_b, scripted_b = control_lead(store, {"astra-tech-lead": [_reopen(), _reopen()]})
    t = _ready(store, jid)
    ownership.sweep(store, now=t, job_lead=lead_a, owner="ctl-a:1")
    ownership.sweep(store, now=t, job_lead=lead_b, owner="ctl-b:2")                  # a concurrent controller
    assert len(scripted_a.calls) == 1 and scripted_b.calls == []                      # one review, one writer
    b = blockers.open_blockers(store, jid)[0]
    blockers.add_evidence(store, b.blocker_id, ["answer:q1"], actor=ownership.CONTROLLER, now=t + 0.5)
    ownership.sweep(store, now=t + 1, job_lead=lead_a, owner="ctl-a:1")              # the answer is older than the evidence
    assert [e.evidence["reason"] for e in store.events(jid) if e.what == "tech_lead_answer_discarded"] == [
        "evidence_changed"]
    assert store.get_job(jid).state == JobState.NEEDS_HUMAN
    assert not [e for e in store.events(jid) if e.what in ("sent_back", "tech_lead_reopen")]
    restarted, scripted_c = control_lead(store, {"astra-tech-lead": [_reopen()]})     # the controller restarts
    ownership.recover(store, now=t + 2, owner="ctl-c:3")
    for dt_ in (40, 41, 42):
        ownership.sweep(store, now=t + dt_, job_lead=restarted, owner="ctl-c:3")
    assert store.get_job(jid).state == JobState.RUNNING
    assert len([e for e in store.events(jid) if e.what == "sent_back"]) == 1          # exactly one send-back
    assert len(scripted_c.calls) == 1


def test_continuous_ownership_recovery_acceptance_j1_low_consequence_jev_claim_does_not_interrupt_the_worker(
        steward_home):
    store = Store(steward_home)
    notes = Notes()
    jid = _running(store)
    ownership.raise_signal_concern(store, jid, source="jev", choice="thrash", text="progress looks slow",
                                   confidence=0.6, now=time.time())
    ownership.sweep(store, now=time.time() + 100, notifier=notes)
    assert store.get_job(jid).state == JobState.RUNNING                               # never halted or parked
    assert Steering(store).all_instructions(jid) == [] and notes.events == [] and notes.texts == []
    assert ownership.owner_view(store, jid).status != "needs_owner"
    assert not [e for e in store.events(jid) if e.what in ("sent_back", "stop requested")]


def test_continuous_ownership_recovery_acceptance_j2_disputed_completion_claim_is_resolved_by_review_before_reliance(
        steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    t = _ready(store, jid)
    c, _ = ownership.raise_signal_concern(store, jid, source="jev", choice="evidence_contradicts",
                                          text="the done claim's cited test does not exist", confidence=0.85,
                                          now=t - 31, refs=["claim:done:abc123"])
    assert [b.blocker_id for b in ownership.reliance_hold(store, jid, "claim:done:abc123")] == [c.blocker_id]
    lead, scripted = control_lead(store, {"astra-tech-lead": [proposal(
        {"type": "resolve_blocker", "params": {"blocker_id": c.blocker_id, "evidence": ["git show HEAD:tests/t.py"],
                                               "reason": "the test exists at tests/t.py::test_a; Jev saw a stale path",
                                               "confidence": 0.9}},
        {"type": "reopen", "params": {"text": "continue; the cited evidence checks out", "evidence": "x",
                                      "confidence": 0.9}})]})
    _cycle(store, lead, t)
    assert blockers.get(store, c.blocker_id).resolution["actor"] == "tech_lead:astra-tech-lead"
    assert ownership.reliance_hold(store, jid, "claim:done:abc123") == []             # assessed before it was relied on
    assert "evidence_contradicts" in scripted.calls[0][1] or "claim:done:abc123" in scripted.calls[0][1]


def test_continuous_ownership_recovery_acceptance_j3_independent_work_continues_while_a_concern_is_assessed(
        steward_home):
    store = Store(steward_home)
    notes = Notes()
    concerned = park_tracks(store)
    independent = park_done(store)                                                     # a genuine acceptance decision
    running = _running(store)
    t = _ready(store, concerned)
    ownership.reconcile_job(store, independent, now=t - 31)
    ownership.raise_signal_concern(store, concerned, source="jev", choice="thrash", text="x", confidence=0.7,
                                   now=t - 31)
    lead = FakeLead()
    ownership.sweep(store, now=t, job_lead=lead, notifier=notes)
    assert [c["job"] for c in lead.calls] == [concerned]                               # only the concern is under review
    assert notes.kinds() == ["READY"] and notes.events[0].job_id == independent        # unrelated decision still surfaces
    assert store.get_job(running).state == JobState.RUNNING and store.get_job(independent).state == JobState.NEEDS_HUMAN


def test_continuous_ownership_recovery_acceptance_j4_repeated_unchanged_findings_reuse_one_episode_and_one_review(
        steward_home):
    store = Store(steward_home)
    notes = Notes()
    jid = _running(store)
    t = time.time()
    seen = {ownership.raise_signal_concern(store, jid, source="jev", choice="thrash", text="x", confidence=0.7,
                                           now=t + i)[0].blocker_id for i in range(6)}
    assert len(seen) == 1 and len(blockers.for_job(store, jid)) == 1
    for i in range(4):                                                                 # repeated ticks: one Arbiter fact
        ownership.sweep(store, now=t + 40 + i * 31, notifier=notes)
    from steward import fleet_runtime as rt

    assert [x["source"] for x in rt.Arbiter(store).pending_for_tech_lead(jid)] == ["ownership"]
    assert notes.events == [] and notes.texts == []
    assert len([e for e in store.events(jid) if e.what == "blocker_raised"]) == 1
    from test_flow_actions import _episode, _investigation
    from steward import flow_actions as fa

    parked = park_tracks(store)
    for i in range(3):                                                                  # the same Flow finding again
        ep = _episode(store, parked, label="retry_or_rebuild", now=1000.0 + i * 2000)
        fa.process(store, ep, _investigation(store, ep), mode="active", now=1000.0 + i * 2000)
    assert len([b for b in blockers.for_job(store, parked) if b.kind == "flow_finding"]) == 1


def test_continuous_ownership_recovery_acceptance_j5_jev_abstention_and_outage_cannot_starve_a_required_review(
        steward_home):
    store = Store(steward_home)
    notes = Notes()
    jid = park_tracks(store)
    jev_down = jev_watch.OutageAlert(store, lambda text: True, clock=lambda: 1.0)
    store.kv_set(jid, jev_observer.STATUS_KEY, "unavailable:http_402")
    jev_down.check(jid, None)                                                          # Jev is down, abstains entirely
    assert jev_watch.outage_status(store) is not None
    lead, scripted = control_lead(store, {"astra-tech-lead": [_reopen()]}, notifier=notes)
    t = _ready(store, jid)
    _cycle(store, lead, t, notes)
    assert len(scripted.calls) == 1 and store.get_job(jid).state == JobState.RUNNING   # code facts + aging routed it
    starved = park_tracks(store)                                                        # no lead at all: never concealed
    ownership.reconcile_job(store, starved, now=1000.0)
    ownership.sweep(store, now=1000.0 + ownership.UNREVIEWED_FAILOPEN_S + 10, job_lead=None, notifier=notes)
    assert ownership.owner_view(store, starved, now=1000.0 + ownership.UNREVIEWED_FAILOPEN_S + 10).status in (
        "needs_owner", "technical_review_unavailable")


def test_continuous_ownership_recovery_acceptance_j6_a_jev_concern_cannot_accumulate_into_an_unwithdrawable_hold(
        steward_home):
    store = Store(steward_home)
    jid = _running(store)
    t = 1000.0
    ids = [ownership.raise_signal_concern(store, jid, source="jev", choice=c, text="x", confidence=0.7, now=t,
                                          refs=["claim:done:c1"])[0].blocker_id
           for c in ("thrash", "permission_confusion", "risky_action")]
    assert len(ownership.reliance_hold(store, jid, "claim:done:c1")) == 3
    ownership.reconcile_job(store, jid, now=t + ownership.JEV_CONCERN_TTL_S / 2)
    assert len(ownership.reliance_hold(store, jid, "claim:done:c1")) == 3              # still seen recently
    ownership.raise_signal_concern(store, jid, source="jev", choice="thrash", text="x", confidence=0.7,
                                   now=t + ownership.JEV_CONCERN_TTL_S / 2, refs=["claim:done:c1"])
    ownership.reconcile_job(store, jid, now=t + ownership.JEV_CONCERN_TTL_S + 10)
    assert [b.issue_key for b in blockers.open_blockers(store, jid)] == [blockers.issue_key("jev_concern", "jev:thrash")]
    ownership.reconcile_job(store, jid, now=t + ownership.JEV_CONCERN_TTL_S * 2)       # not seen again: all withdrawn
    assert blockers.open_blockers(store, jid) == [] and ownership.reliance_hold(store, jid, "claim:done:c1") == []
    assert store.get_job(jid).state == JobState.RUNNING


def test_continuous_ownership_recovery_acceptance_j15_jev_labels_cannot_complete_or_halt_a_job(steward_home):
    from steward import stability
    from steward.runner import Runner, _JevActions
    from steward.diagnosis import Diagnosis, FakeDiagnoser
    from steward.worker import FakeWorker
    from ownership_fixtures import git_ok

    store = Store(steward_home)
    jid = _running(store)
    runner = Runner(store, lambda ref: FakeWorker(script=[]), git_ok(),
                    FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="g", hypothesis="h")),
                    clock=time.time, notifier=Notes())
    acts = _JevActions(runner)
    acts.concern(jid, "risky_action", 0.99)                                            # a strongly negative label
    assert not hasattr(acts, "finished")                                                # no positive label completes
    assert store.get_job(jid).state == JobState.RUNNING                                 # neither halted nor completed
    (b,) = blockers.open_blockers(store, jid)
    jev = ActorRef(Actor.JEV, "jev")
    for to in ("resolved", "confirmed_human"):
        with pytest.raises(blockers.BlockerError):
            blockers.transition(store, b.blocker_id, to, actor=jev, reason="label", now=time.time(),
                                human_request=blockers.HumanRequest("d", "w") if to == "confirmed_human" else None)
    assert [e for e in store.events(jid) if e.what in ("checks passed at exact commit", "job accepted")] == []


def test_continuous_ownership_recovery_acceptance_f1_a_flag_preserves_evidence_across_restart_and_retention_until_closed(
        steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    lead, _ = control_lead(store, {"astra-tech-lead": [_reopen()]})
    t = time.time()
    ownership.reconcile_job(store, jid, now=t)
    _cycle(store, lead, t + 31)
    before = [(r["stage"], r["decision"]) for r in dt.trace(store, jid)]
    assert ("proposal", next(d for s, d in before if s == "proposal")) in before and len(before) >= 4
    flag, created = dt.flag_run(store, jid, description="the review reopened a job I wanted stopped", actor=OWNER,
                                now=t + 60)
    assert created
    restarted = Store(steward_home)
    far = t + 90 * 86400
    ownership.sweep(restarted, now=far)                                                 # retention runs in the sweep
    assert [(r["stage"], r["decision"]) for r in dt.trace(restarted, jid)] == before    # preserved, nothing reconstructed
    assert dt.preserved(restarted, jid, now=far) is True
    dt.close_flag(restarted, flag["flag_id"], actor=OWNER, resolution="investigated", now=far + 10)
    ownership.sweep(restarted, now=far + dt.PURGE_EVERY_S + 20)                         # within 30 days of closing: kept
    assert len(dt.trace(restarted, jid)) == len(before)
    late = far + 40 * 86400
    ownership.sweep(restarted, now=late)
    assert dt.trace(restarted, jid) == []
    assert dt.trace(restarted, jid) == []                                                # purged after closure + retention


def test_continuous_ownership_recovery_acceptance_f7_a_flag_alone_changes_nothing_about_the_run(steward_home):
    from steward import cli

    store = Store(steward_home)
    notes = Notes()
    jid = park_tracks(store)
    t = _ready(store, jid)
    ownership.sweep(store, now=t, job_lead=FakeLead(), notifier=notes)
    snapshot = lambda: (store.get_job(jid).state, [(b.blocker_id, b.disposition) for b in blockers.open_blockers(store, jid)],  # noqa: E731
                        len(intents.for_job(store, jid)), Steering(store).all_instructions(jid), store.get_revision(jid),
                        len(notes.events), tpol.load_or_defaults()[0])
    before = snapshot()
    got = cli._build_control_handlers(store)["flag"]({"op": "flag", "origin": "cli", "args": {
        "job_id": jid, "description": "this should not have been parked", "actor": {"kind": "owner", "origin": "cli"}}})
    assert got["accepted"]
    assert snapshot() == before                                                          # state, blockers, intents,
    assert len(dt.open_flags(store, jid)) == 1                                           # steering, policy: untouched
    dup = cli._build_control_handlers(store)["flag"]({"op": "flag", "origin": "cli", "args": {
        "job_id": jid, "description": "same incident", "actor": {"kind": "owner", "origin": "cli"}}})
    assert dup["accepted"] and not dup["created"] and len(dt.open_flags(store, jid)) == 1   # no second investigation


def test_continuous_ownership_recovery_acceptance_f9_model_judgments_operator_agents_and_owner_keep_distinct_provenance(
        steward_home, monkeypatch):
    from steward import actor, cli

    store = Store(steward_home)
    jid = park_tracks(store)
    lead, _ = control_lead(store, {"astra-tech-lead": [_reopen()]})
    t = time.time()
    ownership.reconcile_job(store, jid, now=t)
    _cycle(store, lead, t + 31)
    h = cli._build_control_handlers(store)["flag"]
    monkeypatch.delenv("CLAUDECODE", raising=False)
    h({"op": "flag", "origin": "cli", "actor": actor.detect_cli_actor({}),
       "args": {"job_id": jid, "description": "owner says wrong"}})
    ownership.raise_signal_concern(store, jid, source="jev", choice="thrash", text="x", confidence=0.7, now=t + 100)
    h({"op": "flag", "origin": "cli", "actor": actor.detect_cli_actor({"CLAUDECODE": "1"}),
       "args": {"job_id": jid, "description": "agent notes it too"}})
    kinds = {r["actor_kind"] for r in dt.trace(store, jid)}
    assert {"tech_lead", "controller", "jev"} <= kinds and "owner" not in kinds        # models never recorded as the owner
    (f,) = dt.open_flags(store, jid)
    assert (f["actor_kind"], f["actor_id"]) == ("owner", "cli")                          # the owner's own flag
    notes = store._conn.execute("SELECT actor_kind FROM run_flag_notes WHERE flag_id = ?", (f["flag_id"],)).fetchall()
    assert [n[0] for n in notes] == ["operator_agent"]                                    # an agent's note is its own
