"""Fleet wiring: manifest seeding, the explicit `launch --fleet` opt-in, controller construction and the live
runner path (steward.fleet_launch, steward.fleet_live, Runner fleet tick). Temp Git + real Store; scripted lane
adapters, never a real Herdr or model."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_fleet_lanes import World

from steward import cli, fleet_closeout as fc, fleet_launch, fleet_live
from steward import fleet_lease as fl
from steward import fleet_runtime as rt
from steward.contract import Contract, store_contract
from steward.discovery import DiscoveredCommand, Discovery
from steward.fabric import Frame, encode
from steward.fleet_store import FleetStore
from steward.herdr_transport import HerdrApiError, HerdrUnavailable, Mailbox
from steward.models import JobState
from steward.runner import Runner, TickReport
from steward.runner_exec import SubprocessRunner
from steward.shim import Shim
from steward.store import Store
from steward.worker import DeliveryResult

PLAN = ".planning/phases/01/01-01-PLAN.md"
MANIFEST = ".planning/phases/01/01-01-FLEET.json"
ROSTER = """schema_version = 1
revision = 1

[[entries]]
id = "claude-sonnet-worker"
provider = "anthropic"
runtime = "claude-fabric"
model = "sonnet"
roles = ["worker"]
capabilities = ["edit", "tests"]
enabled = true
label = "worker"

[[entries]]
id = "claude-opus-tech-lead"
provider = "anthropic"
runtime = "claude-fabric"
model = "opus"
roles = ["tech_lead"]
capabilities = ["plan", "review"]
enabled = true
label = "lead"
"""


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def commit(repo, files: dict, msg="c"):
    for rel, text in files.items():
        f = Path(repo) / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=T", "-c", "user.email=t@x", "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def task(tid, owns, **over):
    t = {"id": tid, "objective": f"write {tid}", "done_when": [f"{tid} test passes"], "depends_on": [],
         "owns": owns, "evidence": ["pytest"], "required": True, "priority": 1}
    t.update(over)
    return t


TASKS = [task("a", ["a/"]), task("b", ["b/"])]


def make_repo(tmp_path, *, manifest="good", tasks=TASKS, commit_manifest=True) -> Path:
    """A git repo with a committed PLAN.md and (optionally) its FLEET.json companion bound to the plan blob."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    commit(repo, {"README.md": "r\n", PLAN: "# plan\n"}, "plan")
    blob = git(repo, "rev-parse", f"HEAD:{PLAN}")
    if manifest is None:
        return repo
    if manifest == "good":
        body = json.dumps({"schema_version": 1, "plan_blob_id": blob, "tasks": tasks})
    elif manifest == "stale":
        body = json.dumps({"schema_version": 1, "plan_blob_id": "b" * 40, "tasks": tasks})
    elif manifest == "malformed":
        body = json.dumps({"schema_version": 1, "plan_blob_id": blob, "tasks": [{"id": "a"}]})
    elif manifest == "unparseable":
        body = "{not json"
    else:
        body = manifest
    if commit_manifest:
        commit(repo, {MANIFEST: body}, "manifest")
    else:
        (repo / MANIFEST).write_text(body)
    return repo


def contracted_job(store, repo, job_id="J1", goal="two independent modules"):
    job = store.create_job(goal, str(repo), job_id=job_id)
    store_contract(store, job_id, Contract(goal=goal, done_when=["both tests pass"], boundaries=[],
                                           authority=["edit files in job worktree", "run: pytest"]))
    return job


def fleet_rows(store, job_id="J1") -> dict:
    return {t: store._conn.execute(f"SELECT COUNT(*) FROM {t} WHERE job_id=?", (job_id,)).fetchone()[0]
            for t in ("fleet_jobs", "fleet_graph_revisions", "fleet_job_state", "fleet_tasks", "fleet_lanes",
                      "fleet_attempts")}


@pytest.fixture
def roster(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "roster.toml").write_text(ROSTER)
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(cfg))
    return cfg / "roster.toml"


# ------------------------------------------------------------------------------------------- manifest seeding

def test_committed_manifest_compiles_and_seeds_revision_one_bound_to_the_plan_blob(steward_home, tmp_path):
    repo = make_repo(tmp_path)
    store = Store(steward_home)
    contracted_job(store, repo)
    assert fleet_launch.discover_plan(repo) == PLAN
    c = fleet_launch.compile_for_repo(repo, PLAN, goal="g")
    blob = git(repo, "rev-parse", f"HEAD:{PLAN}")
    assert not c.graph.implicit and c.graph.errors == [] and c.plan_blob_id == blob == c.graph.plan_blob_id
    assert c.manifest == MANIFEST and [t.id for t in c.graph.tasks] == ["a", "b"]
    assert not rt.is_fleet_job(store, "J1")
    assert fleet_launch.seed_graph(store, "J1", c.graph) == 1
    fs = FleetStore(store)
    assert fs.current_graph("J1") == (1, 1) and rt.is_fleet_job(store, "J1")
    view = fs.graph_view("J1")
    assert view["mode"] == "fleet" and view["graph_rev"] == 1 and not view["implicit"]
    assert rt.fleet_graph_row(store, "J1")["plan_blob_id"] == blob
    assert any(e["what"] == "fleet graph seeded" for e in fs.fleet_events("J1"))
    assert fleet_launch.seed_graph(store, "J1", c.graph) == 1  # idempotent: no second revision
    assert fleet_rows(store)["fleet_graph_revisions"] == 1


