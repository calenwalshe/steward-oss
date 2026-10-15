"""v2.2 Phase 21 Task 3 (DL-07, DL-08, DL-18): honest shadow status — evidence-capped rules, hysteresis, misses, overrun."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from deadline_fixtures import Repo, commit_deadline, plan_json
from steward import deadline, deadline_plan, deadline_status, efficiency_watch
from steward.models import JobState, Judgment
from steward.runner_exec import SubprocessRunner
from steward.store import Store

NOW = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
DUE = NOW + timedelta(hours=4)
RUN = SubprocessRunner()


def _snap(**kw):
    rec = {"rev": 1, "due_at_utc": "2026-10-03T00:00:00Z", "items": []}
    plan = {"source": "worker", "commitment_rev": 1, "plan_rev": 1, "infeasible": False, "milestones": [
        {"id": "M1", "target_at_utc": "2026-10-02T21:00:00Z", "evidence": "commit", "title": "m1", "items": []},
        {"id": "M2", "target_at_utc": "2026-10-02T22:00:00Z", "evidence": "commit", "title": "m2", "items": []},
        {"id": "candidate", "target_at_utc": "2026-10-02T23:30:00Z", "evidence": "verify", "title": "c", "items": []}]}
    base = {"job_state": "RUNNING", "commitment": rec, "start": "2026-10-02T20:00:00Z", "plan": plan,
            "facts": {"met": {}, "missed": {}}, "eta": None, "last_class": None, "class_since": None, "verdict": None,
            "prev": None}
    base.update(kw)
    return base


def _eta(source, p10, p50, p90, units_done=2, at=NOW):
    return {"source": source, "units_done": units_done, "at": at.timestamp(),
            "shown": {"p10_end": p10.timestamp(), "p50_end": p50.timestamp() if p50 else None,
                      "p90_end": p90.timestamp()}}


def _met(mid="M1", at="2026-10-02T20:50:00Z"):
    return {mid: {"met_at": at, "met_sha": "abc", "evidence": {"kind": "commit"}, "recorded_at": at, "late": False}}


def _miss(mid, target):
    return {f"1:{mid}:{target}": {"milestone_id": mid, "target_at_utc": target, "grace_min": 6.0, "gap_min": 60.0}}


# -- rules ---------------------------------------------------------------------------------------------------

def test_planless_and_no_evidence_is_at_risk_estimating_never_on_track():
    a = deadline_status.assess(_snap(plan=None), NOW)
    assert (a["status"], a["label"], a["basis"]) == ("at_risk", "At risk · estimating", "none")
    b = deadline_status.assess(_snap(), NOW + timedelta(minutes=5))  # adopted plan, still no progress evidence
    assert b["label"] == "At risk · estimating" and b["reasons"][0]["rule"] == "E0"


def test_on_track_needs_a_worker_plan_and_affirmative_progress():
    a = deadline_status.assess(_snap(facts={"met": _met(), "missed": {}}), NOW + timedelta(minutes=55))
    assert a["status"] == "on_track" and a["affirmative"] and "M1 met" in a["reasons"][0]["evidence"]
    eta = _eta("units", NOW + timedelta(hours=1), NOW + timedelta(hours=2), NOW + timedelta(hours=3))
    assert deadline_status.assess(_snap(eta=eta), NOW)["status"] == "on_track"
    fallback = _snap(facts={"met": _met(), "missed": {}})
    fallback["plan"] = {**fallback["plan"], "source": "fallback"}
    assert deadline_status.assess(fallback, NOW)["label"] == "At risk · estimating"  # fallback is no affirmation


def test_prior_grade_eta_can_only_say_at_risk_early_signal_never_unlikely_or_on_track():
    late = _eta("prior", DUE + timedelta(hours=1), None, DUE + timedelta(hours=5), units_done=0)
    a = deadline_status.assess(_snap(eta=late), NOW)
    assert (a["status"], a["basis"], a["label"]) == ("at_risk", "weak", "At risk (early signal)")
    fits = _eta("prior", NOW + timedelta(hours=1), None, NOW + timedelta(hours=2), units_done=0)
    assert deadline_status.assess(_snap(eta=fits), NOW)["label"] == "At risk · estimating"


def test_strong_evidence_makes_unlikely():
    u1 = _eta("units+trend", DUE + timedelta(minutes=10), DUE + timedelta(hours=1), DUE + timedelta(hours=2))
    a = deadline_status.assess(_snap(eta=u1), NOW)
    assert a["status"] == "unlikely" and a["reasons"][0]["rule"] == "U1"
    two = {**_miss("M1", "2026-10-02T21:00:00Z"), **_miss("M2", "2026-10-02T22:00:00Z")}
    b = deadline_status.assess(_snap(facts={"met": {}, "missed": two}), NOW + timedelta(hours=3, minutes=1))
    assert b["status"] == "unlikely" and any(r["rule"] == "U3" for r in b["reasons"])
    one = _miss("M1", "2026-10-02T21:00:00Z")
    c = deadline_status.assess(_snap(facts={"met": {}, "missed": one}), NOW + timedelta(hours=1, minutes=10))
    assert c["status"] == "at_risk" and c["reasons"][0]["rule"] == "R1"


def test_stale_eta_is_skipped_with_a_visible_basis():
    stale = _eta("units", DUE + timedelta(minutes=10), DUE, DUE, at=NOW - timedelta(minutes=20))
    a = deadline_status.assess(_snap(eta=stale, facts={"met": _met(), "missed": {}}), NOW)
    assert a["status"] == "on_track" and "milestones only: ETA stale" in a["basis"]


def test_short_window_reports_infeasibility_without_unlikely():
    rec = {"rev": 1, "due_at_utc": "2026-10-02T20:01:00Z", "items": []}
    a = deadline_status.assess(_snap(commitment=rec, plan=None), NOW)
    assert a["status"] == "at_risk" and any(r["rule"] == "R4" for r in a["reasons"])


# -- hysteresis ---------------------------------------------------------------------------------------------

def _cand(status, rev=1):
    return {"status": status, "label": deadline_status.LABEL[status], "basis": "evidence", "reasons": [],
            "affirmative": False, "phase": "running", "commitment_rev": rev, "plan_rev": 1}


def test_worsening_is_immediate_improving_is_damped_and_rate_limited():
    prev, cause = deadline_status.apply_hysteresis(None, _cand("on_track"), NOW, met_since=False)
    worse, cause = deadline_status.apply_hysteresis(prev, _cand("unlikely"), NOW + timedelta(minutes=1), met_since=False)
    assert worse["status"] == "unlikely" and cause == "worsened"
    t = NOW + timedelta(minutes=2)
    same, cause = deadline_status.apply_hysteresis(worse, _cand("at_risk"), t, met_since=False)
    assert same["status"] == "unlikely" and cause is None  # one clean assessment is not enough
    later, cause = deadline_status.apply_hysteresis(same, _cand("at_risk"), t + timedelta(minutes=11), met_since=False)
    assert later["status"] == "at_risk" and cause == "improved: two clean assessments"
    held, cause = deadline_status.apply_hysteresis(later, _cand("on_track"), t + timedelta(minutes=12), met_since=True)
    assert held["status"] == "at_risk" and cause is None  # at most one improvement per 30 min
    ok, cause = deadline_status.apply_hysteresis(held, _cand("on_track"), t + timedelta(minutes=45), met_since=True)
    assert ok["status"] == "on_track" and cause == "improved: milestone met"


def test_a_human_commitment_revision_resets_hysteresis_with_a_labelled_cause():
    prev, _ = deadline_status.apply_hysteresis(None, _cand("unlikely"), NOW, met_since=False)
    new, cause = deadline_status.apply_hysteresis(prev, _cand("on_track", rev=2), NOW + timedelta(minutes=1),
                                                  met_since=False)
    assert new["status"] == "on_track" and "commitment revised by Sam (rev 2)" == cause


# -- the sweep (the only writer) --------------------------------------------------------------------------------

@pytest.fixture
def store(steward_home):
    return Store(steward_home)


@pytest.fixture
def repo(tmp_path, store):
    r = Repo(tmp_path, store)
    commit_deadline(store, r.job_id, now=NOW)
    return r


def _sweep(store, repo, t, **kw):
    return deadline_status.sweep(store, repo.job_id, now=t, runner=RUN, worktree=repo.wt, **kw)


def _whats(store, repo, what):
    return [e for e in store.events(repo.job_id) if e.what == what]


def test_target_grace_miss_and_late_completion_are_separate_facts(store, repo):
    m1 = NOW + timedelta(hours=1)
    repo.commit({deadline_plan.plan_path(repo.job_id): plan_json(repo.job_id, 1, due=DUE, now=NOW, m1_target=m1)},
                "plan", when=NOW + timedelta(minutes=2))
    _sweep(store, repo, NOW + timedelta(minutes=3))
    assert deadline_status.project(store, repo.job_id, NOW + timedelta(minutes=3))["sign"] == "At risk · estimating"
    _sweep(store, repo, m1 + timedelta(minutes=1), force=True)  # inside grace: no miss yet
    assert _whats(store, repo, "deadline_milestone_missed") == []
    _sweep(store, repo, m1 + timedelta(minutes=7), force=True)  # past target + grace (max(5, 10 % of 60) = 6 min)
    _sweep(store, repo, m1 + timedelta(minutes=8), force=True)
    assert len(_whats(store, repo, "deadline_milestone_missed")) == 1
    assert deadline_status.project(store, repo.job_id, m1 + timedelta(minutes=8))["status"] == "at_risk"
    late = m1 + timedelta(minutes=20)
    repo.commit({"importer.py": "x\n"}, "parser", when=late)
    _sweep(store, repo, late + timedelta(minutes=1), force=True)
    met = _whats(store, repo, "deadline_milestone_met")
    assert len(met) == 1 and met[0].evidence["late"] is True and met[0].evidence["met_at"] == "2026-10-02T21:20:00Z"
    assert len(_whats(store, repo, "deadline_milestone_missed")) == 1  # completion never erases the miss


def test_just_in_time_commit_inside_grace_is_met_not_missed(store, repo):
    m1 = NOW + timedelta(hours=1)
    repo.commit({deadline_plan.plan_path(repo.job_id): plan_json(repo.job_id, 1, due=DUE, now=NOW, m1_target=m1)},
                "plan", when=NOW + timedelta(minutes=2))
    _sweep(store, repo, NOW + timedelta(minutes=3))
    repo.commit({"importer.py": "x\n"}, "parser", when=m1 + timedelta(minutes=4))
    _sweep(store, repo, m1 + timedelta(minutes=30), force=True)
    assert _whats(store, repo, "deadline_milestone_missed") == []
    (met,) = _whats(store, repo, "deadline_milestone_met")
    assert met.evidence["late"] is False
    p = deadline_status.project(store, repo.job_id, m1 + timedelta(minutes=30))
    assert p["status"] == "on_track" and p["sign"] == "On track"


def test_after_due_the_banner_counts_up_and_due_time_never_moves(store, repo):
    _sweep(store, repo, DUE + timedelta(minutes=12), force=True)
    _sweep(store, repo, DUE + timedelta(minutes=40), force=True)
    assert len(_whats(store, repo, "deadline_passed")) == 1
    p = deadline_status.project(store, repo.job_id, DUE + timedelta(minutes=65))
    assert p["banner"] == "Deadline passed · 1h05 over" and p["due_at_utc"] == "2026-10-03T00:00:00Z"
    assert deadline.current(store, repo.job_id)["due_at_utc"] == "2026-10-03T00:00:00Z"


def test_status_change_events_carry_rule_evidence_and_code_judgment(store, repo):
    _sweep(store, repo, NOW + timedelta(minutes=1))
    (ev,) = _whats(store, repo, "deadline_status_changed")
    assert ev.judgment == Judgment.code and ev.evidence["to"] == "at_risk" and ev.evidence["reasons"][0]["rule"] == "E0"
    assert ev.evidence["commitment_rev"] == 1 and ev.evidence["cause"] == "initial"
    deadline.revise(store, repo.job_id, action="set", judgment=Judgment.human, origin="cli", request_id="r2",
                    reason="later", due=deadline.parse_due("+6h", now=NOW, tz="America/Los_Angeles"))
    _sweep(store, repo, NOW + timedelta(minutes=2))
    last = _whats(store, repo, "deadline_status_changed")[-1]
    assert last.evidence["commitment_rev"] == 2 and "commitment revised by" in last.evidence["cause"]


def test_supervisor_exception_is_bounded_into_deadline_error(store, repo, monkeypatch):
    monkeypatch.setattr(deadline_plan, "refresh", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    for _ in range(3):
        assert _sweep(store, repo, NOW, force=True) is None
    errs = _whats(store, repo, "deadline_error")
    assert len(errs) == 1 and "boom" in errs[0].evidence["error"]


def test_mode_off_does_nothing_but_keeps_the_merge_gate(store, repo):
    deadline.set_mode(store, repo.job_id, "off", judgment=Judgment.human, origin="cli", request_id="m")
    before = len(store.events(repo.job_id))
    assert _sweep(store, repo, NOW + timedelta(hours=2), force=True) is None
    assert len(store.events(repo.job_id)) == before and deadline.merge_protected(store, repo.job_id)


def test_eta_state_is_byte_identical_with_deadline_supervision_on(store, repo):
    eta = {"v": 1, "at": NOW.timestamp(), "source": "units", "units_done": 2,
           "shown": {"p10_end": 1.0, "p50_end": 2.0, "p90_end": 3.0}}
    store.kv_set(repo.job_id, efficiency_watch.STATE_KEY, {"eta": eta, "other": [1, 2]})
    before = json.dumps(store.kv_get(repo.job_id, efficiency_watch.STATE_KEY), sort_keys=True)
    for minutes in (1, 30, 90, 300):
        _sweep(store, repo, NOW + timedelta(minutes=minutes), force=True)
    assert json.dumps(store.kv_get(repo.job_id, efficiency_watch.STATE_KEY), sort_keys=True) == before


def test_control_pane_sweep_covers_paused_and_waiting_jobs(store, repo):
    from deadline_fixtures import set_state

    set_state(store, repo.job_id, JobState.PAUSED)
    assert deadline_status.sweep_all(store, RUN, now=DUE + timedelta(minutes=1)) == 1
    assert len(_whats(store, repo, "deadline_passed")) == 1


# -- replay through the real runner tick and closeout (deadline_replay) ---------------------------------------

def test_replay_on_time_run_plans_meets_and_ends_candidate_ready_waiting_for_sam(steward_home, tmp_path):
    from deadline_replay import Replay

    r = Replay(steward_home, tmp_path)
    assert "DEADLINE" in r.worker.started_brief and "commitment rev 1" in r.worker.started_brief
    r.at(1).tick()
    assert r.project()["sign"] == "At risk · estimating"
    r.at(5).plan(m1=60, cand=200)
    r.at(6).tick()
    assert r.project()["plan"]["source"] == "worker"
    r.at(40).implement()
    r.at(61).tick()
    assert r.whats("deadline_milestone_missed") == [] and len(r.whats("deadline_milestone_met")) == 1
    r.at(75).tick()
    assert r.project()["status"] == "on_track"
    r.at(120).cleanup()
    report = r.at(121).finish()
    assert report.action == "closeout" and r.state() == JobState.NEEDS_HUMAN
    assert r.store.events(r.job_id)[-1].what in ("deadline_candidate_ready", "approval_opened") or \
        any(e.what == "deadline_candidate_ready" for e in r.store.events(r.job_id))
    verdict = r.project()["candidate"]
    assert verdict["ready"] and [i["outcome"] for i in verdict["items"] if i["item"] == "R1"] == ["proven"]


def test_replay_item_omission_never_reaches_ready(steward_home, tmp_path):
    from deadline_replay import Replay

    r = Replay(steward_home, tmp_path)
    r.at(5).plan(m1=60, cand=200)
    r.at(6).tick()
    r.at(30).commit({"tests/test_importer.py": "def test_rows():\n    assert True\n"}, "tests only, no importer")
    r.at(90).cleanup()
    r.at(91).finish()
    assert r.state() == JobState.NEEDS_HUMAN
    assert not any(e.what == "deadline_candidate_ready" for e in r.store.events(r.job_id))
    assert r.project()["candidate"]["ready"] is False


def test_replay_plan_revision_keeps_both_targets_and_control_sweep_covers_a_paused_job(steward_home, tmp_path):
    from deadline_replay import Replay
    from steward.control import Controller
    from steward.models import ControlRequest

    r = Replay(steward_home, tmp_path)
    r.at(5).plan(m1=60, cand=200)
    r.at(6).tick()
    r.at(50).plan(m1=90, cand=200)
    r.at(51).tick()
    m1 = [s["target_at_utc"] for s in r.project()["slots"] if s["milestone_id"] == "M1"]
    assert m1 == ["2026-10-02T21:00:00Z", "2026-10-02T21:30:00Z"]
    Controller(r.store).submit(ControlRequest(request_id="p", job_id=r.job_id, kind="pause"))
    r.at(101).control()  # past 21:30 + 9 min grace; no runner progress while paused: the control pane still records the miss
    assert len(r.whats("deadline_milestone_missed")) == 1
