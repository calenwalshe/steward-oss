"""Fleet-aware deadline projection (Phase 29, FLEET-19). Pure reads over the Store: it writes nothing.

The forecast is a critical-path RANGE over the validated remaining task DAG, scheduled onto the real lane
allowance (so queueing and claim conflicts are modelled, never perfect speedup), followed by a serial integration
queue (the exclusive IntegrationWriter) and the existing verification/closeout tail (`deadline_plan.verification_tail_min`).
Task durations come only from OBSERVED integrated tasks of the same runtime; with too few comparable samples, or a
runtime with no history of its own (e.g. a new Codex lane), the answer is `estimating` with no numbers - unknown
latency stays unknown. Progress counts only tasks with an integrated ledger row whose output SHA resolves in Git:
a lane's own commit is not milestone evidence. Original commitment target slots (`deadline_targets`) are read back
untouched and never revised here (they are human-owned).
"""

from __future__ import annotations

import copy
import heapq
import math
from datetime import datetime, timezone
from typing import Callable

from steward import deadline_plan
from steward import fleet_lease as fl
from steward import fleet_graph as fg
from steward.fleet_store import FleetStore
from steward.store import Store

MIN_COMPARABLE = 2          # observed same-runtime task durations before any number is shown
Z = 1.28                    # p10/p90 half-width in standard deviations (matches steward.eta)
SD_FALLBACK = 0.35          # relative spread when all samples are identical
INTEGRATION_DEFAULT_MIN = 2.0   # serial merge time per task until integration timings are observable
RUN_FLOOR = 0.2             # a running task always has at least this share of its estimate left
MIN_MIN = 0.5


def _parse(ts: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def duration_range(samples: list[float]) -> tuple[float, float, float] | None:
    """(lo, mid, hi) minutes from observed samples, or None below MIN_COMPARABLE."""
    xs = [x for x in samples if x is not None and x > 0]
    if len(xs) < MIN_COMPARABLE:
        return None
    mean = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (len(xs) - 1))
    sd = sd if sd > 0 else SD_FALLBACK * mean
    return max(mean - Z * sd, MIN_MIN), mean, mean + Z * sd


def observed_durations(store: Store, job_id: str) -> list[dict]:
    """One row per INTEGRATED task: runtime + minutes from the lane's first attempt to its admitted candidate."""
    fs = FleetStore(store)
    out = []
    for task_id in sorted(fs.integrated_tasks(job_id)):
        cand = fs.latest_candidate(job_id, task_id)
        if cand is None:
            continue
        att = store._conn.execute("SELECT started_at, runtime FROM fleet_attempts WHERE job_id=? AND lane_id=?"
                                  " ORDER BY attempt_no LIMIT 1", (job_id, cand["lane_id"])).fetchone()
        a, b = _parse(att["started_at"]) if att else None, _parse(cand["recorded_at"])
        if att is None or a is None or b is None or b <= a:
            continue
        out.append({"task": task_id, "runtime": att["runtime"], "minutes": (b - a).total_seconds() / 60.0})
    return out


def _task_runtime(fs: FleetStore, job_id: str, task_id: str) -> str | None:
    rt_ = fs.lane_runtime(job_id, f"lane-{task_id}")
    return rt_["runtime"] if rt_ else None


def critical_path_min(tasks: list[fg.Task], est: dict) -> float:
    """Longest dependency chain (sum of estimates) over `tasks` only; dependencies outside the set are done."""
    by = {t.id: t for t in tasks}
    memo: dict = {}

    def length(i):
        if i not in memo:
            memo[i] = est[i] + max([length(d) for d in by[i].depends_on if d in by] or [0.0])
        return memo[i]

    return max([length(i) for i in by] or [0.0])


