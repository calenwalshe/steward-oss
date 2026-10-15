"""Delivery latency fix #2/#5 (2026-10-07): the merged-tree test runs with NO merge lock held, the lock is held only
to land, a target that moved during the test is re-tested (strict re-test), and delivery reports `incorporated` only
when a merge commit exists."""
from __future__ import annotations

import fcntl
import os
import threading
import time
from types import SimpleNamespace

import pytest

from steward import autopilot, blockers, check_runs, delivery, intents
from steward import incorporate as inc
from steward.models import JobState
from steward.runner_exec import CommandResult, FakeRunner, SubprocessRunner
from steward.store import Store
from test_incorporate import git, mkjob, scratch, whats  # noqa: F401 - scratch is a fixture


def lock_is_free(store, repo) -> bool:
    """True when ANOTHER thread can take target_lock within 0.5 s and a fresh fd can flock the lock file."""
    got = {}

    def other():
        try:
            with inc.target_lock(Store(store.home), repo, timeout=0.5, purpose="probe"):
                got["ok"] = True
        except inc.LockBusy:
            got["ok"] = False

    t = threading.Thread(target=other)
    t.start()
    t.join(5)
    path = inc.lock_path(store, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        flock_ok = True
    except BlockingIOError:
        flock_ok = False
    finally:
        os.close(fd)
    return bool(got.get("ok")) and flock_ok


def suite(on_test=None, red=False):
    """A FakeRunner whose `bash -lc` check calls `on_test(n)` (n = 1, 2, ...) before answering."""
    calls = []

    def respond(argv, cwd):
        if argv[:2] == ["bash", "-lc"]:
            calls.append(str(cwd))
            if on_test is not None:
                on_test(len(calls))
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=1 if red else 0, stdout="", stderr="",
                                 duration_s=0.0)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)
    r = FakeRunner(respond)
    r.tests = calls
    return r


def go(store, job, r, **kw):
    return autopilot.try_merge(store, job.job_id, r, checks=["pytest -q"], **kw)


def commit_on_main(repo, name, text="x\n"):
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-qm", f"main moves: {name}")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def auto(monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)


# -- the lock is free during the test --------------------------------------------------------------------------------

def test_suite_runs_without_merge_lock(steward_home, scratch, tmp_path, auto):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    free = []
    res = go(store, job, suite(lambda n: free.append(lock_is_free(store, scratch))))
    assert res["outcome"] == "merged" and free == [True]


def test_delivery_suite_and_heavy_slot_wait_run_without_merge_lock(steward_home, scratch, tmp_path, auto, monkeypatch):
    store = Store(steward_home)
    jid = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"}).job_id
    seen = []

    def fake_shared(_store, job_id, *, purpose, **kw):
        def check_fn(runner, line, cwd, xml_path, python_env=""):
            seen.append((purpose, lock_is_free(store, scratch)))  # stands in for the heavy-slot wait + the suite
            return SimpleNamespace(exit_code=0, broken=None, tests={"t": "passed"})
        return check_fn

    monkeypatch.setattr(check_runs, "shared_check_fn", fake_shared)
    monkeypatch.setattr(autopilot, "_check_lines", lambda tree: ["pytest -q"])
    result = delivery.run_delivery(store, jid, SubprocessRunner(), now=1000.0)
    assert result["outcome"] == "incorporated" and seen == [("merged_tree", True)]


def test_post_merge_verify_runs_outside_lock(steward_home, scratch, tmp_path, auto, monkeypatch):
    store = Store(steward_home)
    jid = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"}).job_id
    seen = []

    def fake_shared(_store, job_id, *, purpose, **kw):
        def check_fn(runner, line, cwd, xml_path, python_env=""):
            seen.append((purpose, lock_is_free(store, scratch)))
            return SimpleNamespace(exit_code=0, broken=None, tests={})
        return check_fn

    monkeypatch.setattr(check_runs, "shared_check_fn", fake_shared)
    monkeypatch.setattr(autopilot, "_check_lines", lambda tree: ["pytest -q"])
    monkeypatch.setattr(autopilot, "_test_merged_tree", lambda *a, **k: (True, "not-the-merged-tree", []))
    result = delivery.run_delivery(store, jid, SubprocessRunner(), now=1000.0)
    assert result["outcome"] == "incorporated"
    assert seen == [("post_merge", True)] and result["receipt"]["post_merge_verification"]["green"]


