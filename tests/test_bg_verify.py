"""SUP-02: candidate verification beyond the tick budget runs in the background against the exact candidate SHA.

A moved SHA discards the result and re-runs it; new failures send the job back to RUNNING with the first failing
test named; a crashed run is unverified -> NEEDS_HUMAN with the reason (never a silent pass); the tick never blocks.
"""

from __future__ import annotations

import pytest

from steward import bg_baseline, bg_verify
from steward.models import JobState
from steward.steering import Steering
from steward.store import Store
from steward.verify import Verification
from test_finish_line_gate import TimeoutRunner, track_states
from ruling_support import StubLead, green
from test_stability import _done_obs, _events, _finish_job, _runner


class HeadRunner(TimeoutRunner):
    """git rev-parse answers `self.head` (the worker's current candidate); git status is clean."""

    def __init__(self, head="sha-1"):
        super().__init__()
        self.head = head

    def _respond(self, argv, cwd):
        if argv[:2] == ["git", "rev-parse"]:
            from test_stability import _res
            return _res(argv, cwd, 0, self.head + "\n")
        return super()._respond(argv, cwd)


def _slow_baseline(store, job_id, seconds=2200.0):
    store.kv_set(job_id, bg_baseline.KEY, {"status": "done", "sha": "base", "started_at": 1.0,
                                           "finished_at": 1.0 + seconds, "duration_s": seconds})


def _bg_job(store, *, head="sha-1", seconds=2200.0):
    job = _finish_job(store)
    _slow_baseline(store, job.job_id, seconds)
    sr = HeadRunner(head)
    runner = _runner(store, sr, [[_done_obs()] * 12])
    spawned: list[list[str]] = []
    runner.bg_verify_spawn = lambda argv: spawned.append(argv)  # no pid: liveness falls back to staleness
    runner.start(job.job_id)
    return job, runner, sr, spawned


def _complete(store, job_id, verification):
    """What the detached process records when it finishes."""
    bg_verify.finish(store, job_id, status="done", verification=verification)


# -- prediction -------------------------------------------------------------------------------------------


def test_needed_only_when_the_predicted_duration_exceeds_the_inline_budget(steward_home):
    store = Store(steward_home)
    job = _finish_job(store)
    assert not bg_verify.needed(store, job.job_id)  # nothing known: inline, as today
    _slow_baseline(store, job.job_id, 60.0)
    assert not bg_verify.needed(store, job.job_id)
    _slow_baseline(store, job.job_id, 2200.0)
    assert bg_verify.needed(store, job.job_id)
    assert not bg_verify.needed(store, job.job_id, env={"STEWARD_BG_VERIFY": "0"})


def test_measured_inline_verify_duration_also_predicts(steward_home):
    store = Store(steward_home)
    job = _finish_job(store)
    store.kv_set(job.job_id, bg_verify.DURATION_KEY, 900.0)
    assert bg_verify.predicted_s(store, job.job_id) == 900.0 and bg_verify.needed(store, job.job_id)


def test_verification_round_trips_through_the_store_record():
    v = Verification(commit_sha="abc", new_failures=["tests/t.py::a"], fixed=["x"], preexisting_failures=["p"],
                     broken=[{"command": "pytest", "exit": 4, "reason": "usage"}], docs_only=False)
    assert bg_verify.from_dict(bg_verify.to_dict(v)) == v


# -- the runner never blocks; results feed closeout unchanged ---------------------------------------------


def test_slow_candidate_verifies_in_background_and_the_tick_returns(steward_home):
    store = Store(steward_home)
    job, runner, sr, spawned = _bg_job(store)

    report = runner.tick(job.job_id)

    assert report.state == "WORKER_FINISHED" and "verifying in background" in report.detail
    assert len(spawned) == 1 and spawned[0][-2:] == ["steward.bg_verify", job.job_id]
    rec = bg_verify.record(store, job.job_id)
    assert rec["status"] == "running" and rec["sha"] == "sha-1"
    assert not _events(store, job, "verification recorded")  # nothing verified inline
    again = runner.tick(job.job_id)  # still running: the tick returns at once, no second spawn
    assert again.state == "WORKER_FINISHED" and len(spawned) == 1


