"""v2.3 Flow episode persistence in the Store (phase 23): atomic, idempotent, bounded, masked."""

from __future__ import annotations

import json

import pytest

from steward.store import Store


# -- v2.3 Flow episodes (phase 23) ------------------------------------------------------------


def _ep(store, job_id, episode_id="ep-1", label="retry_or_rebuild", now=1000.0, **kw):
    args = dict(episode_id=episode_id, label=label, confidence=0.9, detector_version="flow-v1", mode="shadow",
                snapshot={"turns": [["tool", "pytest"]]}, source_refs=["turn:3", "event:7"], job_revision=2,
                head_sha="abc123", now=now, dedupe_window_s=900.0)
    args.update(kw)
    return store.record_flow_episode(job_id, **args)


def test_flow_episode_recorded_once_with_one_linked_event(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    rec, created = _ep(store, job.job_id)
    assert created and rec["episode_id"] == "ep-1" and rec["label"] == "retry_or_rebuild"
    assert rec["source_refs"] == ["turn:3", "event:7"] and rec["job_revision"] == 2 and rec["head_sha"] == "abc123"
    evs = [e for e in store.events(job.job_id) if e.what == "flow_episode_recorded"]
    assert len(evs) == 1 and evs[0].seq == rec["event_seq"] and evs[0].evidence["authority"] == "none"
    again, created2 = _ep(store, job.job_id)
    assert not created2 and again == rec
    assert len([e for e in store.events(job.job_id) if e.what == "flow_episode_recorded"]) == 1


def test_flow_episode_same_label_inside_window_dedupes_outside_does_not(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    first, _ = _ep(store, job.job_id, "ep-1", now=1000.0)
    dup, created = _ep(store, job.job_id, "ep-2", now=1500.0)
    assert not created and dup["episode_id"] == "ep-1"
    other, created_other = _ep(store, job.job_id, "ep-3", label="blocked_handoff", now=1500.0)
    assert created_other and other["episode_id"] == "ep-3"
    later, created_later = _ep(store, job.job_id, "ep-4", now=1000.0 + 901)
    assert created_later
    assert [r["episode_id"] for r in store.flow_episodes(job.job_id)] == ["ep-1", "ep-3", "ep-4"]


def test_flow_episode_survives_restart_and_dedupes_after_it(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    rec, _ = _ep(store, job.job_id)
    store.close()
    reopened = Store(steward_home)
    assert reopened.flow_episode("ep-1") == rec
    assert _ep(reopened, job.job_id)[1] is False


def test_flow_episode_concurrent_writers_create_exactly_one(steward_home):
    import threading

    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    results, errors = [], []

    def worker(i):
        try:
            results.append(_ep(Store(steward_home), job.job_id, f"ep-{i}", now=1000.0 + i)[1])
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and results.count(True) == 1
    assert len(store.flow_episodes(job.job_id)) == 1
    assert len([e for e in store.events(job.job_id) if e.what == "flow_episode_recorded"]) == 1


def test_flow_episode_is_masked_and_bounded(steward_home):
    from steward import masking

    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    masking.register(["literal-flow-secret-999"])
    try:
        rec, _ = _ep(store, job.job_id, snapshot={"turns": [["said", "key literal-flow-secret-999"]]},
                     source_refs=[f"event:{i}" for i in range(100)])
        blob = json.dumps(rec) + json.dumps([e.evidence for e in store.events(job.job_id)])
        assert "literal-flow-secret-999" not in blob
        assert len(rec["source_refs"]) == store.FLOW_MAX_REFS
        with pytest.raises(ValueError):
            _ep(store, job.job_id, "ep-big", label="wrong_direction", snapshot={"raw": "x" * 5000})
        assert store.flow_episode("ep-big") is None
    finally:
        masking.clear()
