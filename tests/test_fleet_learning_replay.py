"""Phase 29-02 (FLEET-20/21): append-only comparable outcomes, a mixed Claude/Codex recovery replay to ONE verified
candidate, and the shadow-to-active rollout gate. Real Store + temp Git; fake adapters/advisors only."""

from __future__ import annotations

import json
import sqlite3

import pytest
from test_fleet_closeout import disc
from test_fleet_integration import git
from test_fleet_lanes import JOB
from test_fleet_tech_lead import TL, ActiveRollout

from steward import fleet_closeout as fc
from steward import fleet_learning as flearn
from steward import fleet_runtime as rt
from steward import fleet_tech_lead as tl
from steward.api import StewardAPI
from steward.fleet_store import FleetStore
from steward.models import JobState, Judgment
from steward.runner_exec import SubprocessRunner

BUILD = {"id": "build-1", "digest": "sha256:aaa"}
INSTANCE = {"home": "/dev/inst", "config": "c1", "service": "svc-dev", "socket": "s1", "herdr": "h1"}


def reassign(e, pid="re-1"):
    return e.proposal([{"type": "assign", "key": "re", "target": "t1", "params": {"roster_id": "codex-gpt"}}],
                      pid=pid, rationale="claude lane stalled and regressed; try codex")


def table_counts(store):
    names = [r[0] for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {n: store._conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in sorted(names)}


def run_replay(e, monkeypatch, *, classes=("backend", "backend")):
    """Stall + regression on a Claude lane -> one stop-confirmed reassignment to Codex -> serial integration ->
    combined-SHA closeout. Returns the closeout result."""
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "auto")
    e.admit()
    arb = e.ctl.arbiter
    arb.submit(JOB, "doctor", "stall", lane="lane-t1")
    arb.submit(JOB, "doctor", "stall", lane="lane-t1")            # second stall escalates to the outer loop
    arb.submit(JOB, "trajectory", "regression", lane="lane-t1", detail="checks went red after the last commit")
    seen = []

    def advisor(packet):
        seen.append(packet)
        return reassign(e)

    sup = tl.TechLeadSupervisor(e.store, e.ctl, advisor, clock=e.clock, facts=arb)
    res = sup.on_event(JOB)
    assert res["status"] == "applied", res
    old = e.world.adapters[("lane-t1", 1)]
    assert old.stopped and e.world.alive[old.ref] is False        # stop confirmed BEFORE the replacement started
    assert e.lease("t1")["attempt_no"] == 2
    new = e.adapter("t1")
    new.usage = lambda: {"tokens": 4200, "source": "adapter-report", "confidence": "reported"}
    e.wts["t1"] = e.wts["t1"]  # same lane worktree path is reused by the replacement attempt
    e.finish("t1", {"src/a/a.py": "a\n"})
    e.finish("t2", {"src/b/b.py": "b\n"})
    e.writer.integrate(JOB)
    e.store.add_check(JOB, commit_sha=e.ctl.base_sha(), command="true", exit_code=0, results={}, label="baseline")
    for st in (JobState.CONTRACTED, JobState.RUNNING, JobState.WORKER_FINISHED):
        e.store.set_state(JOB, st, what="x", evidence={}, rule="t", judgment=Judgment.code)
    closer = fc.FleetCloseout(e.store, SubprocessRunner(), base_dir=e.writer.base_dir, base_sha=e.ctl.base_sha(),
                              discover=disc("true"))
    return closer.run(JOB), seen


# ------------------------------------------------------------------------------------------------ tracer

