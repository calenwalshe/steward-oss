"""Code-only wait conditions with bounded, exponentially backed-off probes."""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Callable

from steward.models import Judgment
from steward.store import Store

FIRST_PROBE_S = 60.0
WAIT_BACKOFF = 2.0
MAX_PROBE_S = 3600.0
PROBES_PER_SWEEP = 50
GIT_TIMEOUT_S = 5.0


class WaitError(ValueError):
    pass


ProbeFn = Callable[[Store, dict, dict], tuple[bool, dict]]
PROBES: dict[str, ProbeFn] = {}
_BASELINES: dict[str, Callable[[Store, str, dict], dict]] = {}


def register_probe(kind: str, fn: ProbeFn, baseline: Callable[[Store, str, dict], dict] | None = None) -> None:
    PROBES[kind] = fn
    if baseline is not None:
        _BASELINES[kind] = baseline


def _worktree(store: Store, job_id: str) -> Path:
    return (Path(store.home) / "worktrees" / job_id).resolve()


def _inside(store: Store, job_id: str, rel: str) -> Path:
    root = _worktree(store, job_id)
    p = (root / str(rel)).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise WaitError("a file_exists wait must name a path inside the job's worktree") from None
    return p


def _p_job_state(store, row, params):
    job = store.get_job(row["job_id"])
    return (job is not None and job.state.value in set(params.get("states") or []),
            {"state": job.state.value if job else None})


def _p_time(store, row, params):
    return float(row["_now"]) >= float(params["at"]), {"at": params["at"]}


def _b_evidence(store, job_id, params):
    from steward import ownership

    events = store.events(job_id)
    return {"seq": events[-1].seq if events else 0, "authority_rev": ownership.authority_rev(store, job_id)}


def _p_evidence(store, row, params):
    from steward import blockers, ownership

    base = json.loads(row["baseline_json"] or "{}")
    if ownership.authority_rev(store, row["job_id"]) != base.get("authority_rev"):
        return True, {"changed": "authority"}
    wanted = set(params.get("events") or [])
    new = [e for e in store.events_since(row["job_id"], int(base.get("seq", 0))) if not wanted or e.what in wanted]
    if row["blocker_id"]:
        b = blockers.get(store, row["blocker_id"])
        if b is not None and base.get("digest") and b.evidence_digest != base["digest"]:
            return True, {"changed": "blocker_evidence"}
    return bool(new), {"events": [e.what for e in new[:5]]}


def _p_approval(store, row, params):
    from steward import approvals

    a = approvals.get_approval(store, row["job_id"], params["approval_id"])
    return (a is not None and a.get("status") != "pending"), {"status": a.get("status") if a else None}


def _p_file(store, row, params):
    p = _inside(store, row["job_id"], params["path"])
    return p.exists(), {"path": str(params["path"])}


def _p_service(store, row, params):
    service = params.get("service")
    if service == "jev":
        from steward import jev_watch

        return jev_watch.outage_status(store) is None, {"service": "jev"}
    from steward import review_service

    return not review_service.ReviewOutage(store, lambda text: True).is_down(), {"service": "review"}


def _git_sha(params) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(params["repo"]), "rev-parse", str(params["ref"])],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _p_branch(store, row, params):
    sha = _git_sha(params)
    base = json.loads(row["baseline_json"] or "{}")
    return (sha is not None and sha != base.get("sha")), {"sha": sha}


def _p_headroom(store, row, params):
    from steward import spend_policy

    chk = spend_policy.check_hard(store, row["job_id"], source=params.get("source", "tech_lead"),
                                  bound_usd=params.get("bound_usd"), now=float(row["_now"]))
    return chk.ok, {"reason": chk.reason}


register_probe("job_state", _p_job_state)
register_probe("time", _p_time)
register_probe("evidence_change", _p_evidence, _b_evidence)
register_probe("approval_decided", _p_approval)
register_probe("file_exists", _p_file)
register_probe("service_up", _p_service)
register_probe("branch_moved", _p_branch, lambda store, job_id, params: {"sha": _git_sha(params)})
register_probe("hard_cap_headroom", _p_headroom)

CHECK_RUN_FINAL = ("done", "failed", "lost", "cancelled")


def _p_check_run(store, row, params):
    """v2.7 Phase 42: a Steward-owned check run the worker parked on is final (a vanished row never parks forever)."""
    from steward import check_runs

    run = check_runs.get(store, str(params.get("run_id") or ""))
    if run is None:
        return True, {"run_id": params.get("run_id"), "state": "missing"}
    return run.state in CHECK_RUN_FINAL, {"run_id": run.run_id, "state": run.state}


