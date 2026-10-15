"""Autopilot must reach a terminal event after `merged_tree_verification_needed`, across runner deaths (2026-10-05).

Replays of ed40c142 (06:56Z) and c1ecb696 (08:45Z): accepted, `autopilot_attempt via=ensure`, reuse declined
("merged tree differs from the tested tree"), the ~50 min merged-tree test started inside the runner, the runner went
away mid-test, and nothing followed: no auto_merged / autopilot_blocked / escalated event, ever."""

import subprocess
import time

import pytest

from steward import autopilot, autopilot_bg, bg_baseline
from steward import incorporate as inc
from steward.models import JobState
from steward.runner_exec import CommandResult, FakeRunner, SubprocessRunner
from steward.store import Store
from test_incorporate import git, mkjob, scratch  # noqa: F401  (scratch is a fixture)

CHECKS = ["pytest -q"]


class RunnerDied(BaseException):
    """The runner process vanishing mid-test (exec on HEAD move, tab closed, killed): nothing in-process catches it."""


def green(sleep_s=0.0):
    def respond(argv, cwd):
        if argv[:2] == ["bash", "-lc"] and sleep_s:
            time.sleep(sleep_s)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="3 passed", stderr="", duration_s=0.0)
    return FakeRunner(respond)


def dies_mid_test():
    def respond(argv, cwd):
        if argv[:2] == ["bash", "-lc"]:
            raise RunnerDied()
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)
    return FakeRunner(respond)


def whats(store, job):
    return [e.what for e in store.events(job.job_id)]


def after(store, job, what):
    """Events after the first `what`."""
    w = whats(store, job)
    return w[w.index(what) + 1:] if what in w else []


def _stall(store, job, monkeypatch):
    """Reproduce the stall: the attempt is consumed, the merged-tree test starts, the owning runner dies."""
    with pytest.raises(RunnerDied):
        autopilot.ensure_attempted(store, job.job_id, dies_mid_test(), checks=CHECKS)
    w = whats(store, job)
    ordered = [w.index(event) for event in
               ("autopilot_attempt", "merged_tree_verification_needed", "merged_tree_verification_started")]
    assert ordered == sorted(ordered) and len(set(ordered)) == 3
    assert not set(w) & set(autopilot_bg.TERMINAL)  # the bug: nothing terminal followed
    assert autopilot_bg.record(store, job.job_id)["status"] == "running"
    monkeypatch.setattr(autopilot_bg, "_TOKEN", "after-execv")  # the runner re-exec'd (same pid, new process image)


def in_process_spawn(store, runner, calls):
    def spawn(argv):
        calls.append(argv)
        autopilot_bg.run(store, argv[-1], runner, checks=CHECKS)  # the detached `-m steward.autopilot_bg JOB` body
    return spawn


def test_ed40c142_runner_dies_mid_merged_tree_test_and_the_next_ensure_resumes_to_auto_merged(
        steward_home, scratch, tmp_path, monkeypatch):
    store, calls = Store(steward_home), []
    job = mkjob(store, scratch, tmp_path, {"new.txt": "x\n"})
    _stall(store, job, monkeypatch)
    monkeypatch.setattr(bg_baseline, "_spawn_detached", in_process_spawn(store, green(), calls))
    # a restarted runner reaches _terminal_exit -> ensure_attempted again: the attempt is NOT silently consumed
    res = autopilot.ensure_attempted(store, job.job_id, green(), checks=CHECKS)
    assert res["outcome"] == "pending" and calls and calls[0][-2:] == ["steward.autopilot_bg", job.job_id]
    tail = after(store, job, "merged_tree_verification_started")
    tail = [event for event in tail if event != "autopilot_progress"]
    assert tail[:3] == ["autopilot_run_lost", "autopilot_resumed", "autopilot_background_started"]
    assert "auto_merged" in tail and (scratch / "new.txt").exists()
    rec = autopilot_bg.record(store, job.job_id)
    assert rec["status"] == "done" and rec["outcome"] == "merged" and rec["resumes"] == 1
    assert autopilot.ensure_attempted(store, job.job_id, green(), checks=CHECKS) is None  # done: never again