def test_mixed_runtime_stall_and_regression_replay_ends_in_one_combined_sha_candidate(steward_home, tmp_path,
                                                                                       monkeypatch):
    e = TL(tmp_path, steward_home)
    head_before = git(e.repo, "rev-parse", "HEAD")
    out, seen = run_replay(e, monkeypatch)
    # exactly one candidate at the verified combined SHA, waiting for the human
    cand = fc.final_candidate(e.store, JOB)
    head = git(e.writer.integration_path(JOB), "rev-parse", "HEAD")
    assert out["status"] == "candidate" and out["state"] == "NEEDS_HUMAN"
    assert len(fc.candidates(e.store, JOB)) == 1 and cand["final_sha"] == head == cand["verified_sha"]
    # exactly one bounded model-origin reassignment, history of the prior assignment kept
    assert len(e.assignments()) == 1
    rows = e.fs.assignments(JOB, "lane-t1")
    assert [(r["assignment_rev"], r["runtime"], r["actor"]) for r in rows] == [
        (1, "claude-code", "policy"), (2, "codex", "tech_lead")]
    attempts = e.store._conn.execute("SELECT attempt_no, runtime FROM fleet_attempts WHERE lane_id='lane-t1'"
                                     " ORDER BY attempt_no").fetchall()
    assert [(a["attempt_no"], a["runtime"]) for a in attempts] == [(1, "claude-code"), (2, "codex")]
    assert len(e.fs.proposals(JOB)) == 1
    # the model's rationale is advice on a receipt, not evidence of success: the candidate came from verification
    prop = e.fs.proposal(JOB, "re-1")
    assert prop["outcome"] == "applied" and cand["checks"] == [{"command": "true", "exit_code": 0, "passed": True}]
    assert any(ev["kind"] == "repeated_stall" for ev in seen[0]["events"])
    # the source checkout never moved
    assert git(e.repo, "rev-parse", "HEAD") == head_before and git(e.repo, "status", "--porcelain") == ""


def test_outcomes_bind_runtime_attempt_and_exact_shas_and_keep_unknowns_null(steward_home, tmp_path, monkeypatch):
    e = TL(tmp_path, steward_home)
    run_replay(e, monkeypatch)
    new = flearn.record_job_outcomes(e.store, JOB, task_classes={"t1": "backend", "t2": "backend"})
    by = {(o["lane_id"], o["attempt_no"]): o for o in new}
    assert set(by) == {("lane-t1", 1), ("lane-t1", 2), ("lane-t2", 1)}
    first, second, other = by[("lane-t1", 1)], by[("lane-t1", 2)], by[("lane-t2", 1)]
    # replaced Claude attempt: no candidate, never integrated, stopped for the reassignment
    assert (first["runtime"], first["model"]) == ("claude-code", "m1")
    assert first["candidate_sha"] is None and first["integration_sha"] is None
    assert first["recovery"]["reassigned"] is True and first["rework"] == 0
    # the Codex replacement: exact candidate + integration SHA, reported usage with provenance
    cand = next(c for c in e.fs.candidates(JOB) if c["lane_id"] == "lane-t1")
    integ = e.fs.integrated_tasks(JOB)["t1"]
    assert (second["runtime"], second["model"], second["assignment_rev"]) == ("codex", "m2", 2)
    assert second["candidate_sha"] == cand["candidate_sha"] and second["integration_sha"] == integ
    assert second["recovery"]["replacement_attempt"] is True and second["rework"] == 1
    assert (second["tokens"], second["usage_source"], second["usage_confidence"]) == (4200, "adapter-report", "reported")
    assert second["graph_rev"] == 1 and second["registry_hash"] == e.snap["registry_hash"]
    assert second["task_class"] == "backend" and second["repository"] == str(e.repo)
    # nothing observed -> null, never 0
    assert other["tokens"] is None and other["cost_usd"] is None and other["usage_source"] == "unknown"
    assert second["cost_usd"] is None
    assert first["evidence_ref"] is None and second["evidence_ref"] == "ev:1"
    # idempotent: a second sweep adds nothing
    assert flearn.record_job_outcomes(e.store, JOB) == []
    assert len(e.fs.outcomes(JOB)) == 3


