import json
import os
import subprocess

import pytest
from fixtures.repos import python_repo
from ruling_support import StubLead, finish as finish_ticks, patch_green_verification

from steward import cli
from steward.baseline import run_baseline
from steward.checkpoint import load as load_handoff
from steward.contract import Contract, propose_contract, store_contract
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.discovery import discover
from steward.jev_observer import FakeTransport, JevObserver, TransportResponse
from steward.herdr_worker import HerdrWorker
from steward.lease import LeaseHeld, acquire
from steward.models import JobState, Judgment
from steward.recovery import RecoveryPolicy
from steward.runner import Runner
from steward.runner_exec import CommandResult, FakeRunner, SubprocessRunner
from steward.steering import Steering
from steward.store import Store
from steward.worker import FakeWorker, WorkerObservation
from steward.worktree import create_worktree


def _contracted_job(store, goal="do the thing", repo="/repo"):
    job = store.create_job(goal, repo)
    contract = Contract(
        goal=goal, boundaries=[], done_when=["done"], next_step="", authority=[], assumptions=[]
    )
    store_contract(store, job.job_id, contract)
    return job


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


class _Clock:
    """Deterministic, injectable clock — never a real sleep (SP-21 test hygiene)."""

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


# -- start() / dispatch -----------------------------------------------------


