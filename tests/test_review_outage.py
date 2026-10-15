"""Plan 1 Task 10: reviewer outage (spec §5 settled behaviour; G13, G35; review focus 4)."""
from __future__ import annotations

import time

from steward import approvals, code_facts, jev_watch, ownership, review_service
from steward import fleet_runtime as rt
from steward import fleet_tech_lead as tl
from steward import tech_lead_actions as tla
from steward import tech_lead_policy as tpol
from steward.models import JobState, Judgment
from steward.store import Store
from ownership_fixtures import (ASTRA, FABLE, Clock, Notes, ScriptedLead, SyncBg, contracted, control_lead,
                                park_tracks, proposal)


def _reopen():
    return proposal({"type": "reopen", "params": {"text": "retry with the unit powered", "evidence": "x",
                                                   "confidence": 0.9}})


def test_failure_classes():
    c = review_service.classify_failure
    assert c("timeout: astra: no answer within 300s") == "timeout"
    assert c("malformed: malformed_json") == "malformed"
    assert c("unavailable: RuntimeError: astra: exit 1: 401 Unauthorized") == "auth"
    assert c("unavailable: RuntimeError: fable: exit 1: 402 billing_error") == "auth"
    assert c("unavailable: codex CLI not on PATH") == "unavailable"
    assert c("unavailable: RuntimeError: astra: exit 2: boom") == "provider_error"
    assert c("self_review: the worker runs gpt-6-astra") == "self_review"
    assert c("repeated_failed_strategy: strategy 'send_back' already failed") == "strategy"


def test_backoff_slots_and_one_probe_per_slot(steward_home):
    store = Store(steward_home)
    clock = Clock(1000.0)
    texts: list[str] = []
    j1, j2 = contracted(store), contracted(store)
    o = review_service.ReviewOutage(store, lambda t: texts.append(t) or True, clock=clock)
    fail = [{"id": "astra-tech-lead", "reason": "timeout: x"}, {"id": "fable-tech-lead", "reason": "timeout: y"}]
    o.record_failure(j1, fail)
    assert o.is_down() and o.next_retry_at() == 1060.0
    assert o.may_attempt(j1, 1030.0) is False
    assert o.may_attempt(j1, 1061.0) is True and o.may_attempt(j2, 1061.0) is False  # one probe per slot
    clock.t = 1062.0
    o.record_failure(j1, fail)
    assert o.next_retry_at() == 1062.0 + 120.0
    for _ in range(10):
        clock.advance(5000)
        o.record_failure(j1, fail)
    assert o.next_retry_at() - clock.t == review_service.BACKOFF_S[-1]
    assert len(texts) == 1  # same classes, same job: no update


def test_material_change_updates_at_most_hourly_and_remedy_after_three_auth_failures(steward_home):
    store = Store(steward_home)
    clock = Clock(1000.0)
    texts: list[str] = []
    j1, j2 = contracted(store), contracted(store)
    o = review_service.ReviewOutage(store, lambda t: texts.append(t) or True, clock=clock)
    auth = [{"id": "astra-tech-lead", "reason": "unavailable: RuntimeError: exit 1: 401 Unauthorized"}]
    o.record_failure(j1, auth)
    clock.advance(100)
    o.record_failure(j2, auth)                         # a newly affected job, but < 1 h since the alert
    assert len(texts) == 1
    clock.advance(3600)
    o.record_failure(j2, auth)                       # third consecutive auth failure; > 1 h: update + remedy
    assert len(texts) == 3 and "re-login" in texts[-1] and "nothing else is waiting on you" in texts[-1]
    assert "No action needed" in texts[0] and "NEEDS" not in " ".join(texts)


def test_timeouts_and_malformed_answers_are_an_outage_not_an_owner_request(steward_home):
    store = Store(steward_home)
    notes = Notes()
    jid = park_tracks(store)
    outage = review_service.ReviewOutage(store, notes.send_text)
    lead, _ = control_lead(store, {"astra-tech-lead": [TimeoutError("no answer within 300s")],
                                   "fable-tech-lead": ["not json at all"]}, notifier=notes, outage=outage)
    t = time.time()
    ownership.reconcile_job(store, jid, now=t)
    for k in range(4):
        ownership.sweep(store, now=t + 31 + k, job_lead=lead, notifier=notes, outage=outage)
    assert outage.is_down() and len(notes.texts) == 1 and "No action needed" in notes.texts[0]
    assert notes.events == [] and approvals.list_approvals(store, jid, status="pending") == []
    view = ownership.owner_view(store, jid)
    assert view.status == "technical_review_unavailable" and view.next_retry_at
    assert [e for e in store.events(jid) if e.what == "tech_lead_review_unavailable"]
    assert not [e for e in store.events(jid) if e.what in ("tech_lead_exhausted", "tech_lead_escalated")]


