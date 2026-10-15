"""Phase 13-01 operator polish: 11-03-SUMMARY's Friction list — `steward
verification JOB`, `steward approvals JOB --full`, and the runner pane
closing itself on exit. No test here touches a real Herdr pane; the
`_inprocess_control_relay` autouse fixture (tests/conftest.py) gives `launch`
an in-process control-pane relay, and later POL-03 pane-close assertions use
`FakeRunner`/a scripted `Runner` stand-in, never a live socket."""

from __future__ import annotations

import json

from steward import cli, launch
from steward.models import JobState, Judgment
from steward.runner import TickReport
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store

_TAB_OK = json.dumps(
    {"result": {"tab": {"tab_id": "w3T:t2"}, "root_pane": {"pane_id": "w3T:p2"}}}
)


def _respond(argv, cwd):
    if argv[:3] == ["herdr", "tab", "create"]:
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=_TAB_OK, stderr="", duration_s=0.0)
    return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)


def _advance_to(store, job_id, state):
    """Walk a job through the (legal, `steward.machine`-enforced) transitions
    needed to reach `state` for these read-only-verb tests — CONTRACTED is
    always the first hop since `CREATED -> state` is illegal for anything
    tested here."""
    store.set_state(
        job_id, JobState.CONTRACTED, what="working contract proposed",
        evidence={}, rule="discovery_complete", judgment=Judgment.code,
    )
    if state == JobState.CONTRACTED:
        return
    if state == JobState.NEEDS_HUMAN:
        store.set_state(
            job_id, JobState.NEEDS_HUMAN, what="checks passed; acceptance unresolved",
            evidence={"reasons": ["contract has no done_when criteria"]}, rule="acceptance_unresolved",
            judgment=Judgment.code,
        )
        return
    if state == JobState.ACCEPTED:
        # The real pre-authorized path (steward.closeout): RUNNING ->
        # WORKER_FINISHED -> CHECKS_PASSED -> ACCEPTED, all judgment=code.
        store.set_state(job_id, JobState.RUNNING, what="dispatched", evidence={}, rule="r", judgment=Judgment.code)
        store.set_state(
            job_id, JobState.WORKER_FINISHED, what="worker finished", evidence={}, rule="r", judgment=Judgment.code
        )
        store.set_state(
            job_id, JobState.CHECKS_PASSED, what="checks passed at exact commit",
            evidence={}, rule="checks_pass_at_sha", judgment=Judgment.code,
        )
        store.set_state(
            job_id, JobState.ACCEPTED, what="pre-authorized local acceptance",
            evidence={}, rule="preauthorized_local_acceptance", judgment=Judgment.code,
        )
        return
    raise ValueError(f"_advance_to doesn't know a path to {state}")


# --------------------------------------------------------------- verification


def _job_with_baseline_and_verify(store, *, new_failures=None, preexisting=None, fixed=None, commit="abc123"):
    job = store.create_job("goal", "/repo")
    store.add_check(
        job.job_id, commit_sha="base-sha", command="pytest", exit_code=1,
        results={"t::a": "passed", "t::b": "failed", "t::c": "passed"}, label="baseline",
    )
    store.add_check(
        job.job_id, commit_sha=commit, command="pytest", exit_code=1,
        results={
            "t::a": "passed",
            "t::b": "failed" if preexisting else "passed",
            "t::c": "failed" if new_failures else "passed",
        },
        label="verify",
    )
    store.append_event(
        job.job_id,
        what="verification recorded",
        evidence={
            "commit_sha": commit,
            "new_failures": new_failures or [],
            "fixed": fixed or [],
            "preexisting_failures": preexisting or [],
        },
        rule="verify_at_exact_commit",
        judgment=Judgment.code,
    )
    return job


def test_cli_verification_unknown_job_exits_2(steward_home, capsys):
    rc = cli.main(["verification", "no-such-job"])

    assert rc == 2
    assert "unknown job" in capsys.readouterr().err


def test_cli_verification_reports_no_verification_yet(steward_home, capsys):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    rc = cli.main(["verification", job.job_id])

    assert rc == 0
    out = capsys.readouterr().out
    assert "verified: no" in out


def test_cli_verification_readable_form_has_baseline_and_current_counts(steward_home, capsys):
    store = Store(steward_home)
    job = _job_with_baseline_and_verify(store, new_failures=["t::c"], preexisting=["t::b"])

    rc = cli.main(["verification", job.job_id])

    assert rc == 0
    out = capsys.readouterr().out
    assert "verified: yes" in out
    assert "commit: abc123" in out
    assert "dirty: no" in out
    assert "baseline: 2 passed, 1 failed" in out
    assert "current: 1 passed, 2 failed" in out
    assert "new failures (1): t::c" in out