def test_manifest_name_follows_the_plan():
    assert fleet_launch.manifest_for("x/20-01-PLAN.md") == "x/20-01-FLEET.json"
    assert fleet_launch.plan_for("x/20-01-FLEET.json") == "x/20-01-PLAN.md"
    assert fleet_launch.manifest_for("notes.md") == "notes-FLEET.json"


@pytest.mark.parametrize("kind,commit_manifest,prefix", [
    (None, True, "absent:"),
    ("good", False, "absent:"),          # an uncommitted manifest is not plan metadata
    ("malformed", True, "malformed:"),
    ("unparseable", True, "malformed:"),
    ("stale", True, "stale:"),
])
def test_each_fallback_reason_is_implicit_and_seeds_nothing(steward_home, tmp_path, kind, commit_manifest, prefix):
    repo = make_repo(tmp_path, manifest=kind, commit_manifest=commit_manifest)
    store = Store(steward_home)
    contracted_job(store, repo)
    c = fleet_launch.compile_for_repo(repo, PLAN, goal="g")
    assert c.graph.implicit and c.graph.fallback_reason.startswith(prefix), c.graph.fallback_reason
    assert [t.id for t in c.graph.tasks] == ["implicit"]
    assert fleet_launch.seed_graph(store, "J1", c.graph) is None
    assert not rt.is_fleet_job(store, "J1") and fleet_rows(store) == dict.fromkeys(fleet_rows(store), 0)
    assert FleetStore(store).graph_view("J1")["mode"] == "legacy"


def test_no_plan_at_all_is_the_absent_fallback(tmp_path):
    repo = make_repo(tmp_path, manifest=None)
    assert fleet_launch.discover_plan(repo) is None
    c = fleet_launch.compile_for_repo(repo, None)
    assert c.graph.implicit and c.graph.fallback_reason.startswith("absent:")


def test_invalid_explicit_graph_is_not_seeded(steward_home, tmp_path):
    repo = make_repo(tmp_path, tasks=[task("a", ["a/"], depends_on=["b"]), task("b", ["b/"], depends_on=["a"])])
    store = Store(steward_home)
    contracted_job(store, repo)
    c = fleet_launch.compile_for_repo(repo, PLAN)
    assert c.graph.errors and fleet_launch.seed_graph(store, "J1", c.graph) is None
    assert not rt.is_fleet_job(store, "J1")


# ------------------------------------------------------------------------------------------- --fleet opt-in

class Launches:
    """Records launch_tab calls; never touches herdr."""

    def __init__(self):
        self.calls = []

    def __call__(self, runner_exec, job, **kw):
        self.calls.append((job.job_id, kw))
        return "w9:t1", "w9:p1"


@pytest.fixture
def tabs(monkeypatch):
    t = Launches()
    monkeypatch.setattr(cli.launch, "launch_tab", t)
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    return t


def launch(job, *extra):
    return cli.main(["launch", job, "--workspace", "w9", *extra])


def test_fleet_launch_seeds_snapshot_graph_and_choice_then_opens_the_tab(steward_home, tmp_path, roster, tabs, capsys):
    repo = make_repo(tmp_path)
    store = Store(steward_home)
    contracted_job(store, repo)
    assert launch("J1", "--fleet", "--max-lanes", "2") == 0
    assert tabs.calls and rt.is_fleet_job(store, "J1")
    choice = store.kv_get("J1", fleet_launch.LAUNCH_KEY)
    assert choice["max_lanes"] == 2 and choice["worker"] == "claude-sonnet-worker" and choice["worker_model"] == "sonnet"
    assert choice["tech_lead"] == "claude-opus-tech-lead" and choice["plan"] == PLAN and choice["graph_rev"] == 1
    snap = FleetStore(store).get_snapshot("J1")
    assert snap["limits"] == {"max_lanes": 2} and snap["policy"] == "fleet_opt_in"
    rows = fleet_rows(store)
    assert rows["fleet_jobs"] == 1 and rows["fleet_graph_revisions"] == 1 and rows["fleet_tasks"] == 2
    # a second launch (play) of the already-seeded job is idempotent
    assert launch("J1", "--fleet") == 0 and fleet_rows(store)["fleet_graph_revisions"] == 1


def _refused(store, tabs, capsys, job="J1"):
    out = capsys.readouterr()
    assert "refused" in (out.out + out.err) or "only apply with --fleet" in (out.out + out.err)
    assert not tabs.calls, "a refused fleet launch must not open a tab"
    assert not rt.is_fleet_job(store, job)
    assert fleet_rows(store, job)["fleet_jobs"] == 0 and store.kv_get(job, fleet_launch.LAUNCH_KEY) is None
    return out.out + out.err


def test_fleet_refused_without_a_roster(steward_home, tmp_path, tabs, capsys, monkeypatch):
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "nocfg"))
    store = Store(steward_home)
    contracted_job(store, make_repo(tmp_path))
    assert launch("J1", "--fleet") != 0
    assert "no_roster" in _refused(store, tabs, capsys)


@pytest.mark.parametrize("text,reason", [
    ("schema_version = 1\nrevision = 1\n[[entries]]\nid = \"x\"\n", "roster_invalid"),
    ("this is = = not toml", "roster_invalid"),
    (ROSTER.replace('roles = ["tech_lead"]', 'roles = ["worker"]'), "roster_no_tech_lead"),
    (ROSTER.replace("enabled = true", "enabled = false", 1), "roster_no_worker"),
])
def test_fleet_refused_with_an_invalid_or_incomplete_roster(steward_home, tmp_path, roster, tabs, capsys, text, reason):
    roster.write_text(text)
    store = Store(steward_home)
    contracted_job(store, make_repo(tmp_path))
    assert launch("J1", "--fleet") != 0
    assert reason in _refused(store, tabs, capsys)


