import subprocess
import time
from datetime import datetime, timezone

import pytest

from steward import housekeeping
from steward.models import Judgment
from steward.runner_exec import SubprocessRunner
from steward.store import Store
from steward.worktree import create_worktree

DAY = 86400
NOW = 2_000_000_000.0


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_USER_LIBRARY", "0")
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    store = Store(tmp_path / "home")
    return repo, store, tmp_path / "home" / "worktrees"


def make_job(env, state, age_days, *, merged_days=None, dirty=False, commit=False):
    repo, store, base = env
    job = store.create_job("g", str(repo))
    wt = create_worktree(repo, job.job_id, SubprocessRunner(), base_dir=base)
    if commit:
        (wt / "work.txt").write_text("w\n")
        git(wt, "add", "-A")
        git(wt, "commit", "-qm", "work")
    if dirty:
        (wt / "scratch.txt").write_text("untracked\n")
        (wt / "a.txt").write_text("modified\n")
    ts = datetime.fromtimestamp(NOW - age_days * DAY, timezone.utc).isoformat()
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=?, updated_at=? WHERE job_id=?", (state, ts, job.job_id))
    if merged_days is not None:
        git(repo, "merge", "--no-ff", "-qm", "merge", f"steward/{job.job_id}")
        store.kv_set(job.job_id, "incorporated_commit", git(repo, "rev-parse", "HEAD"))
        with store._conn:
            store._conn.execute(
                "INSERT INTO events (job_id, ts, what, evidence, rule, judgment) VALUES (?,?,?,?,?,?)",
                (job.job_id, datetime.fromtimestamp(NOW - merged_days * DAY, timezone.utc).isoformat(),
                 "incorporated", "{}", "t", "code"))
    return job.job_id, wt


def has_branch(repo, job_id):
    return bool(git(repo, "branch", "--list", f"steward/{job_id}"))


def test_only_eligible_worktrees_removed_and_live_untouched(env):
    repo, store, base = env
    merged_old, wt_m = make_job(env, "RELEASED", 3, merged_days=2)
    merged_new, wt_mn = make_job(env, "RELEASED", 1, merged_days=0.2)
    accepted_old, wt_a = make_job(env, "ACCEPTED", 8)
    accepted_new, wt_an = make_job(env, "ACCEPTED", 6)
    stopped, wt_s = make_job(env, "STOPPED", 9)
    failed, wt_f = make_job(env, "FAILED", 10)
    running, wt_r = make_job(env, "RUNNING", 30)
    needs, wt_n = make_job(env, "NEEDS_HUMAN", 30)
    stranger = base / "not-a-job"
    stranger.mkdir()
    (stranger / "keep.txt").write_text("x")
    # a dir named for a terminal job but not a worktree git knows about
    orphan = store.create_job("g", str(repo))
    with store._conn:
        store._conn.execute("UPDATE jobs SET state='FAILED', updated_at=? WHERE job_id=?",
                            (datetime.fromtimestamp(NOW - 40 * DAY, timezone.utc).isoformat(), orphan.job_id))
    (base / orphan.job_id).mkdir()

    report = housekeeping.sweep(store, now=NOW)

    assert not report.errors
    for gone in (wt_m, wt_a, wt_s, wt_f):
        assert not gone.exists()
    for kept in (wt_mn, wt_an, wt_r, wt_n, stranger, base / orphan.job_id):
        assert kept.exists()
    assert {a.job_id for a in report.worktrees} == {merged_old, accepted_old, stopped, failed}
    assert "worktree_removed" in [e.what for e in store.events(stopped)]
    assert "1 errors" not in report.summary() and "GB freed" in report.summary()
    assert housekeeping.sweep(store, now=NOW, dry_run=True).worktrees == []


def test_dry_run_changes_nothing(env):
    repo, store, base = env
    job, wt = make_job(env, "STOPPED", 9, dirty=True)
    report = housekeeping.sweep(store, now=NOW, dry_run=True)
    assert [a.job_id for a in report.worktrees] == [job] and report.worktrees[0].archived
    assert wt.exists() and (wt / "scratch.txt").exists()
    assert git(wt, "log", "--format=%s", "-1") == "init"
    assert "worktree_removed" not in [e.what for e in store.events(job)]