def test_conflict_followup_spawned_outside_lock(steward_home, scratch, tmp_path, auto, monkeypatch):
    store = Store(steward_home)
    jid = mkjob(store, scratch, tmp_path, {"a.txt": "job side\n"}).job_id
    commit_on_main(scratch, "a.txt", "main side\n")
    seen = []
    monkeypatch.setattr(autopilot, "_failed", lambda store_, cand, pf, detail, files, *a: seen.append(
        (detail, files, lock_is_free(store, scratch))) or {"outcome": "followup", "detail": detail})
    result = delivery.run_delivery(store, jid, SubprocessRunner(), now=1000.0)
    assert result["outcome"] == "followup" and seen == [("merge conflict", ["a.txt"], True)]


def test_autopilot_lock_held_only_for_land(steward_home, scratch, tmp_path, auto, monkeypatch):
    """Deterministic (no wall-clock bound): the slow verify step advances a fake clock by 2 s and is logged against the
    lock's acquire/release, so a lock held across the verify shows up as ordering AND as held time, never as load."""
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    now = {"t": 1000.0}
    order, held, real = [], [], inc.target_lock
    locked = {"n": 0}

    class FakeTime:
        def __getattr__(self, name):
            return getattr(time, name)

        @staticmethod
        def monotonic():
            return now["t"]

    from contextlib import contextmanager

    @contextmanager
    def logged(*a, **k):
        with real(*a, **k):
            order.append("acquire")
            locked["n"] += 1
            t0 = now["t"]
            try:
                yield
            finally:
                held.append(now["t"] - t0)
                locked["n"] -= 1
                order.append("release")

    def slow_verify(n):
        order.append(("verify", locked["n"]))
        now["t"] += 2.0  # the verify step "takes" 2 s on the clock the land timer reads

    monkeypatch.setattr(autopilot, "time", FakeTime())
    monkeypatch.setattr(inc, "target_lock", logged)
    res = go(store, job, suite(slow_verify))
    assert res["outcome"] == "merged"
    # the verify ran unlocked and before any acquire; everything after it is the land (incorporate re-enters the lock)
    assert order[0] == ("verify", 0) and set(order[1:]) == {"acquire", "release"}
    assert held and all(h == 0.0 for h in held)  # none of the verify's 2 s fell inside any lock hold
    (landed,) = [e for e in store.events(job.job_id) if e.what == "merge_landed"]
    assert landed.evidence["outcome"] == "merged" and landed.evidence["lock_held_s"] == 0.0


# -- the target moves during the test --------------------------------------------------------------------------------

def test_head_moved_different_tree_retests_and_merges_on_new_head(steward_home, scratch, tmp_path, auto):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    moved = {}
    r = suite(lambda n: moved.setdefault("head", commit_on_main(scratch, "other.txt")) if n == 1 else None)
    res = go(store, job, r)
    assert res["outcome"] == "merged" and len(r.tests) == 2  # tested twice: once per target HEAD
    assert git(scratch, "rev-parse", f"{res['merge_commit']}^1") == moved["head"]
    retest = [e for e in store.events(job.job_id) if e.what == "merge_retest"]
    assert len(retest) == 1 and retest[0].evidence["head"] == moved["head"]
    assert (scratch / "c.txt").exists() and (scratch / "other.txt").exists()


