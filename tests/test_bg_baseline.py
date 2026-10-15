"""Background baseline (latency #5): `new` dispatches immediately; the baseline runs at the pinned sha in an
isolated temp worktree (a fake spawn, then the real detached body in-process); verification waits only while
it is genuinely running."""

from __future__ import annotations

import csv
import subprocess
from pathlib import Path

from fixtures.repos import python_repo

from steward import bg_baseline, cli
from steward.contract import load_contract
from steward.runner_exec import SubprocessRunner
from steward.store import Store

DATA = Path(__file__).resolve().parent / "fixtures" / "latency"


def _head(repo):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def _new_bg(steward_home, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(bg_baseline.ENV, "1")
    spawned = []
    monkeypatch.setattr(bg_baseline, "_spawn_detached", spawned.append)
    repo = python_repo(tmp_path)
    assert cli.main(["new", "demo goal", "--repo", str(repo)]) == 0
    job_id = capsys.readouterr().out.split()[0]
    return Store(steward_home), job_id, repo, spawned


def test_new_returns_without_running_the_baseline(steward_home, tmp_path, monkeypatch, capsys):
    store, job_id, repo, spawned = _new_bg(steward_home, tmp_path, monkeypatch, capsys)
    rec = store.kv_get(job_id, bg_baseline.KEY)
    assert rec["status"] == "running" and rec["sha"] == _head(repo)
    assert store.get_checks(job_id, label="baseline") == []  # nothing ran synchronously
    assert spawned and spawned[0][-3:] == ["-m", "steward.bg_baseline", job_id]
    assert store.get_job(job_id).state.value == "CONTRACTED"
    assert any("background" in a for a in load_contract(store, job_id).assumptions)


def test_detached_body_runs_at_the_pinned_sha_in_an_isolated_worktree(steward_home, tmp_path, monkeypatch, capsys):
    store, job_id, repo, _ = _new_bg(steward_home, tmp_path, monkeypatch, capsys)
    pinned = _head(repo)
    # the repo moves on and its working copy gets dirty after the pin: neither may leak into the baseline
    (repo / "tests" / "test_sample.py").write_text("def test_ok():\n    assert True\n")
    subprocess.run(["git", "commit", "-qam", "later"], cwd=repo, check=True,
                   env={"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": "x",
                        "GIT_COMMITTER_EMAIL": "x@x", "PATH": "/usr/bin:/bin"})
    (repo / "scratch.txt").write_text("uncommitted\n")
    assert bg_baseline.run(store, job_id, SubprocessRunner()) == "done"
    rows = store.get_checks(job_id, label="baseline")
    assert rows and {r["commit_sha"] for r in rows} == {pinned}
    outcomes = set(rows[0]["results"].values())
    assert "failed" in outcomes  # the pinned commit still had the failing test
    assert store.kv_get(job_id, bg_baseline.KEY)["status"] == "done"
    worktrees = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout
    assert "steward-baseline-" not in worktrees  # temp worktree removed
    assert (repo / "scratch.txt").read_text() == "uncommitted\n"


def test_detached_entry_point(steward_home, tmp_path, monkeypatch, capsys):
    store, job_id, repo, _ = _new_bg(steward_home, tmp_path, monkeypatch, capsys)
    assert bg_baseline.main([job_id]) == 0
    assert bg_baseline.main([]) == 2
    assert store.kv_get(job_id, bg_baseline.KEY)["status"] == "done"


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_wait_blocks_only_while_running(steward_home):
    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    clock = _Clock()
    assert bg_baseline.wait(store, job.job_id, clock=clock, sleep=lambda d: None) == "none"
    bg_baseline.start(store, job.job_id, "/repo", "abc", spawn=lambda argv: None, now=clock.t)
    sleeps = []

    def sleep(d):
        sleeps.append(d)
        clock.t += d
        if len(sleeps) == 3:
            bg_baseline.finish(store, job.job_id, status="done")

    assert bg_baseline.wait(store, job.job_id, clock=clock, sleep=sleep) == "done"
    assert len(sleeps) == 3 and all(d <= 10 for d in sleeps)
    assert any(e.what == "verification waited for the background baseline" for e in store.events(job.job_id))


def test_wait_times_out_and_ignores_a_dead_run(steward_home):
    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    clock = _Clock()
    bg_baseline.start(store, job.job_id, "/repo", "abc", spawn=lambda argv: None, now=clock.t)

    def sleep(d):
        clock.t += d

    assert bg_baseline.wait(store, job.job_id, timeout_s=10, clock=clock, sleep=sleep) == "timeout"
    clock.t += bg_baseline.STALE_S
    assert bg_baseline.wait(store, job.job_id, clock=clock, sleep=sleep) == "stale"


def test_spawn_failure_is_recorded_and_never_blocks(steward_home):
    store = Store(steward_home)
    job = store.create_job("g", "/repo")

    def boom(argv):
        raise OSError("no fork")

    bg_baseline.start(store, job.job_id, "/repo", "abc", spawn=boom)
    assert store.kv_get(job.job_id, bg_baseline.KEY)["status"] == "failed"
    assert bg_baseline.wait(store, job.job_id) == "failed"


def test_verify_waits_for_the_background_baseline(steward_home, tmp_path, monkeypatch):
    from steward import verify as verify_module
    from steward.discovery import discover

    repo = python_repo(tmp_path)
    store = Store(steward_home)
    job = store.create_job("g", str(repo))
    calls = []
    monkeypatch.setattr(bg_baseline, "wait", lambda st, jid, **k: calls.append(jid) or "done")
    verify_module.verify(store, job.job_id, repo, discover(repo), SubprocessRunner())
    assert calls == [job.job_id]


def test_measured_dispatch_wait_this_removes():
    """data/pipeline.csv: jobs whose dispatch waited on a > 30 s baseline (0.94 h in the window)."""
    with open(DATA / "pipeline.csv", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["created_to_baseline_s"]]
    blocked = [float(r["created_to_baseline_s"]) for r in rows if float(r["created_to_baseline_s"]) > 30]
    assert len(blocked) >= 10 and sum(blocked) / 3600 > 0.5


def test_a_dead_baseline_process_is_never_waited_on(steward_home):
    import subprocess as sp

    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    proc = sp.Popen(["true"])
    proc.wait()
    bg_baseline.start(store, job.job_id, "/repo", "abc", spawn=lambda argv: proc.pid)
    assert store.kv_get(job.job_id, bg_baseline.KEY)["pid"] == proc.pid
    sleeps = []
    assert bg_baseline.wait(store, job.job_id, sleep=sleeps.append) == "stale" and sleeps == []


def test_detached_spawn_runs_from_the_package_root(monkeypatch):
    seen = {}

    class _Proc:
        pid = 4321

    def popen(argv, **kw):
        seen.update(kw, argv=argv)
        return _Proc()

    monkeypatch.setattr(bg_baseline.subprocess, "Popen", popen)
    assert bg_baseline._spawn_detached(["python3", "-m", "steward.bg_baseline", "J"]) == 4321
    assert (Path(seen["cwd"]) / "steward" / "bg_baseline.py").is_file() and seen["start_new_session"]


# --- 2026-10-04 job bf5237fe: a baseline that takes 45+ min must not time verification out at 900 s ------------

def test_wait_keeps_waiting_past_900s_while_the_baseline_pid_is_alive(steward_home):
    import os

    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    clock = _Clock()
    bg_baseline.start(store, job.job_id, "/repo", "abc", spawn=lambda argv: os.getpid(), now=clock.t)
    assert bg_baseline.WAIT_S == 900.0 and bg_baseline.STALE_S == 3600.0
    sleeps = []

    def sleep(d):
        sleeps.append(d)
        clock.t += d
        if clock.t - 1000.0 >= 2700:  # the baseline finishes 45 min in
            bg_baseline.finish(store, job.job_id, status="done")

    assert bg_baseline.wait(store, job.job_id, clock=clock, sleep=sleep) == "done"
    assert clock.t - 1000.0 >= 2700
    assert not any(e.what == "baseline wait timed out" for e in store.events(job.job_id))
    progress = [e for e in store.events(job.job_id) if e.what == "verification waiting for the background baseline"]
    assert len(progress) in (8, 9)  # one per 5 minutes (300..2400 s), not one per poll
    assert len(progress) < len(sleeps)


def test_wait_still_gives_up_at_the_stale_bound_for_a_live_but_endless_baseline(steward_home):
    import os

    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    clock = _Clock()
    bg_baseline.start(store, job.job_id, "/repo", "abc", spawn=lambda argv: os.getpid(), now=clock.t)

    def sleep(d):
        clock.t += d

    assert bg_baseline.wait(store, job.job_id, clock=clock, sleep=sleep) in ("timeout", "stale")
    assert clock.t - 1000.0 <= bg_baseline.STALE_S + bg_baseline.POLL_S