@pytest.mark.parametrize("kind,commit_manifest", [(None, True), ("stale", True), ("malformed", True),
                                                  ("good", False)])
def test_fleet_refused_without_an_explicit_graph(steward_home, tmp_path, roster, tabs, capsys, kind, commit_manifest):
    store = Store(steward_home)
    contracted_job(store, make_repo(tmp_path, manifest=kind, commit_manifest=commit_manifest))
    assert launch("J1", "--fleet", "--plan", PLAN) != 0
    assert "no_explicit_graph" in _refused(store, tabs, capsys)


def test_fleet_refused_for_an_invalid_graph_bad_lanes_wrong_worker_or_launched_job(
        steward_home, tmp_path, roster, tabs, capsys):
    store = Store(steward_home)
    contracted_job(store, make_repo(tmp_path))
    assert launch("J1", "--fleet", "--max-lanes", "5") != 0  # v2.5: the hard cap is 4 (FLEET-31)
    assert "bad_max_lanes" in _refused(store, tabs, capsys)
    assert launch("J1", "--fleet", "--max-lanes", "0") != 0
    assert "bad_max_lanes" in _refused(store, tabs, capsys)
    assert launch("J1", "--fleet", "--worker", "herdr-tui") != 0
    assert "fabric" in _refused(store, tabs, capsys)
    assert launch("J1", "--max-lanes", "2") != 0  # fleet-only flags without --fleet launch nothing
    _refused(store, tabs, capsys)
    from steward.models import Judgment
    store.set_state("J1", JobState.RUNNING, what="legacy start", evidence={}, rule="t", judgment=Judgment.code)
    assert launch("J1", "--fleet") != 0
    assert "not_launchable" in _refused(store, tabs, capsys)


def test_fleet_refused_for_a_cyclic_graph(steward_home, tmp_path, roster, tabs, capsys):
    store = Store(steward_home)
    repo = make_repo(tmp_path, tasks=[task("a", ["a/"], depends_on=["b"]), task("b", ["b/"], depends_on=["a"])])
    contracted_job(store, repo)
    assert launch("J1", "--fleet") != 0
    assert "invalid_graph" in _refused(store, tabs, capsys)


def test_without_fleet_a_job_with_a_manifest_stays_legacy(steward_home, tmp_path, roster, tabs, capsys):
    store = Store(steward_home)
    contracted_job(store, make_repo(tmp_path))
    assert launch("J1") == 0 and tabs.calls
    assert not rt.is_fleet_job(store, "J1") and fleet_rows(store) == dict.fromkeys(fleet_rows(store), 0)
    assert store.kv_get("J1", fleet_launch.LAUNCH_KEY) is None


# ------------------------------------------------------------------------------------------- controller construction

class Traj:
    def __init__(self):
        self.steps = 0

    def step(self, job_id):
        self.steps += 1


def _runner(store, trajectory=None):
    return Runner(store, lambda ref: pytest.fail("a fleet job must never build a single worker"),
                  SubprocessRunner(), diagnoser=None, trajectory=trajectory)


def seeded(tmp_path, home, max_lanes=2):
    repo = make_repo(tmp_path)
    store = Store(home)
    contracted_job(store, repo)
    (Path(home) / "worktrees").mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-q", "-b", "steward/J1", str(Path(home) / "worktrees" / "J1"), "HEAD")
    roster_file = tmp_path / "roster.toml"
    roster_file.write_text(ROSTER)
    fleet_launch.prepare(store, "J1", max_lanes=max_lanes, roster_path=roster_file)
    return store, repo


def test_wire_constructs_and_connects_controller_writer_closeout_and_tech_lead(steward_home, tmp_path):
    store, repo = seeded(tmp_path, steward_home)
    runner = _runner(store, trajectory=Traj())
    assert runner.fleet_controller is None and not runner._fleet_wired("J1")
    world = World()
    ctl = fleet_live.wire(runner, "J1", adapter_factory=world.factory, probe=world.probe,
                          runner_exec=SubprocessRunner())
    assert runner.fleet_controller is ctl and isinstance(ctl, rt.JobController)
    assert runner.integration_writer is ctl.integration and runner.integration_writer is not None
    assert isinstance(runner.fleet_closeout, fc.FleetCloseout)
    assert runner.tech_lead is not None and runner.tech_lead.controller is ctl and runner.tech_lead.facts is ctl.arbiter
    assert ctl.base_dir == store.home / "worktrees" and ctl.base_sha() == git(repo, "rev-parse", "HEAD")
    assert store.kv_get("J1", fleet_live.BASE_KEY) == ctl.base_sha()
    assert runner._fleet_wired("J1") and runner._tech_lead_owns_outer_loop("J1")
    assert ctl.limits("J1")["max_lanes"] == 2


def test_wire_refuses_a_legacy_job_and_legacy_runners_stay_unwired(steward_home, tmp_path):
    store = Store(steward_home)
    contracted_job(store, make_repo(tmp_path))
    runner = _runner(store)
    with pytest.raises(ValueError):
        fleet_live.wire(runner, "J1", adapter_factory=World().factory, probe=World().probe,
                        runner_exec=SubprocessRunner())
    assert runner.fleet_controller is None and runner.tech_lead is None and not runner._fleet_wired("J1")
    assert not runner._tech_lead_owns_outer_loop("J1")
    assert fleet_rows(store) == dict.fromkeys(fleet_rows(store), 0)


