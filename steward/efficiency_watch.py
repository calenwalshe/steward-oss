"""The efficiency watcher: a built-in, code-only supervisor role that runs on every job (phase 16, G4/G10/G11/G12/G14).

`EfficiencyWatcher.step` is what the runner's tick calls in place of the pre-phase `_spend_step`. It

* keeps the spend guard (watcher overspend halving, the $/h alert, `spend_check_error`) with the same events,
* parses the job's transcripts incrementally on every tick,
* recomputes the metrics and detectors at most once per `recompute_every_s` (and when a subagent newly finishes),
* records new/cleared findings as `efficiency_finding` / `efficiency_finding_cleared` events and keeps a compact
  state row in kv (`efficiency_state`) that the supervisor row and the pane's ⚠ lines read.

Authority (16-05, G6/G8/G11) sits on the Phase 15 trust ladder under the record `watcher_authority:efficiency`,
initialised at T0 (never the legacy T2). Every power is decided by code: at T0 or in shadow mode it only records
`efficiency_would_nudge`; a T2 nudge goes only through `Steering.record` (origin `watcher:efficiency`, never urgent)
at a boundary. A failure inside it is recorded as `efficiency_error` and never reaches the tick.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

from steward import efficiency, eta, eta_jev, eta_prior, eta_structure, tokens
from steward import efficiency_review as review_module
from steward import efficiency_detect as detect_module
from steward import watcher_authority as wa
from steward.models import Judgment
from steward.runner_exec import SubprocessRunner
from steward.steering import Steering
from steward.store import Store

STATE_KEY = "efficiency_state"
SWITCH_KEY = "supervisor_efficiency"
MODES = ("off", "shadow", "active")
SLOW_CPU_S = 1.0
SLOW_EVERY_S = 600.0
SPEND_KINDS = ("watcher_overspend", "spend_rate")
RULE = "efficiency_watch"
GATE_RULE = "efficiency_gate"
ETA_RULE = "eta"
ETA_ERROR_KEY = "eta_last_error"
ETA_JEV_SEEN_KEY = "eta_jev_seen"
ETA_NOTIFY_KEY = "eta_notify"
ETA_SEEN_KEEP = 50
NAME = "efficiency"
ORIGIN = "watcher:efficiency"
NUDGE_CAP = 600
JOB_COOLDOWN_S = 300.0  # latency #6: findings coalesce into at most one steer per job per 5 min
OBSERVE_SHARE = 0.25  # G12: supervision above 25% of worker spend -> observe only
SEEN_KEY = "efficiency_would_nudge_seen"
UNITS_COMPLETE_KEY = "efficiency_units_complete"  # units seen complete (a flip is a review boundary)
BASELINES_KEY = "efficiency_baselines"  # G13: {finding id: baseline} (closed once its effect is reported)
DEFER_S = 600.0  # G11: a doctor/Jev stall signal this recent owns an idle wait
DEFER_EVENTS = frozenset({"stall diagnosis produced", "jev_flag", "jev_escalated"})

# All phase-16 event names (the contract; the feed renders every one).
EVENT_NAMES = (
    "efficiency_finding", "efficiency_finding_cleared", "efficiency_error", "efficiency_slow",
    "efficiency_units_set", "efficiency_would_nudge", "efficiency_nudged", "efficiency_escalated",
    "efficiency_policy_changed", "efficiency_refused", "efficiency_effect", "efficiency_review",
    "efficiency_cap_tripped", "efficiency_overrides_changed",
)


# -- spend shape (absorbed from steward.stability, G11) -----------------------------------------------------


def spend_shape_findings(tokens_data: dict) -> dict:
    """Pure: `{"overspenders": [(watcher, usd, worker_usd, ratio)], "rate_usd_h": float}` from `tokens.job_tokens`."""
    from steward import stability

    sources = (tokens_data or {}).get("sources") or {}
    worker = tokens.worker_usd(tokens_data or {})  # worker + subagents (G1)
    over = []
    for src, s in sources.items():
        if not src.startswith("watcher:"):
            continue
        usd = float(s.get("usd") or 0.0)
        if usd >= stability.OVERSPEND_MIN_USD and usd > stability.OVERSPEND_RATIO * worker:
            over.append((src.removeprefix("watcher:"), usd, worker, (usd / worker) if worker else float("inf")))
    rate = float(((tokens_data or {}).get("last_hour") or {}).get("usd") or 0.0)
    return {"overspenders": sorted(over, key=lambda t: -t[1]), "rate_usd_h": rate}


def spend_shape_warnings(tokens_data: dict | None) -> list[str]:
    """Pane lines: `⚠ <watcher> costs 2.3× worker`, `⚠ spend $6.20/h`."""
    from steward import stability

    f = spend_shape_findings(tokens_data or {})
    out = []
    for name, usd, worker, ratio in f["overspenders"]:
        out.append(f"⚠ {name} costs {ratio:.1f}× worker" if worker else f"⚠ {name} costs ${usd:.2f}, worker $0.00")
    if f["rate_usd_h"] > stability.RATE_LIMIT_USD_PER_H:
        out.append(f"⚠ spend ${f['rate_usd_h']:.2f}/h")
    return out


def apply_spend_shape(store: Store, job_id: str, tokens_data: dict, notify_fn=None) -> list[str]:
    """Record + route what `spend_shape_findings` shows. Per watcher: `watcher_overspend`, then halve its cadence
    (only after it has run again since the last halving; max 2). Rate over $5/h: `spend_rate_high` + one NEEDS_YOU."""
    from steward import custom_watchers as cw
    from steward import stability

    done: list[str] = []
    f = spend_shape_findings(tokens_data)
    for name, usd, worker, ratio in f["overspenders"]:
        runs = cw.run_count(store, job_id, name)
        seen = store.kv_get(job_id, f"watcher_overspend_runs:{name}")
        if seen is not None and runs <= int(seen):
            continue  # nothing new to react to since the last halving
        store.kv_set(job_id, f"watcher_overspend_runs:{name}", runs)
        halved = cw.halve_cadence(store, job_id, name, reason=f"costs {ratio:.1f}x worker")
        store.append_event(job_id, what="watcher_overspend",
                           evidence={"name": name, "usd": round(usd, 4), "worker_usd": round(worker, 4),
                                     "ratio": None if ratio == float("inf") else round(ratio, 2),
                                     "cadence_halved": halved is not None,
                                     "every_s": halved["every_s"] if halved else None},
                           rule="spend_shape", judgment=Judgment.code)
        done.append(f"{name}: {'halved' if halved else 'capped'}")
    limit = stability.RATE_LIMIT_USD_PER_H
    from steward import spend_policy

    if spend_policy.enabled():
        lim = spend_policy.effective(store, job_id, "spend_rate_hour")
        limit = lim.usd if lim is not None else limit
        if f["rate_usd_h"] > limit:
            spend_policy.advise(store, job_id, "spend_rate_hour", observed_usd=f["rate_usd_h"], notify_fn=notify_fn,
                                now=time.time(), default_usd=limit)
        if f["rate_usd_h"] > limit and not store.kv_get(job_id, "spend_rate_alerted", default=False):
            store.kv_set(job_id, "spend_rate_alerted", True)
            store.append_event(job_id, what="spend_rate_high",
                               evidence={"usd_per_hour": round(f["rate_usd_h"], 2), "limit": limit,
                                         "binding": "advisory"},
                               rule="spend_shape", judgment=Judgment.code)
            done.append("rate")
        return done
    if f["rate_usd_h"] > limit and not store.kv_get(job_id, "spend_rate_alerted", default=False):
        store.kv_set(job_id, "spend_rate_alerted", True)
        store.append_event(job_id, what="spend_rate_high",
                           evidence={"usd_per_hour": round(f["rate_usd_h"], 2), "limit": limit},
                           rule="spend_shape", judgment=Judgment.code)
        if notify_fn is not None:
            notify_fn(job_id, "NEEDS_YOU", "Steward: needs you",
                      f"spending ${f['rate_usd_h']:.2f}/h (limit ${limit:.0f}/h)")
        done.append("rate")
    return done


def spend_finding_dicts(tokens_data: dict | None) -> list[dict]:
    """The spend-shape results as efficiency findings (kinds watcher_overspend and spend_rate)."""
    from steward import stability

    f = spend_shape_findings(tokens_data or {})
    warnings = spend_shape_warnings(tokens_data or {})
    out = []
    for i, (name, usd, worker, ratio) in enumerate(f["overspenders"]):
        out.append(_spend_finding("watcher_overspend", name, warnings[i].removeprefix("⚠ "),
                                  ("ratio", None if ratio == float("inf") else round(ratio, 2)), usd))
    if f["rate_usd_h"] > stability.RATE_LIMIT_USD_PER_H:
        out.append(_spend_finding("spend_rate", "job", warnings[-1].removeprefix("⚠ "),
                                  ("usd_per_hour", round(f["rate_usd_h"], 2)), 0.0))
    return out


def _spend_finding(kind: str, subject: str, evidence: str, metric: tuple, usd: float) -> dict:
    return {"id": f"{kind}:{subject}", "kind": kind, "subject": subject, "stage": "", "unit": None,
            "evidence": evidence, "metric": {"name": metric[0], "value": metric[1]},
            "saving_usd": 0.0, "saving_min": 0.0, "remedy": "", "remedy_text": "", "remedy_short": "",
            "actions": [], "policy": None, "actionable": False, "epoch": None}


# -- mode ---------------------------------------------------------------------------------------------------


def mode(store: Store, job_id: str, config_dir=None) -> str:
    """off | shadow | active. STEWARD_EFFICIENCY=0 forces off; else the per-job switch ("on" = shadow); else
    `[efficiency] mode` in steward.toml; else shadow."""
    if os.environ.get("STEWARD_EFFICIENCY") == "0":
        return "off"
    flag = store.kv_get(job_id, SWITCH_KEY)
    if flag in ("on", *MODES):
        return "shadow" if flag == "on" else flag
    m = str(detect_module.load_config(None, None, config_dir).get("mode") or "shadow").lower()
    return "shadow" if m == "on" else m if m in MODES else "shadow"


# -- state row formatting -----------------------------------------------------------------------------------


def _k(n: float) -> str:
    return f"{n / 1000:.0f}k"


def row_parts(state) -> list[str]:
    """Supervisor-row parts from the stored state (a dict or None)."""
    if isinstance(state, dict) and state.get("codex"):
        return ["n/a (codex transcript)"]
    s = state.get("summary") if isinstance(state, dict) else None
    if not s:
        return ["no data yet"]
    usd = float(s.get("usd") or 0.0)
    calls = int(s.get("calls") or 0)
    parts = [f"${usd:.0f}" if usd >= 10 else f"${usd:.2f}",
             f"sub {float(s.get('sub_share') or 0.0):.0%}",
             f"{calls / 1000:.1f}k calls" if calls >= 1000 else f"{calls} calls",
             f"peak ctx {_k(float(s.get('peak_ctx') or 0))}",
             f"think≈{float(s.get('think_share') or 0.0):.0%}",
             f"{float(s.get('tok_s') or 0.0):.0f} tok/s"]
    if s.get("crit"):
        parts.append(f"crit: {s['crit']}")
    return parts + eta_row_parts(state.get("eta"))


def eta_row_parts(stored) -> list[str]:
    """`units 3/7`, `eta from cycle times` for the efficiency row (G16); nothing without a real ETA source."""
    if not isinstance(stored, dict) or stored.get("source") in (None, "none", "n/a"):
        return []
    parts = []
    if stored.get("units_total") is not None:
        parts.append(f"units {stored.get('units_done', 0)}/{'' if stored.get('units_exact') else '~'}{stored['units_total']}")
    src = stored["source"]
    if src == "peers" and stored.get("stage"):
        parts.append("eta from peers + stage")
    elif src in eta._FROM:
        parts.append(f"eta {eta._FROM[src]}")
    return parts


def finding_label(f: dict) -> str:
    stage, unit = f.get("stage") or "", f.get("unit")
    if stage and unit is not None:
        return f"{stage}#{efficiency.unit_label(unit)}"
    return stage or str(f.get("subject") or f.get("kind") or "")


def format_finding(f: dict) -> str:
    """`⚠ <label> <evidence> → <remedy> (≈-$X / ≈N min)` for one finding."""
    saving = []
    if f.get("saving_usd"):
        saving.append(f"≈-${float(f['saving_usd']):.2f}")
    if f.get("saving_min"):
        saving.append(f"≈{float(f['saving_min']):.0f} min")
    tail = f" ({' / '.join(saving)})" if saving else ""
    return f"⚠ {finding_label(f)} {f.get('evidence', '')} → {f.get('remedy_short', '')}{tail}"


def pane_warnings(store: Store, job_id: str) -> list[str]:
    """One ⚠ line per active detector finding (spend-shape kinds are printed by the dash from the token data)."""
    state = store.kv_get(job_id, STATE_KEY)
    if not isinstance(state, dict) or state.get("mode") == "off" or state.get("codex"):
        return []
    return [format_finding(f) for f in state.get("findings") or [] if f.get("kind") not in SPEND_KINDS]


# -- authority: spec, record, powers (G6) --------------------------------------------------------------------


def builtin_spec(cfg: dict) -> dict:
    """The efficiency watcher as a Phase 15 spec. `ask` carries the thresholds hash, so a threshold change moves the
    spec hash and demotes it the Phase 15 way."""
    return {"name": NAME, "model": "code", "every_s": cfg["recompute_every_s"],
            "may": ["escalate", "nudge", "reconfigure"],
            "ask": "builtin:efficiency:v1:" + detect_module.thresholds_hash(cfg)}


def ensure_record(store: Store, job_id: str, spec: dict) -> dict:
    """Create the trust record at T0 before anything reads the tier (no record would read as legacy T2)."""
    rec = wa.get_record(store, job_id, spec["name"])
    return rec if rec is not None else wa.init_record(store, job_id, spec)


def powers(store: Store, job_id: str, spec: dict) -> set[str]:
    """Phase 15 tier powers ∩ may, plus reconfigure at T2+."""
    out = wa.active_powers(store, job_id, spec)
    if "reconfigure" in (spec.get("may") or []) and wa.tier_of(store, job_id, spec) >= 2:
        out.add("reconfigure")
    return out


# -- boundary and nudge text (G8) -----------------------------------------------------------------------------


def worker_idle(transcripts: list[dict]) -> bool:
    """The worker transcript's last line is an assistant message whose call ended with stop_reason end_turn."""
    for tr in transcripts:
        if tr.get("source") != "worker":
            continue
        d = (tr.get("entry") or {}).get("detail") or {}
        if d.get("last_type") != "assistant":
            return False
        last = next(iter(reversed((d.get("calls") or {}).values())), None)
        return bool(last) and last[4] == "end_turn"
    return False


