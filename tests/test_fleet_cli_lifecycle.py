"""Guard the audited CLI gap: fleet metadata must never silently fall through to the legacy worker runner."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from test_fleet_graph import task
from test_fleet_lanes import JOB, seed

from steward import cli
from steward import fleet_graph as fg
from steward import fleet_registry as fr
from steward.fleet_store import FleetStore
from steward.models import JobState
from steward.store import Store


def test_run_refuses_to_start_a_fleet_job_on_the_legacy_worker_path(steward_home, tmp_path, capsys):
    store, _repo, _graph = seed(tmp_path, steward_home, [task("t1", owns=["src/a/"])])

    assert cli._cmd_run(store, SimpleNamespace(job=JOB, worker="fabric", pane=None, sock=None)) == 2
    assert "requires --sock/$HERDR_SOCKET_PATH and --pane/$HERDR_PANE_ID" in capsys.readouterr().err
    assert store.get_job(JOB).state == JobState.CREATED
    assert store._conn.execute("SELECT COUNT(*) FROM fleet_lanes WHERE job_id=?", (JOB,)).fetchone()[0] == 0


def test_resume_all_refuses_fleet_jobs_before_legacy_reattach(steward_home, tmp_path, capsys):
    store, _repo, _graph = seed(tmp_path, steward_home, [task("t1", owns=["src/a/"])])
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (JobState.RUNNING.value, JOB))

    assert cli._cmd_resume_all(store, SimpleNamespace(worker="fake")) == 2
    assert "fleet restart requires --worker fabric" in capsys.readouterr().err
    assert store._conn.execute("SELECT COUNT(*) FROM fleet_lanes WHERE job_id=?", (JOB,)).fetchone()[0] == 0


def test_runner_resume_all_never_calls_legacy_factory_for_fleet_job(steward_home, tmp_path):
    from steward.runner import Runner
    store, _repo, _graph = seed(tmp_path, steward_home, [task("t1", owns=["src/a/"])])
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (JobState.RUNNING.value, JOB))
    called = []
    runner = object.__new__(Runner)
    runner.store = store
    runner.fleet_controller = None
    runner.worker_factory = lambda ref: called.append(ref)
    assert runner.resume_all([JOB]) == []
    assert called == []


def test_fleet_tick_records_settled_outcomes_after_serial_integration(steward_home, tmp_path, monkeypatch):
    from steward import fleet_learning
    from steward.runner import Runner

    store, _repo, _graph = seed(tmp_path, steward_home, [task("t1", owns=["src/a/"])])
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (JobState.RUNNING.value, JOB))
    order = []
    facts = []

    class FleetView:
        def graph_view(self, job_id):
            return {"mode": "fleet", "tasks": [{"id": "t1", "required": True}]}

        def integrated_tasks(self, job_id):
            return []

    class Controller:
        fleet = FleetView()
        arbiter = SimpleNamespace(submit=lambda *args, **kwargs: facts.append((args, kwargs)))

        def fence(self, job_id):
            return None

        def tick(self, job_id):
            order.append("controller")
            return {"observations": {"lane-t1": {"lifecycle": "working", "activity": True,
                                                   "waiting": False, "last_activity_at": 90.0}},
                    "recovered": [{"status": "started", "lane": "lane-t1", "reason": "worker_dead"}]}

    class Writer:
        def integrate(self, job_id):
            order.append("integrate")
            return []

    monkeypatch.setattr(fleet_learning, "record_job_outcomes",
                        lambda _store, job_id: order.append("outcomes"))
    from steward import deadline_status
    monkeypatch.setattr(deadline_status, "project", lambda *_: {"status": "at_risk", "basis": "remaining_work"})
    runner = object.__new__(Runner)
    runner.store = store
    runner.fleet_controller = Controller()
    runner.integration_writer = Writer()
    runner._code_recorded = {JOB}
    runner.tech_lead = None
    runner.clock = lambda: 100.0
    from steward.heartbeat import HeartbeatPolicy
    runner.heartbeat_policy = HeartbeatPolicy()
    runner._fleet_last_cls = {}
    runner._signals = {}
    runner.efficiency = SimpleNamespace(step=lambda *_: {"finding": "high_spend"})
    runner.relay_fleet_permission_decisions = lambda *_: None
    # Merged with v2.5's fleet tick: integration goes through the stop-confirmed `_fleet_integrate` gate, which calls
    # the writer only once a candidate's lane stop is confirmed; this test pins the order, so it records the call.
    runner._fleet_integrate = lambda job_id: runner.integration_writer.integrate(job_id)
    runner._workers = {}

    Runner._tick_fleet_job(runner, JOB)

    assert order == ["controller", "integrate", "outcomes"]
    assert any(args[1:3] == ("heartbeat", "progressing") for args, _ in facts)
    assert any(args[1:3] == ("recovery", "started") for args, _ in facts)
    assert any(args[1:3] == ("efficiency", "finding") for args, _ in facts)
    assert any(args[1:3] == ("deadline", "deadline_risk") for args, _ in facts)


def test_fleet_heartbeat_uses_typed_lane_activity_and_persisted_quiet_time():
    from steward.heartbeat import Class, HeartbeatPolicy
    from steward.runner import Runner

    runner = object.__new__(Runner)
    runner.clock = lambda: 120.0
    runner.heartbeat_policy = HeartbeatPolicy(stall_s=30)

    cls, stalled = Runner._fleet_heartbeat(runner, {"lane-a": {
        "lifecycle": "working", "activity": True, "waiting": False, "last_activity_at": 110.0,
    }})
    assert cls == Class.progressing and stalled == []

    cls, stalled = Runner._fleet_heartbeat(runner, {"lane-a": {
        "lifecycle": "working", "activity": False, "waiting": False, "last_activity_at": 80.0,
    }})
    assert cls == Class.suspect_stall and stalled == ["lane-a"]

    cls, stalled = Runner._fleet_heartbeat(runner, {"lane-a": {
        "lifecycle": "blocked", "activity": False, "waiting": True, "last_activity_at": 80.0,
    }})
    assert cls == Class.waiting and stalled == []


def test_configure_job_atomically_freezes_graph_and_qualified_roster(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo", job_id="FLEET-JOB")
    fs = FleetStore(store)
    roster = fr.validate_roster({"schema_version": 1, "revision": 1, "entries": [{
        "id": "worker", "provider": "anthropic", "runtime": "claude-code", "model": "sonnet",
        "roles": ["worker"], "capabilities": ["edit", "tests"], "strengths": [], "enabled": True,
        "label": "Worker",
    }]})
    fs.record_qualification("claude-code", "1", ["edit", "tests"], qualified=True, available=True)
    graph = fg.compile_graph({"schema_version": 1, "plan_blob_id": "a" * 40, "tasks": [{
        "id": "t1", "objective": "implement", "done_when": ["checks pass"], "depends_on": [],
        "owns": ["src/"], "required": True, "priority": 1,
    }]}, committed_plan_blob_id="a" * 40, goal=job.goal)

    snap = fs.configure_job(roster, graph, job_id=job.job_id, pins={}, policy="default", tech_lead=None,
                             limits={"max_lanes": 1})

    assert snap["eligible"] == ["worker"]
    assert fs.get_snapshot(job.job_id)["registry_hash"] == roster.hash
    view = fs.graph_view(job.job_id)
    assert view["mode"] == "fleet" and view["tasks"][0]["id"] == "t1"


def test_configure_job_rolls_back_all_fleet_rows_on_invalid_tech_lead(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo", job_id="FLEET-JOB")
    fs = FleetStore(store)
    roster = fr.validate_roster({"schema_version": 1, "revision": 1, "entries": [{
        "id": "worker", "provider": "anthropic", "runtime": "claude-code", "model": "sonnet",
        "roles": ["worker"], "capabilities": ["edit", "tests"], "strengths": [], "enabled": True,
        "label": "Worker",
    }]})
    fs.record_qualification("claude-code", "1", ["edit", "tests"], qualified=True, available=True)
    graph = fg.compile_graph({"schema_version": 1, "plan_blob_id": "a" * 40, "tasks": [{
        "id": "t1", "objective": "implement", "done_when": ["checks pass"], "depends_on": [],
        "owns": ["src/"], "required": True, "priority": 1,
    }]}, committed_plan_blob_id="a" * 40, goal=job.goal)

    with pytest.raises(fr.RosterError, match="tech_lead_not_eligible"):
        fs.configure_job(roster, graph, job_id=job.job_id, pins={}, policy="default", tech_lead="worker",
                         limits={"max_lanes": 1})

    assert fs.get_snapshot(job.job_id) is None
    assert store._conn.execute("SELECT COUNT(*) FROM fleet_roster_revisions").fetchone()[0] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM fleet_graph_revisions WHERE job_id=?", (job.job_id,)).fetchone()[0] == 0


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def test_fleet_cli_preflight_binds_manifest_to_committed_plan_and_qualified_roster(steward_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    plan = repo / "PLAN.md"
    plan.write_text("# Verified plan\n")
    _git(repo, "add", "PLAN.md")
    _git(repo, "commit", "-qm", "plan")
    blob = _git(repo, "rev-parse", "HEAD:PLAN.md")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "plan_blob_id": blob, "tasks": [{
        "id": "t1", "objective": "implement", "done_when": ["checks pass"], "depends_on": [],
        "owns": ["src/"], "required": True, "priority": 1,
    }]}))
    roster = tmp_path / "roster.toml"
    roster.write_text('''schema_version = 1\nrevision = 1\n\n[[entries]]\nid = "worker"\nprovider = "anthropic"\nruntime = "claude-code"\nmodel = "sonnet"\nroles = ["worker"]\ncapabilities = ["edit"]\nstrengths = []\nenabled = true\nlabel = "Worker"\n\n[[entries]]\nid = "lead"\nprovider = "anthropic"\nruntime = "claude-code"\nmodel = "opus"\nroles = ["tech_lead"]\ncapabilities = ["review"]\nstrengths = []\nenabled = true\nlabel = "Lead"\n''')
    store = Store(steward_home)
    fs = FleetStore(store)
    fs.record_qualification("claude-code", "1", ["edit", "review"], qualified=True, available=True)

    inputs, error = cli._prepare_fleet_inputs(store, "goal", str(repo), {
        "fleet_plan": "PLAN.md", "fleet_manifest": str(manifest), "fleet_roster": str(roster),
        "fleet_tech_lead": "lead", "fleet_pins": ["t1=worker"], "fleet_max_lanes": 1,
    })

    assert error is None
    assert inputs["graph"].plan_blob_id == blob
    assert inputs["pins"] == {"lane-t1": "worker"}
    assert inputs["tech_lead"] == "lead"

    receipt = cli._build_control_handlers(store)["new"]({
        "op": "new", "request_id": "fleet-create", "origin": "cli",
        "args": {"goal": "goal", "repo": str(repo), "no_baseline": True,
                 "fleet_plan": "PLAN.md", "fleet_manifest": str(manifest), "fleet_roster": str(roster),
                 "fleet_tech_lead": "lead", "fleet_pins": ["t1=worker"], "fleet_max_lanes": 1},
    })
    assert receipt["accepted"]
    assert store.kv_get(receipt["job_id"], "fleet_mode_requested") is True
    assert fs.get_snapshot(receipt["job_id"])["tech_lead"] == "lead"
    assert fs.graph_view(receipt["job_id"])["mode"] == "fleet"


def test_fleet_cli_preflight_rejects_uncommitted_plan_and_partial_opt_in(steward_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "PLAN.md").write_text("uncommitted")
    partial, error = cli._prepare_fleet_inputs(Store(steward_home), "goal", str(repo), {"fleet_plan": "PLAN.md"})
    assert partial is None and "requires" in error
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "add", "PLAN.md")
    _git(repo, "commit", "-qm", "plan")
    blob = _git(repo, "rev-parse", "HEAD:PLAN.md")
    manifest = tmp_path / "stale.json"
    manifest.write_text(json.dumps({"schema_version": 1, "plan_blob_id": "f" * 40, "tasks": [{
        "id": "t1", "objective": "implement", "done_when": ["checks pass"], "depends_on": [],
        "owns": ["src/"], "required": True, "priority": 1,
    }]}))
    # The plan itself is committed, but the companion still names a different blob.
    assert blob != "f" * 40
    stale, error = cli._prepare_fleet_inputs(Store(steward_home), "goal", str(repo), {
        "fleet_plan": "PLAN.md", "fleet_manifest": str(manifest), "fleet_roster": str(tmp_path / "unused.toml"),
        "fleet_tech_lead": "lead",
    })
    assert stale is None and "not dispatchable" in error
    (repo / "PLAN.md").write_text("edited after commit")
    uncommitted, error = cli._prepare_fleet_inputs(Store(steward_home), "goal", str(repo), {
        "fleet_plan": "PLAN.md", "fleet_manifest": str(manifest), "fleet_roster": str(tmp_path / "unused.toml"),
        "fleet_tech_lead": "lead",
    })
    assert uncommitted is None and "uncommitted content" in error


def test_new_cli_relay_preserves_explicit_fleet_inputs():
    args = cli._build_parser().parse_args([
        "new", "goal", "--fleet-plan", "PLAN.md", "--fleet-manifest", "tasks.json", "--fleet-roster",
        "roster.toml", "--fleet-tech-lead", "lead", "--fleet-pin", "t1=worker", "--fleet-max-lanes", "1",
    ])
    op, payload = cli._op_and_args_for(args)
    assert op == "new"
    assert payload["fleet_plan"] == "PLAN.md"
    assert payload["fleet_manifest"] == "tasks.json"
    assert payload["fleet_roster"] == "roster.toml"
    assert payload["fleet_tech_lead"] == "lead"
    assert payload["fleet_pins"] == ["t1=worker"]
    assert payload["fleet_max_lanes"] == 1
