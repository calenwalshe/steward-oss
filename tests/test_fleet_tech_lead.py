"""Phase 29-01 task 1 (FLEET-15/16/17): bounded tech-lead proposals through the sole JobController arbiter.

Real Store + temp Git + the Phase 27/28 controller, writer and closeout; fake lane adapters and fake advisors."""

from __future__ import annotations

import copy
import json
import sqlite3
import subprocess

import pytest
from fixtures.repos import python_repo
from test_fleet_closeout import disc
from test_fleet_graph import BLOB, manifest, task
from test_fleet_integration import commit_in, git
from test_fleet_lanes import JOB, World

from steward import fleet_closeout as fc
from steward import fleet_graph as fg
from steward import fleet_integration as fi
from steward import fleet_lease as fl
from steward import fleet_registry as fr
from steward import fleet_runtime as rt
from steward import fleet_tech_lead as tl
from steward.fabric import Frame
from steward.fleet_store import FleetStore
from steward.models import JobState, Judgment
from steward.runner_exec import SubprocessRunner
from steward.steering import Steering
from steward.store import Store

TWO = [task("t1", owns=["src/a/"]), task("t2", owns=["src/b/"])]
ALL_CAPS = list(fr.RUNTIME_CAPABILITIES)


def test_astra_lead_invocation_pins_medium_effort(monkeypatch):
    entry = fr.RosterEntry(id="astra-tech-lead", provider="openai", runtime="codex", model="gpt-6-astra",
                           roles=("tech_lead",), capabilities=("plan",), strengths=(), enabled=True, label="Astra")
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["cwd"] = kwargs["cwd"]
        from pathlib import Path
        Path(argv[argv.index("--output-last-message") + 1]).write_text('{"ok": true}')
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(tl.subprocess, "run", fake_run)
    assert tl.invoke_via_runtime(entry, "lead prompt", 10) == ('{"ok": true}', None)
    assert seen["argv"][seen["argv"].index("-m") + 1] == "gpt-6-astra"
    assert seen["argv"][seen["argv"].index("-c") + 1] == "model_reasoning_effort=medium"
    assert seen["argv"][seen["argv"].index("--sandbox") + 1] == "read-only"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def entry(rid, runtime, model, roles=("worker",)):
    return {"id": rid, "provider": "p-" + runtime, "runtime": runtime, "model": model, "roles": list(roles),
            "capabilities": ["edit"], "strengths": [], "enabled": True, "label": rid}


class ActiveRollout:
    """Phase 29-02: ordinary proposal authority is shadow unless reviewed rollout evidence exists. The 29-01
    arbiter tests exercise the arbiter itself, so they inject an always-active gate (the real gate is proven in
    test_fleet_learning_replay)."""

    def authorize(self, job_id, n_actions, **_):
        return {"mode": "active", "allowed": True, "reasons": []}

    def consume_trial(self, job_id, n):
        pass

    def kill_switch_active(self):
        return False


class TL:
    """Phase 28-ready world: two disjoint tasks, a two-runtime roster, integration writer, fake lane adapters."""

    def __init__(self, tmp_path, home, *, tasks=TWO, pins=None, qualify=("claude-code", "codex"), max_lanes=2,
                 limits=None):
        self.clock = Clock()
        repo = python_repo(tmp_path, "src_repo")
        self.store = Store(home)
        self.store.create_job("goal", str(repo), job_id=JOB)
        self.fs = FleetStore(self.store)
        roster = fr.validate_roster({"schema_version": 1, "revision": 1, "entries": [
            entry("claude-sonnet", "claude-code", "m1"), entry("codex-gpt", "codex", "m2")]})
        self.fs.record_roster(roster)
        for runtime in ("claude-code", "codex"):
            self.fs.record_qualification(runtime, "1.0", ["edit"], qualified=True, available=True)
        for runtime in qualify:
            for cap in ALL_CAPS:
                self.fs.record_runtime_capability(runtime, cap, qualified=True, adapter_version="1", evidence="probe")
        self.snap = self.fs.snapshot_job(roster, job_id=JOB, pins=pins or {}, policy="default", tech_lead=None,
                                         limits={"max_lanes": max_lanes, **(limits or {})})
        g = fg.compile_graph(manifest(*tasks), committed_plan_blob_id=BLOB, goal="goal")
        self.fs.record_graph(JOB, 1, g)
        assert self.fs.set_current_graph(JOB, 1, expected_version=0)
        self.world = World()
        self.ctl = rt.JobController(self.store, repo, SubprocessRunner(), self.world.factory,
                                    base_dir=tmp_path / "wts", probe=self.world.probe, controller_id="ctl-1",
                                    clock=self.clock, settle_s=300.0, rollout=ActiveRollout())
        self.writer = fi.IntegrationWriter(self.store, repo, SubprocessRunner(), base_dir=tmp_path / "wts",
                                           base_sha=self.ctl.base_sha())
        self.ctl.integration = self.writer
        self.repo = repo
        self.wts, self.seq, self.n = {}, 0, 0

    def admit(self):
        for t in self.fs.graph_view(JOB)["tasks"]:  # the dispatch policy's initial assignment gives each lane an identity
            if not self.fs.assignments(JOB, rt.lane_id_for(t["id"])):
                self.fs.record_assignment(JOB, rt.lane_id_for(t["id"]), roster_id="claude-sonnet", actor="policy",
                                          reason="initial", policy_rule="default")
        for r in self.ctl.admit(JOB):
            if r["status"] == "started":
                self.wts[r["task"]] = r["worktree"]

    def lease(self, task_id):
        return fl.get(self.store, JOB, rt.lane_id_for(task_id))

    def adapter(self, task_id):
        lease = self.lease(task_id)
        return self.world.adapters[(rt.lane_id_for(task_id), lease["attempt_no"])]

    def frame(self, task_id, type_, body):
        lease = self.lease(task_id)
        lane = rt.lane_id_for(task_id)
        runtime = (self.fs.lane_runtime(JOB, lane) or {}).get("runtime")
        sender = f"codex:{lane}" if runtime == "codex" else f"shim:{lane}"
        self.seq += 1
        return Frame(v=2, seq=self.seq, job=JOB, type=type_, body=body, task=task_id, lane=rt.lane_id_for(task_id),
                     attempt=lease["attempt_no"], sender=sender, epoch=lease["epoch"])

    def finish(self, task_id, files):
        sha = commit_in(self.wts[task_id], files)
        assert self.ctl.accept_frame(JOB, self.frame(task_id, "done", {"candidate_sha": sha, "evidence_ref": "ev:1"})).accepted
        self.ctl.tick(JOB)
        return sha

    def proposal(self, actions, *, pid=None, **over):
        self.n += 1
        b = self.ctl.bindings(JOB)
        body = {"proposal_id": pid or f"p{self.n}", "trigger_id": "trg-1",
                "observed": {k: b[k] for k in ("contract", "graph_rev", "registry_hash", "steer_rev")},
                "evidence_refs": ["ev:1"], "rationale": "because", "actions": actions}
        body.update(over)
        return body

    def apply(self, actions, **kw):
        return self.ctl.apply_proposal(JOB, self.proposal(actions, **kw))

    def deliveries(self):
        return sum(len(a.delivered) for a in self.world.adapters.values())

    def assignments(self):
        """Assignments made by the tech-lead path (the policy's initial ones are not advice)."""
        return [a for a in self.fs.assignments(JOB) if a["actor"] == "tech_lead"]


