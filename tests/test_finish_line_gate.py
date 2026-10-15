"""SUP-01 / SUP-03: a finish check that cannot finish is `unverified`, never `not met`; `new` warns on slow checks.

Job 14305aeb's finish line named the ~30-minute full suite: the 120 s gate timed out four times, nudged the worker
with no failing test named and escalated to NEEDS_HUMAN before `verify_worker` + `closeout` ever ran.
"""

from __future__ import annotations

import subprocess

from steward import stability
from steward.models import JobState
from steward.steering import Steering
from steward.store import Store
from ruling_support import StubLead, finish, with_check
from test_stability import Clock, ScriptedRunner, _done_obs, _events, _finish_job, _runner


class TimeoutRunner(ScriptedRunner):
    """The finish check (any line naming check_finish) never finishes inside the gate's budget."""

    def __init__(self, shell=None, *, match="check_finish"):
        super().__init__(shell)
        self.match = match

    def shell_line(self, line, cwd, *, timeout=None, env=None):
        if self.match in line and timeout is not None:
            self.calls.append({"argv": ["bash", "-lc", line], "cwd": str(cwd), "timeout": timeout, "env": env})
            raise subprocess.TimeoutExpired(line, timeout)
        return super().shell_line(line, cwd, timeout=timeout, env=env)


def test_timeout_result_is_unverified_not_failed(tmp_path):
    res = stability.run_finish_check(TimeoutRunner(), tmp_path, "python3 check_finish.py")
    assert res.unverified and not res.ok and res.runnable
    assert "timed out" in res.reason


def test_unrunnable_result_is_unverified(tmp_path):
    sr = ScriptedRunner(lambda line: (127, "bash: python3: command not found"))
    res = stability.run_finish_check(sr, tmp_path, "python3 check_finish.py")
    assert res.unverified and not res.ok and not res.runnable


def test_real_failure_is_not_unverified(tmp_path):
    sr = ScriptedRunner(lambda line: (1, "FAIL: nope"))
    res = stability.run_finish_check(sr, tmp_path, "python3 check_finish.py")
    assert not res.unverified and not res.ok


def test_timed_out_finish_check_spends_no_bounce_and_verification_runs(steward_home):
    store = Store(steward_home)
    job = _finish_job(store)
    with_check(store, job)
    runner = _runner(store, TimeoutRunner(), [[_done_obs()] * 4])
    runner.job_lead = StubLead(store)
    runner.start(job.job_id)

    report = finish(runner, job.job_id)

    ev = _events(store, job, "finish_check_unverified")
    assert len(ev) == 1 and ev[0].evidence["command"] == "python3 check_finish.py"
    assert "timed out" in ev[0].evidence["reason"]
    assert not _events(store, job, "finish_line_not_met")
    assert stability.finish_bounces(store, job.job_id) == 0
    assert store.kv_get(job.job_id, stability.FINISH_UNPROVEN_KEY) is True
    assert _events(store, job, "verification recorded")  # verify_worker + closeout ran for the candidate
    assert report.state == "ACCEPTED"
    assert not [i for i in Steering(store).all_instructions(job.job_id) if "timed out" in i["text"]]


def track_states(store):
    """Every state the store is moved to, in order."""
    seen: list[str] = []
    real = store.set_state

    def wrapped(job_id, new_state, **kw):
        seen.append(new_state.value)
        return real(job_id, new_state, **kw)

    store.set_state = wrapped
    return seen


def test_repeated_timeouts_never_reach_needs_human(steward_home):
    store = Store(steward_home)
    job = _finish_job(store)
    with_check(store, job)
    states = track_states(store)
    runner = _runner(store, TimeoutRunner(), [[_done_obs()] * 8])
    runner.job_lead = StubLead(store)
    runner.start(job.job_id)
    report = finish(runner, job.job_id)
    assert report.state != "NEEDS_HUMAN" and "NEEDS_HUMAN" not in states


def test_unrunnable_finish_check_records_unverified_and_still_verifies(steward_home):
    store = Store(steward_home)
    job = _finish_job(store)
    with_check(store, job)
    sr = ScriptedRunner(lambda line: (127, "bash: python3: command not found") if "check_finish" in line else (0, ""))
    runner = _runner(store, sr, [[_done_obs()]])
    runner.job_lead = StubLead(store)
    runner.start(job.job_id)

    report = finish(runner, job.job_id)

    assert _events(store, job, "finish_check_unverified") and _events(store, job, "finish_check_unrunnable")
    assert _events(store, job, "verification recorded")
    assert stability.finish_bounces(store, job.job_id) == 0
    assert report.state == "ACCEPTED"


def test_real_nonzero_exit_still_bounces_exactly_as_before(steward_home):
    store = Store(steward_home)
    job = _finish_job(store)
    sr = ScriptedRunner(lambda line: (1, "FAIL: docs/widget.md missing") if "check_finish" in line else (0, ""))
    runner = _runner(store, sr, [[_done_obs()] * 4])
    runner.start(job.job_id)

    report = runner.tick(job.job_id)

    assert report.state == "RUNNING" and report.action == "finish_bounce"
    assert stability.finish_bounces(store, job.job_id) == 1
    assert not _events(store, job, "finish_check_unverified")
    assert not _events(store, job, "verification recorded")
    assert store.get_job(job.job_id).state == JobState.RUNNING


# -- SUP-03: `steward new` warns about a finish check that cannot finish inside the gate -----------------


def test_full_suite_finish_check_is_warned(tmp_path):
    warn = stability.finish_check_warnings("Do it. Finish line: `python3 -m pytest -q` passes.", tmp_path)
    assert any("full suite" in w for w in warn)
    assert stability.finish_check_warnings("Finish line: `pytest -q` green", tmp_path)


def test_targeted_finish_check_is_not_warned(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("")
    goal = "Do it. Finish line: `python3 -m pytest -q tests/test_a.py -k fast` passes."
    assert stability.finish_check_warnings(goal, tmp_path) == []


def test_predicted_slow_finish_check_is_warned(tmp_path):
    (tmp_path / "check_finish.py").write_text("")
    goal = "Finish line: `python3 check_finish.py` exits 0"
    assert stability.finish_check_warnings(goal, tmp_path, predicted_s=60) == []
    warn = stability.finish_check_warnings(goal, tmp_path, predicted_s=2200)
    assert any("predicted" in w and "120" in w for w in warn)


def test_finish_check_picked_from_the_donts_text_is_warned(tmp_path):
    (tmp_path / "check_finish.py").write_text("")
    goal = ("Ship it. Finish line: the docs read well. Don'ts: never run `python3 check_finish.py` against the live "
            "store.")
    warn = stability.finish_check_warnings(goal, tmp_path)
    assert any("Don'ts" in w for w in warn)


def test_new_prints_the_warning_on_its_receipt_and_still_creates_the_job(steward_home, tmp_path, capsys):
    from fixtures.repos import python_repo

    from steward import cli

    repo = python_repo(tmp_path)
    rc = cli.main(["new", "Fix it. Finish line: `python3 -m pytest -q` passes", "--repo", str(repo)])
    assert rc == 0  # a warning, never a refusal
    cap = capsys.readouterr()
    assert "CREATED" in cap.out or "CONTRACTED" in cap.out
    assert "warning: finish check:" in cap.err and "full suite" in cap.err
    job_id = cap.out.split()[0]
    store = Store(steward_home)
    assert [e for e in store.events(job_id) if e.what == "finish_check_warning"]
