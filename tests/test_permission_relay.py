"""Permission-prompt relay (Phase 8 live-run gap, SP-2, SP-19, SP-24).

A worker blocked on a permission prompt outside its pre-authorized tools
classifies `waiting`; the runner opens exactly one `kind="permission"`
approval and notifies once (not per tick), relays a human decision back into
the worker as soon as it's seen, and treats direct pane input (break-glass)
as making the open approval stale rather than fighting it.
"""

from __future__ import annotations

from steward import approvals
from steward.contract import Contract, store_contract
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.models import JobState, Judgment
from steward.runner import Runner
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store
from steward.worker import FakeWorker, WorkerObservation

_PROMPT_TAIL = (
    " Bash command\n"
    "   rm -rf build/\n"
    "   clean the build dir\n"
    " This command requires approval\n"
    " Do you want to proceed?\n"
    " ❯ 1. Yes\n"
)

_PROMPT_OBS = WorkerObservation(lifecycle="blocked", output_tail=_PROMPT_TAIL, pid_alive=True, ts="t")
_IDLE_OBS = WorkerObservation(lifecycle="working", output_tail="continuing...", pid_alive=True, ts="t2")


def _contracted_job(store, goal="do the thing", repo="/repo"):
    job = store.create_job(goal, repo)
    contract = Contract(
        goal=goal, boundaries=[], done_when=["done"], next_step="", authority=[], assumptions=[]
    )
    store_contract(store, job.job_id, contract)
    return job


def _git_and_tg_runner(*, head="sha-1"):
    tg_calls = []

    def respond(argv, cwd):
        if argv[:2] == ["tg", "send"]:
            tg_calls.append(argv)
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)
        if argv[:2] == ["git", "rev-parse"]:
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=head + "\n", stderr="", duration_s=0.0)
        if argv[:2] in (["git", "status"], ["git", "diff"]):
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    return FakeRunner(respond), tg_calls


def _fake_diagnoser():
    return FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="try again", hypothesis="hang"))


class _Factory:
    def __init__(self, scripts=None):
        self._scripts = list(scripts) if scripts else []
        self.created: list[FakeWorker] = []

    def __call__(self, existing_ref):
        script = self._scripts.pop(0) if self._scripts else []
        worker = FakeWorker(script=script)
        self.created.append(worker)
        return worker


# -- opening: one approval, one NEEDS_YOU, not per tick ----------------------


def test_permission_prompt_opens_one_approval_and_notifies_once(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_NOTIFIER", "telegram")
    store = Store(steward_home)
    job = _contracted_job(store)
    fake, tg_calls = _git_and_tg_runner()
    factory = _Factory(scripts=[[_PROMPT_OBS, _PROMPT_OBS]])
    runner = Runner(store, factory, fake, _fake_diagnoser())
    runner.start(job.job_id)
    tg_calls.clear()  # drop the RUNNING notification from start()

    runner.tick(job.job_id)
    runner.tick(job.job_id)

    permission_approvals = [a for a in approvals.list_approvals(store, job.job_id) if a["kind"] == "permission"]
    assert len(permission_approvals) == 1
    assert permission_approvals[0]["status"] == "pending"
    assert "rm -rf build/" in permission_approvals[0]["question"]
    assert len(tg_calls) == 1
    assert store.get_job(job.job_id).state == JobState.RUNNING

    events = [e for e in store.events(job.job_id) if e.rule == "worker_permission_requested"]
    assert len(events) == 1
    assert events[0].judgment == Judgment.code


# -- relay: approve/deny sent to the worker exactly once ---------------------


def test_approve_via_api_relays_answer_once_on_next_tick(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    fake = _git_and_tg_runner()[0]
    factory = _Factory(scripts=[[_PROMPT_OBS, _IDLE_OBS, _IDLE_OBS]])
    runner = Runner(store, factory, fake, _fake_diagnoser())
    runner.start(job.job_id)
    worker = factory.created[0]

    runner.tick(job.job_id)  # opens the approval
    record = next(a for a in approvals.list_approvals(store, job.job_id) if a["kind"] == "permission")
    approvals.decide(store, job.job_id, record["id"], "approve", request_id="req-1")

    runner.tick(job.job_id)  # relays the approval
    runner.tick(job.job_id)  # must not relay a second time

    assert worker.answered == [True]
    relayed = approvals.get_approval(store, job.job_id, record["id"])
    assert relayed["relayed_at"] is not None
    events = [e for e in store.events(job.job_id) if e.rule == "permission_decision_relayed"]
    assert len(events) == 1


def test_deny_via_api_sends_denial_and_no_second_relay(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    fake = _git_and_tg_runner()[0]
    factory = _Factory(scripts=[[_PROMPT_OBS, _IDLE_OBS, _IDLE_OBS]])
    runner = Runner(store, factory, fake, _fake_diagnoser())
    runner.start(job.job_id)
    worker = factory.created[0]

    runner.tick(job.job_id)
    record = next(a for a in approvals.list_approvals(store, job.job_id) if a["kind"] == "permission")
    approvals.decide(store, job.job_id, record["id"], "deny", request_id="req-1")

    runner.tick(job.job_id)
    runner.tick(job.job_id)

    assert worker.answered == [False]


# -- break-glass: answered in the pane before a decision was made -----------


def test_answered_in_pane_before_decision_marks_stale_and_records_break_glass(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    fake = _git_and_tg_runner()[0]
    factory = _Factory(scripts=[[_PROMPT_OBS, _IDLE_OBS]])
    runner = Runner(store, factory, fake, _fake_diagnoser())
    runner.start(job.job_id)
    worker = factory.created[0]

    runner.tick(job.job_id)  # opens the approval, still pending
    record = next(a for a in approvals.list_approvals(store, job.job_id) if a["kind"] == "permission")

    runner.tick(job.job_id)  # worker no longer blocked without any decision

    assert worker.answered == []  # never sent input for a pending approval
    stale = approvals.get_approval(store, job.job_id, record["id"])
    assert stale["status"] == "stale"
    events = [e for e in store.events(job.job_id) if e.what == "break_glass_input"]
    assert len(events) == 1
    assert events[0].judgment == Judgment.human
    assert events[0].evidence["approval_id"] == record["id"]
