"""Control's incorporate never parks on the merge lock; the lock records its holder; after-accept is detached."""

import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from steward import autopilot, autopilot_bg, cli, health, incorporate as inc
from steward.models import JobState
from steward.runner_exec import SubprocessRunner
from steward.store import Store

_ENV = {"GIT_AUTHOR_NAME": "Sam", "GIT_AUTHOR_EMAIL": "c@x", "GIT_COMMITTER_NAME": "Sam", "GIT_COMMITTER_EMAIL": "c@x"}


def git(cwd, *a):
    r = subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True, env={**os.environ, **_ENV})
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def scratch(tmp_path):
    repo = tmp_path / "src"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Sam")
    git(repo, "config", "user.email", "c@x")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo


def mkjob(store, repo, tmp_path, files):
    job = store.create_job("Add the thing to the repo", str(repo))
    wt = tmp_path / f"wt-{job.job_id[:6]}"
    git(repo, "worktree", "add", "-q", "-b", f"steward/{job.job_id}", str(wt), "HEAD")
    for name, content in files.items():
        (wt / name).write_text(content)
        git(wt, "add", "-A")
        git(wt, "commit", "-qm", f"job edits {name}")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.ACCEPTED.value, job.job_id))
    return job


def test_target_lock_timeout_raises_busy_with_holder(scratch):
    store = Store()
    ready, release = threading.Event(), threading.Event()

    def hold():
        with inc.target_lock(store, scratch, purpose="test holder", job_id="j"):
            ready.set()
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    ready.wait(5)
    try:
        start = time.monotonic()
        with pytest.raises(inc.LockBusy) as ei:
            with inc.target_lock(store, scratch, timeout=0.3):
                pass
        assert time.monotonic() - start < 3
        assert ei.value.holder["purpose"] == "test holder" and ei.value.holder["pid"] == os.getpid()
        assert "busy: merge lock held by test holder" in str(ei.value)
    finally:
        release.set()
        t.join()
    assert inc.lock_holder(inc.lock_path(store, scratch)) == {}  # truncated on release


def test_waiter_open_does_not_truncate_holder_record(scratch):
    store = Store()
    path = inc.lock_path(store, scratch)
    with inc.target_lock(store, scratch, purpose="first"):
        assert inc.lock_holder(path)["purpose"] == "first"
        code = ("import sys; from steward import incorporate as i; from steward.store import Store\n"
                "try:\n    with i.target_lock(Store(), sys.argv[1], timeout=0.3): pass\n"
                "except i.LockBusy as e: print(e.holder.get('purpose'))\n")
        out = subprocess.run([sys.executable, "-c", code, str(scratch)], capture_output=True, text=True,
                             env={**os.environ, "STEWARD_HOME": str(store.home), "PYTHONPATH": str(Path(__file__).resolve().parent.parent)})
        assert out.stdout.strip() == "first", out
        assert inc.lock_holder(path)["purpose"] == "first"


def test_control_incorporate_answers_busy_within_seconds(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl, os, sys, time, json\n"
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT); fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "os.write(fd, json.dumps({'pid': os.getpid(), 'purpose': 'land abcd1234', 'since': time.time()}).encode())\n"
        "print('up', flush=True); time.sleep(30)\n"), str(inc.lock_path(store, scratch))], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "up"
        handlers = cli._build_control_handlers(store)
        start = time.monotonic()
        r = handlers["incorporate"]({"op": "incorporate", "args": {"job_id": job.job_id}})
        assert time.monotonic() - start < 7
        assert not r["accepted"] and r["error_kind"] == "merge_lock_busy"
        assert "land abcd1234" in r["detail"] and f"pid {holder.pid}" in r["detail"]
        assert "incorporate_blocked" not in [e.what for e in store.events(job.job_id)]
    finally:
        holder.kill()
        holder.wait()


def test_after_accept_from_control_never_runs_inline_when_prediction_unknown(scratch, tmp_path, monkeypatch):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    monkeypatch.setenv(autopilot_bg.ENV, "1")
    calls = []
    monkeypatch.setattr(autopilot_bg, "start", lambda s, j: calls.append("start") or {"status": "started"})
    monkeypatch.setattr(autopilot_bg, "run_inline", lambda *a, **k: calls.append("inline") or {})
    assert autopilot_bg.bg_verify.predicted_s(store, job.job_id) is None  # a never-measured job
    with autopilot_bg.detached_only():
        autopilot.after_accept(store, job.job_id, SubprocessRunner(), notify=lambda *a: None)
    assert calls == ["start"]


def test_health_reports_stale_delivery_sweep(scratch, tmp_path):
    store = Store()
    mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    now = time.time()
    store.kv_set("_delivery", "delivery_sweep_at", now - 400)
    m = health.lock_metrics(store, now=now)
    assert m["level"] == "red" and m["delivery_sweep_age_s"] >= 399
    store.kv_set("_delivery", "delivery_sweep_at", now - 10)
    assert health.lock_metrics(store, now=now)["level"] == "ok"


def test_health_reports_stranded_index_lock(scratch, tmp_path):
    store = Store()
    mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    lock = Path(git(scratch, "rev-parse", "--absolute-git-dir")) / "index.lock"
    lock.write_text("")
    os.utime(lock, (time.time() - 700, time.time() - 700))
    m = health.lock_metrics(store)
    assert m["level"] == "red" and m["index_lock_age_s"] >= 699


def test_control_accept_handler_runs_after_accept_detached(scratch, tmp_path, monkeypatch):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.NEEDS_HUMAN.value, job.job_id))
    seen = []
    monkeypatch.setattr(autopilot, "after_accept", lambda st, jid, runner, **kw: seen.append(autopilot_bg.needed(st, jid, runner)))
    cli._build_control_handlers(store)["accept"]({"op": "accept", "args": {"job_id": job.job_id}})
    assert seen == [True]