def steer_action(task_id="t1", text="focus on the importer", key="a1"):
    return {"type": "steer", "key": key, "target": task_id, "params": {"verb": "focus", "text": text}}


@pytest.fixture
def w(steward_home, tmp_path):
    e = TL(tmp_path, steward_home)
    e.admit()
    return e


# ------------------------------------------------------------------------------------------------ tracer

def test_material_event_to_one_verified_candidate_through_the_sole_arbiter(steward_home, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "auto")
    e = TL(tmp_path, steward_home)
    e.admit()
    seen = []

    def advisor(packet):
        seen.append(packet)
        return e.proposal([steer_action("t1")], trigger_id=packet["events"][0]["ref"].replace(":", "-"))

    sup = tl.TechLeadSupervisor(e.store, e.ctl, advisor, clock=e.clock)
    res = sup.on_event(JOB)
    assert res["status"] == "applied", res
    assert e.deliveries() == 1
    pkt = seen[0]
    assert pkt["observed"]["graph_rev"] == 1 and {x["task"] for x in pkt["lanes"]} == {"t1", "t2"}
    assert json.dumps(pkt)  # plain data only: no adapter handles
    assert not any(hasattr(v, "deliver") for v in vars(sup).values() if v is not e.ctl)
    receipt = e.fs.proposal(JOB, res["receipt"]["proposal_id"])
    assert receipt["outcome"] == "applied" and receipt["bindings"]["observed"]["graph_rev"] == 1
    assert receipt["bindings"]["observed"]["contract"] == e.ctl.contract_digest(JOB)
    ev = [x for x in e.fs.fleet_events(JOB) if x["what"] == "tech_lead_proposal_applied"][0]
    assert ev["judgment"] == Judgment.model_informed_code_authorized.value  # SP-18, not code-only
    # worker acks, both lanes finish, serial integration, combined verification, one exact-SHA candidate
    rev = e.store.get_revision(JOB)
    assert e.ctl.accept_frame(JOB, e.frame("t1", "ack", {"rev": rev, "stage": "acknowledged"})).accepted
    assert e.ctl.receipts(JOB)[f"{res['receipt']['proposal_id']}:a1:lane-t1"]["stage"] == "acknowledged"
    e.finish("t1", {"src/a/a.py": "a\n"})
    e.finish("t2", {"src/b/b.py": "b\n"})
    e.writer.integrate(JOB)
    e.store.add_check(JOB, commit_sha=e.ctl.base_sha(), command="true", exit_code=0, results={}, label="baseline")
    for st in (JobState.CONTRACTED, JobState.RUNNING, JobState.WORKER_FINISHED):
        e.store.set_state(JOB, st, what="x", evidence={}, rule="t", judgment=Judgment.code)
    closer = fc.FleetCloseout(e.store, SubprocessRunner(), base_dir=e.writer.base_dir, base_sha=e.ctl.base_sha(),
                              discover=disc("true"))
    out = closer.run(JOB)
    cand = fc.final_candidate(e.store, JOB)
    head = git(e.writer.integration_path(JOB), "rev-parse", "HEAD")
    assert out["status"] == "candidate" and out["state"] == "NEEDS_HUMAN"
    assert cand["final_sha"] == head == cand["verified_sha"] and len(fc.candidates(e.store, JOB)) == 1


# ------------------------------------------------------------------------------------------------ authority

@pytest.mark.parametrize("bad,reason", [
    ({"type": "stop", "key": "x"}, "forbidden_action"),
    ({"type": "accept", "key": "x"}, "forbidden_action"),
    ({"type": "release", "key": "x"}, "forbidden_action"),
    ({"type": "incorporate", "key": "x"}, "forbidden_action"),
    ({"type": "notify", "key": "x", "params": {"to": "someone"}}, "forbidden_action"),
    ({"type": "set_deadline", "key": "x"}, "forbidden_action"),
    ({"type": "teleport", "key": "x"}, "unknown_action"),
])
def test_no_operation_exists_for_stop_accept_release_incorporate_or_contact(w, bad, reason):
    res = w.apply([bad])
    assert res["outcome"] == "malformed" and reason in res["reason"]
    assert w.deliveries() == 0 and w.assignments() == [] and w.ctl.fence(JOB) is None
    assert w.store.get_job(JOB).state == JobState.CREATED
    assert fl.get(w.store, JOB, "lane-t1")["state"] == "running"


