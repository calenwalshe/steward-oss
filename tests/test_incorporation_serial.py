"""Plan 4 Task 8: one incorporation per target at a time on every path; the merged tree is what gets verified
(spec §9; P4-12/13/21)."""
from __future__ import annotations

import threading
import time

from steward import autopilot, blockers, delivery, incorporate, ownership
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store
from test_incorporate import git, mkjob, scratch, snap  # noqa: F401 - scratch is a fixture


def red_checks(exit_code=1):
    return FakeRunner(lambda argv, cwd: CommandResult(argv=argv, cwd=str(cwd), exit_code=exit_code, stdout="",
                                                      stderr="", duration_s=0.0))


def test_target_lock_is_reentrant_and_shared_with_autopilot(steward_home, scratch):
    store = Store(steward_home)
    with incorporate.target_lock(store, scratch):
        with incorporate.target_lock(store, scratch):
            assert incorporate.lock_path(store, scratch).exists()
    assert autopilot._repo_lock(store, str(scratch))[1] == incorporate.lock_path(store, scratch)


def test_human_and_autopilot_incorporation_into_one_target_are_serial(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    held, release, done = threading.Event(), threading.Event(), threading.Event()

    def holder():  # stands in for an autopilot merge in progress on the same target
        with incorporate.target_lock(store, scratch):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    result = {}
    h = threading.Thread(target=lambda: (result.update(incorporate.incorporate(Store(steward_home), job.job_id)),
                                         done.set()))
    h.start()
    time.sleep(0.3)
    assert not done.is_set()  # the human merge waits for the in-flight one
    release.set()
    h.join(5)
    t.join(5)
    assert done.is_set() and result["accepted"]


def test_human_incorporate_records_an_owner_receipt(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    r = incorporate.incorporate(store, job.job_id)
    (rec,) = delivery.receipts(store, job.job_id, "incorporation")
    assert rec["revision"] == r["merge_commit"] and rec["actor"].startswith("owner")
    assert rec["evidence"]["tested_tree_matches"] is False  # nothing verified this merged tree yet


def test_moved_target_merge_is_verified_post_merge_for_any_repo(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    monkeypatch.setattr(autopilot, "_test_merged_tree", lambda *a, **k: (True, "tree-tested-before-target-moved", []))
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    res = autopilot.try_merge(store, job.job_id, red_checks(0), check_runner=red_checks(1), checks=["make test"])
    assert res["outcome"] == "merged"
    (pm,) = delivery.receipts(store, job.job_id, "post_merge_verification")
    assert pm["revision"] == res["merge_commit"] and pm["evidence"]["green"] is False
    (red,) = [b for b in blockers.open_blockers(store, job.job_id) if b.kind == delivery.RED_KIND]
    assert red.disposition == "candidate"
    m = next(m for m in delivery.milestones(store, job.job_id) if m.name == "incorporation")
    assert m.status == "blocked" and not delivery.summary(store, job.job_id)["delivered"]


def test_matching_tree_needs_no_post_merge_run(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    checks = red_checks(0)
    res = autopilot.try_merge(store, job.job_id, checks, check_runner=checks, checks=["make test"])
    assert res["outcome"] == "merged"
    (rec,) = delivery.receipts(store, job.job_id, "incorporation")
    assert rec["evidence"]["tested_tree_matches"] is True
    assert delivery.receipts(store, job.job_id, "post_merge_verification") == []


def test_dirty_source_repo_waits_and_never_touches_owner_work(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"a.txt": "job edit\n"})
    (scratch / "a.txt").write_text("owner's uncommitted edit\n")
    before = snap(scratch)
    res = autopilot.try_merge(store, job.job_id, red_checks(0))
    assert res["outcome"] == "human" and snap(scratch) == before
    (w,) = [b for b in blockers.open_blockers(store, job.job_id) if b.kind == delivery.WAITING_KIND]
    assert w.disposition == "waiting_external" and "uncommitted" in w.claim
    git(scratch, "checkout", "--", "a.txt")
    res2 = ownership.reconcile_job(store, job.job_id, now=time.time())
    assert w.blocker_id in res2.cleared
