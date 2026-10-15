"""Lock hygiene: read-only git never takes optional index locks, and a stranded `.git/index.lock` self-heals."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from steward import gitenv, incorporate as inc
from steward.models import JobState
from steward.runner_exec import SubprocessRunner
from steward.store import Store

_REPO = Path(__file__).resolve().parent.parent
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


def _lock(repo, age_s=0.0):
    lock = Path(git(repo, "rev-parse", "--absolute-git-dir")) / "index.lock"
    lock.write_text("")
    t = time.time() - age_s
    os.utime(lock, (t, t))
    return lock


def whats(store, job):
    return [e.what for e in store.events(job.job_id)]


def test_import_sets_optional_locks_default():
    env = {k: v for k, v in os.environ.items() if k != "GIT_OPTIONAL_LOCKS"}
    env["PYTHONPATH"] = str(_REPO)
    cmd = [sys.executable, "-c", "import steward, os; print(os.environ.get('GIT_OPTIONAL_LOCKS'))"]
    assert subprocess.run(cmd, env=env, capture_output=True, text=True).stdout.strip() == "0"
    env["GIT_OPTIONAL_LOCKS"] = "1"
    assert subprocess.run(cmd, env=env, capture_output=True, text=True).stdout.strip() == "1"


def test_merged_keeps_none_and_caller_values():
    assert gitenv.merged(None) is None
    assert gitenv.merged({"A": "b"}) == {"A": "b", "GIT_OPTIONAL_LOCKS": "0"}
    assert gitenv.merged({"GIT_OPTIONAL_LOCKS": "1"})["GIT_OPTIONAL_LOCKS"] == "1"


def test_subprocess_runner_explicit_env_gets_optional_locks_off(tmp_path):
    res = SubprocessRunner().run(["sh", "-c", "echo $GIT_OPTIONAL_LOCKS"], tmp_path, env={"PATH": os.environ["PATH"]})
    assert res.stdout.strip() == "0"


def test_status_with_env_does_not_rewrite_index(scratch):
    idx = scratch / ".git" / "index"
    (scratch / "a.txt").write_text("changed\n")
    t = time.time() - 100
    os.utime(scratch / "a.txt", (t, t))
    before = idx.stat().st_mtime_ns
    time.sleep(0.05)
    r = subprocess.run(["git", "status"], cwd=scratch, capture_output=True, text=True,
                       env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    assert r.returncode == 0 and idx.stat().st_mtime_ns == before


def test_dash_script_exports_optional_locks():
    text = (_REPO / "steward" / "templates" / "cockpit" / "steward-dash.sh").read_text()
    assert "export GIT_OPTIONAL_LOCKS=0" in text


def test_heal_quarantines_stale_lock_and_merge_succeeds(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    lock = _lock(scratch, 120)
    r = inc.incorporate(store, job.job_id)
    assert r["accepted"] and r.get("merge_commit"), r
    assert not lock.exists()
    assert list(lock.parent.glob("index.lock.stale-*"))
    assert whats(store, job).count("index_lock_healed") == 1


def test_heal_refuses_fresh_lock(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    lock = _lock(scratch, 5)
    r = inc.incorporate(store, job.job_id)
    assert not r["accepted"] and "index.lock held: fresh" in r["detail"]
    assert lock.exists() and "index_lock_healed" not in whats(store, job)
    assert "incorporate_blocked" in whats(store, job)


def test_heal_refuses_lock_open_in_live_process(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    lock = _lock(scratch, 120)
    holder = subprocess.Popen([sys.executable, "-c", f"import time; f=open({str(lock)!r}); print('up', flush=True); time.sleep(30)"],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "up"
        r = inc.incorporate(store, job.job_id)
        assert not r["accepted"] and f"held by pid {holder.pid}" in r["detail"]
        assert lock.exists()
    finally:
        holder.kill()
        holder.wait()


def test_heal_retries_merge_only_once(scratch, tmp_path, monkeypatch):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    merges = []
    real = inc._git

    def fake(repo, *args):
        if args[:1] == ("merge",) and "--abort" not in args:
            merges.append(1)
            return subprocess.CompletedProcess(args, 128, "", "fatal: Unable to create '.git/index.lock': File exists.")
        return real(repo, *args)

    monkeypatch.setattr(inc, "_git", fake)
    monkeypatch.setattr(inc, "heal_index_lock", lambda *a, **k: {"healed": True})
    r = inc.incorporate(store, job.job_id)
    assert not r["accepted"] and len(merges) == 2


def test_heal_absent_lock_reports_absent(scratch):
    assert inc.heal_index_lock(Store(), "j", scratch) == {"healed": False, "reason": "absent"}
