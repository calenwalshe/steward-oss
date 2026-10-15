"""Builds tiny real git repos in a tmp_path for discovery/baseline/worktree tests.

Each helper `git init`s and commits so the repo starts life with a clean tree
and a resolvable HEAD, matching what `create_worktree` / `run_baseline`
require.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _run(argv: list[str], cwd: Path) -> None:
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True)


def _git_init_commit(repo: Path) -> None:
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "test@example.com"], repo)
    _run(["git", "config", "user.name", "Test"], repo)
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "init"], repo)


def python_repo(tmp_path: Path, name: str = "python_repo") -> Path:
    """pyproject.toml configured for pytest, plus a passing + a failing test."""
    repo = tmp_path / name
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    )
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_sample.py").write_text(
        "def test_ok():\n"
        "    assert True\n"
        "\n"
        "\n"
        "def test_fails():\n"
        "    assert False\n"
    )
    _git_init_commit(repo)
    return repo


def node_repo(tmp_path: Path, name: str = "node_repo") -> Path:
    """package.json with test/lint/build/deploy scripts."""
    repo = tmp_path / name
    repo.mkdir()
    (repo / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "scripts": {
                    "test": "jest",
                    "lint": "eslint .",
                    "build": "webpack",
                    "deploy": "./deploy.sh",
                },
            },
            indent=2,
        )
    )
    _git_init_commit(repo)
    return repo


def make_repo(tmp_path: Path, name: str = "make_repo") -> Path:
    """Makefile with test/lint/release targets."""
    repo = tmp_path / name
    repo.mkdir()
    (repo / "Makefile").write_text(
        "test:\n"
        "\tpytest\n"
        "\n"
        "lint:\n"
        "\truff check .\n"
        "\n"
        "release:\n"
        "\t./release.sh\n"
    )
    _git_init_commit(repo)
    return repo


def ci_repo(tmp_path: Path, name: str = "ci_repo") -> Path:
    """.github/workflows/ci.yml with `run:` lines including `npm publish`."""
    repo = tmp_path / name
    repo.mkdir()
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "name: CI\n"
        "on: [push]\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: npm ci\n"
        "      - run: npm test\n"
        "      - run: npm publish\n"
    )
    _git_init_commit(repo)
    return repo


def instructions_repo(tmp_path: Path, name: str = "instructions_repo") -> Path:
    """AGENTS.md + CLAUDE.md present, no test tooling at all."""
    repo = tmp_path / name
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# Agents\n")
    (repo / "CLAUDE.md").write_text("# Claude\n")
    (repo / "README.md").write_text("# Demo\n")
    _git_init_commit(repo)
    return repo
