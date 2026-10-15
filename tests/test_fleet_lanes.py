"""Phase 27 (FLEET-06/07/08): one controller, isolated lanes, transactional epoch fencing."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest
from fixtures.repos import python_repo
from test_fleet_graph import BLOB, manifest, task

from steward import fleet_graph as fg
from steward import fleet_lease as fl
from steward import fleet_registry as fr
from steward import fleet_runtime as rt
from steward.fleet_store import FleetStore
from steward.runner_exec import SubprocessRunner
from steward.store import Store
from steward.worker import DeliveryResult, WorkerObservation

JOB = "J1"


class LaneAdapter:
    """Scripted lane worker: records calls, never spawns anything."""

    def __init__(self, world, job_id, lane_id, task_id, attempt_no, epoch, lease_check=None):
        self.world, self.job_id, self.lane_id = world, job_id, lane_id
        self.task_id, self.attempt_no, self.epoch, self.lease_check = task_id, attempt_no, epoch, lease_check
        self.started = self.stopped = False
        self.delivered, self.interrupted, self.events = [], 0, []
        self.stop_result = True
        self.attached_to = None
        self.worktree = None

    def start(self, brief, worktree):
        self.brief, self.worktree, self.started = brief, Path(worktree), True
        self.ref = f"pane-{self.lane_id}-{self.attempt_no}"
        self.world.alive[self.ref] = True
        self.world.started.append((self.lane_id, self.attempt_no, self.epoch))
        return self.ref

    def process_identity(self):
        return {"pgid": 1000 + self.epoch}

    def attach(self, ref):
        self.attached_to = self.ref = ref
        return self.world.attachable.get(ref, True)

    def deliver(self, text, *, urgent=False):
        self.delivered.append(text)
        return DeliveryResult(ok=True, detail="delivered")

    def interrupt(self):
        self.interrupted += 1

    def stop(self):
        self.stopped = True
        self.world.stop_calls.append(self.lane_id)
        if self.stop_result and self.world.kill_on_stop:
            self.world.alive[self.ref] = False
        return self.stop_result

    def observe(self):
        events, self.events = tuple(self.events), []
        life = "exited" if not self.world.alive.get(getattr(self, "ref", None), True) else "working"
        return WorkerObservation(lifecycle=life, output_tail="", pid_alive=life != "exited", ts="t", events=events)

    def answer_prompt(self, approve, request_id=None):
        return DeliveryResult(ok=True, detail="ok")


class World:
    def __init__(self):
        self.alive, self.started, self.stop_calls = {}, [], []
        self.attachable = {}
        self.kill_on_stop = True
        self.adapters = {}
        self.probe_override = {}

    def factory(self, *, job_id, lane_id, task_id, attempt_no, epoch, lease_check=None):
        a = LaneAdapter(self, job_id, lane_id, task_id, attempt_no, epoch, lease_check)
        self.adapters[(lane_id, attempt_no)] = a
        return a

    def probe(self, job_id, lane_id, identity):
        if (job_id, lane_id) in self.probe_override:
            return self.probe_override[(job_id, lane_id)]
        ref = (identity or {}).get("ref")
        if ref is None:
            return rt.UNKNOWN
        return rt.ALIVE if self.alive.get(ref) else rt.DEAD


def seed(tmp_path, home, tasks, *, max_lanes=1, implicit=False, extra_limits=None):
    """Real Store + fixture git repo + validated graph + frozen snapshot. Returns (store, repo, graph)."""
    repo = python_repo(tmp_path, "src_repo")
    store = Store(home)
    store.create_job("goal", str(repo), job_id=JOB)
    fs = FleetStore(store)
    roster = fr.validate_roster({"schema_version": 1, "revision": 1, "entries": [{
        "id": "claude-sonnet", "provider": "anthropic", "runtime": "claude-code", "model": "m",
        "roles": ["worker"], "capabilities": ["edit"], "strengths": [], "enabled": True, "label": "L"}]})
    fs.record_roster(roster)
    fs.record_qualification("claude-code", "1.0", ["edit"], qualified=True, available=True)
    fs.snapshot_job(roster, job_id=JOB, pins={}, policy="default", tech_lead=None,
                    limits={"max_lanes": max_lanes, **(extra_limits or {})})
    if implicit:
        g = fg.compile_graph(None, committed_plan_blob_id=BLOB, goal="goal")
    else:
        g = fg.compile_graph(manifest(*tasks), committed_plan_blob_id=BLOB, goal="goal")
    fs.record_graph(JOB, 1, g)
    assert fs.set_current_graph(JOB, 1, expected_version=0)
    return store, repo, g


def controller(tmp_path, store, repo, world=None):
    world = world or World()
    ctl = rt.JobController(store, repo, SubprocessRunner(), world.factory, base_dir=tmp_path / "wts",
                           probe=world.probe, controller_id="ctl-1")
    return ctl, world


TWO = [task("t1", owns=["src/a/"]), task("t2", owns=["src/b/"])]


def lease_of(store, lane):
    return fl.get(store, JOB, lane)


# --------------------------------------------------------------------- tracer: two owned tasks, two lanes

def test_two_disjoint_tasks_dispatch_into_distinct_fenced_worktrees(steward_home, tmp_path):
    store, repo, _ = seed(tmp_path, steward_home, TWO, max_lanes=2)
    ctl, world = controller(tmp_path, store, repo)
    res = ctl.admit(JOB)
    assert [r["status"] for r in res] == ["started", "started"]
    wts = {r["lane"]: Path(r["worktree"]) for r in res}
    assert len(set(wts.values())) == 2 and all(p.is_dir() for p in wts.values())
    # same pinned base, distinct branches (never the parent branch)
    heads = {l: subprocess.run(["git", "rev-parse", "HEAD"], cwd=p, capture_output=True, text=True).stdout.strip()
             for l, p in wts.items()}
    assert len(set(heads.values())) == 1 == len({ctl.base_sha()})
    branches = {l: subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=p, capture_output=True,
                                  text=True).stdout.strip() for l, p in wts.items()}
    assert branches == {"lane-t1": f"steward-lanes/{JOB}/lane-t1", "lane-t2": f"steward-lanes/{JOB}/lane-t2"}
    # one live attempt each, own persisted epoch, state survives a fresh Store handle
    fresh = Store(steward_home)
    for lane in wts:
        lease = fl.get(fresh, JOB, lane)
        assert lease["state"] == "running" and lease["epoch"] == 1 and lease["attempt_no"] == 1
        assert lease["identity"]["ref"].startswith("pane-")
    assert sorted(world.started) == [("lane-t1", 1, 1), ("lane-t2", 1, 1)]
    attempts = fresh._conn.execute("SELECT lane_id, attempt_no FROM fleet_attempts WHERE job_id=?", (JOB,)).fetchall()
    assert len(attempts) == 2


FIVE = [task(f"t{i}", owns=[f"{c}/"]) for i, c in zip(range(1, 6), "abcde")]


def test_default_is_one_lane_and_cap_is_four_even_if_policy_asks_for_more(steward_home, tmp_path):
    """v2.5 (FLEET-31): the hard cap rose 2 -> 4 only after the live multi-runtime proof; the default stays 1."""
    store, repo, _ = seed(tmp_path, steward_home, FIVE, max_lanes=1)
    ctl, _w = controller(tmp_path, store, repo)
    assert [r["status"] for r in ctl.admit(JOB)] == ["started", "skipped"]
    assert len(fl.live(store, JOB)) == 1

    assert fl.HARD_LANE_CAP == 4
    assert fl.effective_max_lanes(9) == 4 and fl.effective_max_lanes(None) == 1 and fl.effective_max_lanes(0) == 1


def test_policy_of_nine_lanes_still_runs_at_most_four(steward_home, tmp_path):
    store, repo, _ = seed(tmp_path, steward_home, FIVE, max_lanes=9)
    ctl, _w = controller(tmp_path, store, repo)
    ctl.admit(JOB)
    assert len(fl.live(store, JOB)) == 4
    assert ctl.limits(JOB)["max_lanes"] == 4
    # the lease layer enforces the cap on its own, not just admission
    FleetStore(store).record_lane(JOB, "extra", task_id="t5")
    with pytest.raises(fl.LeaseHeld, match="cap"):
        fl.acquire_lane(store, JOB, "extra", max_lanes=9)


def test_explicit_job_uses_snapshot_assignment_only_after_runtime_qualification(steward_home, tmp_path):
    from steward import fleet_registry as fr

    store, repo, _ = seed(tmp_path, steward_home, [task("t1", owns=["src/a/"])])
    store.kv_set(JOB, "fleet_mode_requested", True)
    rt.require_capabilities(store, JOB)
    ctl, _world = controller(tmp_path, store, repo)

    refused = ctl.admit(JOB)
    assert refused[0]["status"] == "skipped"
    assert refused[0]["reason"].startswith("capability_unqualified")
    assert FleetStore(store).assignments(JOB)
    assert store._conn.execute("SELECT COUNT(*) FROM fleet_lanes WHERE job_id=?", (JOB,)).fetchone()[0] == 0

    for capability in fr.RUNTIME_CAPABILITIES:
        FleetStore(store).record_runtime_capability("claude-code", capability, qualified=True,
                                                    adapter_version="1", evidence="isolated qualification fixture")
    started = ctl.admit(JOB)
    assert started[0]["status"] == "started"
    assert FleetStore(store).assignments(JOB, "lane-t1")[-1]["roster_id"] == "claude-sonnet"


def test_prose_only_job_runs_exactly_one_implicit_lane(steward_home, tmp_path):
    store, repo, g = seed(tmp_path, steward_home, [], implicit=True, max_lanes=2)
    assert g.implicit
    ctl, _w = controller(tmp_path, store, repo)
    res = ctl.admit(JOB)
    assert [r["status"] for r in res] == ["started"] and len(fl.live(store, JOB)) == 1


def test_legacy_job_without_fleet_graph_is_left_to_the_runner(steward_home, tmp_path):
    repo = python_repo(tmp_path, "r")
    store = Store(steward_home)
    store.create_job("g", str(repo), job_id="LEG")
    ctl, world = controller(tmp_path, store, repo)
    assert not rt.is_fleet_job(store, "LEG")
    assert ctl.admit("LEG") == [] and world.started == []


@pytest.mark.parametrize("owns_a,owns_b", [
    (["src/x/"], ["src/x/y.py"]),     # nested overlap
    ([], ["src/b/"]),                 # empty claim
    (["pyproject.toml"], ["src/b/"]),  # global/cross-cutting claim
])
def test_conflicting_or_empty_ownership_never_runs_concurrently(steward_home, tmp_path, owns_a, owns_b):
    tasks = [task("t1", owns=owns_a), task("t2", owns=owns_b)]
    store, repo, _ = seed(tmp_path, steward_home, tasks, max_lanes=2)
    ctl, _w = controller(tmp_path, store, repo)
    res = ctl.admit(JOB)
    assert [r["status"] for r in res] == ["started"]
    assert len(fl.live(store, JOB)) == 1


# --------------------------------------------------------------------- lease fencing

def _lane(store, lane="L", task_id="t"):
    FleetStore(store).record_lane(JOB, lane, task_id=task_id)


def test_epochs_increase_monotonically_and_only_confirmed_stop_allows_replacement(steward_home, tmp_path):
    store, repo, _ = seed(tmp_path, steward_home, TWO, max_lanes=2)
    _lane(store)
    l1 = fl.acquire_lane(store, JOB, "L")
    fl.bind(store, JOB, "L", l1["epoch"], {"ref": "p1"})
    with pytest.raises(fl.LeaseHeld):
        fl.acquire_lane(store, JOB, "L")
    # unconfirmed release keeps the lane fenced
    fl.release(store, JOB, "L", 1, confirmed=False, evidence={"probe": "unknown"})
    assert fl.get(store, JOB, "L")["state"] == "uncertain"
    with pytest.raises(fl.LeaseHeld):
        fl.acquire_lane(store, JOB, "L")
    fl.release(store, JOB, "L", 1, confirmed=True, evidence={"probe": "dead"})
    l2 = fl.acquire_lane(store, JOB, "L")
    assert (l2["epoch"], l2["attempt_no"]) == (2, 2)
    fl.release(store, JOB, "L", 2, confirmed=True, evidence={"probe": "dead"})
    assert fl.acquire_lane(store, JOB, "L")["epoch"] == 3


def test_confirmed_release_requires_evidence_and_stale_epoch_cannot_release_or_bind(steward_home, tmp_path):
    store, repo, _ = seed(tmp_path, steward_home, TWO)
    _lane(store)
    fl.acquire_lane(store, JOB, "L")
    with pytest.raises(ValueError):
        fl.release(store, JOB, "L", 1, confirmed=True, evidence={})
    fl.release(store, JOB, "L", 1, confirmed=True, evidence={"probe": "dead"})
    fl.acquire_lane(store, JOB, "L")  # epoch 2
    with pytest.raises(fl.LeaseFenced):
        fl.release(store, JOB, "L", 1, confirmed=True, evidence={"probe": "dead"})
    with pytest.raises(fl.LeaseFenced):
        fl.bind(store, JOB, "L", 1, {"ref": "x"})
    assert fl.get(store, JOB, "L")["state"] == "reserved"


def test_expiry_never_lets_a_live_or_unknown_writer_be_stolen(steward_home, tmp_path):
    store, repo, _ = seed(tmp_path, steward_home, TWO)
    _lane(store)
    lease = fl.acquire_lane(store, JOB, "L")
    fl.bind(store, JOB, "L", lease["epoch"], {"ref": "p"})
    fl.renew(store, JOB, "L", lease["epoch"], "1970-01-01T00:00:00Z")  # long expired
    with pytest.raises(fl.LeaseHeld):
        fl.acquire_lane(store, JOB, "L")
    assert fl.get(store, JOB, "L")["state"] == "running"


def test_concurrent_claims_serialize_in_sqlite(steward_home, tmp_path):
    store, repo, _ = seed(tmp_path, steward_home, TWO)
    _lane(store)
    results, barrier = [], threading.Barrier(6)

    def claim():
        s = Store(steward_home)
        barrier.wait()
        try:
            results.append(("ok", fl.acquire_lane(s, JOB, "L")["epoch"]))
        except fl.LeaseHeld:
            results.append(("held", None))

    threads = [threading.Thread(target=claim) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(r[0] for r in results) == ["held"] * 5 + ["ok"]
    assert [r[1] for r in results if r[0] == "ok"] == [1]
    assert store._conn.execute("SELECT lease_epoch FROM fleet_lanes WHERE lane_id='L'").fetchone()[0] == 1


def test_controller_replacement_after_unconfirmed_stop_is_refused_then_allowed(steward_home, tmp_path):
    store, repo, _ = seed(tmp_path, steward_home, TWO, max_lanes=2)
    ctl, world = controller(tmp_path, store, repo)
    ctl.admit(JOB)
    world.kill_on_stop = False  # process survives the stop request
    world.adapters[("lane-t1", 1)].stop_result = False
    out = ctl.recover_lane(JOB, "lane-t1", "a1", reason="stalled")
    assert out["status"] == "blocked" and out["reason"] == "stop_unconfirmed"
    assert lease_of(store, "lane-t1")["state"] == "uncertain" and lease_of(store, "lane-t1")["epoch"] == 1
    assert len(world.started) == 2  # no replacement was started
    world.alive["pane-lane-t1-1"] = False
    world.adapters[("lane-t1", 1)].stop_result = True
    out = ctl.recover_lane(JOB, "lane-t1", "a2", reason="stalled")
    assert out["status"] == "started" and out["start"]["epoch"] == 2


# --------------------------------------------------------------------- write containment / integration role

def test_lane_cannot_write_parent_or_sibling_worktree_or_claim_sibling_paths(steward_home, tmp_path):
    store, repo, _ = seed(tmp_path, steward_home, TWO, max_lanes=2)
    ctl, _w = controller(tmp_path, store, repo)
    res = {r["lane"]: Path(r["worktree"]) for r in ctl.admit(JOB)}
    assert ctl.authorize_write(JOB, "lane-t1", res["lane-t1"] / "src" / "a" / "f.py")
    assert not ctl.authorize_write(JOB, "lane-t1", res["lane-t2"] / "src" / "b" / "f.py")
    assert not ctl.authorize_write(JOB, "lane-t1", ctl.parent_worktree(JOB) / "x.py")
    assert not ctl.authorize_write(JOB, "lane-t1", repo / "x.py")
    assert not ctl.authorize_write(JOB, "lane-t1", res["lane-t1"] / ".." / "lane-t2" / "x")
    assert ctl.authorize_claim(JOB, "lane-t1", "src/a/new.py")
    assert not ctl.authorize_claim(JOB, "lane-t1", "src/b/new.py")


def test_integration_writer_lease_is_exclusive_and_blocked_by_uncertain_lane(steward_home, tmp_path):
    store, repo, _ = seed(tmp_path, steward_home, TWO, max_lanes=2)
    first = fl.acquire_integration(store, JOB, holder="integrator-1")
    with pytest.raises(fl.LeaseHeld):
        fl.acquire_integration(store, JOB, holder="integrator-2")
    fl.release_integration(store, JOB, first["epoch"], confirmed=True, evidence={"stopped": True})
    assert fl.acquire_integration(store, JOB, holder="integrator-2")["epoch"] == 2
    fl.release_integration(store, JOB, 2, confirmed=True, evidence={"stopped": True})
    _lane(store)
    fl.acquire_lane(store, JOB, "L")
    fl.release(store, JOB, "L", 1, confirmed=False, evidence={"probe": "unknown"})
    with pytest.raises(fl.LeaseHeld, match="blocked"):
        fl.acquire_integration(store, JOB, holder="integrator-3")


def test_lane_worktree_cleanup_is_identity_safe(steward_home, tmp_path):
    from steward import worktree as wt
    store, repo, _ = seed(tmp_path, steward_home, TWO, max_lanes=2)
    ctl, _w = controller(tmp_path, store, repo)
    res = {r["lane"]: Path(r["worktree"]) for r in ctl.admit(JOB)}
    runner = SubprocessRunner()
    # a directory squatting on the lane path with the wrong branch is never removed
    squat = wt.lane_path(tmp_path / "wts", JOB, "lane-zz")
    squat.mkdir()
    assert wt.remove_lane_worktree(repo, tmp_path / "wts", JOB, "lane-zz", runner) is False and squat.exists()
    assert wt.remove_lane_worktree(repo, tmp_path / "wts", JOB, "lane-t1", runner) is True
    assert not res["lane-t1"].exists() and res["lane-t2"].exists()
    # idempotent create for an existing, matching lane worktree
    again = wt.create_lane_worktree(repo, JOB, "lane-t2", runner, tmp_path / "wts", base_sha=ctl.base_sha())
    assert again == res["lane-t2"]
