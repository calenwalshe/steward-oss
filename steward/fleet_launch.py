"""Explicit fleet opt-in at launch: seed the task graph and freeze the roster snapshot (wiring for Phases 26-29).

`steward launch --fleet [--max-lanes N] [--plan PATH]` is the ONLY way a job becomes a fleet job. Before any write
it requires both a validated operator roster (an enabled worker and an enabled tech lead) and an explicit task graph:
the GSD companion manifest `<plan>-FLEET.json` committed beside `<plan>-PLAN.md`, compiled by
`fleet_graph.compile_graph` against the committed PLAN.md Git blob id. Anything else - no manifest, a malformed or
stale one, an invalid graph, a bad roster - is refused with its visible reason and nothing is recorded, so the job
stays exactly what it was: a legacy single-worker job. Without `--fleet` none of this runs.

On success it records, in order: the frozen roster snapshot (`fleet_jobs`), graph revision 1 as the current graph
(`fleet_graph_revisions` + `fleet_job_state`), each lane's frozen runtime assignment (`fleet_assignments`) and the
launch choice the runner reads to wire the controller.

Lane assignment (v2.5, FLEET-28/29, D-05..D-07): the roster is the allowlist. A worker is ELIGIBLE only when it is
enabled, has the worker role, its runtime is qualified for `start` on the CLI version installed now. Without
`--runtimes` lanes are spread across eligible workers (distinct runtimes first, then strengths matching the task,
then cost); with `--runtimes codex,muse` (or `runtime:model`) only those are used, and a named runtime with no
eligible roster entry - or a model the roster does not list - refuses the launch before anything is written, naming
the fix. Skipped workers are listed with their reason. Nothing falls back to Claude: a roster whose structured
workers are all undispatchable is refused. Only a Claude-only roster (the v2.4 shape, no structured runtime at all)
keeps the v2.4 behaviour of running its first worker unassigned.
"""

from __future__ import annotations

import json
import subprocess
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

from steward import fleet_graph as fg
from steward import fleet_lease as fl
from steward import fleet_registry as fr
from steward import fleet_runtime as rt
from steward.fleet_store import FleetStore
from steward.models import JobState, Judgment
from steward.store import Store, resolve_config_dir

LAUNCH_KEY = "fleet_launch"  # kv: what --fleet chose (worker entry, requested model, tech lead, lanes, plan)
MANIFEST_SUFFIX = "-FLEET.json"
PLAN_SUFFIX = "-PLAN.md"


