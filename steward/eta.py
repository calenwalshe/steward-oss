"""The ETA: a pure estimator ladder, its bands, and the display rules (Phase 18, G4-G9, G14, G15).

`estimate(metrics, structure, prior, now, *, kind, ...)` is pure (no I/O, no clock of its own) and returns an `Eta`.
The ladder is exactly as backtested (research §3.1):

  prior        no structure yet: the class median, band 0.6x-1.6x, a range only
  class-rate   units known, none done: start + U*r - run + tail, a range only
  units        one or more units done: cycle mean shrunk toward r with K pseudo-units (a P50 from the first unit)
  units+trend  four or more done: least-squares slope, damped x0.5, clamped to +-mean/(2n)

Off the ladder (plan 18-05): `peers` (a helper-less job: the class range plus a Jev stage, never a point), `clock` (a stated
duration: the exact end) and `n/a` (a codex worker, `na_eta`).

Cycle times exclude on-path planning; the critical-path rule adds it back explicitly when plan-ahead is off. The
stored dict (`Eta.to_dict`, keys `SCHEMA_KEYS`) is the contract later plans read; `render` / `api_view` / `changes`
turn it into what the owner sees: a range before a number, a P50 that moves only on evidence and counts down with the
clock, `overdue by X (was ~Y)` instead of a silent extension.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone

from steward import eta_jev
from steward.efficiency import unit_key, unit_label
from steward.eta_structure import Structure

K = 2.0                   # pseudo-units of the class rate in the shrunk cycle mean
TAIL_P90_FACTOR = 1.7     # tail P90 when the history gives only a median
FIX_P90_MIN = 15.0        # a finish-line send-back is overdue this long after the gate failed
TAIL_SINGLE_MIN = 6.0     # verify + closeout of a single-phase job; 0 inside a multi-phase job
PRIOR_LO = 0.6
PRIOR_HI = 1.6
FLOORS = ((2, 0.40), (4, 0.30), (None, 0.20))   # band floor on remaining work by units done
TOTAL_FLOOR = 0.10        # of (elapsed + remaining) until the final verifier starts (decision I-1)
Z = 1.28
SD_FALLBACK = 0.35
MIN_CYCLE_MIN = 0.5
P90_MIN_PRIOR = 10.0
SCOPE_FLOOR = 0.30
MIN_REMAINING_MIN = 1.0   # a P50 is never less than a minute from now
RUN_FLOOR = 0.2           # the running unit always has at least this share of a cycle left
TOKEN_FRAC_CAP = 0.95
TREND_DAMP = 0.5
CHANGE_FRAC = 0.30        # eta_changed on a shown-P50 move larger than this
HYST_FRAC = 0.10          # the shown P50 re-anchors on a move larger than this share ...
HYST_MIN = 5.0            # ... or than this many minutes
DEFAULT_FIRST_PLAN_MIN = 30.0
DEFAULT_PLANS_PER_PHASE = 3.0
MAX_OPEN_UNITS = 12
EPS = 1e-6
P50_SOURCES = ("units", "units+trend", "clock")
JEV_NOTE_SOURCES = ("prior", "peers", "class-rate", "units", "units+trend")
NA_TEXT = "eta n/a (codex transcript)"

SCHEMA_KEYS = (
    "v", "at", "kind", "source", "mid_min", "p10_min", "p90_min", "p50_min", "elapsed_min", "units_done",
    "units_total", "units_exact", "units_source", "unit_label", "open_units", "phases", "cycle_mean_min",
    "cycle_sd_min", "plan_ahead", "tail_min", "first_build_at", "first_plan_min", "attempt_no", "stage", "jev",
    "jev_state", "finishing", "done_claimed", "scope_grown_at", "grown_units_done", "shown",
    "last_unit_at", "fin_stage", "fin_since", "fin_p90_end",
)
SHOWN_KEYS = ("p10_end", "p50_end", "p90_end", "source", "set_at", "units_done_at_set", "band_units_done",
              "p50_min_at_set", "attempt_no", "overdue_noted")


def _empty_shown() -> dict:
    return {"p10_end": None, "p50_end": None, "p90_end": None, "source": "none", "set_at": None,
            "units_done_at_set": 0, "band_units_done": 0, "p50_min_at_set": None, "attempt_no": 0,
            "overdue_noted": False}


@dataclass
class Eta:
    v: int = 1
    at: float = 0.0
    kind: str = "other"
    source: str = "none"
    mid_min: float | None = None
    p10_min: float | None = None
    p90_min: float | None = None
    p50_min: float | None = None
    elapsed_min: float = 0.0
    units_done: int = 0
    units_total: int | None = None
    units_exact: bool = False
    units_source: str = "none"
    unit_label: str = ""
    open_units: list = field(default_factory=list)
    phases: dict = field(default_factory=dict)
    cycle_mean_min: float | None = None
    cycle_sd_min: float | None = None
    plan_ahead: bool = True
    tail_min: float = 0.0
    first_build_at: float | None = None
    first_plan_min: float | None = None
    attempt_no: int = 0
    stage: str | None = None
    jev: dict | None = None
    jev_state: str = "off"
    finishing: bool = False
    done_claimed: bool = False
    scope_grown_at: float | None = None
    grown_units_done: int | None = None
    shown: dict = field(default_factory=_empty_shown)
    last_unit_at: float | None = None   # epoch the latest unit finished
    fin_stage: str | None = None        # "verifying" | "closing out" | "fixing <check>" once all known units are done
    fin_since: float | None = None      # epoch the finishing stage began (elapsed is counted from it)
    fin_p90_end: float | None = None    # epoch past which the finishing tail is overdue

    def to_dict(self) -> dict:
        out = {}
        for k in SCHEMA_KEYS:
            v = getattr(self, k)
            out[k] = dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "Eta":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


def _d(x) -> dict | None:
    if x is None:
        return None
    if isinstance(x, Eta):
        return x.to_dict()
    return x if isinstance(x, dict) else None


def _half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


# -- intervals ---------------------------------------------------------------------------------


def _merge(iv: list) -> list:
    out: list = []
    for a, b in sorted(i for i in iv if i[1] > i[0]):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _measure(iv: list) -> float:
    return sum(b - a for a, b in iv)


def _overlap(xs: list, ys: list) -> float:
    total = 0.0
    for a, b in xs:
        for c, d in ys:
            total += max(0.0, min(b, d) - max(a, c))
    return total


def _span(a: dict, now: float) -> tuple:
    s = float(a.get("start") or 0.0)
    e = float(a.get("end") or 0.0) if a.get("finished") else now
    return s, max(e if e else now, s)


def _clipped(agents: list, kind: str, lo: float, hi: float, now: float) -> list:
    iv = []
    for a in agents:
        if a.get("kind") != kind or not a.get("start"):
            continue
        s, e = _span(a, now)
        s, e = max(s, lo), min(e, hi)
        if e > s:
            iv.append([s, e])
    return _merge(iv)


def on_path_planning_s(agents: list, lo: float, hi: float, now: float) -> float:
    """Seconds inside [lo, hi] when a planning helper runs and no build helper does."""
    plan = _clipped(agents, "plan", lo, hi, now)
    build = _clipped(agents, "build", lo, hi, now)
    return max(_measure(plan) - _overlap(plan, build), 0.0)


# -- structure facts ---------------------------------------------------------------------------


def _as_structure(s) -> Structure:
    if isinstance(s, Structure):
        return s
    if isinstance(s, dict):
        known = {f.name for f in fields(Structure)}
        return Structure(**{k: v for k, v in s.items() if k in known})
    return Structure()


def _first_build_at(agents: list) -> float | None:
    starts = [float(a["start"]) for a in agents if a.get("kind") == "build" and a.get("start")]
    return min(starts) if starts else None


def cycle_times(structure, agents: list, now: float, first_build_at: float | None = None) -> list:
    """Minutes per finished unit, boundary to boundary, minus on-path planning, each at least MIN_CYCLE_MIN."""
    st = _as_structure(structure)
    bounds = sorted(float(t) for t in st.done.values())
    if not bounds:
        return []
    b0 = None
    for cand in (first_build_at, st.known_at, st.start):
        if cand is not None and cand <= bounds[0]:
            b0 = float(cand)
            break
    if b0 is None:
        b0 = bounds[0]
    out, prev = [], b0
    for b in bounds:
        span = max(b - prev, 0.0)
        planning = on_path_planning_s(agents, prev, b, now)
        out.append(max((span - planning) / 60.0, MIN_CYCLE_MIN))
        prev = b
    return out


def _unit_total(st: Structure, prior) -> tuple:
    """(U as an int or None, exact)."""
    if st.units_total is not None:
        return int(st.units_total), bool(st.units_exact)
    if st.phases:
        ppp = prior.plans_per_phase if prior is not None else DEFAULT_PLANS_PER_PHASE
        total = sum(c if c is not None else ppp for c in st.phases.values())
        return max(_half_up(total), 1), all(c is not None for c in st.phases.values())
    return None, False


def _phase_of(label: str) -> str:
    k = unit_key(label)
    return unit_label((k[0],)) if k else ""


def _running_build(agents: list, done_labels: set):
    best = None
    for a in agents:
        if a.get("kind") != "build" or a.get("finished") or not a.get("start"):
            continue
        key = unit_key(a.get("unit"))
        if key and len(key) == 2 and unit_label(key) in done_labels:
            continue
        if best is None or a["start"] < best["start"]:
            best = a
    return best


def _grown(prev: dict | None, phases: dict) -> bool:
    """G9: a counted phase grew, or a new phase appeared, after units started. TBD -> a number is not growth."""
    if not prev or (prev.get("units_done") or 0) < 1:
        return False
    old = prev.get("phases") or {}
    if not old:
        return False
    for label, cnt in (phases or {}).items():
        if label not in old:
            return True
        was = old[label]
        if isinstance(cnt, int) and isinstance(was, int) and cnt > was:
            return True
    return False


def scope_grew(prev, structure) -> bool:
    """G9's growth test on its own (the watcher hands it to eta_jev.check as the file-growth fact)."""
    return _grown(_d(prev), _as_structure(structure).phases)


