#!/usr/bin/env python3
"""Live multi-runtime fleet proof in the ISOLATED Steward home (Milestone v2.5, D-03). Never touches live Steward.

Refuses to run unless STEWARD_HOME and STEWARD_CONFIG_DIR are under ~/.cache/steward-proof/. Strips every Herdr
variable, so nothing can reach a live pane or the live control pane. Steps (each the production code path):

  1. a disposable Git repo with a committed 2-task PLAN.md + -FLEET.json manifest (optionally steered so both tasks
     touch the same file: --overlap, the Phase 32 lanes_overlapping provocation);
  2. `steward new <goal> --repo <repo> --no-notify` through the in-process control relay - the same handlers
     `steward control` runs (the real CLI relays writes over Herdr to the control pane; here there is none);
  3. `fleet_launch.prepare(..., max_lanes=2, runtimes=...)` - exactly what `steward launch --fleet` runs before it
     opens the runner's Herdr tab - and the launch receipt lines; or, with --fleet-plan, step 2 is instead
     `steward new <goal> --fleet-plan PLAN --fleet-manifest MANIFEST --fleet-roster ROSTER --fleet-tech-lead LEAD
     --fleet-pin add=<id> --fleet-pin mul=<id>` (the snapshot-bound `steward.fleet_services` path, where each task
     is pinned to the roster worker of the --runtimes runtime) and there is no launch step;
  4. `steward run <job> --worker fabric --interval N` as a subprocess (structured-only fleets need no Herdr), polled
     until the job leaves RUNNING; then `steward fleet graph <job>`.

Usage: scripts/fleet_live_proof.py --runtimes codex,pi [--overlap] [--jev-standin] [--timeout 2400]
       scripts/fleet_live_proof.py --fleet-plan --runtimes muse [--tech-lead astra-tech-lead]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The isolated proof root: ~/.cache/steward-proof, or $STEWARD_PROOF_ROOT (e.g. a directory inside the job's own
# worktree, so every proof write stays there). Repos, debug logs and the Claude dir all live under it.
PROOF = Path(os.environ.get("STEWARD_PROOF_ROOT") or Path.home() / ".cache" / "steward-proof")
LIVE_HOME = Path.home() / ".local" / "state" / "steward"
LIVE_CONFIG = Path.home() / ".config" / "steward"
CLI = [sys.executable, "-c", "import sys; from steward.cli import main; sys.exit(main(sys.argv[1:]))"]


def guard() -> dict:
    home, cfg = os.environ.get("STEWARD_HOME", ""), os.environ.get("STEWARD_CONFIG_DIR", "")
    root = PROOF.resolve()
    if root in (LIVE_HOME.resolve(), LIVE_CONFIG.resolve()) or root in LIVE_HOME.resolve().parents \
            or root in LIVE_CONFIG.resolve().parents:
        sys.exit(f"refused: proof root {root} is, or contains, the live Steward home/config")
    for name, val in (("STEWARD_HOME", home), ("STEWARD_CONFIG_DIR", cfg)):
        if not val or root not in Path(val).resolve().parents:
            sys.exit(f"refused: {name}={val!r} is not under {PROOF} (the isolated proof home)")
        if Path(val).resolve() in (LIVE_HOME.resolve(), LIVE_CONFIG.resolve()):
            sys.exit(f"refused: {name}={val!r} is the live Steward home/config")
    for v in [k for k in os.environ if k.startswith("HERDR_")]:
        os.environ.pop(v)
    os.environ.pop("META_API_KEY", None)
    os.environ.setdefault("STEWARD_EXCEPTIONS", str(Path(cfg) / "exceptions.toml"))
    os.environ.setdefault("STEWARD_DATA_DIR", str(PROOF / "data"))
    os.environ.setdefault("STEWARD_CLAUDE_DIR", str(PROOF / "claude-dir"))
    os.environ["STEWARD_BG_BASELINE"] = "0"
    os.environ["STEWARD_AUTODEPLOY"] = "0"
    os.environ["STEWARD_RESOLVER"] = "0"
    os.environ.pop("STEWARD_NOTIFIER", None)
    os.environ.pop("STEWARD_FORCED_ORIGIN", None)
    for sub in ("claude-dir", "dbg", "tmp"):
        (PROOF / sub).mkdir(parents=True, exist_ok=True)
    return dict(os.environ)


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"git {args}: {r.stderr}")
    return r.stdout.strip()


def make_repo(overlap: bool) -> Path:
    repo = PROOF / "tmp" / f"proof-repo-{time.strftime('%Y%m%dT%H%M%S')}"
    (repo / "calc").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "calc" / "__init__.py").write_text('"""Tiny calculator package (fleet proof)."""\n')
    (repo / "tests" / "test_base.py").write_text("import calc\n\n\ndef test_package_imports():\n    assert calc\n")
    (repo / "pyproject.toml").write_text('[project]\nname = "calc"\nversion = "0.0.0"\n\n'
                                         '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (repo / "README.md").write_text("# calc\n\nRun the tests with `python3 -m pytest -q`.\n")
    plan = ".planning/phases/01-calc/01-01-PLAN.md"
    (repo / plan).parent.mkdir(parents=True)
    (repo / plan).write_text("# 01-01 calc\n\nTwo independent tasks: add() and mul(), each with its own test.\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=proof", "-c", "user.email=proof@localhost", "commit", "-q", "-m", "plan")
    blob = git(repo, "rev-parse", f"HEAD:{plan}")
    if overlap:
        # Phase 32 provocation: the manifest claims disjoint files, but both briefs steer the lanes to the SAME file,
        # first thing, so both lanes are live with the shared path for several triage rounds.
        a = {"id": "add", "objective": "FIRST append the line 'add: started' to NOTES.md at the repo root (create it "
             "if missing). Then create calc/add.py with a documented add(a, b) returning a + b, and "
             "tests/test_add.py with at least three pytest cases.", "owns": ["calc/add.py", "tests/test_add.py"]}
        b = {"id": "mul", "objective": "FIRST append the line 'mul: started' to NOTES.md at the repo root (create it "
             "if missing). Then create calc/mul.py with a documented mul(a, b) returning a * b, and "
             "tests/test_mul.py with at least three pytest cases.", "owns": ["calc/mul.py", "tests/test_mul.py"]}
    else:
        a = {"id": "add", "objective": "Create calc/add.py with add(a, b) returning a + b, and tests/test_add.py "
             "with a pytest test of it.", "owns": ["calc/add.py", "tests/test_add.py"]}
        b = {"id": "mul", "objective": "Create calc/mul.py with mul(a, b) returning a * b, and tests/test_mul.py "
             "with a pytest test of it.", "owns": ["calc/mul.py", "tests/test_mul.py"]}
    tasks = [{**t, "done_when": [f"python3 -m pytest -q passes including the new {t['id']} test"],
              "depends_on": [], "evidence": ["python3 -m pytest -q"], "required": True, "priority": 1}
             for t in (a, b)]
    manifest = {"schema_version": 1, "plan_blob_id": blob, "tasks": tasks}
    (repo / ".planning/phases/01-calc/01-01-FLEET.json").write_text(json.dumps(manifest, indent=2) + "\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=proof", "-c", "user.email=proof@localhost", "commit", "-q", "-m", "fleet manifest")
    return repo, plan


def roster_pins(runtimes: list[str]) -> list[str]:
    """`--fleet-pin` values for the two proof tasks: each names the first enabled roster worker of its runtime (one
    runtime -> both tasks on it), read from the isolated config dir's roster."""
    from steward import fleet_registry
    from steward.store import resolve_config_dir

    roster = fleet_registry.load_roster(resolve_config_dir() / "roster.toml")
    ids = []
    for runtime in runtimes:
        entry = next((e for e in roster.entries if e.runtime == runtime and e.enabled and "worker" in e.roles), None)
        if entry is None:
            sys.exit(f"refused: the proof roster has no enabled {runtime} worker")
        ids.append(entry.id)
    ids = (ids * 2)[:2]
    return [f"add={ids[0]}", f"mul={ids[1]}"]


