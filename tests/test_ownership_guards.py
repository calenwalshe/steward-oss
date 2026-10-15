"""Plan 1 Task 11: the legacy guards' real paths (G1, G2, G23-G27, G31, G34) and rework relaunch."""
from __future__ import annotations

import time

from steward import approvals, blockers, intents, jev_watch, ownership, sendback
from steward import flow_actions as fa
from steward import fleet_runtime as rt
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.models import JobState, Judgment
from steward.runner import Runner
from steward.store import Store
from steward.worker import FakeWorker
from ownership_fixtures import Notes, contracted, control_lead, git_ok, park_rule, park_tracks, proposal
from test_flow_actions import _episode, _investigation


def _reopen(text="restore the deleted test, then rerun verify"):
    return proposal({"type": "reopen", "params": {"text": text, "evidence": "x", "confidence": 0.9}})


def _runner(store, notes):
    return Runner(store, lambda ref: FakeWorker(script=[]), git_ok(),
                  FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="g", hypothesis="h")),
                  clock=time.time, notifier=notes)


def _park_site_is_reviewed_then_resumed(steward_home, rule, what, kind):
    store = Store(steward_home)
    notes = Notes()
    jid = park_rule(store, rule, what)
    _runner(store, notes)._notify_closeout_result(jid, JobState.NEEDS_HUMAN)  # the real legacy notice site
    assert notes.events == []                                                  # held: review comes first
    (b,) = blockers.open_blockers(store, jid)
    assert (b.kind, b.disposition) == (kind, "candidate")
    pend = approvals.list_approvals(store, jid, status="pending")
    assert pend and all(a.get("awaiting_review") for a in pend)               # the legacy approval is kept
    lead, _ = control_lead(store, {"astra-tech-lead": [_reopen()]}, notifier=notes)
    t = time.time() + 31
    ownership.sweep(store, now=t, job_lead=lead, notifier=notes)
    ownership.sweep(store, now=t + 1, job_lead=lead, notifier=notes)
    assert store.get_job(jid).state == JobState.RUNNING and notes.events == []
    assert store.kv_get(jid, sendback.REWORK_KEY)["by"] == "tech_lead:astra-tech-lead"


def test_finish_line_exhaustion_is_reviewed_before_owner(steward_home):
    _park_site_is_reviewed_then_resumed(steward_home, "finish_line_bounces_exhausted",
                                        "finish line still not met after 3 nudges: verify --phase 2", "finish_line_unmet")


def test_verify_rounds_exhausted_is_a_reviewable_blocker(steward_home):
    _park_site_is_reviewed_then_resumed(steward_home, "verify_rounds_exhausted", "verify round budget exhausted",
                                        "verification_failing")


def test_fleet_park_is_a_candidate_blocker(steward_home):
    _park_site_is_reviewed_then_resumed(steward_home, "fleet_required_task_stuck", "fleet task cannot complete",
                                        "fleet_blocked")


def test_worker_start_blocked_clears_when_a_worker_starts(steward_home):
    store = Store(steward_home)
    jid = park_rule(store, "worker_start_blocked", "worker startup blocked; brief not delivered")
    ownership.reconcile_job(store, jid, now=100.0)
    (b,) = blockers.open_blockers(store, jid)
    sendback.send_back(store, jid, "start again", origin="cli")
    store.append_event(jid, what="worker attempt started", evidence={"attempt_no": 2}, rule="worker_dispatch",
                       judgment=Judgment.code)
    res = ownership.reconcile_job(store, jid, now=200.0)
    assert b.blocker_id in res.cleared and blockers.open_blockers(store, jid) == []


def test_recovery_exhaustion_is_reviewed_and_owner_resume_still_works(steward_home):
    from test_false_hung_call import _kinds, _needs_human, _setup, _sig

    store = Store(steward_home)
    job, runner, worker, _, notes = _setup(store, "off")
    _needs_human(runner, job.job_id, _sig(pid_alive=True))
    assert store.get_job(job.job_id).state == JobState.NEEDS_HUMAN
    assert "NEEDS_YOU" not in _kinds(notes)                                   # reviewed first (G23)
    (b,) = blockers.open_blockers(store, job.job_id)
    assert b.kind == "recovery_exhausted" and ownership.owner_view(store, job.job_id).status == "awaiting_technical_review"
    (rec,) = [a for a in approvals.list_approvals(store, job.job_id, status="pending")
              if approvals.is_recovery_approval(a)]
    assert rec["awaiting_review"] is True
    approvals.decide(store, job.job_id, rec["id"], "approve", decided_by="human")  # the owner may still resume it
    assert store.get_job(job.job_id).state == JobState.RUNNING


