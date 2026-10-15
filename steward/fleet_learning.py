"""Auditable outcome learning for the fleet (Phase 29-02, FLEET-20).

Two halves, deliberately separate:

* `record_job_outcomes` is the ONLY writer. It derives one insert-only outcome per SETTLED attempt from facts the
  controller and the Phase 28 integration ledger already recorded (attempt identity, candidate/integration SHAs,
  lease release, usage events, integration conflicts). Nothing is measured or guessed here: a figure that was never
  observed (duration, tokens, cost, task class) stays None.
* `compare_outcomes` / `outcome_view` are pure reads. They group ONLY outcomes whose repository and task class the
  caller named explicitly, report per-group sample counts, descriptive facts and missing-data rates, and return no
  rank, score, recommendation or assignment input. Nothing here feeds dispatch, promotion or the roster.
"""

from __future__ import annotations

import json
from datetime import datetime
from statistics import mean, median

from steward.fleet_store import FleetStore
from steward.store import Store

NOTICE = ("descriptive only: not a ranking, recommendation, assignment input or promotion decision; "
          "unknown measurements are excluded from averages and counted as missing")
MEASURES = ("duration_s", "tokens", "cost_usd")


def _ts(s: str | None) -> float | None:
    try:
        return datetime.fromisoformat(s).timestamp() if s else None
    except ValueError:
        return None


def _ev(e: dict) -> dict:
    try:
        return json.loads(e["evidence"])
    except (TypeError, ValueError):
        return {}


def record_job_outcomes(store: Store, job_id: str, *, task_classes: dict | None = None) -> list[dict]:
    """Append the outcome of every settled attempt not yet recorded. Returns the NEW rows only.
    Settled = its lease was released (stop recorded) and, if it produced a candidate, that candidate's
    integration reached a terminal ledger outcome. `task_classes` ({task_id: class}) is the caller's explicit
    classification; a missing class stays None."""
    fs = FleetStore(store)
    job = store.get_job(job_id)
    if job is None:
        return []
    events = fs.fleet_events(job_id)
    attempts = store._conn.execute("SELECT * FROM fleet_attempts WHERE job_id=? ORDER BY lane_id, attempt_no",
                                   (job_id,)).fetchall()
    cands = fs.candidates(job_id)
    hist = fs.integration_history(job_id)
    cur_graph = fs.current_graph(job_id)
    out = []
    for att in attempts:
        lane, n = att["lane_id"], att["attempt_no"]
        lane_row = store._conn.execute("SELECT task_id FROM fleet_lanes WHERE job_id=? AND lane_id=?",
                                       (job_id, lane)).fetchone()
        if lane_row is None:
            continue
        mine = [e for e in events if e["lane_id"] == lane and e["attempt_no"] == n]
        released = [e for e in mine if e["what"] == "lane lease released"]
        if not released:
            continue  # still running / unconfirmed: an outcome would be a guess
        cand = next((c for c in reversed(cands) if c["lane_id"] == lane and c["attempt_no"] == n), None)
        ints = [h for h in hist if h["lane_id"] == lane and h["attempt_no"] == n]
        integrated = next((h for h in ints if h["outcome"] == "integrated"), None)
        if cand is not None and not any(h["outcome"] in ("integrated", "conflict", "blocked") for h in ints):
            continue  # candidate awaiting the serial integration writer
        started = next((e for e in mine if e["what"] == "lane_started"), None)
        t0, t1 = _ts(started["ts"] if started else None), _ts(released[-1]["ts"])
        usage = next((_ev(e).get("usage") for e in reversed(mine) if e["what"] == "lane_usage"), None) or {}
        reason = (_ev(released[-1]).get("evidence") or {}).get("reason")
        asg = [a for a in fs.assignments(job_id, lane) if a["runtime"] == att["runtime"] and a["model"] == att["model"]
               and a["provider"] == att["provider"]]
        a = asg[-1] if asg else None
        failures = [{"kind": "integration_blocked", "reason": h["reason"][:160]} for h in ints
                    if h["outcome"] == "blocked"]
        failures += [{"kind": "stop_unconfirmed"} for e in mine if e["what"] == "stop_unconfirmed"]
        conflicts = [{"paths": h["conflict_paths"], "reason": h["reason"][:160]} for h in ints
                     if h["outcome"] == "conflict"]
        recovery = {"replacement_attempt": n > 1, "stop_reason": reason,
                    "reassigned": bool(reason and str(reason).startswith("reassign")),
                    "worker_exit": reason == "worker_exited"}
        row, fresh = fs.record_outcome(
            job_id, lane, n, task_id=lane_row["task_id"], graph_rev=cur_graph[0] if cur_graph else None,
            assignment_rev=a["assignment_rev"] if a else None, registry_hash=a["registry_hash"] if a else None,
            task_class=(task_classes or {}).get(lane_row["task_id"]), repository=job.repo,
            candidate_sha=cand["candidate_sha"] if cand else None,
            integration_sha=integrated["output_sha"] if integrated else None,
            duration_s=(round(t1 - t0, 3) if t0 is not None and t1 is not None and t1 >= t0 else None),
            usage_source=usage.get("source") or "unknown", usage_confidence=usage.get("confidence") or "none",
            tokens=usage.get("tokens"), cost_usd=usage.get("cost_usd"), conflicts=conflicts, failures=failures,
            recovery=recovery, rework=n - 1, termination=str(reason) if reason else None,
            evidence_ref=cand["evidence_ref"] if cand else None)
        if fresh:
            out.append(row)
    return out


