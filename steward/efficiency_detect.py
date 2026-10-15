"""The G7 detector table and the fixed remedy catalog (phase 16-03).

Pure code: `detect(metrics, cfg, ...)` turns the dict from `efficiency.compute` into findings, each with one catalog
remedy and an estimated saving. No model, no wall clock (only `now` and the metrics), no transcript text in any
remedy string (T-16-07). Thresholds are DEFAULTS < `[efficiency]` in steward.toml < the per-job kv
`efficiency_overrides` (dotted keys reach the nested tables). Nudges and policy changes are 16-05's; this only reports.
"""
from __future__ import annotations

import hashlib
import json
import tomllib
from datetime import datetime
from pathlib import Path

from steward import efficiency, owner, tokens
from steward.store import resolve_config_dir

DEFAULTS: dict = {
    "long_lived_calls": 80, "long_lived_mean_ctx": 250_000, "long_lived_ctx_sum": 20_000_000,
    "ctx_growth_ratio": 1.5, "ctx_growth_min_calls": 5,
    "thinking_share": 0.60, "thinking_stage_output": 150_000, "think_cut": 0.5,
    "plan_serial_units": 2, "plan_serial_tolerance_s": 60,
    "artifact_chars": 40_000,
    "build_output_chars": 20_000, "build_output_repeats": 3,
    "cache_miss_share": 0.30, "cache_miss_min_calls": 20, "cache_ttl_s": 300,
    "idle_wait_s": 600,
    "unit_cost_ratio": 1.5, "unit_cost_min_prior": 2,
    "supervision_share": 0.25, "redundant_runs": 3, "redundant_window_s": 600,
    "min_saving_usd": 1.0, "min_saving_min": 2.0,
    "recompute_every_s": 60,
    "mode": "shadow", "model_review": False, "job_budget_usd": None,
    "stage_kinds": efficiency.DEFAULT_STAGE_KINDS,
    "envelope": {"effort_floor": "medium", "min_max_calls": 40, "plan_ahead": True},
}
# Keys that are detector thresholds: only these move thresholds_hash (mode, model_review, envelope and the
# tick cadence do not, so promoting or re-tuning them never demotes the watcher).
_THRESHOLD_KEYS = tuple(sorted(
    k for k in DEFAULTS
    if k not in ("mode", "model_review", "job_budget_usd", "stage_kinds", "envelope", "recompute_every_s")))

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_SETTABLE_EFFORT = ("high", "medium")  # G9: the watcher may only set high|medium

# Texts may carry {owner}/{owner_poss} placeholders; `_finding` fills them at use time, never at import.
REMEDIES: dict[str, dict] = {
    "respawn_per_unit": {
        "text": "end each helper agent when its unit is done and spawn a fresh one for the next unit; "
                "hand over through a short file, not the conversation history",
        "short": "respawn a fresh helper per unit", "actions": ("nudge", "reconfigure"),
        "policy_key": "respawn", "policy_value": "per_unit"},
    "trim_spawn_reads": {
        "text": "end each helper agent when its unit is done and spawn a fresh one for the next unit, "
                "hand over through a short file, not the conversation history, and have each helper read "
                "only what its unit needs",
        "short": "respawn per unit and trim what each helper reads", "actions": ("nudge", "reconfigure"),
        "policy_key": "respawn", "policy_value": "per_unit"},
    "effort_step_down": {
        "text": "run this kind of helper at one effort level lower",
        "short": "step this helper's effort down one level", "actions": ("nudge", "reconfigure"),
        "policy_key": "effort", "policy_value": None},
    "plan_ahead": {
        "text": "start planning unit N+1 while unit N builds",
        "short": "plan the next unit while this one builds", "actions": ("nudge", "reconfigure"),
        "policy_key": "plan_ahead", "policy_value": "on"},
    "smaller_artifact": {
        "text": "ask for a smaller artifact (coarser granularity or a shorter template) and keep every quality section",
        "short": "ask for a smaller artifact", "actions": ("nudge",), "policy_key": None, "policy_value": None},
    "quiet_output": {
        "text": "run noisy commands with quiet flags or `| tail -n 80`, or write the output to a file and grep it",
        "short": "quiet noisy command output", "actions": ("nudge", "reconfigure"),
        "policy_key": "output_hygiene", "policy_value": "on"},
    "warm_cache": {
        "text": "keep calls close together, avoid gaps longer than the cache TTL, keep the prompt prefix stable",
        "short": "keep the prompt cache warm", "actions": ("nudge",), "policy_key": None, "policy_value": None},
    "escalate_idle": {
        "text": "nothing is generating or running; liveness belongs to the doctor and Jev, so this is for {owner} to see",
        "short": "idle wait, for {owner} to see", "actions": ("escalate",), "policy_key": None, "policy_value": None},
    "escalate_projection": {
        "text": "the cost per unit is rising or the projection passes the budget; {owner} decides what to change",
        "short": "cost per unit rising, for {owner} to see", "actions": ("escalate",),
        "policy_key": None, "policy_value": None},
    "escalate_remove_watcher": {
        "text": "this supervision duplicates other watchers or costs a large share of the worker's spend; "
                "removing a watcher is {owner_poss} decision",
        "short": "redundant supervision, removal is {owner_poss}", "actions": ("escalate",),
        "policy_key": None, "policy_value": None},
}