# ------------------------------------------------------------------------------------------- the live fleet tick

def commit_lane(world, lane, files, attempt=1):
    a = world.adapters[(lane, attempt)]
    sha = commit(a.worktree, files, f"lane {lane}")
    a.events.append(Frame(v=2, seq=len(a.events) + 1, job="J1", type="done",
                          body={"candidate_sha": sha, "evidence_ref": f"ev:{lane}"}, task=a.task_id, lane=lane,
                          attempt=a.attempt_no, sender=f"shim:{lane}", epoch=a.epoch))
    return sha


def test_runner_drives_two_lanes_to_one_integrated_candidate_without_the_trajectory_loop(
        steward_home, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "auto")  # fleet jobs never auto-accept
    store, repo = seeded(tmp_path, steward_home)
    store.add_check("J1", commit_sha=git(repo, "rev-parse", "HEAD"), command="true", exit_code=0, results={},
                    label="baseline")
    traj = Traj()
    runner = _runner(store, trajectory=traj)
    world = World()
    fleet_live.wire(runner, "J1", adapter_factory=world.factory, probe=world.probe, runner_exec=SubprocessRunner())
    runner.fleet_closeout.discover = lambda wt: Discovery(
        commands=[DiscoveredCommand(line="true", source="t", kind="check", reason="")])

    runner.start("J1")
    assert store.get_job("J1").state == JobState.RUNNING
    lanes = FleetStore(store).lanes_view("J1")
    assert [l["lane"] for l in lanes] == ["lane-a", "lane-b"]
    assert len({l["worktree"] for l in lanes}) == 2 and len({l["branch"] for l in lanes}) == 2
    assert all(l["state"] == "running" for l in lanes)

    rep = runner.tick("J1")
    assert rep.state == "RUNNING" and "2 live lane(s)" in rep.detail
    commit_lane(world, "lane-a", {"a/hello.py": "x = 1\n"})
    commit_lane(world, "lane-b", {"b/world.py": "y = 2\n"})
    rep = runner.tick("J1")
    assert rep.state == "NEEDS_HUMAN", rep

    fs = FleetStore(store)
    assert sorted(fs.integrated_tasks("J1")) == ["a", "b"]
    head = git(store.home / "worktrees" / "J1", "rev-parse", "HEAD")
    cand = fc.final_candidate(store, "J1")
    assert cand["final_sha"] == head == fs.integration_head("J1") and set(cand["lane_shas"]) == {"a", "b"}
    assert git(repo, "rev-parse", "steward/J1") == head  # ONE job branch holds both lanes
    assert traj.steps == 0  # the tech lead is the outer loop; the legacy trajectory redirect never ran
    assert any(e["what"].startswith("tech_lead_advice") for e in fs.fleet_events("J1"))
    rows = fleet_rows(store)
    assert all(rows[t] > 0 for t in ("fleet_jobs", "fleet_tasks", "fleet_lanes", "fleet_attempts"))
    attempts = store._conn.execute("SELECT model, runtime FROM fleet_attempts WHERE job_id='J1'").fetchall()
    assert {(r["model"], r["runtime"]) for r in attempts} == {("unknown", "unknown")}  # scripted adapter: no claim


def test_result_and_graph_print_the_fleet_candidate_and_lanes(steward_home, tmp_path, monkeypatch, capsys):
    test_runner_drives_two_lanes_to_one_integrated_candidate_without_the_trajectory_loop(
        steward_home, tmp_path, monkeypatch)
    store = Store(steward_home)
    head = fc.final_candidate(store, "J1")["final_sha"]
    capsys.readouterr()
    assert cli.main(["result", "J1"]) == 0
    out = capsys.readouterr().out
    assert "FLEET CANDIDATE" in out and f"fleet candidate: {head}" in out
    assert "task a:" in out and "task b:" in out and "check ok: true" in out
    assert cli.main(["fleet", "graph", "J1"]) == 0
    out = capsys.readouterr().out
    assert "lanes: 2" in out and "branch=steward-lanes/J1/lane-a" in out and "branch=steward-lanes/J1/lane-b" in out
    legacy = Store(steward_home).create_job("legacy", "/repo", job_id="L1")
    assert cli.main(["result", legacy.job_id]) == 0
    assert "FLEET CANDIDATE" not in capsys.readouterr().out


def test_lane_attempt_records_the_adapters_requested_model(steward_home, tmp_path):
    store, _ = seeded(tmp_path, steward_home)
    world = World()

    def factory(**kw):
        a = world.factory(**kw)
        a.requested = {"provider": "anthropic", "runtime": "claude-fabric", "model": "sonnet"}
        return a

    runner = _runner(store)
    fleet_live.wire(runner, "J1", adapter_factory=factory, probe=world.probe, runner_exec=SubprocessRunner())
    runner.start("J1")
    rows = store._conn.execute("SELECT model FROM fleet_attempts WHERE job_id='J1'").fetchall()
    assert [r["model"] for r in rows] == ["sonnet", "sonnet"]


def test_fleet_stop_fences_every_lane_and_confirms(steward_home, tmp_path):
    store, _ = seeded(tmp_path, steward_home)
    world = World()
    runner = _runner(store)
    fleet_live.wire(runner, "J1", adapter_factory=world.factory, probe=world.probe, runner_exec=SubprocessRunner())
    runner.start("J1")
    from steward.models import Judgment
    store.set_state("J1", JobState.STOPPING, what="stop", evidence={}, rule="t", judgment=Judgment.human)
    rep = runner.tick("J1")
    assert rep.state == "STOPPED" and sorted(world.stop_calls) == ["lane-a", "lane-b"]
    assert not fl.live(store, "J1")


