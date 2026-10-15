"""v2.2 Phase 21 Task 1 (DL-05, DL-12): the worker plan's lifecycle — committed, adopted with its SHA, revised
without losing old targets, removed by a cleanup commit before the candidate. Real temp git repos; no network."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from deadline_fixtures import Repo, commit_deadline, git, plan_json
from steward import deadline, deadline_plan
from steward import incorporate as inc
from steward.brief import render_brief
from steward.contract import Contract
from steward.runner_exec import SubprocessRunner
from steward.store import Store

NOW = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
DUE = NOW + timedelta(hours=4)


@pytest.fixture
def store(steward_home):
    return Store(steward_home)


@pytest.fixture
def repo(tmp_path, store):
    r = Repo(tmp_path, store)
    commit_deadline(store, r.job_id, now=NOW)
    return r


def _refresh(store, repo, now=NOW):
    return deadline_plan.refresh(store, repo.job_id, worktree=repo.wt, runner=SubprocessRunner(), now=now)


def _path(repo):
    return deadline_plan.plan_path(repo.job_id)


# -- the artifact lifecycle proof (A-DL-3): cleanup commit -> absent from the final diff and preflight -----------

def test_cleanup_commit_proof_plan_absent_from_final_diff_and_preflight_but_kept_in_store(store, repo):
    path = _path(repo)
    plan_sha = repo.commit({path: plan_json(repo.job_id, 1, due=DUE, now=NOW)}, "deadline: plan")
    _refresh(store, repo)
    snap = deadline_plan.plan_doc(store, repo.job_id)["history"][-1]
    assert snap["sha"] == plan_sha and snap["raw"] == git(repo.wt, "show", f"{plan_sha}:{path}")
    repo.commit({"importer.py": "def rows(): return []\n"}, "work")
    before_cleanup = inc.preflight(store, repo.job_id)
    assert path in before_cleanup.files  # without the cleanup commit the plan would ship
    assert deadline_plan.in_final_diff(repo.wt, SubprocessRunner(), repo.base, repo.head(), repo.job_id)
    repo.commit({path: None}, "deadline: remove transient plan")
    pf = inc.preflight(store, repo.job_id)
    assert not pf.error and path not in pf.files and "importer.py" in pf.files
    final = git(repo.wt, "diff", "--name-only", f"{repo.base}...{repo.head()}").split()
    assert path not in final
    assert not deadline_plan.in_final_diff(repo.wt, SubprocessRunner(), repo.base, repo.head(), repo.job_id)
    assert plan_sha in git(repo.wt, "log", "--format=%H", "--", path)  # intermediate history stays auditable
    _refresh(store, repo)  # HEAD moved, file gone: the adopted plan stays, nothing is "missing"
    assert deadline_plan.current_plan(store, repo.job_id)["sha"] == plan_sha
    assert deadline_plan.plan_doc(store, repo.job_id)["history"][-1]["raw"] == snap["raw"]


# -- validation -----------------------------------------------------------------------------------------------

def _commitment(store, repo):
    return deadline.current(store, repo.job_id)


def _validate(store, repo, raw, now=NOW):
    return deadline_plan.validate(raw, commitment=_commitment(store, repo), now=now)


def test_valid_plan_normalizes_targets_and_drops_prose_claims(store, repo):
    raw = json.loads(plan_json(repo.job_id, 1, due=DUE, now=NOW))
    raw["milestones"][0]["met"] = True
    raw["surprise"] = "ignored"
    plan, reasons = _validate(store, repo, json.dumps(raw))
    assert reasons == [] and plan["milestones"][0]["target_at_utc"].endswith("Z")
    assert "met" not in plan["milestones"][0] and "surprise" not in plan
    assert plan["milestones"][-1]["id"] == "candidate" and "R1" in plan["milestones"][0]["items"]


@pytest.mark.parametrize("mutate,reason", [
    (lambda p: p.update(commitment_rev=0), "stale_commitment_rev"),
    (lambda p: p.update(items=[i for i in p["items"] if i["id"] != "R1"]), "missing_required_item"),
    (lambda p: p["items"][0].update({"class": "optional"}), "reclassified_required"),
    (lambda p: p.update(dropped=["R1"]), "unauthorized_drop"),
    (lambda p: p["items"].append({"id": "W1", "class": "proposed-optional", "text": "retry UI"}) or
     p.update(dropped=["W1"]), "unauthorized_drop"),
    (lambda p: p.update(adaptations=[{"kind": "rewrite_goal"}]), "unauthorized_adaptation"),
    (lambda p: p.update(adaptations=[{"kind": "stop_retry", "failures": 2}]), "stop_retry_unproven"),
    (lambda p: p["milestones"][0].update(paths=[]), "commit_evidence_needs_paths"),
    (lambda p: p["milestones"][-1].update(target_at=(DUE - timedelta(minutes=1)).isoformat()), "beyond_deadline"),
    (lambda p: p["milestones"][0].update(target_at="2026-10-02 15:00"), "bad_target"),
    (lambda p: p["milestones"][0].update(depends_on=["M9"]), "bad_dependency"),
    (lambda p: p.update(milestones=p["milestones"][:1]), "missing_candidate_milestone"),
])
def test_invalid_plans_are_rejected_with_a_named_reason(store, repo, mutate, reason):
    raw = json.loads(plan_json(repo.job_id, 1, due=DUE, now=NOW))
    mutate(raw)
    plan, reasons = _validate(store, repo, json.dumps(raw))
    assert plan is None and any(r.startswith(reason) for r in reasons), reasons


def test_malformed_json_and_owner_optional_drop(store, repo):
    assert _validate(store, repo, "{nope")[1][0].startswith("malformed_json")
    raw = json.loads(plan_json(repo.job_id, 1, due=DUE, now=NOW))
    raw["adaptations"] = [{"kind": "drop_optional", "items": ["O1"], "reason": "M1 late"},
                          {"kind": "stop_retry", "failures": 3, "detail": "flaky fixture"}]
    plan, reasons = _validate(store, repo, json.dumps(raw))
    assert reasons == [] and plan["dropped"] == ["O1"]


def test_window_shorter_than_tail_is_adopted_as_infeasible_never_refused(store, tmp_path):
    r = Repo(tmp_path, store)
    commit_deadline(store, r.job_id, now=NOW, when="+3m")
    due = NOW + timedelta(minutes=3)
    raw = plan_json(r.job_id, 1, due=due, now=NOW, m1_target=NOW + timedelta(minutes=1),
                    cand_target=NOW + timedelta(minutes=2))
    plan, reasons = deadline_plan.validate(raw, commitment=deadline.current(store, r.job_id), now=NOW)
    assert reasons == [] and plan["infeasible"] is True


# -- adoption, revision and immutable target slots --------------------------------------------------------------

def test_committed_plan_is_adopted_once_per_head_and_dirty_tree_is_ignored(store, repo):
    path = _path(repo)
    (repo.wt / path).parent.mkdir(parents=True, exist_ok=True)
    (repo.wt / path).write_text(plan_json(repo.job_id, 1, due=DUE, now=NOW))  # uncommitted: never read
    _refresh(store, repo)
    assert deadline_plan.current_plan(store, repo.job_id) is None
    sha = repo.commit({path: (repo.wt / path).read_text()}, "deadline: plan")
    _refresh(store, repo)
    _refresh(store, repo)
    cur = deadline_plan.current_plan(store, repo.job_id)
    assert cur["sha"] == sha and cur["plan_rev"] == 1 and cur["source"] == "worker"
    adopted = [e for e in store.events(repo.job_id) if e.what == "deadline_plan_adopted"]
    assert len(adopted) == 1 and adopted[0].evidence["sha"] == sha


def test_moving_a_target_keeps_the_old_slot_and_adds_a_new_one(store, repo):
    path = _path(repo)
    m1_a = NOW + timedelta(hours=1)
    m1_b = NOW + timedelta(hours=2)
    repo.commit({path: plan_json(repo.job_id, 1, due=DUE, now=NOW, m1_target=m1_a)}, "plan v1")
    _refresh(store, repo)
    repo.commit({path: plan_json(repo.job_id, 1, due=DUE, now=NOW, m1_target=m1_a, extra={"approach": "v1b"})},
                "plan v1b: same targets")
    repo.commit({"x.txt": "x"}, "unrelated")
    _refresh(store, repo)
    keys = [t["key"] for t in deadline_plan.targets(store, repo.job_id)]
    assert len(keys) == 2  # M1 + candidate; an unchanged target never duplicates
    repo.commit({path: plan_json(repo.job_id, 1, due=DUE, now=NOW, m1_target=m1_b)}, "plan v2")
    _refresh(store, repo)
    slots = deadline_plan.targets(store, repo.job_id)
    m1 = [t for t in slots if t["milestone_id"] == "M1"]
    assert [t["target_at_utc"] for t in m1] == ["2026-10-02T21:00:00Z", "2026-10-02T22:00:00Z"]
    assert m1[0]["created_by_plan_rev"] < m1[1]["created_by_plan_rev"]
    ev = [e for e in store.events(repo.job_id) if e.what == "deadline_plan_adopted"][-1]
    assert ev.evidence["targets_moved"] == [{"milestone": "M1", "from": "2026-10-02T21:00:00Z",
                                             "to": "2026-10-02T22:00:00Z"}]


def test_bad_revision_is_rejected_once_per_sha_and_reason_and_last_valid_plan_stays(store, repo):
    path = _path(repo)
    good = repo.commit({path: plan_json(repo.job_id, 1, due=DUE, now=NOW)}, "plan")
    _refresh(store, repo)
    bad = json.loads(plan_json(repo.job_id, 1, due=DUE, now=NOW))
    bad["dropped"] = ["R1"]
    bad_sha = repo.commit({path: json.dumps(bad)}, "drop R1")
    _refresh(store, repo)
    store.kv_set(repo.job_id, deadline_plan.K_SEEN, {})  # force a re-read of the same HEAD
    _refresh(store, repo)
    assert deadline_plan.current_plan(store, repo.job_id)["sha"] == good
    rejected = [e for e in store.events(repo.job_id) if e.what == "deadline_plan_rejected"]
    guards = [e for e in store.events(repo.job_id) if e.what == "deadline_guardrail"]
    assert len(rejected) == 1 and rejected[0].evidence["sha"] == bad_sha and len(guards) == 1
    assert guards[0].evidence["reason"].startswith("unauthorized_drop:R1")


def test_human_revision_makes_the_plan_stale_until_a_current_rev_plan_arrives(store, repo):
    from steward.models import Judgment

    path = _path(repo)
    repo.commit({path: plan_json(repo.job_id, 1, due=DUE, now=NOW)}, "plan")
    _refresh(store, repo)
    deadline.revise(store, repo.job_id, action="set", judgment=Judgment.human, origin="cli", request_id="r2",
                    reason="later", due=deadline.parse_due("+5h", now=NOW, tz="America/Los_Angeles"))
    _refresh(store, repo)  # same HEAD, new rev: re-validated -> stale, rejected; old plan kept (not a fallback yet)
    assert deadline_plan.current_plan(store, repo.job_id)["commitment_rev"] == 1
    assert any(e.what == "deadline_guardrail" and e.evidence["reason"].startswith("stale_commitment_rev")
               for e in store.events(repo.job_id))
    repo.commit({path: plan_json(repo.job_id, 2, due=DUE + timedelta(hours=1), now=NOW)}, "plan for rev 2")
    _refresh(store, repo)
    assert deadline_plan.current_plan(store, repo.job_id)["commitment_rev"] == 2


def test_no_plan_by_window_adopts_a_labelled_fallback_once_and_work_continues(store, repo):
    later = NOW + timedelta(minutes=16)
    _refresh(store, repo, now=NOW + timedelta(minutes=5))
    assert deadline_plan.current_plan(store, repo.job_id) is None
    _refresh(store, repo, now=later)
    _refresh(store, repo, now=later + timedelta(minutes=1))
    cur = deadline_plan.current_plan(store, repo.job_id)
    assert cur["source"] == "fallback" and cur["milestones"][-1]["id"] == "candidate"
    assert cur["milestones"][-1]["target_at_utc"] <= deadline.current(store, repo.job_id)["due_at_utc"]
    assert len([e for e in store.events(repo.job_id) if e.what == "deadline_plan_missing"]) == 1
    sha = repo.commit({_path(repo): plan_json(repo.job_id, 1, due=DUE, now=NOW)}, "late plan")
    _refresh(store, repo, now=later + timedelta(minutes=2))
    assert deadline_plan.current_plan(store, repo.job_id)["sha"] == sha  # a real plan still replaces it


def test_brief_carries_the_deadline_section_only_for_a_deadline_job(store, repo):
    contract = Contract(goal="Add a CSV importer.", done_when=["tests pass"])
    text = render_brief(contract, [], None, None, deadline=deadline_plan.brief_lines(store, repo.job_id))
    assert "DEADLINE" in text and _path(repo) in text and "commitment rev 1" in text
    assert "cleanup commit" in text and "R1 [required]" in text and "O1 [optional]" in text
    other = store.create_job("plain", "/tmp")
    assert deadline_plan.brief_lines(store, other.job_id) == []
    assert "DEADLINE" not in render_brief(contract, [], None, None)
