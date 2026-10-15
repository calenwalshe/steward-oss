"""Runner wiring for the trajectory outer loop (12-03: ledger hook, inner
verdict, supervisor step). `Runner(..., trajectory=None)` must stay
byte-for-byte the old behaviour — every pre-existing runner test suite
(`tests/test_runner.py`, `tests/test_runner_fabric.py`,
`tests/test_trust_suite.py`) is re-run unmodified alongside this file's own
`--trajectory`-flavoured coverage.
"""

from __future__ import annotations

from pathlib import Path

from steward import trajectory as trajectory_module
from steward.contract import Contract, store_contract
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.fabric import Frame
from steward.heartbeat import Signals
from steward.ledger import TrajectoryLedger
from steward.recovery import RecoveryPolicy
from steward.runner import Runner
from steward.runner_exec import CommandResult, FakeRunner
from steward.steering import Steering
from steward.store import Store
from steward.worker import FakeWorker, WorkerObservation


class _FabricFakeWorker(FakeWorker):
    kind = "fabric"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pane_id = "w1:pF"

    def close_pane(self) -> bool:
        return True


class _Clock:
    def __init__(self, start: float = 1000.0, step: float = 1.0):
        self._next = start
        self._step = step

    def __call__(self) -> float:
        value = self._next
        self._next += self._step
        return value


class _Factory:
    def __init__(self, scripts=None):
        self._scripts = list(scripts) if scripts else []
        self.created: list[_FabricFakeWorker] = []

    def __call__(self, existing_ref):
        script = self._scripts.pop(0) if self._scripts else []
        worker = _FabricFakeWorker(script=script)
        self.created.append(worker)
        return worker


class _DummySupervisor:
    """Never touches the worker; used to isolate the *runner's* wiring
    (call count, exception safety, ledger side effects) from
    `TrajectorySupervisor`'s own decision logic (covered by
    tests/test_trajectory.py)."""

    def __init__(self, on_step=None, raise_on_step: bool = False):
        self.calls: list[str] = []
        self.on_step = on_step
        self.raise_on_step = raise_on_step

    def step(self, job_id: str) -> None:
        self.calls.append(job_id)
        if self.raise_on_step:
            raise RuntimeError("supervisor exploded")
        if self.on_step:
            self.on_step(job_id)


def _contracted_job(store, goal="do the thing", repo="/repo"):
    job = store.create_job(goal, repo)
    contract = Contract(goal=goal, boundaries=[], done_when=["tests pass"], next_step="", authority=[], assumptions=[])
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


def _fake_diagnoser(verdict="retry_with_guidance", guidance="try again", hypothesis="hang"):
    return FakeDiagnoser(Diagnosis(verdict=verdict, guidance=guidance, hypothesis=hypothesis))


def _obs(lifecycle="working", events=(), pid_alive=True):
    return WorkerObservation(lifecycle=lifecycle, output_tail="", pid_alive=pid_alive, ts="t", events=events)


def _frame(seq, type_, body, job="jobA"):
    return Frame(v=1, seq=seq, job=job, type=type_, body=body)


def _sig(**overrides) -> Signals:
    base = dict(
        lifecycle="working", pid_alive=True, head_sha="sha-1", diff_fingerprint="fp-1",
        last_check_exit=None, error_fingerprint="", same_error_count=0, permission_prompt=False,
        running_process=None, since_last_change_s=0.0, output_tail="", turn_open=True, events=(),
    )
    base.update(overrides)
    return Signals(**base)


# ------------------------------------------------------- Runner(trajectory=None)