@pytest.mark.parametrize("field", ["goal", "deadline", "grants", "permissions", "scope", "pins", "limits",
                                   "required_outcomes", "authority"])
def test_goal_deadline_grants_permissions_scope_pins_limits_are_immutable_to_advice(w, field):
    body = w.proposal([steer_action()])
    body[field] = "changed"
    assert w.ctl.apply_proposal(JOB, body)["outcome"] == "malformed"  # unknown top-level field
    body = w.proposal([steer_action()])
    body["actions"][0]["params"][field] = "changed"
    res = w.ctl.apply_proposal(JOB, body)
    assert res["outcome"] == "malformed" and "immutable_field" in res["reason"]
    assert w.deliveries() == 0 and w.fs.get_snapshot(JOB)["pins"] == {}


def test_proposal_bounds_and_malformed_inputs(w):
    many = [steer_action(key=f"k{i}") for i in range(tl.MAX_ACTIONS + 1)]
    assert "too_many_actions" in w.apply(many)["reason"]
    assert "bad_field" in w.apply([steer_action(text="x" * (tl.MAX_TEXT + 1))])["reason"]
    assert w.ctl.apply_proposal(JOB, "{not json")["outcome"] == "malformed"
    assert w.ctl.apply_proposal(JOB, ["a"])["outcome"] == "malformed"
    assert w.ctl.apply_proposal(JOB, {"proposal_id": "p"})["outcome"] == "malformed"
    assert "duplicate_action_key" in w.apply([steer_action(key="k"), steer_action("t2", key="k")])["reason"]
    assert "bad_params" in w.apply([{"type": "steer", "key": "k", "target": "t1",
                                     "params": {"verb": "interrupt", "text": "hi"}}])["reason"]
    assert w.deliveries() == 0
    # every malformed input left a durable, auditable receipt
    assert all(p["outcome"] == "malformed" for p in w.fs.proposals(JOB)) and len(w.fs.proposals(JOB)) >= 6


def test_receipts_are_insert_only(w):
    w.apply([{"type": "bogus", "key": "k"}])
    with pytest.raises(sqlite3.DatabaseError):
        w.store._conn.execute("UPDATE fleet_proposals SET outcome='applied'")
    with pytest.raises(sqlite3.DatabaseError):
        w.store._conn.execute("DELETE FROM fleet_proposals")


# ------------------------------------------------------------------------------------------------ stale advice

def test_stale_advice_after_a_human_steer_is_discarded_with_no_effect(w):
    body = w.proposal([steer_action()])  # packet observed steer revision 0
    Steering(w.store).record(JOB, "nudge", "owner says: use the old importer", origin="cli")
    res = w.ctl.apply_proposal(JOB, body)
    assert res["outcome"] == "discarded_stale" and "steer_rev" in res["reason"]
    assert w.deliveries() == 0 and w.assignments() == []
    rec = w.fs.proposal(JOB, body["proposal_id"])
    assert rec["bindings"]["observed"]["steer_rev"] == 0 and rec["bindings"]["current"]["steer_rev"] == 1


def test_stale_graph_registry_contract_and_lease_revisions_are_discarded(w):
    body = w.proposal([steer_action()])
    for field, value in (("graph_rev", 99), ("registry_hash", "0" * 64), ("contract", "deadbeef")):
        b = copy.deepcopy(body)
        b["proposal_id"] = "p-" + field
        b["observed"][field] = value
        res = w.ctl.apply_proposal(JOB, b)
        assert res["outcome"] == "discarded_stale" and field in res["reason"]
    b = copy.deepcopy(body)
    b["proposal_id"] = "p-lease"
    b["observed"]["leases"] = {"lane-t1": 99}
    assert w.ctl.apply_proposal(JOB, b)["reason"] == "stale_revision: lease:lane-t1"
    assert w.deliveries() == 0


def test_an_owner_contract_change_invalidates_old_advice(w):
    from steward.contract import Contract, store_contract
    body = w.proposal([steer_action()])
    store_contract(w.store, JOB, Contract(goal="a different goal", done_when=["x"]))
    assert w.ctl.apply_proposal(JOB, body)["reason"].startswith("stale_revision: contract")


def test_a_pending_human_steer_blocks_redirects_even_with_current_revisions(w):
    Steering(w.store).record(JOB, "nudge", "owner nudge", origin="cli")
    res = w.apply([steer_action()])  # observed AFTER the human steer, so not stale
    assert res["outcome"] == "rejected" and res["actions"][0]["reason"] == "pending_unacked_steer"
    assert w.deliveries() == 0


# ------------------------------------------------------------------------------------------------ fail open

def test_unavailable_slow_malformed_or_absent_advice_never_blocks_authorized_execution(steward_home, tmp_path):
    e = TL(tmp_path, steward_home)
    outcomes = []
    for adv in (lambda p: (_ for _ in ()).throw(TimeoutError("advisor timed out")),
                lambda p: "{garbage", lambda p: None, None):
        sup = tl.TechLeadSupervisor(e.store, e.ctl, adv, clock=e.clock)
        e.clock.t += 10000
        e.store.kv_delete(JOB, "tech_lead_state")
        outcomes.append(sup.on_event(JOB, [{"kind": "graph_admitted", "ref": f"graph:{len(outcomes)}"}])["status"])
    assert outcomes == ["unavailable", "malformed", "no_proposal", "unavailable"]
    # deterministic execution is untouched: the controller still admits and runs the graph
    res = e.ctl.tick(JOB)
    assert [r["status"] for r in res["admitted"]] == ["started", "started"]
    whats = [x["what"] for x in e.fs.fleet_events(JOB)]
    assert "tech_lead_advice_unavailable" in whats and "tech_lead_proposal_malformed" in whats