def _group(rows: list[dict], verdicts: dict) -> dict:
    n = len(rows)
    missing = {m: sum(1 for r in rows if r[m] is None) for m in MEASURES}

    def known(m):
        return [r[m] for r in rows if r[m] is not None]

    human: dict = {}
    for r in rows:
        v = verdicts.get(r["job_id"]) if r["integration_sha"] else None
        human[v or "none"] = human.get(v or "none", 0) + 1
    return {
        "sample_count": n,
        "integrated": sum(1 for r in rows if r["integration_sha"]),
        "with_candidate": sum(1 for r in rows if r["candidate_sha"]),
        "conflicts": sum(len(r["conflicts"]) for r in rows),
        "failures": sum(len(r["failures"]) for r in rows),
        "recovered_replacements": sum(1 for r in rows if r["recovery"].get("replacement_attempt")),
        "stopped_for_reassignment": sum(1 for r in rows if r["recovery"].get("reassigned")),
        "rework_total": sum(r["rework"] for r in rows if r["rework"] is not None),
        "human_outcomes": dict(sorted(human.items())),
        "duration_s": {"known": len(known("duration_s")), "mean": (round(mean(known("duration_s")), 3)
                                                                    if known("duration_s") else None),
                       "median": median(known("duration_s")) if known("duration_s") else None},
        "tokens_total": sum(known("tokens")) if known("tokens") else None,
        "cost_usd_total": sum(known("cost_usd")) if known("cost_usd") else None,
        "missing": missing,
        "missing_data_rate": round(sum(missing.values()) / (n * len(MEASURES)), 4) if n else None,
        "usage_sources": sorted({r["usage_source"] for r in rows}),
    }


def compare_outcomes(outcomes: list[dict], human: list[dict] | None = None, *, repository: str,
                     task_class: str) -> dict:
    """Pure function over already-read rows. Only rows with exactly this repository AND task class are compared;
    rows with an unknown class are counted as excluded, never guessed into a group. Groups are listed in a fixed
    identity order (provider, runtime, model) - an order that carries no merit."""
    if not repository or not task_class:
        raise ValueError("comparison needs an explicit repository and task_class")
    verdicts = {h["job_id"]: h["verdict"] for h in (human or [])}
    scoped = [r for r in outcomes if r["repository"] == repository]
    matching = [r for r in scoped if r["task_class"] == task_class]
    groups: dict = {}
    for r in matching:
        groups.setdefault((r["provider"], r["runtime"], r["model"]), []).append(r)
    return {
        "dimensions": {"repository": repository, "task_class": task_class},
        "notice": NOTICE,
        "considered": len(outcomes), "matching": len(matching),
        "excluded_other_dimensions": len(outcomes) - len(matching),
        "excluded_unclassified": sum(1 for r in scoped if r["task_class"] is None),
        "groups": [{"provider": k[0], "runtime": k[1], "model": k[2], **_group(v, verdicts)}
                   for k, v in sorted(groups.items())],
    }


def outcome_view(store: Store, *, repository: str, task_class: str) -> dict:
    """Read-only projection used by the API: reads rows, writes nothing."""
    fs = FleetStore(store)
    return compare_outcomes(fs.outcomes(), fs.human_outcomes(), repository=repository, task_class=task_class)