def test_trajectory_none_never_writes_a_ledger_key(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(scripts=[[_obs(), _obs()]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)

    runner.tick(job.job_id)
    runner.tick(job.job_id)

    assert store.kv_get(job.job_id, "trajectory_ledger") is None


# ------------------------------------------------------- start() seeds intent


def test_start_seeds_ledger_intent_from_the_contract(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store, goal="ship the thing")
    factory = _Factory(scripts=[[]])
    supervisor = _DummySupervisor()
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock(), trajectory=supervisor)

    runner.start(job.job_id)

    snapshot = TrajectoryLedger(store, job.job_id).snapshot()
    assert snapshot["intent"]["goal"] == "ship the thing"
    assert "tests pass" in snapshot["intent"]["done_when"]


# ------------------------------------------------------- ledger fact intake


def test_ledger_receives_plan_and_test_result_events(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(scripts=[[]])
    supervisor = _DummySupervisor()
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock(), trajectory=supervisor)
    runner.start(job.job_id)

    sig = _sig(
        events=(
            _frame(1, "event", {"kind": "plan", "items": ["step one", "step two"]}),
            _frame(2, "event", {"kind": "test_result", "passed": 3, "failed": 1, "errors": 0, "source": "worker_observed"}),
        )
    )
    runner._feed_trajectory_ledger(job.job_id, sig)

    snapshot = TrajectoryLedger(store, job.job_id).snapshot()
    assert snapshot["lists"]["plan"][-1]["items"] == ["step one", "step two"]
    test_point = snapshot["lists"]["test_points"][-1]
    assert (test_point["passed"], test_point["failed"], test_point["errors"]) == (3, 1, 0)


def test_ledger_receives_error_frame_as_failed_approach(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(scripts=[[]])
    supervisor = _DummySupervisor()
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock(), trajectory=supervisor)
    runner.start(job.job_id)

    sig = _sig(events=(_frame(1, "error", {"exit_code": 1}),))
    runner._feed_trajectory_ledger(job.job_id, sig)

    snapshot = TrajectoryLedger(store, job.job_id).snapshot()
    assert snapshot["lists"]["failed_approaches"][-1]["kind"] == "worker_error"


def test_ledger_receives_done_frame_as_milestone_and_cost_point(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(scripts=[[]])
    supervisor = _DummySupervisor()
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock(), trajectory=supervisor)
    runner.start(job.job_id)
    runner._attempts[job.job_id] = 1

    sig = _sig(events=(_frame(1, "done", {"sha": "deadbeef", "cost": 0.42, "is_error": False}),))
    runner._feed_trajectory_ledger(job.job_id, sig)

    snapshot = TrajectoryLedger(store, job.job_id).snapshot()
    milestone = snapshot["lists"]["milestones"][-1]
    assert (milestone["kind"], milestone["ref"]) == ("done", "deadbeef")
    cost_point = snapshot["lists"]["cost_points"][-1]
    assert (cost_point["cost_usd"], cost_point["attempt_no"], cost_point["source"]) == (0.42, 1, "worker")


# ------------------------------------------------------- diff sampling debounce


def test_no_diffscan_call_on_unchanged_fingerprint(steward_home, monkeypatch):
    store = Store(steward_home)
    job = _contracted_job(store)
    store.add_check(job.job_id, commit_sha="base-sha", command="baseline", exit_code=0, results={}, label="baseline")
    factory = _Factory(scripts=[[]])
    supervisor = _DummySupervisor()
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock(), trajectory=supervisor)
    runner.start(job.job_id)

    calls = []
    import steward.runner as runner_module

    def fake_scan(worktree, base_sha, cmd_runner):
        calls.append(base_sha)
        return runner_module.diffscan.DiffSample(changed_lines=1, files=["a.py"])

    monkeypatch.setattr(runner_module.diffscan, "scan", fake_scan)

    sig = _sig(diff_fingerprint="fp-a")
    runner._feed_trajectory_ledger(job.job_id, sig)
    assert len(calls) == 1  # fingerprint changed from None -> "fp-a"

    runner._feed_trajectory_ledger(job.job_id, sig)  # same fingerprint again
    assert len(calls) == 1  # no second scan


