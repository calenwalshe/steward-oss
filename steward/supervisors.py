"""Who is watching a job, and how busy they are (dashboard tree rows).

`supervisor_rows` is a read-only snapshot: the worker, Astra (trajectory
advisor), Sol (stall diagnoser) and Jev (observer), each with its model, a
status word, a stats dict and a one-line display string. It reuses
`trajectory.status_snapshot` (the same data `steward trajectory status --json`
prints) and `roles.load_roles`. Supervisors gain no authority from being
listed here.
"""

from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime

from steward import approvals as approvals_module
from steward import trajectory as trajectory_module
from steward.roles import load_roles
from steward.store import Store

_PERMISSION_EVENT = "worker permission requested"
_DIAGNOSIS_EVENT = "stall diagnosis produced"


SWITCH_KEYS = {"sol": "supervisor_sol", "jev": "supervisor_jev", "resolver": "supervisor_resolver",
               "efficiency": "supervisor_efficiency"}
# jev and efficiency are three-state observers like astra; "on" is the legacy alias for "shadow".
ROLE_STATES = {"astra": ("off", "shadow", "active"), "sol": ("on", "off"), "resolver": ("on", "off"),
               "jev": ("off", "shadow", "active", "on"), "efficiency": ("off", "shadow", "active", "on")}
ASTRA_CYCLE = ("off", "shadow", "active")
JEV_MODES = ("off", "shadow", "active")


def switch_flag(store: Store, job_id: str, role: str) -> str | None:
    """The per-job flag for sol ("on"/"off") or jev ("on"/"off"/"shadow"/"active"), or None when never set."""
    v = store.kv_get(job_id, SWITCH_KEYS[role])
    allowed = ("on", "off", "shadow", "active") if role in ("jev", "efficiency") else ("on", "off")
    return v if v in allowed else None


def global_jev_mode(config_dir=None) -> str:
    """`[jev] mode` in steward.toml (off|shadow|active), else `shadow` — Jev is free and
    always-on, so the default is shadow. `STEWARD_JEV=0` forces the default off."""
    import os
    import tomllib

    from steward.store import resolve_config_dir

    mode = "shadow"
    try:
        data = tomllib.loads((resolve_config_dir(config_dir) / "steward.toml").read_text())
        v = str((data.get("jev") or {}).get("mode", mode)).lower()
        mode = {"on": "shadow"}.get(v, v) if v in ("off", "shadow", "active", "on") else mode
    except (OSError, ValueError, AttributeError):
        pass
    if os.environ.get("STEWARD_JEV") == "0":
        mode = "off"
    return mode


def jev_mode(store: Store, job_id: str, config_dir=None) -> str:
    """The effective observer mode for a job: per-job switch, else the global default."""
    flag = switch_flag(store, job_id, "jev")
    if flag is None:
        return global_jev_mode(config_dir)
    return "shadow" if flag == "on" else flag


def sol_enabled(store: Store, job_id: str) -> bool:
    return switch_flag(store, job_id, "sol") != "off"  # default on


def next_state(role: str, current: str) -> str:
    """What `toggle` does: astra, jev and efficiency cycle off -> shadow -> active -> off; sol flips."""
    if role in ("jev", "efficiency") and current == "on":
        current = "shadow"
    if role in ("astra", "jev", "efficiency"):
        return ASTRA_CYCLE[(ASTRA_CYCLE.index(current) + 1) % 3] if current in ASTRA_CYCLE else "off"
    return "off" if current == "on" else "on"


def ago(secs: float) -> str:
    secs = max(0, int(secs))
    return f"{secs}s" if secs < 60 else f"{secs // 60}m" if secs < 3600 else f"{secs // 3600}h{secs % 3600 // 60:02d}m"


def _dur(secs: float) -> str:
    secs = int(secs)
    return f"{secs}s" if secs < 60 else f"{round(secs / 60)}m" if secs < 3600 else f"{secs / 3600:.1f}h".replace(".0h", "h")