def test_new_failures_send_the_job_back_with_the_first_failing_test_named(steward_home):
    store = Store(steward_home)
    job, runner, sr, spawned = _bg_job(store)
    runner.tick(job.job_id)
    _complete(store, job.job_id, Verification(commit_sha="sha-1",
                                               new_failures=["tests/test_owner_sweep.py::test_no_owner_name",
                                                             "tests/test_npm_audit.py::test_x"]))

    report = runner.tick(job.job_id)

    assert report.state == "RUNNING"
    steer = [i for i in Steering(store).all_instructions(job.job_id) if i["origin"] == "steward"][-1]
    assert "first failing test tests/test_owner_sweep.py::test_no_owner_name" in steer["text"]
    assert bg_verify.record(store, job.job_id)["status"] == "consumed"


def test_clean_background_result_takes_the_checks_passed_path(steward_home):
    store = Store(steward_home)
    job, runner, sr, spawned = _bg_job(store)
    states = track_states(store)
    runner.tick(job.job_id)
    runner.job_lead = StubLead(store)
    _complete(store, job.job_id, green("sha-1"))

    report = runner.tick(job.job_id)
    assert report.state == "CHECKS_PASSED"  # green, waiting for its one completion ruling
    report = runner.tick(job.job_id)

    assert "CHECKS_PASSED" in states and report.state == "ACCEPTED"
    assert "NEEDS_HUMAN" not in states


def test_moved_sha_discards_the_result_and_reruns_at_the_new_candidate(steward_home):
    store = Store(steward_home)
    job, runner, sr, spawned = _bg_job(store)
    runner.tick(job.job_id)
    _complete(store, job.job_id, Verification(commit_sha="sha-1"))
    sr.head = "sha-2"  # the candidate moved before the result was consumed

    report = runner.tick(job.job_id)

    assert report.state == "WORKER_FINISHED" and len(spawned) == 2
    assert bg_verify.record(store, job.job_id)["sha"] == "sha-2"
    assert _events(store, job, "background verification discarded")
    assert store.get_job(job.job_id).state == JobState.WORKER_FINISHED


def test_crashed_background_run_is_unverified_needs_human_with_reason(steward_home):
    store = Store(steward_home)
    job, runner, sr, spawned = _bg_job(store)
    runner.tick(job.job_id)
    bg_verify.finish(store, job.job_id, status="failed", error="RuntimeError: worktree add failed")

    report = runner.tick(job.job_id)

    assert report.state == "NEEDS_HUMAN"
    ev = next(e for e in store.events(job.job_id) if e.rule == "bg_verify_unverified")
    assert "worktree add failed" in str(ev.evidence) and "unverified" in ev.what


@pytest.mark.legacy_ownership_off
def test_dead_running_record_is_a_crash_not_a_pass(steward_home):
    store = Store(steward_home)
    job, runner, sr, spawned = _bg_job(store)
    runner.tick(job.job_id)
    rec = bg_verify.record(store, job.job_id)
    store.kv_set(job.job_id, bg_verify.KEY, {**rec, "started_at": rec["started_at"] - bg_verify.STALE_S - 5})

    report = runner.tick(job.job_id)

    assert report.state == "NEEDS_HUMAN"


def test_run_forever_keeps_supervising_while_verifying(steward_home):
    store = Store(steward_home)
    job, runner, sr, spawned = _bg_job(store)
    ticks = {"n": 0}
    runner.job_lead = StubLead(store)

    def sleep(_s):
        ticks["n"] += 1
        if ticks["n"] == 2:
            _complete(store, job.job_id, green("sha-1"))

    report = runner.run_forever(job.job_id, interval_s=1, sleep_fn=sleep, max_ticks=10)

    assert report.state == "ACCEPTED"


def test_fast_candidate_still_verifies_inline(steward_home):
    store = Store(steward_home)
    job = _finish_job(store)
    _slow_baseline(store, job.job_id, 30.0)
    runner = _runner(store, HeadRunner(), [[_done_obs()]])
    runner.bg_verify_spawn = lambda argv: (_ for _ in ()).throw(AssertionError("no background run expected"))
    runner.start(job.job_id)
    # no discoverable checks in this fixture: the inline verification ran (no background spawn), and v2.7 never
    # accepts a verification in which no check ran
    assert runner.tick(job.job_id).state == "NEEDS_HUMAN"
    assert "no checks ran" in str([e.evidence for e in store.events(job.job_id)])
    assert store.kv_get(job.job_id, bg_verify.DURATION_KEY) is not None  # measured for the next prediction