def test_head_moved_same_tree_lands_without_retest(steward_home, scratch, tmp_path, auto):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})

    def empty_commit(n):
        git(scratch, "commit", "-q", "--allow-empty", "-m", "same tree")

    r = suite(empty_commit)
    res = go(store, job, r)
    assert res["outcome"] == "merged" and len(r.tests) == 1 and "merge_retest" not in whats(store, job)
    (rec,) = delivery.receipts(store, job.job_id, "incorporation")
    assert rec["evidence"]["tested_tree_matches"] is True


def test_branch_moved_after_test_retests(steward_home, scratch, tmp_path, auto):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    wt = tmp_path / f"wt-{job.job_id[:6]}"

    def more_work(n):
        if n == 1:
            (wt / "d.txt").write_text("d\n")
            git(wt, "add", "-A")
            git(wt, "commit", "-qm", "late commit")

    r = suite(more_work)
    res = go(store, job, r)
    assert res["outcome"] == "merged" and len(r.tests) == 2 and (scratch / "d.txt").exists()
    assert [e.evidence["reason"] for e in store.events(job.job_id) if e.what == "merge_retest"] == ["job branch moved"]


def test_rounds_exhausted_returns_raced(steward_home, scratch, tmp_path, auto):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    head = git(scratch, "rev-parse", "HEAD")
    r = suite(lambda n: commit_on_main(scratch, f"o{n}.txt"))
    res = go(store, job, r)
    assert res["outcome"] == "raced" and len(r.tests) == autopilot.MAX_MERGE_ROUNDS
    assert inc.is_incorporated(store, job.job_id) is None and "incorporate_blocked" not in whats(store, job)
    assert git(scratch, "rev-list", "--count", f"{head}..HEAD") == str(autopilot.MAX_MERGE_ROUNDS)  # only main's moves
    assert "merge_raced" in whats(store, job)


def test_delivery_raced_is_retry_and_abandons_intent(steward_home, scratch, tmp_path, auto, monkeypatch):
    store = Store(steward_home)
    jid = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"}).job_id
    n = {"i": 0}

    def fake_shared(_store, job_id, *, purpose, **kw):
        def check_fn(runner, line, cwd, xml_path, python_env=""):
            n["i"] += 1
            commit_on_main(scratch, f"o{n['i']}.txt")
            return SimpleNamespace(exit_code=0, broken=None, tests={})
        return check_fn

    monkeypatch.setattr(check_runs, "shared_check_fn", fake_shared)
    monkeypatch.setattr(autopilot, "_check_lines", lambda tree: ["pytest -q"])
    assert intents.claim(store, key="k1", job_id=jid, kind="delivery-worker", lease_s=900, now=1000.0, owner="o")
    result = delivery.run_delivery(store, jid, SubprocessRunner(), now=1000.0, intent_key="k1", owner="o")
    assert result["outcome"] == "retry" and result["transient"] and inc.is_incorporated(store, jid) is None
    assert intents.get(store, "k1").state == "abandoned"


# -- honest outcomes -------------------------------------------------------------------------------------------------

def test_blocked_incorporate_reports_blocked_and_waits(steward_home, scratch, tmp_path, auto, monkeypatch):
    store = Store(steward_home)
    jid = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"}).job_id
    head = git(scratch, "rev-parse", "HEAD")
    monkeypatch.setattr(autopilot, "_check_lines", lambda tree: [])
    monkeypatch.setattr(inc, "incorporate", lambda store_, job_id, **kw: {
        "accepted": False, "error_kind": "incorporate_blocked", "detail": "blocked: git merge failed (boom)"})
    result = delivery.run_delivery(store, jid, SubprocessRunner(), now=1000.0)
    assert result["outcome"] == "blocked" and "boom" in result["reason"]
    assert inc.is_incorporated(store, jid) is None and git(scratch, "rev-parse", "HEAD") == head
    (w,) = [b for b in blockers.open_blockers(store, jid) if b.kind == delivery.WAITING_KIND]
    assert "boom" in w.claim
    finished = [e for e in store.events(jid) if e.what == "delivery_step_finished"]
    assert finished[-1].evidence["outcome"] == "blocked"


