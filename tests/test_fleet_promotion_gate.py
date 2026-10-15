"""Phase 29-03 (FLEET-22): same-build binding of the completed-trial receipt, the phase-gate + separate explicit owner
approval requirements, and the operator path to the insert-only evidence writers. Nothing here promotes anything."""

from __future__ import annotations

import io

import pytest
from test_fleet_dev_isolation import JOB, Trial, World, replay_driver
from test_fleet_learning_replay import note, seed_active_evidence

from steward import fleet_dev as fd
from steward import fleet_runtime as rt
from steward.api import StewardAPI
from steward.store import Store

def seed_verified_gate_receipts(gate, *, failing=()):
    for name in rt.RolloutGate.PHASE_GATES:
        gate._record_phase_gate_receipt(name, actor="test-fixture", source_digest=gate.build["digest"],
                                        command=["pytest", "-q", name], output_digest="a" * 64,
                                        exit_code=1 if name in failing else 0)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def finished_trial(world, tmp_path, monkeypatch):
    t = Trial(world, tmp_path)
    run = fd.run_trial(t.inst, t.gate, t.e.ctl, JOB, replay_driver(t, monkeypatch), clock=t.e.clock)
    assert run["ran"] and run["candidate_sha"]
    return t


# ------------------------------------------------------------------------------------------ completed-trial receipt


def test_completed_trial_receipt_is_issued_only_after_final_sentinels_and_bindings_pass(world, tmp_path, monkeypatch):
    t = finished_trial(world, tmp_path, monkeypatch)
    out = fd.complete_trial(t.inst, t.gate, job_id=JOB, before=t.before, after=t.after())
    assert out["issued"] and out["sentinel"]["unchanged"]
    ev = out["evidence"]
    run = t.gate.fleet.rollout_evidence("trial_run")[-1]["payload"]
    assert ev["commit"] == t.inst.build["commit"] and ev["build_digest"] == t.inst.build["digest"]
    assert ev["candidate_sha"] == run["candidate_sha"] and ev["job_id"] == JOB and ev["public_version"] == "1.2.3"
    assert ev["outbound"] == t.inst.outbound and ev["rollback"]["checkout_head"] == world.commit
    assert ev["actions_used"] == 1 and ev["max_actions"] == 1 and ev["instance"]["service"] == t.inst.service
    row = t.gate.fleet.rollout_evidence("trial_receipt")[-1]
    assert row["seq"] == out["receipt_id"] and row["payload"]["passed"] and row["payload"]["source"] == t.gate.TRIAL_SOURCE
    # the receipt is insert-only
    with pytest.raises(Exception, match="insert-only"):
        t.e.store._conn.execute("UPDATE fleet_rollout_evidence SET kind='x' WHERE seq=?", (row["seq"],))
    # it ends the trial authority and unlocks NOTHING by itself
    assert "trial_complete" in t.gate.trial_authority(JOB, audit=False)["reasons"]
    assert t.gate.eligibility()["missing"] == ["false_action_review", "kill_switch_exercise", "active_owner_approval"]
    assert t.gate.request_mode("active", actor="x")["accepted"] is False
    st = t.gate.promotion_status()
    assert st["eligible"] is False and st["missing"] == ["phase_gates", "production_approval"] and st["performs_promotion"] is False


def test_spending_the_whole_cap_is_a_normal_end_but_a_drift_revocation_fails_the_trial(world, tmp_path, monkeypatch):
    t = finished_trial(world, tmp_path, monkeypatch)
    assert note(t.e, "late")["reason"] == "trial_cap_exhausted"            # audited as a (benign) cap revocation
    assert [r["payload"]["reasons"] for r in t.gate.fleet.rollout_evidence("trial_revoked")] == [["trial_cap_exhausted"]]
    assert fd.complete_trial(t.inst, t.gate, job_id=JOB, before=t.before, after=t.after())["issued"] is True
    t2 = finished_trial(World(tmp_path / "d"), tmp_path / "d", monkeypatch)
    (t2.inst.src / "steward" / "mod.py").write_text("X = 5\n")
    assert "instance_identity_changed" in note(t2.e, "drift")["reason"]      # the gate revokes on the drift
    (t2.inst.src / "steward" / "mod.py").write_text("X = 1\n")             # restoring the bytes does not restore trust
    out = fd.complete_trial(t2.inst, t2.gate, job_id=JOB, before=t2.before, after=t2.after())
    assert out["issued"] is False and any("revoked" in p for p in out["problems"])