def test_cli_verification_caps_new_failures_at_20_then_shows_more(steward_home, capsys):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    many = [f"t::f{i}" for i in range(25)]
    store.append_event(
        job.job_id,
        what="verification recorded",
        evidence={"commit_sha": "abc", "new_failures": many, "fixed": [], "preexisting_failures": []},
        rule="verify_at_exact_commit",
        judgment=Judgment.code,
    )

    rc = cli.main(["verification", job.job_id])

    assert rc == 0
    out = capsys.readouterr().out
    assert "t::f0" in out
    assert "t::f19" in out
    assert "t::f20" not in out
    assert "+5 more" in out


def test_cli_verification_dirty_worktree_skips_counts(steward_home, capsys):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    store.append_event(
        job.job_id,
        what="verify_worktree_dirty",
        evidence={"commit_sha": "abc123"},
        rule="verify_requires_clean_worktree",
        judgment=Judgment.code,
    )

    rc = cli.main(["verification", job.job_id])

    assert rc == 0
    out = capsys.readouterr().out
    assert "dirty: yes" in out
    assert "baseline:" not in out


def test_cli_verification_surfaces_weakening(steward_home, capsys):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    store.append_event(
        job.job_id,
        what="verification recorded",
        evidence={"commit_sha": "abc", "new_failures": [], "fixed": [], "preexisting_failures": []},
        rule="verify_at_exact_commit",
        judgment=Judgment.code,
    )
    store.append_event(
        job.job_id,
        what="checks passed; acceptance unresolved",
        evidence={"reasons": ["test-weakening detected: ['removed_test_function']"]},
        rule="acceptance_unresolved",
        judgment=Judgment.code,
    )

    rc = cli.main(["verification", job.job_id])

    assert rc == 0
    out = capsys.readouterr().out
    assert "weakening: test-weakening detected" in out


def test_cli_verification_acceptance_line_pre_authorized(steward_home, capsys):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    _advance_to(store, job.job_id, JobState.ACCEPTED)

    rc = cli.main(["verification", job.job_id])

    assert rc == 0
    out = capsys.readouterr().out
    assert "acceptance: pre-authorized" in out
    assert "nothing for the operator to do" in out


def test_cli_verification_acceptance_line_needs_human(steward_home, capsys):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    _advance_to(store, job.job_id, JobState.NEEDS_HUMAN)

    rc = cli.main(["verification", job.job_id])

    assert rc == 0
    out = capsys.readouterr().out
    assert "acceptance: NEEDS_HUMAN" in out
    assert "accept" in out
    assert "only if Sam agrees" in out


def test_cli_verification_json_prints_raw_api_dict(steward_home, capsys):
    store = Store(steward_home)
    job = _job_with_baseline_and_verify(store, new_failures=["t::c"])

    rc = cli.main(["verification", job.job_id, "--json"])

    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["verified"] is True
    assert payload["new_failures"] == ["t::c"]
    # raw dict only — no baseline/current counts injected into --json
    assert "baseline" not in payload


# ------------------------------------------------------------------ approvals


def test_cli_approvals_full_shows_untruncated_question(steward_home, capsys):
    from steward import approvals as approvals_module

    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    long_text = "worker permission: Bash " + ("x" * 200)
    record = approvals_module.open_approval(store, job.job_id, long_text, kind="permission")

    cli.main(["approvals", job.job_id])
    default_out = capsys.readouterr().out
    assert long_text not in default_out
    assert record["id"] in default_out

    rc = cli.main(["approvals", job.job_id, "--full"])
    full_out = capsys.readouterr().out

    assert rc == 0
    assert long_text in full_out
    assert record["id"] in full_out
    assert record["status"] in full_out


def test_cli_approvals_default_table_unaffected_by_full_flag_absence(steward_home, capsys):
    from steward import approvals as approvals_module

    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    approvals_module.open_approval(store, job.job_id, "short question", kind="general")

    rc = cli.main(["approvals", job.job_id])

    assert rc == 0
    out = capsys.readouterr().out
    assert "short question" in out


# ---------------------------------------------------------------------- POL-03


def _fake_report(job_id: str, state: str) -> TickReport:
    return TickReport(job_id=job_id, state=state, cls=None, action="closeout", detail="done")


