"""The 8 trust scenarios from `.planning/PROJECT.md` — "Success = the trust suite".

Each test drives the real `Runner` (never a second scheduler) with
`FakeWorker`/`FakeDiagnoser`/`FakeTransport` and, where the scenario turns on
real committed state (worktree changes, a diff-based hard constraint,
test-weakening), a real git repo via `SubprocessRunner`. No test invokes real
`claude`, `herdr`, or the network.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from fixtures.repos import python_repo
from ruling_support import StubLead

from steward.baseline import run_baseline
from steward.checkpoint import load as load_handoff
from steward.contract import Contract, propose_contract, store_contract
from steward.control import Controller
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.discovery import discover
from steward.jev_observer import FakeTransport, JevObserver
from steward.lease import LeaseHeld, acquire
from steward.models import ControlRequest, JobState, Judgment
from steward.runner import Runner
from steward.runner_exec import FakeRunner, CommandResult, SubprocessRunner
from steward.steering import Steering
from steward.store import Store
from steward.worker import FakeWorker, WorkerObservation
from steward.worktree import create_worktree


# -- shared test doubles (mirrors tests/test_runner.py's patterns) ----------


class _Clock:
    def __init__(self, start: float = 1000.0, step: float = 1.0):
        self._next = start
        self._step = step

    def __call__(self) -> float:
        value = self._next
        self._next += self._step
        return value


class _Factory:
    """Test worker_factory: pops a scripted FakeWorker per fresh dispatch."""

    def __init__(self, scripts=None):
        self._scripts = list(scripts) if scripts else []
        self.created: list[FakeWorker] = []

    def __call__(self, existing_ref):
        script = self._scripts.pop(0) if self._scripts else []
        worker = FakeWorker(script=script)
        self.created.append(worker)
        return worker


def _fake_diagnoser(verdict="retry_with_guidance", guidance="try again", hypothesis="hang"):
    return FakeDiagnoser(Diagnosis(verdict=verdict, guidance=guidance, hypothesis=hypothesis))


def _git_runner(*, head="sha-1", diff="", untracked=()):
    def respond(argv, cwd):
        if argv[:2] == ["git", "merge-base"]:  # the job's start commit (differs from `head`: a new commit exists)
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout="base-0\n", stderr="", duration_s=0.0)
        if argv[:2] == ["git", "rev-parse"]:
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=head + "\n", stderr="", duration_s=0.0)
        if argv[:2] == ["git", "diff"]:
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=diff, stderr="", duration_s=0.0)
        if argv[:2] == ["git", "status"]:
            porcelain = "".join(f"?? {u}\n" for u in untracked)
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=porcelain, stderr="", duration_s=0.0)
        return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout="", stderr="", duration_s=0.0)

    return FakeRunner(respond)


def _trivial_contract(store, job_id, goal="goal"):
    contract = Contract(
        goal=goal, boundaries=[], done_when=["done"], next_step="", authority=[], assumptions=[]
    )
    store_contract(store, job_id, contract)
    return contract


def _committed_repo_job(store, tmp_path, *, fix_failing_test=False):
    """A real git repo + worktree + baseline + contract, ready to `runner.start`."""
    repo = python_repo(tmp_path)
    if fix_failing_test:
        (repo / "tests" / "test_sample.py").write_text("def test_ok():\n    assert True\n")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture setup"], cwd=repo, check=True, capture_output=True)

    job = store.create_job("goal", str(repo))
    real_runner = SubprocessRunner()
    discovery = discover(repo)
    worktree = create_worktree(repo, job.job_id, real_runner, base_dir=store.home / "worktrees")
    baseline = run_baseline(store, job.job_id, repo, discovery, real_runner)
    contract = propose_contract("goal", discovery, baseline)
    store_contract(store, job.job_id, contract)
    return job, repo, worktree, real_runner


def _commit_then_done(worker):
    """A worker that committed new work and then ends its turn: a completion CANDIDATE (clean tree, new SHA)."""
    (worker.worktree / "feature.txt").write_text("done\n")
    for argv in (["git", "add", "-A"], ["git", "commit", "-q", "-m", "work"]):
        subprocess.run(argv, cwd=worker.worktree, check=True, capture_output=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                            "GIT_COMMITTER_EMAIL": "t@t"})
    return WorkerObservation(lifecycle="idle", output_tail="STEWARD-DONE", pid_alive=True, ts="t2")


# -- 1: kill worker mid-task --------------------------------------------------


def test_1_kill_worker_mid_task_preserves_changes_and_never_double_writes(steward_home, tmp_path):
    store = Store(steward_home)
    job, repo, worktree, real_runner = _committed_repo_job(store, tmp_path)

    def write_progress_then_die(worker):
        (worker.worktree / "progress.txt").write_text("half done\n")
        return WorkerObservation(
            lifecycle="exited", output_tail="Traceback...\nError: killed", pid_alive=False, ts="t"
        )

    factory = _Factory(scripts=[[write_progress_then_die], []])
    runner = Runner(store, factory, real_runner, _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)

    report = runner.tick(job.job_id)

    assert report.action == "replace"
    # worktree changes intact — the killed worker's edit survives.
    assert (worktree / "progress.txt").read_text() == "half done\n"
    # handoff injected for the replacement.
    handoff = load_handoff(store, job.job_id)
    assert handoff is not None
    assert handoff.attempt_no == 2
    assert "worker process exited" in handoff.hypothesis
    # replacement only happened after the first attempt confirmed stopped.
    assert factory.created[0].stopped is True
    attempts = store.kv_get(job.job_id, "attempts")
    assert attempts[0]["confirmed_stopped"] is True
    assert attempts[1]["status"] == "running"
    # never two leases: a third attempt cannot acquire while attempt 2 runs.
    with pytest.raises(LeaseHeld):
        acquire(store, job.job_id, "someone-else")


# -- 2: restart controller resumes constraints/budget/pending steering ------


def test_2_restart_controller_resumes_constraints_budget_and_pending_steering(steward_home):
    store_a = Store(steward_home)
    job = store_a.create_job("goal", "/repo")
    _trivial_contract(store_a, job.job_id)

    dead = WorkerObservation(lifecycle="exited", output_tail="boom", pid_alive=False, ts="t")
    factory_a = _Factory(scripts=[[dead], []])
    runner_a = Runner(store_a, factory_a, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner_a.start(job.job_id)
    runner_a.tick(job.job_id)  # dead -> replace: spends one recovery
    assert store_a.kv_get(job.job_id, "recoveries_used") == 1

    # A fresh steering instruction arrives right before the controller dies —
    # it is recorded but this crashed process never gets a tick to deliver it.
    Steering(store_a).record(job.job_id, "avoid", "steward/store.py", "req-1")
    # Simulate a controller crash right here: no further ticks on runner_a.

    store_b = Store(steward_home)
    worker_ref = store_b.kv_get(job.job_id, "attempts")[-1]["worker_ref"]
    reattached = FakeWorker(script=[WorkerObservation(lifecycle="working", output_tail="", pid_alive=True, ts="t")])

    def factory_b(existing_ref):
        assert existing_ref == worker_ref
        return reattached

    runner_b = Runner(store_b, factory_b, _git_runner(), _fake_diagnoser(), clock=_Clock())

    resumed = runner_b.resume_all()  # no ids supplied: restart-recovery default

    assert resumed == [job.job_id]
    assert store_b.kv_get(job.job_id, "recoveries_used") == 1  # budget survived the restart

    active = Steering(store_b).active_constraints(job.job_id)
    assert any(c["text"] == "steward/store.py" for c in active)  # constraint survived

    runner_b.tick(job.job_id)
    assert reattached.delivered == ["[rev=1] steward/store.py"]  # pending steering delivered


# -- 3: redirect during work -> receipt shows delivered/acknowledged -------


def test_3_redirect_receipt_shows_delivered_and_acknowledged_times(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    _trivial_contract(store, job.job_id)

    factory = _Factory(scripts=[[]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    factory.created[0].ack_revs = True  # worker will STEWARD-ACK the next instruction it gets

    steering = Steering(store)
    receipt = steering.record(job.job_id, "focus", "work on retry logic first", "req-1")
    assert "Delivered: pending" in receipt.detail
    assert "Acknowledged: pending" in receipt.detail

    runner.tick(job.job_id)  # delivers rev=1, then observes the worker's STEWARD-ACK

    record = steering.all_instructions(job.job_id)[0]
    assert record["delivered_at"] is not None
    assert record["acknowledged_at"] is not None
    final_receipt = steering.receipt_text(job.job_id, 1)
    assert "pending" not in final_receipt


# -- 4: tighten a boundary blocks closeout on violation ----------------------


def test_4_tightened_boundary_blocks_closeout_on_violation(steward_home, tmp_path):
    repo = python_repo(tmp_path)
    (repo / "migrations").mkdir()
    (repo / "migrations" / ".gitkeep").write_text("")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "add migrations dir"], cwd=repo, check=True, capture_output=True)

    store = Store(steward_home)
    job = store.create_job("goal", str(repo))
    real_runner = SubprocessRunner()
    discovery = discover(repo)
    worktree = create_worktree(repo, job.job_id, real_runner, base_dir=store.home / "worktrees")
    baseline = run_baseline(store, job.job_id, repo, discovery, real_runner)
    contract = propose_contract("goal", discovery, baseline)
    store_contract(store, job.job_id, contract)

    Steering(store).record(job.job_id, "avoid", "migrations/**", "req-1")

    def write_migration_and_finish(worker):
        (worker.worktree / "migrations" / "x.py").write_text("bad migration\n")
        subprocess.run(["git", "add", "-A"], cwd=worker.worktree, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "add migration"], cwd=worker.worktree, check=True, capture_output=True)
        return WorkerObservation(lifecycle="idle", output_tail="STEWARD-DONE", pid_alive=True, ts="t")

    factory = _Factory(scripts=[[write_migration_and_finish]])
    runner = Runner(store, factory, real_runner, _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)

    runner.tick(job.job_id)

    violation_events = [e for e in store.events(job.job_id) if e.what == "hard constraint violation detected"]
    assert len(violation_events) == 1
    assert store.kv_get(job.job_id, "closeout_blocked") is True
    assert store.get_job(job.job_id).state == JobState.NEEDS_HUMAN
    assert store.get_job(job.job_id).state != JobState.ACCEPTED


# -- 5: permission prompt / quiet long test -> classified waiting, no loop --


def test_5_permission_prompt_and_quiet_pytest_never_trigger_diagnosis_or_recovery(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    _trivial_contract(store, job.job_id)

    prompt_obs = WorkerObservation(
        lifecycle="blocked", output_tail="Allow this action? [y/n]", pid_alive=True, ts="t"
    )
    quiet_pytest_obs = WorkerObservation(
        lifecycle="working", output_tail="test session starts\ncollecting 42 item", pid_alive=True, ts="t"
    )
    script = [prompt_obs] * 10 + [quiet_pytest_obs] * 10
    factory = _Factory(scripts=[script])
    diagnoser = FakeDiagnoser(Diagnosis(verdict="needs_human", guidance="", hypothesis="must never fire"))
    runner = Runner(store, factory, _git_runner(), diagnoser, clock=_Clock())
    runner.start(job.job_id)

    for _ in range(20):
        report = runner.tick(job.job_id)
        assert report.cls in ("waiting", "quiet_known_process")

    assert diagnoser.contexts == []  # zero diagnoses
    assert store.kv_get(job.job_id, "recoveries_used", default=0) == 0  # zero recoveries


# -- 6: duplicate stop request is deduped -----------------------------------


def test_6_duplicate_stop_request_is_deduped(steward_home):
    store = Store(steward_home)
    controller = Controller(store)
    job = controller.new_job("goal", "/repo")
    store.set_state(job.job_id, JobState.CONTRACTED, what="c", evidence={}, rule="r", judgment=Judgment.code)
    store.set_state(job.job_id, JobState.RUNNING, what="r", evidence={}, rule="r", judgment=Judgment.code)

    first = controller.submit(ControlRequest(request_id="stop-1", job_id=job.job_id, kind="stop"))
    second = controller.submit(ControlRequest(request_id="stop-1", job_id=job.job_id, kind="stop"))

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.detail == first.detail
    stop_events = [e for e in store.events(job.job_id) if e.rule == "operator_stop"]
    assert len(stop_events) == 1


# -- 7: completion after weakening a test -> review required ----------------


def test_7_worker_weakens_a_failing_test_and_reports_done_forces_needs_human(steward_home, tmp_path):
    store = Store(steward_home)
    job, repo, worktree, real_runner = _committed_repo_job(store, tmp_path)  # test_fails is pre-existing

    def remove_failing_test_and_finish(worker):
        (worker.worktree / "tests" / "test_sample.py").write_text("def test_ok():\n    assert True\n")
        subprocess.run(["git", "add", "-A"], cwd=worker.worktree, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "remove failing test"], cwd=worker.worktree, check=True, capture_output=True
        )
        return WorkerObservation(lifecycle="idle", output_tail="STEWARD-DONE", pid_alive=True, ts="t")

    factory = _Factory(scripts=[[remove_failing_test_and_finish]])
    runner = Runner(store, factory, real_runner, _fake_diagnoser(), clock=_Clock())
    lead = runner.job_lead = StubLead(store)  # even a lead that would rule `finished` cannot accept a weakened job
    runner.start(job.job_id)

    runner.tick(job.job_id)
    runner.tick(job.job_id)

    assert store.get_job(job.job_id).state == JobState.NEEDS_HUMAN
    assert lead.calls == 0  # weakening is a code block: no completion ruling is even requested
    events = [e for e in store.events(job.job_id) if e.what == "checks passed; acceptance unresolved"]
    assert len(events) == 1
    assert "test-weakening" in str(events[0].evidence["reasons"])


# -- 8: Jev unavailable -> baseline continues, no invented decision ---------


def test_8_jev_unavailable_does_not_block_baseline_acceptance(steward_home, monkeypatch, tmp_path):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job, repo, worktree, real_runner = _committed_repo_job(store, tmp_path, fix_failing_test=True)

    transport = FakeTransport(ConnectionError("network down"))
    jev = JevObserver(transport=transport, enabled=True)

    factory = _Factory(
        scripts=[
            [
                WorkerObservation(lifecycle="working", output_tail="working", pid_alive=True, ts="t1"),
                _commit_then_done,
            ]
        ]
    )
    (worktree / "change.txt").write_text("one line\n")  # an empty job would be closed RELEASED (issue L)
    subprocess.run(["git", "add", "-A"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "job change"], cwd=worktree, check=True, capture_output=True)
    runner = Runner(store, factory, real_runner, _fake_diagnoser(), clock=_Clock(), jev_observer=jev)
    runner.job_lead = StubLead(store)  # v2.7: the one completion ruling (the baseline path has no Jev in it)
    runner.start(job.job_id)

    runner.tick(job.job_id)
    runner.tick(job.job_id)  # done -> real closeout -> CHECKS_PASSED, the ruling is requested and answered
    report = runner.tick(job.job_id)

    assert report.state == "ACCEPTED"  # the baseline job still reaches acceptance
    unavailable_events = [e for e in store.events(job.job_id) if e.what == "jev_unavailable"]
    assert len(unavailable_events) >= 1  # Jev's own failure was recorded, not invented as a decision
    assert all(e.judgment == Judgment.code for e in unavailable_events)