def schedule(tasks: list[fg.Task], done: set, running: dict, est: dict, lanes: int, integ_min: float):
    """Deterministic list scheduling. `running` = {task_id: minutes left}. Dependents start only after their
    dependency is INTEGRATED (serial queue), claim-conflicting tasks never overlap. Returns
    (makespan_min, first_blocked_reason|None) where makespan ends at the last integration."""
    graph = fg.Graph(tasks=list(tasks))
    done = set(done)
    active = {t: running[t] for t in running}          # task -> finish time (t=0 now)
    integ_free, ev, now = 0.0, [], 0.0                  # ev: (time, kind, task); kinds: 'f' finish, 'i' integrated
    for t, left in active.items():
        heapq.heappush(ev, (left, "f", t))
    last = 0.0
    remaining = {t.id for t in tasks} - done
    launched = set(active)   # a task is started once: finishing frees its lane, integration unblocks dependents
    while True:
        free = max(lanes - len(active), 0)
        if free:
            for t in graph.ready_tasks(done=frozenset(done), active=frozenset(active)):
                if free <= 0:
                    break
                if t.id in remaining and t.id not in launched:
                    launched.add(t.id)
                    active[t.id] = now + est[t.id]
                    heapq.heappush(ev, (active[t.id], "f", t.id))
                    free -= 1
        if not ev:
            break
        now, kind, tid = heapq.heappop(ev)
        if kind == "f":
            active.pop(tid, None)
            start = max(now, integ_free)
            integ_free = start + integ_min
            heapq.heappush(ev, (integ_free, "i", tid))
        else:
            done.add(tid)
            remaining.discard(tid)
            last = max(last, now)
    if remaining:
        return None, f"graph_blocked: {','.join(sorted(remaining))}"
    return last, None


def forecast(store: Store, job_id: str, *, now: datetime | None = None, lanes: int | None = None,
             integration_min: float | None = None, tail_min: float | None = None, held=()) -> dict:
    """Fleet deadline range in minutes from `now`. See module docstring for the model and its honesty rules."""
    fs = FleetStore(store)
    view = fs.graph_view(job_id)
    now = now or datetime.now(timezone.utc)
    if not view or view["mode"] != "fleet" or view.get("implicit"):
        return {"status": "not_fleet", "range_min": None, "reasons": ["no explicit fleet graph"]}
    snap = fs.get_snapshot(job_id) or {}
    lanes = lanes or fl.effective_max_lanes((snap.get("limits") or {}).get("max_lanes"))
    tail = deadline_plan.verification_tail_min() if tail_min is None else tail_min
    integrated = fs.integrated_tasks(job_id)
    tasks = [fg.Task(id=t["id"], objective=t["objective"], done_when=tuple(t["done_when"]),
                     depends_on=tuple(t["depends_on"]), owns=tuple(t["owns"]), required=t["required"],
                     priority=t["priority"]) for t in view["tasks"]]
    remaining = [t for t in tasks if t.id not in integrated]
    obs = observed_durations(store, job_id)
    integ = INTEGRATION_DEFAULT_MIN if integration_min is None else integration_min
    out = {"status": "range", "range_min": None, "graph_rev": view["graph_rev"], "lanes": lanes,
           "remaining_tasks": [t.id for t in remaining], "integrated_tasks": sorted(integrated),
           "comparable_samples": len(obs), "verification_tail_min": tail,
           "integration": {"per_task_min": integ, "source": "default" if integration_min is None else "observed"},
           "reasons": []}
    if not remaining:
        out["range_min"] = {"p10": tail, "p50": tail, "p90": tail}
        out["critical_path_min"] = {"p10": 0.0, "p50": 0.0, "p90": 0.0}
        out["capacity"] = {"lanes": lanes, "work_min": {"p50": 0.0}}
        return out
    est: dict = {}   # task -> (lo, mid, hi)
    pooled = duration_range([o["minutes"] for o in obs])
    for t in remaining:
        runtime = _task_runtime(fs, job_id, t.id)
        mine = [o["minutes"] for o in obs if o["runtime"] == runtime] if runtime else None
        r = duration_range(mine) if mine is not None else pooled
        if r is None:
            out["reasons"].append(f"latency_unknown: {runtime or 'unassigned'} has "
                                  f"{len(mine) if mine is not None else len(obs)} comparable sample(s), need "
                                  f"{MIN_COMPARABLE}")
        else:
            est[t.id] = r
    if len(est) != len(remaining):
        out["status"] = "estimating"
        out["reasons"] = sorted(set(out["reasons"]))
        return out
    blocked = [t.id for t in remaining if t.id in set(held)]
    if blocked:
        out.update(status="estimating", reasons=[f"integration_held: {','.join(sorted(blocked))}"])
        return out
    running_left: dict = {}
    finished = {tid for tid, st in fl.task_states(store, job_id).items() if st == "finished"}
    for t in remaining:
        lane = f"lane-{t.id}"
        lease = fl.get(store, job_id, lane)
        if lease and lease["state"] in fl.HELD:
            att = store._conn.execute("SELECT started_at FROM fleet_attempts WHERE job_id=? AND lane_id=?"
                                      " ORDER BY attempt_no DESC LIMIT 1", (job_id, lane)).fetchone()
            began = _parse(att["started_at"]) if att else None
            running_left[t.id] = (now - began).total_seconds() / 60.0 if began else 0.0
    res, cp, work = {}, {}, {}
    for name, idx in (("p10", 0), ("p50", 1), ("p90", 2)):
        e = {i: est[i][idx] for i in est}
        left = {tid: max(e[tid] - el, RUN_FLOOR * e[tid]) for tid, el in running_left.items()}
        left.update({tid: 0.0 for tid in finished if tid in e})  # finished, awaiting the serial integration queue
        span, why = schedule(tasks, set(integrated), left, {**e}, lanes, integ)
        if span is None:
            out.update(status="estimating", reasons=[why])
            return out
        res[name] = round(span + tail, 2)
        eff = {tid: (left[tid] if tid in left else e[tid]) for tid in e}
        cp[name] = round(critical_path_min(remaining, eff), 2)
        work[name] = round(sum(eff.values()), 2)
    out["range_min"] = res
    out["critical_path_min"] = cp
    out["capacity"] = {"lanes": lanes, "work_min": work,
                       "bound_min": {k: round(work[k] / lanes, 2) for k in work},
                       "queue_min": {k: round(res[k] - tail - max(cp[k], work[k] / lanes), 2) for k in res}}
    return out


