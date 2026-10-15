"""Phase 27 (FLEET-10): all-lane pause/stop, restart reconciliation, idempotency and bounded recovery."""

from __future__ import annotations

from test_fleet_lanes import JOB, TWO, World, controller, seed

from steward import fleet_lease as fl
from steward import fleet_runtime as rt
from steward.store import Store
from test_fleet_graph import task


def running_pair(tmp_path, home, **seed_kw):
    store, repo, _ = seed(tmp_path, home, seed_kw.pop("tasks", TWO), max_lanes=2, **seed_kw)
    ctl, world = controller(tmp_path, store, repo)
    ctl.admit(JOB)
    return store, repo, ctl, world


def restart(tmp_path, home, repo, world):
    """A brand-new controller process: fresh Store handle, no adapters, same external world."""
    from steward.runner_exec import SubprocessRunner
    store = Store(home)
    ctl = rt.JobController(store, repo, SubprocessRunner(), world.factory, base_dir=tmp_path / "wts",
                           probe=world.probe, controller_id="ctl-2")
    return store, ctl


# ----------------------------------------------------------------------------- pause / stop

def test_pause_persists_the_fence_before_touching_any_lane(steward_home, tmp_path):
    store, repo, ctl, world = running_pair(tmp_path, steward_home)
    seen = []
    for (lane, _n), a in world.adapters.items():
        a.interrupt = (lambda lane=lane: seen.append((lane, ctl.fence(JOB))))
    res = ctl.pause(JOB, "p1")
    assert sorted(l for l, _ in seen) == ["lane-t1", "lane-t2"]
    assert all(f and f["action"] == "pause" and f["request_id"] == "p1" for _, f in seen)
    assert res["state"] == "paused" and set(res["lanes"]) == {"lane-t1", "lane-t2"}
    # paused fences dispatch and recovery; leases stay held
    assert ctl.admit(JOB)[0]["reason"] == "dispatch_fenced"
    assert ctl.recover_lane(JOB, "lane-t1", "r", reason="x")["reason"] == "dispatch_fenced"
    assert len(fl.live(store, JOB)) == 2
    assert ctl.resume(JOB, "res1")["state"] == "running" and ctl.fence(JOB) is None


def test_stop_addresses_every_lane_and_releases_only_on_confirmation(steward_home, tmp_path):
    store, repo, ctl, world = running_pair(tmp_path, steward_home)
    fence_at_stop = []
    for (lane, _n), a in world.adapters.items():
        orig = a.stop
        a.stop = (lambda orig=orig: (fence_at_stop.append(ctl.fence(JOB)), orig())[1])
    res = ctl.stop(JOB, "s1")
    assert res["state"] == "stopped" and res["uncertain"] == []
    assert {r["status"] for r in res["lanes"].values()} == {"confirmed"}
    assert all(f and f["action"] == "stop" for f in fence_at_stop) and len(fence_at_stop) == 2
    assert fl.live(store, JOB) == []
    for lane in ("lane-t1", "lane-t2"):
        lease = fl.get(store, JOB, lane)
        assert lease["state"] == "stopped" and lease["stop_evidence"]["probe"] == "dead"
    assert ctl.admit(JOB)[0]["reason"] == "dispatch_fenced"
    assert ctl.resume(JOB, "res")["refused"] == "stopped"


def test_uncertain_lane_keeps_parent_stopping_epoch_and_blocks_replacement_and_integration(steward_home, tmp_path):
    store, repo, ctl, world = running_pair(tmp_path, steward_home)
    world.alive["pane-lane-t2-1"] = True
    world.adapters[("lane-t2", 1)].stop_result = False
    world.kill_on_stop = True  # t1 really dies; t2's stop is refused and it keeps running
    res = ctl.stop(JOB, "s1")
    assert res["state"] == "stopping" and res["uncertain"] == ["lane-t2"]
    assert res["lanes"]["lane-t1"]["status"] == "confirmed" and res["lanes"]["lane-t2"]["status"] == "uncertain"
    assert world.alive["pane-lane-t2-1"] is True
    lease = fl.get(store, JOB, "lane-t2")
    assert lease["state"] == "uncertain" and lease["epoch"] == 1
    import pytest
    with pytest.raises(fl.LeaseHeld):
        fl.acquire_lane(store, JOB, "lane-t2")
    with pytest.raises(fl.LeaseHeld):
        fl.acquire_integration(store, JOB, holder="i")
    assert len(world.started) == 2
    # evidence is durable and visible in the fleet event history
    ev = [e for e in ctl.fleet.fleet_events(JOB) if e["what"] == "stop_unconfirmed"]
    assert ev and ev[-1]["lane_id"] == "lane-t2"
    # a *new* request retries the stop; once the process is really gone the parent reaches stopped
    world.adapters[("lane-t2", 1)].stop_result = True
    world.alive["pane-lane-t2-1"] = False
    res2 = ctl.stop(JOB, "s2")
    assert res2["state"] == "stopped" and fl.live(store, JOB) == []