@pytest.mark.parametrize("what", ["config", "worktree", "canary", "root"])
def test_a_changed_live_sentinel_issues_no_receipt(world, tmp_path, monkeypatch, what):
    t = finished_trial(world, tmp_path, monkeypatch)
    if what == "config":
        (world.cfg / "steward.toml").write_text("changed = 1\n")
    elif what == "worktree":
        (world.home / "worktrees" / "wt-a" / ".git").write_text("gitdir: /moved\n")
    elif what == "canary":
        s = Store(world.home)
        s.create_job("leak", "/r", job_id=JOB)
        s._conn.close()
    else:
        t.world.live = fd.LiveState(**{**world.live.__dict__, "data": world.cfg})
    out = fd.complete_trial(t.inst, t.gate, job_id=JOB, before=t.before, after=t.after())
    assert out["issued"] is False and out["problems"]
    assert t.gate.fleet.rollout_evidence("trial_receipt") == []


def test_stale_or_missing_bindings_issue_no_receipt(world, tmp_path, monkeypatch):
    # no run recorded
    t = Trial(world, tmp_path)
    assert fd.complete_trial(t.inst, t.gate, job_id=JOB, before=t.before, after=t.after())["issued"] is False
    # source drift after the run
    t2 = finished_trial(World(tmp_path / "b"), tmp_path / "b", monkeypatch)
    (t2.inst.src / "steward" / "mod.py").write_text("X = 7\n")
    out = fd.complete_trial(t2.inst, t2.gate, job_id=JOB, before=t2.before, after=t2.after())
    assert out["issued"] is False and any("identity differs" in p for p in out["problems"])
    # kill-switch generation changed after the run
    t3 = finished_trial(World(tmp_path / "c"), tmp_path / "c", monkeypatch)
    t3.gate.set_kill_switch(True, actor="sam")
    t3.gate.set_kill_switch(False, actor="sam")
    out = fd.complete_trial(t3.inst, t3.gate, job_id=JOB, before=t3.before, after=t3.after())
    assert out["issued"] is False and any("kill-switch generation" in p for p in out["problems"])
    for tt in (t, t2, t3):
        assert tt.gate.fleet.rollout_evidence("trial_receipt") == []


def test_a_run_past_expiry_or_a_revoked_trial_cannot_be_completed(world, tmp_path, monkeypatch):
    t = Trial(world, tmp_path, max_actions=1, ttl_s=600)
    t.e.clock.t += 601                                    # expires before the driver can act
    out = fd.run_trial(t.inst, t.gate, t.e.ctl, JOB, lambda ctl: {}, clock=t.e.clock)
    assert out["ran"] is False
    done = fd.complete_trial(t.inst, t.gate, job_id=JOB, before=t.before, after=t.after())
    assert done["issued"] is False and t.gate.fleet.rollout_evidence("trial_receipt") == []


# ------------------------------------------------------------------------------------------ promotion gate


