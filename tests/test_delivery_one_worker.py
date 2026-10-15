"""Latency fix #4 + #7 (Job C): one delivery worker per job, backoff for transient retries, and no worker for a root
whose live repair attempt is already on it."""

import os
import threading
import time
from types import SimpleNamespace

from steward import autopilot, delivery, repair_episode
from steward.models import JobState
from steward.runner_exec import SubprocessRunner
from steward.store import Store
from test_incorporate import git, mkjob, scratch  # noqa: F401 - scratch is a fixture


def accepted(store, scratch, tmp_path, name="c.txt"):
    return mkjob(store, scratch, tmp_path, {name: "c\n"}).job_id


def set_state(store, jid, state):
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, jid))


def move_head(scratch, n):
    (scratch / f"moved{n}.txt").write_text(str(n))
    git(scratch, "add", "-A")
    git(scratch, "commit", "-qm", f"move {n}")


def test_sweeps_spawn_exactly_one_worker_while_it_lives_even_when_head_moves(steward_home, scratch, tmp_path,
                                                                             monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    jid = accepted(store, scratch, tmp_path)
    spawned = []

    def spawn(*args):
        spawned.append(args)
        return os.getpid()  # a live pid: the worker is running

    for n in range(4):
        delivery.sweep(store, now=1000.0 + 31 * n, spawn=spawn)
        move_head(scratch, n)
    assert len(spawned) == 1


def test_key_does_not_include_target_head(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    jid = accepted(store, scratch, tmp_path)
    step = {"kind": "incorporate", "revision": "abc"}
    before = delivery._delivery_key(jid, step, str(scratch))
    move_head(scratch, 1)
    assert delivery._delivery_key(jid, step, str(scratch)) == before


def test_two_jobs_track_liveness_independently(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    a = accepted(store, scratch, tmp_path, "a1.txt")
    b = accepted(store, scratch, tmp_path, "b1.txt")
    spawned = []

    def spawn(jid, key, owner):
        spawned.append(jid)
        return os.getpid()

    delivery.sweep(store, now=1000.0, spawn=spawn)
    delivery.sweep(store, now=1031.0, spawn=spawn)
    assert sorted(spawned) == sorted([a, b])
    assert delivery._worker_alive(store, a, 1031.0) and delivery._worker_alive(store, b, 1031.0)


def test_second_worker_for_same_job_exits_at_once(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    jid = accepted(store, scratch, tmp_path)
    fd = delivery._take_worker_lock(store, jid)
    assert fd is not None
    called = []
    monkeypatch.setattr(delivery, "_incorporate_step", lambda *a, **k: called.append(1) or {})
    try:
        t0 = time.monotonic()
        result = delivery.run_delivery(store, jid, SubprocessRunner(), now=1000.0)
    finally:
        os.close(fd)
    assert result["outcome"] == "duplicate" and not called and time.monotonic() - t0 < 5
    assert "delivery_worker_duplicate" in [e.what for e in store.events(jid)]
    assert delivery.run_delivery(store, jid, SubprocessRunner(), now=1000.0).get("outcome") != "duplicate"


def test_worker_for_released_job_exits_before_any_lock_wait(steward_home, scratch, tmp_path, monkeypatch):
    import fcntl

    from steward import incorporate as inc

    store = Store(steward_home)
    jid = accepted(store, scratch, tmp_path)
    set_state(store, jid, JobState.RELEASED)
    held = os.open(autopilot.test_slot_path(store, scratch), os.O_RDWR | os.O_CREAT)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        with inc.target_lock(store, scratch):  # same-thread re-entry would not block; the flock holders above would
            t0 = time.monotonic()
            result = delivery.run_delivery(store, jid, SubprocessRunner(), now=1000.0)
    finally:
        os.close(held)
    assert result["reason"] == "job is no longer accepted" and time.monotonic() - t0 < 5


def test_slot_waiter_leaves_when_job_is_released_mid_wait(steward_home, scratch, tmp_path, monkeypatch):
    import fcntl

    monkeypatch.setattr(autopilot, "SLOT_POLL_S", 0.05)
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    held = os.open(autopilot.test_slot_path(store, scratch), os.O_RDWR | os.O_CREAT)
    fcntl.flock(held, fcntl.LOCK_EX)

    def allowed():
        cur = store.get_job(job.job_id)
        return None if cur.state == JobState.ACCEPTED else {"outcome": "skipped", "detail": "released"}

    timer = threading.Timer(0.3, lambda: set_state(Store(steward_home), job.job_id, JobState.RELEASED))
    timer.start()
    try:
        t0 = time.monotonic()
        r = autopilot.merge_when_green(store, job, SubprocessRunner(), [], still_allowed=allowed)
    finally:
        timer.join()
        os.close(held)
    assert r["outcome"] == "skipped" and time.monotonic() - t0 < 5


def test_transient_retry_backs_off_instead_of_respawning(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    monkeypatch.setattr(delivery, "_incorporate_step",
                        lambda *a, **k: {"step": "incorporate", "outcome": "retry", "transient": True, "reason": "raced"})
    store = Store(steward_home)
    jid = accepted(store, scratch, tmp_path)
    spawned = []

    def spawn(j, key, owner):
        spawned.append((key, owner))
        return 99999999  # dead pid

    delivery.sweep(store, now=1000.0, spawn=spawn)
    key, owner = spawned[0]
    delivery.run_delivery(store, jid, SubprocessRunner(), now=1005.0, intent_key=key, owner=owner)
    second = delivery.sweep(store, now=1031.0, spawn=spawn)  # inside the 30 s backoff that began at 1005
    assert len(spawned) == 1 and second["backoff"] and second["launched"] == []
    third = delivery.sweep(store, now=1070.0, spawn=spawn)
    assert len(spawned) == 2 and spawned[1][0] == key and third["launched"]


def attempt_root(store, scratch, tmp_path, child_state):
    root = accepted(store, scratch, tmp_path)
    child = store.create_job("repair", str(scratch))
    set_state(store, child.job_id, child_state)
    store.kv_set(child.job_id, "autopilot_followup", root)
    ep = repair_episode.ensure(store, root, repo=str(scratch), target_branch="main", now=1.0)
    repair_episode.record_attempt(store, ep.episode_id, child.job_id, start_ref=None, conflicts=["a.txt"],
                                  digest="d", now=2.0)
    return root, child.job_id


def test_live_repair_attempt_spawns_no_worker(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    root, child = attempt_root(store, scratch, tmp_path, JobState.RUNNING)
    spawned = []
    out = delivery.sweep(store, now=1000.0, spawn=lambda *a: spawned.append(a) or 1)
    step = delivery.next_step(store, root)
    assert spawned == [] and out["launched"] == []
    assert step["kind"] == "wait" and child[:8] in step["reason"] and "needs" not in step


def test_summary_names_needs_human_attempt(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    root, child = attempt_root(store, scratch, tmp_path, JobState.NEEDS_HUMAN)
    step = delivery.next_step(store, root)
    s = delivery.summary(store, root)
    assert step["needs"] == f"owner decision on {child[:8]}"
    assert child[:8] in s["text"] and s["needs"] == step["needs"]


def test_autopilot_blocked_reports_episode_attempts(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(repair_episode, "enabled", lambda: True)
    store = Store(steward_home)
    root, child = attempt_root(store, scratch, tmp_path, JobState.RUNNING)
    pf = SimpleNamespace(source_branch="main", target_head=None)
    autopilot._failed(store, store.get_job(root), pf, "merge conflict", ["a.txt"], SubprocessRunner(), None, None)
    ev = [e for e in store.events(root) if e.what == "autopilot_blocked"][-1].evidence
    assert ev["repair_attempts"] == 1 and ev["current_attempt"] == child and ev["attempt_state"] == "RUNNING"
    assert ev["follow_ups_so_far"] == 0
