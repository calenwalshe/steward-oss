"""OPX-05 gate (task 3 of 11-01-PLAN): the operator control pane against a
real Herdr session.

Marked `integration`; skipped by default. The control pane (`steward
control`) runs as a real subprocess inside a `steward-test-<pid>` session's
own pane; `steward say` / `steward steer` are invoked as ordinary
subprocesses OUTSIDE any Herdr pane (exactly like Moshi or an MCP relay
would reach it) — proving the cmd really travels through Herdr (SP-31) by
reading the control pane's own redirected log file, never a screen scrape.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from steward import exceptions
from steward.contract import Contract, store_contract
from steward.models import JobState, Judgment
from steward.store import Store

pytestmark = pytest.mark.integration

REPO_ROOT = str(Path(__file__).resolve().parents[2])
PYTHON = sys.executable


def _wait_for(predicate, *, timeout: float, poll: float = 0.3):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(poll)
    pytest.fail(f"condition not met within {timeout}s; last={last!r}")


def _running_job(store: Store, repo: str = "/repo"):
    job = store.create_job("do the thing", repo)
    contract = Contract(
        goal="do the thing", boundaries=[], done_when=["done"], next_step="", authority=[], assumptions=[],
    )
    store_contract(store, job.job_id, contract)  # CREATED -> CONTRACTED
    store.set_state(job.job_id, JobState.RUNNING, what="r", evidence={}, rule="r", judgment=Judgment.code)
    return job


def _cli_env(home: Path, exceptions_path: Path, sock: str) -> dict:
    env = dict(os.environ)
    env["STEWARD_HOME"] = str(home)
    env["STEWARD_EXCEPTIONS"] = str(exceptions_path)
    env["HERDR_SOCKET_PATH"] = sock
    env["PYTHONPATH"] = REPO_ROOT
    return env


def _run_cli(args: list[str], *, env: dict, timeout: float = 15.0) -> subprocess.CompletedProcess:
    """An ordinary subprocess, deliberately NOT inside any Herdr pane —
    the same way Moshi's `steward` CLI or an MCP relay would call `say`/a
    write verb: from outside, reaching the control pane only through
    Herdr frames (SP-31), never by being a mailbox endpoint itself."""
    return subprocess.run(
        [PYTHON, "-m", "steward.cli", *args],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=timeout,
    )


def test_say_and_steer_from_outside_reach_the_control_pane(herdr_test_session, tmp_path):
    session = herdr_test_session
    home = tmp_path / "steward_home"
    home.mkdir()
    exceptions_path = tmp_path / "exceptions.toml"

    store = Store(home)
    job = _running_job(store)

    control_pane_id = session.create_pane(cwd=REPO_ROOT)
    control_log = tmp_path / "control.log"
    control_command = (
        f"export STEWARD_HOME={home} && export STEWARD_EXCEPTIONS={exceptions_path} && "
        f"export PYTHONPATH={REPO_ROOT} && cd {REPO_ROOT} && "
        # -u: redirected to a file, stdout would otherwise be fully
        # buffered (never line-buffered like a tty), so the pane's own
        # readable lines wouldn't hit disk until the process exits.
        f"{PYTHON} -u -m steward.cli control --sock {session.socket_path} --pane {control_pane_id} "
        f">> {control_log} 2>&1"
    )
    session.run_in_pane(control_pane_id, control_command)

    def control_pane_registered():
        result = session.api.call("pane.list", {})
        for pane in result.get("panes", []):
            if pane.get("tokens", {}).get("steward_role") == "control":
                return pane["pane_id"]
        return None

    _wait_for(control_pane_registered, timeout=15.0)

    env = _cli_env(home, exceptions_path, session.socket_path)

    say_result = _run_cli(["say", f"pause {job.job_id}"], env=env)
    assert say_result.returncode == 0, say_result.stderr
    assert "accepted=True" in say_result.stdout

    def job_paused():
        j = store.get_job(job.job_id)
        return j if j and j.state == JobState.PAUSED else None

    _wait_for(job_paused, timeout=15.0)

    steer_result = _run_cli(["steer", job.job_id, "focus on the file", "--verb", "focus"], env=env)
    assert steer_result.returncode == 0, steer_result.stderr
    assert "accepted=True" in steer_result.stdout

    def instruction_recorded():
        instructions = store.kv_get(job.job_id, "instructions", default=[])
        return instructions if instructions else None

    instructions = _wait_for(instruction_recorded, timeout=15.0)
    assert instructions[0]["verb"] == "focus"

    # The commands really traveled through Herdr into the control pane's
    # OWN pane (SP-31): its redirected log — not a screen scrape — shows
    # one readable line per cmd and per result, for both requests.
    def log_has_both_results():
        if not control_log.exists():
            return None
        text = control_log.read_text()
        return text if ("result pause" in text and "result steer" in text) else None

    log_text = _wait_for(log_has_both_results, timeout=15.0)
    assert "cmd pause" in log_text
    assert "cmd steer" in log_text

    # Stop the control pane; `say` must then fail cleanly (exit 4), never
    # silently write direct to the store.
    session.api.call("pane.close", {"pane_id": control_pane_id})

    def pane_gone():
        result = session.api.call("pane.list", {})
        ids = {p["pane_id"] for p in result.get("panes", [])}
        return True if control_pane_id not in ids else None

    _wait_for(pane_gone, timeout=15.0)

    say_after_stop = _run_cli(["say", f"resume {job.job_id}"], env=env)
    assert say_after_stop.returncode == 4
    assert "no control pane" in say_after_stop.stderr

    # The job must still be exactly where the last successful cmd left it
    # — the failed `say` never fell back to a direct write.
    assert store.get_job(job.job_id).state == JobState.PAUSED


def test_unregistered_exception_name_records_violation_event(steward_home, tmp_path):
    """OPX-04: doesn't need Herdr at all — the registry mechanism itself."""
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    registry_path = tmp_path / "exceptions.toml"

    with pytest.raises(exceptions.UnregisteredPath):
        exceptions.use(store, job.job_id, "carrier-pigeon", {}, path=registry_path)

    violations = [e for e in store.events(job.job_id) if e.what == "exception_violation"]
    assert len(violations) == 1
    assert violations[0].evidence["name"] == "carrier-pigeon"
