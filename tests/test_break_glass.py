"""Break-glass detection (MOSHI-04, SP-24).

A prompt-echo line in the worker's output that doesn't match anything the
runner itself delivered is recorded as `break_glass_input` (judgment=human)
— never blocked, never treated as a steering instruction revision.
"""

from __future__ import annotations

from steward.contract import Contract, store_contract
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.models import Judgment
from steward.runner import Runner
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store
from steward.worker import FakeWorker, WorkerObservation


def _contracted_job(store, goal="do the thing", repo="/repo"):
    job = store.create_job(goal, repo)
    contract = Contract(
        goal=goal, boundaries=[], done_when=["done"], next_step="", authority=[], assumptions=[]
    )
    store_contract(store, job.job_id, contract)
    return job


def _git_runner(*, head="sha-1"):
    def respond(argv, cwd):
        if argv[:2] == ["git", "merge-base"]:  # the job's start commit (differs from `head`: a new commit exists)
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout="base-0\n", stderr="", duration_s=0.0)
        if argv[:2] == ["git", "rev-parse"]:
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=head + "\n", stderr="", duration_s=0.0)
        return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout="", stderr="", duration_s=0.0)

    return FakeRunner(respond)


def _fake_diagnoser():
    return FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="try again", hypothesis="hang"))


class _Factory:
    def __init__(self, scripts=None):
        self._scripts = list(scripts) if scripts else []

    def __call__(self, existing_ref):
        script = self._scripts.pop(0) if self._scripts else []
        return FakeWorker(script=script)


def test_prompt_line_matching_a_delivery_is_not_flagged(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(scripts=[[WorkerObservation(
        lifecycle="working", output_tail="> look at retries first\nworking...", pid_alive=True, ts="t",
    )]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser())
    runner.start(job.job_id)
    # Simulate the runner itself having just delivered this exact text.
    runner._track_delivery(job.job_id, "look at retries first")

    runner.tick(job.job_id)

    events = [e for e in store.events(job.job_id) if e.what == "break_glass_input"]
    assert events == []


def test_prompt_line_not_matching_any_delivery_is_flagged_as_human(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(scripts=[[WorkerObservation(
        lifecycle="working", output_tail="> rm -rf / please\nworking...", pid_alive=True, ts="t",
    )]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser())
    runner.start(job.job_id)

    runner.tick(job.job_id)

    events = [e for e in store.events(job.job_id) if e.what == "break_glass_input"]
    assert len(events) == 1
    assert events[0].judgment == Judgment.human
    assert events[0].evidence["excerpt"] == "rm -rf / please"


def test_codex_style_prompt_marker_is_also_detected(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(scripts=[[WorkerObservation(
        lifecycle="working", output_tail="› do something unplanned", pid_alive=True, ts="t",
    )]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser())
    runner.start(job.job_id)

    runner.tick(job.job_id)

    events = [e for e in store.events(job.job_id) if e.what == "break_glass_input"]
    assert len(events) == 1
    assert events[0].evidence["excerpt"] == "do something unplanned"


def test_break_glass_never_blocks_dispatch_or_state(steward_home):
    from steward.models import JobState

    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(scripts=[[WorkerObservation(
        lifecycle="working", output_tail="> unplanned keystrokes", pid_alive=True, ts="t",
    )]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser())
    runner.start(job.job_id)

    report = runner.tick(job.job_id)

    assert store.get_job(job.job_id).state == JobState.RUNNING
    assert report.job_id == job.job_id  # tick completed normally, nothing raised


def test_break_glass_never_recorded_as_a_steering_instruction(steward_home):
    from steward.steering import Steering

    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(scripts=[[WorkerObservation(
        lifecycle="working", output_tail="> unplanned keystrokes", pid_alive=True, ts="t",
    )]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser())
    runner.start(job.job_id)

    runner.tick(job.job_id)

    assert Steering(store).all_instructions(job.job_id) == []


def test_same_excerpt_is_only_recorded_once_across_ticks(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    obs = WorkerObservation(
        lifecycle="working", output_tail="> unplanned keystrokes", pid_alive=True, ts="t",
    )
    factory = _Factory(scripts=[[obs, obs, obs]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser())
    runner.start(job.job_id)

    runner.tick(job.job_id)
    runner.tick(job.job_id)
    runner.tick(job.job_id)

    events = [e for e in store.events(job.job_id) if e.what == "break_glass_input"]
    assert len(events) == 1


def test_excerpt_capped_at_200_chars(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    long_text = "x" * 500
    factory = _Factory(scripts=[[WorkerObservation(
        lifecycle="working", output_tail=f"> {long_text}", pid_alive=True, ts="t",
    )]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser())
    runner.start(job.job_id)

    runner.tick(job.job_id)

    events = [e for e in store.events(job.job_id) if e.what == "break_glass_input"]
    assert len(events[0].evidence["excerpt"]) == 200
