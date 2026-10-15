"""v2.2 Phase 20 (DL-03, DL-18): a deadline job never auto-merges, by any route, ever. Human incorporate still works."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from steward import autopilot, cli, closeout, deadline, followup
from steward import incorporate as inc
from steward.contract import Contract
from steward.models import JobState, Judgment
from steward.runner_exec import FakeRunner
from steward.store import Store
from steward.verify import Verification

NOW = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)


def _state(store, job_id, state):
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job_id))


def _deadline_job(store, state=JobState.RUNNING, rid="r1"):
    job = store.create_job("Ship the importer.", "/tmp")
    _state(store, job.job_id, JobState.RUNNING)
    deadline.revise(store, job.job_id, action="create", judgment=Judgment.human, origin="cli", request_id=rid,
                    reason="initial", due=deadline.parse_due("+4h", now=NOW, tz="America/Los_Angeles"))
    _state(store, job.job_id, state)
    return job.job_id


@pytest.fixture
def store(steward_home, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("an automatic route reached merge machinery for a deadline job")

    for name in ("incorporate", "preflight"):
        monkeypatch.setattr(inc, name, forbidden)
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "auto")
    return Store(steward_home)


def _passing():
    from steward.baseline import CheckResult

    return Verification(commit_sha="abc123", dirty=False, new_failures=[], fixed=[], preexisting_failures=[],
                        results=[CheckResult("pytest -q", 0, {"t::a": "passed"})])


def _contract():
    return Contract(goal="Ship the importer.", done_when=["tests pass"])


def _skips(store, job_id):
    return [e for e in store.events(job_id) if e.what == "autopilot_skipped" and e.evidence.get("gate") == "deadline"]


def test_gate1_closeout_routes_passing_checks_to_needs_human_pending_verification(store):
    jid = _deadline_job(store, JobState.WORKER_FINISHED)
    assert closeout.closeout(store, jid, _passing(), [], _contract()) == JobState.NEEDS_HUMAN
    last = store.events(jid)[-1]
    assert last.rule == "acceptance_unresolved" and last.judgment == Judgment.code
    assert any(r.startswith(closeout.DEADLINE_PENDING) for r in last.evidence["reasons"])
    assert not any("deadline_candidate_ready" in str(e.evidence) for e in store.events(jid))


def test_ordinary_job_closeout_still_auto_accepts_after_its_ruling(store):
    job = store.create_job("Ship it.", "/tmp")
    _state(store, job.job_id, JobState.WORKER_FINISHED)
    assert closeout.closeout(store, job.job_id, _passing(), [], _contract()) == JobState.CHECKS_PASSED
    from ruling_support import answer

    answer(store, job.job_id)  # the one completion ruling (finished) + the code guards
    assert closeout.decide_after_ruling(store, job.job_id, _contract()) == JobState.ACCEPTED
    assert autopilot.deadline_gate(store, job.job_id) is None


@pytest.mark.parametrize("after", ["nothing", "clear", "mode_off", "feature_off", "restart"])
def test_gate2_human_accepted_deadline_job_never_auto_merges(store, steward_home, monkeypatch, after):
    jid = _deadline_job(store, JobState.NEEDS_HUMAN)
    closeout.accept(store, jid)  # Sam's accept is not a merge
    if after == "clear":
        deadline.revise(store, jid, action="clear", judgment=Judgment.human, origin="cli", request_id="c", reason="x")
    elif after == "mode_off":
        deadline.set_mode(store, jid, "off", judgment=Judgment.human, origin="cli", request_id="m")
    elif after == "feature_off":
        monkeypatch.setenv("STEWARD_DEADLINE", "0")
    elif after == "restart":
        store = Store(steward_home)  # a fresh process/connection
    runner = FakeRunner()
    for _ in range(3):
        assert autopilot.ensure_attempted(store, jid, runner) == {"outcome": "skipped", "detail": "deadline_mode"}
        assert autopilot.try_merge(store, jid, runner)["outcome"] == "skipped"
    assert store.kv_get(jid, autopilot.ATTEMPT_KEY) is None  # the attempt was never consumed
    assert len(_skips(store, jid)) == 1  # bounded event
    assert runner.calls == []  # no git, no lock work, no follow-up, no deploy


def test_follow_up_and_fix_merge_children_inherit_the_gate(store):
    parent = _deadline_job(store, JobState.NEEDS_HUMAN)
    closeout.accept(store, parent)
    deadline.revise(store, parent, action="clear", judgment=Judgment.human, origin="cli", request_id="c", reason="x")
    child = store.create_job("fix it", "/tmp")
    followup.link(store, parent, child.job_id)
    grandchild = store.create_job("fix the fix", "/tmp")
    store.kv_set(grandchild.job_id, "autopilot_followup", child.job_id)
    for jid in (child.job_id, grandchild.job_id):
        _state(store, jid, JobState.ACCEPTED)
        res = autopilot.ensure_attempted(store, jid, FakeRunner())
        assert res["outcome"] == "skipped" and parent[:8] in res["detail"]
    assert autopilot.start_fix_merge(store, parent, FakeRunner())["accepted"] is False
    assert autopilot._default_followup(store, parent, "resolve")["accepted"] is False
    assert len(store.list_jobs()) == 3  # no child job was started by any route


def test_commitment_retrofitted_onto_an_accepted_job_still_gates(store):
    job = store.create_job("Already accepted.", "/tmp")
    _state(store, job.job_id, JobState.ACCEPTED)
    deadline.revise(store, job.job_id, action="set", judgment=Judgment.human, origin="cli", request_id="late",
                    reason="retrofit", due=deadline.parse_due("+1h", now=NOW, tz="America/Los_Angeles"))
    assert autopilot.ensure_attempted(store, job.job_id, FakeRunner())["outcome"] == "skipped"


def test_only_human_incorporate_merges(store, monkeypatch):
    jid = _deadline_job(store, JobState.NEEDS_HUMAN)
    closeout.accept(store, jid)
    calls = []
    monkeypatch.setattr(inc, "incorporate", lambda s, j: calls.append(j) or {"accepted": True, "detail": "merged",
                                                                             "merge_commit": "m1", "noop": False})
    receipt = cli._build_control_handlers(store)["incorporate"]({"args": {"job_id": jid}, "request_id": "h1",
                                                                "op": "incorporate", "origin": "cli"})
    assert receipt["accepted"] and calls == [jid]


def test_deadline_landing_during_the_merged_tree_test_still_blocks_the_merge(store, monkeypatch):
    """Review finding: try_merge checked the gate once; a commitment set while the merged tree was being tested must
    still stop incorporate and deploy-self (re-checked right before both)."""
    from types import SimpleNamespace

    job = store.create_job("Already accepted.", "/tmp")
    _state(store, job.job_id, JobState.ACCEPTED)
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "auto")
    monkeypatch.setattr(autopilot, "floor_reason", lambda s, j: None)
    pf = SimpleNamespace(error="", contained=False, block="", conflicts=[], branch="steward/x", source_branch="main",
                         files=["a.py"], commit_count=1)
    monkeypatch.setattr(inc, "preflight", lambda s, j: pf)

    def tests_then_deadline(*a, **k):
        _state(store, job.job_id, JobState.RUNNING)  # a running job may take a deadline...
        deadline.revise(store, job.job_id, action="set", judgment=Judgment.human, origin="cli", request_id="mid",
                        reason="mid-merge", due=deadline.parse_due("+1h", now=NOW, tz="America/Los_Angeles"))
        _state(store, job.job_id, JobState.ACCEPTED)  # ...and the job is still ACCEPTED when the merge resumes
        return True, "tree-sha", []

    monkeypatch.setattr(autopilot, "_test_merged_tree", tests_then_deadline)
    res = autopilot.try_merge(store, job.job_id, FakeRunner())
    assert res["outcome"] == "skipped" and res["detail"] == "deadline_mode"  # inc.incorporate raises if reached


def test_unverifiably_deep_ancestry_is_protected(store):
    prev = None
    ids = []
    for i in range(deadline._MAX_ANCESTRY + 5):
        j = store.create_job(f"j{i}", "/tmp").job_id
        if prev:
            store.kv_set(j, "parent_job", prev)
        prev = j
        ids.append(j)
    assert deadline.merge_protected(store, ids[-1]).startswith("deadline_mode (ancestry too deep")
    assert deadline.merge_protected(store, ids[3]) is None  # a short ordinary chain stays unprotected
