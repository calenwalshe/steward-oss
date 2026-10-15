import sqlite3
from pathlib import Path

import pytest

from steward.machine import IllegalTransition
from steward.models import Job, Judgment, JobState, Receipt
from steward.store import Store, ensure_outside


def test_create_and_get_job(steward_home):
    store = Store(steward_home)
    job = store.create_job("do the thing", "/repo")
    assert job.state == JobState.CREATED
    fetched = store.get_job(job.job_id)
    assert fetched == job


def test_persistence_across_two_store_instances(steward_home):
    store1 = Store(steward_home)
    job = store1.create_job("goal", "/repo")

    store2 = Store(steward_home)
    fetched = store2.get_job(job.job_id)
    assert fetched is not None
    assert fetched.job_id == job.job_id
    assert fetched.goal == "goal"


def test_get_unknown_job_returns_none(steward_home):
    store = Store(steward_home)
    assert store.get_job("nope") is None


def test_set_state_appends_event_and_updates_job(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    updated = store.set_state(
        job.job_id,
        JobState.CONTRACTED,
        what="contract proposed",
        evidence={"foo": "bar"},
        rule="discovery_complete",
        judgment=Judgment.code,
    )
    assert updated.state == JobState.CONTRACTED

    events = store.events(job.job_id)
    assert len(events) == 1
    event = events[0]
    assert event.what == "contract proposed"
    assert event.evidence == {"foo": "bar"}
    assert event.rule == "discovery_complete"
    assert event.judgment == Judgment.code


def test_illegal_transition_is_atomic_no_event_no_state_change(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    with pytest.raises(IllegalTransition):
        store.set_state(
            job.job_id,
            JobState.ACCEPTED,
            what="skip ahead",
            evidence={},
            rule="bogus",
            judgment=Judgment.code,
        )

    fetched = store.get_job(job.job_id)
    assert fetched.state == JobState.CREATED
    assert store.events(job.job_id) == []


def test_events_are_append_only_at_db_level(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    store.set_state(
        job.job_id,
        JobState.CONTRACTED,
        what="w",
        evidence={},
        rule="r",
        judgment=Judgment.code,
    )

    # No update/delete API exists on Store.
    assert not hasattr(store, "update_event")
    assert not hasattr(store, "delete_event")

    # DB-level trigger enforcement.
    conn = sqlite3.connect(store.db_path)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE events SET what='tampered' WHERE seq=1")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM events WHERE seq=1")
    conn.close()


def test_events_ordered_by_seq(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    store.set_state(
        job.job_id, JobState.CONTRACTED, what="a", evidence={}, rule="r",
        judgment=Judgment.code,
    )
    store.set_state(
        job.job_id, JobState.RUNNING, what="b", evidence={}, rule="r",
        judgment=Judgment.code,
    )
    events = store.events(job.job_id)
    assert [e.what for e in events] == ["a", "b"]
    assert events[0].seq < events[1].seq


def test_receipts_roundtrip(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    receipt = Receipt(
        request_id="req-1",
        job_id=job.job_id,
        kind="stop",
        accepted=True,
        detail="stopped",
        ts="2026-01-01T00:00:00Z",
    )
    store.put_receipt(receipt)
    fetched = store.get_receipt("req-1")
    assert fetched == receipt


def test_get_unknown_receipt_returns_none(steward_home):
    store = Store(steward_home)
    assert store.get_receipt("nope") is None


def test_kv_roundtrip_and_default(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    assert store.kv_get(job.job_id, "recoveries", default=0) == 0
    store.kv_set(job.job_id, "recoveries", 1)
    assert store.kv_get(job.job_id, "recoveries", default=0) == 1


def test_ensure_outside_raises_when_inside(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    inside = worktree / "steward.db"
    with pytest.raises(ValueError):
        ensure_outside(inside, worktree)


def test_ensure_outside_passes_when_outside(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    outside = tmp_path / "state" / "steward.db"
    ensure_outside(outside, worktree)  # should not raise
