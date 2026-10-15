"""Herdr agent-state reports from the runner: "blocked" only while a job truly waits on the owner.

A fake `runner_exec` records the `herdr pane report-agent` calls; no real Herdr."""

from __future__ import annotations

from ruling_support import StubLead, finish as finish_ticks, patch_green_verification
from test_shim import repo  # noqa: F401  (fixture)
from steward import approvals as approvals_module
from steward.models import JobState, Judgment
from steward.runner import Runner
from steward.store import Store
from test_runner_fabric import (
    _Clock, _contracted_job, _fake_diagnoser, _FabricFactory, _frame, _git_runner, _obs,
)


def _states(exec_) -> list[str]:
    out = []
    for c in exec_.calls:
        if c["argv"][:3] == ["herdr", "pane", "report-agent"]:
            assert c["argv"][-1] == "w1:pF"  # always the worker pane
            out.append(c["argv"][c["argv"].index("--state") + 1])
    return out


def _setup(steward_home, command):
    store = Store(steward_home)
    job = _contracted_job(store)
    frame = _frame(3, "permission_request", {"request_id": "r1", "tool": "Bash", "input": {"command": command}})
    factory = _FabricFactory(scripts=[[_obs(lifecycle="blocked", events=(frame,)), _obs(), _obs()]])
    exec_ = _git_runner()
    runner = Runner(store, factory, exec_, _fake_diagnoser(), clock=_Clock())
    runner.job_lead = StubLead(store)
    runner.start(job.job_id)
    return store, job, runner, exec_


def test_auto_approved_permission_never_reports_blocked(steward_home):
    store, job, runner, exec_ = _setup(steward_home, "ls")
    runner.tick(job.job_id)
    runner.tick(job.job_id)
    states = _states(exec_)
    assert states and set(states) == {"working"}


def test_human_routed_approval_reports_blocked_then_working_on_decision(steward_home):
    store, job, runner, exec_ = _setup(steward_home, "git push origin main")
    runner.tick(job.job_id)
    assert _states(exec_) == ["blocked"]
    runner.tick(job.job_id)
    assert _states(exec_) == ["blocked"]  # deduplicated
    [rec] = approvals_module.list_approvals(store, job.job_id, status="pending")
    approvals_module.decide(store, job.job_id, rec["id"], "deny")
    runner.tick(job.job_id)
    assert _states(exec_) == ["blocked", "working"]


def test_needs_human_reports_blocked(steward_home):
    store, job, runner, exec_ = _setup(steward_home, "ls")
    runner.tick(job.job_id)
    store.set_state(job.job_id, JobState.NEEDS_HUMAN, what="x", evidence={}, rule="t", judgment=Judgment.code)
    runner._sync_herdr_state(job.job_id)
    assert _states(exec_)[-1] == "blocked"


def test_finish_reports_done_as_idle(steward_home):
    store, job, runner, exec_ = _setup(steward_home, "ls")
    runner.tick(job.job_id)
    store.set_state(job.job_id, JobState.STOPPING, what="x", evidence={}, rule="t", judgment=Judgment.code)
    store.set_state(job.job_id, JobState.STOPPED, what="x", evidence={}, rule="t", judgment=Judgment.code)
    runner._sync_herdr_state(job.job_id)
    assert _states(exec_)[-1] == "idle"


def test_shim_permission_request_never_reports_blocked(repo):
    import os

    import test_shim as ts

    script = ts._write_script(repo, [
        {"tool_use": {"name": "Bash", "input": {"command": "touch x"}, "permission": True}},
        {"result": {"subtype": "success", "cost": 0.0}},
    ])
    api = ts.FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = ts._make_shim(repo, script, api, read_fd, own_pane="w1:p9")
        shim.start()
        ts._send_frame(write_fd, 1, "brief", {"text": "go"})
        ts._run_until(shim, lambda: ts._sent_of_type(api, "permission_request"))
        states = [p["state"] for m, p in api.calls if m == "pane.report_agent"]
        assert states and "blocked" not in states
    finally:
        os.close(write_fd)
        os.close(read_fd)


# -- whole-job walks: exactly one done (idle) and the right number of blocked ------------------------------------


def _walk(steward_home, first, *, human=False, monkeypatch=None):
    if monkeypatch is not None:
        patch_green_verification(monkeypatch)  # checks ran: the done reaches CHECKS_PASSED and its ruling
    store = Store(steward_home)
    job = _contracted_job(store)
    perm = _frame(3, "permission_request", {"request_id": "r1", "tool": "Bash", "input": {"command": first}})
    done = _frame(9, "done", {"sha": "abc123"})
    script = [_obs(lifecycle="blocked", events=(perm,)), _obs(lifecycle="idle"), _obs(), _obs(lifecycle="idle")]
    if not human:
        script.append(_obs(lifecycle="idle", events=(done,)))
    factory = _FabricFactory(scripts=[script])
    exec_ = _git_runner()
    runner = Runner(store, factory, exec_, _fake_diagnoser(), clock=_Clock())
    runner.job_lead = StubLead(store)
    runner.start(job.job_id)
    return store, job, runner, exec_


def test_full_job_walk_reports_one_done_and_no_blocked(steward_home, monkeypatch):
    store, job, runner, exec_ = _walk(steward_home, "ls", monkeypatch=monkeypatch)
    for _ in range(4):  # permission handled by Steward, idle turns in between
        runner.tick(job.job_id)
    assert set(_states(exec_)) == {"working"}
    report = finish_ticks(runner, job.job_id)  # worker done -> closeout -> its ruling -> ACCEPTED (unmerged)
    assert report.state == "ACCEPTED"
    assert "idle" not in _states(exec_)  # accepted but not merged is not the end
    store.kv_set(job.job_id, "incorporated_commit", "deadbeef")  # the merge
    from steward import herdr_state
    herdr_state.sync(store, exec_, job.job_id)
    herdr_state.sync(store, exec_, job.job_id)  # a restart re-syncing must stay silent
    states = _states(exec_)
    assert states.count("idle") == 1 and states[-1] == "idle" and "blocked" not in states


def test_accepted_with_nothing_to_merge_reports_done_once(steward_home, monkeypatch):
    from steward import incorporate

    monkeypatch.setattr(incorporate, "preflight", lambda store, job_id: incorporate.Preflight(job_id=job_id, repo="/r", contained=True))
    store, job, runner, exec_ = _walk(steward_home, "ls", monkeypatch=monkeypatch)
    for _ in range(6):
        runner.tick(job.job_id)
    assert store.get_job(job.job_id).state == JobState.ACCEPTED
    states = _states(exec_)
    assert states.count("idle") == 1 and "blocked" not in states


def test_human_routed_approval_walk_reports_one_blocked_then_one_done(steward_home):
    store, job, runner, exec_ = _walk(steward_home, "git push origin main", human=True)
    runner.tick(job.job_id)
    runner.tick(job.job_id)
    [rec] = approvals_module.list_approvals(store, job.job_id, status="pending")
    approvals_module.decide(store, job.job_id, rec["id"], "deny")
    runner.tick(job.job_id)
    runner.tick(job.job_id)
    store.set_state(job.job_id, JobState.STOPPING, what="x", evidence={}, rule="t", judgment=Judgment.code)
    store.set_state(job.job_id, JobState.STOPPED, what="x", evidence={}, rule="t", judgment=Judgment.code)
    runner._sync_herdr_state(job.job_id)
    states = _states(exec_)
    assert states.count("blocked") == 1 and states.count("idle") == 1
    assert states == ["blocked", "working", "idle"]
