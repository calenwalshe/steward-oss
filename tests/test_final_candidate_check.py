from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import final_candidate_check as final_check


def _repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "fixture"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=path, check=True)
    (path / "tracked.txt").write_text("candidate\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "candidate"], cwd=path, check=True)
    subprocess.run(["git", "branch", "fleet-registry"], cwd=path, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True,
                          text=True).stdout.strip()


def test_final_candidate_snapshot_requires_clean_head_containing_source(tmp_path):
    repo = tmp_path / "repo"
    candidate = _repo(repo)
    snapshot = final_check.candidate_snapshot(repo, candidate, "fleet-registry")
    assert snapshot["candidate_commit"] == candidate and snapshot["source_head"] == candidate

    (repo / "tracked.txt").write_text("candidate moved\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "move candidate"], cwd=repo, check=True)
    assert not final_check._candidate_current(repo, snapshot["candidate_ref"], candidate)
    subprocess.run(["git", "reset", "--hard", candidate], cwd=repo, check=True)

    (repo / "untracked.txt").write_text("dirty\n")
    with pytest.raises(final_check.CandidateError, match="dirty"):
        final_check.candidate_snapshot(repo, candidate, "fleet-registry")


def test_final_candidate_start_is_one_shot_and_pins_receipt(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    candidate = _repo(repo)
    evidence = tmp_path / "evidence"
    receipt = evidence / "receipt.json"
    monkeypatch.setattr(final_check, "EVIDENCE", evidence)
    monkeypatch.setattr(final_check, "RECEIPT", receipt)
    monkeypatch.setattr(final_check, "JUNIT", evidence / "junit.xml")
    monkeypatch.setattr(final_check, "LOG", evidence / "log.txt")
    from steward import bg_baseline

    spawned = {}

    def fake_spawn(_argv, *, env):
        spawned.update(env)
        return 12345

    monkeypatch.setattr(bg_baseline, "_spawn_detached", fake_spawn)
    result = final_check.start(repo, candidate, "fleet-registry")
    assert result["state"] == "running" and result["pid"] == 12345
    assert "PYTHONPATH" not in spawned and "GITHUB_TOKEN" not in spawned
    assert spawned["HOME"].endswith("steward-home")
    assert json.loads(receipt.read_text())["candidate_commit"] == candidate
    with pytest.raises(final_check.CandidateError, match="one-shot"):
        final_check.start(repo, candidate, "fleet-registry")


def test_final_candidate_refuses_a_source_branch_that_moves_ahead(tmp_path):
    repo = tmp_path / "repo"
    candidate = _repo(repo)
    (repo / "source.txt").write_text("new source\n")
    subprocess.run(["git", "add", "source.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "advance source"], cwd=repo, check=True)
    subprocess.run(["git", "branch", "-f", "fleet-registry", "HEAD"], cwd=repo, check=True)
    subprocess.run(["git", "reset", "--hard", candidate], cwd=repo, check=True)
    with pytest.raises(final_check.CandidateError, match="does not contain"):
        final_check.candidate_snapshot(repo, candidate, "fleet-registry")


def test_final_suite_environment_excludes_credentials_and_user_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "secret")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/live/socket")
    monkeypatch.setenv("PYTHONPATH", "/user/config")
    env = final_check.isolated_test_environment(tmp_path / "home", tmp_path / "tmp")
    assert env["HOME"] == str(tmp_path / "home")
    assert env["TMPDIR"] == str(tmp_path / "tmp")
    assert env["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)
    assert "GITHUB_TOKEN" not in env and "AWS_ACCESS_KEY_ID" not in env
    assert "HERDR_SOCKET_PATH" not in env and "PYTHONPATH" not in env


def test_junit_case_identity_preserves_classes_and_parameter_ids():
    assert final_check.junit_case_to_nodeid("tests.test_a::test_x") == "tests/test_a.py::test_x"
    assert final_check.junit_case_to_nodeid("tests.test_a.TestC::test_x[value]") == (
        "tests/test_a.py::TestC::test_x[value]")
    assert final_check.junit_case_to_nodeid("not-a-module") is None


def test_failed_candidate_cases_without_durable_baseline_receipt_stay_unattributed(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "fixture"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repo, check=True)
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text("def test_failing_case():\n    assert False\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "source"], cwd=repo, check=True)
    source = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                            check=True).stdout.strip()

    from steward import check_runs
    from steward.baseline import CheckRun
    from steward.store import Store

    store = Store(tmp_path / "store")
    job = store.create_job("attribution fixture", str(repo))
    seen = []

    def fake_shared_check(_store, _job_id, **kwargs):
        def run(_runner, command, cwd, xml_path):
            seen.append((command, Path(cwd), Path(xml_path), final_check._git(Path(cwd), "rev-parse", "HEAD")))
            return CheckRun(exit_code=1, tests={"tests.test_sample::test_failing_case": "failed"})
        return run

    monkeypatch.setattr(check_runs, "shared_check_fn", fake_shared_check)
    runtime = tmp_path / "run"
    runtime.mkdir()
    attr, baseline_id = final_check.compare_candidate_failures(
        store, job.job_id, repo, source, {"tests.test_sample::test_failing_case": "failed"}, 1, runtime)

    assert "test_failing_case" in seen[0][0] and "exec python3 -m pytest -q" in seen[0][0]
    assert seen[0][1] != repo and seen[0][3] == source
    assert not attr["preexisting_failures"] and not attr["new_failures"]
    assert attr["unattributed_failures"] == ["tests.test_sample::test_failing_case"]
    assert attr["complete"] is False
    assert attr["baseline_status"] == "unavailable_not_compared"
    assert baseline_id is None
    store.close()


def test_missing_baseline_case_stays_unattributed():
    attr = final_check.attribute_failed_cases({"tests.test_a::test_x": "failed"}, {})
    assert attr["complete"] is False and attr["unattributed_failures"] == ["tests.test_a::test_x"]


def test_start_command_returns_failure_when_detached_launch_fails(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    candidate = _repo(repo)
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(final_check, "EVIDENCE", evidence)
    monkeypatch.setattr(final_check, "RECEIPT", evidence / "receipt.json")
    monkeypatch.setattr(final_check, "JUNIT", evidence / "junit.xml")
    monkeypatch.setattr(final_check, "LOG", evidence / "log.txt")
    from steward import bg_baseline

    def fail(_argv, *, env):
        assert "PYTHONPATH" not in env
        raise OSError("no process")

    monkeypatch.setattr(bg_baseline, "_spawn_detached", fail)
    assert final_check.main(["--start", "--repo", str(repo), "--candidate", candidate,
                             "--source-branch", "fleet-registry"]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "failed"
