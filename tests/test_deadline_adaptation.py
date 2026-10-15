"""v2.2 Phase 22 Task 3 (DL-11, DL-12, DL-13): evidence warnings with cooldown, bounded adaptation, safe deadline steers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from deadline_fixtures import Repo, commit_deadline, plan_json
from steward import deadline, deadline_outbox, deadline_plan, deadline_status
from steward.models import JobState, Judgment
from steward.runner_exec import SubprocessRunner
from steward.steering import Steering
from steward.store import Store

NOW = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
DUE = NOW + timedelta(hours=4)
RUN = SubprocessRunner()


@pytest.fixture
def store(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_NOTIFIER", "telegram")
    return Store(steward_home)


@pytest.fixture
def repo(tmp_path, store):
    r = Repo(tmp_path, store)
    commit_deadline(store, r.job_id, now=NOW)
    deadline.set_mode(store, r.job_id, "active", judgment=Judgment.human, origin="cli", request_id="mode")
    return r


def _persist(store, jid, cand_status, minutes):
    """Drive one assessment with a forced candidate status through the real persistence wrapper."""
    import steward.deadline_status as ds

    real = ds.assess

    def fake(snap, now):
        out = real(snap, now)
        out.update(status=cand_status, label=ds.LABEL[cand_status], basis="evidence",
                   reasons=[{"rule": "TEST", "evidence": f"forced {cand_status}"}])
        return out

    ds.assess = fake
    try:
        return ds._persist_status(store, jid, NOW + timedelta(minutes=minutes))
    finally:
        ds.assess = real


def _warnings(store, jid):
    return [d for d in deadline_outbox.drafts(store, jid) if d["kind"] == "warning"]


# -- warnings ---------------------------------------------------------------------------------------------------

def test_worsening_records_a_warning_with_cooldown_but_unlikely_is_never_delayed(store, repo):
    jid = repo.job_id
    _persist(store, jid, "on_track", 1)
    _persist(store, jid, "at_risk", 2)
    assert len(_warnings(store, jid)) == 1
    _persist(store, jid, "on_track", 3)  # improvement held by hysteresis
    store.kv_set(jid, deadline_status.K_STATUS, {**store.kv_get(jid, deadline_status.K_STATUS), "status": "on_track"})
    _persist(store, jid, "at_risk", 8)  # a second At risk within 15 min: status event yes, draft no
    changes = [e for e in store.events(jid) if e.what == "deadline_status_changed"]
    assert changes[-1].evidence["warning"]["suppressed"].startswith("cooldown")
    assert len(_warnings(store, jid)) == 1
    _persist(store, jid, "unlikely", 9)  # Unlikely bypasses the cooldown
    assert len(_warnings(store, jid)) == 2 and _warnings(store, jid)[-1]["title"].startswith("⏱ Unlikely")


def test_on_track_between_targets_is_quiet_and_no_evidence_is_not_an_all_clear(store, repo):
    jid = repo.job_id
    deadline_status.sweep(store, jid, now=NOW + timedelta(minutes=1), runner=RUN, worktree=repo.wt, force=True)
    assert deadline_status.project(store, jid, NOW + timedelta(minutes=1))["sign"] == "At risk · estimating"
    assert _warnings(store, jid) == []  # an initial estimating state is not a worsening


def test_shadow_mode_records_status_but_no_warning_drafts(store, repo):
    deadline.set_mode(store, repo.job_id, "shadow", judgment=Judgment.human, origin="cli", request_id="s")
    _persist(store, repo.job_id, "on_track", 1)
    _persist(store, repo.job_id, "unlikely", 2)
    assert _warnings(store, repo.job_id) == []


# -- bounded adaptation ---------------------------------------------------------------------------------------

def _validate(store, repo, body):
    return deadline_plan.validate(json.dumps(body), commitment=deadline.current(store, repo.job_id), now=NOW)


def _body(repo):
    return json.loads(plan_json(repo.job_id, 1, due=DUE, now=NOW))


def test_only_enumerated_adaptations_pass(store, repo):
    ok = _body(repo)
    ok["adaptations"] = [{"kind": "reorder", "detail": "M2 before M1 (independent)"},
                         {"kind": "switch_approach", "detail": "csv.DictReader", "reason": "simpler"},
                         {"kind": "stop_retry", "predicate": "repeated_failure_code"},
                         {"kind": "drop_optional", "items": ["O1"], "reason": "time"}]
    plan, reasons = _validate(store, repo, ok)
    assert reasons == [] and plan["dropped"] == ["O1"] and len(plan["adaptations"]) == 4
    for bad, why in [({"kind": "extend_deadline"}, "unauthorized_adaptation"),
                     ({"kind": "drop_optional", "items": ["R1"]}, "unauthorized_drop"),
                     ({"kind": "stop_retry", "failures": 1}, "stop_retry_unproven")]:
        body = _body(repo)
        body["adaptations"] = [bad]
        assert any(r.startswith(why) for r in _validate(store, repo, body)[1])


def test_reorder_that_breaks_a_dependency_is_rejected(store, repo):
    body = _body(repo)
    body["milestones"].insert(1, {"id": "M2", "title": "x", "items": ["R1"], "evidence": "commit", "paths": ["a.py"],
                                  "target_at": (NOW + timedelta(minutes=30)).isoformat(), "depends_on": ["M1"]})
    body["adaptations"] = [{"kind": "reorder", "detail": "M2 first"}]
    assert any(r.startswith("dependency_after_dependent") for r in _validate(store, repo, body)[1])


def test_adopted_adaptations_and_drops_are_recorded_with_provenance(store, repo):
    body = _body(repo)
    body["adaptations"] = [{"kind": "drop_optional", "items": ["O1"], "reason": "M1 late"}]
    body["tradeoffs"] = ["mark W1 optional to save time (worker-planned, uncalibrated)"]
    repo.commit({deadline_plan.plan_path(repo.job_id): json.dumps(body)}, "plan")
    deadline_plan.refresh(store, repo.job_id, worktree=repo.wt, runner=RUN, now=NOW)
    (ad,) = [e for e in store.events(repo.job_id) if e.what == "deadline_adaptation"]
    (dropped,) = [e for e in store.events(repo.job_id) if e.what == "deadline_scope_dropped"]
    assert ad.evidence["sha"] and ad.evidence["provenance"].startswith("worker plan") and ad.judgment == Judgment.code
    assert dropped.evidence["item"] == "O1"
    p = deadline_status.project(store, repo.job_id, NOW)
    assert p["plan"]["tradeoffs"] == body["tradeoffs"] and deadline.current(store, repo.job_id)["due_at_utc"] == \
        "2026-10-03T00:00:00Z"  # a plan never moves the deadline


# -- deadline-origin steering ----------------------------------------------------------------------------------

@pytest.mark.parametrize("verb", ["goal", "avoid", "authority", "focus"])
def test_deadline_origin_can_only_priority_or_nudge(store, repo, verb):
    with pytest.raises(ValueError, match="origin=deadline"):
        Steering(store).record(repo.job_id, verb, "x", origin="deadline")


def test_deadline_origin_can_never_be_urgent(store, repo):
    with pytest.raises(ValueError, match="urgent"):
        Steering(store).record(repo.job_id, "nudge", "x", origin="deadline", urgent=True)


def test_supervisor_steer_once_per_reason_with_code_provenance(store, repo):
    res = deadline.supervisor_steer(store, repo.job_id, reason="worsened:at_risk:t1", text="keep going")
    assert res == {"recorded": True}
    assert deadline.supervisor_steer(store, repo.job_id, reason="worsened:at_risk:t1", text="again") is None
    (ins,) = [i for i in Steering(store).all_instructions(repo.job_id) if i["origin"] == "deadline"]
    assert ins["verb"] == "nudge" and "commitment rev 1" in ins["text"] and not ins.get("urgent")
    ev = [e for e in store.events(repo.job_id) if e.what == "nudge instruction recorded"][-1]
    assert ev.judgment == Judgment.code


def test_unacknowledged_deadline_or_trajectory_instruction_blocks_stacking(store, repo):
    deadline.supervisor_steer(store, repo.job_id, reason="r1", text="first")
    res = deadline.supervisor_steer(store, repo.job_id, reason="r2", text="second")
    assert res == {"recorded": False, "why": "pending_unacked"}


def test_an_unacknowledged_trajectory_redirect_also_blocks_a_deadline_steer(store, repo):
    Steering(store).record(repo.job_id, "focus", "advisor: finish the parser first", origin="trajectory")
    res = deadline.supervisor_steer(store, repo.job_id, reason="r1", text="deadline reminder")
    assert res == {"recorded": False, "why": "pending_unacked"}
    for ins in Steering(store).all_instructions(repo.job_id):
        Steering(store).mark_acknowledged(repo.job_id, ins["rev"], "acknowledged")
    assert deadline.supervisor_steer(store, repo.job_id, reason="r2", text="deadline reminder") == {"recorded": True}


def test_a_human_steer_since_assessment_makes_the_deadline_steer_stale(store, repo):
    seen = store.get_revision(repo.job_id)
    Steering(store).record(repo.job_id, "focus", "human says: parser first", origin="cli")
    res = deadline.supervisor_steer(store, repo.job_id, reason="r", text="x", expected_revision=seen)
    assert res == {"recorded": False, "why": "stale_after_human_steer"}
    skipped = [e for e in store.events(repo.job_id) if e.what == "deadline_steer_skipped"]
    assert skipped and skipped[-1].evidence["why"] == "stale_after_human_steer"
    assert not [i for i in Steering(store).all_instructions(repo.job_id) if i["origin"] == "deadline"]


def test_shadow_mode_never_steers_and_steers_touch_no_recovery_trajectory_or_efficiency_state(store, repo):
    deadline.set_mode(store, repo.job_id, "shadow", judgment=Judgment.human, origin="cli", request_id="s")
    assert deadline.supervisor_steer(store, repo.job_id, reason="r", text="x") is None
    deadline.set_mode(store, repo.job_id, "active", judgment=Judgment.human, origin="cli", request_id="a")
    before = {k for (k,) in store._conn.execute("SELECT key FROM kv WHERE job_id = ?", (repo.job_id,))}
    deadline.supervisor_steer(store, repo.job_id, reason="r", text="x")
    after = {k for (k,) in store._conn.execute("SELECT key FROM kv WHERE job_id = ?", (repo.job_id,))}
    assert after - before <= {"deadline_steers", "instructions", "job_revision"}
    whats = {e.what for e in store.events(repo.job_id)}
    assert not any(w.startswith(("recovery", "trajectory", "efficiency", "watcher_escalated", "urgent_steer"))
                   for w in whats)


def test_human_deadline_revision_records_the_ordering_fence(store, repo, monkeypatch):
    from steward import cli

    monkeypatch.setattr(deadline, "utcnow", lambda: NOW)
    assert cli.main(["deadline", repo.job_id, "set", "+5h"]) == 0
    fences = [i for i in Steering(store).all_instructions(repo.job_id) if i["origin"] == "cli"
              and "commitment rev 2" in i["text"]]
    assert len(fences) == 1 and fences[0]["verb"] == "priority"


def test_plan_rejection_and_missing_plan_each_steer_at_most_once(store, repo):
    bad = _body(repo)
    bad["dropped"] = ["R1"]
    repo.commit({deadline_plan.plan_path(repo.job_id): json.dumps(bad)}, "bad plan")
    for m in (1, 2, 3):
        deadline_status.sweep(store, repo.job_id, now=NOW + timedelta(minutes=m), runner=RUN, worktree=repo.wt,
                              force=True)
        store.kv_set(repo.job_id, deadline_plan.K_SEEN, {})
    recorded = [e for e in store.events(repo.job_id) if e.what == "deadline_steer_recorded"]
    assert len(recorded) == 1 and recorded[0].evidence["reason"].startswith("plan_rejected:unauthorized_drop")


def test_steer_claims_are_atomic_across_processes_and_ids_are_per_reason(store, repo, steward_home):
    import threading

    barrier = threading.Barrier(3)
    out = []

    def go():
        own = Store(steward_home)
        barrier.wait()
        out.append(deadline.supervisor_steer(own, repo.job_id, reason="worsened:x", text="t"))

    threads = [threading.Thread(target=go) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(str(o) for o in out) == ["None", "None", "{'recorded': True}"]
    ids = {i["text"] for i in Steering(store).all_instructions(repo.job_id) if i["origin"] == "deadline"}
    assert len(ids) == 1


def test_control_pane_tick_steps_are_isolated():
    from steward import cli

    assert cli._isolated(lambda: (_ for _ in ()).throw(RuntimeError("db locked"))) is None
    assert cli._isolated(lambda: 7) == 7
