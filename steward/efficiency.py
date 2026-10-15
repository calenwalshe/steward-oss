"""Deterministic efficiency metrics over a job's transcripts (phase 16, G2/G3).

Pure code over the transcripts (via `tokens.job_transcripts`: the same parser and cache as
`job_tokens`) plus the store. No model call and no `.planning/` read: a stage is a subagent's
agentType (the worker's own turns are stage "worker") and a unit is a tuple key parsed from the
subagent's description (`NN-MM` plan id first, else a phase number), else the Nth occurrence of that agentType. The metrics hold no thresholds;
detectors (16-03) read this dict.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path

from steward import tokens

# Stage kinds by case-insensitive substring of the agentType (or, if that is unknown, the description).
DEFAULT_STAGE_KINDS = {
    "verify": ("verif", "check", "review", "test", "audit"),
    "plan": ("plan",),
    "build": ("exec", "build", "implement", "code", "develop"),
}
_KIND_ORDER = ("verify", "plan", "build")
_GEN_CLAMP_S = 600.0
_UNIT_RE = re.compile(r"(?i)\b(?:phase|plan|step|unit|task|part|stage|wave|milestone)\s*#?\s*(\d+(?:\.\d+)?)\b")
_PLAN_ID_RE = re.compile(r"(?<!\d-)\b(\d+(?:\.\d+)?)-(\d+)\b(?!-\d)")
_LABEL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)(?:-(\d+))?\s*$")


def stage_kind(agent_type: str, description: str, kinds: dict | None = None) -> str:
    kinds = DEFAULT_STAGE_KINDS if kinds is None else kinds
    order = [k for k in _KIND_ORDER if k in kinds] + sorted(k for k in kinds if k not in _KIND_ORDER)
    at = (agent_type or "").lower()
    texts = [at] if at not in ("", "unknown") else [(description or "").lower()]
    for text in texts:
        for kind in order:
            if any(pat.lower() in text for pat in kinds[kind]):
                return kind
    return "other"


def parse_unit(description: str):
    """A sortable tuple key from a helper's description, else None (G1).

    `NN-MM` (a GSD plan id) is tried first and gives `(phase, plan)`; otherwise the first number after
    phase|plan|step|unit|task|part|stage|wave|milestone gives a one-element tuple."""
    text = description or ""
    m = _PLAN_ID_RE.search(text)
    if m:
        return (_num(m.group(1)), int(m.group(2)))
    m = _UNIT_RE.search(text)
    return (_num(m.group(1)),) if m else None


def _num(text: str):
    f = float(text)
    return int(f) if f == int(f) and "." not in text else f


def _norm(x):
    return int(x) if isinstance(x, float) and x == int(x) else x


def unit_key(u) -> tuple | None:
    """Normalise an int, float, tuple, JSON list or label string to the tuple key (None stays None)."""
    if u is None or isinstance(u, bool):
        return None
    if isinstance(u, (tuple, list)):
        return tuple(_norm(x) for x in u) if u else None
    if isinstance(u, (int, float)):
        return (_norm(u),)
    if isinstance(u, str):
        m = _LABEL_RE.match(u)
        if not m:
            return None
        return (_num(m.group(1)), int(m.group(2))) if m.group(2) is not None else (_num(m.group(1)),)
    return None


def unit_before(earlier, later) -> bool:
    """True when `earlier` is a strictly earlier unit than `later` (a phase-level unit never follows its own plans)."""
    ke, kl = unit_key(earlier), unit_key(later)
    if ke is None or kl is None:
        return False
    if len(kl) == 1 and ke[0] == kl[0]:
        return False
    return ke < kl


def _phase_text(p) -> str:
    return str(_norm(p))


def unit_label(u) -> str:
    """The unit as GSD prints it: (16, 1) -> `16-01`, (2.1, 3) -> `02.1-03`, (16,) -> `16`, None -> ``."""
    k = unit_key(u)
    if not k:
        return ""
    if len(k) == 1:
        return _phase_text(k[0])
    head, _, tail = _phase_text(k[0]).partition(".")
    return f"{head.zfill(2)}{'.' + tail if tail else ''}-{int(k[1]):02d}"


def _common(values) -> str:
    c = Counter(v for v in values if v)
    return max(c.items(), key=lambda kv: (kv[1], kv[0]))[0] if c else ""


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def _agents(listing: list[dict], kinds: dict | None) -> list[dict]:
    """Per-agent metrics. Cost and dedupe follow job_tokens: worker first, each message id once."""
    seen: set[str] = set()
    agents = []
    for tr in listing:
        src = tr["source"]
        if src != "worker" and not src.startswith("sub:"):
            continue  # chats, sol, astra and watchers are supervision, not agents
        ent = tr["entry"]
        det = ent["detail"]
        rows, calls = [], []
        for mid, ts, i, o, cr, cc in ent["msgs"]:
            if mid in seen:
                continue
            seen.add(mid)
            rows.append((mid, i, o, cr, cc))
            calls.append(det["calls"].get(mid) or [ts, ts, 0, "", None, "", False])
        model_of = {r[0]: c[3] for r, c in zip(rows, calls)}
        ctx = [i + cr + cc for _, i, _o, cr, cc in rows]
        out = sum(r[2] for r in rows)
        vis = sum(c[2] for c in calls)
        think = max(0.0, out - vis / 4)
        is_worker = src == "worker"
        atype = "worker" if is_worker else tr["agent_type"]
        last = next(iter(reversed(det["calls"].values())), None)
        agents.append({
            "key": tr["stem"], "stage": atype, "kind": "worker" if is_worker else stage_kind(atype, tr["description"], kinds),
            "agent_type": atype, "description": tr["description"], "session": tr["session"],
            "unit": None, "unit_src": "",
            "calls": len(rows), "ctx_mean": _mean(ctx), "ctx_peak": max(ctx, default=0), "ctx_sum": sum(ctx),
            "ctx_first20_mean": _mean(ctx[:20]),
            "out": out, "vis_chars": vis, "think_est": think, "think_share": think / out if out else 0.0,
            "usd": sum(ent["cost"].get(r[0], 0.0) for r in rows),
            "usd_input_side": sum(tokens.price_usd(model_of[r[0]], [r[1], 0, r[3], r[4]],
                                                   (ent.get("cc1h") or {}).get(r[0], 0)) for r in rows),
            "model": _common(model_of.values()), "effort": _common(c[5] for c in calls),
            "start": det["first_ts"] or 0.0, "end": det["last_ts"] or 0.0,
            "finished": bool(last and last[4] == "end_turn" and not last[6]),
        })
        agents[-1].update(_agent_time(ent, calls))
        crd, ccd = sum(r[3] for r in rows), sum(r[4] for r in rows)
        agents[-1]["cache_miss"] = ccd / (crd + ccd) if crd + ccd else 0.0
        agents[-1]["cache_read"], agents[-1]["cache_create"] = crd, ccd  # 16-03's cache-miss saving needs them
        agents[-1]["_cr_cc"] = (crd, ccd)
    return agents


def _agent_time(ent: dict, calls: list) -> dict:
    """Generation and tool seconds, output tok/s, the largest inter-call gap, busy intervals and open tools."""
    det = ent["detail"]
    gen = sum(min(max(c[1] - c[0], 0.0), _GEN_CLAMP_S) for c in calls)
    res_ts = {}
    for tid, ts, _chars in det["results"]:
        res_ts.setdefault(tid, ts)
    tool, busy, open_tool = 0.0, [], False
    for row in det["tools"]:
        rts = res_ts.get(row[0])
        if rts is None:
            open_tool = True
            continue
        tool += max(rts - row[2], 0.0)
        busy.append((row[2], rts))
    busy += [(c[0], c[1]) for c in calls if c[1] > c[0]]
    gap = max((max(calls[k][0] - calls[k - 1][1], 0.0) for k in range(1, len(calls))), default=0.0)
    return {"gen_s": gen, "tool_s": tool, "max_call_gap_s": gap, "open_tool": open_tool, "_busy": busy}


def _union(intervals) -> list[list[float]]:
    out: list[list[float]] = []
    for a, b in sorted(i for i in intervals if i[1] > i[0]):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _span(intervals) -> float:
    return sum(b - a for a, b in _union(intervals))


def _assign_units(agents: list[dict]) -> None:
    occ: Counter = Counter()
    for a in sorted(agents, key=lambda a: (a["start"], a["key"])):
        if a["kind"] == "worker":
            continue
        occ[a["agent_type"]] += 1
        u = parse_unit(a["description"])
        if u is not None:
            a["unit"], a["unit_src"] = u, "description"
        else:
            a["unit"], a["unit_src"] = (occ[a["agent_type"]],), "occurrence"


def _round(x):
    if isinstance(x, float):
        return round(x, 4)
    if isinstance(x, dict):
        return {k: _round(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_round(v) for v in x]
    return x


def to_json(metrics: dict) -> str:
    """Byte-stable serialization: sorted keys, floats rounded to 4 d.p."""
    return json.dumps(_round(metrics), sort_keys=True)


def compute(store, job_id: str, *, now: float | None = None, projects_dir: Path | None = None,
            config: dict | None = None, transcripts: list | None = None, _cache: dict | None = None) -> dict:
    now = time.time() if now is None else now
    kinds = (config or {}).get("stage_kinds")
    cache = tokens._load_cache(Path(store.home)) if _cache is None else _cache
    dirty = False
    if transcripts is None:
        transcripts, dirty = tokens._transcripts(store, job_id, projects_dir, cache)
    if dirty and _cache is None:
        tokens._save_cache(Path(store.home), cache)
    agents = _agents(transcripts, kinds)
    _assign_units(agents)
    agents.sort(key=lambda a: (a["start"], a["key"]))
    for a in agents:
        gs = a["gen_s"]
        a["tok_s"] = a["out"] / gs if gs else 0.0
    starts = [a["start"] for a in agents if a["start"]]
    window = {"start": min(starts, default=0.0), "end": max((a["end"] for a in agents), default=0.0)}
    window["wall_s"] = max(window["end"] - window["start"], 0.0)
    tokens_now = tokens.job_tokens(store, job_id, now=now, projects_dir=projects_dir, _cache=cache)
    worker_usd = tokens.worker_usd(tokens_now)
    sup = {src: s["usd"] for src, s in tokens_now["sources"].items()
           if src != "worker" and not src.startswith("sub:") and s["usd"] > 0}
    sup_usd = sum(sup.values())
    units = _units(agents)
    usd_worker = sum(a["usd"] for a in agents if a["kind"] == "worker")
    usd_sub = sum(a["usd"] for a in agents if a["kind"] != "worker")
    usd = usd_worker + usd_sub
    out_all, gen_all = sum(a["out"] for a in agents), sum(a["gen_s"] for a in agents)
    time_m = _time_metrics(agents, window)
    result = {
        "job_id": job_id, "now": now, "window": window,
        "agents": [{k: v for k, v in a.items() if not k.startswith("_")} for a in agents],
        "stages": _stages(agents), "units": units,
        "supervision": {"by_source": sup, "usd": sup_usd, "share_of_worker": sup_usd / worker_usd if worker_usd else 0.0},
        "time": time_m,
        "totals": {"usd": usd, "usd_worker": usd_worker, "usd_sub": usd_sub, "sub_share": usd_sub / usd if usd else 0.0,
                   "calls": sum(a["calls"] for a in agents), "peak_ctx": max((a["ctx_peak"] for a in agents), default=0),
                   "think_share": sum(a["think_est"] for a in agents) / out_all if out_all else 0.0,
                   "tok_s": out_all / gen_all if gen_all else 0.0},
        "projection": _projection(store, job_id, units),
        "finished": sorted(a["key"] for a in agents if a["finished"]),
    }
    result["artifacts"], result["big_results"] = _artifacts(transcripts, agents)
    return result


def _stages(agents: list[dict]) -> dict:
    out = {}
    for stage in sorted({a["stage"] for a in agents}):
        g = [a for a in agents if a["stage"] == stage]
        o, gen = sum(a["out"] for a in g), sum(a["gen_s"] for a in g)
        cr, cc = sum(a["_cr_cc"][0] for a in g), sum(a["_cr_cc"][1] for a in g)
        think = sum(a["think_est"] for a in g)
        out[stage] = {
            "kind": g[0]["kind"], "agents": len(g), "calls": sum(a["calls"] for a in g),
            "usd": sum(a["usd"] for a in g), "out": o, "think_est": think, "think_share": think / o if o else 0.0,
            "wall_s": _span([(a["start"], a["end"]) for a in g]), "gen_s": gen,
            "tool_s": sum(a["tool_s"] for a in g), "tok_s": o / gen if gen else 0.0,
            "effort": _common(a["effort"] for a in g), "cache_miss": cc / (cr + cc) if cr + cc else 0.0,
            "ctx_sum": sum(a["ctx_sum"] for a in g), "ctx_peak": max(a["ctx_peak"] for a in g),
        }
    return out


def _units(agents: list[dict]) -> list[dict]:
    subs = [a for a in agents if a["kind"] != "worker"]
    has_build = any(a["kind"] == "build" for a in subs)
    groups: dict = {}
    for a in subs:
        groups.setdefault(unit_key(a["unit"]), []).append(a)
    rows = []
    for key in sorted(groups):
        g = groups[key]
        # G1: a phase-level group with no build of its own (its planner, plan-checker, verifier) rolls up into
        # the phase when plan-level groups exist for it; it is never an open unit of its own.
        if len(key) == 1 and not any(a["kind"] == "build" for a in g) and any(
                len(k) == 2 and k[0] == key[0] for k in groups):
            continue
        done = all(a["finished"] for a in g) and (not has_build or any(a["kind"] == "build" and a["finished"] for a in g))
        rows.append({"unit": key, "unit_label": unit_label(key), "usd": sum(a["usd"] for a in g),
                     "wall_s": _span([(a["start"], a["end"]) for a in g]), "complete": done})
    return rows


def _projection(store, job_id: str, units: list[dict]) -> dict | None:
    from steward.contract import load_contract
    c = load_contract(store, job_id)
    total = c.units_total if c else 0
    if total <= 0:
        return None
    done = [u for u in units if u["complete"]]
    remaining = max(total - len(done), 0)
    usd_pu = _mean(u["usd"] for u in done) if done else None
    min_pu = _mean(u["wall_s"] for u in done) / 60 if done else None
    return {"units_total": total, "units_complete": len(done), "remaining": remaining,
            "usd_per_unit": usd_pu, "min_per_unit": min_pu,
            "projected_usd": usd_pu * remaining if usd_pu is not None else None,
            "projected_min": min_pu * remaining if min_pu is not None else None}


def _time_metrics(agents: list[dict], window: dict) -> dict:
    busy = _union([iv for a in agents for iv in a["_busy"]])
    idle, cur = [], window["start"]
    for a, b in busy:
        if a > cur:
            idle.append([cur, min(a, window["end"])])
        cur = max(cur, b)
    if window["end"] > cur:
        idle.append([cur, window["end"]])
    subs = sorted((a for a in agents if a["kind"] != "worker"), key=lambda a: (a["start"], a["key"]))
    handoffs = []
    for b in subs:
        if any(a is not b and a["start"] <= b["start"] < a["end"] for a in subs):
            continue
        ended = [a["end"] for a in subs if a is not b and a["end"] <= b["start"]]
        if ended:
            handoffs.append({"to": b["key"], "latency_s": b["start"] - max(ended)})
    wall = window["wall_s"]
    cp = _critical_path(agents, subs, window)
    return {
        "idle_s": sum(b - a for a, b in idle), "idle_gaps": idle, "last_busy": busy[-1][1] if busy else 0.0,
        "open_tool": any(a["open_tool"] and (a["kind"] == "worker" or not a["finished"]) for a in agents),
        "handoffs": handoffs, "handoff_s": sum(h["latency_s"] for h in handoffs),
        "concurrency": sum(a["end"] - a["start"] for a in subs) / wall if wall else 0.0,
        "critical_path": cp,
        "crit": min((k for k in cp if k != "idle"), key=lambda k: (-cp[k], k), default=""),
    }


def _critical_path(agents: list[dict], subs: list[dict], window: dict) -> dict:
    wbusy = _union([iv for a in agents if a["kind"] == "worker" for iv in a["_busy"]])
    edges = {window["start"], window["end"]}
    for a in subs:
        edges |= {a["start"], a["end"]}
    for a, b in wbusy:
        edges |= {a, b}
    pts = sorted(e for e in edges if window["start"] <= e <= window["end"])
    secs: dict[str, float] = {}
    for a, b in zip(pts, pts[1:]):
        alive = [s for s in subs if s["start"] <= a and s["end"] >= b]
        if alive:
            label = min(alive, key=lambda s: (-s["end"], -s["start"], s["key"]))["kind"]
        else:
            label = "worker" if any(x <= a and y >= b for x, y in wbusy) else "idle"
        secs[label] = secs.get(label, 0.0) + (b - a)
    return dict(sorted(secs.items()))


def _artifacts(transcripts: list[dict], agents: list[dict]):
    stage_of = {a["key"]: a["stage"] for a in agents}
    tools, results, arts, reads = {}, {}, [], []
    for tr in transcripts:
        key = tr["stem"]
        if key not in stage_of:
            continue
        det = tr["entry"]["detail"]
        for tid, name, ts, cmd, path, wchars, whole in det["tools"]:
            tools[tid] = cmd
            if name == "Write" and path and wchars:
                arts.append({"writer": key, "stage": stage_of[key], "path_key": path, "chars": wchars, "ts": ts})
            elif name == "Read" and whole and path:
                reads.append((key, path, ts))
        for tid, _ts, chars in det["results"]:
            results[tid] = chars
    for art in arts:
        art["whole_readers"] = sorted({k for k, path, ts in reads if path == art["path_key"] and ts > art["ts"]
                                       and stage_of[k] != art["stage"]})
    big: dict[str, list[int]] = {}
    for tid, chars in results.items():
        if tools.get(tid) and chars > 0:
            big.setdefault(tools[tid], []).append(chars)
    arts.sort(key=lambda a: (a["ts"], a["writer"], a["path_key"]))
    return arts, {k: sorted(v) for k, v in sorted(big.items())}