def _wake_check_run(store, row, detail, now) -> None:
    """Resume the worker with the result: ONE nudge steer, deduped by the wait id so a runner probe and the control-pane
    sweep racing on the same wake record it once. Delivery is the runner's ordinary steer path (a new worker turn)."""
    from steward import check_runs
    from steward.steering import Steering

    run_id = str(row["params"].get("run_id") or "")
    text = f"[steward] check run {run_id} finished: {check_runs.result_summary(check_runs.get(store, run_id))}"
    Steering(store).record(row["job_id"], "nudge", text, request_id=f"check-run-{row['wait_id']}", origin="check_run")


register_probe("check_run", _p_check_run)
_WAKE_HOOKS: dict[str, Callable[[Store, dict, dict, float], None]] = {"check_run": _wake_check_run}


def _wait_id(job_id: str, blocker_id: str | None, kind: str, params: dict) -> str:
    raw = json.dumps([job_id, blocker_id, kind, params], sort_keys=True, default=str)
    return "wait-" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def _row(r) -> dict:
    d = dict(r)
    d["params"] = json.loads(d.pop("params_json") or "{}")
    return d


def park(store: Store, job_id: str, *, blocker_id: str | None, kind: str, params: dict, reason: str,
         now: float) -> dict:
    from steward import blockers
    from steward.actor import Actor, ActorRef

    if kind not in PROBES:
        raise WaitError(f"unknown wait kind {kind!r}")
    if kind == "file_exists":
        _inside(store, job_id, params.get("path", ""))
    if kind == "time" and "at" not in params:
        raise WaitError("a time wait needs `at`")
    if kind == "approval_decided" and not params.get("approval_id"):
        raise WaitError("an approval wait needs `approval_id`")
    if kind == "check_run" and not params.get("run_id"):
        raise WaitError("a check_run wait needs `run_id`")
    if kind == "branch_moved" and not (params.get("repo") and params.get("ref")):
        raise WaitError("a branch wait needs `repo` and `ref`")
    wid = _wait_id(job_id, blocker_id, kind, params)
    existing = store._conn.execute("SELECT * FROM wait_conditions WHERE wait_id = ? AND state = 'waiting'",
                                   (wid,)).fetchone()
    if existing is not None:
        return _row(existing)
    baseline = _BASELINES.get(kind, lambda s, j, p: {})(store, job_id, params)
    if blocker_id:
        b = blockers.get(store, blocker_id)
        if b is not None:
            baseline["digest"] = b.evidence_digest
    with store.immediate() as tx:
        tx.conn.execute(
            "INSERT OR REPLACE INTO wait_conditions (wait_id, job_id, blocker_id, kind, params_json, reason, state,"
            " baseline_json, probes, next_probe_epoch, created_epoch) VALUES (?, ?, ?, ?, ?, ?, 'waiting', ?, 0, ?, ?)",
            (wid, job_id, blocker_id, kind, json.dumps(params, sort_keys=True), str(reason)[:400],
             json.dumps(baseline), now + FIRST_PROBE_S, now))
        tx.append_event(job_id, what="wait_parked", evidence={"wait_id": wid, "blocker_id": blocker_id, "kind": kind,
                                                              "params": params, "reason": str(reason)[:200]},
                        rule="waits", judgment=Judgment.code)
    if blocker_id and blockers.get(store, blocker_id) is not None:
        b = blockers.get(store, blocker_id)
        if b.is_open and b.disposition != "waiting_external":
            blockers.transition(store, blocker_id, "waiting_external", actor=ActorRef(Actor.CONTROLLER, "waits"),
                                reason=f"waiting: {reason}"[:200], now=now, wake={"kind": kind, **params})
    return _row(store._conn.execute("SELECT * FROM wait_conditions WHERE wait_id = ?", (wid,)).fetchone())


def waiting(store: Store, job_id: str) -> list[dict]:
    rows = store._conn.execute("SELECT * FROM wait_conditions WHERE job_id = ? AND state = 'waiting'"
                               " ORDER BY created_epoch", (job_id,)).fetchall()
    return [_row(r) for r in rows]


def status_suffix(store: Store, job_id: str, *, now: float) -> str:
    rows = waiting(store, job_id)
    if not rows:
        return ""
    r = rows[0]
    nxt = time.strftime("%H:%M", time.localtime(float(r["next_probe_epoch"])))
    more = f" (+{len(rows) - 1} more)" if len(rows) > 1 else ""
    return f"waiting: {r['kind']} — {r['reason'][:80]} (next check {nxt}, no paid polling){more}"


