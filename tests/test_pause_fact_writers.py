# tests/test_pause_fact_writers.py
"""Spec 2026-10-13 §4.5: Sol's stall guidance (recorded with origin `steward`) and the independent reviewer's focus
(recorded with origin `tech_lead`) are facts for the tech lead on a pause job, not steers. Pause=off is unchanged."""
from __future__ import annotations

from ownership_fixtures import SyncBg
from pause_support import ASTRA, FABLE, pause_runner, set_pause, steers
from test_runner_fabric import _contracted_job

from steward import independent_review as ir
from steward import ownership, pause_checkpoint, recovery
from steward import tech_lead_actions as tla
from steward import tech_lead_policy as tpol
from steward.actor import Actor, ActorRef
from steward.models import JobState, Judgment
from steward.store import Store


def _guide(detail="re-run the failing test alone"):
    return recovery.Action(kind="guide", detail=detail, judgment=Judgment.model, rule="stall_diagnosis")


def test_stall_guidance_is_a_fact_not_a_steer_when_pause_is_on(steward_home):
    store, job, runner, factory, _ = pause_runner(steward_home)
    out = runner._apply_action(job.job_id, factory.created[0], _guide(), None)
    assert out == "re-run the failing test alone" and steers(store, job.job_id) == []
    (f,) = pause_checkpoint.load(store, job.job_id)["pending_facts"]
    assert (f["source"], f["kind"], f["text"]) == ("diagnosis", "stall_guidance", "re-run the failing test alone")


def test_stall_guidance_still_steers_when_pause_is_off(steward_home):
    store, job, runner, factory, _ = pause_runner(steward_home, mode="off")
    runner._apply_action(job.job_id, factory.created[0], _guide(), None)
    assert [s["origin"] for s in steers(store, job.job_id)] == ["steward"]
    assert not pause_checkpoint.exists(store, job.job_id)


def _reviewer_apply(steward_home, mode):
    store = Store(steward_home)
    job = _contracted_job(store)
    set_pause(store, job.job_id, mode)
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    b = ownership.request_strategy_review(store, job.job_id, blocker_id=None, reason="2 unproductive rounds",
                                          signature="recovery:worker", now=1.0)
    rev = ir.IndependentReviewer(store, [ASTRA, FABLE], tla.ActionExecutor(store, runner_exec=None),
                                 invoke=lambda *a: "", clock=lambda: 10.0, start_bg=SyncBg,
                                 policy_fn=lambda: tpol.Policy(), available=lambda e: None)
    v = ir.Verdict(verdict="new_approach", hypothesis="the tmp fixture leaks", mechanism="steering",
                   change="isolate the fixture", success_observation="the test passes alone",
                   evidence_refs=("event:1",), wait=None, human_request=None,
                   instructions="Isolate the tmp fixture before re-running", confidence=0.95)
    inf = {"lead": "fable-tech-lead", "model": "claude-fable-5-1", "intent_key": "ik-1", "issue_key": "recovery:worker"}
    rev._apply(job.job_id, b, inf, v, ActorRef(Actor.TECH_LEAD, "independent:fable-tech-lead"), 10.0)
    applied = [e for e in store.events(job.job_id) if e.what == "independent_review_applied"][-1]
    return store, job.job_id, applied


def test_independent_review_focus_is_a_fact_on_a_running_pause_job(steward_home):
    store, jid, applied = _reviewer_apply(steward_home, "unit")
    assert steers(store, jid) == [] and applied.evidence["outcome"] == "folded"
    (f,) = pause_checkpoint.load(store, jid)["pending_facts"]
    assert f["source"] == "independent_review" and "Isolate the tmp fixture" in f["text"]


def test_independent_review_focus_still_steers_when_pause_is_off(steward_home):
    store, jid, applied = _reviewer_apply(steward_home, "off")
    assert applied.evidence["outcome"] == "executed"
    assert [s["origin"] for s in steers(store, jid)] == ["tech_lead"]