def boundary(transcripts: list[dict], newly_finished, sig=None, *, fabric: bool = False) -> bool:
    """A subagent newly finished, or the worker is idle. Never mid-call. `sig.turn_open is False` on a fabric worker is
    an extra idle hint only."""
    if newly_finished or worker_idle(transcripts):
        return True
    return bool(fabric and sig is not None and getattr(sig, "turn_open", None) is False)


def nudge_text(f: dict, policy_line: str = "") -> str:
    """One change, the evidence, the saving and the measure; a fixed catalog remedy plus numbers (no transcript
    text), capped at 600 chars. A non-default job policy is restated at the end."""
    stage = f.get("stage") or "job"
    metric = (f.get("metric") or {}).get("name") or f.get("kind") or "cost"
    remedy = str(f.get("remedy_text") or "").rstrip(". ")
    tail = f" Job efficiency policy: {policy_line}." if policy_line else ""
    body = (f"[efficiency] From now on, {remedy}. Evidence: {finding_label(f)} {f.get('evidence', '')}. "
            f"Expected saving ≈${float(f.get('saving_usd') or 0):.2f} / ≈{float(f.get('saving_min') or 0):.0f} min "
            f"per {stage} unit. Measured by: {metric} on the next {stage} unit.")
    return body[:max(NUDGE_CAP - len(tail), 0)] + tail


