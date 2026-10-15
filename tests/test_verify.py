import shutil
import subprocess

from fixtures.repos import python_repo

from steward.baseline import run_baseline
from steward.discovery import DiscoveredCommand, Discovery, discover
from steward.models import Judgment
from steward.runner_exec import CommandResult, FakeRunner, SubprocessRunner
from steward.store import Store
from steward.verify import is_current, verify


def _run(argv, cwd):
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True)


def _head(repo):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _ignore_pytest_artifacts(repo):
    """Commit a .gitignore so running real pytest doesn't leave the tree
    looking dirty (untracked __pycache__/.pytest_cache) before `verify` gets
    a chance to check it — the fixture repo itself has no .gitignore."""
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "gitignore"], repo)


def _clear_pycache(repo):
    """Force Python to re-read edited test source rather than trust a stale
    compiled-bytecode cache from an earlier same-second baseline run."""
    cache_dir = repo / "tests" / "__pycache__"
    if cache_dir.exists():
        shutil.rmtree(cache_dir)


def test_verify_distinguishes_new_fixed_and_preexisting_failures(steward_home, tmp_path):
    repo = python_repo(tmp_path)
    _ignore_pytest_artifacts(repo)
    store = Store(steward_home)
    job = store.create_job("goal", str(repo))
    discovery = discover(repo)
    runner = SubprocessRunner()

    # baseline: test_ok passes, test_fails fails
    run_baseline(store, job.job_id, repo, discovery, runner)

    # flip both outcomes and commit (the worker's own commit)
    (repo / "tests" / "test_sample.py").write_text(
        "def test_ok():\n"
        "    assert False\n"
        "\n"
        "\n"
        "def test_fails():\n"
        "    assert True\n"
    )
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "flip"], repo)
    _clear_pycache(repo)

    verification = verify(store, job.job_id, repo, discovery, runner)

    assert verification.dirty is False
    assert verification.commit_sha == _head(repo)
    assert any("test_ok" in f for f in verification.new_failures)
    assert any("test_fails" in f for f in verification.fixed)
    assert verification.preexisting_failures == []


def test_verify_persists_checks_with_verify_label_and_commit_sha(steward_home, tmp_path):
    repo = python_repo(tmp_path)
    _ignore_pytest_artifacts(repo)
    store = Store(steward_home)
    job = store.create_job("goal", str(repo))
    discovery = discover(repo)
    runner = SubprocessRunner()
    run_baseline(store, job.job_id, repo, discovery, runner)

    verify(store, job.job_id, repo, discovery, runner)

    checks = store.get_checks(job.job_id, label="verify")
    assert len(checks) == 1
    assert checks[0]["command"] == "pytest"
    assert checks[0]["commit_sha"] == _head(repo)


def test_verify_dirty_worktree_sets_dirty_and_skips_checks(steward_home, tmp_path):
    repo = python_repo(tmp_path)
    store = Store(steward_home)
    job = store.create_job("goal", str(repo))
    discovery = discover(repo)

    def respond(argv, cwd):
        if argv[:2] == ["git", "status"]:
            return CommandResult(
                argv=argv, cwd=str(cwd), exit_code=0,
                stdout=" M tests/test_sample.py\n", stderr="", duration_s=0.0,
            )
        if argv[:2] == ["git", "rev-parse"]:
            return CommandResult(
                argv=argv, cwd=str(cwd), exit_code=0, stdout="deadbeef\n", stderr="", duration_s=0.0
            )
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    fake = FakeRunner(respond)

    verification = verify(store, job.job_id, repo, discovery, fake)

    assert verification.dirty is True
    assert verification.commit_sha == "deadbeef"
    assert store.get_checks(job.job_id, label="verify") == []
    assert not any(call["argv"][:2] == ["bash", "-lc"] for call in fake.calls)
    events = store.events(job.job_id)
    assert any(e.what == "verify_worktree_dirty" for e in events)