def in_process_relay():
    from steward import cli
    from steward.control_pane import ControlPane

    def relay(store, op, args, *, request_id=None, origin="cli", **_ignored):
        pane = ControlPane(store, cli._build_control_handlers(store), print_fn=lambda _line: None)
        return pane.execute({"request_id": request_id or str(uuid.uuid4()), "op": op, "args": args,
                             "origin": origin})
    cli._relay = relay
    return cli


IN_FLIGHT = ("RUNNING", "CONTRACTED", "CREATED", "WORKER_FINISHED", "VERIFYING")


def assess(store, job_id: str, runtimes: list[str] | None = None, *, timed_out: bool = False) -> tuple[bool, list[str]]:
    """The proof verdict from the job's own record, never the runner's exit status. A fleet job never auto-accepts, so
    a SUCCESSFUL proof is NEEDS_HUMAN whose only open owner item is the acceptance decision, with: every graph task's
    lane finished (`task finished`, lease released `task_finished`) on one of `runtimes`, every task integrated, and
    checks passed at the combined candidate SHA. Anything else - a timeout, a stopped/failed/never-started lane, a
    missing integration or verification, any other reason to need the owner - is a failed proof."""
    from steward.fleet_store import FleetStore

    fs, problems, lines = FleetStore(store), [], []
    job = store.get_job(job_id)
    state = job.state.value if job else "unknown"
    if timed_out or state in IN_FLIGHT:
        problems.append(f"timeout: job still {state}")
    elif state != "NEEDS_HUMAN":
        problems.append(f"job ended {state}, not NEEDS_HUMAN (awaiting acceptance)")
    view = fs.graph_view(job_id) or {}
    tasks = [t["id"] for t in view.get("tasks") or []]
    if not tasks:
        problems.append("no fleet graph tasks")
    fleet = fs.fleet_events(job_id)
    lanes = {ln["task"]: ln for ln in fs.lanes_view(job_id)}
    integrated = fs.integrated_tasks(job_id)
    for task in tasks:
        ln = lanes.get(task)
        if ln is None:
            problems.append(f"task {task}: no lane ever started")
            continue
        finished = any(e["what"] == "task finished" and e["lane_id"] == ln["lane"] for e in fleet)
        released = [json.loads(e["evidence"] or "{}").get("evidence", {}).get("reason") for e in fleet
                    if e["what"] == "lane lease released" and e["lane_id"] == ln["lane"]]
        lines.append(f"  {ln['lane']}: runtime={ln['runtime']} model={ln['model']} attempts={len(ln['attempts'])} "
                     f"finished={finished} released={released or '-'} integrated={integrated.get(task, '-')} "
                     f"tokens={ln['usage'].get('tokens')}")
        if not finished:
            problems.append(f"{ln['lane']}: no `task finished` (lane state {ln['state']})")
        if released and released[-1] != "task_finished":
            problems.append(f"{ln['lane']}: lease released for {released[-1]!r}, not task_finished")
        if runtimes and ln["runtime"] not in runtimes:
            problems.append(f"{ln['lane']}: ran on {ln['runtime']!r}, not one of {runtimes}")
        if task not in integrated:
            problems.append(f"task {task}: never integrated")
    final = next((json.loads(e["evidence"] or "{}").get("final_sha") for e in reversed(fleet)
                  if e["what"] == "fleet candidate recorded"), None)
    events = store.events(job_id)
    passed = [e.evidence.get("commit_sha") for e in events if e.what == "checks passed at exact commit"]
    if final is None:
        problems.append("no combined fleet candidate recorded")
    elif final not in passed:
        problems.append(f"checks did not pass at the combined candidate {final[:12]}")
    owner = next((e.evidence for e in reversed(events) if e.what == "ownership_status"), {})
    kinds = owner.get("owner_kinds") or []
    if state == "NEEDS_HUMAN" and kinds != ["acceptance_decision"]:
        problems.append(f"needs the owner for {kinds or 'an unrecorded reason'}, not only the acceptance decision")
    lines.insert(0, f"  state={state} candidate={final or '-'} checks_passed_at={passed or '-'} owner_kinds={kinds}")
    verdict = "PASS" if not problems else "FAIL: " + "; ".join(problems)
    return not problems, [f"proof verdict: {verdict}", *lines]