def test_an_exhausted_required_task_goes_to_the_owner_once_no_lane_is_live(steward_home, tmp_path):
    store, _ = seeded(tmp_path, steward_home, max_lanes=1)
    world = World()
    runner = _runner(store)
    fleet_live.wire(runner, "J1", adapter_factory=world.factory, probe=world.probe, runner_exec=SubprocessRunner())
    runner.start("J1")
    assert [l["lane_id"] for l in fl.live(store, "J1")] == ["lane-a"]  # one lane allowed
    fl.force_task_state(store, "J1", "a", "exhausted")
    world.alive[world.adapters[("lane-a", 1)].ref] = False  # lane process gone, recovery budget spent
    assert runner.tick("J1").state == "RUNNING"  # lane-a fenced; the independent task b still gets its lane
    assert [l["lane_id"] for l in fl.live(store, "J1")] == ["lane-b"]
    commit_lane(world, "lane-b", {"b/world.py": "y = 2\n"})
    rep = runner.tick("J1")
    assert rep.state == "NEEDS_HUMAN" and FleetStore(store).integrated_tasks("J1").keys() == {"b"}
    assert store.events("J1")[-1].what == "fleet task cannot complete"


# ------------------------------------------------------------------------------------------- lane plumbing

def test_mailbox_hub_routes_frames_by_lane_and_drops_v1():
    class Box:
        def __init__(self, frames):
            self.frames = frames

        def read(self, timeout):
            out, self.frames = self.frames, []
            return out

    def f(lane, seq):
        return Frame(v=2, seq=seq, job="J1", type="event", body={}, task="t", lane=lane, attempt=1, sender="s", epoch=1)

    hub = fleet_live.LaneMailboxHub(Box([f("lane-a", 1), f("lane-b", 1), Frame(v=1, seq=9, job="J1", type="event",
                                                                              body={}), f("lane-a", 2)]))
    a, b = hub.view("lane-a"), hub.view("lane-b")
    assert [x.seq for x in b.read(0)] == [1]  # b's read pulled a's frames into a's queue, not lost
    assert [x.seq for x in a.read(0)] == [1, 2]
    assert a.read(0) == [] and hub.orphans == 1


def test_herdr_probe_maps_pane_state():
    class Api:
        def __init__(self, result=None, exc=None):
            self.result, self.exc = result, exc

        def call(self, method, params):
            if self.exc:
                raise self.exc
            return self.result

    ident = {"pane": "w:p1"}
    assert fleet_live.herdr_lane_probe(Api(exc=HerdrApiError("pane_not_found", "gone")))("J", "l", ident) == rt.DEAD
    assert fleet_live.herdr_lane_probe(Api(exc=HerdrUnavailable("x")))("J", "l", ident) == rt.UNKNOWN
    shell = {"process_info": {"foreground_process_group_id": 5, "shell_pid": 5}}
    busy = {"process_info": {"foreground_process_group_id": 6, "shell_pid": 5}}
    assert fleet_live.herdr_lane_probe(Api(shell))("J", "l", ident) == rt.DEAD
    assert fleet_live.herdr_lane_probe(Api(busy))("J", "l", ident) == rt.ALIVE
    assert fleet_live.herdr_lane_probe(Api(busy))("J", "l", None) == rt.UNKNOWN


def test_lane_permission_relay_uses_the_job_authority_policy(steward_home, tmp_path):
    store, _ = seeded(tmp_path, steward_home)

    class A:
        def __init__(self):
            self.answers = []

        def answer_prompt(self, approve, request_id=None, message=None):
            self.answers.append((approve, request_id, message))
            return DeliveryResult(ok=True, detail="ok")

    relay = fleet_live.permission_relay(store, store.home / "worktrees", lambda c, auth, wt: c.startswith("pytest"))
    a = A()

    def req(rid, tool, inp):
        return Frame(v=2, seq=1, job="J1", type="permission_request", body={"request_id": rid, "tool": tool,
                                                                             "input": inp},
                     task="a", lane="lane-a", attempt=1, sender="s", epoch=1)

    relay("J1", "lane-a", req("r1", "Bash", {"command": "pytest -q"}), a)
    relay("J1", "lane-a", req("r2", "Bash", {"command": "git push origin main"}), a)
    assert a.answers[0][:2] == (True, "r1")
    assert a.answers[1][0] is False and a.answers[1][1] == "r2" and "owner" in a.answers[1][2]
    whats = [e["what"] for e in FleetStore(store).fleet_events("J1")]
    assert "lane_permission_allowed" in whats and "lane_permission_denied" in whats


def test_controller_routes_a_current_lanes_permission_request_to_the_handler(steward_home, tmp_path):
    store, _ = seeded(tmp_path, steward_home)
    world = World()
    runner = _runner(store)
    seen = []
    ctl = fleet_live.wire(runner, "J1", adapter_factory=world.factory, probe=world.probe,
                          runner_exec=SubprocessRunner(), permission_handler=lambda *a: seen.append(a[:2]))
    runner.start("J1")
    lease = fl.get(store, "J1", "lane-a")
    fr = Frame(v=2, seq=1, job="J1", type="permission_request", body={"request_id": "r", "tool": "Bash", "input": {}},
               task="a", lane="lane-a", attempt=lease["attempt_no"], sender="s", epoch=lease["epoch"])
    assert ctl.accept_frame("J1", fr).accepted and seen == [("J1", "lane-a")]
    stale = Frame(v=2, seq=2, job="J1", type="permission_request", body={}, task="a", lane="lane-a",
                  attempt=lease["attempt_no"], sender="s", epoch=lease["epoch"] + 7)
    assert not ctl.accept_frame("J1", stale).accepted and len(seen) == 1


