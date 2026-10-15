"""Behavioral regressions from job f3ef4b4d; isolated controller and worker paths only."""

from steward import approvals, bg_verify, closeout, outcome_scope, session, stability
from steward.contract import Contract, store_contract
from steward.control_pane import ControlPane
from steward.fabric import Frame
from steward.models import JobState, Judgment
from steward.runner import Runner
from steward.steering import Steering
from steward.store import Store
from steward.verify import Verification
from test_runner_fabric import (
    _Clock, _FabricFactory, _contracted_job, _fake_diagnoser, _frame, _git_runner, _obs,
)
from test_shim import FakeApi, _make_shim, _run_until, _send_frame, _sent_of_type, _write_script
import os
from test_push import _herdr, _pane
from steward import push


def test_continuous_ownership_recovery_acceptance_acceptance_error_result_stays_recoverable(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    failed = _obs(lifecycle="working", events=(_frame(11, "done", {
        "sha": "abc123", "result_subtype": "error_during_execution", "is_error": True,
    }),))
    factory = _FabricFactory(scripts=[[failed]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)

    runner.tick(job.job_id)

    assert store.get_job(job.job_id).state == JobState.RUNNING
    assert not [e for e in store.events(job.job_id) if e.rule == "fabric_done_frame"]
    failure = [e for e in store.events(job.job_id) if e.rule == "fabric_turn_error"]
    assert failure and failure[-1].evidence["result_subtype"] == "error_during_execution"


def test_continuous_ownership_recovery_acceptance_harness_error_frame_stays_recoverable(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    failed = _obs(lifecycle="exited", events=(_frame(11, "error", {
        "status": "interrupted", "detail": "harness interrupted",
    }),), pid_alive=False)
    runner = Runner(store, _FabricFactory(scripts=[[failed]]), _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)

    runner.tick(job.job_id)

    assert store.get_job(job.job_id).state == JobState.RUNNING
    failure = [e for e in store.events(job.job_id) if e.rule == "fabric_turn_error"]
    assert failure and failure[-1].evidence["result_subtype"] == "interrupted"


def test_continuous_ownership_recovery_acceptance_error_result_cannot_idle_closeout(steward_home, monkeypatch):
    store = Store(steward_home)
    job = _contracted_job(store)
    failed = _obs(lifecycle="idle", events=(_frame(11, "done", {
        "sha": "abc123", "result_subtype": "error_during_execution", "is_error": True,
    }),))
    activity = {"turns": [{"kind": "said", "ts": "before-error", "text": "finished all the work"}]}
    runner = Runner(store, _FabricFactory(scripts=[[failed, _obs(lifecycle="idle")]]), _git_runner(),
                    _fake_diagnoser(), clock=_Clock(), activity_fn=lambda *_: activity)
    runner.start(job.job_id)

    runner.tick(job.job_id)
    runner.tick(job.job_id)

    assert store.get_job(job.job_id).state == JobState.RUNNING
    assert not [e for e in store.events(job.job_id) if e.rule == "idle_after_final_message"]


def test_continuous_ownership_recovery_acceptance_during_background_verification_reaches_worker(
        steward_home, monkeypatch, tmp_path):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _FabricFactory(scripts=[[_obs()]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    store.set_state(job.job_id, JobState.WORKER_FINISHED, what="verification started",
                    evidence={"sha": "sha-1"}, rule="test", judgment=Judgment.code)
    bg_verify.start(store, job.job_id, tmp_path, "sha-1", spawn=lambda _argv: 123, now=1000.0)
    monkeypatch.setattr(bg_verify, "poll", lambda *_a, **_kw: ("running", None, ""))
    receipt = Steering(store).record(job.job_id, "focus", "continue the implementation", "recovery-1")
    assert receipt.accepted

    runner.tick(job.job_id)

    assert factory.created[0].delivered == ["[rev=1] continue the implementation"]
    assert store.get_job(job.job_id).state == JobState.RUNNING
    assert not bg_verify.pending(store, job.job_id)
    instruction = Steering(store).all_instructions(job.job_id)[0]
    assert instruction["status"] == "delivered"  # not mislabeled as acknowledged or observed progress


def test_continuous_ownership_recovery_acceptance_failed_reattach_keeps_verification_retryable(
        steward_home, monkeypatch, tmp_path):
    store = Store(steward_home)
    job = _contracted_job(store)
    runner = Runner(store, _FabricFactory(scripts=[[_obs()]]), _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    store.set_state(job.job_id, JobState.WORKER_FINISHED, what="verification started",
                    evidence={"sha": "sha-1"}, rule="test", judgment=Judgment.code)
    bg_verify.start(store, job.job_id, tmp_path, "sha-1", spawn=lambda _argv: 123, now=1000.0)
    monkeypatch.setattr(bg_verify, "poll", lambda *_a, **_kw: ("running", None, ""))
    Steering(store).record(job.job_id, "focus", "continue implementation", "recovery-1")
    runner._workers.pop(job.job_id, None)
    attempts = []
    monkeypatch.setattr(runner, "resume_all", lambda ids: attempts.append(ids) or [])

    first = runner.tick(job.job_id)
    second = runner.tick(job.job_id)

    assert first.action == second.action == "recovery_pending"
    assert attempts == [[job.job_id], [job.job_id]]
    assert store.get_job(job.job_id).state == JobState.WORKER_FINISHED
    assert bg_verify.pending(store, job.job_id)
    assert Steering(store).all_instructions(job.job_id)[0]["status"] == "received"


def test_continuous_ownership_recovery_acceptance_completed_check_cannot_close_before_pending_steer(
        steward_home, monkeypatch, tmp_path):
    store = Store(steward_home)
    job = _contracted_job(store)
    runner = Runner(store, _FabricFactory(scripts=[[_obs()]]), _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    store.set_state(job.job_id, JobState.WORKER_FINISHED, what="verification started",
                    evidence={"sha": "sha-1"}, rule="test", judgment=Judgment.code)
    bg_verify.start(store, job.job_id, tmp_path, "sha-1", spawn=lambda _argv: 123, now=1000.0)
    outcomes = iter([("running", None, ""), ("done", Verification(commit_sha="sha-1"), "")])
    monkeypatch.setattr(bg_verify, "poll", lambda *_a, **_kw: next(outcomes))
    Steering(store).record(job.job_id, "focus", "continue implementation", "recovery-1")
    runner._workers.pop(job.job_id, None)
    monkeypatch.setattr(runner, "resume_all", lambda _ids: [])

    runner.tick(job.job_id)
    report = runner.tick(job.job_id)

    assert report.action == "recovery_pending"
    assert store.get_job(job.job_id).state == JobState.WORKER_FINISHED
    assert bg_verify.pending(store, job.job_id)
    assert Steering(store).all_instructions(job.job_id)[0]["status"] == "received"
    assert not [e for e in store.events(job.job_id) if e.rule == "closeout_complete"]


def test_continuous_ownership_recovery_acceptance_unknown_live_outcome_blocks_green_suite(steward_home, monkeypatch):
    monkeypatch.setattr(closeout, "acceptance_mode", lambda: "auto")
    store = Store(steward_home)
    job = store.create_job("implement transfer recovery", "/repo")
    contract = Contract(goal=job.goal, done_when=["Active transfer recovers on the real device (live)"])
    store_contract(store, job.job_id, contract)
    store.set_state(job.job_id, JobState.RUNNING, what="worker started", evidence={}, rule="test",
                    judgment=Judgment.code)
    store.set_state(job.job_id, JobState.WORKER_FINISHED, what="worker reported done", evidence={}, rule="test",
                    judgment=Judgment.code)

    result = closeout.closeout(store, job.job_id, Verification(commit_sha="sha-1"), [], contract)

    assert result == JobState.NEEDS_HUMAN
    assert "outcome unproven (live)" in " ".join(store.events(job.job_id)[-1].evidence["reasons"])
    assert outcome_scope.unproven(store, job.job_id, contract, commit_sha="sha-1")


def test_continuous_ownership_recovery_acceptance_untyped_implementation_outcome_is_not_suite_proof(
        steward_home, monkeypatch):
    monkeypatch.setattr(closeout, "acceptance_mode", lambda: "auto")
    store = Store(steward_home)
    job = store.create_job("implement all approved deliverables", "/repo")
    contract = Contract(goal=job.goal,
                        done_when=["Implement all approved deliverables; passing tests alone are not completion"])
    store_contract(store, job.job_id, contract)
    store.set_state(job.job_id, JobState.RUNNING, what="worker started", evidence={}, rule="test",
                    judgment=Judgment.code)
    store.set_state(job.job_id, JobState.WORKER_FINISHED, what="worker reported done", evidence={}, rule="test",
                    judgment=Judgment.code)

    result = closeout.closeout(store, job.job_id, Verification(commit_sha="sha-1"), [], contract)

    assert result == JobState.NEEDS_HUMAN
    assert outcome_scope.unproven(store, job.job_id, contract, commit_sha="sha-1")


def test_continuous_ownership_recovery_acceptance_routine_steer_preserves_isolated_child_work(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "base"], cwd=repo, check=True)
    script = _write_script(repo, [{"hang": True}, {"commit": "child work completed"},
                                  {"result": {"subtype": "success"}}])
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        shim.start()
        _send_frame(write_fd, 1, "brief", {"text": "work with a child task"})
        _run_until(shim, lambda: shim._turn_active and shim._agent is not None)
        _send_frame(write_fd, 2, "steer", {"rev": 1, "text": "routine status reminder"})
        _run_until(shim, lambda: bool(_sent_of_type(api, "done")))
        assert not [f for f in api.sent if f[1].type == "event" and f[1].body.get("kind") == "interrupted"]
        assert subprocess.run(["git", "log", "--format=%s"], cwd=repo, capture_output=True,
                              text=True, check=True).stdout.startswith("child work completed")
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_continuous_ownership_recovery_acceptance_codex_creator_and_operator_attribution(steward_home, monkeypatch):
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p9")
    monkeypatch.setenv("CODEX_THREAD_ID", "codex-thread-1")
    creator = session.creator_from_env()
    assert creator["session"] == "codex-thread-1"
    assert creator["herdr_pane"] == "w1:p9"

    notice_store = Store(steward_home / "notice")
    notice_job = notice_store.create_job("notify the creator", "/repo")
    notice_store.kv_set(notice_job.job_id, "created_by", creator)
    assert push.notify_creator(notice_store, notice_job.job_id, "stopped",
                               _herdr([_pane(session="codex-thread-1", pane="w1:p9")])) == "delivered"

    store = Store(steward_home)
    job = store.create_job("attribute operator actions", "/repo")
    pane = ControlPane(store, {"record": lambda _cmd: store.append_event(
        job.job_id, what="action", evidence={}, rule="test", judgment=Judgment.human)},
        print_fn=lambda _text: None)
    pane.execute({"request_id": "actor-1", "op": "record", "args": {}, "origin": "cli",
                  "actor": "operator_agent:codex"})
    assert store.events(job.job_id)[-1].evidence["actor"] == "operator_agent:codex"
    approval = approvals.open_approval(store, job.job_id, "review a non-owner action", kind="general")
    from steward.api import StewardAPI

    StewardAPI(store).approve(job.job_id, approval["id"], origin="chatgpt", request_id="chatgpt-approve")
    assert approvals.get_approval(store, job.job_id, approval["id"])["decided_by"] == "operator_agent:chatgpt"


def test_continuous_ownership_recovery_acceptance_routine_unblock_is_not_scope_waiver(steward_home, monkeypatch):
    monkeypatch.setattr(closeout, "acceptance_mode", lambda: "auto")
    monkeypatch.setattr("steward.cli.herdr_tabs.mark_accepted", lambda *_a, **_kw: None)
    monkeypatch.setattr("steward.cli.push_module.notify_creator", lambda *_a, **_kw: "delivered")
    store = Store(steward_home)
    job = store.create_job("implement an unverified live deliverable", "/repo")
    store_contract(store, job.job_id, Contract(goal=job.goal,
                                               done_when=["Implement all accepted recovery deliverables; tests alone are not completion"]))
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.NEEDS_HUMAN.value, job.job_id))
    store.append_event(job.job_id, what="checks passed at exact commit", evidence={"commit_sha": "sha-1"},
                       rule="checks_pass_at_sha", judgment=Judgment.code)
    from steward import cli

    operator_pane = ControlPane(store, cli._build_control_handlers(store), print_fn=lambda _text: None)
    refused = operator_pane.execute({"request_id": "operator-accept", "op": "accept",
                                     "args": {"job_id": job.job_id}, "origin": "cli",
                                     "actor": "operator_agent:codex"})
    assert refused["accepted"] is False and store.get_job(job.job_id).state == JobState.NEEDS_HUMAN
    approval = approvals.open_approval(store, job.job_id, approvals.ACCEPTANCE_QUESTION, kind="general")
    refused_approval = operator_pane.execute({"request_id": "operator-approve", "op": "approve",
                                              "args": {"job_id": job.job_id, "approval_id": approval["id"]},
                                              "origin": "cli", "actor": "operator_agent:codex"})
    assert refused_approval["accepted"] is False and store.get_job(job.job_id).state == JobState.NEEDS_HUMAN
    from steward.api import StewardAPI
    direct_api_refusal = StewardAPI(store).approve(job.job_id, approval["id"], origin="chatgpt",
                                                   request_id="direct-api-approve")
    assert direct_api_refusal["accepted"] is False
    direct_decision_refusal = approvals.decide(store, job.job_id, approval["id"], "approve",
                                               request_id="direct-decision-approve",
                                               decided_by="operator_agent:codex")
    assert direct_decision_refusal.accepted is False
    assert approvals.get_approval(store, job.job_id, approval["id"])["status"] == "pending"

    owner_pane = ControlPane(store, cli._build_control_handlers(store), print_fn=lambda _text: None)
    accepted = owner_pane.execute({"request_id": "owner-accept", "op": "accept",
                                   "args": {"job_id": job.job_id}, "origin": "cli"})
    assert accepted["accepted"] is True and store.get_job(job.job_id).state == JobState.ACCEPTED
    override = [e for e in store.events(job.job_id) if e.what == "operator accepted"][-1]
    assert override.evidence["scope_override"]["actor"] == "owner:explicit_accept"
    assert "outcome unproven (manual)" in " ".join(override.evidence["scope_override"]["outcomes"])
    assert approvals.get_approval(store, job.job_id, approval["id"])["status"] == "pending"
