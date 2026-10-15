"""WRK-05 gate: FabricWorker + shim, fake agent, real Herdr panes.

Marked `integration`; skipped by default. `steward run --worker fabric`
executes as a real subprocess inside a `steward-test-<pid>` session's own
pane (the "runner pane"); `FabricWorker.start()`'s `spawn_pane` splits a
second real pane off it and runs `steward shim --agent fake` there. Both
processes talk only in typed frames over Herdr (SP-31) — steer/approve are
issued directly through `steward.api` against the same STEWARD_HOME sqlite
store the runner subprocess polls each tick (WAL mode; the same path
`steward steer`/`steward approve` already use — no Herdr control pane
involved, that's Phase 11/OPX-*).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from steward import approvals as approvals_module
from steward.api import StewardAPI
from steward.contract import Contract, store_contract
from steward.runner_exec import SubprocessRunner
from steward.store import Store
from steward.worktree import create_worktree

pytestmark = pytest.mark.integration

REPO_ROOT = str(Path(__file__).resolve().parents[2])
PYTHON = sys.executable


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _scratch_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "root", cwd=repo)
    return repo


def _contracted_job(store: Store, repo: Path):
    job = store.create_job("do the thing", str(repo))
    contract = Contract(
        goal="do the thing", boundaries=[], done_when=["done"], next_step="", authority=[], assumptions=[]
    )
    store_contract(store, job.job_id, contract)
    return job


def _write_script(path: Path, steps: list[dict]) -> None:
    path.write_text(json.dumps({"steps": steps}))


def _run_command(home: Path, job_id: str, *, sock: str, pane: str, script: Path, interval: float) -> str:
    return (
        f"export STEWARD_HOME={home} && export PYTHONPATH={REPO_ROOT} && cd {REPO_ROOT} && "
        f"{PYTHON} -m steward.cli run {job_id} --worker fabric --sock {sock} --pane {pane} "
        f"--agent fake --agent-script {script} --interval {interval} "
        f">> {home}/run.log 2>&1"
    )


def _wait_for(predicate, *, timeout: float, poll: float = 0.5):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(poll)
    pytest.fail(f"condition not met within {timeout}s; last={last!r}")


def _pending_permission(store: Store, job_id: str):
    approvals = approvals_module.list_approvals(store, job_id)  # a table since TUI speed plan 10a
    return next((a for a in approvals if a["kind"] == "permission" and a["status"] == "pending"), None)


# ---------------------------------------------------------------- (a) happy path


def test_happy_path_steer_permission_approve_done_reaches_terminal(herdr_test_session, tmp_path):
    session = herdr_test_session
    home = tmp_path / "steward_home"
    home.mkdir()
    repo = _scratch_repo(tmp_path)

    store = Store(home)
    job = _contracted_job(store, repo)
    create_worktree(str(repo), job.job_id, SubprocessRunner(), base_dir=home / "worktrees")

    script = tmp_path / "happy.json"
    _write_script(
        script,
        [
            {"say": "on it"},
            # SP-36 (14-01/NB-02): default-allow means an ordinary command
            # like "touch x" auto-approves within the same tick and never
            # stays pending — `git push` is on the outbound list, so it's
            # still a genuine human-approval wait to exercise here.
            {"tool_use": {"name": "Bash", "input": {"command": "git push origin main"}, "permission": True}},
            {"commit": "did the thing"},
            {"result": {"subtype": "success", "cost": 0.01}},
        ],
    )

    runner_pane = session.create_pane(cwd=REPO_ROOT)
    command = _run_command(home, job.job_id, sock=session.socket_path, pane=runner_pane, script=script, interval=1.0)
    session.run_in_pane(runner_pane, command)

    approval = _wait_for(lambda: _pending_permission(store, job.job_id), timeout=30.0)

    api = StewardAPI(store)
    steer_receipt = api.steer(job.job_id, "focus on the file", verb="focus", origin="cli")
    assert steer_receipt["accepted"] is True

    approve_receipt = api.approve(job.job_id, approval["id"], decision="approve")
    assert approve_receipt["accepted"] is True

    def terminal_state():
        j = store.get_job(job.job_id)
        return j if j and j.state.value in ("CHECKS_PASSED", "ACCEPTED", "NEEDS_HUMAN", "FAILED") else None

    final_job = _wait_for(terminal_state, timeout=60.0)
    log = (home / "run.log").read_text() if (home / "run.log").exists() else ""
    assert final_job.state.value in ("CHECKS_PASSED", "ACCEPTED"), log

    # the steer instruction reached "acknowledged" via a typed ack frame,
    # not text-scanned STEWARD-ACK output (WRK-02).
    instructions = store.kv_get(job.job_id, "instructions", default=[])
    assert instructions[0]["status"] == "acknowledged", instructions

    ack_events = [e for e in store.events(job.job_id) if e.rule == "fabric_ack_frame"]
    assert any(e.evidence.get("stage") == "acknowledged" for e in ack_events)
    done_events = [e for e in store.events(job.job_id) if e.rule == "fabric_done_frame"]
    assert len(done_events) == 1 and done_events[0].evidence.get("sha")


# ---------------------------------------------------------------- (b) crash -> recovery


def test_crash_mid_turn_replaces_worker_with_old_pane_confirmed_exited(herdr_test_session, tmp_path):
    session = herdr_test_session
    home = tmp_path / "steward_home"
    home.mkdir()
    repo = _scratch_repo(tmp_path)

    store = Store(home)
    job = _contracted_job(store, repo)
    create_worktree(str(repo), job.job_id, SubprocessRunner(), base_dir=home / "worktrees")

    script = tmp_path / "crash.json"
    _write_script(script, [{"crash": 1}])

    runner_pane = session.create_pane(cwd=REPO_ROOT)
    command = _run_command(home, job.job_id, sock=session.socket_path, pane=runner_pane, script=script, interval=1.0)
    session.run_in_pane(runner_pane, command)

    first_pane = _wait_for(lambda: store.kv_get(job.job_id, "herdr_pane"), timeout=30.0)

    def replaced():
        pane = store.kv_get(job.job_id, "herdr_pane")
        return pane if pane and pane != first_pane else None

    second_pane = _wait_for(replaced, timeout=30.0)
    assert second_pane != first_pane

    # single writer (SP-14): the old worker pane's foreground process has
    # already exited (its shell is back in the foreground) — confirmed via
    # Herdr's own process-info, independent of FabricWorker's internal
    # bookkeeping.
    def old_pane_shell_foreground():
        info = session.api.call("pane.process_info", {"pane_id": first_pane})["process_info"]
        return info if info["foreground_process_group_id"] == info["shell_pid"] else None

    _wait_for(old_pane_shell_foreground, timeout=15.0)

    def terminal_state():
        j = store.get_job(job.job_id)
        return j if j and j.state.value in ("NEEDS_HUMAN", "FAILED", "CHECKS_PASSED", "ACCEPTED") else None

    final_job = _wait_for(terminal_state, timeout=60.0)
    log = (home / "run.log").read_text() if (home / "run.log").exists() else ""
    assert final_job.state.value == "NEEDS_HUMAN", log  # recovery budget exhausted; the script only ever crashes

    replace_events = [e for e in store.events(job.job_id) if e.rule == "worker_replacement"]
    assert len(replace_events) >= 1
    error_events = [e for e in store.events(job.job_id) if e.rule == "fabric_error_frame"]
    assert len(error_events) >= 1


# ---------------------------------------------------------------- (c) controller restart


def _runner_process_gone(job_id: str) -> bool | None:
    check = subprocess.run(["pgrep", "-f", f"steward.cli run {job_id}"], capture_output=True)
    return True if check.returncode != 0 else None


def test_controller_restart_reattaches_no_second_writer(herdr_test_session, tmp_path):
    """10-04: kill the runner mid-run; a fresh `steward run` in a new pane
    reattaches to the still-live shim instead of starting a second worker."""
    session = herdr_test_session
    home = tmp_path / "steward_home"
    home.mkdir()
    repo = _scratch_repo(tmp_path)

    store = Store(home)
    job = _contracted_job(store, repo)
    create_worktree(str(repo), job.job_id, SubprocessRunner(), base_dir=home / "worktrees")

    script = tmp_path / "resume.json"
    _write_script(
        script,
        [
            {"say": "starting"},
            {"sleep": 10},
            {"commit": "did the thing"},
            {"result": {"subtype": "success", "cost": 0.01}},
        ],
    )

    runner_pane_1 = session.create_pane(cwd=REPO_ROOT)
    command_1 = _run_command(home, job.job_id, sock=session.socket_path, pane=runner_pane_1, script=script, interval=1.0)
    session.run_in_pane(runner_pane_1, command_1)

    worker_pane = _wait_for(lambda: store.kv_get(job.job_id, "herdr_pane"), timeout=30.0)

    # Kill the first runner process mid-run. The shim + agent in the worker
    # pane are separate OS processes — they never see this at all, they only
    # ever talk to the runner through Herdr frames.
    subprocess.run(["pkill", "-9", "-f", f"steward.cli run {job.job_id}"])
    _wait_for(lambda: _runner_process_gone(job.job_id), timeout=15.0)

    # exactly one shim so far: the worker pane's own foreground process is
    # still something other than a bare shell.
    info = session.api.call("pane.process_info", {"pane_id": worker_pane})["process_info"]
    assert info["foreground_process_group_id"] != info["shell_pid"]

    runner_pane_2 = session.create_pane(cwd=REPO_ROOT)
    command_2 = _run_command(home, job.job_id, sock=session.socket_path, pane=runner_pane_2, script=script, interval=1.0)
    session.run_in_pane(runner_pane_2, command_2)

    def terminal_state():
        j = store.get_job(job.job_id)
        return j if j and j.state.value in ("CHECKS_PASSED", "ACCEPTED", "NEEDS_HUMAN", "FAILED") else None

    final_job = _wait_for(terminal_state, timeout=60.0)
    log = (home / "run.log").read_text() if (home / "run.log").exists() else ""
    assert final_job.state.value in ("CHECKS_PASSED", "ACCEPTED"), log

    # exactly one shim throughout: the worker pane never changed (no
    # replacement worker was ever dispatched — attach() succeeded).
    assert store.kv_get(job.job_id, "herdr_pane") == worker_pane
    reattach_events = [e for e in store.events(job.job_id) if e.rule == "fabric_resume_attach"]
    assert len(reattach_events) == 1 and reattach_events[0].evidence["attached"] is True
    replace_events = [e for e in store.events(job.job_id) if e.rule == "worker_replacement"]
    assert replace_events == []