class FleetRefused(Exception):
    """`--fleet` was asked for but the job cannot run as a fleet; `reason` is a stable slug."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason, self.detail = reason, detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class Compiled:
    graph: fg.Graph
    plan: str | None
    manifest: str | None
    plan_blob_id: str | None


def _git(repo, args, runner: Callable | None = None):
    run = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False))
    try:
        return run(["git", "-C", str(repo), *args])
    except (OSError, subprocess.SubprocessError):
        return None


def manifest_for(plan: str) -> str:
    """`20-01-PLAN.md` -> `20-01-FLEET.json` beside it (any other plan name: `<stem>-FLEET.json`)."""
    p = PurePosixPath(plan)
    stem = p.name[: -len(PLAN_SUFFIX)] if p.name.endswith(PLAN_SUFFIX) else p.stem
    return str(p.with_name(stem + MANIFEST_SUFFIX))


def plan_for(manifest: str) -> str:
    p = PurePosixPath(manifest)
    return str(p.with_name(p.name[: -len(MANIFEST_SUFFIX)] + PLAN_SUFFIX))


def discover_plan(repo, runner: Callable | None = None) -> str | None:
    """The one plan whose companion manifest is committed at HEAD. None if there is none; two or more is ambiguous
    (the operator names one with --plan) - parallelism is never guessed."""
    r = _git(repo, ["ls-tree", "-r", "--name-only", "HEAD"], runner)
    if r is None or getattr(r, "returncode", 1) != 0:
        return None
    found = sorted(p for p in (getattr(r, "stdout", "") or "").splitlines() if p.endswith(MANIFEST_SUFFIX))
    if len(found) > 1:
        raise FleetRefused("ambiguous_manifest", f"{len(found)} committed *{MANIFEST_SUFFIX} files "
                                                 f"({', '.join(found[:4])}); name one with --plan")
    return plan_for(found[0]) if found else None


def compile_for_repo(repo, plan: str | None, *, goal: str = "", runner: Callable | None = None) -> Compiled:
    """Compile the committed companion manifest of `plan` (repo-relative PLAN.md). Absent, unreadable, malformed
    or stale metadata compiles to the one-task implicit graph with its visible reason (never raises for those)."""
    if plan is None:
        return Compiled(fg.compile_graph(None, committed_plan_blob_id=None, goal=goal), None, None, None)
    plan = str(PurePosixPath(plan))
    manifest = manifest_for(plan)
    blob = fg.committed_blob_id(str(repo), plan, runner)
    shown = _git(repo, ["show", f"HEAD:{manifest}"], runner)
    if shown is None or getattr(shown, "returncode", 1) != 0:
        data = None  # not committed (an uncommitted file is not plan metadata)
    else:
        try:
            data = json.loads(getattr(shown, "stdout", "") or "")
        except ValueError:
            data = "unparseable manifest"
    return Compiled(fg.compile_graph(data, committed_plan_blob_id=blob, goal=goal), plan, manifest, blob)


def seed_graph(store: Store, job_id: str, graph: fg.Graph) -> int | None:
    """Record an explicit, valid graph as revision 1 and make it current. Returns the current revision, or None
    (nothing written) for an implicit or invalid graph: that job stays a legacy single-worker job. Idempotent."""
    fs = FleetStore(store)
    cur = fs.current_graph(job_id)
    if cur is not None:
        return cur[0]
    if graph.implicit or graph.errors:
        return None
    fs.record_graph(job_id, 1, graph)
    if not fs.set_current_graph(job_id, 1, expected_version=0):
        raise RuntimeError("graph_changed_concurrently")
    fs.append_event(job_id, what="fleet graph seeded", actor="steward_launch", rule="fleet_manifest_seed",
                    judgment=Judgment.code, graph_rev=1,
                    evidence={"plan_blob_id": graph.plan_blob_id, "graph_hash": graph.graph_hash,
                              "tasks": [t.id for t in graph.tasks]})
    return 1


FABRIC_RUNTIME = "claude-fabric"


def tech_leads(roster: fr.Roster) -> list[fr.RosterEntry]:
    """Enabled tech-lead entries in fallthrough order: `priority` (lower first; unset last), then id."""
    leads = [e for e in roster.entries if e.enabled and "tech_lead" in e.roles]
    return sorted(leads, key=lambda e: (e.priority if e.priority is not None else 10_000, e.id))


def worker_ineligibility(fs: FleetStore, entry: fr.RosterEntry) -> str | None:
    """None when `entry` may take a lane now; else why not, with the fix."""
    if not entry.enabled:
        return f"disabled in the roster (enable {entry.id} in roster.toml)"
    if "worker" not in entry.roles:
        return "not a worker entry"
    stale = fs.stale(entry.runtime)
    if stale:
        return stale
    q = fs.qualification(entry.runtime)
    missing = fr.missing_capabilities(fs.qualified_capabilities(entry.runtime), "start")
    if q is None and missing == sorted(fr.ACTION_REQUIREMENTS["start"]):
        from steward import fleet_qualify
        if entry.runtime not in fleet_qualify.SUITES:
            return (f"runtime {entry.runtime} has no probe suite, so it cannot be runtime-qualified for fleet lanes "
                    f"(probe suites: {', '.join(sorted(fleet_qualify.SUITES))})")
        return f"runtime {entry.runtime} was never qualified; run `steward fleet qualify {entry.runtime}`"
    if missing:
        return (f"runtime {entry.runtime} is NOT qualified for {', '.join(missing)}; "
                f"run `steward fleet qualify {entry.runtime}` once it can demonstrate them")
    if not fs.dispatchable(entry):
        return (f"roster claims capabilities the {entry.runtime} qualification does not cover; "
                f"run `steward fleet qualify {entry.runtime}`")
    return None


@dataclass(frozen=True)
class LaneChoice:
    roster_id: str
    runtime: str
    model: str
    provider: str
    rule: str

    def view(self) -> dict:
        return {"roster_id": self.roster_id, "runtime": self.runtime, "model": self.model, "rule": self.rule}


@dataclass
class AssignmentPlan:
    lanes: dict = field(default_factory=dict)      # task_id -> LaneChoice
    skipped: list = field(default_factory=list)    # [{"id", "runtime", "reason"}]
    mode: str = "auto"                             # auto | pinned | roster_default


def parse_runtimes(raw) -> list[str]:
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    return [str(x).strip() for x in items if str(x).strip()]


def _cost_rank(e: fr.RosterEntry) -> float:
    """Cheaper first: an explicit `low cost` strength, then the lowest recorded price observation, unknown last."""
    if any("low cost" in s.lower() or s.lower() == "cheap" for s in e.strengths):
        return 0.0
    prices = []
    for obs in e.observations:
        o = dict(obs)
        if o.get("kind") == "price":
            m = re.search(r"\d+(?:\.\d+)?", o.get("value", ""))
            if m:
                prices.append(float(m.group()))
    return 1.0 + min(prices) if prices else 1e9


def _strength_match(e: fr.RosterEntry, text: str) -> int:
    words = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    return sum(1 for s in e.strengths if set(re.findall(r"[a-z0-9]+", s.lower())) & words - {"and", "the", "of"})


def plan_assignments(fs: FleetStore, roster: fr.Roster, task_ids, *, objectives: dict | None = None,
                     pins=None) -> AssignmentPlan:
    """Pure: which roster worker takes each task's lane. Deterministic for a given roster, store and graph.
    Raises FleetRefused for a pin it cannot honour or when no worker is eligible (never a Claude fallback)."""
    objectives = objectives or {}
    workers = [e for e in roster.entries if "worker" in e.roles]
    why = {e.id: worker_ineligibility(fs, e) for e in workers}
    eligible = [e for e in workers if why[e.id] is None]
    plan = AssignmentPlan(skipped=[{"id": e.id, "runtime": e.runtime, "reason": why[e.id]}
                                   for e in workers if why[e.id] is not None])
    pins = parse_runtimes(pins)
    if pins:
        chosen = []
        for token in pins:
            runtime, _, model = token.partition(":")
            listed = [e for e in workers if e.runtime == runtime]
            if not listed:
                raise FleetRefused("not_in_roster", f"runtime {runtime!r} has no worker entry in the roster (the "
                                                    "roster is the allowlist; add one to roster.toml)")
            if model:
                listed = [e for e in listed if e.model == model]
                if not listed:
                    raise FleetRefused("off_roster_model", f"{runtime}:{model} is not in the roster (allowlist) even "
                                                           f"though {runtime} may be installed and qualified; add the "
                                                           "model to roster.toml to use it")
            ok = [e for e in listed if why[e.id] is None]
            if not ok:
                reasons = "; ".join(f"{e.id}: {why[e.id]}" for e in listed)
                raise FleetRefused("runtime_not_dispatchable", f"{token} cannot take a lane - {reasons}")
            chosen += [e for e in ok if e not in chosen]
        pool, rule, plan.mode = chosen, "pinned", "pinned"
    else:
        pool, rule = eligible, "auto_assign"
        if not pool:
            structured = [e for e in workers if e.enabled and e.runtime != FABRIC_RUNTIME]
            if not structured:
                plan.mode = "roster_default"   # the v2.4 Claude-only roster: unchanged behaviour
                return plan
            raise FleetRefused("no_dispatchable_worker", "no roster worker can take a lane (no fallback to Claude): "
                               + "; ".join(f"{s['id']}: {s['reason']}" for s in plan.skipped))
    used: dict[str, int] = {}
    for tid in task_ids:
        text = objectives.get(tid, "")
        best = min(pool, key=lambda e: (used.get(e.runtime, 0), -_strength_match(e, text), _cost_rank(e), e.id))
        used[best.runtime] = used.get(best.runtime, 0) + 1
        plan.lanes[tid] = LaneChoice(best.id, best.runtime, best.model, best.provider, rule)
    return plan


def load_operator_roster(path: Path | str | None = None) -> tuple[fr.Roster, fr.RosterEntry, fr.RosterEntry]:
    """The operator roster (default `<config dir>/roster.toml`) with its first enabled worker and (in fallthrough
    order) first tech lead."""
    p = Path(path) if path else resolve_config_dir() / "roster.toml"
    if not p.exists():
        raise FleetRefused("no_roster", f"{p} does not exist")
    try:
        roster = fr.load_roster(p)
    except (fr.RosterError, OSError) as exc:
        raise FleetRefused("roster_invalid", str(exc)) from None
    worker = next((e for e in roster.entries if e.enabled and "worker" in e.roles), None)
    lead = next(iter(tech_leads(roster)), None)
    if worker is None:
        raise FleetRefused("roster_no_worker", f"{p} has no enabled worker entry")
    if lead is None:
        raise FleetRefused("roster_no_tech_lead", f"{p} has no enabled tech_lead entry")
    return roster, worker, lead


def prepare(store: Store, job_id: str, *, max_lanes: int | None = None, plan: str | None = None,
            roster_path: Path | str | None = None, runner: Callable | None = None, runtimes=None,
            fabric_ok: bool = True) -> dict:
    """Validate everything, then (only then) write the snapshot, graph revision 1 and the launch choice.
    Raises FleetRefused - with nothing written - unless roster AND explicit graph are both present."""
    job = store.get_job(job_id)
    if job is None:
        raise FleetRefused("unknown_job", job_id)
    if rt.is_fleet_job(store, job_id):  # re-launch / play of an already-seeded fleet job
        return {"status": "already", **(store.kv_get(job_id, LAUNCH_KEY) or {})}
    if job.state != JobState.CONTRACTED:
        raise FleetRefused("not_launchable", f"job is {job.state.value}; only a contracted, never-launched job "
                                             "can opt into fleet mode")
    lanes = fl.DEFAULT_LANES if max_lanes is None else max_lanes
    if isinstance(lanes, bool) or not isinstance(lanes, int) or not 1 <= lanes <= fl.HARD_LANE_CAP:
        raise FleetRefused("bad_max_lanes", f"--max-lanes must be 1..{fl.HARD_LANE_CAP}, got {max_lanes!r}")
    roster, worker, lead = load_operator_roster(roster_path)
    plan = plan or discover_plan(job.repo, runner)
    compiled = compile_for_repo(job.repo, plan, goal=job.goal, runner=runner)
    g = compiled.graph
    if g.implicit:
        raise FleetRefused("no_explicit_graph", g.fallback_reason)
    if g.errors:
        raise FleetRefused("invalid_graph", "; ".join(g.errors[:3]))

    fs = FleetStore(store)
    objectives = {t.id: " ".join([t.objective, *t.owns]) for t in g.tasks}
    assigned = plan_assignments(fs, roster, [t.id for t in g.tasks], objectives=objectives, pins=runtimes)
    lane_choice = {rt.lane_id_for(tid): c for tid, c in assigned.lanes.items()}
    if not fabric_ok and (assigned.mode == "roster_default"
                          or any(c.runtime == FABRIC_RUNTIME for c in lane_choice.values())):
        raise FleetRefused("fabric_worker_required", "Claude fabric fleet lanes need --worker fabric")
    if assigned.lanes:   # the receipt names the worker the lanes actually run on
        first = roster.get(next(iter(assigned.lanes.values())).roster_id)
        worker = first or worker
    snap = fs.get_snapshot(job_id)
    if snap is None:
        dispatchable = {e.id for e in roster.entries if fs.dispatchable(e)}
        try:
            snap = fs.snapshot_job(
                roster, job_id=job_id, pins={ln: c.roster_id for ln, c in lane_choice.items() if c.rule == "pinned"},
                policy="fleet_opt_in", tech_lead=lead.id if lead.id in dispatchable else None,
                limits={"max_lanes": lanes},
                reason=f"steward launch --fleet: worker {worker.id} ({worker.model}), tech lead {lead.id} "
                       f"({lead.model}){'' if worker.id in dispatchable else '; roster runtime not yet qualified: requested model only'}"
                       + (f"; lanes {assigned.mode}: " + ", ".join(f"{ln}={c.runtime}/{c.model}"
                                                                   for ln, c in sorted(lane_choice.items()))
                          if lane_choice else ""))
        except fr.RosterError as exc:
            raise FleetRefused("roster_snapshot", str(exc)) from None
        for ln, c in sorted(lane_choice.items()):
            fs.record_assignment(job_id, ln, roster_id=c.roster_id, actor="steward_launch",
                                 reason=f"{assigned.mode}: {c.runtime}/{c.model}", policy_rule=c.rule)
    rev = seed_graph(store, job_id, g)
    choice = {"max_lanes": lanes, "plan": compiled.plan, "manifest": compiled.manifest,
              "plan_blob_id": compiled.plan_blob_id, "graph_rev": rev, "roster_hash": roster.hash,
              "worker": worker.id, "worker_model": worker.model, "worker_runtime": worker.runtime,
              "worker_provider": worker.provider, "tech_lead": lead.id, "tech_lead_model": lead.model,
              "tech_leads": [e.id for e in tech_leads(roster)], "assignment_mode": assigned.mode,
              "assignments": {ln: c.view() for ln, c in sorted(lane_choice.items())},
              "skipped": assigned.skipped}
    store.kv_set(job_id, LAUNCH_KEY, choice)
    fs.append_event(job_id, what="fleet mode opted in", actor="steward_launch", rule="fleet_explicit_opt_in",
                    judgment=Judgment.code, registry_hash=snap.get("registry_hash"), graph_rev=rev, evidence=choice)
    return {"status": "seeded", **choice}