def test_start_transitions_to_running_and_acquires_lease(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(scripts=[[]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())

    runner.start(job.job_id)

    updated = store.get_job(job.job_id)
    assert updated.state == JobState.RUNNING
    attempts = store.kv_get(job.job_id, "attempts")
    assert len(attempts) == 1
    assert attempts[0]["status"] == "running"
    assert factory.created[0].started_brief is not None


def test_start_blocked_worker_escalates_without_lease_or_claimed_delivery(steward_home):
    from steward.herdr_worker import WorkerStartBlocked

    class _Blocked:
        pane_id = "w9:p3"

        def start(self, brief, worktree):
            raise WorkerStartBlocked("worker not ready after 300s (last status: blocked)")

    store = Store(steward_home)
    job = _contracted_job(store)
    runner = Runner(store, lambda ref: _Blocked(), _git_runner(), _fake_diagnoser(), clock=_Clock())

    runner.start(job.job_id)

    assert store.get_job(job.job_id).state == JobState.NEEDS_HUMAN
    assert not store.kv_get(job.job_id, "attempts")
    last = store.events(job.job_id)[-1]
    assert last.rule == "worker_start_blocked" and last.evidence["pane_id"] == "w9:p3"


# -- pause/stop honoured before any dispatch (SP-11) ------------------------


def test_pause_blocks_dispatch_before_any_worker_interaction(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(scripts=[[]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    store.set_state(
        job.job_id, JobState.PAUSED, what="operator pause", evidence={}, rule="operator_pause",
        judgment=Judgment.human,
    )

    report = runner.tick(job.job_id)

    assert report.state == "PAUSED"
    assert report.detail == "paused; no dispatch"
    assert factory.created[0].delivered == []


def test_stopping_confirmed_releases_lease_and_reaches_stopped(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(scripts=[[]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    store.set_state(
        job.job_id, JobState.STOPPING, what="operator stop", evidence={}, rule="operator_stop",
        judgment=Judgment.human,
    )

    report = runner.tick(job.job_id)

    assert report.state == "STOPPED"
    attempts = store.kv_get(job.job_id, "attempts")
    assert attempts[0]["confirmed_stopped"] is True
    assert factory.created[0].stopped is True


def test_stopping_unconfirmed_keeps_lease_held_and_reports_uncertainty(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)

    class _NeverConfirms(FakeWorker):
        def stop(self):
            self.stopped = True
            return False

    worker = _NeverConfirms()
    runner = Runner(store, lambda ref: worker, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    store.set_state(
        job.job_id, JobState.STOPPING, what="operator stop", evidence={}, rule="operator_stop",
        judgment=Judgment.human,
    )

    report = runner.tick(job.job_id)

    assert report.detail == "stop_unconfirmed"
    assert report.state == "STOPPING"
    attempts = store.kv_get(job.job_id, "attempts")
    assert attempts[0]["confirmed_stopped"] is False
    assert any(e.what == "stop_unconfirmed" for e in store.events(job.job_id))


# -- steering delivery + STEWARD-DONE ---------------------------------------


def test_tick_delivers_pending_instruction_and_marks_delivered(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(
        scripts=[[WorkerObservation(lifecycle="working", output_tail="ok", pid_alive=True, ts="t")]]
    )
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    Steering(store).record(job.job_id, "focus", "work on X", "req-1")

    runner.tick(job.job_id)

    assert factory.created[0].delivered == ["[rev=1] work on X"]
    instructions = Steering(store).all_instructions(job.job_id)
    assert instructions[0]["status"] == "delivered"


def test_tick_reaches_worker_finished_on_steward_done_marker(steward_home, monkeypatch):
    patch_green_verification(monkeypatch)  # checks ran; with none discovered v2.7 parks the job ("no checks ran")
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(
        scripts=[[WorkerObservation(lifecycle="idle", output_tail="all good\nSTEWARD-DONE", pid_alive=True, ts="t")]]
    )
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.job_lead = StubLead(store)
    runner.start(job.job_id)

    report = finish_ticks(runner, job.job_id)

    # STEWARD-DONE drives WORKER_FINISHED, and closeout (verify + weakening +
    # acceptance decision) runs in the same tick (SP-16, SP-17); a green verification waits for its one completion
    # ruling (the stub lead answers `finished`) and the next tick decides: ACCEPTED.
    marker_events = [e for e in store.events(job.job_id) if e.rule == "steward_done_marker"]
    assert len(marker_events) == 1
    assert report.action == "closeout"
    assert report.state == "ACCEPTED"


def test_tick_runs_real_closeout_and_reaches_accepted_on_clean_finish(steward_home, tmp_path):
    repo = python_repo(tmp_path)
    # fix up the fixture so baseline is clean (no pre-existing failure) and
    # running pytest for real doesn't leave the tree looking dirty.
    (repo / "tests" / "test_sample.py").write_text("def test_ok():\n    assert True\n")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "clean"], cwd=repo, check=True, capture_output=True)

    store = Store(steward_home)
    job = store.create_job("goal", str(repo))
    real_runner = SubprocessRunner()
    discovery = discover(repo)
    create_worktree(repo, job.job_id, real_runner, base_dir=store.home / "worktrees")
    baseline = run_baseline(store, job.job_id, repo, discovery, real_runner)
    contract = propose_contract("goal", discovery, baseline)
    store_contract(store, job.job_id, contract)

    def commit_then_done(worker):  # a candidate: a new commit, clean tree, then the turn ends
        (worker.worktree / "feature.txt").write_text("done\n")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t"}
        for argv in (["git", "add", "-A"], ["git", "commit", "-q", "-m", "work"]):
            subprocess.run(argv, cwd=worker.worktree, check=True, capture_output=True, env=env)
        return WorkerObservation(lifecycle="idle", output_tail="STEWARD-DONE", pid_alive=True, ts="t")

    factory = _Factory(scripts=[[commit_then_done]])
    runner = Runner(store, factory, real_runner, _fake_diagnoser(), clock=_Clock())
    runner.job_lead = StubLead(store)
    runner.start(job.job_id)

    first = runner.tick(job.job_id)
    assert first.action == "closeout" and first.state == "CHECKS_PASSED"  # real pytest ran, green: awaiting the ruling
    report = finish_ticks(runner, job.job_id)

    assert report.action == "closeout"
    assert report.state == "ACCEPTED"


# -- recovery: dead worker -> replace, with handoff, single writer ---------


def test_dead_worker_triggers_replacement_with_handoff_and_no_double_writer(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    dead_obs = WorkerObservation(lifecycle="exited", output_tail="Traceback...\nError: boom", pid_alive=False, ts="t")
    factory = _Factory(scripts=[[dead_obs], []])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)

    report = runner.tick(job.job_id)

    assert report.action == "replace"
    assert len(factory.created) == 2
    assert factory.created[0].stopped is True

    attempts = store.kv_get(job.job_id, "attempts")
    assert len(attempts) == 2
    assert attempts[0]["confirmed_stopped"] is True
    assert attempts[1]["status"] == "running"

    handoff = load_handoff(store, job.job_id)
    assert handoff is not None
    assert handoff.attempt_no == 2
    assert "worker process exited" in handoff.hypothesis

    # single writer: a third attempt cannot acquire while attempt 2 is running.
    with pytest.raises(LeaseHeld):
        acquire(store, job.job_id, "someone-else")


def test_replace_deferred_when_stop_not_confirmed_does_not_start_second_worker(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    dead_obs = WorkerObservation(lifecycle="exited", output_tail="boom", pid_alive=False, ts="t")

    class _Stubborn(FakeWorker):
        def stop(self):
            self.stopped = True
            return False

    worker = _Stubborn(script=[dead_obs])
    runner = Runner(store, lambda ref: worker, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)

    report = runner.tick(job.job_id)

    assert report.detail == "replacement deferred: previous attempt not confirmed stopped"
    attempts = store.kv_get(job.job_id, "attempts")
    assert len(attempts) == 1
    assert attempts[0]["confirmed_stopped"] is False


def test_needs_human_after_recovery_budget_exhausted(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    dead = WorkerObservation(lifecycle="exited", output_tail="gone", pid_alive=False, ts="t")
    factory = _Factory(scripts=[[dead], [dead], [dead]])
    runner = Runner(
        store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock(), policy=RecoveryPolicy(max_recoveries=2)
    )
    runner.start(job.job_id)

    runner.tick(job.job_id)
    runner.tick(job.job_id)
    report = runner.tick(job.job_id)

    assert report.state == "NEEDS_HUMAN"
    assert report.action == "needs_human"


# -- controller restart: resume + redeliver pending once (SP-2, SP-9, SP-14) --


def test_resume_all_reattaches_and_redelivers_pending_instruction_once(steward_home):
    store_a = Store(steward_home)
    job = _contracted_job(store_a)
    factory_a = _Factory(scripts=[[]])
    runner_a = Runner(store_a, factory_a, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner_a.start(job.job_id)
    Steering(store_a).record(job.job_id, "avoid", "steward/store.py", "req-1")
    # Simulate a controller crash: no tick() ever ran on runner_a.

    store_b = Store(steward_home)
    worker_ref = store_b.kv_get(job.job_id, "attempts")[0]["worker_ref"]
    reattached = FakeWorker(script=[WorkerObservation(lifecycle="working", output_tail="", pid_alive=True, ts="t")])

    def factory_b(existing_ref):
        assert existing_ref == worker_ref
        return reattached

    runner_b = Runner(store_b, factory_b, _git_runner(), _fake_diagnoser(), clock=_Clock())

    resumed = runner_b.resume_all(["unknown-job", job.job_id])
    assert resumed == [job.job_id]

    runner_b.tick(job.job_id)
    runner_b.tick(job.job_id)  # must not redeliver a second time

    assert reattached.delivered == ["[rev=1] steward/store.py"]
    instructions = Steering(store_b).all_instructions(job.job_id)
    assert instructions[0]["status"] == "delivered"
    assert len(store_b.kv_get(job.job_id, "attempts")) == 1  # no second writer started


def test_resume_all_restores_herdr_worker_cwd(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    Runner(store, _Factory(scripts=[[]]), _git_runner(), _fake_diagnoser(), clock=_Clock()).start(job.job_id)
    worker_ref = store.kv_get(job.job_id, "attempts")[0]["worker_ref"]
    command_runner = FakeRunner(lambda argv, cwd: CommandResult(
        argv=argv, cwd=cwd, exit_code=0,
        stdout=json.dumps({"result": {"agent": {"agent_status": "idle"}}}),
        stderr="", duration_s=0.0,
    ))
    worker = HerdrWorker(command_runner, caller_pane="pane-1", kind="codex", name=worker_ref)
    resumed = Runner(store, lambda ref: worker, command_runner, _fake_diagnoser(), clock=_Clock())

    assert resumed.resume_all([job.job_id]) == [job.job_id]
    assert worker.worktree == steward_home / "worktrees" / job.job_id
    assert worker._agent_state() == "idle"
    assert command_runner.calls[-1]["cwd"] == str(worker.worktree)


def test_resume_all_skips_jobs_not_running(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)  # CONTRACTED, never started

    runner = Runner(store, _Factory(), _git_runner(), _fake_diagnoser(), clock=_Clock())
    resumed = runner.resume_all([job.job_id])

    assert resumed == []


def test_resume_all_defaults_to_every_running_paused_or_stopping_job(steward_home):
    store_a = Store(steward_home)
    running_job = _contracted_job(store_a, goal="a")
    factory_a = _Factory(scripts=[[], []])
    runner_a = Runner(store_a, factory_a, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner_a.start(running_job.job_id)

    paused_job = _contracted_job(store_a, goal="b")
    runner_a.start(paused_job.job_id)
    store_a.set_state(
        paused_job.job_id, JobState.PAUSED, what="pause", evidence={}, rule="r", judgment=Judgment.human,
    )

    never_started_job = _contracted_job(store_a, goal="c")  # CONTRACTED: not eligible
    # Simulate a controller crash: no further ticks ever ran on runner_a.

    store_b = Store(steward_home)
    reattached = {}

    def factory_b(existing_ref):
        worker = FakeWorker()
        reattached[existing_ref] = worker
        return worker

    runner_b = Runner(store_b, factory_b, _git_runner(), _fake_diagnoser(), clock=_Clock())

    resumed = runner_b.resume_all()  # no job_ids: discovers every in-flight job

    assert set(resumed) == {running_job.job_id, paused_job.job_id}
    assert never_started_job.job_id not in resumed


# -- run_forever -------------------------------------------------------------


def test_run_forever_stops_once_worker_finished(steward_home, monkeypatch):
    patch_green_verification(monkeypatch)
    store = Store(steward_home)
    job = _contracted_job(store)
    script = [
        WorkerObservation(lifecycle="working", output_tail="working...", pid_alive=True, ts="t1"),
        WorkerObservation(lifecycle="working", output_tail="still going", pid_alive=True, ts="t2"),
        WorkerObservation(lifecycle="idle", output_tail="STEWARD-DONE", pid_alive=True, ts="t3"),
    ]
    factory = _Factory(scripts=[script])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.job_lead = StubLead(store)
    runner.start(job.job_id)

    sleeps = []
    report = runner.run_forever(job.job_id, interval_s=5, sleep_fn=sleeps.append, max_ticks=10)

    # Closeout runs in the same tick STEWARD-DONE is observed; the green verification then waits (the runner keeps
    # ticking) for its one completion ruling and the next tick reaches ACCEPTED — a dispatch-terminal state
    # run_forever stops on.
    assert report.state == "ACCEPTED"
    assert sleeps == [5, 5, 5]


# -- jev observer: its own 10s timer, not once per tick (SP-6) ---------------


def test_tick_calls_jev_at_most_once_per_ten_seconds(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = _contracted_job(store)
    prompt_obs = WorkerObservation(
        lifecycle="blocked", output_tail="Allow this action? [y/n]", pid_alive=True, ts="t"
    )
    factory = _Factory(scripts=[[prompt_obs, prompt_obs]])
    payload = {
        "model": "jev-latest",
        "answers": {
            "triage": {
                "choice": "ok",
                "confidence": 0.9,
                "probabilities": {"ok": 0.9, "stalled": 0.1},
            }
        },
    }
    transport = FakeTransport(TransportResponse(status_code=200, body=json.dumps(payload).encode()))
    jev = JevObserver(transport=transport, enabled=True)
    now = [1000.0]
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=lambda: now[0], jev_observer=jev)
    runner.start(job.job_id)

    runner.tick(job.job_id)  # jev's timer is due -> one call
    runner.tick(job.job_id)  # same instant: not due again
    assert len(transport.calls) == 1
    events = [e for e in store.events(job.job_id) if e.what == "jev_shadow"]
    assert len(events) == 1


# -- CLI ---------------------------------------------------------------------


def test_cli_explain_state_prints_plain_english_summary(steward_home, capsys):
    store = Store(steward_home)
    job = _contracted_job(store, goal="ship reconnect fix")
    Steering(store).record(job.job_id, "avoid", "steward/store.py", "req-1")
    capsys.readouterr()

    rc = cli.main(["explain-state", job.job_id])

    assert rc == 0
    out = capsys.readouterr().out
    assert "ship reconnect fix" in out
    assert "CONTRACTED" in out
    assert "[hard] steward/store.py" in out
    assert "Recoveries used: 0/2" in out


def test_cli_explain_state_unknown_job_errors(steward_home, capsys):
    rc = cli.main(["explain-state", "no-such-job"])
    assert rc == 1


def _new_job_id(capsys) -> str:
    out = capsys.readouterr().out
    return out.split()[0]


def test_cli_run_once_dispatches_worker_and_prints_report(steward_home, tmp_path, capsys):
    repo = python_repo(tmp_path)
    cli.main(["new", "demo goal", "--repo", str(repo), "--no-baseline"])
    job_id = _new_job_id(capsys)

    rc = cli.main(["run", job_id, "--worker", "fake", "--once"])

    assert rc == 0
    out = capsys.readouterr().out
    assert job_id in out
    assert "RUNNING" in out


def test_cli_run_syncs_exceptions_registry_at_start(steward_home, tmp_path, capsys, monkeypatch):
    """NB-05 (SP-36): `steward run` self-heals the exceptions registry
    (additive defaults only) at start, and reports what was added."""
    from steward import exceptions

    repo = python_repo(tmp_path)
    registry = steward_home / "exceptions.toml"
    registry.write_text(exceptions._DEFAULT_HEADER + "\n" + exceptions._DEFAULT_SECTIONS["git"])
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(registry))
    cli.main(["new", "demo goal", "--repo", str(repo), "--no-baseline"])
    job_id = _new_job_id(capsys)

    rc = cli.main(["run", job_id, "--worker", "fake", "--once"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "exceptions registry: added" in out
    remaining = set(exceptions._DEFAULT_SECTIONS) - {"git"}
    for name in remaining:
        assert name in out


def test_cli_run_herdr_without_pane_errors(steward_home, tmp_path, capsys):
    repo = python_repo(tmp_path)
    cli.main(["new", "demo goal", "--repo", str(repo), "--no-baseline"])
    job_id = _new_job_id(capsys)

    rc = cli.main(["run", job_id, "--worker", "herdr", "--once"])

    assert rc == 2


def test_cli_resume_without_job_reattaches_and_ticks_every_in_flight_job(
    steward_home, tmp_path, capsys
):
    repo = python_repo(tmp_path)
    cli.main(["new", "demo goal", "--repo", str(repo), "--no-baseline"])
    job_id = _new_job_id(capsys)
    cli.main(["run", job_id, "--worker", "fake", "--once"])  # dispatches; still RUNNING
    capsys.readouterr()

    rc = cli.main(["resume", "--worker", "fake", "--once"])

    assert rc == 0
    out = capsys.readouterr().out
    assert job_id in out


def test_cli_resume_without_job_and_no_in_flight_jobs_reports_nothing_to_resume(
    steward_home, capsys
):
    rc = cli.main(["resume", "--worker", "fake", "--once"])

    assert rc == 0
    assert "no in-flight jobs" in capsys.readouterr().out


def test_cli_resume_with_job_still_unpauses_via_control_request(steward_home, tmp_path, capsys):
    repo = python_repo(tmp_path)
    cli.main(["new", "demo goal", "--repo", str(repo), "--no-baseline"])
    job_id = _new_job_id(capsys)
    cli.main(["run", job_id, "--worker", "fake", "--once"])
    capsys.readouterr()
    cli.main(["pause", job_id])
    capsys.readouterr()

    rc = cli.main(["resume", job_id])

    assert rc == 0
    out = capsys.readouterr().out
    assert "accepted=True" in out


# -- audit rec 8: "commit your work" nudge before a dirty-worktree verify bounce --------------


def test_marker_with_uncommitted_files_is_stopped_dirty_not_a_closeout(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(
        scripts=[[WorkerObservation(lifecycle="idle", output_tail="STEWARD-DONE", pid_alive=True, ts="t")]]
    )
    runner = Runner(store, factory, _git_runner(untracked=("notes.py",)), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    runner._worktree(job.job_id).mkdir(parents=True, exist_ok=True)

    report = runner.tick(job.job_id)

    assert report.state == "RUNNING" and report.action != "closeout"
    (yc,) = [e for e in store.events(job.job_id) if e.what == "yield_classified"]
    assert yc.evidence["lane"] == "STOPPED" and yc.evidence["failed_preconditions"] == ["dirty_worktree"]
    assert not [e for e in store.events(job.job_id) if e.rule == "steward_done_marker"]
    # the marker stays in the pane's tail: the same state is classified once, not every tick
    runner.tick(job.job_id)
    assert len([e for e in store.events(job.job_id) if e.what == "yield_classified"]) == 1


def test_commit_nudge_asks_for_a_commit_then_end_turn_and_is_capped(steward_home):
    """`_commit_nudge` still guards a closeout reached outside the yield classifier (resume of WORKER_FINISHED)."""
    from steward import heartbeat
    store = Store(steward_home)
    job = _contracted_job(store)
    runner = Runner(store, _Factory(scripts=[[]]), _git_runner(untracked=("notes.py",)), _fake_diagnoser(),
                    clock=_Clock())
    runner.start(job.job_id)
    runner._worktree(job.job_id).mkdir(parents=True, exist_ok=True)
    store.set_state(job.job_id, JobState.WORKER_FINISHED, what="t", evidence={}, rule="t", judgment=Judgment.code)

    report = runner._commit_nudge(job.job_id, heartbeat.Class.idle)

    assert report.action == "commit_nudge" and report.state == "RUNNING"
    assert store.kv_get(job.job_id, "commit_nudges") == 1
    texts = [i["text"] for i in Steering(store).all_instructions(job.job_id)]
    assert any("Commit your work" in t and "notes.py" in t and "end your turn" in t and "STEWARD-DONE" not in t
               for t in texts)
    store.kv_set(job.job_id, "commit_nudges", Runner.MAX_COMMIT_NUDGES)
    assert runner._commit_nudge(job.job_id, heartbeat.Class.idle) is None


# -- runtime qualification drift: the runner's wiring to fleet_drift.DriftWatch ---------------------------------


def test_tick_runs_the_runtime_drift_watch_when_enabled_and_survives_its_errors(steward_home, monkeypatch):
    """`tests/conftest.py` sets STEWARD_RUNTIME_DRIFT=0 for every test so no tick reads real CLI versions. This test
    turns the switch back on with a fake watch and checks the wiring that fixture hides: built once on the first tick
    (store, clock, background), `check(job_id)` every tick, a failing check recorded and never breaking the tick, and
    "0" really disabling it. `DriftWatch` itself is tested in tests/test_fleet_drift.py."""
    from steward import fleet_drift

    built, checks = [], []

    class FakeWatch:
        def __init__(self, store, **kw):
            built.append((store, kw))
            self.fail = False

        def check(self, job_id):
            checks.append(job_id)
            if self.fail:
                raise RuntimeError("version read exploded")
            return []

    monkeypatch.setenv("STEWARD_RUNTIME_DRIFT", "1")
    monkeypatch.setattr(fleet_drift, "DriftWatch", FakeWatch)
    store = Store(steward_home)
    job = _contracted_job(store)
    working = WorkerObservation(lifecycle="working", output_tail="working...", pid_alive=True, ts="t")
    runner = Runner(store, _Factory(scripts=[[working] * 5]), _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    assert runner.drift_watch is None  # built on the first tick, not at construction

    runner.tick(job.job_id)
    assert isinstance(runner.drift_watch, FakeWatch) and checks == [job.job_id]
    (built_store, kw) = built[0]
    assert built_store is store and kw["background"] is True and kw["clock"] is runner.clock and callable(kw["send_text"])

    runner.tick(job.job_id)
    assert len(built) == 1 and checks == [job.job_id] * 2  # one watch per runner, checked every tick

    runner.drift_watch.fail = True
    report = runner.tick(job.job_id)
    assert report.state == "RUNNING"  # a drift-check failure never breaks the tick
    (err,) = [e for e in store.events(job.job_id) if e.what == "runtime_drift_error"]
    assert err.rule == "runtime_drift" and "version read exploded" in err.evidence["detail"]
    assert checks == [job.job_id] * 3

    monkeypatch.setenv("STEWARD_RUNTIME_DRIFT", "0")
    runner.drift_watch.fail = False
    runner.tick(job.job_id)
    assert checks == [job.job_id] * 3  # the conftest switch: no check at all


def test_commit_nudges_are_capped_then_the_normal_verify_path_decides(steward_home):
    """Past the cap, a WORKER_FINISHED closeout with a dirty tree is not nudged again: the verify path decides."""
    from steward import heartbeat
    store = Store(steward_home)
    job = _contracted_job(store)
    runner = Runner(store, _Factory(scripts=[[]]), _git_runner(untracked=("notes.py",)), _fake_diagnoser(),
                    clock=_Clock())
    runner.start(job.job_id)
    runner._worktree(job.job_id).mkdir(parents=True, exist_ok=True)
    store.set_state(job.job_id, JobState.WORKER_FINISHED, what="t", evidence={}, rule="t", judgment=Judgment.code)
    store.kv_set(job.job_id, "commit_nudges", Runner.MAX_COMMIT_NUDGES)

    report = runner._run_closeout(job.job_id, heartbeat.Class.idle)

    assert report.action == "closeout"
    assert store.kv_get(job.job_id, "commit_nudges") == Runner.MAX_COMMIT_NUDGES