# -- effect measurement (G13) --------------------------------------------------------------------------------

# finding metric name -> how one comparable agent yields the same metric (None = special-cased below)
_AGENT_FIELD = {"ctx_sum": "ctx_sum", "ctx_mean": "ctx_mean", "think_share": "think_share",
                "cache_miss_share": "cache_miss", "max_call_gap_s": "max_call_gap_s"}
_SPECIAL = ("serial_plan_s", "artifact_chars", "big_results")
MEASURABLE = tuple(_AGENT_FIELD) + _SPECIAL


def _r(x) -> float:
    return round(float(x), 4)


def _minutes(a: dict) -> float:
    return max(float(a.get("end") or 0) - float(a.get("start") or 0), 0.0) / 60


def _serial_s(a: dict, agents: list[dict], cfg: dict) -> float:
    """Seconds of a plan agent that ran serial after the previous unit's build (0 when it overlapped the build)."""
    earlier = [b for b in agents if b["kind"] == "build" and efficiency.unit_before(b["unit"], a["unit"])]
    if not earlier:
        return 0.0
    prev = max(earlier, key=lambda b: (efficiency.unit_key(b["unit"]), b["end"], b["key"]))
    tol = float(cfg.get("plan_serial_tolerance_s", 0))
    return max(a["end"] - a["start"], 0.0) if prev["finished"] and a["start"] >= prev["end"] - tol else 0.0


def _comparable(metrics: dict, stage: str) -> list[dict]:
    return sorted((a for a in metrics.get("agents") or [] if a["kind"] != "worker" and (not stage or a["stage"] == stage)),
                  key=lambda a: (a["start"], a["key"]))


def _agent_value(name: str, a: dict, metrics: dict, cfg: dict):
    """(value, usd, minutes) of one agent for the metric `name`."""
    if name in _AGENT_FIELD:
        return _r(a[_AGENT_FIELD[name]]), _r(a["usd"]), _r(_minutes(a))
    if name == "serial_plan_s":
        v = _serial_s(a, metrics.get("agents") or [], cfg)
        return _r(v), 0.0, _r(v / 60)
    if name == "artifact_chars":
        chars = max((x["chars"] for x in metrics.get("artifacts") or [] if x["writer"] == a["key"]), default=0)
        return _r(chars), _r(a["usd"]), _r(_minutes(a))
    big = [c for c in (metrics.get("big_results") or {}).values() for c in c if c > cfg["build_output_chars"]]
    return _r(len(big)), _r(a["usd"]), _r(_minutes(a))


def make_baseline(f: dict, action: str, ref, now: float, metrics: dict | None, cfg: dict) -> dict:
    """The metric that fired, for the last comparable finished agent of that stage (else the firing agent's, or the
    finding's own value)."""
    name = (f.get("metric") or {}).get("name") or ""
    stage = f.get("stage") or ""
    fired = (f.get("metric") or {}).get("value")
    before, usd, mins, known, count_at = fired, 0.0, 0.0, [], None
    if metrics is not None and name in MEASURABLE:
        pool = _comparable(metrics, stage)
        known = [a["key"] for a in pool]
        wrote = {x["writer"] for x in metrics.get("artifacts") or []}
        finished = [a for a in pool if a["finished"] and (name != "artifact_chars" or a["key"] in wrote)]
        firing = next((a for a in pool if a["key"] == f.get("subject")), pool[-1] if pool else None)
        if name == "big_results":
            count_at = int(fired or 0)  # the job-wide count of big results for this command when it fired
        if finished:
            v, usd, mins = _agent_value(name, finished[-1], metrics, cfg)
            if name != "big_results":
                before = v
        elif firing is not None:
            _v, usd, mins = _agent_value(name, firing, metrics, cfg)
    if isinstance(before, (int, float)):
        before = _r(before)
    return {"finding": f["id"], "kind": f.get("kind"), "stage": stage, "metric": name, "before": before,
            "before_usd": usd, "before_min": mins, "unit": f.get("unit"), "action": action, "ref": ref, "at": now,
            "known": known, "cmd": f.get("subject") if name == "big_results" else None, "count_at": count_at,
            "measurable": name in MEASURABLE, "closed": False}


