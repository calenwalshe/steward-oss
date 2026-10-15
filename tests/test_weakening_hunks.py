"""Hunk-level test-config classification (RC-1, 2026-10-11 stuck-after-sendback)."""

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



def test_additive_conftest_fixture_is_not_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "tests" / "conftest.py").write_text(
        "import pytest\n"
        "\n"
        "\n"
        "@pytest.fixture(autouse=True)\n"
        "def _hermetic_git(monkeypatch):\n"
        '    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "0")\n'
    )
    _commit(repo, "add hermetic conftest")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert findings == []


def test_conftest_deselect_is_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "tests" / "conftest.py").write_text("import pytest\n")
    base_sha = _commit(repo, "add conftest")
    (repo / "tests" / "conftest.py").write_text(
        "import pytest\n"
        "\n"
        "\n"
        "def pytest_collection_modifyitems(config, items):\n"
        '    items[:] = [i for i in items if "test_a" not in i.nodeid]\n'
    )
    _commit(repo, "deselect test_a")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert any(f.kind == "test_config_changed" and "conftest.py" in f.path for f in findings)


def test_conftest_added_skip_is_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "tests" / "conftest.py").write_text('collect_ignore = ["test_sample.py"]\n')
    _commit(repo, "ignore test_sample")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert any(f.kind == "test_config_changed" and "conftest.py" in f.path for f in findings)


def test_conftest_removed_fixture_is_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "tests" / "conftest.py").write_text(
        "import pytest\n"
        "\n"
        "\n"
        "@pytest.fixture\n"
        "def strict():\n"
        "    assert True\n"
    )
    base_sha = _commit(repo, "add conftest")
    (repo / "tests" / "conftest.py").write_text("import pytest\n")
    _commit(repo, "drop fixture")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert any(f.kind == "test_config_changed" and "conftest.py" in f.path for f in findings)


def test_pyproject_packaging_edit_is_not_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        "[project.scripts]\n"
        'lanes-hub = "lanes.hub:main"\n'
        "\n"
        "[tool.setuptools.package-data]\n"
        'lanes = ["*.json"]\n'
        "\n"
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    )
    _commit(repo, "packaging only")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert findings == []


def test_pyproject_added_marker_is_not_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
        'markers = [\n    "slow: long-running",\n]\n'
    )
    _commit(repo, "add marker")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert findings == []


def test_pyproject_removed_pytest_line_is_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    _commit(repo, "drop testpaths")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert any(f.kind == "test_config_changed" and "pyproject.toml" in f.path for f in findings)


def test_setup_cfg_coverage_fail_under_is_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "setup.cfg").write_text("[metadata]\nname = demo\n\n[coverage:report]\nfail_under = 90\n")
    base_sha = _commit(repo, "add setup.cfg")
    (repo / "setup.cfg").write_text("[metadata]\nname = demo\n\n[coverage:report]\nfail_under = 10\n")
    _commit(repo, "lower coverage bar")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert any(f.kind == "test_config_changed" and "setup.cfg" in f.path for f in findings)


def test_setup_cfg_metadata_edit_is_not_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "setup.cfg").write_text("[metadata]\nname = demo\n\n[tool:pytest]\ntestpaths = tests\n")
    base_sha = _commit(repo, "add setup.cfg")
    (repo / "setup.cfg").write_text("[metadata]\nname = demo2\n\n[tool:pytest]\ntestpaths = tests\n")
    _commit(repo, "rename package")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert findings == []


def test_pytest_ini_edit_stays_file_level(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "pytest.ini").write_text("[pytest]\nmarkers =\n    slow: long\n")
    _commit(repo, "add pytest.ini")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert any(f.kind == "test_config_changed" and "pytest.ini" in f.path for f in findings)


def test_conftest_timeout_in_docstring_and_comment_is_not_flagged(tmp_path):
    # Trinity job 371e92a4 (2026-10-14): a fake helper's docstring mentioned timeouts.
    repo, base_sha = _base_repo(tmp_path)
    (repo / "tests" / "conftest.py").write_text("import pytest\n")
    base_sha = _commit(repo, "add conftest")
    (repo / "tests" / "conftest.py").write_text(
        "import pytest\n"
        "\n"
        "\n"
        "class FakeRunner:\n"
        '    """Fake runner.\n'
        "\n"
        '    "timeout_before" times out with no effect\n'
        '    """\n'
        "\n"
        "    def run(self, argv):\n"
        '        """"timeout_before" times out with no effect"""\n'
        "        return argv  # timeout is ignored here\n"
        "\n"
        "\n"
        "# a timeout comment line\n"
    )
    _commit(repo, "add fake runner")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert findings == []


def test_conftest_added_timeout_mark_is_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "tests" / "conftest.py").write_text("import pytest\n")
    base_sha = _commit(repo, "add conftest")
    (repo / "tests" / "conftest.py").write_text(
        "import pytest\n"
        "\n"
        "\n"
        "def pytest_collection_modifyitems_hook(items):\n"
        "    for item in items:\n"
        "        item.add_marker(pytest.mark.timeout(3600))\n"
    )
    _commit(repo, "raise timeout")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert any(f.kind == "test_config_changed" and "conftest.py" in f.path for f in findings)


def test_conftest_added_timeout_option_is_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "tests" / "conftest.py").write_text("import pytest\n")
    base_sha = _commit(repo, "add conftest")
    (repo / "tests" / "conftest.py").write_text(
        "import pytest\n"
        "\n"
        "\n"
        "def pytest_load_initial_conftests(args):\n"
        '    args[:] = args + ["--timeout", "0"]\n'
    )
    _commit(repo, "disable timeout")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert any(f.kind == "test_config_changed" and "conftest.py" in f.path for f in findings)


def test_conftest_added_timeout_config_key_is_flagged(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "tests" / "conftest.py").write_text("import pytest\n")
    base_sha = _commit(repo, "add conftest")
    (repo / "tests" / "conftest.py").write_text(
        "import pytest\n"
        "\n"
        "\n"
        "def pytest_configure(config):\n"
        "    config.option.timeout = 0\n"
    )
    _commit(repo, "zero timeout")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert any(f.kind == "test_config_changed" and "conftest.py" in f.path for f in findings)


def test_conftest_weakening_in_code_still_flagged_beside_docstring(tmp_path):
    repo, base_sha = _base_repo(tmp_path)
    (repo / "tests" / "conftest.py").write_text("import pytest\n")
    base_sha = _commit(repo, "add conftest")
    (repo / "tests" / "conftest.py").write_text(
        "import pytest\n"
        "\n"
        "\n"
        "def pytest_configure(config):\n"
        '    """Nothing to see."""\n'
        '    config.option.deselect = ["tests/test_sample.py"]\n'
    )
    _commit(repo, "deselect test_sample")

    findings = detect(repo, base_sha, SubprocessRunner())

    assert any(
        f.kind == "test_config_changed" and "conftest.py" in f.path and "deselect" in f.detail
        for f in findings
    )
