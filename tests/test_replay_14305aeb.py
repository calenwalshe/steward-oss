"""SUP-04: replay of job 14305aeb's supervision gap, end to end through the runner.

The real job: its finish line named `python3 -m pytest -q` (~30 min, baseline 37 min). The 120 s finish gate timed
out four times, nudged "finish check timed out after 120s" (no failing test named) and escalated to NEEDS_HUMAN,
returning before verify_worker + closeout. The candidate carried a one-line defect (the owner's name in two comments
in steward/fleet_runtime.py) that broke tests/test_owner_sweep.py. The owner fixed it by hand with one send-back.

Here the same shape: slow suite (the finish check times out; the baseline took 2200 s), a planted owner-name leak.
The job must be sent back naming test_owner_sweep, and after the fix reach CHECKS_PASSED with no human, never
passing through NEEDS_HUMAN on a timeout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from steward import bg_verify, stability
from steward.discovery import DiscoveredCommand, Discovery
from steward.runner import Runner
from steward.runner_exec import CommandResult, FakeRunner
from steward.steering import Steering
from steward.store import Store
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.worker import WorkerObservation
from ruling_support import StubLead
from test_finish_line_gate import track_states
from test_runner import _contracted_job, _Factory
from test_stability import Clock

GOAL = ("Execute Steward milestone v2.5 autonomously. Finish line: `python3 -m pytest -q` passes and every "
        "SUMMARY exists.")
SUITE = "python3 -m pytest -q"
OWNER_TEST = "tests.test_owner_sweep::test_owner_name_never_in_package"
OTHER = ["tests.test_runner::test_start", "tests.test_npm_audit::test_manifest", OWNER_TEST]


def _junit(path: Path, failing: set[str]) -> None:
    cases = []
    for name in OTHER:
        cls, _, test = name.partition("::")
        body = '<failure message="owner name found in steward/fleet_runtime.py:158"/>' if name in failing else ""
        cases.append(f'<testcase classname="{cls}" name="{test}">{body}</testcase>')
    path.write_text(f'<testsuite>{"".join(cases)}</testsuite>')


class Repo(FakeRunner):
    """The job's worktree, faked: HEAD moves when the worker commits; the full suite is slow (the 120 s finish
    gate times it out) and fails test_owner_sweep while the leak is in the candidate."""

    def __init__(self):
        super().__init__(self._respond)
        self.head = "c1-leak"
        self.leak = True

    def _respond(self, argv, cwd):
        out = ""
        if argv[:2] == ["git", "rev-parse"]:
            out = self.head + "\n"
        return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=out, stderr="", duration_s=0.0)

    def shell_line(self, line, cwd, *, timeout=None, env=None):
        self.calls.append({"argv": ["bash", "-lc", line], "cwd": str(cwd), "timeout": timeout, "env": env})
        if line.startswith(SUITE) and timeout is not None:
            raise subprocess.TimeoutExpired(line, timeout)  # ~30 minutes never fits the finish gate
        if line.startswith(SUITE) and "--junitxml=" in line:
            _junit(Path(line.split("--junitxml=", 1)[1]), {OWNER_TEST} if self.leak else set())
            return CommandResult(argv=["bash", "-lc", line], cwd=str(cwd), exit_code=1 if self.leak else 0,
                                 stdout="", stderr="", duration_s=1800.0)
        return CommandResult(argv=["bash", "-lc", line], cwd=str(cwd), exit_code=0, stdout="", stderr="",
                             duration_s=0.0)


def _done(text="all phases done\nSTEWARD-DONE"):
    return WorkerObservation(lifecycle="idle", output_tail=text, pid_alive=True, ts="t")


def _setup(store, monkeypatch):
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "auto")
    job = _contracted_job(store, GOAL)
    wt = store.home / "worktrees" / job.job_id
    wt.mkdir(parents=True)
    # the baseline at the job's base: every test green, and the suite measured at 37 minutes
    store.add_check(job.job_id, commit_sha="base", command=SUITE, exit_code=0,
                    results={n: "passed" for n in OTHER}, label="baseline")
    store.kv_set(job.job_id, "baseline_duration_s", 2200.0)
    repo = Repo()
    discovery = Discovery(commands=[DiscoveredCommand(SUITE, "pyproject.toml", "check", "pytest")])

    def spawn(argv):  # the detached `python3 -m steward.bg_verify JOB`, run to completion in-process
        return bg_verify.run(store, job.job_id, repo, discover_fn=lambda tree: discovery)

    runner = Runner(store, _Factory(scripts=[[_done(), _done("working on the fix"), _done()]]), repo,
                    FakeDiagnoser(Diagnosis("retry_with_guidance", "g", "h")), clock=Clock())
    runner.bg_verify_spawn = spawn
    runner.job_lead = StubLead(store)  # answers the one completion ruling (finished) once the verification is green
    runner.start(job.job_id)
    return job, runner, repo


def test_replay_14305aeb_reaches_checks_passed_with_no_human(steward_home, monkeypatch):
    store = Store(steward_home)
    job, runner, repo = _setup(store, monkeypatch)
    states = track_states(store)
    assert stability.finish_check_warnings(GOAL, store.home / "worktrees" / job.job_id)  # `new` would have warned

    # 1. done claim: the finish gate times out -> unverified (no bounce); verification goes to the background
    r1 = runner.tick(job.job_id)
    assert r1.state == "WORKER_FINISHED" and "background" in r1.detail
    assert stability.finish_bounces(store, job.job_id) == 0
    ev = [e for e in store.events(job.job_id) if e.what == "finish_check_unverified"]
    assert ev and "timed out" in ev[0].evidence["reason"]

    # 2. the background result (the leak's failure) sends the job back, naming test_owner_sweep
    r2 = runner.tick(job.job_id)
    assert r2.state == "RUNNING"
    steer = [i for i in Steering(store).all_instructions(job.job_id) if i["origin"] == "steward"][-1]
    assert "first failing test" in steer["text"] and "test_owner_sweep" in steer["text"]

    # 3. the worker removes the owner's name and commits; the next done claim verifies clean in the background
    repo.leak, repo.head = False, "c2-fixed"
    runner.tick(job.job_id)  # the worker is still working
    r4 = runner.tick(job.job_id)
    assert r4.state == "WORKER_FINISHED"
    r5 = runner.tick(job.job_id)
    assert r5.state == "CHECKS_PASSED"  # green at the fixed SHA, no human: now it waits for its one ruling
    r6 = runner.tick(job.job_id)

    assert "CHECKS_PASSED" in states and r6.state == "ACCEPTED"
    assert "NEEDS_HUMAN" not in states
    assert not [i for i in Steering(store).all_instructions(job.job_id) if "timed out" in i["text"]]
    passed = next(e for e in store.events(job.job_id) if e.rule == "checks_pass_at_sha")
    assert passed.evidence["commit_sha"] == "c2-fixed"


def test_replay_under_run_forever_never_parks_in_needs_human(steward_home, monkeypatch):
    store = Store(steward_home)
    job, runner, repo = _setup(store, monkeypatch)
    states = track_states(store)
    calls = {"n": 0}

    def sleep(_s):
        calls["n"] += 1
        if store.get_job(job.job_id).state.value == "RUNNING" and calls["n"] > 1:
            repo.leak, repo.head = False, "c2-fixed"

    report = runner.run_forever(job.job_id, interval_s=1, sleep_fn=sleep, max_ticks=12)

    assert report.state == "ACCEPTED" and "CHECKS_PASSED" in states and "NEEDS_HUMAN" not in states