def test_outcome_history_is_insert_only_and_rejects_mismatched_or_duplicate_bindings(steward_home, tmp_path,
                                                                                      monkeypatch):
    e = TL(tmp_path, steward_home)
    run_replay(e, monkeypatch)
    flearn.record_job_outcomes(e.store, JOB)
    with pytest.raises(sqlite3.DatabaseError, match="insert-only"):
        with e.store._conn:
            e.store._conn.execute("UPDATE fleet_outcomes SET tokens=1")
    with pytest.raises(sqlite3.DatabaseError, match="insert-only"):
        with e.store._conn:
            e.store._conn.execute("DELETE FROM fleet_outcomes")
    # duplicate with the same bindings returns the stored row; a differing binding is rejected, never an update
    c2, i2 = e.fs.latest_candidate(JOB, "t2")["candidate_sha"], e.fs.integrated_tasks(JOB)["t2"]
    row, fresh = e.fs.record_outcome(JOB, "lane-t2", 1, task_id="t2", candidate_sha=c2, integration_sha=i2)
    assert fresh is False and row["integration_sha"] == i2
    with pytest.raises(ValueError, match="outcome_binding_mismatch: duplicate"):  # fewer bindings than the record
        e.fs.record_outcome(JOB, "lane-t2", 1, task_id="t2")
    with pytest.raises(ValueError, match="outcome_binding_mismatch"):
        e.fs.record_outcome(JOB, "lane-t2", 1, task_id="t2",
                            candidate_sha=e.fs.latest_candidate(JOB, "t2")["candidate_sha"],
                            integration_sha="0" * 40)
    with pytest.raises(ValueError, match="outcome_binding_mismatch"):  # a SHA that is not this attempt's candidate
        e.fs.record_outcome(JOB, "lane-t1", 1, task_id="t1", candidate_sha="f" * 40)
    with pytest.raises(ValueError, match="outcome_unknown_attempt"):
        e.fs.record_outcome(JOB, "lane-t1", 9, task_id="t1")
    with pytest.raises(ValueError, match="outcome_bad_measurement"):
        e.fs.record_outcome(JOB, "lane-t1", 1, task_id="t1", tokens=-5)
    with pytest.raises(ValueError, match="outcome_binding_mismatch: task"):
        e.fs.record_outcome(JOB, "lane-t1", 1, task_id="t2")


def test_unsettled_attempts_are_not_guessed(steward_home, tmp_path):
    e = TL(tmp_path, steward_home)
    e.admit()                                 # both lanes still running
    assert flearn.record_job_outcomes(e.store, JOB) == []
    sha = e.finish("t1", {"src/a/a.py": "a\n"})   # lane stopped, candidate admitted but not yet integrated
    assert sha and flearn.record_job_outcomes(e.store, JOB) == []
    e.writer.integrate(JOB)
    got = flearn.record_job_outcomes(e.store, JOB)
    assert [(o["lane_id"], o["integration_sha"] is not None) for o in got] == [("lane-t1", True)]


def test_human_outcome_binds_to_the_exact_final_sha_once(steward_home, tmp_path, monkeypatch):
    e = TL(tmp_path, steward_home)
    run_replay(e, monkeypatch)
    final = fc.final_candidate(e.store, JOB)["final_sha"]
    with pytest.raises(ValueError, match="human_outcome_binding_mismatch"):
        e.fs.record_human_outcome(JOB, final_sha="e" * 40, verdict="accepted", actor="sam")
    with pytest.raises(ValueError, match="bad_verdict"):
        e.fs.record_human_outcome(JOB, final_sha=final, verdict="great", actor="sam")
    row, fresh = e.fs.record_human_outcome(JOB, final_sha=final, verdict="accepted", actor="sam", note="ok")
    assert fresh and row["final_sha"] == final
    again, fresh2 = e.fs.record_human_outcome(JOB, final_sha=final, verdict="accepted", actor="sam")
    assert not fresh2 and again["seq"] == row["seq"]
    with pytest.raises(ValueError, match="human_outcome_binding_mismatch: duplicate"):
        e.fs.record_human_outcome(JOB, final_sha=final, verdict="rejected", actor="sam")
    with pytest.raises(sqlite3.DatabaseError, match="insert-only"):
        with e.store._conn:
            e.store._conn.execute("UPDATE fleet_human_outcomes SET verdict='rejected'")


# ------------------------------------------------------------------------------------------------ comparison