STANDIN = "measured_overlap_standin (TypeSafe Jev unavailable: 402 billing_error)"


def install_jev_standin() -> None:
    """ONLY for the --jev-standin proof run, in the runner child process: the Jev verdict for a lane is replaced by
    a deterministic reading of the lane context's MEASURED overlap (real git paths of two live lanes). Labelled on
    every flag (`observer`). Everything downstream - gate, Arbiter, tech-lead packet, the real Astra/Fable call,
    shadow recording - is the production code."""
    from steward.jev_observer import JevObserver

    def observe_lane(self, store, job_id, lane_id, signals):
        choice = "lanes_overlapping" if signals.get("overlap") else "ok"
        return {"choice": choice, "confidence": 1.0, "probabilities": {choice: 1.0}, "lane": lane_id,
                "observer": STANDIN}
    JevObserver.observe_lane = observe_lane


def runner_child(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job")
    ap.add_argument("--interval", type=float, default=5)
    ap.add_argument("--jev-standin", action="store_true")
    a = ap.parse_args(argv)
    guard()
    if a.jev_standin:
        install_jev_standin()
    from steward import cli
    return cli.main(["run", a.job, "--worker", "fabric", "--interval", str(a.interval)])


def main() -> int:
    if sys.argv[1:2] == ["--runner-child"]:
        return runner_child(sys.argv[2:])
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtimes", default="codex,pi")
    ap.add_argument("--overlap", action="store_true")
    ap.add_argument("--jev-standin", action="store_true",
                    help="label-carrying deterministic stand-in for the Jev verdict (TypeSafe unavailable)")
    ap.add_argument("--timeout", type=float, default=2400)
    ap.add_argument("--interval", type=float, default=5)
    ap.add_argument("--fleet-plan", action="store_true",
                    help="create the job with `steward new --fleet-plan` (snapshot-bound fleet services path)")
    ap.add_argument("--tech-lead", default="astra-tech-lead", help="roster tech lead for --fleet-plan")
    a = ap.parse_args()
    env = guard()
    from steward import fleet_launch
    from steward.store import Store

    repo, plan = make_repo(a.overlap)
    print(f"repo: {repo}  HEAD={git(repo, 'rev-parse', 'HEAD')}")
    cli = in_process_relay()
    goal = ("Fleet proof: add calc.add and calc.mul with tests" + (" (overlap provocation)" if a.overlap else ""))
    import contextlib
    import io
    from steward.store import resolve_config_dir
    argv = ["new", goal, "--repo", str(repo), "--no-notify"]
    if a.fleet_plan:
        argv += ["--fleet-plan", str(repo / plan), "--fleet-manifest", str(repo / plan.replace("-PLAN.md", "-FLEET.json")),
                 "--fleet-roster", str(resolve_config_dir() / "roster.toml"), "--fleet-tech-lead", a.tech_lead,
                 "--fleet-max-lanes", "2"]
        for pin in roster_pins(a.runtimes.split(",")):
            argv += ["--fleet-pin", pin]
        print("$ steward " + " ".join(argv))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    print(buf.getvalue().strip())
    store = Store()
    job = max(store.list_jobs(), key=lambda j: j.created_at)
    print(f"job: {job.job_id}  state={job.state.value}  rc={rc}")
    if a.fleet_plan:
        from steward.fleet_store import FleetStore
        snap = FleetStore(store).get_snapshot(job.job_id)
        if snap is None:
            sys.exit("refused: `steward new --fleet-plan` froze no fleet snapshot")
        print(f"fleet snapshot: eligible={snap['eligible']} pins={snap['pins']} tech_lead={snap['tech_lead']}")
    else:
        res = fleet_launch.prepare(store, job.job_id, max_lanes=2, runtimes=a.runtimes.split(","))
        print("launch --fleet receipt:")
        for line in cli._fleet_receipt_lines(res):
            print(line)
    log = PROOF / "dbg" / f"runner-{job.job_id[:8]}.log"
    t0 = time.time()
    with open(log, "w") as fh:
        proc = subprocess.Popen([sys.executable, __file__, "--runner-child", job.job_id, "--interval",
                                 str(a.interval), *(["--jev-standin"] if a.jev_standin else [])],
                                cwd=str(ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    print(f"runner pid {proc.pid}, log {log}")
    state, timed_out = None, True
    while time.time() - t0 < a.timeout:
        time.sleep(10)
        state = Store().get_job(job.job_id).state.value
        if state not in IN_FLIGHT:
            timed_out = False
            break
    print(f"job {job.job_id} -> {state} after {time.time() - t0:.0f}s" + (" (TIMEOUT)" if timed_out else ""))
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("$ steward fleet graph", job.job_id)
    print(subprocess.run([*CLI, "fleet", "graph", job.job_id], cwd=str(ROOT), env=env, capture_output=True,
                         text=True).stdout)
    ok, lines = assess(Store(), job.job_id, [r.split(":", 1)[0] for r in a.runtimes.split(",")], timed_out=timed_out)
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