def test_diffscan_waits_60s_even_on_a_new_fingerprint(steward_home, monkeypatch):
    store = Store(steward_home)
    job = _contracted_job(store)
    store.add_check(job.job_id, commit_sha="base-sha", command="baseline", exit_code=0, results={}, label="baseline")
    factory = _Factory(scripts=[[]])
    supervisor = _DummySupervisor()
    clock = _Clock(start=1000.0, step=0.0)
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=clock, trajectory=supervisor)
    runner.start(job.job_id)

    calls = []
    import steward.runner as runner_module

    def fake_scan(worktree, base_sha, cmd_runner):
        calls.append(base_sha)
        return runner_module.diffscan.DiffSample(changed_lines=1, files=["a.py"])

    monkeypatch.setattr(runner_module.diffscan, "scan", fake_scan)

    runner._feed_trajectory_ledger(job.job_id, _sig(diff_fingerprint="fp-a"))
    assert len(calls) == 1

    clock._next = 1010.0  # only 10s later
    runner._feed_trajectory_ledger(job.job_id, _sig(diff_fingerprint="fp-b"))
    assert len(calls) == 1  # too soon, even though the fingerprint changed

    clock._next = 1070.0  # >= 60s since the last sample
    runner._feed_trajectory_ledger(job.job_id, _sig(diff_fingerprint="fp-b"))
    assert len(calls) == 2


# ------------------------------------------------------- decision facts + inner verdict