def test_promotion_needs_every_phase_gate_the_trial_receipt_and_an_explicit_approval_for_the_identical_build(
        world, tmp_path, monkeypatch):
    t = finished_trial(world, tmp_path, monkeypatch)
    g = t.gate
    assert fd.complete_trial(t.inst, g, job_id=JOB, before=t.before, after=t.after())["issued"]
    g.fleet.record_rollout_evidence("owner_approval", build_id=g.build["id"],
                                    build_digest=g.build["digest"], payload={"passed": True, "actor": "legacy"})
    assert "active_owner_approval" in g.eligibility()["missing"]
    assert "production_approval" in g.promotion_status()["missing"]
    # one failing gate is enough to block
    seed_verified_gate_receipts(g, failing=("FLEET-19",))
    g.record_owner_approval(purpose="production", actor="sam")
    assert g.promotion_status()["missing"] == ["phase_gates"]
    assert "active_owner_approval" in g.eligibility()["missing"]
    seed_verified_gate_receipts(g)
    st = g.promotion_status()
    assert st == {"eligible": True, "missing": [], "performs_promotion": False, "ordinary_active": False}
    assert "active_owner_approval" in g.eligibility()["missing"]
    # a different build sees none of it
    other = rt.RolloutGate(g.store, build={"id": "other", "digest": "sha256:zzz"})
    assert other.promotion_status()["missing"] == ["phase_gates", "trial_receipt", "production_approval"]
    assert rt.RolloutGate(g.store).promotion_status()["missing"][-1] == "running_build_unknown"
    # the same build with a different digest is a different build
    same_id = rt.RolloutGate(g.store, build={"id": t.inst.build["id"], "digest": "sha256:changed"})
    assert same_id.promotion_status()["eligible"] is False


def test_phase_gate_record_rejects_every_caller_assertion(world, tmp_path):
    t = Trial(world, tmp_path)
    for asserted in ({}, {g: "yes" for g in rt.RolloutGate.PHASE_GATES},
                     {g: True for g in rt.RolloutGate.PHASE_GATES}):
        with pytest.raises(ValueError, match="caller-supplied booleans"):
            t.gate.record_phase_gates(asserted, actor="x")
    assert t.gate.fleet.rollout_evidence("phase_gate_receipt") == []


