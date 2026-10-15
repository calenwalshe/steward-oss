"""A12 (Plan 1 Task 13; spec §4, §11 s9/s14/s19): restart recovery and concurrent controllers. Every intent kind in use
(review launch, send-back/reopen, worker relaunch, owner notice) is reconciled against receipts and actual state after
a crash; an unknown fate stays unknown (never a second writer); one broken job or intent never blocks the others;
legacy jobs are reconciled without losing explicit grants, stops, hard caps, approvals or pending delivery; skill-guided
and direct work are judged against the same required-outcome evidence."""
from __future__ import annotations

import time

import pytest

from steward import approvals, blockers, intents, ownership, questions, sendback
from steward import questions_delivery as qd
from steward.models import JobState, Judgment
from steward.store import Store
from ownership_fixtures import FakeLead, Notes, contracted, control_lead, park_done, park_rule, park_tracks, proposal

A, B = "ctl-a:1", "ctl-b:2"


def _reopen(text="restore the deleted test, then rerun verify"):
    return proposal({"type": "reopen", "params": {"text": text, "evidence": "x", "confidence": 0.9}})


def _crashed_review(store, jid, t):
    """Controller A claimed a review through the real sweep and died holding it (blockers reviewing, no outcome)."""
    ownership.reconcile_job(store, jid, now=t)
    lead_a = FakeLead()
    out = ownership.sweep(store, now=t + 31, job_lead=lead_a, owner=A)
    assert [r["via"] for r in out["reviews"]] == ["control_lead"]
    ids = [b.blocker_id for b in blockers.open_blockers(store, jid)]
    ownership.mark_reviewing(store, ids, now=t + 31)
    (it,) = intents.for_job(store, jid, ownership.REVIEW_KIND)
    assert it.owner == A and it.state == "claimed" and it.params["after_seq"] > 0
    return it, ids


# -- review launch ------------------------------------------------------------------------------------------------


