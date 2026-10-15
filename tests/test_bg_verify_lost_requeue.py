"""Zero-touch §5.5: a LOST background verification (the process vanished, or its shared check run was lost) is
re-queued (`verify_requeued`, Judgment.code) instead of parking NEEDS_HUMAN "crashed"; the third loss is NEEDS_HUMAN
"lost twice" — a reviewable park whose next ladder step is the single ask. A plain crash keeps the crashed path; a moved candidate's failure is
stale (re-run), never a crash."""
from __future__ import annotations

import time

import pytest

from steward import bg_verify, liveness, ownership, runner as runner_module
from steward.models import JobState, Judgment
from steward.store import Store
from steward.verify import Verification
from test_bg_verify import _bg_job

_LADDER = {"enabled": True, "every_s": 60.0, "orphan_s": 1800.0, "idle_s": 1800.0, "tries_per_day": 2}


def _vanish(store, job_id):
    rec = bg_verify.record(store, job_id)
    store.kv_set(job_id, bg_verify.KEY, {**rec, "started_at": rec["started_at"] - bg_verify.STALE_S - 5})


def _lost_by_check_run(store, job_id):
    bg_verify.finish(store, job_id, status="failed", error="Lost: check run cr-1 lost: lost twice: process gone")


def _requeues(store, job_id):
    return [e for e in store.events(job_id) if e.what == "verify_requeued"]