def test_verify_exit_code_only_command_preexisting_failure(steward_home, tmp_path):
    store = Store(steward_home)
    job = store.create_job("goal", str(tmp_path))
    discovery = Discovery(
        commands=[DiscoveredCommand(line="./check.sh", source="Makefile:1", kind="check", reason="r")]
    )
    store.add_check(
        job.job_id, commit_sha="base-sha", command="./check.sh", exit_code=1, results={}, label="baseline"
    )

    def respond(argv, cwd):
        if argv[:2] == ["git", "status"]:
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)
        if argv[:2] == ["git", "rev-parse"]:
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="new-sha\n", stderr="", duration_s=0.0)
        if argv[:2] == ["bash", "-lc"]:
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=1, stdout="", stderr="", duration_s=0.0)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    fake = FakeRunner(respond)
    verification = verify(store, job.job_id, tmp_path, discovery, fake)

    assert verification.preexisting_failures == ["./check.sh"]
    assert verification.new_failures == []
    assert verification.fixed == []


def test_verify_exit_code_only_command_new_failure(steward_home, tmp_path):
    store = Store(steward_home)
    job = store.create_job("goal", str(tmp_path))
    discovery = Discovery(
        commands=[DiscoveredCommand(line="./check.sh", source="Makefile:1", kind="check", reason="r")]
    )
    store.add_check(
        job.job_id, commit_sha="base-sha", command="./check.sh", exit_code=0, results={}, label="baseline"
    )

    def respond(argv, cwd):
        if argv[:2] == ["git", "status"]:
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)
        if argv[:2] == ["git", "rev-parse"]:
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="new-sha\n", stderr="", duration_s=0.0)
        if argv[:2] == ["bash", "-lc"]:
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=1, stdout="", stderr="", duration_s=0.0)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    fake = FakeRunner(respond)
    verification = verify(store, job.job_id, tmp_path, discovery, fake)

    assert verification.new_failures == ["./check.sh"]


def test_verify_exit_code_only_command_fixed(steward_home, tmp_path):
    store = Store(steward_home)
    job = store.create_job("goal", str(tmp_path))
    discovery = Discovery(
        commands=[DiscoveredCommand(line="./check.sh", source="Makefile:1", kind="check", reason="r")]
    )
    store.add_check(
        job.job_id, commit_sha="base-sha", command="./check.sh", exit_code=1, results={}, label="baseline"
    )

    def respond(argv, cwd):
        if argv[:2] == ["git", "status"]:
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)
        if argv[:2] == ["git", "rev-parse"]:
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="new-sha\n", stderr="", duration_s=0.0)
        if argv[:2] == ["bash", "-lc"]:
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    fake = FakeRunner(respond)
    verification = verify(store, job.job_id, tmp_path, discovery, fake)

    assert verification.fixed == ["./check.sh"]


def test_is_current_true_only_for_the_last_verified_sha(steward_home, tmp_path):
    repo = python_repo(tmp_path)
    _ignore_pytest_artifacts(repo)
    store = Store(steward_home)
    job = store.create_job("goal", str(repo))
    discovery = discover(repo)
    runner = SubprocessRunner()
    run_baseline(store, job.job_id, repo, discovery, runner)

    first_sha = _head(repo)
    verify(store, job.job_id, repo, discovery, runner)
    assert is_current(store, job.job_id, first_sha) is True

    # a later commit invalidates the prior verification
    (repo / "README.md").write_text("more\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "later"], repo)
    later_sha = _head(repo)

    assert is_current(store, job.job_id, later_sha) is False

    verify(store, job.job_id, repo, discovery, runner)
    assert is_current(store, job.job_id, later_sha) is True
    assert is_current(store, job.job_id, first_sha) is False


def test_is_current_false_with_no_verification_recorded(steward_home, tmp_path):
    store = Store(steward_home)
    job = store.create_job("goal", str(tmp_path))
    assert is_current(store, job.job_id, "any-sha") is False


def test_verify_records_event_with_commit_sha_and_judgment_code(steward_home, tmp_path):
    repo = python_repo(tmp_path)
    _ignore_pytest_artifacts(repo)
    store = Store(steward_home)
    job = store.create_job("goal", str(repo))
    discovery = discover(repo)
    runner = SubprocessRunner()
    run_baseline(store, job.job_id, repo, discovery, runner)

    verify(store, job.job_id, repo, discovery, runner)

    events = [e for e in store.events(job.job_id) if e.what == "verification recorded"]
    assert len(events) == 1
    assert events[0].judgment == Judgment.code
    assert events[0].rule == "verify_at_exact_commit"
    assert events[0].evidence["commit_sha"] == _head(repo)


# ---- audit rec 8: verification gate fits the change ---------------------------------------------


def _fake(git_diff="", check_exit=1, check_out=""):
    calls = []

    def respond(argv, cwd):
        calls.append(argv)
        out = ""
        if argv[:2] == ["git", "rev-parse"]:
            out = "new-sha\n"
        elif argv[:3] == ["git", "diff", "--name-only"]:
            out = git_diff
        code = check_exit if argv[:2] == ["bash", "-lc"] else 0
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=code, stdout=check_out if code else out, stderr="", duration_s=0.0)

    return FakeRunner(respond), calls