def cancel(store: Store, wait_id: str, *, reason: str, now: float) -> None:
    with store.immediate() as tx:
        row = tx.conn.execute("SELECT job_id FROM wait_conditions WHERE wait_id = ? AND state = 'waiting'",
                              (wait_id,)).fetchone()
        if row is None:
            return
        tx.conn.execute("UPDATE wait_conditions SET state = 'cancelled', woken_epoch = ? WHERE wait_id = ?",
                        (now, wait_id))
        tx.append_event(row["job_id"], what="wait_cancelled", evidence={"wait_id": wait_id, "reason": reason[:200]},
                        rule="waits", judgment=Judgment.code)


def _wake(store: Store, row: dict, detail: dict, now: float) -> None:
    from steward import blockers, ownership
    from steward.actor import Actor, ActorRef

    hook = _WAKE_HOOKS.get(row["kind"])
    if hook is not None:
        hook(store, row, detail, now)  # idempotent; a raise leaves the wait open for the next probe
    with store.immediate() as tx:
        cur = tx.conn.execute("UPDATE wait_conditions SET state = 'woken', woken_epoch = ?, wake_json = ?"
                              " WHERE wait_id = ? AND state = 'waiting'", (now, json.dumps(detail), row["wait_id"]))
        if cur.rowcount != 1:
            return
        tx.append_event(row["job_id"], what="wait_woken",
                        evidence={"wait_id": row["wait_id"], "kind": row["kind"], "detail": detail},
                        rule="waits", judgment=Judgment.code)
    if row["blocker_id"]:
        b = blockers.get(store, row["blocker_id"])
        if b is not None and b.disposition == "waiting_external":
            b = blockers.transition(store, b.blocker_id, "candidate", actor=ActorRef(Actor.CONTROLLER, "waits"),
                                    reason=f"wake condition met: {row['kind']}", now=now,
                                    evidence=[f"wait:{row['wait_id']}"])
            ownership.submit_review_fact(store, b, reason=f"wake condition met: {row['kind']} {detail}"[:200])


def sweep(store: Store, *, now: float) -> dict:
    """Probe due waits without models or network-dependent work."""
    out = {"probed": 0, "woken": 0, "errors": 0}
    rows = store._conn.execute("SELECT * FROM wait_conditions WHERE state = 'waiting' AND next_probe_epoch <= ?"
                               " ORDER BY next_probe_epoch LIMIT ?", (now, PROBES_PER_SWEEP)).fetchall()
    for r in rows:
        row = {**_row(r), "_now": now}
        out["probed"] += 1
        try:
            met, detail = PROBES[row["kind"]](store, row, row["params"])
        except Exception as exc:  # noqa: BLE001 - one broken probe never breaks the sweep
            met, detail = False, {"error": type(exc).__name__}
            out["errors"] += 1
        if met:
            try:
                _wake(store, row, detail, now)
            except Exception as exc:  # noqa: BLE001 - a failed wake hook leaves the wait open; never breaks the sweep
                out["errors"] += 1
                _hook_error(store, row, exc)
                continue
            out["woken"] += 1
            continue
        nxt = min(MAX_PROBE_S, FIRST_PROBE_S * (WAIT_BACKOFF ** (int(row["probes"]) + 1)))
        with store.immediate() as tx:
            tx.conn.execute("UPDATE wait_conditions SET probes = probes + 1, next_probe_epoch = ? WHERE wait_id = ?",
                            (now + nxt, row["wait_id"]))
    return out


def _hook_error(store: Store, row: dict, exc: Exception) -> None:
    store.append_event(row["job_id"], what="wait_wake_error", evidence={"wait_id": row["wait_id"], "kind": row["kind"],
                                                                        "error": f"{type(exc).__name__}: {exc}"[:200]},
                       rule="waits", judgment=Judgment.code)


def probe_job(store: Store, job_id: str, *, now: float, kinds: tuple[str, ...] | None = None) -> dict:
    """Probe THIS job's open waits right now, ignoring the sweep's backoff (a cheap code-only read per wait). The
    runner calls it each tick for `check_run` waits so a finished run resumes the worker within a tick."""
    out = {"probed": 0, "woken": 0, "errors": 0}
    for r in waiting(store, job_id):
        if kinds is not None and r["kind"] not in kinds:
            continue
        row = {**r, "_now": now}
        out["probed"] += 1
        try:
            met, detail = PROBES[row["kind"]](store, row, row["params"])
            if not met:
                continue
            before = len(waiting(store, job_id))
            _wake(store, row, detail, now)
            out["woken"] += before - len(waiting(store, job_id))
        except Exception as exc:  # noqa: BLE001 - one broken probe/hook never breaks the tick
            out["errors"] += 1
            _hook_error(store, row, exc)
    return out
