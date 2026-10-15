"""Plan 4 Task 5: equivalent verifications share one run; attribution stays per job (spec §8; P4-3/4/5/8/27/30)."""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import pytest

from steward import autopilot, check_runs as cr, verify as verify_mod
from steward.discovery import DiscoveredCommand, Discovery
from steward.runner_exec import CommandResult, SubprocessRunner
from steward.store import Store

SUITE = "python3 -m pytest -q"
DISC = Discovery(commands=[DiscoveredCommand(SUITE, "pyproject.toml", "check", "pytest")])
_JUNIT = re.compile(r"--junitxml=(\S+)")



@pytest.fixture(autouse=True)
def _one_heavy_slot(monkeypatch):
    """These tests stage a busy heavy slot; with the multi-slot default one busy run would not make anyone queue."""
    monkeypatch.setenv(cr.HEAVY_SLOTS_ENV, "1")

def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class GitThenScripted:
    """Real git; every check line writes a junit with `outcomes` and exits 1 when any test failed."""

    def __init__(self, outcomes):
        self.outcomes, self.executed, self._git = outcomes, [], SubprocessRunner()

    def run(self, argv, cwd, *, timeout=None, env=None):
        return self._git.run(argv, cwd, timeout=timeout, env=env)

    def shell_line(self, line, cwd, *, timeout=None, env=None):
        self.executed.append(line)
        m = _JUNIT.search(line)
        if m:
            cases = "".join(f'<testcase classname="t" name="{n.split("::")[-1]}">{"<failure/>" if o == "failed" else ""}</testcase>'
                            for n, o in self.outcomes.items())
            Path(m.group(1)).write_text(f"<testsuite>{cases}</testsuite>")
        red = any(o == "failed" for o in self.outcomes.values())
        return CommandResult(argv=["bash", "-lc", line], cwd=str(cwd), exit_code=1 if red else 0,
                             stdout="1 failed" if red else "2 passed", stderr="", duration_s=0.0)


def two_worktrees_one_tree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "a@b")
    git(repo, "config", "user.name", "a")
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_text("x = 2\n")
    git(repo, "commit", "-qam", "change")
    wa, wb = tmp_path / "wa", tmp_path / "wb"
    git(repo, "worktree", "add", "-q", "--detach", str(wa), "HEAD")
    git(repo, "worktree", "add", "-q", "--detach", str(wb), "HEAD")
    return base, wa, wb


def test_default_check_fn_is_unchanged_run_check(steward_home, tmp_path, monkeypatch):
    store = Store(steward_home)
    jid = store.create_job("g", "/r").job_id
    base, wa, _ = two_worktrees_one_tree(tmp_path)
    seen = []
    real = verify_mod.run_check
    monkeypatch.setattr(verify_mod, "run_check", lambda *a, **k: seen.append(a[1]) or real(*a, **k))
    verify_mod.verify(store, jid, wa, DISC, GitThenScripted({"t::a": "passed"}))
    assert seen == [SUITE] and store.get_checks(jid, label="verify")


def test_two_jobs_same_tree_share_one_run_and_attribute_separately(steward_home, tmp_path):
    store = Store(steward_home)
    ja, jb = (store.create_job("g", "/r").job_id for _ in range(2))
    base, wa, wb = two_worktrees_one_tree(tmp_path)
    store.add_check(ja, commit_sha=base, command=SUITE, exit_code=1, results={"t::x": "failed"}, label="baseline")
    store.add_check(jb, commit_sha=base, command=SUITE, exit_code=0, results={"t::x": "passed"}, label="baseline")
    runner = GitThenScripted({"t::x": "failed", "t::y": "passed"})
    va = verify_mod.verify(store, ja, wa, DISC, runner, check_fn=cr.shared_check_fn(store, ja, purpose="verify"))
    vb = verify_mod.verify(store, jb, wb, DISC, runner, check_fn=cr.shared_check_fn(store, jb, purpose="verify"))
    assert len([l for l in runner.executed if l.startswith(SUITE)]) == 1
    assert va.preexisting_failures == ["t::x"] and va.new_failures == []
    assert vb.new_failures == ["t::x"] and vb.preexisting_failures == []
    assert store.get_checks(ja, label="verify") and store.get_checks(jb, label="verify")