def test_index_lock_block_reports_retry_and_reschedules(steward_home, scratch, tmp_path, auto, monkeypatch):
    store = Store(steward_home)
    jid = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"}).job_id
    lock = scratch / ".git" / "index.lock"

    def fake_shared(_store, job_id, *, purpose, **kw):
        def check_fn(runner, line, cwd, xml_path, python_env=""):
            lock.write_text("")  # someone's git takes the index lock while the suite runs (fresh: not healed)
            return SimpleNamespace(exit_code=0, broken=None, tests={})
        return check_fn

    monkeypatch.setattr(check_runs, "shared_check_fn", fake_shared)
    monkeypatch.setattr(autopilot, "_check_lines", lambda tree: ["pytest -q"])
    assert intents.claim(store, key="k2", job_id=jid, kind="delivery-worker", lease_s=900, now=1000.0, owner="o")
    result = delivery.run_delivery(store, jid, SubprocessRunner(), now=1000.0, intent_key="k2", owner="o")
    assert result["outcome"] == "retry" and "index.lock" in result["reason"] and inc.is_incorporated(store, jid) is None
    assert intents.get(store, "k2").state == "abandoned" and lock.exists()


def test_noop_merge_is_already_incorporated_not_incorporated(steward_home, scratch, tmp_path, auto, monkeypatch):
    store = Store(steward_home)
    jid = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"}).job_id
    monkeypatch.setattr(autopilot, "_check_lines", lambda tree: ["pytest -q"])

    def fake_shared(_store, job_id, *, purpose, **kw):
        def check_fn(runner, line, cwd, xml_path, python_env=""):
            git(scratch, "merge", "-q", "--no-ff", "--no-edit", f"steward/{jid}")  # the owner merged it by hand
            return SimpleNamespace(exit_code=0, broken=None, tests={})
        return check_fn

    monkeypatch.setattr(check_runs, "shared_check_fn", fake_shared)
    result = delivery.run_delivery(store, jid, SubprocessRunner(), now=1000.0)
    assert result["outcome"] == "already_incorporated" and result["receipt"]["noop"]


def test_expected_head_mismatch_records_no_incorporate_blocked(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    head = git(scratch, "rev-parse", "HEAD")
    r = inc.incorporate(store, job.job_id, expected_head="0" * 40)
    assert r["accepted"] is False and r["error_kind"] == "moved" and "target HEAD" in r["detail"]
    r = inc.incorporate(store, job.job_id, expected_head=head, expected_branch="0" * 40)
    assert r["error_kind"] == "moved" and "job branch" in r["detail"]
    assert git(scratch, "rev-parse", "HEAD") == head and "incorporate_blocked" not in whats(store, job)
    assert inc.is_incorporated(store, job.job_id) is None
    branch = git(scratch, "rev-parse", f"steward/{job.job_id}")
    ok = inc.incorporate(store, job.job_id, expected_head=head, expected_branch=branch)
    assert ok["accepted"] and git(scratch, "rev-parse", f"{ok['merge_commit']}^2") == branch


def test_released_during_test_does_not_merge(steward_home, scratch, tmp_path, auto):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})

    def release(n):
        with store._conn:
            store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.RELEASED.value, job.job_id))

    res = go(store, job, suite(release))
    assert res["outcome"] == "skipped" and inc.is_incorporated(store, job.job_id) is None


def test_land_lock_busy_is_busy_not_blocked(steward_home, scratch, tmp_path, auto, monkeypatch):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    monkeypatch.setattr(autopilot, "LAND_LOCK_WAIT_S", 0.3)
    hold, release = threading.Event(), threading.Event()

    def holder():
        with inc.target_lock(Store(steward_home), scratch, purpose="someone else"):
            hold.set()
            release.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    hold.wait(5)
    try:
        res = go(store, job, suite())
    finally:
        release.set()
        t.join(5)
    assert res["outcome"] == "busy" and "someone else" in res["detail"]
    assert inc.is_incorporated(store, job.job_id) is None and "incorporate_blocked" not in whats(store, job)