def settle_prev(prev, structure):
    """`prev` with phases that were merely DISCOVERED folded in, so they are not scope growth (plan 18-05).

    Phase labels come from the ROADMAP (TBD counts mean one is listed) or from PLAN files as a phase gets planned. With no
    TBD phase anywhere there is no roadmap announcing the scope, so a new counted phase is the job planning its next
    phase, not scope growing. A counted phase that gains plans is still growth."""
    p = _d(prev)
    st = _as_structure(structure)
    if not p or not p.get("phases"):
        return p
    if any(c is None for c in list(p["phases"].values()) + list(st.phases.values())):
        return p
    new = {label: c for label, c in st.phases.items() if label not in p["phases"]}
    return {**p, "phases": {**p["phases"], **new}} if new else p


def running_unit(metrics, structure):
    """The label (`16-04`) of the plan-level build helper that is running and not done, else None."""
    agents = [a for a in ((metrics or {}).get("agents") or []) if a.get("kind") != "worker"]
    build = _running_build(agents, set(_as_structure(structure).done))
    key = unit_key(build.get("unit")) if build else None
    return unit_label(key) if key and len(key) == 2 else None


def na_eta(kind: str = "other") -> dict:
    """The static ETA of a codex worker (G20): no helper transcripts to read. `at` stays 0, so storing it twice is a no-op."""
    return Eta(kind=kind, source="n/a").to_dict()