def test_shared_run_keeps_per_job_baseline_attribution(steward_home, tmp_path):
    store = Store(steward_home)
    ja = store.create_job("g", "/r").job_id
    base, wa, _ = two_worktrees_one_tree(tmp_path)
    runner = GitThenScripted({"t::x": "failed"})
    v = verify_mod.verify(store, ja, wa, DISC, runner, check_fn=cr.shared_check_fn(store, ja, purpose="verify"))
    assert v.new_failures == ["t::x"] and not v.unattributed  # no baseline rows and no running bg baseline


def test_merged_tree_identical_to_verified_tree_reuses_receipt(steward_home, tmp_path):
    store = Store(steward_home)
    ja = store.create_job("g", "/r").job_id
    base, wa, wb = two_worktrees_one_tree(tmp_path)
    runner = GitThenScripted({"t::y": "passed"})
    verify_mod.verify(store, ja, wa, DISC, runner, check_fn=cr.shared_check_fn(store, ja, purpose="verify"))
    red = autopilot._run_checks(wb, runner, [SUITE], check_fn=cr.shared_check_fn(store, ja, purpose="merged_tree"))
    assert red is None and len([l for l in runner.executed if l.startswith(SUITE)]) == 1
    run_id = store._conn.execute("SELECT run_id FROM check_runs").fetchone()[0]
    assert ("merged_tree", "reused") in {(w["purpose"], w["how"]) for w in cr.waiters(store, run_id)}


def test_red_merged_tree_through_check_fn_is_reported_red(steward_home, tmp_path):
    store = Store(steward_home)
    ja = store.create_job("g", "/r").job_id
    base, wa, _ = two_worktrees_one_tree(tmp_path)
    red = autopilot._run_checks(wa, GitThenScripted({"t::x": "failed"}), [SUITE],
                                check_fn=cr.shared_check_fn(store, ja, purpose="merged_tree"))
    assert red and "merged tree is red" in red


def test_baseline_under_repo_lock_still_queues_for_the_heavy_slot(steward_home, tmp_path, monkeypatch):
    from steward import bg_baseline

    store = Store(steward_home)
    ja, jo = (store.create_job("g", "/r").job_id for _ in range(2))
    base, wa, _ = two_worktrees_one_tree(tmp_path)
    store.kv_set(ja, bg_baseline.KEY, {"status": "running", "sha": base, "repo": str(wa), "started_at": 1.0})
    busy, _ = cr.request(store, cr.RunSpec("OTHER", SUITE, "e", "tc", True, 900.0), job_id=jo, purpose="verify",
                         now=time.time())
    cr.try_start(store, busy.run_id, owner="elsewhere", pid=None, now=time.time())
    real_shared, seen = cr.shared_check_fn, []
    monkeypatch.setattr(cr, "shared_check_fn", lambda st, jid, **kw: real_shared(st, jid, **{**kw, "wait": False}))

    def fake_run_baseline(st, jid, tree, discovery, runner, *, check_fn):
        try:
            check_fn(runner, SUITE, tree, tmp_path / "b.xml", "")
        except cr.Queued as q:  # inside the repo lock, the baseline still waits its turn for the heavy slot
            seen.append(q.position)

    assert bg_baseline.run(store, ja, GitThenScripted({}), discover_fn=lambda p: DISC,
                           run_baseline_fn=fake_run_baseline, lock_dir=tmp_path / "locks") == "done"
    assert seen == [2]


def test_inline_verify_that_must_queue_goes_to_background(steward_home, monkeypatch):
    from steward import runner as runner_mod
    from test_bg_verify import HeadRunner
    from test_stability import _done_obs, _finish_job, _runner

    store = Store(steward_home)
    job = _finish_job(store)
    other = store.create_job("other", "/r").job_id
    busy, _ = cr.request(store, cr.RunSpec("OTHER", SUITE, "e", "tc", True, 900.0), job_id=other,
                         purpose="verify", now=time.time())
    cr.try_start(store, busy.run_id, owner="elsewhere", pid=None, now=time.time())
    monkeypatch.setattr(runner_mod, "discover_repo", lambda wt: DISC)
    spawned = []
    runner = _runner(store, HeadRunner(), [[_done_obs()]])
    runner.bg_verify_spawn = lambda argv: spawned.append(argv)
    runner.start(job.job_id)
    report = runner.tick(job.job_id)
    assert report.state == "WORKER_FINISHED" and len(spawned) == 1
    assert any(e.what == "verification queued for the heavy slot; continuing in background"
               for e in store.events(job.job_id))