def measure_effect(bl: dict, metrics: dict, cfg: dict) -> dict | None:
    """The one efficiency_effect payload once the next comparable agent has finished, else None."""
    if bl.get("closed") or not bl.get("measurable"):
        return None
    name = bl["metric"]
    known = set(bl.get("known") or [])
    nxt = [a for a in _comparable(metrics, bl.get("stage") or "") if a["key"] not in known and a["finished"]]
    if name == "artifact_chars":
        nxt = [a for a in nxt if any(x["writer"] == a["key"] for x in metrics.get("artifacts") or [])]
    if not nxt:
        return None
    a = nxt[0]
    after, usd, mins = _agent_value(name, a, metrics, cfg)
    if name == "big_results":
        cmd = bl.get("cmd")
        now_big = len([c for c in (metrics.get("big_results") or {}).get(cmd, []) if c > cfg["build_output_chars"]])
        after = _r(max(now_big - int(bl.get("count_at") or 0), 0))
    before = bl["before"]
    return {"finding": bl["finding"], "kind": bl.get("kind"), "stage": bl.get("stage"), "metric": name,
            "before": before, "after": after, "before_usd": bl["before_usd"], "after_usd": usd,
            "saved_usd": _r(bl["before_usd"] - usd), "saved_min": _r(bl["before_min"] - mins),
            "action": bl["action"], "ref": bl["ref"], "agent": a["key"]}


# -- the watcher --------------------------------------------------------------------------------------------


def _finished_keys(transcripts: list[dict]) -> set[str]:
    """Subagent transcripts whose last call ended its turn (mirrors efficiency._agents' `finished`)."""
    out = set()
    for tr in transcripts:
        if not str(tr.get("source", "")).startswith("sub:"):
            continue
        calls = ((tr.get("entry") or {}).get("detail") or {}).get("calls") or {}
        last = next(iter(reversed(calls.values())), None)
        if last and last[4] == "end_turn" and not last[6]:
            out.add(tr["stem"])
    return out


def _plain(f: dict) -> dict:
    return {**f, "actions": list(f.get("actions") or [])}


