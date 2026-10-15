"""Plan 1 Task 9: the lead's blocker actions through the real gate and executor (spec §5, §7; G9, G11, G12, G16,
G18-G21, G37), idempotent reopen, and discarded stale answers (spec §11 s9; review focus 2)."""
from __future__ import annotations

import time

import pytest

from steward import approvals, blockers, intents, ownership, sendback
from steward import tech_lead_actions as tla
from steward import tech_lead_floor as floor
from steward import tech_lead_notebook as nb
from steward import tech_lead_policy as tpol
from steward.models import JobState, Judgment
from steward.store import Store
from ownership_fixtures import Notes, contracted, control_lead, park_done, park_tracks, proposal
from test_tech_lead_notebook import _lead, review, sb


def _cycle(store, lead, t, notifier=None):
    """One review: the sweep at t starts it (the scripted answer is ready at once), the sweep at t+1 finishes it."""
    ownership.sweep(store, now=t, job_lead=lead, notifier=notifier)
    return ownership.sweep(store, now=t + 1, job_lead=lead, notifier=notifier)


def _ready(store, jid, t=None) -> float:
    """Classify at real time (the lead's own clock is real: its records must not look newer than the sweeps)."""
    t = time.time() if t is None else t
    ownership.reconcile_job(store, jid, now=t)
    return t + 31  # past the coalescing window


def _reopen(conf=0.9, text="check the Pi downloads dir before trusting pack_download_no_file"):
    return proposal({"type": "reopen", "params": {"text": text, "evidence": "last_said", "confidence": conf}})


def _escalate(decision="Restore the Pi's account?", why="no grant covers the device account", reason=None):
    params = {"decision": decision, "why_authority_insufficient": why} if decision else {}
    if reason:
        params["reason"] = reason
    return proposal({"type": "escalate", "params": params})


def _tl_approvals(store, jid):
    return [a for a in approvals.list_approvals(store, jid, status="pending") if a["kind"] == "tech_lead"]


def _repark(store, jid, reasons=()):
    store.kv_set(jid, sendback.REWORK_KEY, None)  # the attempt launched and parked again
    store.set_state(jid, JobState.WORKER_FINISHED, what="worker idle after its final message",
                    evidence={"marker": "idle_after_final_message"}, rule="idle_after_final_message",
                    judgment=Judgment.code)
    store.set_state(jid, JobState.NEEDS_HUMAN, what="checks passed; acceptance unresolved",
                    evidence={"reasons": list(reasons)}, rule="acceptance_unresolved", judgment=Judgment.code)


def test_steward_done_with_a_technical_blocker_is_reopened_once_through_the_seam(steward_home):
    store = Store(steward_home)
    jid = park_done(store, ["test-weakening detected: ['removed_test_function']"])
    lead, scripted = control_lead(store, {"astra-tech-lead": [_reopen()]})
    _cycle(store, lead, _ready(store, jid))
    assert store.get_job(jid).state == JobState.RUNNING
    (sent,) = [e for e in store.events(jid) if e.what == "sent_back"]
    assert sent.evidence["by"] == "tech_lead:astra-tech-lead"
    (it,) = intents.for_job(store, jid, "reopen")
    assert it.state == "done" and it.receipt["steering_rev"] == sent.evidence["steering_rev"]


