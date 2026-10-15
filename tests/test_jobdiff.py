"""Issue K: every reporter counts the job's OWN change, not upstream commits merged into its branch."""
import os
import subprocess
from pathlib import Path

import pytest

from steward import api as api_module, diffscan, evidence, followup, incorporate, jobdiff
from steward.runner_exec import SubprocessRunner  # noqa: F401  (real runner for diffscan)
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
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo


@pytest.fixture
def merged_job(scratch, tmp_path, monkeypatch):
    store = Store(tmp_path / "home")
    job = store.create_job("Add two files", str(scratch))
    wt = store.home / "worktrees" / job.job_id
    wt.parent.mkdir(parents=True, exist_ok=True)
    base = git(scratch, "rev-parse", "HEAD")
    git(scratch, "worktree", "add", "-q", "-b", f"steward/{job.job_id}", str(wt), "HEAD")
    store.add_check(job.job_id, commit_sha=base, command="pytest", exit_code=0, results={}, label="baseline")
    (wt / "j1.txt").write_text("1\n2\n3\n")
    (wt / "j2.txt").write_text("x\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "job change")
    for i in range(5):
        (scratch / f"up{i}.txt").write_text("u\n" * 10)
    git(scratch, "add", "-A")
    git(scratch, "commit", "-qm", "upstream")
    git(wt, "merge", "-q", "--no-edit", "main")  # the job branch absorbs upstream
    return store, job, wt


def _check_job_only(store, job, wt):
    assert evidence._numstat(store, job.job_id) == (4, 0, ["j1.txt", "j2.txt"])
    assert followup.changed_files(store, job) == (4, 0, ["j1.txt", "j2.txt"])
    summary = api_module.StewardAPI(store).get_diff_summary(job.job_id)
    assert sorted(summary["files"]) == ["j1.txt", "j2.txt"]
    assert (summary["insertions"], summary["deletions"]) == (4, 0)
    base = jobdiff.job_diff_base(store, job)[0]
    sample = diffscan.scan(wt, base, SubprocessRunner())
    assert sample.files == ["j1.txt", "j2.txt"] and sample.changed_lines == 4


def test_unincorporated_reports_only_job_files(merged_job):
    store, job, wt = merged_job
    _check_job_only(store, job, wt)


def test_incorporated_reports_merge_own_change(merged_job, scratch):
    store, job, wt = merged_job
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = 'ACCEPTED' WHERE job_id = ?", (job.job_id,))
    r = incorporate.incorporate(store, job.job_id)
    assert r["accepted"]
    # the merge brings only j1/j2 into main (upstream was already there)
    assert evidence._numstat(store, job.job_id) == (4, 0, ["j1.txt", "j2.txt"])
    assert followup.changed_files(store, job) == (4, 0, ["j1.txt", "j2.txt"])
    rng, cwd = jobdiff.job_diff_range(store, job)
    assert rng.endswith(f"..{r['merge_commit']}") and cwd == Path(job.repo)
