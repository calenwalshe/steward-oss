# tests/test_pause_recovery.py
"""Spec 2026-10-13 §4.5 + RC-3: a parked worker waiting on its pause review (or on the owner) is not stalled, so neither
Sol nor the Jev path diagnoses it. Pause=off jobs keep today's rule."""
from __future__ import annotations

from pause_support import set_pause
from test_runner_fabric import _contracted_job

from steward import pause_checkpoint, recovery
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.heartbeat import Class
from steward.store import Store


def _job(steward_home, mode):
    store = Store(steward_home)
    job = _contracted_job(store)
    set_pause(store, job.job_id, mode)
    return store, job.job_id


def test_an_outstanding_pause_review_or_a_wait_on_owner_is_a_pending_ruling(steward_home):
    store, jid = _job(steward_home, "unit")
    assert recovery.ruling_pending(store, jid) is None
    pause_checkpoint.update(store, jid, lambda c: c.update(awaiting_review=True))
    assert recovery.ruling_pending(store, jid) == "a pause review is outstanding"
    pause_checkpoint.update(store, jid, lambda c: c.update(awaiting_review=False, review_in_flight=True))
    assert recovery.ruling_pending(store, jid) == "a pause review is outstanding"
    pause_checkpoint.update(store, jid, lambda c: c.update(review_in_flight=False, waiting_on="owner"))
    assert recovery.ruling_pending(store, jid) == "waiting on the owner's decision (ask_owner)"


def test_suspect_stall_is_not_diagnosed_while_the_pause_review_is_outstanding(steward_home):
    store, jid = _job(steward_home, "unit")
    pause_checkpoint.update(store, jid, lambda c: c.update(awaiting_review=True))
    diag = FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="redo task 6", hypothesis="h"))
    action = recovery.decide(store, jid, Class.suspect_stall, diag, {"goal": "g"}, now=1.0)
    assert action.kind == "none" and action.rule == "ruling_pending" and diag.contexts == []


def test_pause_off_ignores_a_stray_checkpoint(steward_home):
    store, jid = _job(steward_home, "off")
    store.kv_set(jid, pause_checkpoint.KEY, {"awaiting_review": True})
    assert recovery.ruling_pending(store, jid) is None