def test_dirty_worktree_archived_on_branch_before_removal(env):
    repo, store, base = env
    job, wt = make_job(env, "STOPPED", 9, dirty=True, commit=True)
    housekeeping.sweep(store, now=NOW)
    assert not wt.exists()
    branch = f"steward/{job}"
    assert git(repo, "log", "--format=%s", "-1", branch) == housekeeping.ARCHIVE_MESSAGE
    assert git(repo, "show", f"{branch}:scratch.txt") == "untracked"
    assert git(repo, "show", f"{branch}:a.txt") == "modified"
    assert git(repo, "show", f"{branch}:work.txt") == "w"  # earlier commit intact
    ev = [e for e in store.events(job) if e.what == "worktree_removed"][0]
    assert ev.evidence["archived_uncommitted"] is True


def test_clean_worktree_gets_no_archive_commit(env):
    repo, store, base = env
    job, wt = make_job(env, "FAILED", 9, commit=True)
    housekeeping.sweep(store, now=NOW)
    assert git(repo, "log", "--format=%s", "-1", f"steward/{job}") == "work"


def test_unmerged_branches_survive_and_merged_branch_deleted_after_30_days(env):
    repo, store, base = env
    unmerged, _ = make_job(env, "STOPPED", 400, commit=True)
    merged_old, _ = make_job(env, "RELEASED", 40, merged_days=35)
    merged_mid, _ = make_job(env, "RELEASED", 40, merged_days=10)
    report = housekeeping.sweep(store, now=NOW)
    assert has_branch(repo, unmerged)
    assert has_branch(repo, merged_mid)  # merged < 30 days ago
    assert not has_branch(repo, merged_old)
    assert [a.job_id for a in report.branches] == [merged_old]
    assert "branch_deleted" in [e.what for e in store.events(merged_old)]


def test_merged_branch_kept_when_git_says_not_fully_merged(env):
    repo, store, base = env
    # a branch that kv calls merged but git does not (squash-style) must survive
    job2 = store.create_job("g", str(repo))
    wt2 = create_worktree(repo, job2.job_id, SubprocessRunner(), base_dir=base)
    (wt2 / "new.txt").write_text("n")
    git(wt2, "add", "-A")
    git(wt2, "commit", "-qm", "unmerged commit")
    store.kv_set(job2.job_id, "incorporated_commit", "deadbeef")
    ts = datetime.fromtimestamp(NOW - 40 * DAY, timezone.utc).isoformat()
    with store._conn:
        store._conn.execute("UPDATE jobs SET state='RELEASED', updated_at=? WHERE job_id=?", (ts, job2.job_id))
    housekeeping.sweep(store, now=NOW)
    assert not wt2.exists()
    assert has_branch(repo, job2.job_id)


def test_sweep_runs_at_most_once_a_day(env):
    repo, store, base = env
    job, wt = make_job(env, "STOPPED", 9)
    first = housekeeping.maybe_sweep(store, now=NOW)
    assert first is not None and len(first.worktrees) == 1
    job2, wt2 = make_job(env, "STOPPED", 9)
    assert housekeeping.maybe_sweep(store, now=NOW + DAY - 60) is None
    assert wt2.exists()
    second = housekeeping.maybe_sweep(store, now=NOW + DAY + 60)
    assert second is not None and not wt2.exists()


def test_housekeeping_cli_dry_run(env, monkeypatch, capsys):
    repo, store, base = env
    job, wt = make_job(env, "STOPPED", 9)
    from steward import cli
    monkeypatch.setenv("STEWARD_HOME", str(store.home))
    monkeypatch.setattr(housekeeping.time, "time", lambda: NOW)
    assert cli.main(["housekeeping", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would remove 1 worktrees" in out and wt.exists()