# -- the detached process body -----------------------------------------------------------------------------


def test_run_verifies_the_exact_sha_in_a_temp_worktree(steward_home, tmp_path):
    store = Store(steward_home)
    job = _finish_job(store)
    wt = store.home / "worktrees" / job.job_id
    bg_verify.start(store, job.job_id, wt, "sha-9", spawn=lambda argv: None)
    seen = {}

    def fake_verify(st, jid, tree, discovery, runner):
        seen["tree"] = tree
        return Verification(commit_sha="sha-9", new_failures=["t::a"])

    sr = HeadRunner("sha-9")
    assert bg_verify.run(store, job.job_id, sr, verify_fn=fake_verify, discover_fn=lambda p: None) == "done"
    add = next(c for c in sr.calls if c["argv"][:3] == ["git", "worktree", "add"])
    assert add["argv"][-1] == "sha-9" and str(seen["tree"]) == add["argv"][-2]
    assert any(c["argv"][:3] == ["git", "worktree", "remove"] for c in sr.calls)
    rec = bg_verify.record(store, job.job_id)
    assert rec["status"] == "done" and bg_verify.from_dict(rec["result"]).new_failures == ["t::a"]


def test_run_records_a_crash(steward_home):
    store = Store(steward_home)
    job = _finish_job(store)
    bg_verify.start(store, job.job_id, store.home / "worktrees" / job.job_id, "sha-9", spawn=lambda argv: None)

    def boom(*a):
        raise RuntimeError("pytest exploded")

    assert bg_verify.run(store, job.job_id, HeadRunner("sha-9"), verify_fn=boom, discover_fn=lambda p: None) == "failed"
    assert "pytest exploded" in bg_verify.record(store, job.job_id)["error"]


def test_verification_records_the_new_failures_messages(steward_home, tmp_path):
    """v2.6: a send-back names the first failing test; the evidence also carries what it said (from JUnit)."""
    from steward.discovery import DiscoveredCommand, Discovery
    from steward.verify import verify
    from test_replay_14305aeb import OWNER_TEST, Repo, SUITE

    store = Store(steward_home)
    job = _finish_job(store)
    store.add_check(job.job_id, commit_sha="base", command=SUITE, exit_code=0,
                    results={OWNER_TEST: "passed"}, label="baseline")
    repo = Repo()
    disc = Discovery(commands=[DiscoveredCommand(SUITE, "pyproject.toml", "check", "pytest")])
    verify(store, job.job_id, store.home / "worktrees" / job.job_id, disc, repo)
    ev = next(e for e in store.events(job.job_id) if e.what == "verification recorded")
    assert "owner name found in steward/fleet_runtime.py:158" in ev.evidence["failure_messages"][OWNER_TEST]


# -- a job already moved back to RUNNING must not crash the runner (2026-10-05: 214096dc, 48675e4e) -----------------


def test_returning_to_running_is_idempotent_when_the_job_is_already_running(steward_home):
    store = Store()
    job, runner, _sr, _spawned = _bg_job(store)
    if store.get_job(job.job_id).state != JobState.RUNNING:  # the worker resumed first
        store.set_state(job.job_id, JobState.RUNNING, what="worker resumed first", evidence={}, rule="test",
                        judgment=__import__("steward.models", fromlist=["Judgment"]).Judgment.code)
    assert store.get_job(job.job_id).state == JobState.RUNNING
    runner._set_running(job.job_id, what="verification failed; returning to running with guidance",
                        evidence={"reason": "x"}, rule="test", judgment=__import__("steward.models",
                        fromlist=["Judgment"]).Judgment.code)
    assert store.get_job(job.job_id).state == JobState.RUNNING
    ev = [e for e in store.events(job.job_id) if e.what == "verification failed; returning to running with guidance"]
    assert ev and ev[-1].evidence.get("already_running") is True