def test_comparison_is_read_only_scoped_to_matching_dimensions_and_reports_missingness(steward_home, tmp_path,
                                                                                         monkeypatch):
    e = TL(tmp_path, steward_home)
    run_replay(e, monkeypatch)
    flearn.record_job_outcomes(e.store, JOB, task_classes={"t1": "backend", "t2": "docs"})
    final = fc.final_candidate(e.store, JOB)["final_sha"]
    e.fs.record_human_outcome(JOB, final_sha=final, verdict="accepted", actor="sam")
    api = StewardAPI(e.store)
    before, seq = table_counts(e.store), e.store._conn.execute("SELECT MAX(seq) FROM fleet_events").fetchone()[0]
    ev_seq = e.store._conn.execute("SELECT MAX(seq) FROM events").fetchone()[0]
    view = api.get_outcome_comparison(repository=str(e.repo), task_class="backend")
    assert table_counts(e.store) == before                      # no row written anywhere
    assert e.store._conn.execute("SELECT MAX(seq) FROM fleet_events").fetchone()[0] == seq
    assert e.store._conn.execute("SELECT MAX(seq) FROM events").fetchone()[0] == ev_seq
    assert json.dumps(view)                                     # plain data
    # only the t1 (backend) attempts match: one Claude and one Codex sample; the docs task is excluded, not mixed in
    assert view["dimensions"] == {"repository": str(e.repo), "task_class": "backend"}
    assert view["matching"] == 2 and view["excluded_other_dimensions"] == 1
    groups = {g["runtime"]: g for g in view["groups"]}
    assert set(groups) == {"claude-code", "codex"}
    cl, cx = groups["claude-code"], groups["codex"]
    assert cl["sample_count"] == cx["sample_count"] == 1
    assert cx["tokens_total"] == 4200 and cl["tokens_total"] is None            # unknown stays unknown
    assert cx["cost_usd_total"] is None and cx["missing"]["cost_usd"] == 1
    # claude sample: tokens + cost unknown (duration known); codex: cost unknown -> exact missing-data rates
    assert cl["missing"]["tokens"] == 1 and cl["missing_data_rate"] == round(2 / 3, 4)
    assert cx["missing_data_rate"] == round(1 / 3, 4)
    assert cx["human_outcomes"] == {"accepted": 1} and cl["human_outcomes"] == {"none": 1}
    assert cl["stopped_for_reassignment"] == 1 and cx["recovered_replacements"] == 1
    # descriptive only: no ranking, score, winner or recommendation anywhere in the payload
    text = json.dumps(view).lower()
    for banned in ("rank", "score", "best", "winner", "recommend", "leaderboard", "prefer"):
        assert banned not in text.replace("not a ranking, recommendation", "")
    assert [g["runtime"] for g in view["groups"]] == sorted(g["runtime"] for g in view["groups"])
    assert view["notice"].startswith("descriptive only")


def test_comparison_needs_explicit_dimensions_and_never_guesses_unclassified(steward_home, tmp_path, monkeypatch):
    e = TL(tmp_path, steward_home)
    run_replay(e, monkeypatch)
    flearn.record_job_outcomes(e.store, JOB)                    # no classification supplied -> task_class NULL
    api = StewardAPI(e.store)
    with pytest.raises(ValueError, match="explicit repository and task_class"):
        api.get_outcome_comparison(repository=str(e.repo), task_class="")
    view = api.get_outcome_comparison(repository=str(e.repo), task_class="backend")
    assert view["matching"] == 0 and view["groups"] == [] and view["excluded_unclassified"] == 3
    other = api.get_outcome_comparison(repository="/some/other/repo", task_class="backend")
    assert other["considered"] == 3 and other["matching"] == 0 and other["excluded_unclassified"] == 0


def test_model_claims_stay_advice_and_cannot_create_a_candidate(steward_home, tmp_path, monkeypatch):
    """A worker/advisor saying 'done and verified' produces no candidate; only the closeout gate does."""
    e = TL(tmp_path, steward_home)
    e.admit()
    res = e.apply([{"type": "owner_decision", "key": "d", "params": {"question": "Is it verified? yes!",
                                                                     "options": ["yes", "no"]}}],
                  rationale="all tasks verified, ship it")
    assert res["outcome"] == "applied"
    assert fc.final_candidate(e.store, JOB) is None and fc.candidates(e.store, JOB) == []
    ev = [x for x in e.fs.fleet_events(JOB) if x["what"] == "tech_lead_proposal_applied"][0]
    assert ev["judgment"] == Judgment.model_informed_code_authorized.value


# ================================================================================================ rollout gate
# (Phase 29-02 task 2) shadow by default; trial and active are separate, evidence-derived, kill-switch-fenced.

INST = dict(INSTANCE)


def gated(tmp_path, home, *, build=BUILD, inst=None, probe=True, **kw):
    tmp_path.mkdir(parents=True, exist_ok=True)
    e = TL(tmp_path, home, **kw)
    e.admit()
    e.inst = dict(INSTANCE) if inst is None else inst
    e.gate = rt.RolloutGate(e.store, build=build, instance_probe=(lambda: dict(e.inst)) if probe else None,
                            clock=e.clock)
    e.ctl.rollout = e.gate
    return e


