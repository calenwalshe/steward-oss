"""Regressions from the 2026-10-03 overnight run: curl flag case, `do not merge` goals, unparseable junit,
a runner restarted onto a WORKER_FINISHED job, and check scratch space living outside /tmp."""

import os
import re

import pytest

from steward import autopilot
from steward.baseline import check_scratch, run_check
from steward.models import JobState
from steward.permissions import is_remote_write
from steward.runner import Runner
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store

from test_runner import _Clock, _Factory, _contracted_job, _fake_diagnoser, _git_runner


# -- 1. curl/wget short flags are case-sensitive ----------------------------------------------------------------

@pytest.mark.parametrize("head", [
    "curl -sk -D- https://host/health",
    "curl -f https://host/x",
    "curl -t https://host/x",
    "wget -S https://host/x",
])
def test_read_only_curl_short_flags_are_not_writes(head):
    assert not is_remote_write(head)


@pytest.mark.parametrize("head", [
    "curl -d x=1 https://h",
    "curl -F f=@a https://h",
    "curl -T a https://h",
    "curl -X post https://h",
    "curl -X POST https://h",
    "curl --DATA x https://h",
    "curl --Request=put https://h",
])
def test_curl_write_flags_still_detected(head):
    assert is_remote_write(head)


# -- 2. a goal that says not to merge needs a human ------------------------------------------------------------

@pytest.mark.parametrize("goal", [
    "Fix the bug. Do not merge/incorporate this branch.",
    "do not merge",
    "Never auto-merge this; I will review it.",
    "Please don't incorporate until I say so",
    "Leave it on the branch, without merging.",
])
def test_no_merge_goal_is_detected(goal):
    assert autopilot.no_merge_ask(goal)


@pytest.mark.parametrize("goal", [
    "Merge master into YOUR branch and resolve conflicts",
    "Fix the merge conflict handling in incorporate.py",
    "Add tests; do not touch the docs",
    "Handle the no-merge-base case in git helpers",
    "",
])
def test_merge_mentions_are_not_no_merge_instructions(goal):
    assert autopilot.no_merge_ask(goal) is None


@pytest.mark.parametrize("goal", [
    "Fix the pipeline so ACCEPTED jobs never finish incorporation/activation",
    "Jobs never finish incorporation after acceptance; find out why",
    "The runner never merges a branch whose checks are pending",
    "Investigate why the gate does not merge routine jobs",
    "Bug: the autopilot doesn't incorporate follow-ups, so they pile up",
    "Merges are never attempted when the queue is paused; fix that",
    "Accepted jobs cannot incorporate because the lock is stale",
    "Currently Steward never auto-merges docs-only jobs; make it do so",
])
def test_descriptive_no_merge_text_is_not_an_instruction(goal):
    assert autopilot.no_merge_ask(goal) is None


@pytest.mark.parametrize("goal", [
    "Fix the typo, and do not merge it.",
    "Tidy up. Never merge this branch.",
    "Leave it unmerged: do not incorporate.",
    "You must not merge this job.",
    "This job never merges itself; a human will.",
    "Please don't merge until I review",
    "The worker must not incorporate this change.",
    "Please carefully do not merge this branch",
    "For this change do not merge this branch",
])
def test_imperative_no_merge_text_is_an_instruction(goal):
    assert autopilot.no_merge_ask(goal)


def test_floor_reason_ignores_job_59643014_goal(steward_home):
    store = Store(steward_home)
    job = store.create_job("Fix the bug so ACCEPTED jobs never finish incorporation/activation", "/tmp/repo")
    assert autopilot.floor_reason(store, job.job_id) is None


def test_floor_reason_flags_no_merge_goal(steward_home):
    store = Store(steward_home)
    job = store.create_job("Tidy the README. Do not merge/incorporate.", "/tmp/repo")
    assert "not to merge" in autopilot.floor_reason(store, job.job_id)
    plain = store.create_job("Merge master into your branch", "/tmp/repo")
    assert autopilot.floor_reason(store, plain.job_id) is None


# -- 3. empty/unparseable junit is unverified, never a crash ---------------------------------------------------

@pytest.mark.parametrize("content", ["", "<testsuites><testcase"])
def test_unparseable_junit_is_unverified_not_a_crash(tmp_path, content):
    def respond(argv, cwd):
        xml = re.search(r"--junitxml=(\S+)", argv[-1]).group(1)
        open(xml, "w").write(content)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    run = run_check(FakeRunner(respond), "pytest", tmp_path, tmp_path / "r.xml")
    assert run.tests == {}
    assert run.broken and "junit" in run.broken


def test_runner_started_on_worker_finished_job_runs_closeout(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    runner = Runner(store, _Factory(scripts=[]), _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    store.set_state(job.job_id, JobState.WORKER_FINISHED, what="worker reported done", evidence={},
                    rule="steward_done_marker", judgment=__import__("steward.models", fromlist=["Judgment"]).Judgment.code)

    report = runner.run_forever(job.job_id, max_ticks=1)

    assert any(e.what == "closeout_resumed" for e in store.events(job.job_id))
    assert store.get_job(job.job_id).state != JobState.WORKER_FINISHED
    assert report.state != JobState.WORKER_FINISHED.value


# -- 4. check runs use scratch under the steward home, not /tmp ------------------------------------------------

def test_check_scratch_lives_under_steward_home_and_is_cleaned(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_HOME", str(steward_home))
    with check_scratch() as env:
        d = env["TMPDIR"]
        assert d.startswith(str(steward_home)) and os.path.isdir(d)
        assert env["PYTEST_DEBUG_TEMPROOT"] == d
    assert not os.path.exists(d)


def test_run_check_passes_scratch_env_to_the_command(steward_home, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_HOME", str(steward_home))
    seen = {}

    def respond(argv, cwd):
        seen["tmp"] = os.environ.get("TMPDIR")
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    fake = FakeRunner(respond)
    orig = fake.shell_line
    fake.shell_line = lambda line, cwd, **kw: (seen.update(env=kw.get("env")), orig(line, cwd, **kw))[1]
    run_check(fake, "pytest", tmp_path, tmp_path / "r.xml")
    assert seen["env"]["TMPDIR"].startswith(str(steward_home))
    assert not os.path.exists(seen["env"]["TMPDIR"])
