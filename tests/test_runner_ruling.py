"""SRD-06/09/10 at the runner: CHECKS_PASSED waits for its ruling (the runner keeps ticking), then decides, notifies and
hands an ACCEPTED job to autopilot; no tech lead wired means "acceptance unresolved: no completion ruling"."""

from __future__ import annotations

import pytest

from steward import autopilot, closeout
from steward.baseline import CheckResult
from steward.contract import load_contract
from steward.models import JobState, Judgment
from steward.runner import Runner, wire_job_lead
from steward.store import Store
from steward.verify import Verification
from test_completion_ruling import SHA, ruling_answer
from test_runner import _contracted_job, _Factory, _fake_diagnoser, _git_runner, _Clock
from test_tech_lead_no_clog import ASTRA, FABLE, Gate, wait_done


@pytest.fixture
def parked_candidate(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store, "fix it")
    jid = job.job_id
    (store.home / "worktrees" / jid).mkdir(parents=True)
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    store.set_state(jid, JobState.WORKER_FINISHED, what="t", evidence={}, rule="t", judgment=Judgment.code)
    store.add_check(jid, commit_sha=SHA, command="pytest -q", exit_code=0, results={"t::a": "passed"}, label="verify")
    v = Verification(commit_sha=SHA, results=[CheckResult("pytest -q", 0, {"t::a": "passed"})])
    assert closeout.closeout(store, jid, v, [], load_contract(store, jid)) == JobState.CHECKS_PASSED
    runner = Runner(store, _Factory(scripts=[[]]), _git_runner(), _fake_diagnoser(), clock=_Clock())
    return store, jid, runner


def test_the_runner_waits_for_the_ruling_and_then_accepts_and_attempts_the_merge(parked_candidate, monkeypatch):
    store, jid, runner = parked_candidate
    attempts = []
    monkeypatch.setattr(autopilot, "ensure_attempted", lambda st, j, r, **kw: attempts.append(j))
    gate = Gate({"astra-tech-lead": [ruling_answer()], "fable-tech-lead": []})
    lead = wire_job_lead(runner, [ASTRA, FABLE], invoke=gate)

    assert runner._awaiting_ruling(jid) and runner._terminal_exit(jid) is None  # the runner must not exit now
    r1 = runner.tick(jid)
    assert r1.state == "CHECKS_PASSED" and "awaiting" in r1.detail
    wait_done(lead, jid)
    runner.tick(jid)  # the review answered: the ruling is stored (job lead step) ...
    r3 = runner.tick(jid)  # ... and the next tick decides
    assert r3.state == "ACCEPTED" and r3.action == "closeout"
    assert len(gate.calls) == 1
    assert attempts == [jid]
    assert not runner._awaiting_ruling(jid)


def test_no_tech_lead_wired_means_no_completion_ruling(parked_candidate):
    store, jid, runner = parked_candidate
    assert runner.job_lead is None

    report = runner.tick(jid)

    assert report.state == "NEEDS_HUMAN"
    assert "no completion ruling" in str(store.events(jid)[-1].evidence) + str(
        [e.evidence for e in store.events(jid) if e.what == "checks passed; acceptance unresolved"])


def test_an_unanswered_ruling_is_given_up_after_the_wait_cap(parked_candidate):
    store, jid, runner = parked_candidate
    gate = Gate({"astra-tech-lead": ["block"], "fable-tech-lead": ["block"]})
    wire_job_lead(runner, [ASTRA, FABLE], invoke=gate)
    runner.tick(jid)
    runner.clock._next += runner.RULING_WAIT_CAP_S + 5  # nobody answered for longer than the cap
    gate.release.set()

    report = runner.tick(jid)

    assert report.state == "NEEDS_HUMAN"
    assert "no completion ruling within" in str([e.evidence for e in store.events(jid)])


def _open_blocking(store, jid):
    store.kv_set(jid, "questions", [{"qid": "steward_questions:STEWARD-QUESTIONS.json:q1", "status": "open",
                                     "blocking": True}])


def test_post_ruling_guard_refuses_accepted_while_a_blocking_question_is_open(parked_candidate, monkeypatch):
    store, jid, runner = parked_candidate
    from ruling_support import answer
    attempts = []
    monkeypatch.setattr(autopilot, "ensure_attempted", lambda st, j, r, **kw: attempts.append(j))
    answer(store, jid, "finished")
    _open_blocking(store, jid)  # the question lands after the ruling was requested (the live race)

    assert closeout.decide_after_ruling(store, jid) == JobState.NEEDS_HUMAN
    assert store.get_job(jid).state == JobState.NEEDS_HUMAN
    ev = [e for e in store.events(jid) if e.what == "checks passed; acceptance unresolved"]
    assert any("open blocking question" in str(e.evidence) for e in ev)
    assert not attempts


def test_pre_ruling_probe_parks_on_an_open_blocking_question_without_buying_a_ruling(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store, "fix it")
    jid = job.job_id
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    store.set_state(jid, JobState.WORKER_FINISHED, what="t", evidence={}, rule="t", judgment=Judgment.code)
    _open_blocking(store, jid)
    v = Verification(commit_sha=SHA, results=[CheckResult("pytest -q", 0, {"t::a": "passed"})])

    assert closeout.closeout(store, jid, v, [], load_contract(store, jid)) == JobState.NEEDS_HUMAN
    assert not [e for e in store.events(jid) if e.what == "completion_ruling_requested"]

