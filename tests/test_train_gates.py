from steward import train_gates
from steward.runner_exec import FakeRunner, SubprocessRunner, CommandResult
from test_incorporate import git  # noqa: F401

TEST_SRC = "def test_one():\n    assert 1 == 1\n\n\ndef test_two():\n    assert 2 == 2\n"


def _repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "tests").mkdir()
    (r / "tests" / "test_x.py").write_text(TEST_SRC)
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    return r, git(r, "rev-parse", "HEAD").strip()


def test_adding_tests_passes(tmp_path):
    r, base = _repo(tmp_path)
    (r / "tests" / "test_x.py").write_text(TEST_SRC + "\n\ndef test_three():\n    assert 3\n")
    git(r, "commit", "-qam", "add")
    assert train_gates.tests_removed(r, base, SubprocessRunner()) == []


def test_removing_a_test_function_blocks(tmp_path):
    r, base = _repo(tmp_path)
    (r / "tests" / "test_x.py").write_text("def test_one():\n    assert 1 == 1\n")
    git(r, "commit", "-qam", "remove")
    kinds = {f.kind for f in train_gates.tests_removed(r, base, SubprocessRunner())}
    assert "removed_test_function" in kinds


def test_merge_of_branch_that_removed_then_restored_is_clean(tmp_path):
    """The 1b441f50 shape: a branch's own history removes a test, a later merge restores it.
    Measured on the merged tree against the train base, nothing is removed."""
    r, base = _repo(tmp_path)
    git(r, "checkout", "-qb", "job")
    (r / "tests" / "test_x.py").write_text("def test_one():\n    assert 1 == 1\n")
    git(r, "commit", "-qam", "drop")
    (r / "tests" / "test_x.py").write_text(TEST_SRC)
    git(r, "commit", "-qam", "restore")
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--no-ff", "-m", "train", "job")
    assert train_gates.tests_removed(r, base, SubprocessRunner()) == []


def test_run_suite_sets_tmpdir_and_basetemp(tmp_path):
    seen = {}

    def respond(argv, cwd):
        seen.update(argv=argv)
        return CommandResult(argv, cwd, 0, "5 passed", "", 0.1)

    runner = FakeRunner(respond)
    res = train_gates.run_suite(tmp_path, "python3 -m pytest -q", tmp_path / "tmp", runner)
    seen["env"] = runner.calls[-1]["env"]
    assert res.exit_code == 0
    assert seen["env"]["TMPDIR"].startswith(str(tmp_path / "tmp"))
    assert "--basetemp" in seen["argv"][-1]


def test_run_suite_removes_its_tmp_dir(tmp_path):
    """C3: every suite run's TMPDIR/basetemp is deleted afterwards (a full suite leaves ~3.6 GB)."""
    def respond(argv, cwd):
        tmpd = runner.calls[-1]["env"]["TMPDIR"]
        import pathlib
        (pathlib.Path(tmpd) / "bt").mkdir(parents=True)
        (pathlib.Path(tmpd) / "bt" / "junk").write_text("x" * 100)
        return CommandResult(argv, cwd, 0, "ok", "", 0.1)

    runner = FakeRunner(respond)
    train_gates.run_suite(tmp_path, "true", tmp_path / "tmp", runner)
    assert list((tmp_path / "tmp").iterdir()) == []


def test_run_suite_timeout_is_red_and_cleaned(tmp_path):
    import subprocess

    class R:
        def run(self, argv, cwd, timeout=None, env=None):
            raise subprocess.TimeoutExpired(argv, timeout)

    res = train_gates.run_suite(tmp_path, "true", tmp_path / "tmp", R(), timeout=1)
    assert res.exit_code != 0 and "timed out" in res.stderr
    assert list((tmp_path / "tmp").iterdir()) == []


def test_run_suite_env_drops_git_config(tmp_path, monkeypatch):
    for k, v in {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "c",
                 "GIT_DIR": "/x", "GIT_WORK_TREE": "/y", "GIT_INDEX_FILE": "/z"}.items():
        monkeypatch.setenv(k, v)
    runner = FakeRunner(lambda argv, cwd: CommandResult(argv, cwd, 0, "ok", "", 0.1))
    train_gates.run_suite(tmp_path, "true", tmp_path / "tmp", runner)
    env = runner.calls[-1]["env"]
    assert not [k for k in env if k.startswith("GIT_CONFIG_")]
    assert not {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"} & set(env)
    assert "TMPDIR" in env