def test_exact_build_verifier_runs_fixed_suites_and_latest_failure_revokes_phase_gate(world, tmp_path, monkeypatch):
    source = tmp_path / "verified-source"
    for targets in fd.PHASE_GATE_TESTS.values():
        for target in targets:
            path = source / target
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# immutable target\n")
    digest = fd.tree_digest(source)
    build = {"id": "f" * 12 + "-" + digest[7:19], "digest": digest}
    store = Store(tmp_path / "store")
    calls = []

    def successful_run(command, **kwargs):
        calls.append((command, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": "passed", "stderr": ""})()

    gate = rt.RolloutGate(store, build=build)
    monkeypatch.setattr(fd.subprocess, "run", successful_run)
    result = fd.verify_phase_gates(source, gate, actor="operator")
    assert result["passed"] and all(result["gates"].values())
    assert len(calls) == len(rt.RolloutGate.PHASE_GATES)
    assert all(call[0][2:4] == ["pytest", "-q"] for call in calls)
    assert gate._phase_gates_passed()

    # Real mapping commands name test paths rather than requirement labels, so inject a failed fixed run at the
    # process boundary to assert that the latest receipt revokes eligibility without changing the digest.
    calls_left = iter([1 if i == 5 else 0 for i in range(7)])
    def failed_latest(command, **kwargs):
        rc = next(calls_left)
        return type("Result", (), {"returncode": rc, "stdout": str(rc), "stderr": ""})()
    monkeypatch.setattr(fd.subprocess, "run", failed_latest)
    failed = fd.verify_phase_gates(source, gate, actor="operator")
    assert not failed["passed"] and not gate._phase_gates_passed()


def test_a_replay_or_other_source_cannot_pass_as_the_trial_receipt(world, tmp_path, monkeypatch):
    t = finished_trial(world, tmp_path, monkeypatch)
    assert t.gate.record_trial_receipt(job_id=JOB, passed=True, source="replay")["passed"] is False
    assert t.gate.promotion_status()["missing"][1] == "trial_receipt"


def test_ordinary_active_still_needs_review_kill_switch_exercise_and_approval_after_the_receipt(world, tmp_path, monkeypatch):
    t = finished_trial(world, tmp_path, monkeypatch)
    assert fd.complete_trial(t.inst, t.gate, job_id=JOB, before=t.before, after=t.after())["issued"]
    # exercising the kill switch writes it, which is the intended way to retire the old trial binding
    seed_active_evidence(t.gate, skip=("trial_receipt",))
    assert t.gate.request_mode("active", actor="sam")["accepted"] is True
    assert t.gate.promotion_status()["eligible"] is False and t.gate.promotion_status()["missing"] == [
        "phase_gates", "production_approval"]


def test_api_reports_missing_owner_decision_without_recording_one(world, tmp_path, monkeypatch):
    t = finished_trial(world, tmp_path, monkeypatch)
    fd.complete_trial(t.inst, t.gate, job_id=JOB, before=t.before, after=t.after())
    st = StewardAPI(t.e.store).get_dev_trial_status(JOB, build_id=t.inst.build["id"], build_digest=t.inst.build["digest"])
    assert st["receipt"]["passed"] and st["owner_approval_recorded"] is False
    assert st["promotion"]["eligible"] is False and "production_approval" in st["promotion"]["missing"]
    assert st["active_owner_approval_recorded"] is False and st["production_approval_recorded"] is False
    assert t.gate.fleet.rollout_evidence("active_owner_approval") == []
    assert t.gate.fleet.rollout_evidence("production_approval") == []


# ------------------------------------------------------------------------------------------ operator path


class Tty(io.StringIO):
    def isatty(self):
        return True


def cli(world, t, *args, stdin=None):
    out = []
    rc = fd.main(["--home", str(t.inst.home), "--build-id", t.inst.build["id"], "--build-digest", t.inst.build["digest"],
                  *args], stdin=stdin, out=out.append)
    return rc, out


def test_operator_cli_reaches_the_evidence_writers_and_approval_is_typed_and_interactive(world, tmp_path):
    t = Trial(world, tmp_path)
    rc, out = cli(world, t, "false-action-review", "--reviewed", "30", "--false", "0")
    assert rc == 0 and '"passed": true' in out[0]
    rc, out = cli(world, t, "exercise-kill-switch")
    assert rc == 0 and '"passed": true' in out[0]
    n = len(t.gate.fleet.rollout_evidence("production_approval"))
    assert cli(world, t, "approve", stdin=io.StringIO("whatever\n"))[0] == 2                    # not a terminal
    assert cli(world, t, "approve", stdin=Tty("not-the-build\n"))[0] == 1                      # wrong text
    assert len(t.gate.fleet.rollout_evidence("production_approval")) == n == 0
    rc, out = cli(world, t, "approve", stdin=Tty(t.inst.build["id"] + "\n"))
    assert rc == 0 and "nothing was deployed" in out[-1]
    appr = t.gate.fleet.rollout_evidence("production_approval")
    assert len(appr) == 1 and appr[0]["build_id"] == t.inst.build["id"] and appr[0]["build_digest"] == t.inst.build["digest"]
    assert t.gate.fleet.rollout_evidence("active_owner_approval") == []
    assert "active_owner_approval" in t.gate.eligibility()["missing"]
    rc, out = cli(world, t, "approve-active", stdin=Tty(t.inst.build["id"] + "\n"))
    assert rc == 0 and "ordinary active tech-lead mode" in out[-1]
    active = t.gate.fleet.rollout_evidence("active_owner_approval")
    assert len(active) == 1 and active[0]["build_id"] == t.inst.build["id"]
    assert len(t.gate.fleet.rollout_evidence("production_approval")) == 1
    rc, out = cli(world, t, "status")
    assert rc == 0 and '"promotion"' in out[0]


def test_operator_cli_always_needs_an_explicit_home_and_build(world):
    with pytest.raises(SystemExit):
        fd.main(["status"])
    with pytest.raises(SystemExit):
        fd.main(["--home", "/x", "status"])
