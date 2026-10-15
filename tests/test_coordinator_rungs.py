"""Code rungs (spec 5.13 / 6.1 / 6.3): trigger + evidence -> whitelisted verbs, tries recorded before acting."""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from steward import approvals
from steward.coordinator import db, reruns, rungs, triggers, verbs
from steward.coordinator.config import Settings
from steward.models import JobState, Judgment
from steward.store import Store

S = Settings()
H = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"   # head (verification) sha
B = "0badc0de11223344556677889900aabbccddeeff"   # baseline sha
IDS = ["tests/test_health.py::test_a", "tests/test_launch.py::test_b", "tests/test_x.py::test_c",
       "tests/test_y.py::test_d"]
DEAD = lambda _j: False  # noqa: E731
ALIVE = lambda _j: True  # noqa: E731


@pytest.fixture(autouse=True)
def _coord_state(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_COORDINATOR_STATE", str(tmp_path / "coord"))


def _to(store, jid, state, what="t", rule="x", evidence=None, judgment=Judgment.code):
    store.set_state(jid, state, what=what, evidence=evidence or {}, rule=rule, judgment=judgment)


def _running(store, goal="ZT phases 1+2", repo="/tmp/repo"):
    j = store.create_job(goal, repo)
    _to(store, j.job_id, JobState.CONTRACTED, "contracted", "contract")
    _to(store, j.job_id, JobState.RUNNING, "launched", "launch")
    return j


def _five_b(steward_home, *, ruled=H, approval=True, rule="recovery_budget_exhausted"):
    """5b165868: RUNNING -> red verification at H -> last_ruled_sha -> recovery park + annotated general approval."""
    store = Store()
    j = _running(store)
    store.add_check(j.job_id, commit_sha=B, command="pytest", exit_code=0, results={}, label="baseline")
    store.append_event(j.job_id, what="verification recorded",
                       evidence={"commit_sha": H, "new_failures": IDS, "fixed": [], "preexisting_failures": []},
                       rule="verify_at_exact_commit", judgment=Judgment.code)
    if ruled:
        store.kv_set(j.job_id, "last_ruled_sha", ruled)
    _to(store, j.job_id, JobState.NEEDS_HUMAN, "recovery escalated to human", rule)
    rec = None
    if approval:
        rec = approvals.open_approval(store, j.job_id, "recovery budget exhausted; worker exited and cannot be replaced")
        approvals.annotate(store, j.job_id, rec["id"], recovery_rule="recovery_budget_exhausted")
    return store, j, rec


def _ro(home):
    return triggers.RoStore(home / "steward.db", home)


def _episode(tmp_path, key, kind="needs_human:recovery_budget_exhausted", name="c.db", detail=None):
    cdb = db.CoordDB(tmp_path / name)
    cdb.open_episode(key, kind, 1, now=time.time())
    job_id = None if key.startswith("health:") else key
    return cdb, cdb.open_for(key), triggers.Trigger(kind, job_id, 1, detail or {})


def _rerun(outcome, calls=None):
    def fn(**kw):
        if calls is not None:
            calls.append(kw)
        ids = kw["ids"]
        ok = {i: "passed" for i in ids}
        bad = {i: "failed" for i in ids}
        hr, live, br = {"env_dependent": ([ok, ok], ok, []), "flaky": ([ok, bad], bad, []),
                        "env_live_red": ([ok, ok], bad, []), "preexisting": ([bad, bad], bad, [bad, bad]),
                        "real": ([bad, bad], bad, [ok, ok]), "inconclusive": ([bad, bad], bad, [ok, bad])}[outcome]
        return reruns.RerunResult(outcome, kw["head_sha"], kw["base_sha"], ids, list(ids), hr, live, br,
                                  ["head hermetic 1: pytest"], [1.0], f"{outcome}: fake")
    return fn


def _select(ro, cdb, ep, trig, **kw):
    kw.setdefault("settings", S)
    kw.setdefault("now", time.time())
    kw.setdefault("runner_alive", DEAD)
    kw.setdefault("watchdog_active", False)
    kw.setdefault("workspace", "w1")
    return rungs.select(ro, cdb, ep, trig, **kw)


# ---------- false_red ----------

def test_false_red_env_dependent_approves_then_nudges_with_empty_commit(steward_home, tmp_path):
    store, j, rec = _five_b(steward_home)
    cdb, ep, trig = _episode(tmp_path, j.job_id)
    calls = []
    plan = _select(_ro(steward_home), cdb, ep, trig, rerun_fn=_rerun("env_dependent", calls))
    assert plan.rung == "false_red" and plan.classification == "env_dependent"
    assert [v.argv[0] for v in plan.verbs] == ["approve", "steer"]
    assert plan.verbs[0].argv == ["approve", j.job_id, rec["id"]]
    steer = plan.verbs[1].argv
    assert steer[:2] == ["steer", j.job_id] and steer[3:] == ["--verb", "nudge"]
    assert H[:8] in steer[2] and "git commit --allow-empty" in steer[2]
    assert f"coordinator {ep['episode_id'][:8]}" in steer[2] and "The 4 failing" in steer[2]
    assert ", ".join(IDS[:3]) in steer[2] and IDS[3] not in steer[2]
    assert plan.rule == rungs.RULE_FALSE_RED.format(ids=", ".join(IDS))
    assert plan.evidence["head_sha"] == H and plan.evidence["base_sha"] == B and plan.evidence["head_runs"]
    assert cdb.try_count(j.job_id, "false_red", now=time.time()) == 1
    # rerun inputs: head/base/ids/repo and a per-episode scratch under STEWARD_COORDINATOR_STATE
    kw = calls[0]
    assert kw["head_sha"] == H and kw["base_sha"] == B and kw["ids"] == IDS and str(kw["repo"]) == "/tmp/repo"
    assert kw["scratch"] == tmp_path / "coord" / "scratch" / ep["episode_id"]
    for v in plan.verbs:
        assert verbs.check_command(j.job_id, v.argv, workspace="w1") is None


def test_false_red_flaky_also_resubmits(steward_home, tmp_path):
    store, j, rec = _five_b(steward_home)
    cdb, ep, trig = _episode(tmp_path, j.job_id)
    plan = _select(_ro(steward_home), cdb, ep, trig, rerun_fn=_rerun("flaky"))
    assert plan.classification == "flaky" and [v.argv[0] for v in plan.verbs] == ["approve", "steer"]
    assert plan.rule.startswith("yield/closeout:")


def test_false_red_without_approval_sends_back(steward_home, tmp_path):
    store, j, _ = _five_b(steward_home, approval=False, rule="verify_rounds_exhausted")
    cdb, ep, trig = _episode(tmp_path, j.job_id, "needs_human:verify_rounds_exhausted")
    plan = _select(_ro(steward_home), cdb, ep, trig, rerun_fn=_rerun("env_dependent"))
    assert [v.argv[0] for v in plan.verbs] == ["send-back"]
    argv = plan.verbs[0].argv
    assert argv[-2:] == ["--workspace", "w1"] and "git commit --allow-empty" in argv[2]
    assert verbs.check_command(j.job_id, argv, workspace="w1") is None


def test_false_red_send_back_needs_a_workspace(steward_home, tmp_path):
    store, j, _ = _five_b(steward_home, approval=False, rule="verify_rounds_exhausted")
    cdb, ep, trig = _episode(tmp_path, j.job_id, "needs_human:verify_rounds_exhausted")
    calls = []
    assert _select(_ro(steward_home), cdb, ep, trig, workspace=None, rerun_fn=_rerun("env_dependent", calls)) is None
    assert calls == [] and cdb.try_count(j.job_id, "false_red", now=time.time()) == 0


def test_false_red_denied_approval_is_not_used(steward_home, tmp_path):
    store, j, rec = _five_b(steward_home)
    approvals.decide(store, j.job_id, rec["id"], "deny", decided_by="test")
    assert store.get_job(j.job_id).state is JobState.NEEDS_HUMAN  # a deny keeps the job parked for the owner
    cdb, ep, trig = _episode(tmp_path, j.job_id)
    plan = _select(_ro(steward_home), cdb, ep, trig, rerun_fn=_rerun("env_dependent"))
    assert [v.argv[0] for v in plan.verbs] == ["send-back"]


@pytest.mark.parametrize("outcome,needle", [("env_live_red", "live state"), ("preexisting", "fail on base")])
def test_false_red_live_red_and_preexisting_ask(steward_home, tmp_path, outcome, needle):
    store, j, rec = _five_b(steward_home)
    cdb, ep, trig = _episode(tmp_path, j.job_id)
    plan = _select(_ro(steward_home), cdb, ep, trig, rerun_fn=_rerun(outcome))
    assert plan.rung == "false_red" and plan.verbs == [] and plan.classification == outcome
    assert needle in plan.diagnosis and plan.followup is None
    assert (H[:8] if outcome == "env_live_red" else B[:8]) in plan.diagnosis
    assert plan.evidence["outcome"] == outcome


def test_false_red_real_once_then_none(steward_home, tmp_path):
    store, j, rec = _five_b(steward_home)
    ro = _ro(steward_home)
    cdb, ep, trig = _episode(tmp_path, j.job_id)
    p1 = _select(ro, cdb, ep, trig, rerun_fn=_rerun("real"))
    assert p1.rung == "false_red" and p1.classification == "real" and p1.rule is None
    assert [v.argv[0] for v in p1.verbs] == ["approve", "steer"]
    assert "real" in p1.verbs[-1].argv[2] and B[:8] in p1.verbs[-1].argv[2] and H[:8] in p1.verbs[-1].argv[2]
    assert cdb.try_count(j.job_id, "false_red_real", now=time.time()) == 1
    p2 = _select(ro, cdb, ep, trig, now=time.time() + 1, rerun_fn=_rerun("real"))
    assert p2.rung == "false_red" and p2.classification == "real" and p2.verbs == [] and p2.rule is None
    assert "real regressions" in p2.diagnosis and IDS[0] in p2.diagnosis and "already nudged once" in p2.diagnosis
    assert H[:8] in p2.diagnosis and B[:8] in p2.diagnosis and p2.evidence["outcome"] == "real"
    assert cdb.try_count(j.job_id, "false_red_real", now=time.time() + 1) == 1


def test_false_red_inconclusive_is_none_and_counts_try(steward_home, tmp_path):
    store, j, rec = _five_b(steward_home)
    cdb, ep, trig = _episode(tmp_path, j.job_id)
    assert _select(_ro(steward_home), cdb, ep, trig, rerun_fn=_rerun("inconclusive")) is None
    assert cdb.try_count(j.job_id, "false_red", now=time.time()) == 1


def test_false_red_stale_red_is_not_applicable(steward_home, tmp_path):
    store, j, rec = _five_b(steward_home, ruled="ffffffffffffffffffffffffffffffffffffffff")
    cdb, ep, trig = _episode(tmp_path, j.job_id)
    calls = []
    assert _select(_ro(steward_home), cdb, ep, trig, rerun_fn=_rerun("env_dependent", calls)) is None
    assert calls == [] and cdb.try_count(j.job_id, "false_red", now=time.time()) == 0


def test_false_red_unset_last_ruled_counts_as_ruled(steward_home, tmp_path):
    store, j, rec = _five_b(steward_home, ruled=None)
    cdb, ep, trig = _episode(tmp_path, j.job_id)
    plan = _select(_ro(steward_home), cdb, ep, trig, rerun_fn=_rerun("env_dependent"))
    assert plan.rung == "false_red"


def test_false_red_green_verification_is_not_applicable(steward_home, tmp_path):
    store, j, rec = _five_b(steward_home)
    store.append_event(j.job_id, what="verification recorded", evidence={"commit_sha": H, "new_failures": []},
                       rule="verify_at_exact_commit", judgment=Judgment.code)
    cdb, ep, trig = _episode(tmp_path, j.job_id)
    assert _select(_ro(steward_home), cdb, ep, trig, rerun_fn=_rerun("env_dependent")) is None


def test_false_red_other_trigger_is_not_applicable(steward_home, tmp_path):
    store, j, rec = _five_b(steward_home)
    cdb, ep, trig = _episode(tmp_path, j.job_id, "needs_human:finish_line_bounces_exhausted")
    assert _select(_ro(steward_home), cdb, ep, trig, rerun_fn=_rerun("env_dependent")) is None


def test_rung_skipped_after_two_tries_and_when_disabled(steward_home, tmp_path):
    store, j, rec = _five_b(steward_home)
    ro = _ro(steward_home)
    cdb, ep, trig = _episode(tmp_path, j.job_id)
    now = time.time()
    cdb.record_try(j.job_id, "false_red", now=now - 10)
    cdb.record_try(j.job_id, "false_red", now=now - 5)
    assert _select(ro, cdb, ep, trig, now=now, rerun_fn=_rerun("env_dependent")) is None
    assert cdb.try_count(j.job_id, "false_red", now=now) == 2
    cdb2, ep2, trig2 = _episode(tmp_path, j.job_id, name="c2.db")
    cdb2.disable_rung("false_red", "x", now=now)
    assert _select(ro, cdb2, ep2, trig2, now=now, rerun_fn=_rerun("env_dependent")) is None
    assert cdb2.try_count(j.job_id, "false_red", now=now) == 0


def test_rung_already_tried_in_episode_moves_on(steward_home, tmp_path):
    store, j, rec = _five_b(steward_home)
    cdb, ep, trig = _episode(tmp_path, j.job_id)
    cdb.mark(ep["episode_id"], rungs_json=json.dumps([{"rung": "false_red"}]))
    ep = cdb.open_for(j.job_id)
    assert _select(_ro(steward_home), cdb, ep, trig, rerun_fn=_rerun("env_dependent")) is None


def test_ask_only_episode_gets_no_plan(steward_home, tmp_path):
    store, j, rec = _five_b(steward_home)
    cdb, ep, trig = _episode(tmp_path, j.job_id)
    cdb.mark(ep["episode_id"], evidence_json=json.dumps({"ask_only": True}))
    ep = cdb.open_for(j.job_id)
    assert _select(_ro(steward_home), cdb, ep, trig, rerun_fn=_rerun("env_dependent")) is None


def test_job_no_longer_parked_is_not_applicable(steward_home, tmp_path):
    store, j, rec = _five_b(steward_home)
    approvals.decide(store, j.job_id, rec["id"], "approve", decided_by="test")
    assert store.get_job(j.job_id).state is JobState.RUNNING
    cdb, ep, trig = _episode(tmp_path, j.job_id)
    assert _select(_ro(steward_home), cdb, ep, trig, rerun_fn=_rerun("env_dependent")) is None


# ---------- superseded_verify ----------

def _superseded(steward_home, *, ruled):
    store = Store()
    j = _running(store)
    if ruled:
        store.kv_set(j.job_id, "last_ruled_sha", ruled)
    _to(store, j.job_id, JobState.NEEDS_HUMAN, "background verification crashed; tests unverified",
        "bg_verify_unverified",
        evidence={"reasons": ["tests unverified: check run cr-0123456789ab lost: superseded by cr-ba9876543210"],
                  "commit_sha": H})
    return store, j


def test_superseded_verify_sends_back_with_empty_commit_when_head_was_ruled(steward_home, tmp_path):
    store, j = _superseded(steward_home, ruled=H)
    cdb, ep, trig = _episode(tmp_path, j.job_id, "needs_human:bg_verify_unverified")
    plan = _select(_ro(steward_home), cdb, ep, trig)
    assert plan.rung == "superseded_verify" and [v.argv[0] for v in plan.verbs] == ["send-back"]
    text = plan.verbs[0].argv[2]
    assert text.startswith("Your verification run was superseded by a newer run, not failed. Report STEWARD-DONE again")
    assert "git commit --allow-empty" in text and ep["episode_id"][:8] in text and text.endswith(".")
    assert plan.verbs[0].argv[-2:] == ["--workspace", "w1"]
    assert plan.evidence["check_runs"] == ["cr-0123456789ab", "cr-ba9876543210"]
    assert verbs.check_command(j.job_id, plan.verbs[0].argv, workspace="w1") is None


def test_superseded_verify_without_clause_when_head_not_ruled(steward_home, tmp_path):
    store, j = _superseded(steward_home, ruled=B)
    cdb, ep, trig = _episode(tmp_path, j.job_id, "needs_human:bg_verify_unverified")
    plan = _select(_ro(steward_home), cdb, ep, trig)
    assert plan.verbs[0].argv[2] == rungs.SEND_BACK_SUPERSEDED.format(empty_commit_clause="")


def test_unverified_but_not_superseded_is_not_applicable(steward_home, tmp_path):
    store = Store()
    j = _running(store)
    _to(store, j.job_id, JobState.NEEDS_HUMAN, "background verification crashed; tests unverified",
        "bg_verify_unverified", evidence={"reasons": ["tests unverified: pytest crashed"], "commit_sha": H})
    cdb, ep, trig = _episode(tmp_path, j.job_id, "needs_human:bg_verify_unverified")
    assert _select(_ro(steward_home), cdb, ep, trig) is None


# ---------- obsolete_fixup ----------

def _fixup(store, parent_state=JobState.RELEASED, link="str"):
    parent = _running(store, "p", "/tmp/r")
    if parent_state is JobState.RELEASED:
        _to(store, parent.job_id, JobState.WORKER_FINISHED)
        _to(store, parent.job_id, JobState.CHECKS_PASSED)
        _to(store, parent.job_id, JobState.RELEASED, judgment=Judgment.human)
    fix = _running(store, "Merge `p` into fleet-registry", "/tmp/r")
    store.kv_set(fix.job_id, "autopilot_followup", parent.job_id if link == "str" else {"parent": parent.job_id})
    _to(store, fix.job_id, JobState.NEEDS_HUMAN, "merge conflict", "merge_repair")
    return parent, fix


def test_obsolete_fixup_stops(steward_home, tmp_path):
    store = Store()
    parent, fix = _fixup(store)
    cdb, ep, trig = _episode(tmp_path, fix.job_id, "needs_human:finish_line_bounces_exhausted")
    plan = _select(_ro(steward_home), cdb, ep, trig, runner_alive=ALIVE)
    assert plan.rung == "obsolete_fixup" and plan.verbs[0].argv == ["stop", fix.job_id]
    assert plan.evidence["parent_id"] == parent.job_id and plan.evidence["parent_state"] == "RELEASED"
    assert "incorporated_commit" in plan.evidence


def test_obsolete_fixup_needs_done_parent_and_string_link(steward_home, tmp_path):
    store = Store()
    _, fix = _fixup(store, parent_state=JobState.RUNNING)
    cdb, ep, trig = _episode(tmp_path, fix.job_id, "needs_human:finish_line_bounces_exhausted")
    assert _select(_ro(steward_home), cdb, ep, trig, runner_alive=ALIVE) is None
    _, fix2 = _fixup(store, link="dict")
    cdb2, ep2, trig2 = _episode(tmp_path, fix2.job_id, "needs_human:finish_line_bounces_exhausted", name="c2.db")
    assert _select(_ro(steward_home), cdb2, ep2, trig2, runner_alive=ALIVE) is None


# ---------- recorded_default ----------

def _iso(dt):
    return dt.isoformat()


def _questions(store, jid, *, age_s, recommended=("a",), status="open"):
    seen = _iso(datetime.now(timezone.utc) - timedelta(seconds=age_s))
    recs = [{"qid": "steward_questions:q.json:q1", "status": status, "starred": bool(recommended),
             "recommended": list(recommended), "recommended_text": "Option A", "first_seen_at": seen},
            {"qid": "steward_questions:q.json:q2", "status": "open", "starred": False, "recommended": [],
             "first_seen_at": seen}]
    store.kv_set(jid, "questions", recs)


def test_recorded_default_answers_and_follows_up_when_parked(steward_home, tmp_path):
    store = Store()
    j = _running(store)
    _to(store, j.job_id, JobState.NEEDS_HUMAN, "parked", "worker_start_blocked")
    _questions(store, j.job_id, age_s=7200)
    cdb, ep, trig = _episode(tmp_path, j.job_id, "needs_human:worker_start_blocked")
    plan = _select(_ro(steward_home), cdb, ep, trig, runner_alive=ALIVE)
    assert plan.rung == "recorded_default"
    assert plan.verbs[0].argv == ["answer", j.job_id, "--accept-recommended", "steward_questions:q.json:q1"]
    assert plan.followup_after_s >= 70 and [v.argv[0] for v in plan.followup] == ["send-back"]
    fu = plan.followup[0].argv
    assert fu[2] == rungs.SEND_BACK_ANSWERED.format(qids="steward_questions:q.json:q1")
    assert fu[-2:] == ["--workspace", "w1"]
    assert plan.evidence["questions"][0]["recommended"] == ["a"]
    for v in plan.verbs + plan.followup:
        assert verbs.check_command(j.job_id, v.argv, workspace="w1") is None


def test_recorded_default_running_has_no_followup(steward_home, tmp_path):
    store = Store()
    j = _running(store)
    _questions(store, j.job_id, age_s=7200)
    store.append_event(j.job_id, what="liveness_ask", evidence={}, rule="liveness", judgment=Judgment.code)
    cdb, ep, trig = _episode(tmp_path, j.job_id, "liveness_ask")
    plan = _select(_ro(steward_home), cdb, ep, trig, runner_alive=ALIVE)
    assert plan.rung == "recorded_default" and plan.followup is None and plan.followup_after_s == 0


def test_recorded_default_young_or_unstarred_questions_wait(steward_home, tmp_path):
    store = Store()
    j = _running(store)
    _questions(store, j.job_id, age_s=600)
    cdb, ep, trig = _episode(tmp_path, j.job_id, "liveness_ask")
    assert _select(_ro(steward_home), cdb, ep, trig, runner_alive=ALIVE) is None
    _questions(store, j.job_id, age_s=7200, recommended=())
    assert _select(_ro(steward_home), cdb, ep, trig, runner_alive=ALIVE) is None
    _questions(store, j.job_id, age_s=7200, status="answered")
    assert _select(_ro(steward_home), cdb, ep, trig, runner_alive=ALIVE) is None


def test_recorded_default_falls_back_to_questions_found_event(steward_home, tmp_path):
    store = Store()
    j = _running(store)
    store.append_event(j.job_id, what="questions_found", evidence={"qids": ["q9"]}, rule="questions_scan",
                       judgment=Judgment.code)
    store.kv_set(j.job_id, "questions", [{"qid": "q9", "status": "open", "starred": True, "recommended": ["b"]}])
    cdb, ep, trig = _episode(tmp_path, j.job_id, "liveness_ask")
    assert _select(_ro(steward_home), cdb, ep, trig, runner_alive=ALIVE) is None
    plan = _select(_ro(steward_home), cdb, ep, trig, runner_alive=ALIVE, now=time.time() + 4000)
    assert plan.rung == "recorded_default" and plan.verbs[0].argv[-1] == "q9"


# ---------- dead_runner / cockpit_down ----------

def test_dead_runner_restarts_only_when_watchdog_inactive(steward_home, tmp_path):
    store = Store()
    j = _running(store, "g", "/tmp/r")
    store.kv_set(j.job_id, "runner_code", {"pid": 4242})
    ro = _ro(steward_home)
    cdb, ep, trig = _episode(tmp_path, j.job_id, "orphan:RUNNING")
    assert _select(ro, cdb, ep, trig, watchdog_active=True) is None
    assert _select(ro, cdb, ep, trig, runner_alive=ALIVE) is None
    plan = _select(ro, cdb, ep, trig, watchdog_active=lambda: False)
    assert plan.rung == "dead_runner" and plan.verbs[0].argv == ["runner-restart", j.job_id]
    assert plan.evidence["runner_code"] == {"pid": 4242} and plan.evidence["last_non_noise_event_ts"]


def test_dead_runner_on_liveness_ask_needs_running(steward_home, tmp_path):
    store = Store()
    j = _running(store, "g", "/tmp/r")
    _to(store, j.job_id, JobState.WORKER_FINISHED)
    cdb, ep, trig = _episode(tmp_path, j.job_id, "liveness_ask")
    assert _select(_ro(steward_home), cdb, ep, trig) is None


def test_cockpit_down_ups_only_when_watchdog_inactive(steward_home, tmp_path):
    ro = _ro_empty(steward_home)
    detail = {"component": "control", "detail": "no control pane recorded", "red_since": 1.0}
    cdb, ep, trig = _episode(tmp_path, "health:control", "health:control", detail=detail)
    assert _select(ro, cdb, ep, trig, watchdog_active=True) is None
    assert _select(ro, cdb, ep, trig, workspace=None) is None
    plan = _select(ro, cdb, ep, trig)
    assert plan.rung == "cockpit_down" and plan.verbs[0].argv == ["up", "--workspace", "w1"] and plan.verbs[0].direct
    assert cdb.try_count("health:control", "cockpit_down", now=time.time()) == 1
    cdb2, ep2, trig2 = _episode(tmp_path, "health:control", "health:control", name="c2.db",
                                detail={"component": "control", "detail": "control code is stale"})
    assert _select(ro, cdb2, ep2, trig2) is None


def _ro_empty(home):
    Store()  # creates the schema
    return _ro(home)


# ---------- unstuck / resolved ----------

def _ep(**kw):
    base = {"episode_id": "e1", "job_id": None, "trigger": "needs_human:recovery_budget_exhausted",
            "opened_at": time.time() - 100, "acted_at": None, "unstuck_at": None}
    return {**base, **kw}


def test_unstuck_and_resolved_for_needs_human(steward_home, tmp_path):
    store, j, rec = _five_b(steward_home)
    ro = _ro(steward_home)
    now = time.time()
    ep = _ep(job_id=j.job_id, acted_at=now - 5)
    assert not rungs.unstuck(ro, ep)
    assert not rungs.resolved(ro, ep, now=now, settings=S)
    approvals.decide(store, j.job_id, rec["id"], "approve", decided_by="test")
    assert rungs.unstuck(ro, ep)
    assert not rungs.resolved(ro, ep, now=now, settings=S)  # unstuck, but not yet for resolved_watch_s
    assert rungs.resolved(ro, {**ep, "unstuck_at": now - 601}, now=now, settings=S)
    assert not rungs.resolved(ro, {**ep, "unstuck_at": now - 100}, now=now, settings=S)
    store.append_event(j.job_id, what="verification recorded", evidence={"commit_sha": "e9db81b0", "new_failures": []},
                       rule="verify_at_exact_commit", judgment=Judgment.code)
    assert rungs.resolved(ro, ep, now=now, settings=S)


def test_resolved_when_checks_passed(steward_home):
    store = Store()
    j = _running(store)
    _to(store, j.job_id, JobState.WORKER_FINISHED)
    _to(store, j.job_id, JobState.CHECKS_PASSED)
    assert rungs.resolved(_ro(steward_home), _ep(job_id=j.job_id, acted_at=time.time()), now=time.time(), settings=S)


def test_old_green_verification_does_not_resolve(steward_home):
    store, j, rec = _five_b(steward_home)
    store.append_event(j.job_id, what="verification recorded", evidence={"commit_sha": H, "new_failures": []},
                       rule="verify_at_exact_commit", judgment=Judgment.code)
    ep = _ep(job_id=j.job_id, acted_at=time.time() + 60)
    assert not rungs.resolved(_ro(steward_home), ep, now=time.time() + 61, settings=S)


def test_unstuck_orphan_needs_a_real_event_after_acting(steward_home):
    store = Store()
    j = _running(store, "g", "/tmp/r")
    ro = _ro(steward_home)
    ep = _ep(job_id=j.job_id, trigger="orphan:RUNNING", acted_at=time.time())
    assert not rungs.unstuck(ro, ep)
    store.append_event(j.job_id, what="heartbeat", evidence={}, rule="hb", judgment=Judgment.code)
    store.append_event(j.job_id, what="runner_restarted", evidence={"origin": "coordinator"},
                       rule="operator_runner_restart", judgment=Judgment.code)
    assert not rungs.unstuck(ro, ep)
    store.append_event(j.job_id, what="worker progress", evidence={}, rule="r", judgment=Judgment.code)
    assert rungs.unstuck(ro, ep)
    assert rungs.unstuck(ro, {**ep, "trigger": "liveness_ask"})


def test_unstuck_health_control_reads_latest_health(steward_home):
    ro = _ro_empty(steward_home)
    ep = _ep(job_id="health:control", trigger="health:control", acted_at=time.time())
    assert not rungs.unstuck(ro, ep)
    assert not rungs.unstuck(ro, ep, health={"components": {"control": {"ok": False}}})
    assert rungs.unstuck(ro, ep, health={"components": [{"name": "control", "ok": True}]})
    assert not rungs.resolved(ro, ep, now=time.time(), settings=S)
    assert rungs.resolved(ro, {**ep, "unstuck_at": time.time() - 700}, now=time.time(), settings=S)


def test_text_constants_are_exact():
    assert rungs.NUDGE_RESUBMIT.startswith("Steward coordinator: your work at {head8} is done as far as these checks go.")
    assert "`git commit --allow-empty -m \"chore: re-run verification (coordinator {ep8})\"`" in rungs.NUDGE_RESUBMIT
    assert rungs.NUDGE_REAL == ("Steward coordinator: {ids3}… fail on your commit {head8} and pass on base {base8}; "
                                "they are real regressions. Fix these only, commit, and report STEWARD-DONE.")
    assert rungs.SEND_BACK_SUPERSEDED == ("Your verification run was superseded by a newer run, not failed. "
                                          "Report STEWARD-DONE again{empty_commit_clause}.")
    assert rungs.SEND_BACK_ANSWERED == ("Your open questions {qids} were answered with their recorded "
                                        "recommendations. Continue from there and report STEWARD-DONE when finished.")
    assert "Quarantine or hermeticize: {ids}." in rungs.RULE_FALSE_RED