def test_one_review_in_flight_and_each_trigger_reviewed_once(w):
    calls = []
    sup = tl.TechLeadSupervisor(w.store, w.ctl, lambda p: calls.append(p) or None, clock=w.clock)
    ev = [{"kind": "graph_admitted", "ref": "graph:1"}]
    sup.on_event(JOB, ev)
    assert sup.on_event(JOB, ev)["reason"] == "trigger_already_reviewed" and len(calls) == 1
    w.store.kv_set(JOB, "tech_lead_state", {"in_flight": {"trigger_id": "t", "at": w.clock()}})
    assert sup.on_event(JOB, [{"kind": "x", "ref": "y"}])["reason"] == "review_in_flight"
    w.clock.t += 301  # a crashed review expires
    assert sup.on_event(JOB, [{"kind": "x", "ref": "y"}])["status"] == "no_proposal"


def test_packets_are_bounded(steward_home, tmp_path):
    many = [task(f"t{i}", owns=[f"src/p{i}/"], objective="o" * 2000) for i in range(30)]
    e = TL(tmp_path, steward_home, tasks=many)
    pkts = []
    sup = tl.TechLeadSupervisor(e.store, e.ctl, lambda p: pkts.append(p), clock=e.clock,
                                cfg=tl.TechLeadConfig(max_list=10, max_text=100))
    sup.on_event(JOB)
    assert len(pkts[0]["graph"]["tasks"]) == 10 and all(len(t["objective"]) <= 100 for t in pkts[0]["graph"]["tasks"])


# ------------------------------------------------------------------------------------------------ actions

def test_assignment_is_validated_pinned_and_idempotent(steward_home, tmp_path):
    e = TL(tmp_path, steward_home, pins={"lane-t2": "claude-sonnet"})
    act = {"type": "assign", "key": "a", "target": "t1", "params": {"roster_id": "codex-gpt"}}
    body = e.proposal([act], pid="assign-1")
    res = e.ctl.apply_proposal(JOB, body)
    assert res["outcome"] == "applied"
    a = e.assignments()[0]
    assert (a["lane_id"], a["roster_id"], a["actor"], a["policy_rule"]) == ("lane-t1", "codex-gpt", "tech_lead",
                                                                         "tech_lead_proposal")
    again = e.ctl.apply_proposal(JOB, body)
    assert again["replayed"] and len(e.assignments()) == 1 and len(e.fs.proposals(JOB)) == 1
    # a human pin is untouchable
    pin = e.apply([{"type": "assign", "key": "p", "target": "t2", "params": {"roster_id": "codex-gpt"}}])
    assert pin["outcome"] == "rejected" and pin["actions"][0]["reason"] == "pinned"
    # the next dispatch uses the assigned model's exact identity
    e.admit()
    att = e.store._conn.execute("SELECT runtime, model FROM fleet_attempts WHERE lane_id='lane-t1'").fetchone()
    assert (att["runtime"], att["model"]) == ("codex", "m2")


def test_assignment_needs_eligible_worker_with_qualified_capabilities(steward_home, tmp_path):
    e = TL(tmp_path, steward_home, qualify=("claude-code",))  # codex has no capability evidence at all
    r = e.apply([{"type": "assign", "key": "a", "target": "t1", "params": {"roster_id": "codex-gpt"}}])
    assert r["actions"][0]["reason"].startswith("capability_unqualified: ")
    assert e.assignments() == []
    r = e.apply([{"type": "assign", "key": "b", "target": "t1", "params": {"roster_id": "ghost"}}])
    assert r["actions"][0]["reason"] == "not_eligible"


def test_batch_is_atomic_one_bad_action_means_no_effect_and_per_action_reasons(w):
    res = w.apply([steer_action("t1", key="good"),
                   {"type": "assign", "key": "bad", "target": "t2", "params": {"roster_id": "ghost"}}])
    assert res["outcome"] == "rejected"
    assert [(a["key"], a["status"]) for a in res["actions"]] == [("good", "validated"), ("bad", "rejected")]
    assert w.deliveries() == 0 and w.assignments() == []


def test_conflicting_actions_in_one_proposal_are_rejected(w):
    a1 = {"type": "assign", "key": "a", "target": "t1", "params": {"roster_id": "codex-gpt"}}
    a2 = {"type": "assign", "key": "b", "target": "t1", "params": {"roster_id": "claude-sonnet"}}
    res = w.apply([a1, a2])
    assert res["outcome"] == "rejected" and res["reason"].startswith("conflicting_actions")
    assert w.assignments() == []


def test_reassign_a_live_lane_requires_confirmed_stop_then_restarts_under_the_new_identity(w):
    old = w.adapter("t1")
    res = w.apply([{"type": "assign", "key": "r", "target": "t1", "params": {"roster_id": "codex-gpt"}}])
    assert res["outcome"] == "applied" and old.stopped
    lease = w.lease("t1")
    assert lease["attempt_no"] == 2 and lease["state"] == "running"
    row = w.store._conn.execute(
        "SELECT runtime FROM fleet_attempts WHERE lane_id='lane-t1' AND attempt_no=2").fetchone()
    assert row["runtime"] == "codex"
    # an unconfirmed stop means no assignment is recorded and no replacement exists
    w.clock.t += 1000
    w.world.kill_on_stop = False
    before = len(w.assignments())
    res = w.apply([{"type": "assign", "key": "r2", "target": "t1", "params": {"roster_id": "claude-sonnet"}}])
    assert res["outcome"] == "rejected" and res["actions"][0]["reason"] == "stop_unconfirmed"
    assert len(w.assignments()) == before and w.lease("t1")["state"] == "uncertain"