def seed_active_evidence(g, *, skip=(), build=None):
    if "false_action_review" not in skip:
        assert g.record_false_action_review(reviewed_actions=50, false_actions=1, reviewer="sam",
                                            build=build)["passed"]
    if "kill_switch_exercise" not in skip:
        assert g.exercise_kill_switch(actor="sam", build=build)["passed"]
    if "trial_receipt" not in skip:
        b = build or g.build
        g.record_trial_preflight(job_id="trial-job", instance=INSTANCE, isolated=True, low_risk=True,
                                 disposable=True, ttl_s=600, max_actions=1, build=b)
        assert g.record_trial_receipt(job_id="trial-job", passed=True, source=g.TRIAL_SOURCE, build=b)["passed"]
    if "active_owner_approval" not in skip:
        g.record_owner_approval(purpose="active", actor="sam", build=build)


def note(e, key="n1", pid=None):
    return e.apply([{"type": "owner_decision", "key": key, "params": {"question": "which way?",
                                                                       "options": ["a", "b"]}}], pid=pid)


def preflight(g, e, *, max_actions=2, ttl_s=600, job=JOB):
    return g.record_trial_preflight(job_id=job, instance=e.inst, isolated=True, low_risk=True, disposable=True,
                                    ttl_s=ttl_s, max_actions=max_actions)


def test_default_is_shadow_and_a_proposal_takes_no_lane_action(steward_home, tmp_path):
    e = TL(tmp_path, steward_home)
    e.admit()
    e.ctl.rollout = rt.RolloutGate(e.store, clock=e.clock)       # the controller's own default gate, no evidence
    assert e.ctl.rollout.requested_mode() == "shadow"
    res = e.apply([steer_for("t1")])
    assert res["outcome"] == "shadowed" and res["reason"] == "shadow_mode"
    assert res["actions"][0]["status"] == "shadowed" and e.deliveries() == 0 and e.assignments() == []
    ev = [x for x in e.fs.fleet_events(JOB) if x["what"] == "tech_lead_proposal_shadowed"]
    assert len(ev) == 1 and ev[0]["judgment"] == "code"          # nothing model-informed was authorized
    assert rt.JobController(e.store, e.repo, SubprocessRunner(), e.world.factory,
                            base_dir=tmp_path / "w2", probe=e.world.probe).rollout.authorize(JOB, 1)["mode"] == "shadow"


def steer_for(task_id):
    return {"type": "steer", "key": "a1", "target": task_id, "params": {"verb": "focus", "text": "look at importer"}}


def test_requesting_active_without_every_piece_of_evidence_is_rejected_and_changes_nothing(steward_home, tmp_path):
    e = gated(tmp_path, steward_home)
    g = e.gate
    r = g.request_mode("active", actor="sam")
    assert r["accepted"] is False and r["mode"] == "shadow"
    assert {x.removeprefix("missing:") for x in r["reasons"]} == set(g.ACTIVE_REQUIRES)
    assert e.apply([steer_for("t1")])["outcome"] == "shadowed" and e.deliveries() == 0
    # each missing piece blocks on its own
    for skip in g.ACTIVE_REQUIRES:
        e2 = gated(tmp_path / skip, steward_home / skip)
        seed_active_evidence(e2.gate, skip=(skip,))
        out = e2.gate.request_mode("active", actor="sam")
        assert out["accepted"] is False and out["reasons"] == [f"missing:{skip}"]
        assert e2.apply([steer_for("t1")])["outcome"] == "shadowed" and e2.deliveries() == 0


def test_false_action_review_must_be_measured_and_under_the_bar(steward_home, tmp_path):
    g = gated(tmp_path, steward_home).gate
    assert g.record_false_action_review(reviewed_actions=5, false_actions=0, reviewer="c")["passed"] is False  # too few
    assert g.record_false_action_review(reviewed_actions=20, false_actions=2, reviewer="c")["passed"] is False  # 10%
    with pytest.raises(ValueError, match="measured counts"):
        g.record_false_action_review(reviewed_actions=3, false_actions=9, reviewer="c")
    with pytest.raises(ValueError, match="measured counts"):
        g.record_false_action_review(reviewed_actions=None, false_actions=0, reviewer="c")
    assert g.eligibility()["evidence"]["false_action_review"] is False
    assert g.record_false_action_review(reviewed_actions=100, false_actions=1, reviewer="c")["passed"] is True
    assert g.eligibility()["evidence"]["false_action_review"] is True
    # a later failing review withdraws it (latest evidence wins; history is kept)
    g.record_false_action_review(reviewed_actions=100, false_actions=9, reviewer="c")
    assert g.eligibility()["evidence"]["false_action_review"] is False