# -- config -------------------------------------------------------------------

def _type_ok(default, value) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if default is None:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, type(default))


def _kinds_ok(value) -> bool:
    return isinstance(value, (list, tuple)) and all(isinstance(x, str) for x in value)


def _merge(cfg: dict, layer) -> None:
    """Apply one config layer onto cfg in place; a wrongly typed value is ignored (the lower layer stays)."""
    if not isinstance(layer, dict):
        return
    for key, value in layer.items():
        if not isinstance(key, str):
            continue
        head, _, tail = key.partition(".")
        if head == "stage_kinds":
            kinds = value if not tail else {tail: value}
            if isinstance(kinds, dict):
                cfg["stage_kinds"] = {**cfg["stage_kinds"],
                                      **{str(k): tuple(v) for k, v in kinds.items() if _kinds_ok(v)}}
        elif head == "envelope":
            sub = value if not tail else {tail: value}
            if isinstance(sub, dict):
                for k, v in sub.items():
                    if k in DEFAULTS["envelope"] and _type_ok(DEFAULTS["envelope"][k], v):
                        cfg["envelope"][k] = v
        elif not tail and head in DEFAULTS and head not in ("stage_kinds", "envelope"):
            if _type_ok(DEFAULTS[head], value):
                cfg[head] = value


def load_config(store=None, job_id: str | None = None, config_dir: Path | str | None = None) -> dict:
    cfg = {**DEFAULTS, "stage_kinds": dict(DEFAULTS["stage_kinds"]), "envelope": dict(DEFAULTS["envelope"])}
    try:
        data = tomllib.loads((resolve_config_dir(config_dir) / "steward.toml").read_text()).get("efficiency") or {}
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        flat = {k: v for k, v in data.items() if k not in ("stage_kinds", "envelope")}
        for nested in ("stage_kinds", "envelope"):
            if isinstance(data.get(nested), dict):
                flat.update({f"{nested}.{k}": v for k, v in data[nested].items()})
        _merge(cfg, flat)
    if store is not None and job_id:
        try:
            _merge(cfg, store.kv_get(job_id, "efficiency_overrides"))
        except Exception:  # noqa: BLE001 - a broken kv row must never stop detection
            pass
    return cfg


def thresholds_hash(cfg: dict) -> str:
    subset = {k: cfg.get(k, DEFAULTS[k]) for k in _THRESHOLD_KEYS}
    return hashlib.sha256(json.dumps(subset, sort_keys=True).encode()).hexdigest()[:12]


# -- findings -----------------------------------------------------------------

def _k(x: float) -> str:
    return f"{x / 1000:.0f}k"