def test_continuous_ownership_recovery_acceptance_review_whose_lead_acted_before_the_crash_is_recovered_not_rerun(
        steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    t = time.time()
    it, ids = _crashed_review(store, jid, t)
    store.append_event(jid, what="tech_lead_action", evidence={"action": "wait", "outcome": "executed"},
                       rule="tech_lead_executed", judgment=Judgment.model_informed_code_authorized)
    out = ownership.recover(store, now=t + 1000, owner=B)
    assert [(s["kind"], s["state"]) for s in out["intents"]["settled"]] == [("tech_review", "done")]
    assert intents.get(store, it.key).receipt["reconciled"] is True
    assert all(blockers.get(store, bid).disposition == "candidate" and blockers.get(store, bid).review_state == "done"
               for bid in ids)
    lead_b = FakeLead()
    out = ownership.sweep(store, now=t + 1001, job_lead=lead_b, owner=B)
    assert lead_b.calls == [] and out["reviews"] == []                           # unchanged evidence: no repeat review


def test_continuous_ownership_recovery_acceptance_review_lost_before_any_answer_is_taken_over_exactly_once(
        steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    t = time.time()
    it, ids = _crashed_review(store, jid, t)
    out = ownership.recover(store, now=t + 100, owner=B)                          # lease (360 s) still live
    assert out["intents"]["settled"] == [] and intents.get(store, it.key).state == "claimed"
    late = t + 31 + ownership.REVIEW_LEASE_S + 5
    out = ownership.recover(store, now=late, owner=B)
    assert [(s["kind"], s["state"]) for s in out["intents"]["settled"]] == [("tech_review", "released")]
    assert all(blockers.get(store, bid).disposition == "candidate" for bid in ids)  # back in the queue, no diagnosis
    lead_b, lead_c = FakeLead(), FakeLead()
    ownership.sweep(store, now=late + 40, job_lead=lead_b, owner=B)
    ownership.sweep(store, now=late + 41, job_lead=lead_c, owner="ctl-c:3")
    assert len(lead_b.calls) == 1 and lead_c.calls == []                          # one takeover, never two reviews


# -- send-back / reopen -------------------------------------------------------------------------------------------


def _claim_reopen(store, jid, owner, now):
    ids = sorted(b.blocker_id for b in blockers.open_blockers(store, jid))
    key = intents.intent_key(jid, "reopen", ownership.park_seq(store, jid), *ids, 0)
    it = intents.claim(store, key=key, job_id=jid, kind="reopen", lease_s=60.0, now=now, owner=owner,
                       params={"after_seq": store.events(jid)[-1].seq, "lead": "astra-tech-lead", "blocker_ids": ids})
    assert it is not None
    return it


def test_continuous_ownership_recovery_acceptance_send_back_recorded_before_the_crash_is_done_not_repeated(
        steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    ownership.reconcile_job(store, jid, now=100.0)
    it = _claim_reopen(store, jid, A, 100.0)
    sendback.send_back(store, jid, "continue", origin="tech_lead:astra-tech-lead",
                       judgment=Judgment.model_informed_code_authorized)           # A's effect landed, receipt did not
    out = ownership.recover(store, now=500.0, owner=B)
    assert [(s["kind"], s["state"]) for s in out["intents"]["settled"]] == [("reopen", "done")]
    assert intents.get(store, it.key).receipt["steering_rev"] is not None
    assert len([e for e in store.events(jid) if e.what == "sent_back"]) == 1
    assert store.get_job(jid).state == JobState.RUNNING


def test_continuous_ownership_recovery_acceptance_send_back_never_recorded_is_abandoned_and_a_fresh_review_resumes(
        steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    t = time.time()
    ownership.reconcile_job(store, jid, now=t)
    it = _claim_reopen(store, jid, A, t)                                           # A died before sending anything
    out = ownership.recover(store, now=t + 500, owner=B)
    assert [(s["kind"], s["state"]) for s in out["intents"]["settled"]] == [("reopen", "abandoned")]
    assert store.get_job(jid).state == JobState.NEEDS_HUMAN
    lead, scripted = control_lead(store, {"astra-tech-lead": [_reopen()]})
    ownership.sweep(store, now=t + 501, job_lead=lead, owner=B)
    ownership.sweep(store, now=t + 502, job_lead=lead, owner=B)
    assert store.get_job(jid).state == JobState.RUNNING                            # the abandoned key did not wedge it
    assert len([e for e in store.events(jid) if e.what == "sent_back"]) == 1


def test_continuous_ownership_recovery_acceptance_a_half_recorded_send_back_stays_unknown(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    ownership.reconcile_job(store, jid, now=100.0)
    it = _claim_reopen(store, jid, A, 100.0)
    store.append_event(jid, what="sent_back", evidence={"by": "tech_lead:astra-tech-lead", "steering_rev": 2},
                       rule="tech_lead_send_back", judgment=Judgment.model_informed_code_authorized)
    out = ownership.recover(store, now=500.0, owner=B)                              # event written, state not changed
    assert out["intents"]["settled"] == []
    assert [(u["kind"], u["key"]) for u in out["intents"]["uncertain"]] == [("reopen", it.key)]
    assert intents.get(store, it.key).state == "uncertain"
    assert store.get_job(jid).state == JobState.NEEDS_HUMAN                         # nothing acted on a guess


# -- worker relaunch ----------------------------------------------------------------------------------------------


def test_continuous_ownership_recovery_acceptance_a_launch_that_died_midway_is_never_repeated(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    sendback.send_back(store, jid, "try again", origin="tech_lead:astra-tech-lead",
                       judgment=Judgment.model_informed_code_authorized)
    calls = []
    t = time.time()

    def dies(job_id):
        calls.append("a")
        raise KeyboardInterrupt("controller killed mid-launch")

    ownership.sweep(store, now=t, launch_fn=dies, owner=A)
    with pytest.raises(KeyboardInterrupt):
        ownership.sweep(store, now=t + 121, launch_fn=dies, owner=A)
    (it,) = intents.for_job(store, jid, ownership.LAUNCH_KIND)
    assert it.state == "claimed" and it.owner == A

    def launch_b(job_id):
        calls.append("b")
        return {"accepted": True, "tab_id": "w:1"}
    late = t + 121 + ownership.LAUNCH_LEASE_S + 5
    out = ownership.sweep(store, now=late, launch_fn=launch_b, owner=B)
    assert out["recovered"]["launches_held"] == [jid] and calls == ["a"]            # a runner may exist: no second launch
    assert intents.get(store, it.key).state == "uncertain"
    ownership.sweep(store, now=late + 1000, launch_fn=launch_b, owner=B)
    assert calls == ["a"]
    (b,) = blockers.open_blockers(store, jid)
    assert b.kind == "worker_start_blocked" and "never started a worker" in b.claim  # reviewable, not silent
    assert len([x for x in intents.for_job(store, jid, ownership.LAUNCH_KIND)]) == 1


def test_continuous_ownership_recovery_acceptance_a_launch_whose_worker_started_is_settled_done(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    sendback.send_back(store, jid, "try again", origin="cli")
    t = time.time()
    ownership.sweep(store, now=t, launch_fn=lambda j: {"accepted": False, "detail": "x"}, owner=A)

    def dies(job_id):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        ownership.sweep(store, now=t + 121, launch_fn=dies, owner=A)
    store.append_event(jid, what="worker attempt started", evidence={"attempt_no": 2}, rule="worker_dispatch",
                       judgment=Judgment.code)
    out = ownership.recover(store, now=t + 1000, owner=B)
    assert ("launch_worker", "done") in [(s["kind"], s["state"]) for s in out["intents"]["settled"]]
    assert blockers.open_blockers(store, jid) == []


# -- owner notice -------------------------------------------------------------------------------------------------


def test_continuous_ownership_recovery_acceptance_a_notice_of_unknown_fate_is_reported_not_resent(steward_home):
    store = Store(steward_home)
    notes = Notes()
    jid = park_done(store, ["hard constraint violation blocks closeout"])
    ownership.ensure_classified(store, jid, now=100.0)
    (b,) = blockers.open_blockers(store, jid)
    key = intents.intent_key(jid, "owner_notice", b.blocker_id)
    intents.claim(store, key=key, job_id=jid, kind="owner_notice", lease_s=10, now=100.0, owner=A,
                  blocker_id=b.blocker_id)
    out = ownership.recover(store, now=500.0, owner=B, notifier=notes)
    assert notes.events == [] and notes.texts == []
    assert [(u["kind"], u["key"]) for u in out["intents"]["uncertain"]] == [("owner_notice", key)]
    again = ownership.recover(store, now=600.0, owner=B, notifier=notes)
    assert notes.events == [] and len(again["intents"]["uncertain"]) == 1
    assert ownership.owner_view(store, jid).status == "needs_owner"                  # still shown to the owner


# -- isolation ------------------------------------------------------------------------------------------------------


def test_continuous_ownership_recovery_acceptance_one_broken_intent_or_job_never_blocks_the_rest(
        steward_home, monkeypatch):
    store = Store(steward_home)
    good, bad = park_tracks(store), park_tracks(store)
    ownership.reconcile_job(store, good, now=100.0)
    ownership.reconcile_job(store, bad, now=100.0)
    it_good = _claim_reopen(store, good, A, 100.0)
    key = intents.intent_key(bad, "weird")
    intents.claim(store, key=key, job_id=bad, kind="weird", lease_s=10, now=100.0, owner=A)

    def broken(st, i):
        raise RuntimeError("corrupt receipt")
    intents.register_checker("weird", broken)
    real = ownership.reconcile_job

    def boom(store_, job_id, **kw):
        if job_id == bad:
            raise RuntimeError("corrupt job")
        return real(store_, job_id, **kw)
    monkeypatch.setattr(ownership, "reconcile_job", boom)
    out = ownership.recover(store, now=500.0, owner=B)
    assert intents.get(store, it_good.key).state == "abandoned"                       # the healthy intent settled
    assert intents.get(store, key).state == "uncertain"                                # the broken one stayed unknown
    assert [j["job"] for j in out["jobs"]] == [good] and [e["job"] for e in out["errors"]] == [bad]


def test_continuous_ownership_recovery_acceptance_recovery_is_idempotent_and_throttled_in_the_sweep(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    ownership.reconcile_job(store, jid, now=100.0)
    _claim_reopen(store, jid, A, 100.0)
    first = ownership.recover(store, now=500.0, owner=B)
    second = ownership.recover(store, now=501.0, owner=B)
    assert len(first["intents"]["settled"]) == 1 and second["intents"]["settled"] == []
    store.kv_set("_ownership", "recovered_at", 1000.0)
    assert ownership.sweep(store, now=1010.0)["recovered"] == {"settled": [], "uncertain": [], "launches_held": []}


def test_continuous_ownership_recovery_acceptance_a_live_controllers_slow_review_is_not_reconciled_away(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    t = time.time()
    ownership.reconcile_job(store, jid, now=t)
    ownership.sweep(store, now=t + 31, job_lead=FakeLead(), owner=A)
    out = ownership.recover(store, now=t + 31 + ownership.REVIEW_LEASE_S + 10, owner=A)   # A itself is alive
    assert out["intents"]["settled"] == [] and out["intents"]["uncertain"] == []


# -- s14: legacy jobs reconciled conservatively -------------------------------------------------------------------


def test_continuous_ownership_recovery_acceptance_s14_legacy_jobs_keep_grants_stops_caps_approvals_and_delivery(
        steward_home):
    store = Store(steward_home)
    stopped = park_tracks(store)
    store.set_state(stopped, JobState.STOPPED, what="operator stop", evidence={}, rule="operator_stop",
                    judgment=Judgment.human)
    capped = park_done(store, ["hard constraint violation blocks closeout"])
    granted = park_tracks(store)
    caps = {"research": True, "keys": ["EXA_API_KEY"], "cap_usd": 3.0}
    store.kv_set(granted, "capabilities", caps)
    approval = approvals.open_approval(store, granted, "may I run the migration?", kind="permission")
    delivering = park_tracks(store)
    questions.save(store, delivering, [{"qid": "q1", "status": "answered", "blocking": False, "title": "unit",
                                        "question": "Which unit?", "answer": {"option": "other", "other": "B", "by": "sam",
                                                                               "at": "2026-10-05T10:00:00"}}])
    pending_before = [r["qid"] for r in qd.pending(store, delivering)]
    states = {j: store.get_job(j).state for j in (stopped, capped, granted, delivering)}
    out = ownership.recover(store, now=time.time(), owner=B)
    assert out["errors"] == []
    assert {j: store.get_job(j).state for j in states} == states                       # no lifecycle changes
    assert blockers.open_blockers(store, stopped) == [] and store.get_job(stopped).state == JobState.STOPPED
    assert store.kv_get(granted, "capabilities") == caps                                # grants and hard cap intact
    assert [a["id"] for a in approvals.list_approvals(store, granted, status="pending")] == [approval["id"]]
    assert [r["qid"] for r in qd.pending(store, delivering)] == pending_before == ["q1"]  # delivery still pending
    (hard,) = blockers.open_blockers(store, capped)
    assert hard.disposition == "confirmed_human" and ownership.owner_view(store, capped).status == "needs_owner"
    lead = FakeLead()
    ownership.sweep(store, now=time.time() + 100, job_lead=lead, owner=B)
    assert capped not in [c["job"] for c in lead.calls]                                  # the owner's decision isn't reviewed away
    assert stopped not in [c["job"] for c in lead.calls]


# -- s19: skill-guided vs direct work -----------------------------------------------------------------------------

from steward import outcome_scope  # noqa: E402
from steward.closeout import closeout  # noqa: E402
from steward.steering import Steering  # noqa: E402
from steward.verify import Verification  # noqa: E402
from test_closeout import _finished_job  # noqa: E402
from ruling_support import green  # noqa: E402

_GOAL = "Implement the feature. REQUIRED OUTCOMES: error turns recover; recovery reaches the worker. FINISH LINE: pytest passes."


def _skill_marked(store, jid):
    store.append_event(jid, what="worker output", evidence={"text": "using skill superpowers:test-driven-development"},
                       rule="worker_note", judgment=Judgment.code)


def test_continuous_ownership_recovery_acceptance_s19_skill_guided_and_direct_work_meet_the_same_evidence_bar(
        steward_home):
    store = Store(steward_home)
    required = ("error turns recover (live)", "recovery reaches the worker (live)")
    direct_job, _ = _finished_job(store, goal=_GOAL, done_when=required)
    guided_job, _ = _finished_job(store, goal=_GOAL, done_when=required)
    direct, guided = direct_job.job_id, guided_job.job_id
    _skill_marked(store, guided)
    states, texts = {}, {}
    for jid in (direct, guided):
        contract = __import__("steward.contract", fromlist=["load_contract"]).load_contract(store, jid)
        states[jid] = closeout(store, jid, green("sha1"), [], contract)
        texts[jid] = outcome_scope.acceptance_reasons(store, jid, contract, commit_sha="sha1")
    assert states[direct] == states[guided] == JobState.NEEDS_HUMAN                     # skill use does not prove outcomes
    assert texts[direct] == texts[guided]                                                # identical required evidence
    assert len(outcome_scope.unproven(store, direct, contract, commit_sha="sha1")) == 2


def test_continuous_ownership_recovery_acceptance_s19_a_named_skill_never_invoked_is_not_a_blocker(steward_home):
    store = Store(steward_home)
    job, contract = _finished_job(store, goal="Use superpowers:brainstorming to implement the parser. "
                                                "FINISH LINE: pytest passes.")
    jid = job.job_id
    # nothing blocks acceptance (no blocker, no unproven outcome): the job waits for its one completion ruling
    assert closeout(store, jid, green("sha1"), [], contract) == JobState.CHECKS_PASSED
    assert blockers.open_blockers(store, jid) == []
    assert ownership.owner_view(store, jid).status == "none"


def test_continuous_ownership_recovery_acceptance_s19_internal_workflow_stop_is_reviewed_and_explicit_owner_stop_binds(
        steward_home):
    store = Store(steward_home)
    internal = park_rule(store, "verify_rounds_exhausted", "recovery escalated to human",
                         evidence={"rounds": 3})
    store.append_event(internal, what="worker output", evidence={"text": "skill workflow: waiting for design approval "
                                                                         "before writing code"},
                       rule="worker_note", judgment=Judgment.code)
    owner_stopped = park_tracks(store)
    store.set_state(owner_stopped, JobState.STOPPED, what="operator stop", evidence={}, rule="operator_stop",
                    judgment=Judgment.human)
    t = time.time()
    ownership.reconcile_job(store, internal, now=t)
    kinds = {b.kind for b in blockers.open_blockers(store, internal)}
    assert kinds == {"recovery_exhausted"} and not any(blockers.spec(k).owner_only for k in kinds)
    notes = Notes()
    lead, scripted = control_lead(store, {"astra-tech-lead": [_reopen("The design is approved by the contract; "
                                                                      "proceed to implementation.")]}, notifier=notes)
    for dt in (31, 32, 33):
        ownership.sweep(store, now=t + dt, job_lead=lead, notifier=notes, owner=B)
    assert len(scripted.calls) == 1                                                       # reviewed by the tech lead
    assert store.get_job(internal).state == JobState.RUNNING and notes.events == []        # resumed, owner not asked
    assert store.get_job(owner_stopped).state == JobState.STOPPED                           # the explicit stop is kept
    assert blockers.open_blockers(store, owner_stopped) == []
    assert not [e for e in store.events(owner_stopped) if e.what == "sent_back"]