def no_jev_note(eta) -> str:
    """` (no Jev)` when an ETA that Jev would label was computed without it (G11), else an empty string."""
    e = _d(eta) or {}
    return " (no Jev)" if e.get("source") in JEV_NOTE_SOURCES and e.get("jev_state") != "ok" else ""


def _labels(st: Structure, agents: list, done_labels: set) -> tuple:
    """(unit_label text, open units): G6 forms (`phase 5 · plan 05-03`, `plan 16-04`, `phase 2 · planning`)."""
    open_units = sorted((u for u in st.planned if u not in done_labels), key=lambda u: unit_key(u) or ())
    multi = len(st.phases) > 1

    def first_open(phase: str | None):
        for u in open_units:
            if phase is None or _phase_of(u) == phase:
                return u
        return None

    def compose(plan, phase):
        if plan:
            return f"phase {_phase_of(plan)} · plan {plan}" if multi else f"plan {plan}"
        if phase:
            return f"phase {phase}"
        return ""

    build = _running_build(agents, done_labels)
    if build is not None:
        key = unit_key(build.get("unit"))
        if key and len(key) == 2:
            return compose(unit_label(key), None), open_units[:MAX_OPEN_UNITS]
        if key:
            ph = unit_label(key)
            return compose(first_open(ph), ph), open_units[:MAX_OPEN_UNITS]
        return compose(first_open(None), None), open_units[:MAX_OPEN_UNITS]
    planners = sorted((a for a in agents if a.get("kind") == "plan" and not a.get("finished") and a.get("start")),
                      key=lambda a: a["start"])
    if planners:
        key = unit_key(planners[0].get("unit"))
        if multi and key:
            return f"phase {unit_label((key[0],))} · planning", open_units[:MAX_OPEN_UNITS]
        return "planning", open_units[:MAX_OPEN_UNITS]
    return compose(first_open(None), None), open_units[:MAX_OPEN_UNITS]


def _verifier(agents: list, last_boundary: float) -> tuple:
    """(a verify helper started after the last boundary, one of them is still running)."""
    started = running = False
    for a in agents:
        if a.get("kind") == "verify" and a.get("start") and a["start"] >= last_boundary - 1.0:
            started = True
            if not a.get("finished"):
                running = True
    return started, running


