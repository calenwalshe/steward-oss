"""Venv-aware check discovery + verification: the repo's own interpreter wins over bare/`python3 -m pytest`."""
import os
import stat
import sys

from fixtures.repos import python_repo

from steward.baseline import run_check, run_baseline
from steward.contract import propose_contract
from steward.discovery import discover, explain_broken, project_python
from steward.runner_exec import CommandResult, FakeRunner, SubprocessRunner
from steward.store import Store


def _shim(repo, rel=".venv/bin/python"):
    exe = repo / rel
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def test_venv_python_preferred_and_recorded(tmp_path):
    repo = python_repo(tmp_path)
    exe = _shim(repo)
    d = discover(repo)
    cmd = [c for c in d.commands if "pytest" in c.line][0]
    assert cmd.line == f"{exe.resolve()} -m pytest"
    assert cmd.kind == "check" and ".venv" in cmd.source
    assert d.python_env == ".venv"
    contract = propose_contract("goal", d, None)
    assert f"run: {cmd.line}" in contract.authority
    assert any(".venv" in a and "same command" in a for a in contract.assumptions)


def test_venv_order_and_windows_layout(tmp_path):
    repo = python_repo(tmp_path)
    _shim(repo, "venv/bin/python")
    assert project_python(repo)[1] == "venv"
    _shim(repo)
    assert project_python(repo)[1] == ".venv"
    repo2 = python_repo(tmp_path, "win")
    _shim(repo2, ".venv/Scripts/python.exe")
    assert project_python(repo2)[1] == ".venv"


def test_uv_and_poetry_fallbacks(tmp_path, monkeypatch):
    monkeypatch.setattr("steward.discovery.shutil.which", lambda n: f"/bin/{n}")
    repo = python_repo(tmp_path)
    (repo / "uv.lock").write_text("")
    assert project_python(repo) == ("uv run python", "uv")
    (repo / "poetry.lock").write_text("")
    assert project_python(repo) == ("poetry run python", "poetry")
    assert [c.line for c in discover(repo).commands][0] == "poetry run python -m pytest"
    monkeypatch.setattr("steward.discovery.shutil.which", lambda n: None)
    assert project_python(repo) is None


def test_no_env_keeps_bare_pytest(tmp_path):
    assert [c.line for c in discover(python_repo(tmp_path)).commands] == ["pytest"]


def test_explicit_steward_toml_command_wins(tmp_path):
    repo = python_repo(tmp_path)
    _shim(repo)
    (repo / "steward.toml").write_text('[checks]\ncommand = "make verify-all"\n')
    d = discover(repo)
    assert d.commands[0].line == "make verify-all"
    assert d.commands[0].kind == "check" and d.commands[0].source.startswith("steward.toml")
    assert not any("pytest" in c.line for c in d.commands)


def test_explicit_command_still_blocks_unsafe(tmp_path):
    repo = python_repo(tmp_path)
    (repo / "steward.toml").write_text('[checks]\ncommands = ["npm run deploy"]\n')
    assert discover(repo).commands[0].kind == "unclassified"


def test_real_run_uses_venv_for_baseline(steward_home, tmp_path):
    repo = python_repo(tmp_path)
    _shim(repo)
    store = Store(steward_home)
    job = store.create_job("goal", str(repo))
    d = discover(repo)
    base = run_baseline(store, job.job_id, repo, d, SubprocessRunner())
    assert base.results[0].command == d.commands[0].line
    assert "passed" in base.results[0].tests.values()


def test_import_collection_error_under_fallback_is_unverified_not_failure(tmp_path):
    out = "E   ModuleNotFoundError: No module named 'librosa'\n"
    runner = FakeRunner(lambda argv, cwd: CommandResult(
        argv=argv, cwd=cwd, exit_code=2, stdout=out, stderr="", duration_s=0.0))
    run = run_check(runner, "pytest", tmp_path, tmp_path / "x.xml", python_env=".venv")
    assert run.broken
    assert "missing dependency librosa" in run.broken and "repo has .venv" in run.broken


def test_explain_broken():
    assert explain_broken("pytest ... (exit 2)", "") == "pytest ... (exit 2)"
    assert "missing dependency yaml" in explain_broken("x", "No module named 'yaml.foo'")
