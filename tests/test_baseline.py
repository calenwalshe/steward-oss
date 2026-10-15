import subprocess

from fixtures.repos import python_repo

from steward.baseline import Baseline, CheckResult, pre_existing_failure_count, run_baseline, run_check
from steward.discovery import discover
from steward.models import Judgment
from steward.runner_exec import CommandResult, FakeRunner, SubprocessRunner
from steward.store import Store


def test_run_check_keeps_timeout_as_a_nonpass(steward_home, monkeypatch, tmp_path):
    monkeypatch.setenv("STEWARD_HOME", str(steward_home))

    class TimeoutRunner:
        def shell_line(self, line, cwd, *, timeout=None, env=None):
            assert timeout == 3
            raise subprocess.TimeoutExpired(line, timeout, output="partial stdout", stderr="partial stderr")

    result = run_check(TimeoutRunner(), "exec python3 -m pytest -q", tmp_path, tmp_path / "junit.xml",
                       timeout_s=3)
    assert result.exit_code == 124
    assert result.broken == "timed out after 3s"
    assert "partial stdout" in result.output and "partial stderr" in result.output


def _head(repo):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_real_pytest_repo_distinguishes_pass_and_fail(steward_home, tmp_path):
    repo = python_repo(tmp_path)
    store = Store(steward_home)
    job = store.create_job("goal", str(repo))
    discovery = discover(repo)
    runner = SubprocessRunner()

    baseline = run_baseline(store, job.job_id, repo, discovery, runner)

    assert baseline.commit_sha == _head(repo)
    assert len(baseline.results) == 1
    result = baseline.results[0]
    assert result.command == "pytest"
    outcomes = set(result.tests.values())
    assert "passed" in outcomes
    assert "failed" in outcomes
    assert result.exit_code != 0  # one test fails


def test_baseline_persists_to_store_checks_with_baseline_label(steward_home, tmp_path):
    repo = python_repo(tmp_path)
    store = Store(steward_home)
    job = store.create_job("goal", str(repo))
    discovery = discover(repo)
    runner = SubprocessRunner()

    run_baseline(store, job.job_id, repo, discovery, runner)

    checks = store.get_checks(job.job_id, label="baseline")
    assert len(checks) == 1
    assert checks[0]["command"] == "pytest"
    assert checks[0]["label"] == "baseline"
    assert "passed" in checks[0]["results"].values()


def test_baseline_appends_code_judgment_event(steward_home, tmp_path):
    repo = python_repo(tmp_path)
    store = Store(steward_home)
    job = store.create_job("goal", str(repo))
    discovery = discover(repo)
    runner = SubprocessRunner()

    run_baseline(store, job.job_id, repo, discovery, runner)

    events = store.events(job.job_id)
    baseline_events = [e for e in events if e.what == "baseline recorded"]
    assert len(baseline_events) == 1
    event = baseline_events[0]
    assert event.rule == "discovered_check_authorized"
    assert event.judgment == Judgment.code


def test_baseline_only_runs_authorized_check_commands(steward_home, tmp_path):
    repo = python_repo(tmp_path)
    store = Store(steward_home)
    job = store.create_job("goal", str(repo))
    discovery = discover(repo)

    fake = FakeRunner(
        lambda argv, cwd: CommandResult(
            argv=argv, cwd=cwd, exit_code=0, stdout="deadbeef\n", stderr="", duration_s=0.0
        )
    )
    run_baseline(store, job.job_id, repo, discovery, fake)

    # Only "git rev-parse HEAD" plus one shell_line per check-kind command.
    check_lines = [c for c in discovery.commands if c.kind == "check"]
    assert len(fake.calls) == 1 + len(check_lines)
    for call in fake.calls[1:]:
        assert call["argv"][:2] == ["bash", "-lc"]


def test_baseline_with_fake_runner_nonzero_exit_no_test_detail(steward_home, tmp_path):
    repo = python_repo(tmp_path)
    store = Store(steward_home)
    job = store.create_job("goal", str(repo))
    discovery = discover(repo)

    def _respond(argv, cwd):
        if argv[:2] == ["git", "rev-parse"]:
            return CommandResult(
                argv=argv, cwd=cwd, exit_code=0, stdout="abc123\n", stderr="", duration_s=0.0
            )
        return CommandResult(argv=argv, cwd=cwd, exit_code=1, stdout="", stderr="boom", duration_s=0.0)

    fake = FakeRunner(_respond)
    baseline = run_baseline(store, job.job_id, repo, discovery, fake)

    assert baseline.commit_sha == "abc123"
    assert len(baseline.results) == 1
    assert baseline.results[0].exit_code == 1
    # No junitxml was actually written by the fake, so tests dict stays empty.
    assert baseline.results[0].tests == {}


def test_pre_existing_failure_count_dedupes_same_test_across_two_check_commands():
    # `pytest` and `make test` both run the same suite; the one failing test
    # must be counted once, not twice, when both commands report it.
    baseline = Baseline(
        commit_sha="deadbeef",
        results=[
            CheckResult(command="pytest", exit_code=1, tests={"t::test_fails": "failed"}),
            CheckResult(command="make test", exit_code=1, tests={"t::test_fails": "failed"}),
        ],
    )

    assert pre_existing_failure_count(baseline) == 1


def test_pre_existing_failure_count_counts_exit_only_commands_separately():
    baseline = Baseline(
        commit_sha="deadbeef",
        results=[
            CheckResult(command="pytest", exit_code=1, tests={"t::test_fails": "failed"}),
            CheckResult(command="./check.sh", exit_code=1, tests={}),
            CheckResult(command="./other_check.sh", exit_code=0, tests={}),
        ],
    )

    # 1 deduped test failure + 1 failing exit-code-only command (the passing
    # exit-code-only command contributes nothing).
    assert pre_existing_failure_count(baseline) == 2