def _planning_add(st: Structure, agents: list, plan_len: float, now: float) -> float:
    """Critical path (I-2): one planning length per phase still to be planned; the running planner's remainder for its own."""
    unplanned = [p for p, c in st.phases.items() if c is None]
    if not unplanned:
        return 0.0
    planners = [a for a in agents if a.get("kind") == "plan" and not a.get("finished") and a.get("start")]
    running_for: dict = {}
    for a in sorted(planners, key=lambda a: a["start"]):
        key = unit_key(a.get("unit"))
        label = unit_label((key[0],)) if key else None
        if label not in unplanned:
            label = next((p for p in unplanned if p not in running_for), None) if key is None else None
        if label is not None and label not in running_for:
            running_for[label] = a
    add = 0.0
    for p in unplanned:
        a = running_for.get(p)
        if a is None:
            add += plan_len
        else:
            add += max(plan_len - (now - a["start"]) / 60.0, RUN_FLOOR * plan_len)
    return add


def _slope(cycles: list) -> float:
    n = len(cycles)
    xm = (n + 1) / 2.0
    sxx = sum((i + 1 - xm) ** 2 for i in range(n))
    sxy = sum((i + 1 - xm) * c for i, c in enumerate(cycles))
    return sxy / sxx if sxx else 0.0


def _floor(n: int) -> float:
    for bound, frac in FLOORS:
        if bound is None or n < bound:
            return frac
    return FLOORS[-1][1]


def _run_minutes(helper: dict, st_done: dict, now: float, first_pred: float, agents: list) -> tuple:
    """(wall minutes the running unit has run, effective minutes run) - tokens refine it inside plan-level units only."""
    bounds = [float(t) for t in st_done.values()]
    ref = max([float(helper["start"])] + ([max(bounds)] if bounds else []))
    run = max((now - ref) / 60.0, 0.0)
    key = unit_key(helper.get("unit"))
    if key and len(key) == 2 and helper.get("out"):
        peers = [a["out"] for a in agents
                 if a.get("kind") == "build" and a.get("finished") and a.get("agent_type") == helper.get("agent_type")
                 and a.get("out")]
        if len(peers) >= 2:
            frac = min(float(helper["out"]) / statistics.median(peers), TOKEN_FRAC_CAP)
            return run, frac * first_pred
    return run, run


# -- estimate ----------------------------------------------------------------------------------


