"""TRJ-14 gate: a live trajectory redirect on a throwaway Herdr session.

Marked `integration`; skipped by default. Two real panes in a
`steward-test-<pid>` session (the `herdr_test_session` fixture): a `steward
advisor --reviewer fake` pane and a `steward run --worker fabric --agent
fake` pane, exactly the `tests/integration/test_worker_fabric_live.py`
pattern plus the trajectory outer loop. Never a real `claude` — the fake
reviewer and fake agent are scripted, deterministic JSON files.

The fake agent pauses on a permission-gated `tool_use` (rather than a plain
`sleep`) so the pause lasts exactly as long as the test needs — long enough
for a cadence-5s trajectory review round trip to land and be acknowledged —
without depending on wall-clock sleep timing inside a real subprocess
(DEVIATION from the plan's literal "sleeps totalling ~40s" script shape; see
12-04-SUMMARY.md).
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


def test_live_trajectory_redirect_acknowledged_and_closeout_deferred(herdr_test_session, tmp_path):
    session = herdr_test_session
    home = tmp_path / "steward_home"
    home.mkdir()
    exceptions_path = tmp_path / "exceptions.toml"
    repo = _scratch_repo(tmp_path)

    store = Store(home)
    job = _contracted_job(store, repo)
    create_worktree(str(repo), job.job_id, SubprocessRunner(), base_dir=home / "worktrees")

    # -- Astra's fake script: an off_track redirect, then sound forever --------
    reviewer_script = tmp_path / "reviewer.json"
    reviewer_script.write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "trajectory": "off_track",
                        "confidence": 0.95,
                        "urgency": "high",
                        "concern": "wrong_approach",
                        "observation": "drifting off the contract",
                        "recommendation": {
                            "action": "change_approach",
                            "instruction": "switch to approach b",
                            "current_approach": "approach-a",
                            "recommended_approach": "approach-b",
                        },
                    },
                    {
                        "trajectory": "sound",
                        "confidence": 0.9,
                        "urgency": "normal",
                        "concern": "none",
                        "observation": "on track",
                        "recommendation": {
                            "action": "continue",
                            "instruction": "keep going",
                            "current_approach": "approach-b",
                            "recommended_approach": "",
                        },
                    },
                ]
            }
        )
    )

    # -- the fake agent: TodoWrite (plan) + a worker-observed "3 passed" test
    #    result, then pauses on a permission-gated tool_use -- long enough for
    #    the trajectory redirect to be delivered and acknowledged before the
    #    job is ever allowed to finish.
    agent_script = tmp_path / "agent.json"
    agent_script.write_text(
        json.dumps(
            {
                "steps": [
                    {"say": "starting"},
                    {
                        "tool_use": {
                            "name": "TodoWrite",
                            "input": {"todos": [{"content": "step one"}, {"content": "step two"}, {"content": "step three"}]},
                        }
                    },
                    {"tool_result": "3 passed"},
                    # SP-36 (14-01/NB-02): "true" is default-allow now and
                    # auto-approves within the same tick, so it no longer
                    # pauses the worker at all — "git push" is still on the
                    # outbound list, so this still pauses long enough for
                    # the trajectory redirect to land and be acknowledged.
                    {"tool_use": {"name": "Bash", "input": {"command": "git push origin main"}, "permission": True}},
                    {"commit": "did the thing"},
                    {"result": {"subtype": "success", "cost": 0.01}},
                ]
            }
        )
    )

    # -- T: a fast trajectory config (cadence_s=5, settle_s stays long so a
    #    second redirect never fires once this one is authorized).
    trajectory_config = tmp_path / "trajectory.toml"
    trajectory_config.write_text(
        'mode = "active"\n[triggers]\ncadence_s = 5\nsteady_cadence_s = 5\nmin_gap_s = 0\nsettle_s = 3600\n'
    )

    advisor_pane = session.create_pane(cwd=REPO_ROOT)
    advisor_log = tmp_path / "advisor.log"
    advisor_command = (
        f"export STEWARD_HOME={home} && export STEWARD_EXCEPTIONS={exceptions_path} && "
        f"export PYTHONPATH={REPO_ROOT} && cd {REPO_ROOT} && "
        f"{PYTHON} -u -m steward.cli advisor --sock {session.socket_path} --pane {advisor_pane} "
        f"--reviewer fake --reviewer-script {reviewer_script} >> {advisor_log} 2>&1"
    )
    session.run_in_pane(advisor_pane, advisor_command)

    def advisor_registered():
        result = session.api.call("pane.list", {})
        for pane in result.get("panes", []):
            if pane.get("tokens", {}).get("steward_role") == "advisor":
                return pane["pane_id"]
        return None

    _wait_for(advisor_registered, timeout=15.0)

    runner_pane = session.create_pane(cwd=REPO_ROOT)
    run_log = tmp_path / "run.log"
    run_command = (
        f"export STEWARD_HOME={home} && export STEWARD_EXCEPTIONS={exceptions_path} && "
        f"export PYTHONPATH={REPO_ROOT} && cd {REPO_ROOT} && "
        f"{PYTHON} -m steward.cli run {job.job_id} --worker fabric --sock {session.socket_path} "
        f"--pane {runner_pane} --agent fake --agent-script {agent_script} "
        f"--trajectory active --trajectory-config {trajectory_config} --interval 1.0 "
        f">> {run_log} 2>&1"
    )
    session.run_in_pane(runner_pane, run_command)

    # -- the advisor log shows `review` + `-> off_track` lines ----------------
    def advisor_saw_off_track():
        if not advisor_log.exists():
            return None
        text = advisor_log.read_text()
        return text if ("review " in text and "-> off_track" in text) else None

    _wait_for(advisor_saw_off_track, timeout=60.0)

    # -- exactly 1 origin=trajectory instruction, acknowledged via a typed ack
    #    frame from the real shim -----------------------------------------------
    def trajectory_instruction_acknowledged():
        instructions = store.kv_get(job.job_id, "instructions", default=[])
        matches = [i for i in instructions if i.get("origin") == "trajectory"]
        return matches[0] if matches and matches[0]["status"] == "acknowledged" else None

    instr = _wait_for(trajectory_instruction_acknowledged, timeout=60.0)
    ack_events = [
        e for e in store.events(job.job_id)
        if e.rule == "fabric_ack_frame" and e.evidence.get("rev") == instr["rev"] and e.evidence.get("stage") == "acknowledged"
    ]
    assert len(ack_events) == 1
    ack_seq = ack_events[0].seq

    instructions = store.kv_get(job.job_id, "instructions", default=[])
    trajectory_instructions = [i for i in instructions if i.get("origin") == "trajectory"]
    assert len(trajectory_instructions) == 1

    # -- an exception_used event with name supervisor-model and role astra ---
    exception_events = [
        e for e in store.events(job.job_id)
        if e.what == "exception_used" and e.evidence.get("name") == "supervisor-model" and e.evidence.get("role") == "astra"
    ]
    assert len(exception_events) >= 1

    # -- now let the job actually finish: approve the permission ---------------
    approval = _wait_for(lambda: _pending_permission(store, job.job_id), timeout=30.0)
    api = StewardAPI(store)
    approve_receipt = api.approve(job.job_id, approval["id"], decision="approve")
    assert approve_receipt["accepted"] is True

    def terminal_state():
        j = store.get_job(job.job_id)
        return j if j and j.state.value in ("CHECKS_PASSED", "ACCEPTED", "NEEDS_HUMAN", "FAILED") else None

    final_job = _wait_for(terminal_state, timeout=60.0)
    run_log_text = run_log.read_text() if run_log.exists() else ""
    assert final_job.state.value in ("CHECKS_PASSED", "ACCEPTED"), run_log_text

    # -- closeout deferral: the job reaches a terminal state only AFTER the ack
    all_events = store.events(job.job_id)
    terminal_events = [e for e in all_events if e.rule in ("checks_pass_at_sha", "preauthorized_local_acceptance")]
    assert terminal_events, [e.what for e in all_events]
    assert terminal_events[0].seq > ack_seq

    # -- the advisor never sends a frame: its log has no "->" lines besides its
    #    own readable verdict lines (`-> <trajectory> ...` / `-> <status>: ...`)
    advisor_text = advisor_log.read_text()
    for line in advisor_text.splitlines():
        if "->" not in line:
            continue
        assert line.strip().startswith("->"), line

    # -- teardown leaves no steward-test-* session behind (belt + suspenders;
    #    the herdr_test_session fixture's own teardown already does this) ------
    session.teardown()
    listing = subprocess.run(["herdr", "session", "list", "--json"], capture_output=True, text=True)
    if listing.returncode == 0 and listing.stdout.strip():
        sessions = json.loads(listing.stdout).get("sessions", [])
        assert all(not s["name"].startswith("steward-test-") for s in sessions)