def test_repeated_request_ids_return_the_original_receipt_without_new_actions(steward_home, tmp_path):
    store, repo, ctl, world = running_pair(tmp_path, steward_home)
    r1 = ctl.stop(JOB, "s1")
    calls = list(world.stop_calls)
    assert ctl.stop(JOB, "s1") == r1 and world.stop_calls == calls
    assert len(fl.live(store, JOB)) == 0


def test_recovery_action_ids_are_idempotent(steward_home, tmp_path):
    store, repo, ctl, world = running_pair(tmp_path, steward_home)
    a = ctl.recover_lane(JOB, "lane-t1", "act-1", reason="stalled")
    assert a["status"] == "started"
    n = len(world.started)
    assert ctl.recover_lane(JOB, "lane-t1", "act-1", reason="stalled") == a
    assert len(world.started) == n
    assert fl.recoveries(store, JOB)["lanes"] == {"lane-t1": 1}


# ----------------------------------------------------------------------------- restart

def test_restart_reattaches_exact_live_identity_and_never_duplicates(steward_home, tmp_path):
    store, repo, ctl, world = running_pair(tmp_path, steward_home)
    store2, ctl2 = restart(tmp_path, steward_home, repo, world)
    out = ctl2.reconcile(JOB)
    assert sorted(out["reattached"]) == ["lane-t1", "lane-t2"] and not out["fenced"] and not out["ambiguous"]
    assert world.adapters[("lane-t1", 1)] is not None
    assert ctl2.adapter(JOB, "lane-t1").attached_to == "pane-lane-t1-1"
    assert ctl2.admit(JOB) == [] or all(r["status"] != "started" for r in ctl2.admit(JOB))
    assert len(world.started) == 2 and len(fl.live(store2, JOB)) == 2
    # the reattached lane is controllable
    assert ctl2.stop(JOB, "s")["state"] == "stopped"


def test_restart_fences_a_confirmed_dead_attempt_before_replacement(steward_home, tmp_path):
    store, repo, ctl, world = running_pair(tmp_path, steward_home)
    world.alive["pane-lane-t1-1"] = False
    store2, ctl2 = restart(tmp_path, steward_home, repo, world)
    out = ctl2.reconcile(JOB)
    assert out["fenced"] == ["lane-t1"] and out["reattached"] == ["lane-t2"]
    lease = fl.get(store2, JOB, "lane-t1")
    assert lease["state"] == "stopped" and lease["stop_evidence"]["reason"] == "restart_probe_dead"
    rep = ctl2.tick(JOB)
    assert [r["status"] for r in rep["recovered"]] == ["started"]
    assert fl.get(store2, JOB, "lane-t1")["epoch"] == 2
    assert sum(1 for s in world.started if s[0] == "lane-t1") == 2  # one original + one replacement


def test_restart_with_ambiguous_identity_blocks_dispatch_until_resolved(steward_home, tmp_path):
    store, repo, ctl, world = running_pair(tmp_path, steward_home)
    world.probe_override[(JOB, "lane-t1")] = rt.UNKNOWN
    store2, ctl2 = restart(tmp_path, steward_home, repo, world)
    out = ctl2.reconcile(JOB)
    assert out["ambiguous"] == ["lane-t1"] and out["reattached"] == ["lane-t2"]
    assert fl.get(store2, JOB, "lane-t1")["state"] == "uncertain"
    assert ctl2.admit(JOB)[0]["reason"] == "dispatch_blocked_ambiguous_identity"
    assert ctl2.recover_lane(JOB, "lane-t1", "a", reason="x")["reason"] == "stop_unconfirmed"
    assert len(world.started) == 2  # still no duplicate writer
    # positively resolved: the process is confirmed dead -> fenced, dispatch unblocked
    world.probe_override[(JOB, "lane-t1")] = rt.DEAD
    out = ctl2.reconcile(JOB)
    assert out["fenced"] == ["lane-t1"] and ctl2._kv(JOB, "fleet_dispatch_blocked", None) is None


def test_restart_reconciles_a_reserved_but_unbound_launch(steward_home, tmp_path):
    store, repo, _ = seed(tmp_path, steward_home, TWO, max_lanes=2)
    from steward.fleet_store import FleetStore
    FleetStore(store).record_lane(JOB, "lane-t1", task_id="t1")
    fl.acquire_lane(store, JOB, "lane-t1")  # crash between reservation and bind
    ctl, world = controller(tmp_path, store, repo)
    out = ctl.reconcile(JOB)
    assert out["ambiguous"] == ["lane-t1"]  # no identity to probe: never launched a replacement
    assert ctl.admit(JOB)[0]["status"] == "blocked"
    world.probe_override[(JOB, "lane-t1")] = rt.DEAD
    assert ctl.reconcile(JOB)["fenced"] == ["lane-t1"]
    assert fl.get(store, JOB, "lane-t1")["state"] == "stopped"