def estimate(metrics, structure, prior, now: float, *, kind: str, plan_ahead: bool = True, jev=None, prev=None,
             jev_state: str | None = None) -> Eta:
    st = _as_structure(structure)
    prev = _d(prev)
    agents = [a for a in ((metrics or {}).get("agents") or []) if a.get("kind") != "worker"]
    now = float(now)
    start = st.start
    if start is None:
        starts = [a["start"] for a in agents if a.get("start")]
        start = min(starts) if starts else now
    elapsed = max((now - start) / 60.0, 0.0)
    first_build = _first_build_at(agents)
    done_labels = set(st.done)
    n = len(st.done)
    cycles = cycle_times(st, agents, now, first_build)
    total, exact = _unit_total(st, prior)
    if total is not None:
        total = max(total, n)
    r = prior.per_unit_min if prior is not None else None
    plan_len = prior.first_plan_min if prior is not None else DEFAULT_FIRST_PLAN_MIN
    if len(st.phases) <= 1:
        tail = TAIL_SINGLE_MIN
    else:   # a multi-phase job still verifies and closes out after its last plan: the kind's history says how long
        tail = float(prior.tail_min) if prior is not None and prior.tail_min is not None else 0.0
    tail_p90 = (float(prior.tail_p90_min) if prior is not None and prior.tail_p90_min is not None
                and len(st.phases) > 1 else tail * TAIL_P90_FACTOR)

    # G9: scope growth (files only), held until the next unit boundary
    grown_at = grown_units = None
    if prev is not None:
        if _grown(prev, st.phases):
            grown_at, grown_units = now, n
        elif prev.get("scope_grown_at") is not None and n <= (prev.get("grown_units_done")
                                                            if prev.get("grown_units_done") is not None else n):
            grown_at, grown_units = prev["scope_grown_at"], prev.get("grown_units_done")
    grown = grown_at is not None and total is not None and r is not None

    if st.units_source == "goal" and st.clock_min:
        source = "clock"   # G10: a stated duration is an exact end, not an estimate
    elif total is not None and n == 0 and r is not None or grown:
        source = "class-rate"
    elif total is not None and n >= 1:
        source = "units+trend" if n >= 4 else "units"
    elif prior is not None:
        source = "peers" if kind in eta_jev.HELPERLESS_KINDS else "prior"   # G10: helper-less jobs never get a point
    else:
        source = "none"

    mid = p10 = p90 = None
    mean = sd = None
    last_boundary = max(st.done.values()) if st.done else (first_build or st.known_at or start)
    ver_started, ver_running = _verifier(agents, last_boundary)
    final_verifier = total is not None and n >= total and ver_started

    stage, tail_out = None, tail
    if source == "clock":
        mid = p10 = p90 = max(float(st.clock_min) - elapsed, 0.0)
    elif source == "peers":
        lo, hi = prior.range_min
        mid = max(prior.median_min - elapsed, 0.0)   # internal: never shown as a point
        # Unclamped on purpose: the shown band stays start + range however long the job runs (`usually 3-20 min`).
        p10, p90 = lo - elapsed, hi - elapsed
        voted = (jev or {}).get("stage") if isinstance(jev, dict) else None
        if voted in eta_jev.STAGES and voted != "unclear":
            stage = voted
            tail_out = float(prior.stage_left.get(stage, 0.0))   # the stage's usual remainder rides in tail_min
        else:
            tail_out = 0.0
    elif source == "prior":
        mid = max(prior.median_min - elapsed, 0.0)
        p10 = min(max(prior.lo_min - elapsed, 0.0), mid)
        p90 = max(prior.hi_min - elapsed, P90_MIN_PRIOR, mid)
    elif source in ("class-rate", "units", "units+trend"):
        todo = max(total - n, 0)
        if n >= 1:
            mean_c = sum(cycles) / len(cycles)
            mean = (n * mean_c + K * r) / (n + K) if r is not None else mean_c
            sd = statistics.pstdev(cycles) if n >= 3 else SD_FALLBACK * mean
        unit_rate = r if source == "class-rate" else mean
        preds = [unit_rate] * todo
        if source == "units+trend":
            slope = _slope(cycles) * TREND_DAMP
            lim = mean / (2.0 * n)
            slope = max(-lim, min(lim, slope))
            preds = [max(mean + slope * (j - (n + 1) / 2.0), RUN_FLOOR * mean, MIN_CYCLE_MIN)
                     for j in range(n + 1, n + todo + 1)]
        helper = _running_build(agents, done_labels)
        if helper is not None:
            first = preds[0] if preds else unit_rate
            _run, run_eff = _run_minutes(helper, st.done, now, first, agents)
            rest = max(first - run_eff, RUN_FLOOR * first)
            work = rest + sum(preds[1:])
        else:
            work = sum(preds)
        offset = 0.0
        if source == "class-rate" and n == 0 and first_build is None:
            offset = 0.5 * plan_len
        planning = 0.0 if plan_ahead else _planning_add(st, agents, plan_len, now)
        remaining = work + tail + planning
        mid = max(remaining + offset, MIN_REMAINING_MIN)
        if source == "class-rate":
            floor = SCOPE_FLOOR if grown else _floor(n)
            disp = 0.0
            base = remaining
        else:
            floor = _floor(n)
            disp = Z * sd * math.sqrt(max(total - n, 1))
            base = remaining
        half = max(disp, floor * base)
        if not final_verifier:
            half = max(half, TOTAL_FLOOR * (elapsed + mid))
        p10 = max(mid - half, 0.0)
        p90 = mid + half

    done_total_known = total is not None
    unplanned = any(c is None for c in st.phases.values())
    jev_d = dict(jev) if isinstance(jev, dict) and jev else None
    jev_agrees = bool(jev_d) and (jev_d.get("unit") == "finished" or jev_d.get("near_done") == "yes")
    finishing = (done_total_known and n >= total - 1 and not unplanned and (ver_running or jev_agrees))
    done_claimed = (st.done_claimed_at is not None and not finishing
                    and (total is None or n < total - 1))
    label, open_units = _labels(st, agents, done_labels)

    fin_stage = fin_since = fin_p90_end = None
    gate = st.gate if isinstance(st.gate, dict) else None
    fixing = (gate is not None and not gate.get("ok") and gate["at"] >= (st.attempt_start or 0.0)
              and gate["at"] > (st.done_claimed_at or 0.0))
    all_done = total is not None and n >= total and source in ("units", "units+trend", "class-rate")
    if fixing or all_done:
        # No stale countdown once the known units are done: the stage, time in it, and a P90 past which it is overdue.
        boundary = max(st.done.values()) if st.done else now
        if fixing:
            fin_stage = f"fixing {gate.get('check') or 'check'}"
            fin_since = gate["at"]
            fin_p90_end = max(boundary + tail_p90 * 60.0, gate["at"] + FIX_P90_MIN * 60.0)
        else:
            fin_stage = "closing out" if st.done_claimed_at is not None else "verifying"
            fin_since = boundary
            fin_p90_end = boundary + tail_p90 * 60.0

    eta = Eta(
        at=now, kind=kind, source=source, mid_min=mid, p10_min=p10, p90_min=p90,
        p50_min=mid if source in P50_SOURCES else None, elapsed_min=elapsed, units_done=n, units_total=total,
        units_exact=exact, units_source=st.units_source, unit_label=label, open_units=open_units,
        phases=dict(st.phases), cycle_mean_min=mean, cycle_sd_min=sd, plan_ahead=bool(plan_ahead), tail_min=tail_out,
        first_build_at=first_build, first_plan_min=prior.first_plan_min if prior is not None else None,
        attempt_no=int(st.attempt_no or 0), stage=stage, jev=jev_d,
        jev_state=jev_state if jev_state is not None else ("ok" if jev_d else "off"),
        finishing=bool(finishing), done_claimed=bool(done_claimed), scope_grown_at=grown_at,
        grown_units_done=grown_units, last_unit_at=max(st.done.values()) if st.done else None,
        fin_stage=fin_stage, fin_since=fin_since, fin_p90_end=fin_p90_end)
    eta.shown = _shown(prev, eta)
    return eta