def test_c1ecb696_operator_incorporates_while_the_test_runs_then_the_sweep_closes_it_with_a_reason(
        steward_home, scratch, tmp_path, monkeypatch):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"new.txt": "x\n"})
    _stall(store, job, monkeypatch)
    receipt = inc.incorporate(store, job.job_id)  # the operator merges by hand and releases; the runner is gone
    assert receipt["accepted"]
    store.set_state(job.job_id, JobState.RELEASED, what="operator released", evidence={}, rule="t",
                    judgment=autopilot.Judgment.human)
    spawned = []
    monkeypatch.setattr(bg_baseline, "_spawn_detached", spawned.append)
    assert autopilot_bg.sweep_all(store, green()) == [job.job_id]  # the control pane's tick backstop
    assert not spawned  # nothing left to merge: no resume
    skip = [e for e in store.events(job.job_id) if e.what == "autopilot_skipped"]
    assert len(skip) == 1 and "incorporated meanwhile" in skip[0].evidence["reason"]
    assert autopilot_bg.record(store, job.job_id)["status"] == "abandoned"
    assert autopilot_bg.sweep_all(store, green()) == []  # closed once


def test_long_suite_runs_detached_so_the_runner_returns_at_once_with_progress_events(
        steward_home, scratch, tmp_path, monkeypatch):
    store, spawned = Store(steward_home), []
    job = mkjob(store, scratch, tmp_path, {"new.txt": "x\n"})
    store.kv_set(job.job_id, "verify_duration_s", 2777.1)  # c1ecb696's measured suite time
    monkeypatch.setattr(bg_baseline, "_spawn_detached", lambda argv: spawned.append(argv) or 4242)
    res = autopilot.ensure_attempted(store, job.job_id, SubprocessRunner(), checks=CHECKS)  # nothing runs in-process
    assert res["outcome"] == "pending" and spawned[0][-3:] == ["-m", "steward.autopilot_bg", job.job_id]
    rec = autopilot_bg.record(store, job.job_id)
    assert rec["status"] == "starting" and rec["pid"] == 4242
    monkeypatch.setattr(bg_baseline, "_alive", lambda pid: pid == 4242)
    assert autopilot_bg.resume_if_orphaned(store, job.job_id) is None  # starting, owner alive: leave it be
    # the detached process body (it takes ownership, heartbeats, and always ends terminal)
    autopilot_bg.run(store, job.job_id, green(sleep_s=0.3), checks=CHECKS, progress_every_s=0.05)
    w = whats(store, job)
    assert {"merged_tree_verification_started", "merged_tree_verification_finished", "autopilot_progress",
            "auto_merged"} <= set(w)
    prog = [e for e in store.events(job.job_id) if e.what == "autopilot_progress"]
    assert prog[0].evidence["phase"] in ("merging", "merged_tree_verification_started")
    assert any(e.evidence["phase"] == "merged_tree_verification_started" for e in prog)
    assert autopilot_bg.record(store, job.job_id)["status"] == "done"


def test_resumes_are_bounded_then_escalated_with_the_reason(steward_home, scratch, tmp_path, monkeypatch):
    store, notes = Store(steward_home), []
    job = mkjob(store, scratch, tmp_path, {"new.txt": "x\n"})
    dead = subprocess.Popen(["true"])
    dead.wait()
    now = time.time()
    store.kv_set(job.job_id, autopilot_bg.KEY, {"status": "running", "pid": dead.pid, "mode": "background",
                                                "phase": "merged_tree_verification_started", "started_at": now,
                                                "heartbeat_at": now, "resumes": autopilot_bg.MAX_RESUMES})
    monkeypatch.setattr(bg_baseline, "_spawn_detached", lambda argv: pytest.fail("must not resume again"))
    res = autopilot_bg.resume_if_orphaned(store, job.job_id, green(), notify=lambda *a: notes.append(a))
    assert res["outcome"] == "escalated" and "died during merged_tree_verification_started" in res["detail"]
    esc = [e for e in store.events(job.job_id) if e.what == "autopilot_escalated"]
    assert len(esc) == 1 and f"after {autopilot_bg.MAX_RESUMES} resumes" in esc[0].evidence["reason"]
    assert notes and notes[0][1] == "NEEDS_YOU"
    assert autopilot_bg.record(store, job.job_id)["status"] == "escalated"