# ------------------------------------------------------------------------------------------- lane shim (v2)

class FakeApi:
    def __init__(self):
        self.sent = []

    def send(self, pane, frame):
        self.sent.append(frame)
        return {"type": "ok"}

    def call(self, method, params):
        return {}


def test_lane_shim_speaks_v2_and_names_its_committed_candidate(tmp_path):
    repo = tmp_path / "lane"
    repo.mkdir()
    git(repo, "init", "-q")
    base = commit(repo, {"r": "r\n"})
    script = repo / "script.json"
    script.write_text(json.dumps({"steps": [{"commit": "lane work"}, {"result": {"subtype": "success"}}]}))
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=T", "-c", "user.email=t@x", "commit", "-q", "-m", "script")
    base = git(repo, "rev-parse", "HEAD")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
           "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x"}
    api = FakeApi()
    rfd, wfd = os.pipe()
    shim = None
    try:
        shim = Shim(job="J1", peer_pane="w:p1", api=api, mailbox=Mailbox(fd=rfd),
                    spawn=lambda argv: subprocess.Popen(argv, cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                        stderr=subprocess.PIPE, text=True, bufsize=1, env=env),
                    agent_argv=[sys.executable, "-m", "steward.fake_agent", "--script", str(script)],
                    git=lambda: git(repo, "rev-parse", "HEAD"), task="a", lane="lane-a", attempt=1, epoch=3)
        shim.start()
        assert api.sent[0].v == 2 and api.sent[0].lane == "lane-a" and api.sent[0].sender == "shim:lane-a"
        # a frame for another lane (or a v1 frame) is ignored: no transport ack, no brief written
        other = Frame(v=2, seq=1, job="J1", type="brief", body={"text": "x"}, task="b", lane="lane-b", attempt=1,
                      sender="c", epoch=3)
        os.write(wfd, (encode(other) + "\n").encode())
        os.write(wfd, (encode(Frame(v=1, seq=2, job="J1", type="brief", body={"text": "x"})) + "\n").encode())
        mine = Frame(v=2, seq=3, job="J1", type="brief", body={"text": "go"}, task="a", lane="lane-a", attempt=1,
                     sender="controller:lane-a", epoch=3)
        os.write(wfd, (encode(mine) + "\n").encode())
        import time
        deadline = time.time() + 15
        while time.time() < deadline and not any(f.type == "done" for f in api.sent):
            shim.step(0.1)
        done = next(f for f in api.sent if f.type == "done")
        acked = {f.body["acked_seq"] for f in api.sent if f.type == "ack" and "acked_seq" in f.body}
        assert acked == {3}
        assert done.v == 2 and done.epoch == 3 and done.attempt == 1 and done.task == "a"
        head = git(repo, "rev-parse", "HEAD")
        assert head != base and done.body["candidate_sha"] == head and done.body["sha"] == head
        assert done.body["evidence_ref"].startswith("lane:lane-a:attempt1:turn1")
        # the controller's transport ack (its own identity) clears the shim's outbox entry for that frame
        shim.outbox.ack(job="J1", seq=done.seq, ident=shim._ident())
        assert not shim.outbox.pending("J1", done.seq, shim._ident())
    finally:
        if shim is not None and shim._agent is not None and shim._agent.poll() is None:
            shim._agent.kill()
            shim._agent.wait(timeout=5)
        os.close(wfd)
        os.close(rfd)


def test_shim_cli_refuses_a_partial_lane_identity(steward_home, capsys):
    Store(steward_home).create_job("g", "/repo", job_id="J1")
    assert cli.main(["shim", "J1", "--sock", "s", "--peer", "p", "--lane", "lane-a"]) == 2
    assert "--lane, --task, --attempt and --epoch" in capsys.readouterr().err


def test_lane_shim_command_carries_the_v2_identity():
    cmd = cli._shim_run_command("J1", sock="s", peer_pane="w:p1", agent="claude", agent_script=None, model="sonnet",
                                extra_args=[], steward_home=None,
                                lane={"lane": "lane-a", "task": "a", "attempt": 2, "epoch": 5})
    assert "--lane lane-a --task a --attempt 2 --epoch 5" in cmd and "--model sonnet" in cmd
    legacy = cli._shim_run_command("J1", sock="s", peer_pane="w:p1", agent="claude", agent_script=None, model=None,
                                   extra_args=[], steward_home=None)
    assert "--lane" not in legacy


def test_pane_commands_forward_a_caller_tmpdir_only_when_set(monkeypatch):
    from steward import launch as launch_module
    monkeypatch.setenv("TMPDIR", "/data/scratch tmp")
    assert launch_module._env_exports().endswith("export TMPDIR='/data/scratch tmp' && ")
    assert "export TMPDIR='/data/scratch tmp' && export STEWARD_PYTHON=" in cli._shim_run_command(
        "J1", sock="s", peer_pane="p", agent="claude", agent_script=None, model=None, extra_args=[], steward_home=None)
    monkeypatch.delenv("TMPDIR")
    assert "TMPDIR" not in launch_module._env_exports()