# -- the shown numbers (G14, G15) ----------------------------------------------------------------


def _shown(prev: dict | None, eta: Eta) -> dict:
    now = eta.at
    if eta.source == "none":
        s = _empty_shown()
        s.update(set_at=now, units_done_at_set=eta.units_done, band_units_done=eta.units_done,
                 attempt_no=eta.attempt_no)
        return s
    p50 = eta.p50_min is not None
    new = {"p10_end": now + eta.p10_min * 60.0, "p50_end": now + eta.p50_min * 60.0 if p50 else None,
           "p90_end": now + eta.p90_min * 60.0}
    ps = (prev or {}).get("shown") if prev else None

    def anchor() -> dict:
        return {**new, "source": eta.source, "set_at": now, "units_done_at_set": eta.units_done,
                "band_units_done": eta.units_done, "p50_min_at_set": eta.p50_min if p50 else None,
                "attempt_no": eta.attempt_no, "overdue_noted": False}

    if (not isinstance(ps, dict) or ps.get("p10_end") is None or ps.get("source") != eta.source
            or ps.get("attempt_no") != eta.attempt_no):
        return anchor()
    shown = {k: ps.get(k) for k in SHOWN_KEYS}
    if p50 and shown["p50_end"] is not None:
        if now > shown["p50_end"]:
            if eta.units_done > (shown["band_units_done"] or 0):
                return anchor()  # the next unit boundary re-anchors an overdue estimate
            shown["overdue_noted"] = True
            return shown         # frozen: an overrun is information, never a silent extension
        rem = (shown["p50_end"] - now) / 60.0
        diff = abs(eta.p50_min - rem)
        if diff > HYST_MIN + EPS or diff > HYST_FRAC * rem + EPS:
            return anchor()
    elif p50:
        return anchor()
    boundary = eta.units_done > (shown["band_units_done"] or 0)
    for edge, widen in (("p10_end", min), ("p90_end", max)):
        if boundary:
            shown[edge] = new[edge]
        else:
            shown[edge] = widen(shown[edge], new[edge])
    if boundary:
        shown["band_units_done"] = eta.units_done
    if shown["p50_end"] is not None:
        shown["p10_end"] = min(shown["p10_end"], shown["p50_end"])
        shown["p90_end"] = max(shown["p90_end"], shown["p50_end"])
    return shown


# -- formatting ----------------------------------------------------------------------------------


def fmt_minutes(x) -> str:
    """5 min under an hour, 10 min above, halves round up; under 5 min is `<5m`; never seconds."""
    x = max(float(x), 0.0)
    if x < 5:
        return "<5m"
    step = 5 if x < 60 else 10
    m = _half_up(x / step) * step
    if m < 60:
        return f"{m}m"
    h, mm = divmod(m, 60)
    return f"{h}h" if mm == 0 else f"{h}h{mm:02d}"


def _fmt_whole(m: int) -> str:
    if m < 60:
        return f"{m}m"
    h, mm = divmod(m, 60)
    return f"{h}h" if mm == 0 else f"{h}h{mm:02d}"


def _round_small(x: float) -> int:
    return _half_up(x) if x < 10 else _half_up(x / 5.0) * 5