def test_split_replaces_an_unstarted_task_inside_its_own_scope_and_keeps_required_work(steward_home, tmp_path):
    e = TL(tmp_path, steward_home, tasks=[task("t1", owns=["src/a/"], done_when=["a works"]),
                                           task("t2", owns=["src/b/"], depends_on=["t1"])])
    kids = [{"id": "t1a", "objective": "part a", "done_when": ["a works"], "owns": ["src/a/x/"]},
            {"id": "t1b", "objective": "part b", "done_when": ["b ok"], "owns": ["src/a/y/"]}]
    res = e.apply([{"type": "split", "key": "s", "target": "t1", "params": {"children": kids}}])
    assert res["outcome"] == "applied"
    v = e.fs.graph_view(JOB)
    ids = {t["id"]: t for t in v["tasks"]}
    assert v["graph_rev"] == 2 and "t1" not in ids and {"t1a", "t1b"} <= set(ids)
    assert ids["t1a"]["required"] and ids["t1b"]["required"] and set(ids["t2"]["depends_on"]) == {"t1a", "t1b"}
    assert e.fs.current_graph(JOB)[0] == 2
    # the goal-side outcome is unchanged: every parent done_when is still covered
    assert "a works" in sum((t["done_when"] for t in v["tasks"]), [])
    row = e.store._conn.execute("SELECT parent_task FROM fleet_tasks WHERE graph_rev=2 AND task_id='t1a'").fetchone()
    assert row["parent_task"] == "t1"


@pytest.mark.parametrize("kids,reason", [
    ([{"id": "x", "objective": "o", "done_when": ["d"], "owns": ["src/other/"]},
      {"id": "y", "objective": "o", "done_when": ["d"], "owns": ["src/a/"]}], "child_claim_outside_parent_scope"),
    ([{"id": "x", "objective": "o", "done_when": ["other"], "owns": ["src/a/"]},
      {"id": "y", "objective": "o", "done_when": ["other2"], "owns": ["src/a/"]}], "children_do_not_cover_outcome"),
    ([{"id": "t2", "objective": "o", "done_when": ["d"], "owns": ["src/a/"]},
      {"id": "y", "objective": "o", "done_when": ["d"], "owns": ["src/a/"]}], "child_id_conflict"),
])
def test_split_that_leaves_scope_loses_outcome_or_collides_is_rejected(steward_home, tmp_path, kids, reason):
    e = TL(tmp_path, steward_home, tasks=[task("t1", owns=["src/a/"], done_when=["d"]), task("t2", owns=["src/b/"])])
    res = e.apply([{"type": "split", "key": "s", "target": "t1", "params": {"children": kids}}])
    assert res["outcome"] == "rejected" and res["actions"][0]["reason"].startswith(reason)
    assert e.fs.current_graph(JOB)[0] == 1


def test_split_of_a_started_task_is_rejected(w):
    kids = [{"id": "x", "objective": "o", "done_when": ["done"], "owns": ["src/a/"]},
            {"id": "y", "objective": "o", "done_when": ["done"], "owns": ["src/a/"]}]
    res = w.apply([{"type": "split", "key": "s", "target": "t1", "params": {"children": kids}}])
    assert res["actions"][0]["reason"] == "task_started"


def test_reorder_changes_which_ready_independent_task_dispatches_first(steward_home, tmp_path):
    e = TL(tmp_path, steward_home, max_lanes=1)
    res = e.apply([{"type": "reorder", "key": "r", "params": {"order": ["t2", "t1"]}}])
    assert res["outcome"] == "applied"
    assert [t["id"] for t in e.fs.graph_view(JOB)["tasks"]] == ["t2", "t1"]
    out = e.ctl.admit(JOB)
    assert out[0]["task"] == "t2" and out[0]["status"] == "started"


def test_reorder_rejects_dependent_or_started_tasks(steward_home, tmp_path):
    e = TL(tmp_path, steward_home, tasks=[task("t1", owns=["src/a/"]),
                                           task("t2", owns=["src/b/"], depends_on=["t1"])])
    res = e.apply([{"type": "reorder", "key": "r", "params": {"order": ["t2", "t1"]}}])
    assert res["actions"][0]["reason"] == "not_ready: t2"
    e.admit()
    res = e.apply([{"type": "reorder", "key": "r", "params": {"order": ["t1", "t1x"]}}], pid="p-other")
    assert res["outcome"] == "rejected"


def test_steering_is_scoped_and_waits_for_acknowledgement_before_the_next_redirect(w):
    res = w.apply([steer_action("t1")])
    assert res["outcome"] == "applied" and len(w.adapter("t1").delivered) == 1 and w.adapter("t2").delivered == []
    w.clock.t += 1000  # settle window over, but lane-t1's steer is still unacknowledged
    res = w.apply([steer_action("t1", text="another redirect", key="b")])
    assert res["actions"][0]["reason"] == "pending_unacked_steer: lane-t1"
    assert len(w.adapter("t1").delivered) == 1
    # a job-scoped steer reaches every live lane
    job_wide = {"type": "steer", "key": "j", "target": None, "params": {"verb": "nudge", "text": "keep tests green",
                                                                        "reach": "job"}}
    res = w.apply([job_wide])
    assert res["outcome"] == "rejected"  # lane-t1 is still unacked: the whole action is refused