def model_from_args(args: list[str]) -> str | None:
    """The value after `--model` (or `--model=X`) in an argv-ish list."""
    for i, a in enumerate(args):
        if a == "--model" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--model="):
            return a.split("=", 1)[1]
    return None


def _worker_model_from_ps(job_id: str) -> str | None:
    """Fallback for jobs started before the shim recorded `worker_model`:
    the live shim/runner command line carries `--agent-arg=--model
    --agent-arg=<m>`."""
    try:
        out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if job_id in line and "steward.cli" in line and "--agent-arg=--model" in line:
            m = re.search(r"--agent-arg=--model\s+--agent-arg=(\S+)", line)
            if m:
                return m.group(1)
    return None


def worker_model(store: Store, job_id: str) -> str:
    return store.kv_get(job_id, "worker_model") or _worker_model_from_ps(job_id) or "?"


def _age(ts: str, now: float) -> float:
    return now - datetime.fromisoformat(ts).timestamp()


def _worker_active_age(store: Store, job_id: str, now: float) -> float | None:
    """Seconds since the worker's transcript last grew (any tool call, result or reply), else None."""
    try:
        from steward.transcript import worker_file
        f = worker_file(store, job_id)
        return max(0.0, now - f.stat().st_mtime) if f is not None else None
    except Exception:  # noqa: BLE001 - a missing transcript only drops the field
        return None


