"""v2.2 Phase 21 Task 2 (DL-06): milestones and the candidate are met only by code-checked evidence at an exact SHA."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from deadline_fixtures import Repo, commit_deadline, plan_json, set_state
from steward import autopilot, closeout, deadline, deadline_evidence, deadline_plan
from steward.baseline import CheckResult
from steward.contract import Contract
from steward.models import JobState, Judgment
from steward.runner_exec import FakeRunner, SubprocessRunner
from steward.store import Store
from steward.verify import Verification

NOW = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
DUE = NOW + timedelta(hours=4)
RUN = SubprocessRunner()
PASS = {"tests.test_importer::test_rows": "passed"}


@pytest.fixture
def store(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "auto")
    return Store(steward_home)


@pytest.fixture
def repo(tmp_path, store):
    r = Repo(tmp_path, store)
    commit_deadline(store, r.job_id, now=NOW)
    return r


def _adopt(store, repo, **kw):
    repo.commit({deadline_plan.plan_path(repo.job_id): plan_json(repo.job_id, deadline.current(store, repo.job_id)["rev"],
                                                                  due=DUE, now=NOW, **kw)}, "deadline: plan")
    deadline_plan.refresh(store, repo.job_id, worktree=repo.wt, runner=RUN, now=NOW)
    plan = deadline_plan.current_plan(store, repo.job_id)
    assert plan and plan["source"] == "worker"
    return plan


def _cleanup(repo):
    return repo.commit({deadline_plan.plan_path(repo.job_id): None}, "deadline: remove plan")


def _verification(sha, tests=None, exit_code=0):
    return Verification(commit_sha=sha, results=[CheckResult(command="pytest -q", exit_code=exit_code,
                                                             tests=PASS if tests is None else tests)])


def _verdict(store, repo, sha, tests=None):
    return deadline_evidence.candidate_verdict(
        commitment=deadline.current(store, repo.job_id), plan=deadline_plan.current_plan(store, repo.job_id),
        verification=_verification(sha, tests), worktree=repo.wt, runner=RUN, base=repo.base, job_id=repo.job_id)


# -- milestones ----------------------------------------------------------------------------------------

def test_subject_line_alone_never_meets_a_commit_milestone(store, repo):
    plan = _adopt(store, repo)
    m1 = plan["milestones"][0]
    repo.commit({"notes.txt": "claimed"}, "deadline(M1): parser done")  # the right subject, the wrong work
    hist = deadline_evidence.history(repo.wt, RUN, repo.base, repo.head())
    assert deadline_evidence.milestone_evidence(m1, hist=hist) is None
    t = NOW + timedelta(minutes=30)
    sha = repo.commit({"importer.py": "def rows(): return []\n"}, "wip", when=t)
    hist = deadline_evidence.history(repo.wt, RUN, repo.base, repo.head())
    met = deadline_evidence.milestone_evidence(m1, hist=hist)
    assert met["met_sha"] == sha and met["met_at"] == "2026-10-02T20:30:00Z"  # the commit's time, not when noticed
    assert met["evidence"] == {"kind": "commit", "paths": ["importer.py"]}


def test_unrelated_diff_does_not_meet_and_deleted_path_unmeets(store, repo):
    plan = _adopt(store, repo, m1_paths=("importer.py", "schema.py"))
    m1 = plan["milestones"][0]
    repo.commit({"importer.py": "x\n"}, "half")
    hist = deadline_evidence.history(repo.wt, RUN, repo.base, repo.head())
    assert deadline_evidence.milestone_evidence(m1, hist=hist) is None
    repo.commit({"importer.py": None, "schema.py": "y\n"}, "swap")
    hist = deadline_evidence.history(repo.wt, RUN, repo.base, repo.head())
    assert deadline_evidence.milestone_evidence(m1, hist=hist) is None


def test_units_evidence_needs_git_derived_structure_not_self_description():
    m = {"id": "M2", "evidence": "units:plan 21-01"}
    good = SimpleNamespace(units_source="planning", done={"plan 21-01": NOW.timestamp()}, head="abc")
    told = SimpleNamespace(units_source="descriptions", done={"plan 21-01": NOW.timestamp()}, head="abc")
    assert deadline_evidence.milestone_evidence(m, hist=[], structure=good)["met_sha"] == "abc"
    assert deadline_evidence.milestone_evidence(m, hist=[], structure=told) is None
    assert deadline_evidence.milestone_evidence({"id": "M3", "evidence": "units:nope"}, hist=[], structure=good) is None


# -- the candidate ------------------------------------------------------------------------------------------

def test_every_required_item_proven_at_the_exact_sha_is_ready(store, repo):
    _adopt(store, repo)
    repo.commit({"importer.py": "def rows(): return []\n", "tests/test_importer.py": "def test_rows(): pass\n"}, "R1")
    sha = _cleanup(repo)
    v = _verdict(store, repo, sha)
    assert v["ready"], v["reasons"]
    (r1,) = [r for r in v["items"] if r["item"] == "R1"]
    assert r1["outcome"] == "proven" and r1["sha"] == sha and r1["results"] == {
        "tests/test_importer.py::test_rows": "passed"}
    (o1,) = [r for r in v["items"] if r["item"] == "O1"]
    assert o1["class"] == "optional" and o1["outcome"] == "unproven"  # reported, never blocks


@pytest.mark.parametrize("case", ["subject_only", "missing_check", "failing_check", "late_regression",
                                  "omitted_item", "unrelated_diff"])
def test_unsupported_required_item_blocks_ready(store, repo, case):
    _adopt(store, repo)
    tests = None
    if case == "subject_only":
        repo.commit({"notes.txt": "R1 done"}, "deadline(R1): importer done")
    elif case == "unrelated_diff":
        repo.commit({"other.py": "x\n"}, "refactor")
    elif case in ("missing_check", "failing_check", "late_regression", "omitted_item"):
        repo.commit({"importer.py": "def rows(): return []\n"}, "R1")
        if case == "missing_check":
            tests = {"tests.test_other::test_x": "passed"}
        elif case == "failing_check":
            tests = {"tests.test_importer::test_rows": "failed"}
        elif case == "late_regression":
            repo.commit({"importer.py": None}, "oops, removed it")
        elif case == "omitted_item":
            repo.commit({"importer.py": None}, "dropped the importer")
    sha = _cleanup(repo)
    v = _verdict(store, repo, sha, tests)
    assert not v["ready"] and any(r.startswith("unproven_required_items: R1") for r in v["reasons"]), v


def test_proposed_optional_stays_required_and_owner_optional_drop_is_reported(store, repo):
    items = [{"id": "R1", "class": "required", "evidence": {"paths": ["importer.py"],
                                                            "tests": ["tests/test_importer.py::test_rows"]}},
             {"id": "O1", "class": "optional"},
             {"id": "W1", "class": "proposed-optional", "text": "retry UI", "evidence": {"paths": ["ui.py"],
                                                                                         "tests": []}}]
    _adopt(store, repo, items=items, extra={"dropped": ["O1"]})
    repo.commit({"importer.py": "x\n"}, "R1")
    sha = _cleanup(repo)
    v = _verdict(store, repo, sha)
    assert v["optional_dropped"] == ["O1"] and not v["ready"]
    (w1,) = [r for r in v["items"] if r["item"] == "W1"]
    assert w1["class"] == "required" and w1["outcome"] == "unproven"


def test_stale_plan_rev_and_plan_file_in_diff_block_ready(store, repo):
    _adopt(store, repo)
    sha = repo.commit({"importer.py": "x\n"}, "R1")  # no cleanup commit
    v = _verdict(store, repo, sha)
    assert v["plan_in_diff"] and not v["ready"]
    sha = _cleanup(repo)
    deadline.revise(store, repo.job_id, action="set", judgment=Judgment.human, origin="cli", request_id="r2",
                    reason="later", due=deadline.parse_due("+5h", now=NOW, tz="America/Los_Angeles"))
    v = _verdict(store, repo, sha)
    assert not v["ready"] and any(r.startswith("stale_plan_rev") for r in v["reasons"])


# -- closeout -------------------------------------------------------------------------------------------------

def _closeout_at(store, repo, sha, tests=None):
    set_state(store, repo.job_id, JobState.WORKER_FINISHED)
    verification = _verification(sha, tests)
    deadline_evidence.evaluate_candidate(store, repo.job_id, verification=verification, worktree=repo.wt, runner=RUN)
    return closeout.closeout(store, repo.job_id, verification, [], Contract(goal="g", done_when=["tests pass"]))


def test_closeout_ready_candidate_waits_for_sam_and_still_never_auto_merges(store, repo):
    _adopt(store, repo)
    repo.commit({"importer.py": "x\n"}, "R1")
    sha = _cleanup(repo)
    assert _closeout_at(store, repo, sha) == JobState.NEEDS_HUMAN
    last = store.events(repo.job_id)[-1]
    assert last.what == closeout.CANDIDATE_READY_WHAT and last.evidence["sha"] == sha
    closeout.accept(store, repo.job_id)  # human acceptance resolves acceptance, never authorizes a merge
    assert autopilot.ensure_attempted(store, repo.job_id, FakeRunner())["outcome"] == "skipped"


def test_closeout_unproven_item_is_acceptance_unresolved_not_ready(store, repo):
    _adopt(store, repo)
    repo.commit({"importer.py": "x\n"}, "R1")
    sha = _cleanup(repo)
    assert _closeout_at(store, repo, sha, tests={}) == JobState.NEEDS_HUMAN
    last = store.events(repo.job_id)[-1]
    assert last.what == "checks passed; acceptance unresolved"
    assert any("acceptance unresolved (deadline)" in r and "R1" in r for r in last.evidence["reasons"])


def test_closeout_sends_back_a_candidate_that_still_ships_the_plan_file(store, repo):
    _adopt(store, repo)
    sha = repo.commit({"importer.py": "x\n"}, "R1")
    assert _closeout_at(store, repo, sha) == JobState.RUNNING
    from steward.steering import Steering

    pending = [i["text"] for i in Steering(store).pending(repo.job_id)]
    assert any("cleanup commit" in t for t in pending)


def test_evidence_errors_are_never_ready(store, repo, monkeypatch):
    _adopt(store, repo)
    sha = _cleanup(repo)
    monkeypatch.setattr(deadline_evidence, "candidate_verdict", lambda **k: (_ for _ in ()).throw(OSError("disk")))
    v = deadline_evidence.evaluate_candidate(store, repo.job_id, verification=_verification(sha), worktree=repo.wt,
                                             runner=RUN)
    assert not v["ready"] and v["reasons"][0].startswith("evidence_error")


def test_canonical_test_ids_match_junit_and_pytest_forms():
    assert deadline_evidence.canonical_test("tests/test_x.py::TestC::test_y[a-b]") == \
        deadline_evidence.canonical_test("tests.test_x.TestC::test_y[a-b]")
    assert json.dumps(deadline_evidence.canonical_test("tests/test_x.py::t")) == '"tests.test_x.t"'