def _epoch(ts) -> float:
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _finding(cfg: dict, kind: str, subject: str, evidence: str, metric: tuple, remedy: str, *,
             stage: str = "", unit=None, saving_usd: float = 0.0, saving_min: float = 0.0,
             actions: tuple | None = None, policy: dict | None = None, text_extra: str = "") -> dict:
    rem = {**REMEDIES[remedy], "text": owner.fill(REMEDIES[remedy]["text"]), "short": owner.fill(REMEDIES[remedy]["short"])}
    if actions is None:
        actions = rem["actions"]
    if policy is None and rem["policy_key"] and rem["policy_value"] and "reconfigure" in actions:
        policy = {rem["policy_key"]: rem["policy_value"]}
    if "reconfigure" not in actions:
        policy = None
    return {
        "id": f"{kind}:{subject}", "kind": kind, "subject": subject, "stage": stage, "unit": unit,
        "evidence": evidence, "metric": {"name": metric[0], "value": metric[1]},
        "saving_usd": float(saving_usd), "saving_min": float(saving_min),
        "remedy": remedy, "remedy_text": rem["text"] + text_extra, "remedy_short": rem["short"],
        "actions": tuple(actions), "policy": policy,
        "actionable": saving_usd >= cfg["min_saving_usd"] or saving_min >= cfg["min_saving_min"],
        "epoch": None,
    }


def _price_per_m(model: str, slot: int) -> float:
    """$ per million tokens for slot 0 input, 1 output, 2 cache read, 3 cache creation (via tokens.price_usd)."""
    vals = [0, 0, 0, 0]
    vals[slot] = 1_000_000
    return tokens.price_usd(model, vals)


def _candidates(agents: list[dict]) -> list[dict]:
    """Supersession: only a running agent, or the most recently started agent of its stage, is reported."""
    latest: dict[str, dict] = {}
    for a in agents:
        cur = latest.get(a["stage"])
        if cur is None or (a["start"], a["key"]) > (cur["start"], cur["key"]):
            latest[a["stage"]] = a
    return [a for a in agents if not a["finished"] or latest[a["stage"]] is a]


def _one_per_stage(pairs: list[tuple[dict, dict]]) -> list[dict]:
    """(agent, finding) pairs -> at most one finding per (kind, stage): the most recently started agent's."""
    best: dict[tuple, tuple] = {}
    for agent, f in pairs:
        key = (f["kind"], f["stage"])
        rank = (agent["start"], agent["key"])
        if key not in best or rank > best[key][0]:
            best[key] = (rank, f)
    return [v[1] for _, v in sorted(best.items())]


# -- agent detectors ----------------------------------------------------------

def _long_lived(agents: list[dict], cfg: dict) -> list[dict]:
    pairs = []
    for a in _candidates([x for x in agents if x["stage"] != "worker"]):
        calls, mean, csum = a["calls"], a["ctx_mean"], a["ctx_sum"]
        if not (calls > cfg["long_lived_calls"]
                or (mean > cfg["long_lived_mean_ctx"] and csum > cfg["long_lived_ctx_sum"])):
            continue
        base = a.get("ctx_first20_mean", mean)
        saving = a["usd_input_side"] * max(0.0, 1 - base / mean) if mean else 0.0
        pairs.append((a, _finding(
            cfg, "long_lived_agent", a["key"], f"{calls} calls × {_k(mean)} ctx", ("ctx_sum", csum),
            "respawn_per_unit", stage=a["stage"], unit=a["unit"], saving_usd=saving)))
    return _one_per_stage(pairs)