def test_diagnosis_is_capped_to_one_per_lane_attempt_and_goes_to_the_doctor_seam(steward_home, tmp_path):
    e = TL(tmp_path, steward_home)
    e.admit()
    asked = []
    e.ctl.diagnose_fn = lambda job, lane, q: asked.append((lane, q)) or "diag-1"
    d = {"type": "diagnose", "key": "d", "target": "t1", "params": {"question": "why stuck?"}}
    assert e.apply([d])["outcome"] == "applied" and asked == [("lane-t1", "why stuck?")]
    e.clock.t += 1000
    res = e.apply([{**d, "key": "d2"}])
    assert res["actions"][0]["reason"] == "diagnosis_cap" and len(asked) == 1


def test_owner_decision_is_a_local_card_not_an_outbound_message(w):
    res = w.apply([{"type": "owner_decision", "key": "o", "params": {"question": "Drop feature X?",
                                                                      "options": ["yes", "no"]}}])
    assert res["outcome"] == "applied"
    card = w.ctl.owner_decisions(JOB)[0]
    assert card["status"] == "open" and card["options"] == ["yes", "no"]
    assert w.store.get_job(JOB).state == JobState.CREATED and w.deliveries() == 0


# ------------------------------------------------------------------------------------------------ arbiter

def test_one_direction_change_per_settle_window(w):
    first = w.apply([steer_action("t1")])
    assert first["outcome"] == "applied"
    w.clock.t += 60
    second = w.apply([{"type": "assign", "key": "a", "target": "t2", "params": {"roster_id": "codex-gpt"}}])
    assert second["outcome"] == "rejected" and second["reason"] == "settle_window" and w.assignments() == []
    w.clock.t += 400
    third = w.apply([{"type": "assign", "key": "a", "target": "t2", "params": {"roster_id": "codex-gpt"}}])
    assert third["outcome"] == "applied" and len(w.assignments()) == 1


def test_arbiter_routes_each_supervisor_by_authority(w):
    arb = w.ctl.arbiter
    assert arb.submit(JOB, "heartbeat", "progressing")["route"] == "inner"
    assert arb.submit(JOB, "recovery", "replace")["route"] == "inner"
    assert arb.submit(JOB, "jev", "flag")["route"] == "advisory"
    assert arb.submit(JOB, "efficiency", "finding")["route"] == "advisory"
    assert arb.submit(JOB, "heartbeat", "stall", lane="lane-t1")["route"] == "doctor"
    assert arb.pending_for_tech_lead(JOB) == []  # inner, advisory and a single stall never wake the tech lead
    arb.submit(JOB, "heartbeat", "stall", lane="lane-t1")
    assert [f["kind"] for f in arb.pending_for_tech_lead(JOB)] == ["repeated_stall"]
    arb.submit(JOB, "deadline", "deadline_risk", status="unlikely")
    assert {f["kind"] for f in arb.pending_for_tech_lead(JOB)} == {"repeated_stall", "deadline_risk"}
    assert arb.summary(JOB)["by_route"]["advisory"] == 2


def test_jev_and_efficiency_cannot_trigger_or_swap_models(w):
    w.ctl.arbiter.submit(JOB, "jev", "flag", suggest="swap to codex")
    w.ctl.arbiter.submit(JOB, "efficiency", "finding", suggest="downgrade model")
    calls = []
    sup = tl.TechLeadSupervisor(w.store, w.ctl, lambda p: calls.append(p), clock=w.clock, facts=w.ctl.arbiter)
    w.store.kv_set(JOB, "tech_lead_state", {"seen": {"graph_rev": 1, "candidates": 0, "integrated": [],
                                                      "integration_bad": 0}})
    assert sup.on_event(JOB)["reason"] == "no_material_event" and calls == []
    assert w.assignments() == []


def test_outer_fact_wakes_the_tech_lead_once_and_is_marked_reviewed(w):
    w.ctl.arbiter.submit(JOB, "deadline", "deadline_risk", status="unlikely")
    pkts = []
    sup = tl.TechLeadSupervisor(w.store, w.ctl, lambda p: pkts.append(p), clock=w.clock, facts=w.ctl.arbiter)
    assert sup.on_event(JOB)["status"] == "no_proposal"
    assert any(e["kind"] == "deadline_risk" for e in pkts[0]["events"]) and "by_route" in pkts[0]["facts"]
    assert w.ctl.arbiter.pending_for_tech_lead(JOB) == []


# ------------------------------------------------------------------------------------------------ runner wiring

def test_runner_tick_feeds_the_arbiter_and_wakes_the_tech_lead_for_a_fleet_job(steward_home, tmp_path):
    from steward.contract import Contract, store_contract
    from steward.diagnosis import Diagnosis, FakeDiagnoser
    from steward.recovery import RecoveryPolicy
    from steward.runner import Runner
    from steward.worker import FakeWorker
    e = TL(tmp_path, steward_home)
    store_contract(e.store, JOB, Contract(goal="goal", done_when=["x"]))
    e.store.set_state(JOB, JobState.RUNNING, what="x", evidence={}, rule="t", judgment=Judgment.code)
    seen = []
    sup = tl.TechLeadSupervisor(e.store, e.ctl, lambda p: seen.append(p), clock=e.clock, facts=e.ctl.arbiter)

    class Traj:
        called = 0

        def step(self, job):
            Traj.called += 1

    runner = Runner(e.store, lambda ref: FakeWorker(), SubprocessRunner(), FakeDiagnoser(Diagnosis(verdict='keep_going', guidance='', hypothesis='')),
                    policy=RecoveryPolicy(), trajectory=Traj())
    runner.fleet_controller, runner.tech_lead = e.ctl, sup
    runner._fleet_last_cls.clear()
    from steward import heartbeat
    action = type("A", (), {"kind": "replace"})()
    runner._fleet_outer_step(JOB, heartbeat.Class.suspect_stall, action)
    summary = e.ctl.arbiter.summary(JOB)
    assert summary["by_route"]["doctor"] == 1 and summary["by_route"]["inner"] == 1
    assert seen and seen[0]["events"][0]["kind"] == "graph_admitted"  # material event reached the advisor
    assert runner._tech_lead_owns_outer_loop(JOB) is True
    # legacy job: nothing is fed and the trajectory loop still owns the outer step
    e.store.create_job("legacy goal", "/repo", job_id="LEG")
    runner._fleet_outer_step("LEG", heartbeat.Class.progressing, action)
    assert e.ctl.arbiter.summary("LEG")["by_route"] == {} and runner._tech_lead_owns_outer_loop("LEG") is False


