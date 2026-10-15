# tests/test_pause_tech_lead.py
"""Spec 2026-10-13 §4.4/§6/§7: the tech lead reviews every pause with exactly one rule_pause; pause reviews skip the
hourly cap and the idle guard, share a pool, respect the spend cap (done claims only), fail over to provisional
continue with one notice after three failures, and the code-snapshot clock is gone in pause mode."""
from __future__ import annotations

import json

import pytest

from pause_support import (Spend, events, pause_lead_env, prime_pause, rule, steers)

from steward import code_facts, liveness, pause_checkpoint
from steward import pause_review
from steward import supervision_allowance as sa


@pytest.fixture(autouse=True)
def _fresh_allowance_cache():
    sa.reset_cache()
    yield
    sa.reset_cache()


def test_a_pause_review_rules_and_steers_once(steward_home):
    store, jid, lead, arb, invoke, clock, _ = pause_lead_env(steward_home, {"astra-tech-lead": [rule("continue")]})
    prime_pause(store, jid, arb, clock)
    assert lead.on_event(jid)["status"] == "review_started"
    assert events(store, jid, "tech_lead_review_started")[-1].evidence["facts"] == ["worker_paused"]
    prompt = invoke.prompts[0][1]
    assert "exactly ONE action of type rule_pause" in prompt and '"allowed_actions": ["rule_pause"]' in prompt
    assert pause_checkpoint.load(store, jid)["review_in_flight"]
    assert lead.on_event(jid)["status"] == "acted"
    (s,) = steers(store, jid, "tech_lead")
    assert "Go: unit u2" in s["text"]
    cp = pause_checkpoint.load(store, jid)
    assert cp["verdict"] == "continue" and not cp["awaiting_review"] and not cp["review_in_flight"]


def test_pause_reviews_skip_the_hourly_cap_and_the_idle_guard(steward_home):
    store, jid, lead, arb, invoke, clock, _ = pause_lead_env(steward_home, {"astra-tech-lead": [rule("continue")]})
    lead._set(jid, review_starts=[clock()] * sa.MAX_REVIEWS_PER_HOUR)               # the hourly cap is full
    liveness.record_runner(store, jid, turn_open=False, progress_ts=clock() - 3600, pid=1)   # idle for an hour
    prime_pause(store, jid, arb, clock)
    assert lead.on_event(jid)["status"] == "review_started"
    assert len(lead.state(jid)["review_starts"]) == sa.MAX_REVIEWS_PER_HOUR          # not counted against the cap
    assert events(store, jid, "tech_lead_review_deferred") == [] and events(store, jid, "tech_lead_idle_paused") == []


def test_an_answer_that_is_not_one_rule_pause_falls_through(steward_home):
    send_back = json.dumps({"proposal_id": "p", "trigger_id": "t", "confidence": 0.9, "signature": "s",
                            "actions": [{"type": "send_back", "params": {"text": "x"}}]})
    store, jid, lead, arb, invoke, clock, _ = pause_lead_env(
        steward_home, {"astra-tech-lead": [send_back], "fable-tech-lead": [rule("send_back")]})
    prime_pause(store, jid, arb, clock)
    lead.on_event(jid)
    lead.on_event(jid)
    assert lead.on_event(jid)["status"] == "acted"
    answered = events(store, jid, "tech_lead_answered")[-1]
    assert answered.evidence["answered_by"] == "fable-tech-lead"
    assert answered.evidence["skipped"][0]["reason"] == "malformed: not_a_single_rule_pause"


def test_unreachable_leads_continue_provisionally_and_notify_once_after_three(steward_home):
    plan = {"astra-tech-lead": [TimeoutError("t")] * 4, "fable-tech-lead": [TimeoutError("t")] * 4}
    store, jid, lead, arb, invoke, clock, notes = pause_lead_env(steward_home, plan)
    for i in range(4):
        prime_pause(store, jid, arb, clock)
        statuses = [lead.on_event(jid)["status"] for _ in range(3)]
        assert statuses[-1] == "pause_review_unavailable", statuses
        cp = pause_checkpoint.load(store, jid)
        assert cp["force_provisional"] and cp["tl_failures"] == i + 1 and not cp["review_in_flight"]
        clock.advance(10)
    assert len(events(store, jid, "pause_review_failed")) == 4
    assert len([n for n in notes if n[1] == "PAUSE_REVIEW"]) == 1