# ------------------------------------------------------------------------- v2.5: lanes on their assigned runtimes

MIXED_ROSTER = ROSTER + """
[[entries]]
id = "codex-worker"
provider = "openai"
runtime = "codex"
model = "gpt-x"
roles = ["worker"]
capabilities = ["edit", "tests"]
enabled = true
label = "codex"

[[entries]]
id = "muse-worker"
provider = "meta"
runtime = "muse"
model = "muse-spark-1.3-contributor"
roles = ["worker"]
capabilities = ["edit", "tests"]
enabled = true
label = "muse"
"""


def mixed_seeded(tmp_path, home, monkeypatch, runtimes=("codex", "muse")):
    from steward import fleet_registry as fr
    from steward import fleet_versions
    monkeypatch.setattr(fleet_versions, "current_version", lambda r: "v1")
    repo = make_repo(tmp_path)
    store = Store(home)
    contracted_job(store, repo)
    (Path(home) / "worktrees").mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-q", "-b", "steward/J1", str(Path(home) / "worktrees" / "J1"), "HEAD")
    fs = FleetStore(store)
    for r in ("codex", "muse"):
        for cap in fr.RUNTIME_CAPABILITIES:
            fs.record_runtime_capability(r, cap, qualified=True, adapter_version="1", evidence="probe", cli_version="v1")
        fs.record_qualification(r, "1", ["edit", "tests"], qualified=True, available=True, cli_version="v1")
    roster_file = tmp_path / "roster.toml"
    roster_file.write_text(MIXED_ROSTER)
    fleet_launch.prepare(store, "J1", max_lanes=2, roster_path=roster_file, runtimes=list(runtimes))
    return store, repo


class Structured:
    """Real CodexAppServerAdapter / MuseServeAdapter over their scripted fakes, built by the runtime factory."""

    def __init__(self):
        self.made = {}

    def make(self, runtime, *, model, job_id, lane_id, task_id, attempt_no, epoch, lease_check, head_fn, home):
        from test_fleet_adapter_deadline import FakeServer
        from test_muse_worker import wire

        from steward.muse_worker import MuseServeAdapter
        from steward.worker import CodexAppServerAdapter
        kw = dict(model=model, job_id=job_id, task=task_id, lane=lane_id, attempt=attempt_no, epoch=epoch,
                  lease_check=lease_check, head_fn=head_fn)
        if runtime == "codex":
            srv = FakeServer(model=model)
            srv.auto = False
            a = CodexAppServerAdapter(srv, **kw)
        elif runtime == "muse":
            t, _, _ = wire(auto=False)
            a = MuseServeAdapter(t, **kw)
        else:
            raise AssertionError(runtime)
        self.made[lane_id] = (runtime, a)
        return a

    def finish(self, lane, files):
        runtime, a = self.made[lane]
        commit(a.worktree, files, f"lane {lane}")
        if runtime == "codex":
            a.transport.inbox.append({"method": "turn/completed", "params": {"turn": {"id": a.turn_id,
                                                                                      "status": "completed"}}})
        else:
            a.transport._inbox.put({"jsonrpc": "2.0", "method": "turn/completed",
                                    "params": {"turnId": a.turn_id, "terminal": "completed"}})


def alive_probe(structured):
    def probe(job_id, lane_id, identity):
        hit = structured.made.get(lane_id)
        if hit is None:
            return rt.UNKNOWN
        return rt.ALIVE if hit[1].transport.alive() else rt.DEAD
    return probe


def test_lanes_run_on_their_assigned_runtimes_and_integrate_into_one_candidate(steward_home, tmp_path, monkeypatch):
    store, repo = mixed_seeded(tmp_path, steward_home, monkeypatch)
    store.add_check("J1", commit_sha=git(repo, "rev-parse", "HEAD"), command="true", exit_code=0, results={},
                    label="baseline")
    s = Structured()
    factory = fleet_live.runtime_lane_factory(store, structured=s.make)
    runner = _runner(store)
    fleet_live.wire(runner, "J1", adapter_factory=factory, probe=alive_probe(s), runner_exec=SubprocessRunner())
    runner.fleet_closeout.discover = lambda wt: Discovery(
        commands=[DiscoveredCommand(line="true", source="t", kind="check", reason="")])
    runner.start("J1")
    assert {lane: rtm for lane, (rtm, _) in s.made.items()} == {"lane-a": "codex", "lane-b": "muse"}
    started = {lane: a for lane, (_, a) in s.made.items()}
    assert started["lane-a"].model == "gpt-x" and started["lane-b"].model == "muse-spark-1.3-contributor"
    assert all("You are one lane of a Steward fleet job" in str(m) for m in (
        dict(started["lane-a"].transport.requests)["turn/start"]["input"][0]["text"],))
    rows = {r["lane_id"]: (r["runtime"], r["model"]) for r in
            store._conn.execute("SELECT lane_id, runtime, model FROM fleet_attempts WHERE job_id='J1'")}
    assert rows == {"lane-a": ("codex", "gpt-x"), "lane-b": ("muse", "muse-spark-1.3-contributor")}
    s.finish("lane-a", {"a/hello.py": "x = 1\n"})
    s.finish("lane-b", {"b/world.py": "y = 2\n"})
    rep = runner.tick("J1")
    for _ in range(5):
        if rep.state == "NEEDS_HUMAN":
            break
        rep = runner.tick("J1")
    assert rep.state == "NEEDS_HUMAN", rep
    assert sorted(FleetStore(store).integrated_tasks("J1")) == ["a", "b"]
    lanes = {ln["lane"]: ln for ln in FleetStore(store).lanes_view("J1")}
    assert lanes["lane-a"]["runtime"] == "codex" and lanes["lane-b"]["runtime"] == "muse"
    assert lanes["lane-a"]["usage"]["tokens"] is None  # the fake reported none: unknown stays None, never 0


