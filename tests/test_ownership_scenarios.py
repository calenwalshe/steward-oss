"""Spec §11 scenarios 1, 4, 5, 8, 9, 14, 15, 17, 18, 19 through the real legacy guards (Plan 1). Each test drives
real Store transitions, real closeout/runner/tech-lead/control-pane code and fake models/workers/notifiers, and
asserts owner-visible status and notifications, not only internal events."""
from __future__ import annotations

import json

from steward import approvals, blockers, closeout, jobview, ownership, questions
from steward.models import JobState, Judgment
from steward.store import Store
from ownership_fixtures import Notes, contracted, park_done, park_rule, park_tracks


def test_s4_green_verification_clears_stale_blocker_owner_decision_stays(steward_home):
    store = Store(steward_home)
    notes = Notes()
    jid = park_rule(store, "bg_verify_unverified", "background verification crashed; tests unverified",
                    {"reasons": ["tests unverified: crashed"], "commit_sha": "sha-old"})
    questions.save(store, jid, [{"qid": "q1", "status": "open", "blocking": True, "title": "Which DB?",
                                 "question": "Which database should the migration target?"}])
    ownership.reconcile_job(store, jid, now=100.0, head="sha-new", notifier=notes)
    kinds = {b.kind: b for b in blockers.open_blockers(store, jid)}
    assert set(kinds) == {"verification_unverified", "owner_question"}
    store.add_check(jid, commit_sha="sha-new", command="pytest -q", exit_code=0, results={}, label="verify")
    res = ownership.reconcile_job(store, jid, now=200.0, head="sha-new", notifier=notes)
    assert kinds["verification_unverified"].blocker_id in res.cleared
    assert [b.kind for b in blockers.open_blockers(store, jid)] == ["owner_question"]
    view = ownership.owner_view(store, jid)
    assert view.status == "needs_owner" and "database" in view.text.lower()
    assert "NEEDS_YOU" not in notes.kinds()  # the question queue owns question notices; nothing about verification
def test_s15_all_reviewers_unavailable(steward_home):
    """§11 s15: one visible operational alert, no false owner-action request, independent work continues, pending
    review survives restart, and service recovery resumes it exactly once with a recovery notice."""
    import time

    from steward import review_service
    from ownership_fixtures import Clock, control_lead, proposal

    store = Store(steward_home)
    notes = Notes()
    clock = Clock(time.time())
    stuck = park_tracks(store)
    owner_job = park_done(store)  # independent work: a genuine acceptance decision elsewhere
    outage = review_service.ReviewOutage(store, notes.send_text, clock=clock)
    lead, _ = control_lead(store, {"astra-tech-lead": [TimeoutError("t")], "fable-tech-lead": [TimeoutError("t")]},
                           outage=outage, notifier=notes, clock=clock)
    ownership.reconcile_job(store, stuck, now=clock.t)
    for step in (31, 1, 1, 30):  # both leads time out: down; the next retry is 60 s after the failure
        ownership.sweep(store, now=clock.advance(step), job_lead=lead, notifier=notes, outage=outage)
    assert len(notes.texts) == 1                                           # one operational alert
    assert [e.job_id for e in notes.events] == [owner_job] and notes.kinds() == ["READY"]  # only the real decision
    assert ownership.owner_view(store, stuck).status == "technical_review_unavailable"
    outage2 = review_service.ReviewOutage(store, notes.send_text, clock=clock)
    lead2, scripted2 = control_lead(store, {"astra-tech-lead": [proposal({"type": "reopen", "params": {
        "text": "power-cycle check first", "evidence": "x", "confidence": 0.9}})]},
        outage=outage2, notifier=notes, clock=clock)
    for step in (60, 1, 1, 60, 60):  # the restarted controller probes once at the retry time, then recovers
        ownership.sweep(store, now=clock.advance(step), job_lead=lead2, notifier=notes, outage=outage2)
    assert len(scripted2.calls) == 1 and store.get_job(stuck).state == JobState.RUNNING
    assert len(notes.texts) == 2 and "back" in notes.texts[1]
    assert notes.kinds() == ["READY"]                                      # still no owner request for `stuck`


def test_s1_parked_job_reviewed_once_without_runner_continuous_ownership_recovery_acceptance(steward_home):
    """A parked job remains control-owned after its runner exits; repeated ticks reopen it once."""
    import time

    from ownership_fixtures import control_lead, proposal

    store = Store(steward_home)
    jid = park_tracks(store)
    reopen = proposal({"type": "reopen", "params": {"text": "resume the worker after checking current failures",
                                                        "evidence": "event:latest", "confidence": 0.9}})
    lead, scripted = control_lead(store, {"astra-tech-lead": [reopen]})
    now = time.time()
    ownership.reconcile_job(store, jid, now=now)
    for offset in (31, 32, 33, 34):
        ownership.sweep(store, now=now + offset, job_lead=lead)

    assert store.get_job(jid).state == JobState.RUNNING
    assert len([e for e in store.events(jid) if e.what == "sent_back"]) == 1
    assert len(scripted.calls) == 1
def test_s5_answers_resume_once_unanswered_and_stop_bind(steward_home):
    """§11 s5: answer delivery resumes eligible work once; unanswered questions and an owner stop stay binding."""
    import time

    from ownership_fixtures import control_lead, proposal

    store = Store(steward_home)
    answered = park_tracks(store)
    unanswered = park_tracks(store)
    stopped = park_tracks(store)
    q = {"qid": "q1", "status": "open", "blocking": True, "title": "Which unit?", "question": "Which unit should I test?"}
    for jid in (answered, unanswered, stopped):
        questions.save(store, jid, [dict(q)])
    reopen = proposal({"type": "reopen", "params": {"text": "test unit B as answered", "evidence": "answer:q1",
                                                    "confidence": 0.9}})
    lead, scripted = control_lead(store, {"astra-tech-lead": [reopen]})
    t = time.time()
    for dt in (0, 40, 41):
        ownership.sweep(store, now=t + dt, job_lead=lead)
    assert scripted.calls == []                         # a blocking question: no paid review while it is unanswered
    for jid in (answered, stopped):
        recs = questions.load(store, jid)
        recs[0].update(status="answered", answer={"option": "other", "other": "unit B", "at": "2026-10-05T10:00:00"})
        questions.save(store, jid, recs)
    store.set_state(stopped, JobState.STOPPED, what="operator stop", evidence={}, rule="operator_stop",
                    judgment=Judgment.human)
    for dt in (50, 100, 101, 102, 200, 300):
        ownership.sweep(store, now=t + dt, job_lead=lead)
    assert len(scripted.calls) == 1 and "unit B" in scripted.calls[0][1]  # the answer reached the review packet
    assert store.get_job(answered).state == JobState.RUNNING
    assert len([e for e in store.events(answered) if e.what == "sent_back"]) == 1
    assert store.get_job(unanswered).state == JobState.NEEDS_HUMAN
    assert ownership.owner_view(store, unanswered).status == "needs_owner"
    assert store.get_job(stopped).state == JobState.STOPPED and blockers.open_blockers(store, stopped) == []