def _pytest_job(store, tmp_path, baseline_exit=0):
    job = store.create_job("goal", str(tmp_path))
    discovery = Discovery(commands=[DiscoveredCommand(line="pytest -q", source="pyproject", kind="check", reason="r")])
    store.add_check(job.job_id, commit_sha="base-sha", command="pytest -q", exit_code=baseline_exit, results={}, label="baseline")
    return job, discovery


def test_pytest_failure_without_a_junit_report_names_the_failing_tests(steward_home, tmp_path):
    store = Store(steward_home)
    job, discovery = _pytest_job(store, tmp_path)
    fake, _ = _fake(check_out="FAILED tests/test_a.py::test_x - assert 1 == 2\nERROR tests/test_b.py::test_y\n")
    v = verify(store, job.job_id, tmp_path, discovery, fake)
    assert v.new_failures == ["tests/test_a.py::test_x", "tests/test_b.py::test_y"]


def test_pytest_failure_with_no_named_tests_is_never_the_bare_command(steward_home, tmp_path):
    store = Store(steward_home)
    job, discovery = _pytest_job(store, tmp_path)
    fake, _ = _fake(check_out="Traceback...\nImportError: no module named foo\n")
    v = verify(store, job.job_id, tmp_path, discovery, fake)
    assert v.new_failures != ["pytest -q"]
    assert len(v.new_failures) == 1 and "exit 1" in v.new_failures[0] and "ImportError" in v.new_failures[0]


def test_docs_only_diff_skips_the_test_gate(steward_home, tmp_path):
    store = Store(steward_home)
    job, discovery = _pytest_job(store, tmp_path)
    fake, calls = _fake(git_diff="docs/audit.md\nREADME.md\n.planning/x/PLAN.md\n")
    v = verify(store, job.job_id, tmp_path, discovery, fake)
    assert v.docs_only and v.new_failures == [] and not v.dirty
    assert not any(c[:2] == ["bash", "-lc"] for c in calls)
    assert "verify_skipped_docs_only" in [e.what for e in store.events(job.job_id)]
    assert is_current(store, job.job_id, "new-sha")


def test_a_diff_with_code_still_runs_the_gate(steward_home, tmp_path):
    store = Store(steward_home)
    job, discovery = _pytest_job(store, tmp_path)
    fake, calls = _fake(git_diff="docs/audit.md\nsteward/x.py\n")
    v = verify(store, job.job_id, tmp_path, discovery, fake)
    assert not v.docs_only and v.new_failures
    assert any(c[:2] == ["bash", "-lc"] for c in calls)


def test_failures_the_baseline_never_ran_are_rechecked_at_the_merge_base(steward_home, tmp_path):
    repo = python_repo(tmp_path)
    _ignore_pytest_artifacts(repo)
    store = Store(steward_home)
    job = store.create_job("goal", str(repo))
    discovery = discover(repo)
    line = next(c.line for c in discovery.commands if c.kind == "check")
    # a baseline that saw only test_ok (a narrower selection than the verification run)
    store.add_check(job.job_id, commit_sha=_head(repo), command=line, exit_code=0,
                    results={"tests.test_sample::test_ok": "passed"}, label="baseline")
    (repo / "notes.py").write_text("x = 1\n")  # the worker's own (unrelated) commit
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "work"], repo)
    _clear_pycache(repo)

    v = verify(store, job.job_id, repo, discovery, SubprocessRunner())

    assert v.new_failures == []  # test_fails also fails at the merge-base: pre-existing
    assert any("test_fails" in f for f in v.preexisting_failures)
    assert store.get_checks(job.job_id, label="baseline-merge-base")