class EfficiencyWatcher:
    def __init__(self, store: Store, *, notify: Callable | None = None, clock: Callable[[], float] = time.time,
                 spend_clock: Callable[[], float] = time.monotonic, tokens_fn: Callable | None = None,
                 projects_dir: Path | str | None = None, config_dir=None,
                 cpu_fn: Callable[[], float] = time.process_time, review=None, eta_runner=None):
        self.store = store
        self.eta_runner = eta_runner if eta_runner is not None else SubprocessRunner()
        self._eta_cache: dict = {}          # job -> {head, base, facts, subjects}: the Structure is read once per HEAD sha
        self._priors: tuple | None = None   # (mtime_ns of eta-priors.json or None, loaded priors)
        self.notify = notify
        self.clock = clock
        self.spend_clock = spend_clock
        self.projects_dir = Path(projects_dir) if projects_dir is not None else None
        self.config_dir = config_dir
        self.cpu_fn = cpu_fn
        self.tokens_fn = tokens_fn or (lambda st, jid, now=None: tokens.job_tokens(
            st, jid, now=now, projects_dir=self.projects_dir))
        self._cache: dict = {}
        self._finished: dict[str, set[str]] = {}
        self._spend_at: dict[str, float] = {}
        self._computed_at: dict[str, float] = {}
        self._slow_at: dict[str, float] = {}
        self._last_tokens: dict[str, dict] = {}
        self._share: dict[str, float] = {}
        self._metrics: dict[str, dict] = {}
        self.review = review if review is not None else review_module.EfficiencyReview(
            store, clock=clock, notify=notify, config_dir=config_dir)

    # -- spend guard (runs in every mode) ---------------------------------

    def _spend_guard(self, job_id: str) -> None:
        from steward import stability

        now = self.spend_clock()
        if now - self._spend_at.get(job_id, float("-inf")) < stability.SPEND_CHECK_EVERY_S:
            return
        self._spend_at[job_id] = now
        try:
            data = self.tokens_fn(self.store, job_id, now=self.clock())
            self._last_tokens[job_id] = data
            apply_spend_shape(self.store, job_id, data, self.notify)
        except Exception as exc:  # noqa: BLE001 - SP-6: a supervisor failure never stops the tick
            self.store.append_event(job_id, what="spend_check_error", evidence={"error": str(exc)[:200]},
                                    rule="spend_shape", judgment=Judgment.code)

    # -- the step ----------------------------------------------------------

    def step(self, job_id: str, sig=None) -> dict | None:
        self._spend_guard(job_id)
        try:
            self.review.poll(job_id, self.clock(), self._metrics.get(job_id))
        except Exception as exc:  # noqa: BLE001 - a review failure never stops the tick
            self.store.append_event(job_id, what="efficiency_error", evidence={"error": str(exc)[:200]},
                                    rule=RULE, judgment=Judgment.code)
        try:
            return self._efficiency(job_id, sig)
        except Exception as exc:  # noqa: BLE001 - never breaks the tick
            try:
                self.store.append_event(job_id, what="efficiency_error", evidence={"error": str(exc)[:200]},
                                        rule=RULE, judgment=Judgment.code)
            except Exception:  # noqa: BLE001
                pass
            return None

    def _budget(self, cfg: dict):
        if cfg.get("job_budget_usd"):
            return float(cfg["job_budget_usd"])
        try:
            from steward import trajectory as trajectory_module

            path = Path(self.config_dir) / "trajectory.toml" if self.config_dir else None
            return trajectory_module.load_trajectory_config(path).triggers.budget_usd
        except Exception:  # noqa: BLE001
            return None

    def _efficiency(self, job_id: str, sig=None) -> dict | None:
        store = self.store
        m = mode(store, job_id, self.config_dir)
        codex = store.kv_get(job_id, "worker_kind") == "codex"
        if m == "off" or codex:
            small = {"mode": m, "codex": codex}
            if codex and m != "off":  # G20: static, written once, no git call
                job = store.get_job(job_id)
                small["eta"] = eta.na_eta(eta_prior.job_kind(getattr(job, "goal", "")))
            if store.kv_get(job_id, STATE_KEY) != small:
                store.kv_set(job_id, STATE_KEY, small)
            return None

        cpu0 = self.cpu_fn()
        transcripts = tokens.job_transcripts(store, job_id, self.projects_dir, _cache=self._cache)
        finished = _finished_keys(transcripts)
        newly = finished - self._finished.get(job_id, set())
        self._finished[job_id] = finished
        cfg = detect_module.load_config(store, job_id, self.config_dir)
        now_m = self.spend_clock()
        due = now_m - self._computed_at.get(job_id, float("-inf")) >= float(cfg["recompute_every_s"])
        idle = worker_idle(transcripts)
        if not (due or newly):
            if idle:  # a boundary between recomputes: act on the stored findings
                prev = store.kv_get(job_id, STATE_KEY)
                if isinstance(prev, dict) and prev.get("findings"):
                    self._act(job_id, prev["findings"], sig, newly, self.clock(), worker_idle=True)
            return None
        self._computed_at[job_id] = now_m

        now = self.clock()
        tokens.new_pass(self._cache)  # the per-pass memos (review requests, advisor matches) must not outlive a pass
        metrics = efficiency.compute(store, job_id, now=now, projects_dir=self.projects_dir, config=cfg,
                                     transcripts=transcripts, _cache=self._cache)
        job = store.get_job(job_id)
        events = store.events(job_id)
        found = detect_module.detect(metrics, cfg, events=events, now=now, job_state=job.state.value,
                                     budget_usd=self._budget(cfg))
        found = [_plain(f) for f in found] + spend_finding_dicts(self._last_tokens.get(job_id))
        self._review_boundary(job_id, metrics, found, m, now)
        found += self.review.findings(job_id)  # escalate-only, from the latest review (G5)
        epoch = store.get_revision(job_id)
        for f in found:
            f["epoch"] = epoch

        prev = store.kv_get(job_id, STATE_KEY)
        prev = prev if isinstance(prev, dict) else {}
        prev_findings = {f["id"]: f for f in prev.get("findings") or [] if isinstance(f, dict) and "id" in f}
        first_seen = dict(prev.get("first_seen") or {})
        agent_calls = {a["key"]: a["calls"] for a in metrics["agents"]}
        current = {f["id"]: f for f in found}
        for fid, f in current.items():
            if fid in prev_findings:
                continue
            first_seen[fid] = now
            ev = {**f, "at": now}
            if f["subject"] in agent_calls:
                ev["calls"] = agent_calls[f["subject"]]
            store.append_event(job_id, what="efficiency_finding", evidence=ev, rule=RULE, judgment=Judgment.code)
        for fid, f in prev_findings.items():
            if fid not in current:
                first_seen.pop(fid, None)
                store.append_event(job_id, what="efficiency_finding_cleared",
                                   evidence={"id": fid, "kind": f.get("kind"), "at": now}, rule=RULE,
                                   judgment=Judgment.code)

        eta_dict = self._eta(job_id, metrics, found, now, events, prev.get("eta"))
        tot = metrics["totals"]
        summary = {"usd": tot["usd"], "sub_share": tot["sub_share"], "calls": tot["calls"],
                   "peak_ctx": tot["peak_ctx"], "think_share": tot["think_share"], "tok_s": tot["tok_s"],
                   "crit": (metrics.get("time") or {}).get("crit") or ""}
        cpu_s = max(self.cpu_fn() - cpu0, 0.0)
        state = {"mode": m, "codex": False, "computed_at": now, "summary": summary,
                 "active": [f["id"] for f in found], "findings": found, "first_seen": first_seen,
                 "cpu_s": cpu_s, "finished": sorted(finished)}
        if eta_dict is not None:
            state["eta"] = eta_dict
        store.kv_set(job_id, STATE_KEY, state)
        self._share[job_id] = float((metrics.get("supervision") or {}).get("share_of_worker") or 0.0)
        self._metrics[job_id] = metrics
        self._measure_effects(job_id, metrics, cfg, now)
        self._act(job_id, found, sig, newly, now, worker_idle=idle)
        if cpu_s > SLOW_CPU_S and now_m - self._slow_at.get(job_id, float("-inf")) >= SLOW_EVERY_S:
            self._slow_at[job_id] = now_m
            store.append_event(job_id, what="efficiency_slow", evidence={"cpu_s": round(cpu_s, 3)}, rule=RULE,
                               judgment=Judgment.code)
        return state

    # -- the ETA (phase 18, G13): computed here, at every recompute, read-only ------------------------

    def _priors_cached(self) -> dict:
        path = Path(self.store.home) / eta_prior.PRIORS_FILE
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            stamp = None
        if self._priors is None or self._priors[0] != stamp:
            self._priors = (stamp, eta_prior.load(self.store.home))
        return self._priors[1]

    def _eta_error(self, job_id: str, message: str) -> None:
        message = message[:200]
        if self.store.kv_get(job_id, ETA_ERROR_KEY) == message:
            return
        self.store.kv_set(job_id, ETA_ERROR_KEY, message)
        self.store.append_event(job_id, what="eta_error", evidence={"error": message}, rule=ETA_RULE,
                                judgment=Judgment.code)

    def _jev_labels(self, job_id: str, st, metrics: dict, prev: dict | None, base: dict | None) -> tuple:
        """(labels as plain strings or None, jev_state). Voted from kv eta_jev, fact-checked against the committed
        structure; rejections and disagreements are events, once each. Jev never changes the minutes."""
        from steward import jev_observer, supervisors

        store = self.store
        if supervisors.jev_mode(store, job_id, self.config_dir) == "off":
            return None, "off"
        status = store.kv_get(job_id, jev_observer.STATUS_KEY)
        if isinstance(status, str) and status.startswith("unavailable"):
            return None, "unavailable"
        records = store.kv_get(job_id, eta_jev.KV_KEY)
        if not isinstance(records, list) or not records:
            return None, "off"
        labs = eta_jev.labels(records, (prev or {}).get("jev"))
        facts = {"done": set(st.done), "planned": set(st.planned) if st.planned else None,
                 "units_done": len(st.done), "units_total": st.units_total if st.units_exact else None,
                 "running_unit": eta.running_unit(metrics, st)}
        grown = eta.scope_grew(base, st) or bool(base and base.get("scope_grown_at") is not None)
        kept, rejected, disagree = eta_jev.check(labs, facts, grown=grown)
        seen = store.kv_get(job_id, ETA_JEV_SEEN_KEY)
        seen = list(seen) if isinstance(seen, list) else []
        for name, rows, detail in (("eta_jev_rejected", rejected, "reason"), ("eta_disagree", disagree, "code")):
            for row in rows:
                key = f"{name}|{row['question']}|{row['label']}|{row[detail]}"
                if key in seen:
                    continue
                seen.append(key)
                store.append_event(job_id, what=name, evidence=dict(row), rule=ETA_RULE, judgment=Judgment.code)
        if seen != store.kv_get(job_id, ETA_JEV_SEEN_KEY):
            store.kv_set(job_id, ETA_JEV_SEEN_KEY, seen[-ETA_SEEN_KEEP:])
        plain = {q: (v.get("label") if isinstance(v, dict) else v) for q, v in kept.items()}
        return (plain or None), "ok"

    def _eta(self, job_id: str, metrics: dict, found: list, now: float, events, prev) -> dict | None:
        """The job's ETA dict for this recompute, or the previous one when it cannot be computed. Never raises."""
        store = self.store
        prev = prev if isinstance(prev, dict) and prev.get("source") != "n/a" else None
        try:
            job = store.get_job(job_id)
            kind = eta_prior.job_kind(job.goal)
            prior = eta_prior.prior_for(self._priors_cached(), kind)
            st = eta_structure.read(store, job_id, self.eta_runner, now=now, metrics=metrics, events=events,
                                    cache=self._eta_cache)
            if st.notes:  # the reader never raises: what it could not read is recorded, once per distinct message
                self._eta_error(job_id, "; ".join(st.notes))
                if prev is not None:
                    return prev
            plan_ahead = not any(f.get("kind") == "planning_serial" for f in found)
            base = eta.settle_prev(prev, st)
            labs, jev_state = self._jev_labels(job_id, st, metrics, prev, base)
            new = eta.estimate(metrics, st, prior, now, kind=kind, plan_ahead=plan_ahead, jev=labs,
                               jev_state=jev_state, prev=base)
            d = new.to_dict()
            overdue = False
            # The first ETA of a job is a change from `none`: one eta_changed records when it first had an answer.
            for what, evidence in eta.changes(prev if prev is not None else eta.Eta().to_dict(), d):
                store.append_event(job_id, what=what, evidence=evidence, rule=ETA_RULE, judgment=Judgment.code)
                overdue = overdue or what == "eta_overdue"
            if store.kv_get(job_id, ETA_ERROR_KEY) is not None and not st.notes:
                store.kv_delete(job_id, ETA_ERROR_KEY)
            if overdue and self.notify is not None and store.kv_get(job_id, ETA_NOTIFY_KEY) == "overdue":
                self.notify(job_id, "BUDGET", "Steward: overdue",
                            f"{job.goal[:60]}: {eta.render(d, now)['text']}"[:140])
            return d
        except Exception as exc:  # noqa: BLE001 - the ETA never breaks a tick or the efficiency state
            try:
                self._eta_error(job_id, f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001
                pass
            return prev

    def _review_boundary(self, job_id: str, metrics: dict, found: list[dict], m: str, now: float) -> None:
        """A unit whose complete flag flipped since the last compute is a boundary: at most one review for it."""
        rows = sorted((u for u in metrics.get("units") or [] if u.get("complete")),
                      key=lambda u: efficiency.unit_key(u["unit"]))
        complete = [efficiency.unit_label(u["unit"]) for u in rows]
        prev_raw = self.store.kv_get(job_id, UNITS_COMPLETE_KEY)
        prev = [efficiency.unit_label(x) for x in prev_raw or []]  # old kv held numbers; labels survive a JSON round trip
        new = [u for u in complete if u not in prev]
        if complete != prev_raw:
            self.store.kv_set(job_id, UNITS_COMPLETE_KEY, complete)
        if new:
            self.review.maybe_start(job_id, metrics, found, max(new, key=efficiency.unit_key), now, mode=m)

    # -- authority (16-05) ---------------------------------------------------

    def _on_action(self, job_id: str, finding: dict, kind: str, ref=None) -> None:
        """After a nudge/escalate/reconfigure: record the effect baseline (G13), unless one is still open for the
        finding. The effect is reported once, after the next comparable unit finishes (`_measure_effects`)."""
        store = self.store
        baselines = dict(store.kv_get(job_id, BASELINES_KEY) or {})
        cur = baselines.get(finding["id"])
        if isinstance(cur, dict) and not cur.get("closed"):
            return
        cfg = detect_module.load_config(store, job_id, self.config_dir)
        baselines[finding["id"]] = make_baseline(finding, kind, ref, self.clock(), self._metrics.get(job_id), cfg)
        store.kv_set(job_id, BASELINES_KEY, baselines)

    def _measure_effects(self, job_id: str, metrics: dict, cfg: dict, now: float) -> None:
        """Append exactly one efficiency_effect per open baseline once its next comparable unit finished. It is
        evidence for the owner: no case is recorded and no tier changes (labels stay human, Phase 15 A3)."""
        store = self.store
        baselines = store.kv_get(job_id, BASELINES_KEY)
        if not isinstance(baselines, dict):
            return
        changed = False
        for fid, bl in sorted(baselines.items()):
            eff = measure_effect(bl, metrics, cfg) if isinstance(bl, dict) else None
            if eff is None:
                continue
            store.append_event(job_id, what="efficiency_effect", evidence={**eff, "at": now}, rule=RULE,
                               judgment=Judgment.code)
            baselines[fid] = {**bl, "closed": True, "closed_at": now}
            changed = True
        if changed:
            store.kv_set(job_id, BASELINES_KEY, baselines)

    def _act(self, job_id: str, findings: list[dict], sig, newly_finished, now: float, *,
             worker_idle: bool = False, share: float | None = None) -> dict:
        """Decide, by code only, what each actionable finding may do: reconfigure (T2+), nudge (T2), escalate (T1, or
        escalate-only kinds at T1+). Returns {finding id: blocking reasons}, also kept in efficiency_state."""
        store = self.store
        cfg = detect_module.load_config(store, job_id, self.config_dir)
        spec = builtin_spec(cfg)
        ensure_record(store, job_id, spec)
        ctx = {
            "mode": mode(store, job_id, self.config_dir),
            "tier": wa.tier_of(store, job_id, spec),
            "powers": powers(store, job_id, spec),
            "observe_only": bool(store.kv_get(job_id, "efficiency_observe_only"))
            or float(self._share.get(job_id, 0.0) if share is None else share) > OBSERVE_SHARE,
            "boundary": worker_idle or boundary([], newly_finished, sig,
                                                fabric=store.kv_get(job_id, "worker_kind") == "fabric"),
            "steering": Steering(store),
        }
        blocked: dict[str, list[str]] = {}
        ctx["batch"] = []  # nudges that passed every gate this step: sent together as ONE steer below
        for f in findings:
            if not isinstance(f, dict) or not f.get("actionable") or "id" not in f:
                continue
            actions = tuple(f.get("actions") or ())
            reasons: list[str] = []
            if "reconfigure" in actions and f.get("policy") is not None:
                if self._reconfigure(job_id, f, ctx, cfg, now) == "refused":
                    # Outside the owner's envelope: no change and no nudge for it; it goes to the owner instead (T1+).
                    reasons.append("policy_refused")
                    if ctx["tier"] >= 1:
                        reasons += self._escalate(job_id, f, ctx, now, prefix="efficiency (needs you)")
                    blocked[f["id"]] = list(dict.fromkeys(reasons))
                    continue
            if "nudge" in actions:
                reasons += self._nudge(job_id, f, ctx, now)
                if ctx["tier"] == 1:  # T1 escalates what T2 would nudge
                    reasons += self._escalate(job_id, f, ctx, now)
            elif "escalate" in actions:
                reasons += self._escalate(job_id, f, ctx, now)
            if reasons:
                blocked[f["id"]] = list(dict.fromkeys(reasons))
        for fid, reasons in self._send_batch(job_id, ctx, now).items():
            blocked[fid] = reasons
        state = store.kv_get(job_id, STATE_KEY)
        state = state if isinstance(state, dict) else {}
        if state.get("blocked") != blocked:
            store.kv_set(job_id, STATE_KEY, {**state, "blocked": blocked})
        return blocked

    def _deferred(self, job_id: str, now: float) -> bool:
        """Liveness belongs to the doctor and Jev (G11): a suspected stall, or a recent diagnosis/Jev signal."""
        if self.store.kv_get(job_id, "last_class") == "suspect_stall":
            return True
        return any(e.what in DEFER_EVENTS and 0 <= now - detect_module._epoch(e.ts) <= DEFER_S
                   for e in self.store.events(job_id))

    def _escalate(self, job_id: str, f: dict, ctx: dict, now: float, prefix: str = "efficiency") -> list[str]:
        from steward import approvals

        store = self.store
        fid = f["id"]
        question = f"{prefix}: " + format_finding(f).removeprefix("⚠ ")
        value = float(f.get("saving_usd") or 0.0) or float(f.get("saving_min") or 0.0)
        r = []
        if ctx["mode"] != "active":
            r.append(f"mode_{ctx['mode']}")
        if "escalate" not in ctx["powers"]:
            r.append(f"tier_T{ctx['tier']}")
        if ctx["observe_only"]:
            r.append("observe_only")
        if wa.quarantine(store, job_id):
            r.append("quarantined")
        if f.get("kind") == "idle_wait" and self._deferred(job_id, now):
            r.append("deferred_to_doctor")
        prev = store.kv_get(job_id, f"efficiency_escalated:{fid}")
        if (prev is not None and value < 2 * float(prev)) or any(
                a["question"] == question for a in approvals.list_approvals(store, job_id, status="pending")):
            r.append("duplicate")
        if r:
            return r
        record = approvals.open_approval(store, job_id, question)
        store.kv_set(job_id, f"efficiency_escalated:{fid}", value)
        if self.notify is not None:
            self.notify(job_id, "NEEDS_YOU", "Steward: efficiency", question[:140])
        store.append_event(job_id, what="efficiency_escalated",
                           evidence={"finding": fid, "approval_id": record["id"], "at": now},
                           rule=GATE_RULE, judgment=Judgment.code)
        self._on_action(job_id, f, "escalate", ref=record["id"])
        return []

    def _nudge_gates(self, job_id: str, f: dict, ctx: dict, now: float) -> list[str]:
        from steward import custom_watchers as cw

        store = self.store
        r = []
        if ctx["mode"] != "active":
            r.append(f"mode_{ctx['mode']}")
        if "nudge" not in ctx["powers"]:
            r.append(f"tier_T{ctx['tier']}")
        if ctx["observe_only"]:
            r.append("observe_only")
        if wa.quarantine(store, job_id):
            r.append("quarantined")
        if now < float(store.kv_get(job_id, "watcher_freeze_until") or 0):
            r.append("freeze")
        if now < float(store.kv_get(job_id, "watcher_settle_until") or 0):
            r.append("settle_lease")
        if f.get("epoch") != store.get_revision(job_id):
            r.append("stale_epoch")
        if cw._unacked_pending(ctx["steering"], job_id):
            r.append("unacked_pending")
        if not ctx["boundary"]:
            r.append("not_boundary")
        if store.kv_get(job_id, f"efficiency_nudged:{f['id']}") is not None:
            r.append("cooldown_finding")
        last = store.kv_get(job_id, "efficiency_last_nudge")
        if last is not None and now - float(last) < JOB_COOLDOWN_S:
            r.append("cooldown_job")
        return r

    def _policy_line(self, job_id: str) -> str:
        from steward import efficiency_policy

        return efficiency_policy.policy_line(efficiency_policy.load(self.store, job_id))

    def _reconfigure(self, job_id: str, f: dict, ctx: dict, cfg: dict, now: float) -> str | None:
        """T2+ and mode active: write the finding's policy patch (future spawns only), once per finding.
        Returns "changed", "refused" (now or before) or None (not allowed now / already done)."""
        from steward import efficiency_policy

        store = self.store
        fid = f["id"]
        if ctx["mode"] != "active" or "reconfigure" not in ctx["powers"] or ctx["observe_only"] \
                or wa.quarantine(store, job_id):
            return None
        if store.kv_get(job_id, f"efficiency_reconfigure_refused:{fid}"):
            return "refused"
        if any(e.get("finding") == fid for e in store.kv_get(job_id, efficiency_policy.LOG_KEY) or []):
            return None
        try:
            entry = efficiency_policy.change(store, job_id, f["policy"], actor=ORIGIN, finding=fid, now=now,
                                             envelope=cfg["envelope"])
        except efficiency_policy.PolicyError:
            store.kv_set(job_id, f"efficiency_reconfigure_refused:{fid}", now)
            return "refused"
        self._on_action(job_id, f, "reconfigure", ref=entry["id"])
        return "changed"

    def _nudge(self, job_id: str, f: dict, ctx: dict, now: float) -> list[str]:
        reasons = self._nudge_gates(job_id, f, ctx, now)
        if not reasons:
            ctx["batch"].append(f)  # sent with the step's other passing findings as one steer (`_send_batch`)
            return []
        self._would_nudge(job_id, f, reasons, now)
        return reasons  # transient blocks are retried silently on later steps

    def _would_nudge(self, job_id: str, f: dict, reasons: list[str], now: float) -> None:
        if self._mark_seen(job_id, f["id"]):  # the T0 would-have-nudged record, once per finding
            self.store.append_event(job_id, what="efficiency_would_nudge", evidence={
                "finding": f["id"], "text": nudge_text(f, self._policy_line(job_id)), "blocked": reasons, "at": now},
                rule=GATE_RULE, judgment=Judgment.code)

    def _send_batch(self, job_id: str, ctx: dict, now: float) -> dict[str, list[str]]:
        """Latency #6: every finding that passed the gates this step goes out as ONE steer (then the 5-min job
        cooldown holds the next). Returns {finding id: reasons} for any that could not be sent."""
        batch = ctx.get("batch") or []
        if not batch:
            return {}
        store = self.store
        policy = self._policy_line(job_id)
        texts = [nudge_text(f, policy) for f in batch]
        text = texts[0] if len(texts) == 1 else "\n".join(
            [f"{len(texts)} efficiency findings:"] + [f"{i}. {t}" for i, t in enumerate(texts, 1)])
        receipt = ctx["steering"].record(job_id, "nudge", text, origin=ORIGIN, human_instruction="",
                                         interpreted_instruction=text)
        if not receipt.accepted:
            for f in batch:
                self._would_nudge(job_id, f, ["rejected"], now)
            return {f["id"]: ["rejected"] for f in batch}
        rev = ctx["steering"].all_instructions(job_id)[-1]["rev"]
        store.kv_set(job_id, "efficiency_last_nudge", now)
        for f, t in zip(batch, texts):
            store.kv_set(job_id, f"efficiency_nudged:{f['id']}", now)
            self._mark_seen(job_id, f["id"])
            store.append_event(job_id, what="efficiency_nudged", evidence={
                "finding": f["id"], "rev": rev, "text": t, "saving_usd": f.get("saving_usd"),
                "saving_min": f.get("saving_min"), "at": now, "coalesced": len(batch)},
                rule=GATE_RULE, judgment=Judgment.code)
            self._on_action(job_id, f, "nudge", ref=rev)
        return {}

    def _mark_seen(self, job_id: str, fid: str) -> bool:
        """True when this finding had no would-nudge/nudge record yet (and marks it)."""
        seen = self.store.kv_get(job_id, SEEN_KEY) or []
        if fid in seen:
            return False
        self.store.kv_set(job_id, SEEN_KEY, [*seen, fid])
        return True


# -- `steward efficiency JOB` (read-only) --------------------------------------------------------------------


def report(store: Store, job_id: str, *, projects_dir=None, config_dir=None, now: float | None = None) -> dict:
    """Fresh metrics and findings for one job. Read-only: no event, no kv write."""
    now = time.time() if now is None else now
    cfg = detect_module.load_config(store, job_id, config_dir)
    m = mode(store, job_id, config_dir)
    metrics = efficiency.compute(store, job_id, now=now, projects_dir=projects_dir, config=cfg)
    job = store.get_job(job_id)
    budget = cfg.get("job_budget_usd") or None
    findings = [_plain(f) for f in detect_module.detect(
        metrics, cfg, events=store.events(job_id), now=now, job_state=job.state.value, budget_usd=budget)]
    state = store.kv_get(job_id, STATE_KEY)
    baselines = store.kv_get(job_id, BASELINES_KEY)
    effects = [e.evidence for e in store.events(job_id) if e.what == "efficiency_effect"]
    return {"mode": m, "metrics": metrics, "findings": findings, "state": state if isinstance(state, dict) else {},
            "baselines": baselines if isinstance(baselines, dict) else {}, "effects": effects}


def _dur(s: float) -> str:
    s = int(s)
    return f"{s}s" if s < 60 else f"{s // 60}m" if s < 3600 else f"{s / 3600:.1f}h"


def render_table(rep: dict) -> str:
    m, state = rep["metrics"], rep["state"]
    tot = m["totals"]
    lines = [f"efficiency [{rep['mode']}]  " + " · ".join(row_parts({"summary": {
        "usd": tot["usd"], "sub_share": tot["sub_share"], "calls": tot["calls"], "peak_ctx": tot["peak_ctx"],
        "think_share": tot["think_share"], "tok_s": tot["tok_s"], "crit": (m.get("time") or {}).get("crit") or ""}}))]
    lines.append("agents:")
    lines.append(f"  {'key':<26}{'stage':<18}{'unit':>6}{'calls':>7}{'ctx mean':>10}{'ctx peak':>10}{'usd':>9}"
                 f"{'wall':>7}  state")
    for a in m["agents"]:
        unit = efficiency.unit_label(a["unit"])
        lines.append(f"  {str(a['key'])[:25]:<26}{str(a['stage'])[:17]:<18}{unit:>6}{a['calls']:>7}"
                     f"{_k(a['ctx_mean']):>10}{_k(a['ctx_peak']):>10}{a['usd']:>9.2f}"
                     f"{_dur(max(a['end'] - a['start'], 0)):>7}  {'done' if a['finished'] else 'running'}")
    lines.append("stages:")
    for name, s in m["stages"].items():
        lines.append(f"  {name[:24]:<25}{s['kind']:<8}{s['agents']} agents  {s['calls']} calls  ${s['usd']:.2f}  "
                     f"think≈{s['think_share']:.0%}  {s['tok_s']:.0f} tok/s  effort {s['effort'] or '-'}  "
                     f"cache-miss {s['cache_miss']:.0%}")
    done = sum(1 for u in m["units"] if u["complete"])
    proj = m.get("projection")
    unit_line = f"units: {done}/{len(m['units'])} complete"
    if proj:
        unit_line += f" of {proj['units_total']} planned"
        if proj.get("projected_usd") is not None:
            unit_line += f"; projected ${proj['projected_usd']:.2f} / {proj['projected_min']:.0f} min more"
    lines.append(unit_line)
    sup = m["supervision"]
    by = " ".join(f"{k}=${v:.2f}" for k, v in sorted(sup["by_source"].items())) or "none"
    lines.append(f"supervision: ${sup['usd']:.2f} ({sup['share_of_worker']:.0%} of worker)  {by}")
    first = state.get("first_seen") or {}
    lines.append(f"findings ({len(rep['findings'])}):" + ("" if rep["findings"] else " none"))
    for f in rep["findings"]:
        seen = f" [first seen {first[f['id']]:.0f}]" if f["id"] in first else ""
        lines.append("  " + format_finding(f) + seen)
    lines.append(f"effects ({len(rep.get('effects') or [])}):" + ("" if rep.get("effects") else " none"))
    for e in rep.get("effects") or []:
        lines.append(f"  {e.get('finding')} {e.get('metric')}: {e.get('before')} → {e.get('after')} "
                     f"(saved ${float(e.get('saved_usd') or 0):.2f} / {float(e.get('saved_min') or 0):.1f} min; "
                     f"{e.get('action')} {e.get('ref')})")
    open_bl = [b for b in (rep.get("baselines") or {}).values() if isinstance(b, dict) and not b.get("closed")]
    if open_bl:
        lines.append(f"baselines awaiting the next unit ({len(open_bl)}):")
        for b in open_bl:
            lines.append(f"  {b.get('finding')} {b.get('metric')} before {b.get('before')} ({b.get('action')} {b.get('ref')})")
    return "\n".join(lines)