def test_owner_identity_live_self_reused_pid_and_stale_heartbeat(steward_home, monkeypatch):
    import os

    now = time.time()
    me = {"status": "running", "pid": os.getpid(), "token": autopilot_bg._TOKEN, "started_at": now, "heartbeat_at": now}
    assert autopilot_bg.owner_alive(me)
    assert not autopilot_bg.owner_alive({**me, "token": "an-earlier-exec"})
    assert not autopilot_bg.owner_alive({**me, "heartbeat_at": now - autopilot_bg.STALE_S - 1})
    other = {**me, "pid": 1, "token": "x", "pid_start": "not-init's-start-time"}
    assert not autopilot_bg.owner_alive(other)  # the pid was reused by another process


def test_a_return_without_any_terminal_event_gets_one(steward_home, scratch, tmp_path, monkeypatch):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"new.txt": "x\n"})
    monkeypatch.setattr(autopilot, "try_merge", lambda *a, **k: {"outcome": "skipped", "detail": "autopilot merge is off"})
    autopilot.ensure_attempted(store, job.job_id, green())
    skip = [e for e in store.events(job.job_id) if e.what == "autopilot_skipped"]
    assert len(skip) == 1 and skip[0].evidence["reason"] == "autopilot merge is off"


def test_merged_tree_scratch_lives_under_the_steward_home_not_tmp(steward_home, scratch, tmp_path):
    store, seen = Store(steward_home), []

    def respond(argv, cwd):
        if argv[:2] == ["bash", "-lc"]:
            seen.append(cwd)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)
    job = mkjob(store, scratch, tmp_path, {"new.txt": "x\n"})
    assert autopilot.try_merge(store, job.job_id, FakeRunner(respond), checks=CHECKS)["outcome"] == "merged"
    assert seen and seen[0].startswith(str(steward_home / "tmp"))


def test_a_hung_merged_tree_check_times_out_into_blocked_and_escalated_without_a_fixup(
        steward_home, scratch, tmp_path):
    store, timeouts, notes = Store(steward_home), [], []

    def respond(argv, cwd):
        if argv[:2] == ["bash", "-lc"]:
            raise subprocess.TimeoutExpired(argv, autopilot.CHECK_TIMEOUT_S)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)
    r = FakeRunner(respond)
    job = mkjob(store, scratch, tmp_path, {"new.txt": "x\n"})
    res = autopilot.try_merge(store, job.job_id, r, checks=CHECKS, notify=lambda *a: notes.append(a),
                              start_followup=lambda *a: pytest.fail("a hang is not a model fix-up"))
    timeouts = [c["timeout"] for c in r.calls if c["argv"][:2] == ["bash", "-lc"]]
    assert timeouts == [autopilot.CHECK_TIMEOUT_S] and autopilot.CHECK_TIMEOUT_S >= 2 * 50 * 60  # sized for ~50 min
    assert res["outcome"] == "escalated" and res["detail"].startswith(autopilot.TIMED_OUT)
    w = whats(store, job)
    tail = [x for x in w if x.startswith(("merged_tree", "autopilot"))]
    assert tail[-3:] == ["merged_tree_verification_finished", "autopilot_blocked", "autopilot_escalated"]
    assert notes and notes[0][1] == "NEEDS_YOU" and not (scratch / "new.txt").exists()


def test_a_scripted_runner_keeps_a_long_merge_in_process_so_no_real_process_escapes(
        steward_home, scratch, tmp_path, monkeypatch):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"new.txt": "x\n"})
    store.kv_set(job.job_id, "baseline_duration_s", 2200.0)  # replay 14305aeb's prediction
    monkeypatch.setattr(bg_baseline, "_spawn_detached", lambda argv: pytest.fail(f"real spawn: {argv}"))
    res = autopilot.ensure_attempted(store, job.job_id, green(), checks=CHECKS)
    assert res["outcome"] == "merged" and autopilot_bg.record(store, job.job_id)["mode"] == "inline"
