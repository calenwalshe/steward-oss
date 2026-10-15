# tests/test_pause_single_writer.py
"""Spec 2026-10-13 §4.5 guard: with pause review on, only the tech lead, `steward` (soft stop, backstops, code guards),
`contract`, the mechanical `check_run`/`deadline` channels and human/CLI origins reach the worker; every other
origin's steer becomes a pending fact. Pause=off jobs take every origin as today."""
from __future__ import annotations

import pytest

from pause_support import pause_facts, set_pause
from test_runner_fabric import _contracted_job

from steward import efficiency_watch, flow_actions, liveness, pause_checkpoint
from steward import steering as steering_module
from steward.steering import PAUSE_ALLOWED_ORIGINS, PAUSE_FOLDED_ORIGINS, Steering
from steward.store import Store

HUMAN = ("cli", "moshi", "chatgpt", "dash", "questions", "steer-agent")
CODE = sorted(steering_module._NON_HUMAN_ORIGINS) + ["watcher:tests", efficiency_watch.ORIGIN]


def _job(store, mode):
    job = _contracted_job(store)
    set_pause(store, job.job_id, mode)
    return job.job_id


def test_every_code_origin_is_classified():
    assert steering_module._NON_HUMAN_ORIGINS == PAUSE_ALLOWED_ORIGINS | PAUSE_FOLDED_ORIGINS
    assert not PAUSE_ALLOWED_ORIGINS & PAUSE_FOLDED_ORIGINS
    assert steering_module.pause_folds("watcher:anything") and not steering_module.pause_folds("tech_lead:astra")


@pytest.mark.parametrize("origin", CODE + list(HUMAN))
def test_only_allowed_origins_reach_a_pause_job(steward_home, origin):
    store = Store(steward_home)
    jid = _job(store, "unit")
    receipt = Steering(store).record(jid, "nudge", f"from {origin}", origin=origin)
    reaches = origin in PAUSE_ALLOWED_ORIGINS or not steering_module._is_non_human(origin)
    assert receipt.accepted is reaches
    assert [i["origin"] for i in Steering(store).all_instructions(jid)] == ([origin] if reaches else [])
    facts = pause_checkpoint.load(store, jid)["pending_facts"]
    assert [f["source"] for f in facts] == ([] if reaches else [origin])


@pytest.mark.parametrize("origin", CODE)
def test_pause_off_jobs_still_take_every_origin(steward_home, origin):
    """Review Focus 5 (steering half)."""
    store = Store(steward_home)
    jid = _job(store, "off")
    assert Steering(store).record(jid, "nudge", f"from {origin}", origin=origin).accepted
    assert not pause_checkpoint.exists(store, jid)


def test_a_folded_steer_keeps_source_and_text_and_writes_no_instruction(steward_home):
    store = Store(steward_home)
    jid = _job(store, "unit")
    rev_before = store.get_revision(jid)
    receipt = Steering(store).record(jid, "nudge", "Read the HANDOFF and redo task 6", "req-diag-1", origin="diagnosis")
    assert not receipt.accepted and receipt.detail.startswith("FOLDED pause_review")
    (f,) = pause_checkpoint.load(store, jid)["pending_facts"]
    assert (f["source"], f["kind"], f["text"], f["urgent"]) == ("diagnosis", "steer_nudge",
                                                                 "Read the HANDOFF and redo task 6", False)
    assert store.get_revision(jid) == rev_before and pause_facts(store, jid, "urgent_fact") == []
    again = Steering(store).record(jid, "nudge", "Read the HANDOFF and redo task 6", "req-diag-1", origin="diagnosis")
    assert again.duplicate and len(pause_checkpoint.load(store, jid)["pending_facts"]) == 1
    assert [e.what for e in store.events(jid) if e.what == "steer_folded_to_fact"] == ["steer_folded_to_fact"]


def test_a_liveness_nudge_is_an_urgent_fact_that_wakes_the_tech_lead(steward_home):
    store = Store(steward_home)
    jid = _job(store, "unit")
    liveness._default_nudge(store)(jid, liveness.NUDGE_TEXT)
    assert Steering(store).all_instructions(jid) == []
    (fact,) = pause_facts(store, jid, "urgent_fact")
    assert fact["route"] == "outer" and fact["data"]["origin"] == "liveness"
    assert pause_checkpoint.load(store, jid)["pending_facts"][0]["urgent"] is True


def test_a_gated_watcher_interrupt_is_an_urgent_fact_not_an_interrupt(steward_home):
    store = Store(steward_home)
    jid = _job(store, "unit")
    receipt = Steering(store).record(jid, "nudge", "[watcher loops] stop re-running the same test",
                                     origin="watcher:loops", urgent=True, gated_by="watcher_authority")
    assert not receipt.accepted and Steering(store).all_instructions(jid) == []
    assert len(pause_facts(store, jid, "urgent_fact")) == 1


def test_the_flow_investigator_is_folded_through_its_own_entry_point(steward_home):
    store = Store(steward_home)
    jid = _job(store, "unit")
    status, reason, receipt = flow_actions._steer(store, {"episode_id": "e1", "job_id": jid,
                                                          "job_revision": store.get_revision(jid)},
                                                  "nudge", "look at the log first")
    assert (status, reason) == ("deferred", "steering_refused") and "FOLDED" in receipt["detail"]
    assert pause_checkpoint.load(store, jid)["pending_facts"][0]["source"] == "flow"