def fmt_range(lo, hi) -> str:
    lo = max(float(lo), 0.0)
    hi = max(float(hi), lo)
    if lo >= 60 and hi >= 60 and hi - lo >= 60:
        a, b = _half_up(lo / 60.0), _half_up(hi / 60.0)
        if b <= a:
            b = a + 1
        return f"{a}–{b} h"
    if hi < 60 and lo < 60:
        a, b = _round_small(lo), _round_small(hi)
        if b <= a:
            b = a + (1 if a < 10 else 5)
        return f"{a}–{b} min"
    return f"{fmt_minutes(lo)}–{fmt_minutes(hi)}"


_FROM = {"prior": "from job kind", "class-rate": "from plan count", "units": "from cycle times",
         "units+trend": "from cycle trend", "clock": "from stated duration", "peers": "from peers"}


def _fmt_stated(minutes: float) -> str:
    m = max(_half_up(minutes), 0)
    if m % 60 == 0 and m >= 60:
        return f"{m // 60} h"
    return f"{m} min" if m < 60 else _fmt_whole(m)


def _left(end, now: float):
    return None if end is None else max((end - now) / 60.0, 0.0)


def _render_fin(e: dict, out: dict, now: float) -> dict:
    """All known units are done: `finishing · <stage> · <elapsed>`, or `overdue N` past the tail's P90. Always counted
    from the current clock, so a send-back or a late done claim can never leave a frozen countdown."""
    stage = e["fin_stage"]
    elapsed = max((now - e["fin_since"]) / 60.0, 0.0)
    over = e.get("fin_p90_end")
    out["fin"] = True
    if over is not None and now > over:
        n = max(_half_up((now - over) / 60.0), 1)
        text = f"overdue by {_fmt_whole(n)} (finishing · {stage})"
        out.update(header=f"overdue {_fmt_whole(n)} · {stage}", text=text, detail=f"eta {text}", overdue_min=n,
                   p50_min=0.0, tab=f"+{n}m")
        return out
    el = _fmt_whole(_half_up(elapsed)) if elapsed >= 0.5 else "<1m"
    text = f"finishing · {stage} · {el}"
    out.update(header=text, text=text, detail=f"eta {text}", tab="fin")
    return out


def render(eta, now: float) -> dict:
    """{header, text, detail, p10_min, p50_min, p90_min, overdue_min}: the shown numbers counted down to `now`."""
    e = _d(eta) or Eta().to_dict()
    sh = e.get("shown") or _empty_shown()
    src = e.get("source", "none")
    p10, p50, p90 = _left(sh.get("p10_end"), now), _left(sh.get("p50_end"), now), _left(sh.get("p90_end"), now)
    overdue = None
    if sh.get("p50_end") is not None and now > sh["p50_end"]:
        overdue = max(_half_up((now - sh["p50_end"]) / 60.0), 1)
    out = {"header": "estimating…", "text": "estimating…", "detail": "eta estimating…",
           "p10_min": None, "p50_min": None, "p90_min": None, "overdue_min": None}
    if src == "n/a":  # G20: a codex worker has no helper transcripts to read
        return {**out, "header": NA_TEXT, "text": NA_TEXT, "detail": NA_TEXT}
    if e.get("fin_stage") and e.get("fin_since") is not None and src not in ("none", "n/a"):
        return _render_fin(e, out, now)
    if e.get("done_claimed") or src == "none" or sh.get("p10_end") is None:
        return out
    out.update(p10_min=p10, p50_min=p50, p90_min=p90)
    units = ""
    if e.get("units_total") is not None:
        units = f" · units {e.get('units_done', 0)}/{'' if e.get('units_exact') else '~'}{e['units_total']}"
    frm = f" · {_FROM[src]}" if src in _FROM else ""
    if e.get("finishing"):
        text = "finishing"
        if p50 is not None and overdue is None:
            s = fmt_minutes(p50)
            text += f" · {'' if s.startswith('<') else '~'}{s} left"
        out.update(header=text, text=text, detail=f"eta {text}{units}{frm}", overdue_min=overdue)
        return out
    if overdue is not None:
        was = fmt_minutes(sh.get("p50_min_at_set") or 0.0)
        text = f"overdue by {_fmt_whole(overdue)} (was ~{was})"
        out.update(header=f"overdue {_fmt_whole(overdue)}", text=text, detail=f"eta {text}{units}{frm}",
                   overdue_min=overdue, p50_min=0.0)
        return out
    if src == "clock":  # the stated end, exact: it counts down and is never a band
        start = e.get("at", now) - e.get("elapsed_min", 0.0) * 60.0
        mid = fmt_minutes(p50)
        text = f"{mid if mid.startswith('<') else '~' + mid} left (stated {_fmt_stated((sh['p50_end'] - start) / 60.0)})"
        out.update(header=text, text=text, detail=f"eta {text} · {_FROM['clock']}")
        return out
    if src == "peers":  # G10: the class range (constant) plus the voted stage; never a point
        start = e.get("at", now) - e.get("elapsed_min", 0.0) * 60.0
        core = f"usually {fmt_range((sh['p10_end'] - start) / 60.0, (sh['p90_end'] - start) / 60.0)}"
        if e.get("kind") == "gsd-discuss":
            core += " per attempt"
        stage, left = e.get("stage"), e.get("tail_min") or 0.0
        header = f"{core} · {stage}" if stage else core
        detail = f"eta {core}"
        if stage:
            detail += f" · stage {stage}" + (f" (usually {fmt_minutes(left)} left at this stage)" if left > 0 else "")
        detail += " · from peers + stage" if stage else " · from peers"
        out.update(header=header, text=header, detail=detail + no_jev_note(e))
        return out
    if p50 is None:  # a range is the honest answer
        if src == "prior":
            start = e.get("at", now) - e.get("elapsed_min", 0.0) * 60.0
            core = f"usually {fmt_range((sh['p10_end'] - start) / 60.0, (sh['p90_end'] - start) / 60.0)}"
            out.update(header=core, text=core, detail=f"eta {core}{units}{frm}")
        else:
            core = f"≈{fmt_range(p10, p90)} left"
            out.update(header=core, text=core, detail=f"eta {core}{units}{frm}")
        return out
    lo, mid, hi = fmt_minutes(p10), fmt_minutes(p50), fmt_minutes(p90)
    band = f" ({lo}–{hi})" if lo != hi else ""
    out.update(header=f"~{mid} left{band}", text=f"~{mid} left{band}",
               detail=f"eta ~{mid}{band}{units}{frm}")
    return out