def test_a_done_claim_is_asked_again_after_an_outage_backoff(steward_home):
    plan = {"astra-tech-lead": [TimeoutError("t")], "fable-tech-lead": [TimeoutError("t")]}
    store, jid, lead, arb, invoke, clock, _ = pause_lead_env(steward_home, plan)
    prime_pause(store, jid, arb, clock, done_claim=True, next_unit=None)
    for _ in range(3):
        lead.on_event(jid)
    st = lead.state(jid)
    assert st["pending_facts"][0]["kind"] == "worker_paused" and st["retry_at"] > clock()
    assert not pause_checkpoint.load(store, jid)["force_provisional"]


def test_past_the_spend_cap_only_a_done_claim_is_reviewed(steward_home):
    store, jid, lead, arb, invoke, clock, notes = pause_lead_env(steward_home, {"astra-tech-lead": [rule("done")]})
    lead.spend_report_fn = lambda: Spend(1.0, 10.0)                       # $10 supervision vs a $3 allowance
    prime_pause(store, jid, arb, clock)
    assert lead.on_event(jid) == {"status": "pause_review_skipped", "reason": "spend_cap"}
    assert invoke.prompts == [] and pause_checkpoint.load(store, jid)["force_provisional"]
    prime_pause(store, jid, arb, clock)
    assert lead.on_event(jid)["status"] == "pause_review_skipped"
    prime_pause(store, jid, arb, clock, done_claim=True, next_unit=None)
    assert lead.on_event(jid)["status"] == "review_started"
    assert len([n for n in notes if n[1] == "PAUSE_REVIEW"]) == 1


def test_a_full_pool_queues_the_review_until_a_slot_frees(steward_home):
    store, jid, lead, arb, invoke, clock, _ = pause_lead_env(steward_home, {"astra-tech-lead": [rule("continue")]})
    for i in range(pause_review.POOL_SIZE):
        assert pause_review.pool_acquire(store, f"other-{i}", now=clock(), requested_at=clock() - 50)
    prime_pause(store, jid, arb, clock)
    assert lead.on_event(jid)["status"] == "pause_review_queued"
    assert lead.on_event(jid)["status"] == "pause_review_queued"
    assert invoke.prompts == [] and len(events(store, jid, "pause_review_queued")) == 1
    pause_review.pool_release(store, "other-0")
    assert lead.on_event(jid)["status"] == "review_started"


def test_pause_mode_has_no_code_snapshot_clock(steward_home):
    store, jid, lead, arb, invoke, clock, _ = pause_lead_env(steward_home, {})
    lead.on_event(jid)
    clock.advance(3 * 3600)
    assert lead.on_event(jid) == {"status": "skipped", "reason": "pause_review_owns_the_clock"}
    assert events(store, jid, "tech_lead_sweep") == []


def test_an_urgent_fact_still_wakes_an_ordinary_review_between_pauses(steward_home):
    store, jid, lead, arb, invoke, clock, _ = pause_lead_env(steward_home, {"astra-tech-lead": [rule("continue")]})
    arb.submit(jid, code_facts.SOURCE, "permission_ask_repeated", key="k1")
    assert lead.on_event(jid)["status"] == "review_started"
    assert '"allowed_actions": ["rule_pause"]' not in invoke.prompts[0][1]


def test_facts_reported_during_the_review_survive_the_ruling(steward_home):
    store, jid, lead, arb, invoke, clock, _ = pause_lead_env(steward_home, {"astra-tech-lead": [rule("continue")]})
    pause_checkpoint.append_fact(store, jid, source="flow", kind="steer_nudge", text="before", at=1.0)
    prime_pause(store, jid, arb, clock)
    lead.on_event(jid)
    pause_checkpoint.append_fact(store, jid, source="flow", kind="steer_nudge", text="during", at=2.0)
    lead.on_event(jid)
    assert [f["text"] for f in pause_checkpoint.load(store, jid)["pending_facts"]] == ["during"]
