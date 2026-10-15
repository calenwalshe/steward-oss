"""The reference-class prior for the ETA (Phase 18, G4).

`job_kind(goal)` names the class of a job from its goal text (deterministic). The class is the set of finished jobs of that
kind: `job created` -> `worker reported done`, in minutes. `$STEWARD_HOME/eta-priors.json` holds them, seeded from the
research table so the first jobs after the phase have a prior, and rebuilt from the store by `rebuild` (closeout, plan 18-05).
`prior_for` is None for a kind with fewer than two peers (the ETA then says `estimating…`, not a guess) and for `clock`
jobs (recorded, never used as an estimate).
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from steward import eta_structure

PRIORS_FILE = "eta-priors.json"
VERSION = 1
KINDS = ("gsd-quick", "gsd-discuss", "gsd-build", "research", "clock", "other")
MIN_PEERS = 2
PRIOR_LO = 0.6
PRIOR_HI = 1.6
DEFAULT_FIRST_PLAN_MIN = 30.0
DEFAULT_PLANS_PER_PHASE = 3.0
# verify-work + independent verification + closeout after the last unit: Phase 16/17/18 builds ran 10-25 min
DEFAULT_TAIL = {"gsd-build": (15.0, 25.0)}
STAGE_LEFT = {"gsd-quick": {"testing": 5, "committing": 2}}

_DISCUSS_RE = re.compile(r"(?i)discussion|discuss-phase|gsd-discuss")
_QUICK_RE = re.compile(r"(?i)gsd-quick")
_GSD_RE = re.compile(r"(?i)\bgsd\b")
_RESEARCH_RE = re.compile(r"(?i)research|evidence|investigat")


def _row(job, kind, minutes, plans=None, phases=None, per_unit_min=None, first_plan_min=None) -> dict:
    return {"job": job, "kind": kind, "minutes": minutes, "plans": plans, "phases": phases,
            "per_unit_min": per_unit_min, "first_plan_min": first_plan_min}


# Research §1 (minutes from job created to worker reported done). d9e50a45 (140 min over 3 attempts) is left out of the
# per-attempt discuss seed; the clock rows are recorded and never used as an estimate.
SEED = (
    _row("06ea6a04", "gsd-build", 423, 23, 8, 17.7, 18),
    _row("a7d292d8", "gsd-build", 128, 7, 1, 13.2, 30),
    _row("ed11ba3c", "gsd-build", 181, 7, 1, 20.5, 31),
    _row("a79bf074", "gsd-quick", 20.5),
    _row("02b1a95c", "gsd-quick", 14.1),
    _row("e411f337", "gsd-quick", 3.3),
    _row("5adcafc1", "gsd-quick", 3.1),
    _row("724de869", "gsd-discuss", 6.2),
    _row("bfbdf152", "gsd-discuss", 5.1),
    _row("840ca812", "research", 152),
    _row("abc2a7d3", "research", 41),
    _row("a1c37c66", "research", 18),
    _row("f3369d2d", "research", 13),
    _row("aaca35ed", "other", 54),
    _row("cef825c1", "other", 22),
    _row("080502f5", "clock", 32),
    _row("c28fad3f", "clock", 10),
    _row("ec8b5913", "clock", 247),
    _row("d7e8cf20", "clock", 129),
)


@dataclass(frozen=True)
class Prior:
    kind: str
    peers: int
    median_min: float
    lo_min: float
    hi_min: float
    range_min: tuple
    per_unit_min: float | None = None
    first_plan_min: float = DEFAULT_FIRST_PLAN_MIN
    plans_per_phase: float = DEFAULT_PLANS_PER_PHASE
    stage_left: dict = field(default_factory=dict)
    tail_min: float | None = None       # median minutes from the last unit to the worker's done (verify + closeout)
    tail_p90_min: float | None = None


def job_kind(goal: str | None) -> str:
    """clock, gsd-quick, gsd-discuss, gsd-build, research, other, in that order (case-insensitive)."""
    text = goal or ""
    if eta_structure.stated_duration_min(text) is not None:
        return "clock"
    if _QUICK_RE.search(text):
        return "gsd-quick"
    if _DISCUSS_RE.search(text):
        return "gsd-discuss"
    if _GSD_RE.search(text):
        return "gsd-build"
    if _RESEARCH_RE.search(text):
        return "research"
    return "other"


def _seed() -> dict:
    return {"version": VERSION, "built_at": None, "rows": [dict(r) for r in SEED]}


def _valid_row(row) -> bool:
    if not isinstance(row, dict) or row.get("kind") not in KINDS or not isinstance(row.get("job"), str):
        return False
    m = row.get("minutes")
    return isinstance(m, (int, float)) and not isinstance(m, bool) and m >= 0


def load(home) -> dict:
    """The priors file in `home` when it parses, else the seed (a missing or corrupt file never raises)."""
    try:
        data = json.loads((Path(home) / PRIORS_FILE).read_text(encoding="utf-8"))
        rows = data["rows"]
        if not isinstance(rows, list) or not all(_valid_row(r) for r in rows):
            raise ValueError("rows")
        return {"version": int(data.get("version", VERSION)), "built_at": data.get("built_at"), "rows": rows}
    except (OSError, ValueError, KeyError, TypeError):
        return _seed()


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def prior_for(priors: dict | None, kind: str) -> Prior | None:
    """The class for `kind`, or None with fewer than MIN_PEERS finished jobs (and always for clock)."""
    if kind == "clock":
        return None
    rows = [r for r in (priors or {}).get("rows", []) if r.get("kind") == kind and _num(r.get("minutes"))]
    if len(rows) < MIN_PEERS:
        return None
    minutes = [float(r["minutes"]) for r in rows]
    med = statistics.median(minutes)
    rates = [float(r["per_unit_min"]) for r in rows if _num(r.get("per_unit_min"))]
    firsts = [float(r["first_plan_min"]) for r in rows if _num(r.get("first_plan_min"))]
    ppp = [r["plans"] / r["phases"] for r in rows
           if _num(r.get("plans")) and _num(r.get("phases")) and r["phases"] >= 2]
    tails = sorted(float(r["tail_min"]) for r in rows if _num(r.get("tail_min")) and r["tail_min"] >= 0)
    tail_med = statistics.median(tails) if len(tails) >= 2 else DEFAULT_TAIL.get(kind, (None, None))[0]
    tail_hi = (tails[min(len(tails) - 1, math.ceil(0.9 * len(tails)) - 1)] if len(tails) >= 2
               else DEFAULT_TAIL.get(kind, (None, None))[1])
    return Prior(
        kind=kind, peers=len(rows), median_min=med, lo_min=PRIOR_LO * med, hi_min=PRIOR_HI * med,
        range_min=(min(minutes), max(minutes)),
        per_unit_min=statistics.median(rates) if rates else None,
        first_plan_min=statistics.median(firsts) if firsts else DEFAULT_FIRST_PLAN_MIN,
        plans_per_phase=statistics.median(ppp) if ppp else DEFAULT_PLANS_PER_PHASE,
        stage_left=dict(STAGE_LEFT.get(kind, {})), tail_min=tail_med, tail_p90_min=tail_hi)


def _epoch(ts) -> float | None:
    return eta_structure._epoch(ts)


def _store_row(store, job) -> tuple | None:
    """(row, last done epoch) for a finished job (a `worker reported done...` event), else None."""
    created = _epoch(job.created_at)
    last = None
    for ev in store.events(job.job_id):
        if str(ev.what).startswith("worker reported done"):
            t = _epoch(ev.ts)
            if t is not None:
                last = t
    if created is None or last is None or last < created:
        return None
    row = _row(job.job_id, job_kind(job.goal), round((last - created) / 60.0, 2))
    try:
        state = store.kv_get(job.job_id, "efficiency_state", None) or {}
        eta = state.get("eta") if isinstance(state, dict) else None
    except Exception:  # noqa: BLE001
        eta = None
    if isinstance(eta, dict):
        done = eta.get("units_done")
        if _num(done) and done >= 1:
            row["plans"] = int(done)
            phases = eta.get("phases")
            if isinstance(phases, dict) and phases:
                row["phases"] = len(phases)
            lu = eta.get("last_unit_at")
            if _num(lu) and last >= lu:
                row["tail_min"] = round((last - lu) / 60.0, 2)
            fb = eta.get("first_build_at")
            if _num(fb) and last > fb:
                row["per_unit_min"] = round((last - fb) / 60.0 / done, 2)
        if _num(eta.get("first_plan_min")):
            row["first_plan_min"] = float(eta["first_plan_min"])
    return row, last


def _merge(seed_row: dict, store_row: dict) -> dict:
    out = dict(seed_row)
    for k, v in store_row.items():
        if v is not None:
            out[k] = v
    return out


def rebuild(store, *, now: float | None = None) -> dict | None:
    """Write `$STEWARD_HOME/eta-priors.json` atomically: the seed plus finished jobs in the store. None on any error."""
    try:
        rows = [dict(r) for r in SEED]
        latest = None
        for job in store.list_jobs():
            got = _store_row(store, job)
            if got is None:
                continue
            row, last = got
            latest = last if latest is None else max(latest, last)
            for i, seed in enumerate(rows):
                if row["job"].startswith(seed["job"]):
                    rows[i] = _merge(seed, row)
                    break
            else:
                rows.append(row)
        rows.sort(key=lambda r: (r["kind"], r["job"]))
        stamp = now if now is not None else latest  # data-derived when no clock is given: a rebuild is reproducible
        built_at = datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp is not None else None
        data = {"version": VERSION, "built_at": built_at, "rows": rows}
        target = Path(store.home) / PRIORS_FILE
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, target)
        return data
    except Exception:  # noqa: BLE001 - a prior rebuild never breaks closeout
        return None
