"""Ordinary CLI lifecycle proof with real Git worktrees and deterministic local lane adapters.

This exercises create -> run -> process restart/reattach -> candidate -> serial integration -> exact-SHA closeout.
The fake adapter is local test infrastructure; no model provider, Herdr service or user notification is contacted.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from steward import cli, fleet_lease, fleet_runtime
from steward.fabric import Frame
from steward.fleet_store import FleetStore
from steward.models import JobState
from steward.runner_exec import SubprocessRunner
from steward.store import Store
from steward.worker import WorkerObservation


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


class LocalWorker:
    """A stable fake process identity whose worktree commit is emitted only after a CLI restart."""

    def __init__(self, shared, job_id, lane_id, task_id, attempt_no, epoch, lease_check=None):
        self.shared = shared
        self.job_id, self.lane_id, self.task_id = job_id, lane_id, task_id
        self.attempt_no, self.epoch = attempt_no, epoch
        self.ref = f"local-worker-{lane_id}-{attempt_no}"
        self.worktree = None

    def start(self, brief, worktree):
        self.worktree = Path(worktree)
        self.shared["starts"] += 1
        self.shared["alive"][self.ref] = True
        self.shared["worktrees"][self.lane_id] = self.worktree
        return self.ref

    def attach(self, ref):
        self.ref = ref
        self.worktree = self.shared["worktrees"].get(self.lane_id)
        self.shared["attaches"] += 1
        return self.shared["alive"].get(ref, False)

    def process_identity(self):
        return {"pid": 12345}

    def stop(self):
        self.shared["stops"] += 1
        self.shared["alive"][self.ref] = False
        return True

    def observe(self):
        self.shared["observations"] += 1
        if self.shared["observations"] == 1:
            return WorkerObservation(lifecycle="working", output_tail="", pid_alive=True, ts="first-run")

        target = self.worktree / "src" / "a.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("answer = 42\n")
        git(self.worktree, "add", "src/a.py")
        subprocess.run(["git", "-c", "user.name=Local Worker", "-c", "user.email=worker@localhost",
                        "commit", "-qm", "implement task"], cwd=self.worktree, check=True)
        candidate = git(self.worktree, "rev-parse", "HEAD")
        self.shared["alive"][self.ref] = False
        frame = Frame(v=2, seq=1, job=self.job_id, type="done",
                      body={"candidate_sha": candidate, "evidence_ref": "local-proof:checks-pass"},
                      task=self.task_id, lane=self.lane_id, attempt=self.attempt_no,
                      sender=f"shim:{self.lane_id}", epoch=self.epoch)
        return WorkerObservation(lifecycle="exited", output_tail="done", pid_alive=False, ts="finished",
                                 events=(frame,))

    def deliver(self, _text, *, urgent=False):
        return SimpleNamespace(ok=True, detail="local test delivery")

    def interrupt(self):
        return None

    def answer_prompt(self, approve, request_id=None):
        return SimpleNamespace(ok=True, detail="no permission request in this trial")


def test_public_create_run_restart_and_verified_candidate(steward_home, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("answer = 0\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_check.py").write_text("from src.a import answer\ndef test_answer(): assert answer == 42\n")
    (repo / "PLAN.md").write_text("# Implement answer\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@localhost")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base plan")
    plan_blob = git(repo, "rev-parse", "HEAD:PLAN.md")

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "plan_blob_id": plan_blob, "tasks": [{
        "id": "t1", "objective": "set answer to 42", "done_when": ["pytest passes"],
        "depends_on": [], "owns": ["src/a.py"], "required": True, "priority": 1,
    }]}))
    roster = tmp_path / "roster.toml"
    roster.write_text('''schema_version = 1
revision = 1

[[entries]]
id = "local-worker"
provider = "local"
runtime = "claude-code"
model = "test-worker"
roles = ["worker"]
capabilities = ["edit", "tests"]
strengths = []
enabled = true
label = "Local test worker"

[[entries]]
id = "local-lead"
provider = "local"
runtime = "claude-code"
model = "test-lead"
roles = ["tech_lead"]
capabilities = ["review"]
strengths = []
enabled = true
label = "Local test lead"
''')

    store = Store(steward_home)
    fs = FleetStore(store)
    fs.record_qualification("claude-code", "local-test", ["edit", "tests", "review"],
                             qualified=True, available=True)
    for capability in ("launch", "identity", "poll", "permission_relay", "typed_result", "cancel",
                       "stop_confirm", "reattach"):
        fs.record_runtime_capability("claude-code", capability, qualified=True, adapter_version="local-test",
                                     evidence="deterministic local adapter lifecycle probe", source="test")
    handlers = cli._build_control_handlers(store)

    def in_process_relay(_store, op, args, **kwargs):
        return handlers[op]({"op": op, "args": args, "request_id": kwargs.get("request_id"),
                             "origin": kwargs.get("origin", "cli")})

    monkeypatch.setattr(cli, "_relay", in_process_relay)
    monkeypatch.setattr(cli, "_sync_exceptions_registry", lambda: None)
    new_args = cli._build_parser().parse_args([
        "new", "implement answer", "--repo", str(repo), "--no-baseline", "--no-notify",
        "--fleet-plan", "PLAN.md", "--fleet-manifest", str(manifest), "--fleet-roster", str(roster),
        "--fleet-tech-lead", "local-lead", "--fleet-pin", "t1=local-worker", "--fleet-max-lanes", "1",
    ])
    assert cli._cmd_new(store, new_args) == 0
    new_output = capsys.readouterr().out
    job_id = new_output.splitlines()[0].split("\t")[0]
    assert fs.get_snapshot(job_id)["tech_lead"] == "local-lead"
    assert fs.graph_view(job_id)["mode"] == "fleet"
    assert store.get_job(job_id).state == JobState.CONTRACTED

    shared = {"starts": 0, "attaches": 0, "stops": 0, "observations": 0, "alive": {}, "worktrees": {}}
    service_builds = []

    class EmptyMailboxSource:
        def read(self, timeout):
            return []

        def close(self):
            return None

    def service_factory(st, jid, args, runner_exec, *, runner, **service_kwargs):
        service_builds.append(jid)
        return original_factory(st, jid, args, runner_exec, runner=runner,
                                adapter_factory=lambda **kw: LocalWorker(shared, **kw),
                                probe=lambda _j, _l, ident: fleet_runtime.ALIVE if
                                    shared["alive"].get((ident or {}).get("ref"), False) else fleet_runtime.DEAD,
                                advisor=lambda _packet: None,
                                mailbox_source=EmptyMailboxSource() if not service_kwargs.get("mailbox_router")
                                else None, **service_kwargs)

    from steward import fleet_services
    original_factory = fleet_services.build_fleet_services
    monkeypatch.setattr(fleet_services, "build_fleet_services", service_factory)
    monkeypatch.setattr(cli, "build_sol", lambda *_: None)
    monkeypatch.setattr(cli, "_build_trajectory_supervisor", lambda *_: None)
    from steward.runner import Runner
    monkeypatch.setattr(Runner, "_notify", lambda *_: None)

    run_argv = ["run", job_id, "--worker", "fabric", "--pane", "local-caller", "--sock",
                str(tmp_path / "herdr.sock"), "--once"]
    assert cli._cmd_run(store, cli._build_parser().parse_args(run_argv)) == 0
    assert store.get_job(job_id).state == JobState.RUNNING
    assert shared["starts"] == 1 and shared["observations"] == 1, (service_builds, store.events(job_id))

    # Model a process restart: close the Store and use the public fleet resume path to reconcile durable leases.
    store.close()
    restarted_store = Store(steward_home)
    from steward import herdr_transport
    monkeypatch.setattr(herdr_transport, "Mailbox", EmptyMailboxSource)
    resume_argv = ["resume", "--worker", "fabric", "--pane", "local-caller", "--sock",
                   str(tmp_path / "herdr.sock"), "--once"]
    assert cli._cmd_resume_all(restarted_store, cli._build_parser().parse_args(resume_argv)) == 0

    assert service_builds == [job_id, job_id]
    assert shared["starts"] == 1 and shared["attaches"] == 1 and shared["stops"] == 1
    assert restarted_store.get_job(job_id).state == JobState.NEEDS_HUMAN
    assert fleet_lease.task_states(restarted_store, job_id)["t1"] == "finished"
    integrated = FleetStore(restarted_store).integrated_tasks(job_id)
    assert "t1" in integrated
    outcomes = FleetStore(restarted_store).outcomes(job_id)
    assert len(outcomes) == 1
    assert outcomes[0]["candidate_sha"] and outcomes[0]["integration_sha"] == integrated["t1"]
    assert outcomes[0]["evidence_ref"] == "local-proof:checks-pass"
    verification = cli.StewardAPI(restarted_store).get_verification(job_id)
    candidate = verification["fleet_candidate"]
    assert verification["verified"] is True
    assert candidate["candidate"] is True and candidate["final_sha"] == candidate["verified_sha"] == integrated["t1"]
    assert candidate["tasks"][0]["evidence_ref"] == "local-proof:checks-pass"