# ------------------------------------------------------------------- v2.5: ordered tech lead (FLEET-34) + shadow (FLEET-35)

LEADS = [fr.RosterEntry(id="astra-tech-lead", provider="openai", runtime="codex", model="gpt-6-astra",
                        roles=("tech_lead",), capabilities=("plan", "review"), strengths=(), enabled=True,
                        label="Astra", priority=1),
         fr.RosterEntry(id="fable-tech-lead", provider="anthropic", runtime="claude-fabric", model="claude-fable-5-1",
                        roles=("tech_lead",), capabilities=("plan", "review"), strengths=(), enabled=True,
                        label="Fable", priority=2)]


def jev_fact(w, kind="lanes_overlapping", lane="lane-t1", mode="shadow"):
    return w.ctl.arbiter.submit(JOB, "jev_fleet", kind, lane=lane, confidence=0.9, trigger_mode=mode)


def packet_with_fact(w, lane="lane-t1"):
    f = jev_fact(w, lane=lane)
    sup = tl.TechLeadSupervisor(w.store, w.ctl, None, clock=w.clock, facts=w.ctl.arbiter)
    events = [e for e in sup.material_events(JOB) if e["ref"].startswith("fact:")]
    return sup.packet(JOB, events), f


def answer(w, steer_text="keep NOTES.md edits in one lane"):
    return json.dumps(w.proposal([steer_action(text=steer_text)]))


def lead(w, plan, **kw):
    """plan: entry id -> callable(prompt) returning raw text or raising."""
    calls = []

    def invoke(entry, prompt, timeout_s):
        calls.append(entry.id)
        return plan[entry.id](prompt)
    return tl.OrderedTechLead(LEADS, invoke, available=kw.pop("available", lambda e: None), **kw), calls


def test_astra_answers_first_and_the_answer_names_who_answered(w):
    pkt, _ = packet_with_fact(w)
    adv, calls = lead(w, {"astra-tech-lead": lambda p: answer(w), "fable-tech-lead": lambda p: pytest.fail("fable")})
    raw = adv(pkt)
    assert tl.parse_proposal(raw) and calls == ["astra-tech-lead"]
    assert adv.last == {"answered_by": "astra-tech-lead", "model": "gpt-6-astra", "skipped": []}


@pytest.mark.parametrize("failure,reason", [
    (TimeoutError("300s"), "timeout"),
    (RuntimeError("codex not installed"), "unavailable"),
    ("this is not json", "malformed"),
    ('{"proposal_id": "x"}', "malformed"),
])
def test_fallthrough_to_fable_on_timeout_unavailable_or_malformed(w, failure, reason):
    pkt, _ = packet_with_fact(w)

    def astra(prompt):
        if isinstance(failure, Exception):
            raise failure
        return failure
    adv, calls = lead(w, {"astra-tech-lead": astra, "fable-tech-lead": lambda p: answer(w)})
    assert tl.parse_proposal(adv(pkt))
    assert calls == ["astra-tech-lead", "fable-tech-lead"]
    assert adv.last["answered_by"] == "fable-tech-lead"
    assert adv.last["skipped"][0]["id"] == "astra-tech-lead" and adv.last["skipped"][0]["reason"].startswith(reason)


def test_an_unavailable_entry_is_skipped_without_being_invoked(w):
    pkt, _ = packet_with_fact(w)
    adv, calls = lead(w, {"astra-tech-lead": lambda p: pytest.fail("astra"), "fable-tech-lead": lambda p: answer(w)},
                      available=lambda e: "codex CLI not on PATH" if e.runtime == "codex" else None)
    adv(pkt)
    assert calls == ["fable-tech-lead"]
    assert adv.last["skipped"] == [{"id": "astra-tech-lead", "reason": "unavailable: codex CLI not on PATH"}]


def test_no_self_review_a_lead_never_judges_a_lane_running_its_own_model(w):
    pkt, _ = packet_with_fact(w, lane="lane-t1")   # the packet's lane runtime is what the lead must not judge
    pkt["lanes"] = [dict(ln, runtime={"provider": "openai", "runtime": "codex", "model": "gpt-6-astra"})
                    if ln["lane"] == "lane-t1" else ln for ln in pkt["lanes"]]
    adv, calls = lead(w, {"astra-tech-lead": lambda p: pytest.fail("self-review"),
                          "fable-tech-lead": lambda p: answer(w)})
    adv(pkt)
    assert calls == ["fable-tech-lead"]
    assert adv.last["skipped"] == [{"id": "astra-tech-lead",
                                    "reason": "self_review: lane-t1 runs gpt-6-astra"}]


def test_every_entry_failing_is_unavailable_with_all_reasons(w):
    pkt, _ = packet_with_fact(w)
    adv, _ = lead(w, {"astra-tech-lead": lambda p: (_ for _ in ()).throw(TimeoutError()),
                      "fable-tech-lead": lambda p: "nope"})
    with pytest.raises(tl.TechLeadUnavailable) as exc:
        adv(pkt)
    assert [s["id"] for s in exc.value.skipped] == ["astra-tech-lead", "fable-tech-lead"]