class _ScriptedRunner:
    """Stand-in for `steward.runner.Runner`: never touches real workers or
    Herdr — `tick`/`run_forever` just hand back a canned `TickReport`, so
    these tests exercise only `_cmd_run`'s own POL-03 pane-close logic."""

    def __init__(self, store, worker_factory, runner_exec, diagnoser, **kwargs):
        self.store = store
        self.runner_exec = runner_exec

    def start(self, job_id):
        pass

    def resume_all(self, job_ids=None):
        return []

    def tick(self, job_id):
        return _fake_report(job_id, self._state)

    def run_forever(self, job_id, interval_s=30.0, **kwargs):
        return _fake_report(job_id, self._state)


def _run_with_scripted_state(monkeypatch, store, job_id, state, *, extra_args=None):
    scripted_runner = type("ScriptedRunner", (_ScriptedRunner,), {"_state": state})
    monkeypatch.setattr(cli, "Runner", scripted_runner)
    runner_exec = FakeRunner()
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: runner_exec)
    rc = cli.main(["run", job_id, "--worker", "fake", "--once", "--pane", "w1:runner"] + (extra_args or []))
    return rc, runner_exec


def test_run_closes_own_pane_on_accepted_when_flag_passed(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    rc, runner_exec = _run_with_scripted_state(
        monkeypatch, store, job.job_id, JobState.ACCEPTED.value, extra_args=["--close-pane-on-exit"],
    )

    assert rc == 0
    close_calls = [c["argv"] for c in runner_exec.calls if c["argv"][:3] == ["herdr", "pane", "close"]]
    assert close_calls == [["herdr", "pane", "close", "w1:runner"]]
    events = [e.what for e in store.events(job.job_id)]
    assert "runner_pane_closed" in events


def test_run_never_closes_without_the_flag(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    rc, runner_exec = _run_with_scripted_state(monkeypatch, store, job.job_id, JobState.ACCEPTED.value)

    assert rc == 0
    close_calls = [c["argv"] for c in runner_exec.calls if c["argv"][:3] == ["herdr", "pane", "close"]]
    assert close_calls == []


def test_run_does_not_close_while_job_still_active(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    rc, runner_exec = _run_with_scripted_state(
        monkeypatch, store, job.job_id, JobState.NEEDS_HUMAN.value, extra_args=["--close-pane-on-exit"],
    )

    assert rc == 0
    close_calls = [c["argv"] for c in runner_exec.calls if c["argv"][:3] == ["herdr", "pane", "close"]]
    assert close_calls == []


def test_run_closes_only_its_own_pane_not_a_worker_pane(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    scripted_runner = type("ScriptedRunner", (_ScriptedRunner,), {"_state": JobState.STOPPED.value})
    monkeypatch.setattr(cli, "Runner", scripted_runner)
    runner_exec = FakeRunner()
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: runner_exec)

    rc = cli.main(
        ["run", job.job_id, "--worker", "fake", "--once", "--pane", "w1:runner-only", "--close-pane-on-exit"]
    )

    assert rc == 0
    close_calls = [c["argv"] for c in runner_exec.calls if c["argv"][:3] == ["herdr", "pane", "close"]]
    assert close_calls == [["herdr", "pane", "close", "w1:runner-only"]]


# --------------------------------------------------------- launch flag plumbing


def test_worker_run_command_includes_close_flag_by_default():
    command = launch.worker_run_command(
        "job-1", worker="fabric", kind="claude", pane="w3T:p2", sock="/tmp/x.sock",
        agent_args=[], interval=15.0,
    )
    assert "--close-pane-on-exit" in command


def test_worker_run_command_omits_close_flag_when_disabled():
    command = launch.worker_run_command(
        "job-1", worker="fabric", kind="claude", pane="w3T:p2", sock="/tmp/x.sock",
        agent_args=[], interval=15.0, close_pane_on_exit=False,
    )
    assert "--close-pane-on-exit" not in command


def test_launch_passes_close_pane_flag_by_default(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    runner_exec = FakeRunner(_respond)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: runner_exec)

    rc = cli.main(["launch", job.job_id, "--workspace", "w3T"])

    assert rc == 0
    run_call = [c["argv"] for c in runner_exec.calls if c["argv"][:3] == ["herdr", "pane", "run"]][0]
    assert "--close-pane-on-exit" in run_call[4]


def test_launch_keep_pane_omits_close_pane_flag(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    runner_exec = FakeRunner(_respond)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: runner_exec)

    rc = cli.main(["launch", job.job_id, "--workspace", "w3T", "--keep-pane"])

    assert rc == 0
    run_call = [c["argv"] for c in runner_exec.calls if c["argv"][:3] == ["herdr", "pane", "run"]][0]
    assert "--close-pane-on-exit" not in run_call[4]