def test_guide_action_records_a_failed_approach_and_a_steering_decision(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(scripts=[[_obs(lifecycle="working"), _obs(lifecycle="working")]])
    supervisor = _DummySupervisor()
    runner = Runner(
        store, factory, _git_runner(), _fake_diagnoser(verdict="retry_with_guidance"),
        clock=_Clock(), policy=RecoveryPolicy(), trajectory=supervisor,
    )
    runner.start(job.job_id)
    Steering(store).record(job.job_id, "focus", "look here", "req-1")

    # Force a stall: `suspect_stall` requires no progress + no permission
    # prompt for long enough; the simplest deterministic trigger already
    # exercised elsewhere in this suite is a `same_error_count` streak, but
    # here we drive it directly through `steward.recovery.decide` semantics
    # by monkeypatching the classifier via a long-quiet worker observation
    # is out of scope for a unit test of the *ledger* feed — instead call
    # the private hook directly with a scripted guide/replace Action.
    from steward.heartbeat import Class
    from steward.recovery import Action
    from steward.models import Judgment as J

    action = Action(kind="guide", detail="try a different approach", judgment=J.model_informed_code_authorized, rule="diagnosis_verdict_retry_with_guidance")
    runner._feed_trajectory_decision_facts(job.job_id, Class.suspect_stall, action)

    snapshot = TrajectoryLedger(store, job.job_id).snapshot()
    kinds = [f["kind"] for f in snapshot["lists"]["failed_approaches"]]
    assert "guide" in kinds
    assert "stall_diagnosis" in kinds  # diagnosed_this_tick, rule != cap
    decisions = snapshot["lists"]["decisions"]
    assert any(d["origin"] == "cli" for d in decisions)


def test_already_diagnosed_cap_does_not_record_a_stall_diagnosis_fact(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(scripts=[[]])
    supervisor = _DummySupervisor()
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock(), trajectory=supervisor)
    runner.start(job.job_id)

    from steward.heartbeat import Class
    from steward.recovery import Action
    from steward.models import Judgment as J

    action = Action(kind="none", detail="already diagnosed", judgment=J.code, rule="stall_episode_diagnosis_cap")
    runner._feed_trajectory_decision_facts(job.job_id, Class.suspect_stall, action)

    snapshot = TrajectoryLedger(store, job.job_id).snapshot()
    kinds = [f["kind"] for f in snapshot["lists"]["failed_approaches"]]
    assert "stall_diagnosis" not in kinds


def test_decision_fold_never_re_records_the_same_instruction_twice(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(scripts=[[]])
    supervisor = _DummySupervisor()
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock(), trajectory=supervisor)
    runner.start(job.job_id)
    Steering(store).record(job.job_id, "focus", "look here", "req-1")

    from steward.heartbeat import Class
    from steward.recovery import Action
    from steward.models import Judgment as J

    action = Action(kind="none", detail="", judgment=J.code, rule="no_recovery_needed")
    runner._feed_trajectory_decision_facts(job.job_id, Class.progressing, action)
    runner._feed_trajectory_decision_facts(job.job_id, Class.progressing, action)

    snapshot = TrajectoryLedger(store, job.job_id).snapshot()
    assert len(snapshot["lists"]["decisions"]) == 1


# ------------------------------------------------------- supervisor.step() wiring


def test_supervisor_step_is_called_every_tick(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(scripts=[[_obs(), _obs()]])
    supervisor = _DummySupervisor()
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock(), trajectory=supervisor)
    runner.start(job.job_id)

    runner.tick(job.job_id)
    runner.tick(job.job_id)

    assert supervisor.calls == [job.job_id, job.job_id]


def test_supervisor_exception_does_not_break_the_tick(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(scripts=[[_obs()]])
    supervisor = _DummySupervisor(raise_on_step=True)
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock(), trajectory=supervisor)
    runner.start(job.job_id)

    report = runner.tick(job.job_id)

    assert report.job_id == job.job_id  # the tick completed and returned normally
    errors = [e for e in store.events(job.job_id) if e.what == "trajectory_error"]
    assert len(errors) == 1
    assert errors[0].evidence["error"] == "supervisor exploded"


def test_a_trajectory_revision_is_delivered_exactly_once_on_the_next_tick(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(scripts=[[_obs(), _obs(), _obs()]])

    def on_step(job_id: str) -> None:
        Steering(store).record(
            job_id, "nudge", "narrow the scope", request_id="trajectory-rev-1", origin="trajectory"
        )

    supervisor = _DummySupervisor(on_step=on_step)
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock(), trajectory=supervisor)
    runner.start(job.job_id)

    runner.tick(job.job_id)  # tick N: supervisor records the revision (via on_step, after delivery step)
    worker = factory.created[0]
    assert worker.delivered == []  # not delivered on the same tick it was recorded

    runner.tick(job.job_id)  # tick N+1: delivery step (2) picks it up
    assert worker.delivered == ["[rev=1] narrow the scope"]

    runner.tick(job.job_id)  # tick N+2: never resent
    assert worker.delivered == ["[rev=1] narrow the scope"]


# ------------------------------------------------------- diff base (audit rec 3a)


def test_committed_work_is_sampled_against_the_merge_base_without_a_baseline(steward_home, monkeypatch):
    """No baseline check and an unchanged `git diff` (all work committed): a new HEAD must still be
    sampled, against the merge-base — not skipped, which showed the advisor '0 files'."""
    store = Store(steward_home)
    job = _contracted_job(store)
    monkeypatch.setattr("steward.jobdiff.job_diff_base", lambda st, j, run=None: ("fork-point", Path("/wt"), "HEAD"))

    def respond(argv, cwd):
        out = ""
        if argv[:2] == ["git", "rev-parse"]:
            out = "src-head\n" if str(cwd) == "/repo" else "wt-head\n"
        elif argv[:2] == ["git", "merge-base"]:
            out = "fork-point\n"
        return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=out, stderr="", duration_s=0.0)

    runner = Runner(store, _Factory(scripts=[[]]), FakeRunner(respond), _fake_diagnoser(), clock=_Clock(), trajectory=_DummySupervisor())
    runner.start(job.job_id)
    calls = []
    import steward.runner as runner_module

    monkeypatch.setattr(runner_module.diffscan, "scan",
                        lambda wt, base, r: calls.append(base) or runner_module.diffscan.DiffSample(changed_lines=1, files=["a.py"]))
    runner._feed_trajectory_ledger(job.job_id, _sig(diff_fingerprint="fp-a", head_sha="c1"))
    assert calls == ["fork-point"]
    runner._feed_trajectory_ledger(job.job_id, _sig(diff_fingerprint="fp-a", head_sha="c1"))
    assert len(calls) == 1