def test_replay_and_nonisolated_trials_cannot_satisfy_the_trial_receipt(steward_home, tmp_path):
    g = gated(tmp_path, steward_home).gate
    with pytest.raises(ValueError, match="isolated, low-risk and disposable"):
        g.record_trial_preflight(job_id="j", instance=INSTANCE, isolated=False, low_risk=True, disposable=True,
                                 ttl_s=60, max_actions=1)
    with pytest.raises(ValueError, match="isolated, low-risk and disposable"):
        g.record_trial_preflight(job_id="j", instance=INSTANCE, isolated=True, low_risk=False, disposable=True,
                                 ttl_s=60, max_actions=1)
    with pytest.raises(ValueError, match="out of bounds"):
        g.record_trial_preflight(job_id="j", instance=INSTANCE, isolated=True, low_risk=True, disposable=True,
                                 ttl_s=86400, max_actions=1)
    with pytest.raises(ValueError, match="out of bounds"):
        g.record_trial_preflight(job_id="j", instance=INSTANCE, isolated=True, low_risk=True, disposable=True,
                                 ttl_s=60, max_actions=50)
    # no preflight at all -> a "passed" receipt does not count
    assert g.record_trial_receipt(job_id="j", passed=True, source=g.TRIAL_SOURCE)["passed"] is False
    g.record_trial_preflight(job_id="j", instance=INSTANCE, isolated=True, low_risk=True, disposable=True,
                             ttl_s=60, max_actions=1)
    assert g.record_trial_receipt(job_id="j", passed=True, source="replay")["passed"] is False   # replay is not it
    assert g.eligibility()["evidence"]["trial_receipt"] is False
    assert g.record_trial_receipt(job_id="j", passed=False, source=g.TRIAL_SOURCE)["passed"] is False
    assert g.record_trial_receipt(job_id="j", passed=True, source=g.TRIAL_SOURCE)["passed"] is True
    assert g.eligibility()["evidence"]["trial_receipt"] is True


def test_evidence_is_bound_to_the_exact_build_and_owner_approval_to_the_identical_build(steward_home, tmp_path):
    e = gated(tmp_path, steward_home)
    g = e.gate
    other = {"id": "build-2", "digest": "sha256:bbb"}
    seed_active_evidence(g, skip=("active_owner_approval",))
    g.record_owner_approval(purpose="active", actor="sam", build=other)  # approving a different build
    assert g.request_mode("active", actor="c")["reasons"] == ["missing:active_owner_approval"]
    g.record_owner_approval(purpose="active", actor="sam", build={"id": "build-1", "digest": "sha256:zzz"})
    assert g.request_mode("active", actor="c")["accepted"] is False
    g.record_owner_approval(purpose="active", actor="sam")
    assert g.request_mode("active", actor="c")["accepted"] is True
    assert e.apply([steer_for("t1")])["outcome"] == "applied"
    # a new running build inherits none of it
    g2 = rt.RolloutGate(e.store, build=other, clock=e.clock)
    assert g2.eligibility()["eligible"] is False and "false_action_review" in g2.eligibility()["missing"]
    assert g2.authorize(JOB, 1)["mode"] != "active"
    with pytest.raises(ValueError, match="exact build"):
        rt.RolloutGate(e.store).record_owner_approval(purpose="active", actor="sam")  # no build, no approval


def test_exercised_kill_switch_is_observed_and_prior_state_restored(steward_home, tmp_path):
    g = gated(tmp_path, steward_home).gate
    assert g.kill_switch_active() is False
    assert g.exercise_kill_switch(actor="sam") == {"passed": True}
    assert g.kill_switch_active() is False                                  # restored
    rows = g.fleet.rollout_evidence("kill_switch")
    assert [r["payload"]["active"] for r in rows] == [True, False]          # flipped, then restored
    ex = g.fleet.rollout_evidence("kill_switch_exercise")[-1]
    assert ex["payload"]["passed"] and ex["build_id"] == "build-1"
    g.set_kill_switch(True, actor="sam")
    assert g.exercise_kill_switch(actor="sam")["passed"] and g.kill_switch_active() is True   # stays on