def test_paid_leads_are_called_only_for_gated_facts(w):
    sup = tl.TechLeadSupervisor(w.store, w.ctl, None, clock=w.clock, facts=w.ctl.arbiter)
    pkt = sup.packet(JOB, [{"kind": "lane_candidate", "ref": "candidate:1"}])
    adv, calls = lead(w, {"astra-tech-lead": lambda p: pytest.fail("paid call"), "fable-tech-lead": lambda p: None})
    assert adv(pkt) is None and calls == [] and adv.last["reason"] == "not_gated"


def test_shadow_trigger_records_the_proposal_with_would_act_and_never_mutates_lanes(w):
    jev_fact(w, mode="shadow")
    adv, _ = lead(w, {"astra-tech-lead": lambda p: answer(w), "fable-tech-lead": lambda p: None})
    sup = tl.TechLeadSupervisor(w.store, w.ctl, adv, clock=w.clock, facts=w.ctl.arbiter)
    before = (json.dumps(w.fs.lanes_view(JOB), sort_keys=True), w.fs.current_graph(JOB), w.deliveries())
    res = sup.on_event(JOB)   # the controller's rollout gate is ACTIVE in this world: shadow must still win
    assert res["status"] == "shadowed"
    after = (json.dumps(w.fs.lanes_view(JOB), sort_keys=True), w.fs.current_graph(JOB), w.deliveries())
    assert before == after
    prop = w.fs.proposals(JOB)[-1]
    assert prop["outcome"] == "shadowed" and prop["reason"] == "jev_trigger_shadow"
    assert prop["bindings"]["would_act"][0]["type"] == "steer"
    answered = [e for e in w.fs.fleet_events(JOB) if e["what"] == "tech_lead_answered"]
    assert answered and json.loads(answered[-1]["evidence"])["answered_by"] == "astra-tech-lead"


def test_after_sam_flips_triggers_active_the_proposal_goes_through_the_normal_arbiter(w):
    rt.set_jev_trigger_mode(w.store, "active", actor="sam")
    jev_fact(w, mode="active")
    adv, _ = lead(w, {"astra-tech-lead": lambda p: answer(w), "fable-tech-lead": lambda p: None})
    sup = tl.TechLeadSupervisor(w.store, w.ctl, adv, clock=w.clock, facts=w.ctl.arbiter)
    assert sup.on_event(JOB)["status"] == "applied"   # ActiveRollout world: authority unchanged, nothing new
    assert rt.jev_trigger_mode(w.store) == "active"


def test_tech_lead_authority_is_unchanged(w):
    assert tl.ACTION_TYPES == ("split", "assign", "reorder", "steer", "diagnose", "owner_decision")
    assert {"stop", "accept", "merge", "push", "notify", "contact"} <= tl.FORBIDDEN_ACTIONS


def test_unavailable_lead_list_is_recorded_and_never_blocks(w):
    jev_fact(w)
    adv, _ = lead(w, {"astra-tech-lead": lambda p: (_ for _ in ()).throw(TimeoutError()),
                      "fable-tech-lead": lambda p: (_ for _ in ()).throw(RuntimeError("down"))})
    sup = tl.TechLeadSupervisor(w.store, w.ctl, adv, clock=w.clock, facts=w.ctl.arbiter)
    res = sup.on_event(JOB)
    assert res["status"] == "unavailable"
    ev = [json.loads(e["evidence"]) for e in w.fs.fleet_events(JOB) if e["what"] == "tech_lead_answered"][-1]
    assert ev["answered_by"] is None and [s["id"] for s in ev["skipped"]] == ["astra-tech-lead", "fable-tech-lead"]


def test_ordered_leads_come_from_the_jobs_frozen_roster_in_priority_order(steward_home, tmp_path):
    roster = fr.validate_roster({"schema_version": 1, "revision": 1, "entries": [
        {**entry("fable-tech-lead", "claude-fabric", "claude-fable-5-1", roles=("tech_lead",)), "priority": 2},
        {**entry("astra-tech-lead", "codex", "gpt-6-astra", roles=("tech_lead",)), "priority": 1},
        entry("w", "codex", "gpt-6-astra")]})
    assert [e.id for e in tl.ordered_leads(roster)] == ["astra-tech-lead", "fable-tech-lead"]


def test_the_prompt_asks_for_one_bounded_json_proposal_bound_to_the_observed_revisions(w):
    pkt, _ = packet_with_fact(w)
    prompt = tl.proposal_prompt(pkt)
    assert "ONE JSON object" in prompt and json.dumps(pkt["observed"]["graph_rev"]) in prompt
    assert "stop" in prompt and "never" in prompt.lower()
    assert tl.extract_json('noise ```json\n{"a": 1}\n``` tail') == '{"a": 1}'


def test_cli_flips_trigger_mode(steward_home, capsys):
    from steward import cli
    assert cli.main(["fleet", "tech-lead"]) == 0 and "shadow" in capsys.readouterr().out
    assert cli.main(["fleet", "tech-lead", "--active"]) == 0
    assert "active" in capsys.readouterr().out and rt.jev_trigger_mode(Store(steward_home)) == "active"
    assert cli.main(["fleet", "tech-lead", "--shadow"]) == 0
    assert rt.jev_trigger_mode(Store(steward_home)) == "shadow"


def test_the_packet_carries_the_trigger_id_the_proposal_must_echo(w):
    """Live 2026-10-04 (job 3ceec071): the packet had no trigger_id, so the tech lead guessed one ('fact:2')."""
    jev_fact(w)
    seen = []
    sup = tl.TechLeadSupervisor(w.store, w.ctl, lambda p: seen.append(p), clock=w.clock, facts=w.ctl.arbiter)
    res = sup.on_event(JOB)
    assert seen[0]["trigger_id"] == res["trigger_id"] and res["trigger_id"].startswith("trg-")
    assert f"trigger_id: {res['trigger_id']}" in tl.proposal_prompt(seen[0])