def _iso(ts) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat()


def api_view(eta, now: float) -> dict:
    """What the API and MCP expose: the shown band counted down to `now`, whole minutes, p50 only when honest."""
    e = _d(eta) or Eta().to_dict()
    r = render(e, now)

    def whole(x):
        return None if x is None else _half_up(x)

    return {"p10_min": whole(r["p10_min"]), "p50_min": whole(r["p50_min"]), "p90_min": whole(r["p90_min"]),
            "shown": r["header"], "units_done": e.get("units_done", 0), "units_total": e.get("units_total"),
            "unit_label": e.get("unit_label", ""), "source": e.get("source", "none"), "stage": e.get("stage"),
            "overdue_min": r["overdue_min"], "updated_at": _iso(e.get("at"))}


# -- change events ---------------------------------------------------------------------------------


def changes(prev, new) -> list:
    """[(what, evidence)]: eta_scope_grown once per growth, eta_changed on a source change or a > 30 % move of the
    shown P50, eta_overdue once per shown estimate. Pure: it reads only the two dicts."""
    p, n = _d(prev), _d(new)
    if not p or not n:
        return []
    out = []
    if n.get("scope_grown_at") is not None and n.get("scope_grown_at") != p.get("scope_grown_at"):
        out.append(("eta_scope_grown", {"from_units": p.get("units_total"), "to_units": n.get("units_total")}))
    ps, ns = p.get("shown") or {}, n.get("shown") or {}
    at = n.get("at") or 0.0
    changed = p.get("source") != n.get("source")
    if (not changed and ps.get("p50_end") is not None and ns.get("p50_end") is not None
            and ns.get("set_at") != ps.get("set_at")):
        old_rem = (ps["p50_end"] - at) / 60.0
        new_rem = (ns["p50_end"] - at) / 60.0
        changed = abs(new_rem - old_rem) / max(old_rem, 1.0) > CHANGE_FRAC + EPS
    if changed:
        def mins(end):
            return None if end is None else round(max((end - at) / 60.0, 0.0), 1)
        out.append(("eta_changed", {
            "from": p.get("source"), "to": n.get("source"), "p50_min": mins(ns.get("p50_end")),
            "p10_min": mins(ns.get("p10_end")), "p90_min": mins(ns.get("p90_end")),
            "units_done": n.get("units_done"), "units_total": n.get("units_total")}))
    if ns.get("p50_end") is not None and at > ns["p50_end"]:
        already = bool(ps.get("overdue_noted")) and ps.get("set_at") == ns.get("set_at")
        if not already:
            out.append(("eta_overdue", {"overdue_min": round((at - ns["p50_end"]) / 60.0, 1),
                                        "was_min": ns.get("p50_min_at_set")}))
    return out