# -- milestones from integrated SHAs --------------------------------------------------------------------------------

def git_resolver(runner, repo) -> Callable[[str], bool]:
    """sha -> True iff Git can resolve it to a commit in `repo` (via the injected command runner)."""
    def resolve(sha: str) -> bool:
        if not sha:
            return False
        res = runner.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo)
        return res.exit_code == 0
    return resolve


def plan_milestone_tasks(store: Store, job_id: str) -> dict:
    """milestone id -> graph task ids named by the adopted plan's `items` (only ids that are real tasks)."""
    fs = FleetStore(store)
    view = fs.graph_view(job_id) or {}
    ids = {t["id"] for t in view.get("tasks", [])}
    plan = deadline_plan.current_plan(store, job_id) or {}
    return {m["id"]: [i for i in (m.get("items") or []) if i in ids] for m in plan.get("milestones") or []}


def milestone_facts(store: Store, job_id: str, milestone_tasks: dict, resolve: Callable[[str], bool]) -> dict:
    """milestone id -> {met, met_sha, met_at, tasks, missing, unresolved}. A milestone is met only when EVERY named
    task has an integrated ledger row whose output SHA resolves; unintegrated lane commits are not evidence."""
    fs = FleetStore(store)
    rows = {r["task_id"]: r for r in fs.integration_history(job_id) if r["outcome"] == "integrated"}
    out = {}
    for mid, tasks in milestone_tasks.items():
        missing = [t for t in tasks if t not in rows]
        unresolved = [t for t in tasks if t in rows and not resolve(rows[t]["output_sha"] or "")]
        met = bool(tasks) and not missing and not unresolved
        last = max((rows[t] for t in tasks if t in rows), key=lambda r: r["seq"], default=None) if met else None
        out[mid] = {"met": met, "tasks": list(tasks), "missing": missing, "unresolved": unresolved,
                    "met_sha": last["output_sha"] if last else None, "met_at": last["created_at"] if last else None}
    return out


def target_slots(store: Store, job_id: str) -> list[dict]:
    """The original commitment target slots exactly as persisted (a deep copy; this module never writes them)."""
    return copy.deepcopy(deadline_plan.targets(store, job_id))


def projection(store: Store, job_id: str, *, resolve: Callable[[str], bool], now: datetime | None = None,
               milestone_tasks: dict | None = None, **kw) -> dict:
    mt = plan_milestone_tasks(store, job_id) if milestone_tasks is None else milestone_tasks
    from steward.fleet_runtime import integration_holds
    return {"forecast": forecast(store, job_id, now=now, held=tuple(integration_holds(store, job_id)), **kw),
            "milestones": milestone_facts(store, job_id, mt, resolve), "slots": target_slots(store, job_id)}