def test_trial_authority_is_capped_expiring_and_scoped_to_the_exact_job_and_build(steward_home, tmp_path):
    e = gated(tmp_path, steward_home)
    g = e.gate
    assert note(e, "x0")["outcome"] == "shadowed"                           # no preflight yet
    preflight(g, e, max_actions=2)
    assert g.authorize("other-job", 1)["mode"] == "shadow"                  # exact disposable job only
    res = note(e, "n1")
    assert res["outcome"] == "applied" and g.trial_authority(JOB)["remaining"] == 1
    assert e.fs.proposal(JOB, res["proposal_id"])["bindings"]["observed"]["graph_rev"] == 1
    # a proposal bigger than the remaining cap is refused whole
    two = e.apply([{"type": "owner_decision", "key": "k1", "params": {"question": "q?", "options": ["a", "b"]}},
                   {"type": "owner_decision", "key": "k2", "params": {"question": "r?", "options": ["a", "b"]}}])
    assert two["outcome"] == "rejected" and two["reason"] == "trial_cap_exhausted"
    assert note(e, "n2")["outcome"] == "applied"                            # the last permitted action
    spent = note(e, "n3")
    assert spent["outcome"] == "rejected" and "trial_cap_exhausted" in spent["reason"]
    assert len(e.ctl.owner_decisions(JOB)) == 2
    # trial never enabled ordinary active mode
    assert g.requested_mode() == "shadow" and g.request_mode("active", actor="c")["accepted"] is False


def test_trial_expiry_revokes_authority_for_good(steward_home, tmp_path):
    e = gated(tmp_path, steward_home)
    preflight(e.gate, e, max_actions=3, ttl_s=100)
    assert note(e, "a")["outcome"] == "applied"
    e.clock.t += 101
    res = note(e, "b")
    assert res["outcome"] == "rejected" and res["reason"] == "trial_expired"
    assert [r["payload"]["reasons"] for r in e.gate.fleet.rollout_evidence("trial_revoked")] == [["trial_expired"]]
    e.clock.t -= 101                                                         # time cannot buy it back
    assert "trial_revoked" in e.gate.trial_authority(JOB)["reasons"] and note(e, "c")["outcome"] == "rejected"


@pytest.mark.parametrize("field,value", [("home", "/elsewhere"), ("config", "c2"), ("service", "svc-prod"),
                                          ("socket", "s2"), ("herdr", "h2")])
def test_any_changed_instance_identity_revokes_trial_authority_fail_closed(steward_home, tmp_path, field, value):
    e = gated(tmp_path, steward_home)
    preflight(e.gate, e, max_actions=3)
    assert note(e, "a")["outcome"] == "applied"
    e.inst[field] = value
    res = note(e, "b")
    assert res["outcome"] == "rejected" and res["reason"] == "instance_identity_changed"
    e.inst[field] = INSTANCE[field]                                          # restoring it does not restore authority
    assert note(e, "c")["outcome"] == "rejected" and len(e.ctl.owner_decisions(JOB)) == 1


def test_trial_without_a_verifiable_instance_or_after_a_changed_build_gets_nothing(steward_home, tmp_path):
    e = gated(tmp_path, steward_home, probe=False)
    preflight(e.gate, e)
    res = note(e)
    assert res["outcome"] == "rejected" and res["reason"] == "instance_unverifiable"
    e2 = gated(tmp_path / "b", steward_home / "b")
    preflight(e2.gate, e2)
    e2.ctl.rollout = rt.RolloutGate(e2.store, build={"id": "build-9", "digest": "sha256:ccc"},
                                    instance_probe=lambda: dict(e2.inst), clock=e2.clock)
    assert note(e2)["outcome"] == "shadowed"                                 # the preflight names another build


def test_completed_trial_ends_trial_authority(steward_home, tmp_path):
    e = gated(tmp_path, steward_home)
    preflight(e.gate, e, max_actions=3)
    assert note(e, "a")["outcome"] == "applied"
    e.gate.record_trial_receipt(job_id=JOB, passed=True, source=e.gate.TRIAL_SOURCE)
    res = note(e, "b")
    assert res["outcome"] == "rejected" and res["reason"] == "trial_complete"


