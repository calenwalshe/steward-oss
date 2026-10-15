"""Plan 1 Task 5: idempotent intents (spec §4): a repeat claim never duplicates an action; an expired foreign claim
is UNCERTAIN, reconciled against receipts/actual state before any retry; never a second writer."""
from __future__ import annotations

from steward import intents
from steward.store import Store


def _claim(store, key="reopen:J1:aa", owner="A", now=100.0, **kw):
    return intents.claim(store, key=key, job_id="J1", kind="reopen", lease_s=120.0, now=now, owner=owner, **kw)


def test_key_is_deterministic_for_the_same_parts():
    assert intents.intent_key("J1", "reopen", "blk-1", "d1") == intents.intent_key("J1", "reopen", "blk-1", "d1")
    assert intents.intent_key("J1", "reopen", "blk-1", "d1") != intents.intent_key("J1", "reopen", "blk-1", "d2")


def test_one_claim_per_key_across_owners_and_after_completion(steward_home):
    store = Store(steward_home)
    a = _claim(store)
    assert a is not None and a.state == "claimed" and a.attempt == 1
    assert _claim(store, owner="B") is None          # held by A
    assert _claim(store, owner="A") is None          # an exactly-once key is not re-entrant
    done = intents.complete(store, a.key, receipt={"steering_rev": 3}, now=110.0, owner="A")
    assert done.state == "done" and done.receipt == {"steering_rev": 3}
    assert _claim(store, owner="B", now=1000.0) is None   # done is final


def test_only_the_owner_completes(steward_home):
    store = Store(steward_home)
    a = _claim(store)
    assert intents.complete(store, a.key, receipt={}, now=110.0, owner="B") is None
    assert intents.get(store, a.key).state == "claimed"


def test_expired_foreign_claim_becomes_uncertain_not_retried(steward_home):
    store = Store(steward_home)
    a = _claim(store, owner="A", now=100.0)
    assert _claim(store, owner="B", now=500.0) is None
    assert intents.get(store, a.key).state == "uncertain"


def test_reconcile_uncertain_uses_the_kind_checker(steward_home):
    store = Store(steward_home)
    a = _claim(store, owner="A", now=100.0)
    b = intents.claim(store, key="launch:J1:bb", job_id="J1", kind="launch_worker", lease_s=60.0, now=100.0, owner="A")
    c = intents.claim(store, key="notify:J1:cc", job_id="J1", kind="notify", lease_s=60.0, now=100.0, owner="A")
    intents.register_checker("reopen", lambda s, i: ("done", {"seen": "sent_back event"}))
    intents.register_checker("launch_worker", lambda s, i: ("unknown", {}))
    intents.register_checker("notify", lambda s, i: ("abandoned", {"reason": "never sent"}))
    out = intents.reconcile_uncertain(store, now=1000.0, owner="B")
    states = {i.key: i.state for i in out}
    assert states == {a.key: "done", b.key: "uncertain", c.key: "abandoned"}
    assert intents.get(store, b.key).state == "uncertain"  # unknown outcome is never blindly retried


def test_retryable_failure_can_be_claimed_again_failed_cannot(steward_home):
    store = Store(steward_home)
    a = _claim(store, owner="A")
    intents.fail(store, a.key, reason="launch refused", now=110.0, owner="A", retryable=True)
    again = _claim(store, owner="B", now=120.0)
    assert again is not None and again.attempt == 2 and again.owner == "B"
    intents.fail(store, again.key, reason="bad", now=130.0, owner="B", retryable=False)
    assert _claim(store, owner="C", now=140.0) is None


def test_reclaimable_lock_is_reentrant_and_reusable(steward_home):
    store = Store(steward_home)
    lk = dict(key="review-lock:J1", job_id="J1", kind="review", lease_s=360.0, reclaimable=True)
    a = intents.claim(store, now=100.0, owner="A", **lk)
    assert intents.claim(store, now=150.0, owner="A", **lk).lease_expires == 510.0  # re-entrant, lease refreshed
    assert intents.claim(store, now=160.0, owner="B", **lk) is None
    intents.complete(store, a.key, receipt={"status": "acted"}, now=170.0, owner="A")
    b = intents.claim(store, now=180.0, owner="B", **lk)
    assert b is not None and b.attempt == 2
    assert intents.claim(store, now=1000.0, owner="C", **lk) is not None  # an expired lock is taken over