def supervisor_rows(store: Store, job_id: str, *, now: float | None = None, config_dir=None, trajectory_config=None) -> list[dict]:
    now = time.time() if now is None else now
    roles = load_roles(config_dir)
    # only what the rows count (TUI speed plan 12b): requests, single auto-allow events, diagnoses
    events = store.events_of(job_id, (_PERMISSION_EVENT, *approvals_module.AUTO_ALLOWED_WHATS, _DIAGNOSIS_EVENT))
    cfg = trajectory_module.load_trajectory_config(trajectory_config)
    snap = trajectory_module.status_snapshot(store, job_id, cfg, now)

    # worker
    perms = [e for e in events if e.what == _PERMISSION_EVENT or approvals_module.is_single_permission_event(e)]
    actions = len(perms)
    last_s = _age(perms[-1].ts, now) if perms else None
    spend = float(snap.get("worker_spend_usd") or 0.0)
    wmodel = worker_model(store, job_id)
    active_s = _worker_active_age(store, job_id, now)
    wparts = ([f"active {ago(active_s)} ago"] if active_s is not None else []) + [f"{actions} approvals"]
    text = "{SPIN} " + " · ".join(wparts)
    worker = {"role": "worker", "model": wmodel, "status": "working",
              "stats": {"actions": actions, "last_action_ago_s": last_s, "last_active_ago_s": active_s,
                        "spend_usd": spend},
              "parts": wparts, "display": text}

    # astra
    mode = snap["mode"]
    reviews = snap["reviews"]
    cadence = cfg.triggers.cadence_s
    nxt = snap["next_review_due_in_s"]
    stats = {"mode": mode, "cadence_s": cadence, "reviews": len(reviews), "next_due_in_s": nxt,
             "supervision_spend_usd": float(snap.get("supervision_spend_usd") or 0.0),
             "last_review_ts": None, "last_verdict": None}
    parts = [f"~{_dur(cadence)}", f"{len(reviews)} rev"]
    if reviews:
        last = reviews[-1]
        verdict = (last["verdict"] or {}).get("trajectory") or last.get("decision") or "n/a"
        stats["last_review_ts"], stats["last_verdict"] = last["ts"], verdict
        try:
            ts = last["ts"]  # epoch float from the trajectory snapshot, or an ISO string
            epoch = float(ts) if isinstance(ts, (int, float)) else datetime.fromisoformat(ts).timestamp()
            hhmm = datetime.fromtimestamp(epoch).strftime("%H:%M")
        except (TypeError, ValueError):
            hhmm = "?"
        parts.append(f"last {hhmm} {verdict}")
    if mode != "off" and nxt is not None:
        parts.append("next " + datetime.fromtimestamp(now + nxt).strftime("%H:%M"))
    if stats["supervision_spend_usd"] > 0:
        parts.append(f"${stats['supervision_spend_usd']:.2f}")
    astra = {"role": "astra", "label": "advisor", "model": roles.astra, "status": mode, "stats": stats,
             "switch": f"{mode}▸", "next": next_state("astra", mode), "parts": parts,
             "display": f"[{mode}▸] " + " · ".join(parts)}

    # sol
    diagnoses = sum(1 for e in events if e.what == _DIAGNOSIS_EVENT)
    sol_on = sol_enabled(store, job_id)
    sol = {"role": "sol", "label": "doctor", "model": roles.sol, "status": "standby" if sol_on else "off",
           "stats": {"diagnoses": diagnoses}, "switch": "on" if sol_on else "off",
           "next": "off" if sol_on else "on", "parts": (["standby"] if sol_on else []) + [f"{diagnoses} diagnoses"],
           "display": ("[on] standby" if sol_on else "[off]") + f" · {diagnoses} diagnoses"}

    # jev: per-job mode wins, else steward.toml [jev] mode, else shadow
    jmode = jev_mode(store, job_id)
    if jmode != "off":
        from steward import jev_observer

        st = jev_observer.get_stats(store, job_id)
        flags = sum(st["flags"].values())
        parts = ["10s", f"{st['ok']} checks", f"{flags} flags"]
        note = ""
        status = store.kv_get(job_id, jev_observer.STATUS_KEY) or ""
        if status.startswith("unavailable:"):
            code = status.split(":", 1)[1]
            parts.append({"no_api_key": "no api key"}.get(code, code))
            from steward import jev_watch

            outage = jev_watch.outage_status(store)  # SUP-08: never a silent degrade
            if outage and code != "disabled":
                parts.append(f"unavailable since {datetime.fromtimestamp(float(outage['since'])).strftime('%H:%M')}")
        elif st["last_ok_ts"]:
            note = " ".join(status.split(":", 1)[1].split()) if status.startswith("ok:") else ""
            parts.append(f"last ok {datetime.fromtimestamp(st['last_ok_ts']).strftime('%H:%M')}")
            if note and note != "ok":
                parts.append(note)
        jev = {"role": "jev", "label": "observer", "model": roles.jev, "status": jmode,
               "stats": {"mode": jmode, "checks": st["ok"], "flags": flags, "flags_by_choice": st["flags"],
                         "unavailable": st["unavailable"], "last_ok_ts": st["last_ok_ts"], "last_ts": st["last_ts"]},
               "switch": f"{jmode}▸", "next": next_state("jev", jmode), "parts": parts, "display": f"[{jmode}▸] " + " · ".join(parts)}
    else:
        jev = {"role": "jev", "label": "observer", "model": roles.jev, "status": "off", "stats": {}, "switch": "off",
               "next": next_state("jev", "off"), "parts": [], "display": "[off]"}
    return [worker, astra, sol, *_research_rows(store, job_id), *_watcher_rows(store, job_id, now),
            *_efficiency_rows(store, job_id), jev,
            *_resolver_rows(store, job_id)]


def _efficiency_rows(store: Store, job_id: str) -> list[dict]:
    """The built-in `efficiency` row (code only, on by default): `$86 · sub 96% · 5.2k calls · peak ctx 602k · ...`."""
    from steward import efficiency_watch as ew

    emode = ew.mode(store, job_id)
    if emode == "off":
        return [{"role": "efficiency", "label": "efficiency", "model": "code", "status": "off", "stats": {},
                 "switch": "off", "next": next_state("efficiency", "off"), "parts": [], "display": "[off]"}]
    state = store.kv_get(job_id, ew.STATE_KEY)
    state = state if isinstance(state, dict) else {}
    if store.kv_get(job_id, "worker_kind") == "codex":
        state = {**state, "codex": True}
    parts = ew.row_parts(state)
    stats = {"mode": emode, "computed_at": state.get("computed_at"), "active": len(state.get("active") or []),
             **(state.get("summary") or {})}
    return [{"role": "efficiency", "label": "efficiency", "model": "code", "status": emode, "stats": stats,
             "switch": f"{emode}▸", "next": next_state("efficiency", emode), "parts": parts,
             "display": f"[{emode}▸] " + " · ".join(parts)}]