def _ctx_growth(agents: list[dict], cfg: dict) -> list[dict]:
    subs = [a for a in agents if a["stage"] != "worker"]
    pairs = []
    for a in _candidates(subs):
        if a["calls"] < cfg["ctx_growth_min_calls"]:
            continue
        prior = sorted(b["ctx_mean"] for b in subs
                       if b["stage"] == a["stage"] and (b["start"], b["key"]) < (a["start"], a["key"])
                       and b["calls"] >= cfg["ctx_growth_min_calls"])
        if not prior:
            continue
        n = len(prior)
        median = prior[n // 2] if n % 2 else (prior[n // 2 - 1] + prior[n // 2]) / 2
        mean = a["ctx_mean"]
        if median <= 0 or mean <= cfg["ctx_growth_ratio"] * median:
            continue
        saving = a["usd_input_side"] * max(0.0, 1 - median / mean)
        pairs.append((a, _finding(
            cfg, "context_growth", a["key"], f"ctx/call {_k(mean)} = {mean / median:.1f}× earlier units",
            ("ctx_mean", mean), "trim_spawn_reads", stage=a["stage"], unit=a["unit"], saving_usd=saving)))
    return _one_per_stage(pairs)


def _step_down(observed: str, floor: str):
    """One level down, only if it is a settable value (high|medium) at or above the floor; else None."""
    if observed not in EFFORT_LEVELS:
        return None
    i = EFFORT_LEVELS.index(observed)
    if i == 0:
        return None
    to = EFFORT_LEVELS[i - 1]
    floor_i = EFFORT_LEVELS.index(floor) if floor in EFFORT_LEVELS else EFFORT_LEVELS.index("medium")
    return to if to in _SETTABLE_EFFORT and EFFORT_LEVELS.index(to) >= floor_i else None


def _hidden_thinking(metrics: dict, agents: list[dict], cfg: dict) -> list[dict]:
    out = []
    for stage, st in sorted((metrics.get("stages") or {}).items()):
        if st.get("kind") == "worker":
            continue
        if not (st["out"] > cfg["thinking_stage_output"] and st["think_share"] > cfg["thinking_share"]):
            continue
        members = [a for a in agents if a["stage"] == stage]
        model = max(members, key=lambda a: (a["start"], a["key"]))["model"] if members else ""
        per_agent = st["think_est"] / max(st["agents"], 1)
        saving_usd = cfg["think_cut"] * per_agent * _price_per_m(model, 1) / 1e6
        saving_min = cfg["think_cut"] * per_agent / max(st["tok_s"], 1) / 60
        to = _step_down(st.get("effort", ""), cfg["envelope"]["effort_floor"])
        kw = ({"policy": {"effort": {stage: to}}, "text_extra": f" ({st['effort']} to {to})"} if to
              else {"actions": ("escalate",)})
        out.append(_finding(
            cfg, "hidden_thinking", stage,
            f"thinking {st['think_share']:.0%} of {_k(st['out'])} output at effort {st.get('effort') or 'unknown'}",
            ("think_share", st["think_share"]), "effort_step_down", stage=stage,
            saving_usd=saving_usd, saving_min=saving_min, **kw))
    return out


def _planning_serial(agents: list[dict], cfg: dict) -> list[dict]:
    builds = [a for a in agents if a["kind"] == "build" and a["unit"] is not None]
    out = []
    for stage in sorted({a["stage"] for a in agents if a["kind"] == "plan"}):
        planners = sorted((a for a in agents if a["stage"] == stage and a["unit"] is not None),
                          key=lambda a: (efficiency.unit_key(a["unit"]), a["start"], a["key"]))
        run: list[dict] = []
        for p in planners:
            earlier = [b for b in builds if efficiency.unit_before(b["unit"], p["unit"])]
            if not earlier:
                continue  # nothing to be serial after; does not break a run
            prev = max(earlier, key=lambda b: (efficiency.unit_key(b["unit"]), b["end"], b["key"]))
            if prev["finished"] and p["start"] >= prev["end"] - cfg["plan_serial_tolerance_s"]:
                run.append(p)
            else:
                run = []
        if len(run) < cfg["plan_serial_units"]:
            continue
        serial_s = sum(p["end"] - p["start"] for p in run) / len(run)
        units = ",".join(efficiency.unit_label(p["unit"]) for p in run)
        out.append(_finding(
            cfg, "planning_serial", stage, f"plan started after build ended, units {units}",
            ("serial_plan_s", serial_s), "plan_ahead", stage=stage, unit=run[-1]["unit"],
            saving_min=serial_s / 60))
    return out


def _cache_misses(agents: list[dict], cfg: dict) -> list[dict]:
    pairs = []
    for a in _candidates(agents):
        share_hit = a["calls"] >= cfg["cache_miss_min_calls"] and a["cache_miss"] > cfg["cache_miss_share"]
        gap_hit = a["stage"] != "worker" and a["max_call_gap_s"] > cfg["cache_ttl_s"]
        if not (share_hit or gap_hit):
            continue
        cr, cc = a.get("cache_read", 0), a.get("cache_create", 0)
        per_m = _price_per_m(a["model"], 3) - _price_per_m(a["model"], 2)
        saving = max(0.0, cc - 0.10 * (cr + cc)) * per_m / 1e6
        if share_hit:
            ev, metric = (f"{a['cache_miss']:.0%} of cached input re-created over {a['calls']} calls",
                          ("cache_miss_share", a["cache_miss"]))
        else:
            ev, metric = (f"{a['max_call_gap_s']:.0f} s between calls > cache TTL {cfg['cache_ttl_s']} s",
                          ("max_call_gap_s", a["max_call_gap_s"]))
        pairs.append((a, _finding(cfg, "cache_misses", a["key"], ev, metric, "warm_cache",
                                  stage=a["stage"], unit=a["unit"], saving_usd=saving)))
    return _one_per_stage(pairs)


def _artifact_bloat(metrics: dict, agents: list[dict], cfg: dict) -> list[dict]:
    by_key = {a["key"]: a for a in agents}
    best: dict[str, dict] = {}
    for art in metrics.get("artifacts") or []:
        readers = [by_key[k] for k in art.get("whole_readers", ()) if k in by_key]
        if art["chars"] <= cfg["artifact_chars"] or not readers:
            continue
        # Upper bound: every call of each whole-reader carries the artifact (per-call read time is not tracked).
        excess = max(0.0, art["chars"] / 4 - 2000)
        saving = sum(excess * r["calls"] * _price_per_m(r["model"], 2) / 1e6 for r in readers)
        f = _finding(cfg, "artifact_bloat", art["path_key"],
                     f"{_k(art['chars'])} chars written, read whole by {len(readers)} later agent(s)",
                     ("artifact_chars", art["chars"]), "smaller_artifact", stage=art["stage"], saving_usd=saving)
        if f["id"] not in best or art["chars"] > best[f["id"]]["metric"]["value"]:
            best[f["id"]] = f
    return [best[k] for k in sorted(best)]


def _build_output(metrics: dict, cfg: dict) -> list[dict]:
    out = []
    for cmd, sizes in sorted((metrics.get("big_results") or {}).items()):
        big = [c for c in sizes if c > cfg["build_output_chars"]]
        if len(big) < cfg["build_output_repeats"]:
            continue
        # One-time cost of the excess over a ~500-token tail, written once to the cache.
        tokens_over = sum(max(0.0, c / 4 - 500) for c in big)
        saving = tokens_over * _price_per_m("", 3) / 1e6
        out.append(_finding(
            cfg, "build_output_bloat", cmd,
            f"{len(big)} tool results > {_k(cfg['build_output_chars'])} chars from one command (largest {_k(max(big))})",
            ("big_results", len(big)), "quiet_output", saving_usd=saving))
    return out


# -- job-level detectors ------------------------------------------------------

_DUP_EVENTS = frozenset({"jev_flag", "jev_escalated", "stall diagnosis produced", "finish_line_not_met",
                         "yield_classified", "spend_rate_high", "watcher_overspend"})


def _idle_wait(metrics: dict, cfg: dict, now: float, job_state: str) -> list[dict]:
    t = metrics.get("time") or {}
    last = t.get("last_busy") or 0.0
    gap = now - last
    if job_state != "RUNNING" or not last or t.get("open_tool") or gap <= cfg["idle_wait_s"]:
        return []
    return [_finding(cfg, "idle_wait", "job", f"nothing generating or running for {gap / 60:.0f} min",
                     ("idle_s", gap), "escalate_idle", saving_min=gap / 60)]


def _cost_per_unit(metrics: dict, cfg: dict, budget_usd) -> list[dict]:
    out = []
    done = [u for u in (metrics.get("units") or []) if u.get("complete")]
    prior = sorted(u["usd"] for u in done[:-1])
    if done and len(prior) >= cfg["unit_cost_min_prior"]:
        n = len(prior)
        median = prior[n // 2] if n % 2 else (prior[n // 2 - 1] + prior[n // 2]) / 2
        last = done[-1]
        if median > 0 and last["usd"] > cfg["unit_cost_ratio"] * median:
            out.append(_finding(
                cfg, "cost_per_unit_trend", f"unit-{efficiency.unit_label(last['unit'])}",
                f"unit {efficiency.unit_label(last['unit'])} ${last['usd']:.2f} = {last['usd'] / median:.1f}× median of prior units",
                ("unit_usd", last["usd"]), "escalate_projection", unit=last["unit"],
                saving_usd=last["usd"] - median))
    proj = metrics.get("projection")
    if budget_usd and proj and proj.get("projected_usd") is not None:
        total = (metrics.get("totals") or {}).get("usd", 0.0) + proj["projected_usd"]
        if total > budget_usd:
            out.append(_finding(
                cfg, "cost_per_unit_trend", "budget", f"projection ${total:,.2f} > budget ${budget_usd:,.2f}",
                ("projected_total_usd", total), "escalate_projection", saving_usd=total - budget_usd))
    return out


def _redundant_supervision(metrics: dict, cfg: dict, events) -> list[dict]:
    out = []
    dups = sorted(_epoch(e.ts) for e in events if e.what in _DUP_EVENTS)
    window = cfg["redundant_window_s"]
    runs: dict[str, list[float]] = {}
    for e in sorted((e for e in events if e.what == "watcher_verdict"), key=lambda e: (_epoch(e.ts), e.seq)):
        ev = e.evidence or {}
        name = ev.get("name")
        if not isinstance(name, str):
            continue
        t = _epoch(e.ts)
        dup = ev.get("status") != "ok" and any(d <= t <= d + window for d in dups)
        cost = ev.get("cost_usd")
        runs[name] = runs.get(name, []) + [float(cost) if isinstance(cost, (int, float)) else 0.0] if dup else []
    for name, costs in sorted(runs.items()):
        if len(costs) >= cfg["redundant_runs"]:
            out.append(_finding(
                cfg, "redundant_supervision", name, f"{len(costs)} runs in a row duplicating other supervisors",
                ("duplicate_runs", len(costs)), "escalate_remove_watcher", saving_usd=sum(costs)))
    sup = metrics.get("supervision") or {}
    share = sup.get("share_of_worker", 0.0)
    if share > cfg["supervision_share"]:
        out.append(_finding(
            cfg, "redundant_supervision", "supervision",
            f"supervision ${sup.get('usd', 0.0):.2f} = {share:.0%} of worker spend",
            ("share_of_worker", share), "escalate_remove_watcher",
            saving_usd=sup.get("usd", 0.0) * (1 - cfg["supervision_share"] / share)))
    return out


# -- entry point --------------------------------------------------------------

def detect(metrics: dict, cfg: dict, *, events=(), now: float = 0.0, job_state: str = "RUNNING",
           budget_usd: float | None = None) -> list[dict]:
    """Pure and sorted: the same metrics, config and events give an identical list."""
    agents = list(metrics.get("agents") or [])
    found = list(_long_lived(agents, cfg))
    found += _ctx_growth(agents, cfg)
    found += _hidden_thinking(metrics, agents, cfg)
    found += _planning_serial(agents, cfg)
    found += _cache_misses(agents, cfg)
    found += _artifact_bloat(metrics, agents, cfg)
    found += _build_output(metrics, cfg)
    found += _idle_wait(metrics, cfg, now, job_state)
    found += _cost_per_unit(metrics, cfg, budget_usd)
    found += _redundant_supervision(metrics, cfg, list(events))
    found.sort(key=lambda f: (-f["saving_usd"], -f["saving_min"], f["id"]))
    return found
