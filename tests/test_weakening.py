import subprocess
from pathlib import Path

from steward.runner_exec import SubprocessRunner
from steward.weakening import detect


def _run(argv: list[str], cwd: Path) -> None:
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True)


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "test@example.com"], repo)
    _run(["git", "config", "user.name", "Test"], repo)
    return repo


def _commit(repo: Path, message: str) -> str:
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", message], repo)
    return _head(repo)


def _base_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = _init_repo(tmp_path)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_sample.py").write_text(
        "def test_a():\n"
        "    x = 1\n"
        "    assert x == 1\n"
        "\n"
        "\n"
        "def test_b():\n"
        "    assert True\n"
    )
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    )
    base_sha = _commit(repo, "init")
    return repo, base_sha


def test_deleted_test_file_is_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "tests" / "test_sample.py").unlink()
    _commit(repo, "delete test file")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert any(
        f.kind == "deleted_test_file" and "test_sample.py" in f.path for f in findings
    )


def test_removed_test_function_is_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "tests" / "test_sample.py").write_text(
        "def test_a():\n"
        "    x = 1\n"
        "    assert x == 1\n"
    )
    _commit(repo, "remove test_b")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert any(f.kind == "removed_test_function" for f in findings)


def test_removed_assertions_net_is_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "tests" / "test_sample.py").write_text(
        "def test_a():\n"
        "    x = 1\n"
        "\n"
        "\n"
        "def test_b():\n"
        "    assert True\n"
    )
    _commit(repo, "weaken test_a")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert any(f.kind == "removed_assertions" for f in findings)
    assert not any(f.kind == "removed_test_function" for f in findings)


def test_added_skip_marker_is_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "tests" / "test_sample.py").write_text(
        "import pytest\n"
        "\n"
        "\n"
        "@pytest.mark.skip(reason='flaky')\n"
        "def test_a():\n"
        "    x = 1\n"
        "    assert x == 1\n"
        "\n"
        "\n"
        "def test_b():\n"
        "    assert True\n"
    )
    _commit(repo, "skip test_a")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert any(f.kind == "added_skip_marker" for f in findings)


def test_edited_test_config_is_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "-k not test_a"\n'
    )
    _commit(repo, "edit pytest config")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert any(f.kind == "test_config_changed" and "pyproject.toml" in f.path for f in findings)


def test_edited_ci_workflow_is_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI\non: [push]\n")
    _commit(repo, "add ci workflow")

    (workflows / "ci.yml").write_text("name: CI\non: [push, pull_request]\n")
    _commit(repo, "edit ci workflow")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert any(
        f.kind == "test_config_changed" and "workflows/ci.yml" in f.path for f in findings
    )


def test_adding_a_new_test_is_a_clean_refactor_not_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "tests" / "test_sample.py").write_text(
        "def test_a():\n"
        "    x = 1\n"
        "    assert x == 1\n"
        "\n"
        "\n"
        "def test_b():\n"
        "    assert True\n"
        "\n"
        "\n"
        "def test_c_new():\n"
        "    assert 1 + 1 == 2\n"
    )
    _commit(repo, "add a new test")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert findings == []


def test_deleting_a_non_test_file_is_not_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "pyproject.toml").unlink()
    (repo / "tests" / "conftest.py").write_text("")  # will itself be flagged as config
    (repo / "tests" / "conftest.py").unlink()
    (repo / "README.md").write_text("# demo\n")
    _commit(repo, "delete pyproject, add readme")

    findings = detect(repo, base_sha, SubprocessRunner())

    # pyproject.toml deletion is a test-config change, not silently ignored;
    # README.md is neither a test file nor test config, so no finding for it.
    assert not any("README.md" in f.path for f in findings)