# ----------------------------------------------------------------------------- bounded recovery

def test_per_lane_recovery_is_capped_at_two_then_lane_is_exhausted(steward_home, tmp_path):
    tasks = [task("t1", owns=["a/"]), task("t2", owns=["b/"]), task("t3", owns=["c/"], depends_on=["t1"])]
    store, repo, ctl, world = running_pair(tmp_path, steward_home, tasks=tasks, extra_limits={"job_recovery_ceiling": 9})
    assert ctl.recover_lane(JOB, "lane-t1", "a1", reason="x")["status"] == "started"
    assert ctl.recover_lane(JOB, "lane-t1", "a2", reason="x")["status"] == "started"
    third = ctl.recover_lane(JOB, "lane-t1", "a3", reason="x")
    assert (third["status"], third["reason"]) == ("exhausted", "lane_recovery_exhausted")
    assert fl.recoveries(store, JOB)["lanes"]["lane-t1"] == 2
    assert fl.task_states(store, JOB)["t1"] == "exhausted"
    # descendants of the exhausted task stay blocked; the independent lane is untouched
    assert all(r.get("task") != "t3" for r in ctl.admit(JOB))
    assert fl.get(store, JOB, "lane-t2")["state"] == "running"
    assert not any(l["lane_id"] == "lane-t3" for l in fl.live(store, JOB))


def test_job_wide_ceiling_bounds_combined_recoveries(steward_home, tmp_path):
    store, repo, ctl, world = running_pair(tmp_path, steward_home, extra_limits={"job_recovery_ceiling": 3})
    assert ctl.recover_lane(JOB, "lane-t1", "a1", reason="x")["status"] == "started"
    assert ctl.recover_lane(JOB, "lane-t1", "a2", reason="x")["status"] == "started"
    assert ctl.recover_lane(JOB, "lane-t2", "b1", reason="x")["status"] == "started"
    blocked = ctl.recover_lane(JOB, "lane-t2", "b2", reason="x")
    assert (blocked["status"], blocked["reason"]) == ("exhausted", "job_recovery_ceiling")
    assert fl.recoveries(store, JOB)["job"] == 3
    assert fl.task_states(store, JOB)["t2"] == "exhausted" and "t1" not in fl.task_states(store, JOB)


def test_tick_recovers_a_dead_lane_while_independent_lane_continues_and_is_model_free(steward_home, tmp_path):
    store, repo, ctl, world = running_pair(tmp_path, steward_home)
    world.alive["pane-lane-t1-1"] = False  # worker crashed
    rep = ctl.tick(JOB)
    assert [r["status"] for r in rep["recovered"]] == ["started"]
    assert fl.get(store, JOB, "lane-t1")["epoch"] == 2 and fl.get(store, JOB, "lane-t2")["epoch"] == 1
    # a second tick with healthy lanes spends nothing further
    rep2 = ctl.tick(JOB)
    assert rep2["recovered"] == [] and fl.recoveries(store, JOB)["job"] == 1


def test_finished_lane_is_stopped_and_frees_its_slot_for_a_dependent(steward_home, tmp_path):
    tasks = [task("t1", owns=["a/"]), task("t2", owns=["b/"], depends_on=["t1"])]
    store, repo, ctl, world = running_pair(tmp_path, steward_home, tasks=tasks)
    assert [l["lane_id"] for l in fl.live(store, JOB)] == ["lane-t1"]  # t2 waits on t1
    from test_fleet_frames import f
    assert ctl.accept_frame(JOB, f("t1", "t1", type_="done", body={})).accepted
    rep = ctl.tick(JOB)
    assert fl.get(store, JOB, "lane-t1")["state"] == "stopped"
    assert [r["status"] for r in rep["admitted"]] == ["started"]
    assert [l["lane_id"] for l in fl.live(store, JOB)] == ["lane-t2"]


def test_resumed_dispatch_revalidates_roster_qualification(steward_home, tmp_path):
    store, repo, ctl, world = running_pair(tmp_path, steward_home, tasks=[task("t1", owns=["a/"])])
    ctl.fleet.record_assignment(JOB, "lane-t1", roster_id="claude-sonnet", actor="t", reason="r", policy_rule="p")
    ctl.pause(JOB, "p")
    ctl.fleet.record_qualification("claude-code", "1.0", ["edit"], qualified=False, available=True)
    ctl.resume(JOB, "r")
    out = ctl.recover_lane(JOB, "lane-t1", "a", reason="x")
    assert out["start"]["reason"] == "roster_unqualified" and out["start"]["status"] == "skipped"
    assert len(world.started) == 1