def test_poll_reports_lost_not_crashed(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r")
    bg_verify.start(store, j.job_id, "/tmp/r", "s", spawn=lambda argv: None)
    bg_verify.finish(store, j.job_id, status="failed", error="Lost: check run cr-1 lost: superseded by cr-2")
    status, _, reason = bg_verify.poll(store, j.job_id, "s")
    assert status == "lost" and "superseded" in reason


def test_poll_reports_a_vanished_process_as_lost(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r")
    bg_verify.start(store, j.job_id, "/tmp/r", "s", spawn=lambda argv: None)
    _vanish(store, j.job_id)
    status, _, reason = bg_verify.poll(store, j.job_id, "s")
    assert status == "lost" and reason.startswith(bg_verify.VANISHED)


def test_poll_keeps_a_real_crash_crashed_and_a_moved_candidates_failure_stale(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r")
    bg_verify.start(store, j.job_id, "/tmp/r", "s", spawn=lambda argv: None)
    bg_verify.finish(store, j.job_id, status="failed", error="RuntimeError: worktree add failed")
    assert bg_verify.poll(store, j.job_id, "s")[0] == "crashed"
    status, _, reason = bg_verify.poll(store, j.job_id, "s-newer")
    assert status == "stale" and "worktree add failed" in reason


def test_lost_requeues_twice_then_asks(steward_home):
    store = Store(steward_home)
    job, runner, sr, spawned = _bg_job(store)
    runner.tick(job.job_id)
    assert len(spawned) == 1

    _vanish(store, job.job_id)
    first = runner.tick(job.job_id)
    assert first.state == "WORKER_FINISHED" and len(spawned) == 2
    _lost_by_check_run(store, job.job_id)
    second = runner.tick(job.job_id)
    assert second.state == "WORKER_FINISHED" and len(spawned) == 3
    ev = _requeues(store, job.job_id)
    assert [e.evidence["n"] for e in ev] == [1, 2] and all(e.judgment == Judgment.code for e in ev)
    assert "vanished" in ev[0].evidence["reason"] and ev[1].evidence["reason"].startswith("Lost:")
    assert store.kv_get(job.job_id, runner_module.VERIFY_REQUEUES_KEY) == 2
    assert not any(e.rule == "bg_verify_unverified" for e in store.events(job.job_id))

    _vanish(store, job.job_id)
    third = runner.tick(job.job_id)
    assert third.state == "NEEDS_HUMAN" and len(spawned) == 3
    park = next(e for e in store.events(job.job_id) if e.rule == "bg_verify_lost")
    assert park.what == "background verification lost twice; tests unverified" and park.judgment == Judgment.code
    assert len(_requeues(store, job.job_id)) == 2
    ownership.reconcile_job(store, job.job_id, now=1e9)                 # a reviewable park, not an owner request yet
    assert ownership.owner_view(store, job.job_id).status == "awaiting_technical_review"

    asked = []                                                          # ... and the ladder's next step is the ask
    out = liveness.remedy_default(store, store.get_job(job.job_id), now=time.time(), settings=_LADDER,
                                  orphan_since=time.time() - 1800,
                                  ask_fn=lambda st, j, **kw: asked.append(kw["tried"]) or "b-1")
    assert out["rung"] == "ask" and len(asked) == 1


def test_two_racing_ticks_requeue_one_loss_once(steward_home):
    store = Store(steward_home)
    job, runner, sr, spawned = _bg_job(store)
    runner.tick(job.job_id)
    _vanish(store, job.job_id)
    assert runner._lost_requeue(job.job_id, "sha-1", "vanished") == "requeued"
    assert runner._lost_requeue(job.job_id, "sha-1", "vanished") == "raced"     # the other tick polled it too
    assert store.kv_get(job.job_id, runner_module.VERIFY_REQUEUES_KEY) == 1
    assert len(_requeues(store, job.job_id)) == 1


def test_a_late_racing_tick_never_counts_the_rerun_it_did_not_poll(steward_home):
    store = Store(steward_home)
    job, runner, sr, spawned = _bg_job(store)
    runner.tick(job.job_id)
    _vanish(store, job.job_id)
    polled = bg_verify.record(store, job.job_id)["started_at"]                  # both ticks polled this lost run
    assert runner._lost_requeue(job.job_id, "sha-1", "vanished", started_at=polled) == "requeued"
    bg_verify.start(store, job.job_id, "/tmp/r", "sha-1", spawn=lambda argv: None, now=polled + 10)  # the re-run
    assert runner._lost_requeue(job.job_id, "sha-1", "vanished", started_at=polled) == "raced"
    assert bg_verify.record(store, job.job_id)["status"] == "running"            # the fresh run is untouched
    assert store.kv_get(job.job_id, runner_module.VERIFY_REQUEUES_KEY) == 1


def test_a_consumed_verification_resets_the_loss_budget(steward_home):
    store = Store(steward_home)
    job, runner, sr, spawned = _bg_job(store)
    runner.tick(job.job_id)
    _vanish(store, job.job_id)
    runner.tick(job.job_id)
    assert store.kv_get(job.job_id, runner_module.VERIFY_REQUEUES_KEY) == 1
    bg_verify.finish(store, job.job_id, status="done", verification=Verification(commit_sha="sha-1"))
    runner.tick(job.job_id)
    assert store.kv_get(job.job_id, runner_module.VERIFY_REQUEUES_KEY) == 0


def test_a_plain_crash_still_parks_crashed(steward_home):
    store = Store(steward_home)
    job, runner, sr, spawned = _bg_job(store)
    runner.tick(job.job_id)
    bg_verify.finish(store, job.job_id, status="failed", error="RuntimeError: worktree add failed")
    assert runner.tick(job.job_id).state == "NEEDS_HUMAN"
    assert any(e.rule == "bg_verify_unverified" for e in store.events(job.job_id))
    assert not _requeues(store, job.job_id)


@pytest.mark.legacy_ownership_off
def test_legacy_mode_keeps_the_crashed_path_for_a_loss(steward_home):
    store = Store(steward_home)
    job, runner, sr, spawned = _bg_job(store)
    runner.tick(job.job_id)
    _vanish(store, job.job_id)
    assert runner.tick(job.job_id).state == "NEEDS_HUMAN"
    assert store.get_job(job.job_id).state == JobState.NEEDS_HUMAN and not _requeues(store, job.job_id)