def test_a_duplicate_reopen_of_the_same_park_is_claimed_once(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    ownership.reconcile_job(store, jid, now=100.0)
    calls = []

    def fake(job_id, text, origin, request_id, judgment):
        calls.append(origin)
        return {"accepted": True, "steering_rev": 1}
    outs = []
    for owner in ("A", "B"):
        ex = tla.ActionExecutor(store, runner_exec=None, send_back_fn=fake)
        ex.owner = owner
        prop = tla.parse_job_proposal(_reopen())
        ctx = tla.ActionContext(jid, "astra-tech-lead", "m", prop, store.home / "worktrees" / jid, tpol.Policy())
        outs.append(ex.execute(ctx, prop.actions[0]))
    assert [o["outcome"] for o in outs] == ["executed", "duplicate"] and len(calls) == 1


def test_reopen_below_confidence_is_recorded_not_escalated(steward_home):
    store = Store(steward_home)
    notes = Notes()
    jid = park_tracks(store)
    lead, scripted = control_lead(store, {"astra-tech-lead": [_reopen(conf=0.5)],
                                          "fable-tech-lead": [_reopen(conf=0.9)]}, notifier=notes)
    t = _ready(store, jid)
    _cycle(store, lead, t, notes)
    assert store.get_job(jid).state == JobState.NEEDS_HUMAN and _tl_approvals(store, jid) == [] and notes.events == []
    rows = blockers.open_blockers(store, jid)
    assert any(["reopen", "policy_blocked"] in (b.diagnosis or {}).get("actions", []) for b in rows)
    assert any(e.startswith("strategy_review:") for b in rows for e in b.evidence)
    _cycle(store, lead, t + 40, notes)  # the strategy review prefers the other lead
    assert [c[0] for c in scripted.calls] == ["astra-tech-lead", "fable-tech-lead"]
    assert store.get_job(jid).state == JobState.RUNNING


def test_escalation_is_structured_deduplicated_and_withdrawable(steward_home):
    store = Store(steward_home)
    notes = Notes()
    jid = park_tracks(store)
    lead, scripted = control_lead(store, {"astra-tech-lead": [_escalate(), _escalate()]}, notifier=notes)
    t = _ready(store, jid)
    _cycle(store, lead, t, notes)
    (esc,) = [b for b in blockers.open_blockers(store, jid) if b.kind == "lead_escalation"]
    assert esc.disposition == "confirmed_human" and esc.human_request["decision"] == "Restore the Pi's account?"
    assert esc.human_request["why_authority_insufficient"] == "no grant covers the device account"
    assert len(_tl_approvals(store, jid)) == 1 and notes.kinds() == ["NEEDS_YOU"]
    tracks = next(b for b in blockers.open_blockers(store, jid) if b.kind == "worker_parked_unfinished")
    blockers.add_evidence(store, tracks.blocker_id, ["event:new"], actor=ownership.CONTROLLER, now=t + 10)
    _cycle(store, lead, t + 50, notes)  # the same escalation again: one blocker, one approval, one notice
    assert len([b for b in blockers.open_blockers(store, jid) if b.kind == "lead_escalation"]) == 1
    assert len(_tl_approvals(store, jid)) == 1 and notes.kinds() == ["NEEDS_YOU"]
    scripted.plan["astra-tech-lead"].append(proposal({"type": "resolve_blocker", "params": {
        "blocker_id": esc.blocker_id, "reason": "obsolete: the unit answered on retry", "confidence": 0.9}}))
    blockers.add_evidence(store, tracks.blocker_id, ["event:newer"], actor=ownership.CONTROLLER, now=t + 60)
    _cycle(store, lead, t + 100, notes)  # the lead withdraws its own escalation
    assert not [b for b in blockers.open_blockers(store, jid) if b.kind == "lead_escalation"]
    assert [a["status"] for a in approvals.list_approvals(store, jid) if a["kind"] == "tech_lead"] == ["withdrawn"]


def test_an_escalation_without_the_exact_decision_never_reaches_the_owner(steward_home):
    store = Store(steward_home)
    notes = Notes()
    jid = park_tracks(store)
    lead, _ = control_lead(store, {"astra-tech-lead": [_escalate(decision=None, reason="stuck")],
                                   "fable-tech-lead": [_reopen()]}, notifier=notes)
    _cycle(store, lead, _ready(store, jid), notes)
    assert _tl_approvals(store, jid) == [] and notes.events == []
    acts = [e.evidence for e in store.events(jid) if e.what == "tech_lead_action"]
    assert acts[-1]["action"] == "escalate" and acts[-1]["outcome"] == "malformed"
    assert [e for e in store.events(jid) if e.what == "tech_lead_escalation_reviewed"]


def test_floor_refusal_is_one_deduplicated_owner_request(steward_home):
    store = Store(steward_home)
    notes = Notes()
    jid = park_tracks(store)
    push = proposal({"type": "push", "params": {}})
    lead, _ = control_lead(store, {"astra-tech-lead": [push], "fable-tech-lead": [push]}, notifier=notes)
    t = _ready(store, jid)
    _cycle(store, lead, t, notes)
    _cycle(store, lead, t + 40, notes)  # the bounded strategy review asks the other lead, which proposes it again
    (reserved,) = [b for b in blockers.for_job(store, jid) if b.kind == "lead_reserved_action"]
    assert reserved.disposition == "confirmed_human"
    assert "outside_world" in reserved.human_request["why_authority_insufficient"]
    assert len(_tl_approvals(store, jid)) == 1 and notes.kinds() == ["NEEDS_YOU"]
    assert store.get_job(jid).state == JobState.NEEDS_HUMAN


@pytest.fixture
def running(steward_home):
    store = Store(steward_home)
    jid = contracted(store, "fix it")
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    return store, jid


def test_third_sighting_requests_strategy_review_not_owner(running):
    store, jid = running
    lead, arb = _lead(store, [sb(), sb(), sb(strategy="takeover_hint", text="different approach"), sb()],
                      nb.Memory(store))
    review(lead, arb, jid)
    review(lead, arb, jid)
    third = review(lead, arb, jid)
    assert third["status"] == "strategy_review"
    assert [a for a in approvals.list_approvals(store, jid, status="pending") if a["kind"] == "tech_lead"] == []
    (b,) = [b for b in blockers.open_blockers(store, jid) if b.kind == "repeated_failure"]
    ev = [e for e in store.events(jid) if e.what == ownership.STRATEGY_EVENT]
    assert ev and ev[-1].evidence["blocker_id"] == b.blocker_id


def test_fix_rounds_reached_requests_strategy_review(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    pol = tpol.Policy(fix_rounds=2)

    def gate_and_run():
        ownership.ensure_classified(store, jid)
        ex = tla.ActionExecutor(store, runner_exec=None, send_back_fn=lambda j, txt, o, r, jd: sendback.send_back(
            store, j, txt, origin=o, request_id=r, judgment=jd))
        prop = tla.parse_job_proposal(_reopen())
        ctx = tla.ActionContext(jid, "astra-tech-lead", "m", prop, store.home / "worktrees" / jid, pol)
        decision, why, extra = tla.gate(prop.actions[0], ctx, floor.context_for(store, jid, ctx.worktree), store=store)
        return decision, why, extra, (ex.execute(ctx, prop.actions[0]) if decision == "execute" else None)
    for _ in range(2):
        decision, _, _, res = gate_and_run()
        assert decision == "execute" and res["outcome"] == "executed"
        _repark(store, jid)
    decision, why, extra, _ = gate_and_run()
    assert decision == "policy_blocked" and "fix_rounds" in why and extra == {"strategy_review": True}
    lead, _ = control_lead(store, {"astra-tech-lead": [_reopen()]}, policy=pol)
    _cycle(store, lead, _ready(store, jid))
    ev = [e for e in store.events(jid) if e.what == ownership.STRATEGY_EVENT]
    assert ev and "fix_rounds" in ev[-1].evidence["reason"]
    assert store.get_job(jid).state == JobState.NEEDS_HUMAN and _tl_approvals(store, jid) == []
    assert ownership.reopens_since_strategy_review(store, jid) == 0  # the counter restarts after the review


def test_rework_pending_blocks_a_second_reopen(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    ownership.reconcile_job(store, jid, now=100.0)
    store.kv_set(jid, sendback.REWORK_KEY, {"rev": 1, "text": "x", "goal_append": False, "by": "cli"})
    ok, why, _ = ownership.blocker_action_allowed(store, jid, "reopen", {"text": "x", "confidence": 0.9},
                                                  policy=tpol.Policy(), proposal_confidence=0.9)
    assert not ok and "rework pending" in why
    store.kv_set(jid, sendback.REWORK_KEY, None)
    sendback.send_back(store, jid, "first", origin="cli")
    assert sendback.send_back(store, jid, "second", origin="cli")["accepted"] is False  # G37: NEEDS_HUMAN only


def test_a_blocking_owner_question_keeps_the_job_parked(steward_home):
    from steward import questions

    store = Store(steward_home)
    jid = park_tracks(store)
    questions.save(store, jid, [{"qid": "q1", "status": "open", "blocking": True, "title": "Which unit?",
                                 "question": "Which unit should I test?"}])
    ownership.reconcile_job(store, jid, now=100.0)
    ok, why, extra = ownership.blocker_action_allowed(store, jid, "reopen", {"text": "x", "confidence": 0.95},
                                                      policy=tpol.Policy(), proposal_confidence=0.95)
    assert not ok and why.startswith("waiting for the owner: Which unit should I test?") and extra["owner_pending"]
    assert ownership.owner_decision_pending(store, jid).startswith("Which unit should I test?")


def test_the_lead_overrules_a_mistaken_blocker_and_acceptance_stays_human(steward_home):
    store = Store(steward_home)
    notes = Notes()
    jid = park_done(store, ["test-weakening detected: ['removed_test_function']"])
    t = _ready(store, jid)
    (tw,) = blockers.open_blockers(store, jid)
    lead, _ = control_lead(store, {"astra-tech-lead": [proposal({"type": "resolve_blocker", "params": {
        "blocker_id": tw.blocker_id, "reason": "renamed, not removed: tests/test_x.py::test_y_v2 still asserts it",
        "evidence": ["git show HEAD -- tests/test_x.py"], "confidence": 0.9}})]})
    _cycle(store, lead, t, notes)
    ownership.sweep(store, now=t + 40, job_lead=lead, notifier=notes)
    assert blockers.get(store, tw.blocker_id).resolution["actor"] == "tech_lead:astra-tech-lead"
    (acc,) = blockers.open_blockers(store, jid)
    assert acc.kind == "acceptance_decision" and acc.disposition == "confirmed_human"
    assert notes.kinds() == ["READY"] and store.get_job(jid).state == JobState.NEEDS_HUMAN
    ok, why, _ = ownership.blocker_action_allowed(store, jid, "resolve_blocker",
                                                  {"blocker_id": acc.blocker_id, "reason": "x", "confidence": 1.0},
                                                  policy=tpol.Policy(), proposal_confidence=1.0, lead_id="astra-tech-lead")
    assert not ok and "owner" in why


def test_wait_records_a_wake_time_and_buys_no_review_until_then(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    t = _ready(store, jid)
    first = blockers.open_blockers(store, jid)[0]
    lead, scripted = control_lead(store, {"astra-tech-lead": [proposal({"type": "wait", "params": {
        "blocker_id": first.blocker_id, "until": "time", "minutes": 30, "reason": "the unit reboots nightly"}})],
        "fable-tech-lead": []})
    _cycle(store, lead, t)
    assert blockers.get(store, first.blocker_id).diagnosis["wake_at"] == pytest.approx(t + 30 * 60, abs=2)
    for k in range(1, 20):
        ownership.sweep(store, now=t + 60 * k, job_lead=lead)
    assert len(scripted.calls) == 1  # no paid re-review before the wake time


def test_owner_stop_during_review_discards_the_late_reopen(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    lead, _ = control_lead(store, {"astra-tech-lead": [_reopen()]})
    t = _ready(store, jid)
    ownership.sweep(store, now=t, job_lead=lead)  # the review started; its answer is ready
    store.set_state(jid, JobState.STOPPED, what="operator stop", evidence={}, rule="operator_stop",
                    judgment=Judgment.human)
    ownership.sweep(store, now=t + 1, job_lead=lead)
    assert store.get_job(jid).state == JobState.STOPPED
    assert not [e for e in store.events(jid) if e.what in ("sent_back", "tech_lead_reopen")]
    (d,) = [e for e in store.events(jid) if e.what == "tech_lead_answer_discarded"]
    assert d.evidence["reason"] == "owner_stop" and blockers.open_blockers(store, jid) == []


def test_an_answer_made_on_older_evidence_is_discarded_and_not_counted(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    lead, scripted = control_lead(store, {"astra-tech-lead": [_reopen(), _reopen()]})
    t = _ready(store, jid)
    ownership.sweep(store, now=t, job_lead=lead)
    b = blockers.open_blockers(store, jid)[0]
    blockers.add_evidence(store, b.blocker_id, ["answer:q1"], actor=ownership.CONTROLLER, now=t + 0.5)
    ownership.sweep(store, now=t + 1, job_lead=lead)
    (d,) = [e for e in store.events(jid) if e.what == "tech_lead_answer_discarded"]
    assert d.evidence["reason"] == "evidence_changed" and store.get_job(jid).state == JobState.NEEDS_HUMAN
    assert {x.disposition for x in blockers.open_blockers(store, jid)} == {"candidate"}
    _cycle(store, lead, t + 40)
    assert [c[0] for c in scripted.calls] == ["astra-tech-lead", "astra-tech-lead"]  # not counted as a failure
    assert store.get_job(jid).state == JobState.RUNNING


def test_a_new_park_after_a_reopen_supersedes_the_old_episode(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    ownership.reconcile_job(store, jid, now=100.0)
    first = blockers.open_blockers(store, jid)
    sendback.send_back(store, jid, "try again", origin="cli")
    _repark(store, jid)
    ownership.reconcile_job(store, jid, now=200.0)
    assert all(not blockers.get(store, b.blocker_id).is_open for b in first)
    (now_open,) = blockers.open_blockers(store, jid)
    assert now_open.kind == "worker_parked_unfinished" and now_open.episode == 2