def test_outage_survives_restart_and_recovers_exactly_once(steward_home):
    store = Store(steward_home)
    notes = Notes()
    clock = Clock(time.time())
    jid = park_tracks(store)
    outage = review_service.ReviewOutage(store, notes.send_text, clock=clock)
    lead, _ = control_lead(store, {"astra-tech-lead": [RuntimeError("astra: exit 1: 401 Unauthorized")],
                                   "fable-tech-lead": [RuntimeError("fable: exit 1: 402 billing")]},
                           outage=outage, clock=clock)
    ownership.reconcile_job(store, jid, now=clock.t)
    for step in (31, 1, 1):
        ownership.sweep(store, now=clock.advance(step), job_lead=lead, outage=outage)
    assert outage.is_down()
    # controller restart: new service object, new lead, same store
    outage2 = review_service.ReviewOutage(store, notes.send_text, clock=clock)
    lead2, scripted2 = control_lead(store, {"astra-tech-lead": [_reopen()]}, outage=outage2, clock=clock)
    ownership.sweep(store, now=clock.advance(30), job_lead=lead2, outage=outage2)
    assert outage2.is_down() and scripted2.calls == []                 # pending review kept, not retried early
    for step in (40, 1, 1, 1, 60):
        ownership.sweep(store, now=clock.advance(step), job_lead=lead2, outage=outage2)
    assert len(scripted2.calls) == 1 and not outage2.is_down()
    assert len([e for e in store.events(jid) if e.what == "sent_back"]) == 1
    assert len(notes.texts) == 2 and "back" in notes.texts[1]          # one alert, one recovery notice
    assert store.get_job(jid).state == JobState.RUNNING


def test_a_running_jobs_pending_review_is_kept_through_an_outage(steward_home):
    store = Store(steward_home)
    clock = Clock(time.time())
    notes = Notes()
    jid = contracted(store)
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    outage = review_service.ReviewOutage(store, notes.send_text, clock=clock)
    arb = rt.Arbiter(store, clock=clock)
    scripted = ScriptedLead({"astra-tech-lead": [TimeoutError("t")], "fable-tech-lead": [TimeoutError("t")]})
    ex = tla.ActionExecutor(store, runner_exec=None, clock=clock)
    lead = tl.JobTechLead(store, [ASTRA, FABLE], ex, arbiter=arb, invoke=scripted, available=lambda e: None,
                          policy_fn=lambda: tpol.Policy(), clock=clock, start_bg=SyncBg, outage=outage)
    arb.submit(jid, code_facts.SOURCE, "new_failures_repeated", key="k1", test="tests.t::a")
    for _ in range(3):
        lead.on_event(jid)
    st = lead.state(jid)
    assert st["pending_facts"][0]["kind"] == "new_failures_repeated" and arb.pending_for_tech_lead(jid) == []
    assert lead.on_event(jid)["status"] == "review_unavailable"
    clock.advance(61)
    scripted.plan["astra-tech-lead"].append(proposal({"type": "send_back", "params": {"text": "fix tests.t::a"}}))
    lead.on_event(jid)
    assert lead.on_event(jid)["status"] == "acted"
    assert scripted.calls[-1][0] == "astra-tech-lead" and "new_failures_repeated" in scripted.calls[-1][1]
    assert not outage.is_down() and len(notes.texts) == 2


def test_self_review_only_uses_a_disclosed_same_model_review(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    store.kv_set(jid, "worker_model", "gpt-6-astra")
    lead, scripted = control_lead(store, {"astra-tech-lead": [_reopen()]})
    lead.leads = [ASTRA]  # the only lead runs the worker's own model
    t = time.time()
    ownership.reconcile_job(store, jid, now=t)
    ownership.sweep(store, now=t + 31, job_lead=lead)
    ownership.sweep(store, now=t + 32, job_lead=lead)
    (fb,) = [e for e in store.events(jid) if e.what == "tech_lead_same_model_fallback"]
    assert fb.evidence["lead"] == "astra-tech-lead" and store.get_job(jid).state == JobState.RUNNING


def test_jev_outage_and_reviewer_outage_are_distinct(steward_home):
    store = Store(steward_home)
    texts: list[str] = []
    j1 = contracted(store)
    o = review_service.ReviewOutage(store, lambda t: texts.append(t) or True)
    o.record_failure(j1, [{"id": "astra-tech-lead", "reason": "timeout: x"}])
    assert jev_watch.outage_status(store) is None and review_service.outage_status(store)["state"] == "down"
    store.kv_set(jev_watch.OutageAlert.KV_JOB, jev_watch.OutageAlert.KEY, {"state": "down", "since": 1.0})
    o.record_success(j1, "astra-tech-lead")
    assert review_service.outage_status(store) is None and jev_watch.outage_status(store) is not None
