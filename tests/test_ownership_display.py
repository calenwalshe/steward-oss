"""Plan 1 Task 7: legacy NEEDS_HUMAN display/notification compatibility (spec §5, §11 s18; G30, G33)."""
from __future__ import annotations

import pytest

from steward import alerts, approvals, evidence, jobview, notify, ownership, session, tabnames
from steward.models import JobState, Judgment
from steward.store import Store
from ownership_fixtures import Notes, contracted, park_done, park_rule, park_tracks


def _ev(store, jid):
    return jobview.event_dicts(store.events(jid))


def _legacy(kind: str, jid: str, revision="7") -> notify.PhoneEvent:
    return notify.PhoneEvent(kind=kind, job_id=jid, title="Steward: x", body="finish line still not met",
                             deep_link=None, revision=revision)


def test_review_statuses_are_shared():
    assert jobview.OWNERSHIP_REVIEW == ownership.REVIEW_STATUSES


def test_a_job_under_review_shows_steward_reviewing_not_waiting_on_you(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    ownership.reconcile_job(store, jid, now=100.0)
    events, apps = _ev(store, jid), approvals.list_approvals(store, jid)
    assert jobview.reason(apps, "NEEDS_HUMAN", events) is None
    kind, detail = jobview.liveness("NEEDS_HUMAN", events)
    assert kind == "live" and detail.startswith("steward reviewing")
    assert tabnames.status_glyph("NEEDS_HUMAN", kind, None) == tabnames.RUNNING


def test_a_confirmed_owner_decision_shows_the_exact_decision(steward_home):
    store = Store(steward_home)
    jid = park_done(store, ["hard constraint violation blocks closeout"])
    ownership.reconcile_job(store, jid, now=100.0)
    why = jobview.reason(approvals.list_approvals(store, jid), "NEEDS_HUMAN", _ev(store, jid))
    assert "Decide how to proceed: hard constraint violation" in why  # via its linked approval or the status text
    assert jobview.liveness("NEEDS_HUMAN", _ev(store, jid))[0] == "waiting"


def test_a_pure_acceptance_keeps_the_legacy_accept_text(steward_home):
    store = Store(steward_home)
    jid = park_done(store)
    ownership.reconcile_job(store, jid, now=100.0)
    assert jobview.reason([], "NEEDS_HUMAN", _ev(store, jid)).startswith("accept or reject?")


def test_unavailable_review_is_quiet_with_the_retry_time(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    ownership.reconcile_job(store, jid, now=100.0)
    ids = [b.blocker_id for b in ownership.blockers.open_blockers(store, jid)]
    ownership.blockers.set_review_state(store, ids[0], "unavailable", now=110.0, next_review_epoch=4000.0)
    ownership.reconcile_job(store, jid, now=120.0)
    kind, detail = jobview.liveness("NEEDS_HUMAN", _ev(store, jid))
    assert kind == "quiet" and detail.startswith("technical review unavailable · retry ")


def test_an_annotated_recovery_approval_is_not_an_owner_ask_while_reviewed(steward_home):
    store = Store(steward_home)
    jid = park_rule(store, "recovery_budget_exhausted", "recovery escalated to human",
                    {"rule": "recovery_budget_exhausted", "detail": "budget spent"})
    rec = approvals.open_approval(store, jid, "budget spent")
    approvals.annotate(store, jid, rec["id"], recovery_rule=approvals.RECOVERY_RULE)
    ownership.reconcile_job(store, jid, now=100.0)
    assert approvals.get_approval(store, jid, rec["id"])["awaiting_review"] is True
    assert jobview.pending_ask(approvals.list_approvals(store, jid)) is None
    assert evidence.decision(store, jid) is None
    assert session.wait_state(store, jid, "decision") is None
    assert approvals.get_approval(store, jid, rec["id"])["status"] == "pending"  # still openable by the owner


def test_alerts_do_not_page_while_steward_reviews(steward_home):
    store = Store(steward_home)
    tracks = park_tracks(store)
    hard = park_done(store, ["hard constraint violation blocks closeout"])
    assert [k for k, _ in alerts.reasons(store, tracks)] == []
    assert [k for k, _ in alerts.reasons(store, hard)] == ["needs_you"]


def test_legacy_park_notice_is_held_once_while_review_is_pending(steward_home):
    store = Store(steward_home)
    notes = Notes()
    jid = park_rule(store, "finish_line_bounces_exhausted", "finish line still not met after 3 nudges: x")
    assert notify.emit(store, notes, _legacy("NEEDS_YOU", jid)) is False
    assert notify.emit(store, notes, _legacy("NEEDS_YOU", jid)) is False
    assert notes.events == []
    held = [e for e in store.events(jid) if e.what == "owner_notice_held"]
    assert len(held) == 1 and held[0].evidence["why"] == "awaiting_technical_review"


def test_an_acceptance_park_sends_one_precise_ready(steward_home):
    store = Store(steward_home)
    notes = Notes()
    jid = park_done(store)
    notify.emit(store, notes, _legacy("READY", jid))
    notify.emit(store, notes, _legacy("READY", jid, revision="8"))
    assert notes.kinds() == ["READY"] and str(notes.events[0].revision).startswith("blocker:")
    assert "Accept or send back" in notes.events[0].body


def test_running_jobs_and_question_notices_are_untouched(steward_home):
    store = Store(steward_home)
    notes = Notes()
    running = contracted(store)
    store.set_state(running, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    assert notify.emit(store, notes, _legacy("NEEDS_YOU", running)) is True
    parked = park_tracks(store)
    assert notify.emit_questions(store, notes, parked, waiting=1, starred=0, goal="g", qhash="h1", link=None)
    assert notes.kinds() == ["NEEDS_YOU", "NEEDS_YOU"]


def test_the_gate_fails_open_when_ownership_breaks(steward_home, monkeypatch):
    store = Store(steward_home)
    notes = Notes()
    jid = park_tracks(store)

    def boom(*a, **k):
        raise RuntimeError("corrupt")
    monkeypatch.setattr(ownership, "ensure_classified", boom)
    assert notify.emit(store, notes, _legacy("NEEDS_YOU", jid)) is True


def test_result_shows_what_steward_is_doing(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    ownership.reconcile_job(store, jid, now=100.0)
    res = session.build_result(store, jid)
    assert res["ownership"]["status"] == "awaiting_technical_review"
    assert "awaiting technical review" in session.render_result(res)


def test_result_status_follows_a_blocker_resolved_between_reconciles(steward_home):
    from steward import blockers
    store = Store(steward_home)
    jid = park_tracks(store)
    ownership.reconcile_job(store, jid, now=100.0)
    assert "awaiting technical review" in session.render_result(session.build_result(store, jid))
    open_ = blockers.open_blockers(store, jid)
    assert len(open_) > 1
    # resolved with no reconcile afterwards: the stored projection keeps the old text, result must not
    for i, b in enumerate(open_):
        blockers.transition(store, b.blocker_id, "resolved", actor=ownership.CONTROLLER, reason="lead overruled",
                            now=200.0 + i)
    assert "awaiting technical review" in ownership.projection(store, jid)["text"]
    out = session.render_result(session.build_result(store, jid))
    assert "awaiting technical review" not in out


@pytest.mark.legacy_ownership_off
def test_kill_switch_restores_the_legacy_notice_and_text(steward_home):
    store = Store(steward_home)
    notes = Notes()
    jid = park_tracks(store)
    assert notify.emit(store, notes, _legacy("NEEDS_YOU", jid)) is True
    assert jobview.liveness("NEEDS_HUMAN", _ev(store, jid))[1].startswith("waiting on you")