def test_an_unknown_assigned_runtime_fails_the_lane_start_and_never_falls_back(steward_home, tmp_path, monkeypatch):
    store, _ = mixed_seeded(tmp_path, steward_home, monkeypatch)
    fallback = []
    factory = fleet_live.runtime_lane_factory(store, structured=lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("no adapter")), fabric=lambda **kw: fallback.append(kw))
    with pytest.raises(RuntimeError):
        factory(job_id="J1", lane_id="lane-a", task_id="a", attempt_no=1, epoch=1)
    assert not fallback


def test_a_claude_fabric_lane_still_uses_the_fabric_factory(steward_home, tmp_path):
    store, _ = seeded(tmp_path, steward_home)   # v2.4 Claude-only roster: no assignments
    got = []
    factory = fleet_live.runtime_lane_factory(store, fabric=lambda **kw: got.append(kw) or "fabric-adapter",
                                              structured=lambda *a, **k: pytest.fail("structured"))
    assert factory(job_id="J1", lane_id="lane-a", task_id="a", attempt_no=1, epoch=1) == "fabric-adapter"
    assert fleet_live.needs_herdr(store, "J1") is True


def test_structured_only_fleets_do_not_need_herdr(steward_home, tmp_path, monkeypatch):
    store, _ = mixed_seeded(tmp_path, steward_home, monkeypatch)
    assert fleet_live.needs_herdr(store, "J1") is False
    with pytest.raises(RuntimeError, match="no frozen runtime assignment"):
        fleet_live.runtime_lane_factory(store, fabric=None, structured=None)(
            job_id="J1", lane_id="lane-zz", task_id="zz", attempt_no=1, epoch=1)


def test_pid_probe_maps_process_state():
    import os
    probe = fleet_live.runtime_probe(None)
    assert probe("J1", "lane-a", {"pid": os.getpid()}) == rt.ALIVE
    assert probe("J1", "lane-a", {"pid": 2 ** 22 + 12345}) == rt.DEAD
    assert probe("J1", "lane-a", {}) == rt.UNKNOWN


def test_fleet_graph_shows_lane_runtime_model_and_usage(steward_home, tmp_path, monkeypatch, capsys):
    test_lanes_run_on_their_assigned_runtimes_and_integrate_into_one_candidate(steward_home, tmp_path, monkeypatch)
    capsys.readouterr()
    assert cli.main(["fleet", "graph", "J1"]) == 0
    out = capsys.readouterr().out
    assert "runtime=codex model=gpt-x" in out and "runtime=muse model=muse-spark-1.3-contributor" in out
    assert "tokens=unknown cost=unknown" in out
    assert cli.main(["fleet", "graph", "J1", "--json"]) == 0
    lanes = {ln["lane"]: ln for ln in json.loads(capsys.readouterr().out)["lanes"]}
    assert lanes["lane-b"]["runtime"] == "muse" and lanes["lane-b"]["usage"] == {"tokens": None, "cost_usd": None}


def test_status_pane_shows_each_lane_runtime_model_and_unknown_usage(steward_home, tmp_path, monkeypatch):
    from steward import dashact, dashrender
    test_lanes_run_on_their_assigned_runtimes_and_integrate_into_one_candidate(steward_home, tmp_path, monkeypatch)
    out = dashrender.Pane(140, 0, dashact.state_path(str(tmp_path / "dash.json")),
                          store=Store(steward_home)).render(refresh=True)
    text = "\n".join(out) if isinstance(out, list) else str(out)
    assert "lane-a  codex/gpt-x" in text and "lane-b  muse/muse-spark-1.3-contributor" in text
    assert "tokens unknown · cost unknown" in text


def test_a_candidate_blocked_for_writing_outside_its_claims_goes_to_the_owner_instead_of_looping(
        steward_home, tmp_path):
    """Live 2026-10-04 (overlap provocation, job 64ee323b): a lane wrote NOTES.md outside its claimed paths; the
    writer blocked it (`out_of_claim_paths`) - correctly - but the job then re-attempted the same blocked candidate
    every tick and never settled. Deterministic for that SHA: like a merge conflict, it is the owner's."""
    store, repo = seeded(tmp_path, steward_home)
    store.add_check("J1", commit_sha=git(repo, "rev-parse", "HEAD"), command="true", exit_code=0, results={},
                    label="baseline")
    runner = _runner(store)
    world = World()
    fleet_live.wire(runner, "J1", adapter_factory=world.factory, probe=world.probe, runner_exec=SubprocessRunner())
    runner.start("J1")
    commit_lane(world, "lane-a", {"a/hello.py": "x = 1\n", "NOTES.md": "a: started\n"})
    commit_lane(world, "lane-b", {"b/world.py": "y = 2\n"})
    states = [runner.tick("J1").state for _ in range(4)]
    assert states[-1] == "NEEDS_HUMAN", states
    blocked = [h for h in FleetStore(store).integration_history("J1") if h["outcome"] == "blocked"]
    assert blocked and blocked[0]["reason"].startswith("out_of_claim_paths")
    assert len(blocked) <= 2   # no attempt-per-tick loop once the job is the owner's
    assert store.events("J1")[-1].what == "fleet task cannot complete"
