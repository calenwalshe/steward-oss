# tests/test_pause_pool.py
"""Spec 2026-10-13 §6: a small global pool of pause reviews (about 4), longest-waiting job first; a lost review is
requested again from the checkpoint."""
from __future__ import annotations

from pause_support import pause_facts, set_pause
from test_runner_fabric import _contracted_job

from steward import pause_checkpoint
from steward import pause_review as pr
from steward.fleet_runtime import Arbiter
from steward.store import Store


def _pause_job(store):
    job = _contracted_job(store)
    set_pause(store, job.job_id, "unit")
    return job.job_id


def test_pool_admits_four_then_queues_longest_waiting_first(steward_home):
    store = Store(steward_home)
    for i in range(pr.POOL_SIZE):
        assert pr.pool_acquire(store, f"j{i}", now=100.0, requested_at=100.0)
    assert not pr.pool_acquire(store, "late", now=101.0, requested_at=90.0)
    assert not pr.pool_acquire(store, "later", now=101.0, requested_at=95.0)
    pr.pool_release(store, "j0")
    assert not pr.pool_acquire(store, "later", now=102.0, requested_at=95.0)   # "late" has waited longer
    assert pr.pool_acquire(store, "late", now=102.0, requested_at=90.0)
    assert pr.pool_acquire(store, "late", now=103.0, requested_at=90.0)        # the holder re-enters


def test_a_stale_slot_is_reclaimed(steward_home):
    store = Store(steward_home)
    for i in range(pr.POOL_SIZE):
        pr.pool_acquire(store, f"j{i}", now=0.0, requested_at=0.0)
    assert pr.pool_acquire(store, "new", now=pr.POOL_STALE_S + 1, requested_at=pr.POOL_STALE_S)


def test_rerequest_resubmits_the_latest_pause_and_review_live_sees_it(steward_home):
    store = Store(steward_home)
    jid = _pause_job(store)
    fact = {"seq": 9, "sha": "abc", "yield_lane": "STOPPED", "unit_id": "u1", "done_claim": True, "pause_seq": 2,
            "pause_kind": "pause"}
    pause_checkpoint.update(store, jid, lambda c: c.update(last_pause_fact=fact, review_in_flight=True))
    assert pr.rerequest(store, jid, now=500.0)
    cp = pause_checkpoint.load(store, jid)
    assert cp["awaiting_review"] and not cp["review_in_flight"] and cp["review_requested_at"] == 500.0
    assert cp["done_claim"] is True
    (f,) = pause_facts(store, jid)
    assert f["route"] == "outer" and f["data"]["resubmitted"] is True and f["data"]["pause_seq"] == 2
    arb = Arbiter(store)
    assert pr.review_live(store, jid, arb)
    arb.mark_reviewed(jid)
    assert not pr.review_live(store, jid, arb)
    store.kv_set(jid, "job_tech_lead_state", {"pending_facts": [{"kind": "worker_paused"}]})
    assert pr.review_live(store, jid, arb)
    store.kv_set(jid, "job_tech_lead_state", {"in_flight": {"lead": "astra-tech-lead"}})
    assert pr.review_live(store, jid, arb)


def test_rerequest_without_a_pause_does_nothing(steward_home):
    store = Store(steward_home)
    jid = _pause_job(store)
    assert not pr.rerequest(store, jid, now=1.0) and pause_facts(store, jid) == []


def test_review_fact_is_the_newest_pause():
    facts = [{"kind": "permission_ask_repeated", "data": {}},
             {"kind": "worker_paused", "data": {"pause_seq": 1, "unit_id": "u1"}},
             {"kind": "worker_paused", "data": {"pause_seq": 3, "unit_id": "u3"}}]
    assert pr.review_fact(facts)["unit_id"] == "u3" and pr.review_fact([{"kind": "x"}]) == {}


def test_queued_is_logged_once_per_pause(steward_home):
    store = Store(steward_home)
    jid = _pause_job(store)
    pause_checkpoint.update(store, jid, lambda c: c.update(pause_seq=1))
    pr.note_queued(store, jid, now=1.0)
    pr.note_queued(store, jid, now=2.0)
    assert len([e for e in store.events(jid) if e.what == "pause_review_queued"]) == 1
