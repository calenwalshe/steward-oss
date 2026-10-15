"""Plan 1 Task 6: legacy NEEDS_HUMAN -> coexisting blockers; deterministic clearing; projection and status."""
from __future__ import annotations

from steward import approvals, blockers, closeout, ownership, questions
from steward.models import JobState, Judgment
from steward.store import Store
from ownership_fixtures import CONTROLLER, Notes, contracted, park_done, park_rule, park_tracks


def _kinds(store, jid):
    return sorted((b.kind, b.disposition) for b in blockers.open_blockers(store, jid))


def test_tracks_shape_is_three_technical_candidates_and_no_acceptance(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    ownership.ensure_classified(store, jid, now=100.0)
    assert _kinds(store, jid) == [("baseline_failures_remaining", "candidate"), ("test_weakening", "candidate"),
                                  ("worker_parked_unfinished", "candidate")]
    assert ownership.owner_view(store, jid).status == "awaiting_technical_review"


def test_steward_done_marker_alone_does_not_make_technical_reasons_owner_only(steward_home):
    store = Store(steward_home)
    jid = park_done(store, ["2 baseline failure(s) still failing: ['a', 'b']", "test-weakening detected: ['x']"])
    ownership.ensure_classified(store, jid, now=100.0)
    assert _kinds(store, jid) == [("baseline_failures_remaining", "candidate"), ("test_weakening", "candidate")]


def test_acceptance_reasons_split_into_coexisting_blockers(steward_home):
    store = Store(steward_home)
    jid = park_done(store, ["hard constraint violation blocks closeout", "contract has no done_when criteria"])
    ownership.ensure_classified(store, jid, now=100.0)
    assert _kinds(store, jid) == [("contract_incomplete", "candidate"), ("hard_constraint", "confirmed_human")]
    view = ownership.owner_view(store, jid)
    assert view.status == "needs_owner" and "1 under technical review" in view.text


def test_pure_human_acceptance_is_confirmed_without_model_review(steward_home):
    store = Store(steward_home)
    jid = park_done(store)
    ownership.ensure_classified(store, jid, now=100.0)
    (b,) = blockers.open_blockers(store, jid)
    assert b.kind == "acceptance_decision" and b.disposition == "confirmed_human"
    assert b.human_request["decision"].startswith("Accept or send back")


def test_rule_sites_map_to_kinds(steward_home):
    store = Store(steward_home)
    cases = {
        ("verify_rounds_exhausted", "verify round budget exhausted"): "verification_failing",
        ("finish_line_bounces_exhausted", "finish line still not met after 3 nudges: x"): "finish_line_unmet",
        ("bg_verify_unverified", "background verification crashed; tests unverified"): "verification_unverified",
        ("worker_start_blocked", "worker startup blocked; brief not delivered"): "worker_start_blocked",
        ("fleet_required_task_stuck", "fleet task cannot complete"): "fleet_blocked",
        ("recovery_budget_exhausted", "recovery escalated to human"): "recovery_exhausted",
        ("something_new", "parked for a new reason"): "unclassified_hold",
    }
    for (rule, what), kind in cases.items():
        jid = park_rule(store, rule, what)
        ownership.ensure_classified(store, jid, now=100.0)
        assert [b.kind for b in blockers.open_blockers(store, jid)] == [kind], (rule, what)


def test_waiting_questions_become_owner_question_blockers(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    questions.save(store, jid, [{"qid": "q1", "status": "open", "blocking": True, "title": "Which unit?",
                                 "question": "Which unit should I test?"}])
    ownership.ensure_classified(store, jid, now=100.0)
    q = next(b for b in blockers.open_blockers(store, jid) if b.kind == "owner_question")
    assert q.disposition == "confirmed_human" and "all" in q.affected


def test_baseline_pending_waits_and_clears_on_reattribution(steward_home):
    store = Store(steward_home)
    jid = park_rule(store, "acceptance_unresolved", closeout.UNATTRIBUTED_WHAT, {"reasons": ["no baseline"]})
    ownership.ensure_classified(store, jid, now=100.0)
    (b,) = blockers.open_blockers(store, jid)
    assert (b.kind, b.disposition) == ("baseline_pending", "waiting_external")
    assert ownership.owner_view(store, jid).status == "waiting_external"
    store.append_event(jid, what=closeout.REATTRIBUTED_WHAT, evidence={}, rule="baseline_reattribution",
                       judgment=Judgment.code)
    res = ownership.reconcile_job(store, jid, now=200.0)
    assert b.blocker_id in res.cleared


def test_classification_is_cached_by_event_seq_and_idempotent(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    ownership.ensure_classified(store, jid, now=100.0)
    n = len(store.events(jid))
    ownership.ensure_classified(store, jid, now=101.0)
    ownership.reconcile_job(store, jid, now=102.0)
    assert len(blockers.for_job(store, jid)) == 3 and len(store.events(jid)) == n


def test_leaving_needs_human_resolves_owner_only_and_stop_resolves_everything(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    ownership.ensure_classified(store, jid, now=100.0)
    store.set_state(jid, JobState.STOPPED, what="operator stop", evidence={}, rule="operator_stop",
                    judgment=Judgment.human)
    ownership.reconcile_job(store, jid, now=200.0)
    assert blockers.open_blockers(store, jid) == []
    reasons = {b.resolution["reason"] for b in blockers.for_job(store, jid)}
    assert reasons == {"job STOPPED"}


def test_overruling_all_technical_blockers_leaves_an_acceptance_decision(steward_home):
    store = Store(steward_home)
    jid = park_done(store, ["test-weakening detected: ['x']"])
    ownership.ensure_classified(store, jid, now=100.0)
    (tw,) = blockers.open_blockers(store, jid)
    blockers.transition(store, tw.blocker_id, "resolved", actor=CONTROLLER, reason="false positive", now=110.0)
    ownership.reconcile_job(store, jid, now=120.0)
    assert _kinds(store, jid) == [("acceptance_decision", "confirmed_human")]


def test_status_events_are_emitted_only_on_change(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    ownership.reconcile_job(store, jid, now=100.0)
    ownership.reconcile_job(store, jid, now=200.0)
    assert len([e for e in store.events(jid) if e.what == ownership.STATUS_WHAT]) == 1


def test_confirmed_owner_blockers_notify_once_with_the_exact_decision(steward_home):
    store = Store(steward_home)
    notes = Notes()
    jid = park_done(store, ["hard constraint violation blocks closeout"])
    ownership.reconcile_job(store, jid, now=100.0, notifier=notes)
    ownership.reconcile_job(store, jid, now=200.0, notifier=notes)
    assert notes.kinds() == ["NEEDS_YOU"] and notes.events[0].revision.startswith("blocker:")
    pend = [a for a in approvals.list_approvals(store, jid, status="pending") if a.get("blocker_id")]
    assert len(pend) == 1


def test_one_broken_job_does_not_stop_reconciling_others(steward_home, monkeypatch):
    store = Store(steward_home)
    good = park_tracks(store)
    bad = park_tracks(store)
    real = ownership.classify_needs_human

    def boom(store_, job_id, events):
        if job_id == bad:
            raise RuntimeError("corrupt")
        return real(store_, job_id, events)
    monkeypatch.setattr(ownership, "classify_needs_human", boom)
    out = ownership.sweep(store, now=100.0)
    assert any(e["job"] == bad for e in out["errors"]) and blockers.open_blockers(store, good)