def _resolver_rows(store: Store, job_id: str) -> list[dict]:
    """The `resolver` row: `auto  3 resolved (2 approve, 1 escalate) · last: approve <reason>`. Shown when the
    resolver is enabled (steward.toml / env) or has ever decided for this job."""
    from steward import resolver

    st = resolver.stats(store, job_id)
    on = resolver.job_switch(store, job_id) == "on"
    if not (resolver.load_config().enabled or st["resolved"] or store.kv_get(job_id, resolver.SWITCH_KEY)):
        return []
    if not on:
        return [{"role": "resolver", "label": "resolver", "model": "auto", "status": "off", "stats": st, "switch": "off",
                 "next": "on", "parts": [], "display": "[off]"}]
    if st["resolved"]:
        counts = ", ".join(f"{st[k]} {k}" for k in ("approve", "deny", "escalate") if st[k])
        last = st["last"] or {}
        applied = {"escalate_human": "escalate"}.get(last.get("applied"), last.get("applied"))
        parts = [f"{st['resolved']} resolved ({counts})", f"last: {applied} {' '.join(str(last.get('reason', '')).split())[:60]}"]
    else:
        parts = ["standby"]
    return [{"role": "resolver", "label": "resolver", "model": "auto", "status": "on", "stats": st, "switch": "on",
             "next": "off", "parts": parts, "display": "[on] " + " · ".join(parts)}]


def _research_rows(store: Store, job_id: str) -> list[dict]:
    """A `research` row for a research-enabled job: calls and $ spent vs the cap.
    No `next`, so it is never a toggle target."""
    from steward import research

    s = research.summary(store, job_id)
    if s is None:
        return []
    status = "capped" if s["tripped"] else "on"
    parts = [f"{s['calls']} calls", f"${s['spent_usd']:.2f}/${s['cap_usd']:.2f}"] + (["CAP REACHED"] if s["tripped"] else [])
    return [{"role": "research", "kind": "research", "source": "research", "model": "mcp", "status": status,
             "stats": s, "switch": status, "parts": parts,
             "display": " · ".join(parts)}]


def _watcher_rows(store: Store, job_id: str, now: float) -> list[dict]:
    from steward import custom_watchers as cw

    rows = []
    for w in cw.list_watchers(store, job_id):
        name, on = w["name"], bool(w.get("enabled", True))
        runs = cw.run_count(store, job_id, name)
        parts = [f"every {_dur(w['every_s'])}", f"{runs} runs"]
        ev = cw.last_verdict(store, job_id, name)
        status = "standby" if on else "off"
        note = ""
        if ev is not None:
            hhmm = datetime.fromisoformat(ev.ts).astimezone().strftime("%H:%M")
            note = " ".join(str(ev.evidence.get("note", "")).split())
            parts.append(f"last {hhmm} {ev.evidence.get('status')}")
        if on:
            parts.append("next " + datetime.fromtimestamp(cw.next_due(store, job_id, w, now)).strftime("%H:%M"))
        if ev is not None and note:
            parts.append(note)  # least important: dropped first when the row is narrow
        rows.append({
            "role": name, "kind": "watcher", "source": f"watcher:{name}", "model": w["model"], "status": status,
            "stats": {"every_s": w["every_s"], "runs": runs, "may": w.get("may") or [],
                      "last_status": ev.evidence.get("status") if ev is not None else None},
            "switch": "on" if on else "off", "next": "off" if on else "on", "parts": parts,
            "display": ("[on] " if on else "[off] ") + " · ".join(parts),
        })
    return rows