def test_ordinary_active_when_every_gate_passes_and_kill_switch_fences_immediately(steward_home, tmp_path):
    e = gated(tmp_path, steward_home)
    g = e.gate
    seed_active_evidence(g)
    assert g.request_mode("active", actor="sam") == {"accepted": True, "mode": "active", "reasons": []}
    ok = e.apply([steer_for("t1")])
    assert ok["outcome"] == "applied" and e.deliveries() == 1
    assert g.authorize(JOB, 1)["mode"] == "active"
    g.set_kill_switch(True, actor="sam", reason="false action seen")
    before = (e.deliveries(), len(e.ctl.owner_decisions(JOB)), len(e.assignments()))
    res = note(e, "after-kill")
    assert res["outcome"] == "rejected" and res["reason"] == "kill_switch"
    assert (e.deliveries(), len(e.ctl.owner_decisions(JOB)), len(e.assignments())) == before
    # deterministic safety paths still work with the switch on
    e.clock.t += 1000
    assert e.ctl.pause(JOB, "pause-1")["state"] == "paused"
    assert e.ctl.resume(JOB, "resume-1")["state"] == "running"
    assert e.ctl.recover_lane(JOB, "lane-t2", "rec-1", reason="deterministic")["status"] == "started"
    assert e.ctl.stop(JOB, "stop-1")["state"] == "stopped"
    # switching it off restores the (still evidence-backed) active mode; the kill left the evidence intact
    g.set_kill_switch(False, actor="sam")
    assert g.authorize(JOB, 1)["mode"] == "active"


def test_kill_switch_is_enforced_before_every_action_in_a_batch(steward_home, tmp_path):
    e = gated(tmp_path, steward_home)
    seed_active_evidence(e.gate)
    assert e.gate.request_mode("active", actor="sam")["accepted"]
    e.ctl.diagnose_fn = lambda job, lane, q: e.gate.set_kill_switch(True, actor="sam") and None
    res = e.apply([{"type": "diagnose", "key": "d", "target": "t1", "params": {"question": "why slow?"}},
                   {"type": "owner_decision", "key": "o", "params": {"question": "next?", "options": ["a", "b"]}}])
    assert [a["status"] for a in res["actions"]] == ["applied", "skipped"] and res["actions"][1]["reason"] == "kill_switch"
    assert res["outcome"] == "partial" and e.ctl.owner_decisions(JOB) == []


def test_no_model_or_api_caller_can_assert_rollout_facts(steward_home, tmp_path):
    e = gated(tmp_path, steward_home)
    for forbidden in ("mode", "rollout", "approval", "kill_switch", "trial", "authority"):
        body = e.proposal([steer_for("t1")])
        body[forbidden] = "active"
        res = e.ctl.apply_proposal(JOB, body)
        assert res["outcome"] == "malformed", forbidden
    act = steer_for("t1")
    act["params"]["mode"] = "active"
    assert e.apply([act])["outcome"] == "malformed"
    assert e.gate.fleet.rollout_evidence("mode_request") == [] and e.deliveries() == 0
    api = StewardAPI(e.store)
    writers = [n for n in dir(api) if any(n.startswith(p) for p in ("record_", "set_kill", "request_mode",
                                                                     "approve_rollout", "grant_"))]
    assert writers == []


def test_api_rollout_status_is_read_only_and_explains_every_block(steward_home, tmp_path):
    e = gated(tmp_path, steward_home)
    preflight(e.gate, e, max_actions=1, ttl_s=100)
    e.inst["config"] = "changed"                                             # would be revoked on the next action
    api = StewardAPI(e.store)
    before = table_counts(e.store)
    st = api.get_rollout_status(JOB, build_id="build-1", build_digest="sha256:aaa")
    assert table_counts(e.store) == before                                   # reading never records a revocation
    assert st["requested_mode"] == "shadow" and st["kill_switch"] is False
    assert st["active_eligibility"]["missing"] == list(rt.RolloutGate.ACTIVE_REQUIRES)
    assert st["instance_verifiable"] is False and st["decision"]["allowed"] is False
    anon = api.get_rollout_status()
    assert "running_build_unknown" in anon["active_eligibility"]["missing"] and "trial" not in anon
    json.dumps(st)


def test_replay_under_a_capped_trial_authority_reaches_one_candidate_then_further_proposals_are_blocked(
        steward_home, tmp_path, monkeypatch):
    e = TL(tmp_path, steward_home)
    e.inst = dict(INSTANCE)
    e.gate = rt.RolloutGate(e.store, build=BUILD, instance_probe=lambda: dict(e.inst), clock=e.clock)
    e.ctl.rollout = e.gate
    e.admit_orig = e.admit
    preflight(e.gate, e, max_actions=1)
    out, _ = run_replay(e, monkeypatch)
    assert out["status"] == "candidate" and len(fc.candidates(e.store, JOB)) == 1
    assert e.gate.trial_authority(JOB)["remaining"] == 0
    res = note(e, "post")
    assert res["outcome"] == "rejected" and "trial_cap_exhausted" in res["reason"]
    assert len(e.assignments()) == 1