def test_flow_proposal_on_parked_job_reaches_review_and_resumes_worker(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    ep = _episode(store, jid, label="retry_or_rebuild")
    rec = fa.process(store, ep, _investigation(store, ep), mode="active", now=1000.0)
    assert (rec["decision"], rec["reason"]) == ("deferred", "job_not_running")   # G1's guard is unchanged
    (f,) = [b for b in blockers.open_blockers(store, jid) if b.kind == "flow_finding"]
    assert f.origin_actor == "model_supervisor:flow" and any(e.startswith("flow_instruction:") for e in f.evidence)
    lead, scripted = control_lead(store, {"astra-tech-lead": [_reopen("Install the missing dependency first.")]})
    t = time.time() + 31
    ownership.sweep(store, now=t, job_lead=lead)
    ownership.sweep(store, now=t + 1, job_lead=lead)
    assert "flow (retry_or_rebuild)" in scripted.calls[0][1]
    assert store.get_job(jid).state == JobState.RUNNING


def test_flow_escalate_human_is_reviewed_not_sent_to_owner(steward_home):
    store = Store(steward_home)
    notes = Notes()
    jid = contracted(store, "fix the parser")
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    ep = _episode(store, jid, label="blocked_handoff")
    rec = fa.process(store, ep, _investigation(store, ep, action="escalate_human"), mode="active", now=1000.0)
    assert rec["decision"] == "escalated"
    (f,) = blockers.open_blockers(store, jid)
    assert f.kind == "flow_finding" and f.disposition == "candidate"
    ownership.sweep(store, now=time.time() + 31, notifier=notes)
    assert notes.events == [] and [x["source"] for x in rt.Arbiter(store).pending_for_tech_lead(jid)] == ["ownership"]


def test_jev_risky_action_becomes_a_reviewable_concern(steward_home):
    store = Store(steward_home)
    notes = Notes()
    jid = contracted(store)
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    runner = _runner(store, notes)
    from steward.runner import _JevActions

    acts = _JevActions(runner)
    first = acts.concern(jid, "risky_action", 0.91)
    again = acts.concern(jid, "risky_action", 0.93)                       # unchanged finding: same episode
    assert "candidate" in first and "repeat" in again and notes.events == []
    (b,) = blockers.open_blockers(store, jid)
    assert b.kind == "jev_concern" and b.origin_actor == "jev:jev"
    import pytest

    with pytest.raises(blockers.BlockerError):                            # Jev may raise, never decide
        blockers.transition(store, b.blocker_id, "resolved", actor=blockers.ActorRef(blockers.Actor.JEV, "jev"),
                            reason="ok now", now=time.time())
    ownership.reconcile_job(store, jid, now=time.time() + ownership.JEV_CONCERN_TTL_S + 10)
    assert blockers.open_blockers(store, jid) == []                       # withdrawn when not seen again
    assert store.get_job(jid).state == JobState.RUNNING                    # never halts the job


def test_jev_watch_routes_risky_action_through_concern_when_available():
    calls = []

    class Acts:
        def concern(self, job_id, choice, confidence):
            calls.append(("concern", choice))
            return "candidate blocker raised"

        def needs_you(self, job_id, text):
            calls.append(("needs_you", text))
            return "notified"

    w = jev_watch.JevWatch.__new__(jev_watch.JevWatch)
    w.actions = Acts()
    out = jev_watch.JevWatch._escalate(w, "J", "risky_action", 0.9)
    assert calls == [("concern", "risky_action")] and out == ["concern: candidate blocker raised"]


def test_reliance_hold_names_disputed_completion_claims(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    ownership.raise_signal_concern(store, jid, source="jev", choice="evidence_contradicts", text="done claim disputed",
                                   confidence=0.8, now=1.0, refs=["claim:done:abc123"])
    ownership.raise_signal_concern(store, jid, source="jev", choice="thrash", text="progress concern",
                                   confidence=0.8, now=1.0)
    assert [b.kind for b in ownership.reliance_hold(store, jid, "claim:done:abc123")] == ["jev_concern"]
    assert ownership.reliance_hold(store, jid, "claim:done:other") == []


def test_a_refused_rework_launch_is_retried_with_backoff_then_reviewed(steward_home):
    store = Store(steward_home)
    notes = Notes()
    jid = park_tracks(store)
    sendback.send_back(store, jid, "try again", origin="tech_lead:astra-tech-lead",
                       judgment=Judgment.model_informed_code_authorized)  # store half done; the launch half refuses
    calls = []

    def launch(job_id):
        calls.append(job_id)
        return {"accepted": False, "detail": "no Herdr workspace known for this job"}
    t = time.time()
    for dt in (0, 60, 121, 181, 482, 1383, 1400):
        ownership.sweep(store, now=t + dt, launch_fn=launch, notifier=notes)
    assert len(calls) == 3                                                # 3 refused launches, then no more
    (b,) = [b for b in blockers.open_blockers(store, jid) if b.kind == "worker_start_blocked"]
    assert "no Herdr workspace" in b.claim and notes.events == []
    assert len(intents.for_job(store, jid, "launch_worker")) == 3
    assert ownership._control_owned(store, store.get_job(jid))            # the control pane's lead reviews it


def test_an_accepted_launch_is_never_repeated(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    sendback.send_back(store, jid, "try again", origin="cli")
    calls = []
    t = time.time()
    for dt in (0, 121, 300, 1000, 2000):
        ownership.sweep(store, now=t + dt, launch_fn=lambda j: calls.append(j) or {"accepted": True, "tab_id": "w:t"})
    assert len(calls) == 1                                                # a slow runner is waited for, not doubled
    assert [b.kind for b in blockers.open_blockers(store, jid)] == ["worker_start_blocked"]
