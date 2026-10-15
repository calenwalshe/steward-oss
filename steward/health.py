"""System health: is the machinery Steward depends on actually alive?

`check_all` is a one-shot, read-only survey (`steward health [--json]`):

  herdr    the Herdr API socket answers `pane.list`
  control  a `steward control` pane exists AND answers a no-op `ping` relay (the same path every write verb takes)
           AND the code it loaded (the ping's `head`) is the HEAD now deployed in the tree it runs from (`root`)
  advisor  a `steward advisor` pane exists (or trajectory mode is off)
  runners  every RUNNING job's runner process (pid recorded at its first tick) and worker pane are alive
  mcp      the steward-mcp service and its tunnels are active

`tick` is what the dashboard refresh calls (at most every `TTL_S`; NOT a daemon): it persists per-component
`red_since` in `$STEWARD_HOME/health.json` and alerts about anything red for longer than `RED_ALERT_S` through the
existing alerts gating (`alerts.system_alert`: nothing is sent unless `[alerts] telegram = true`). Healing is the
watchdog's alone (issue W, `steward watchdog`): the dashboard passes no `heal_fn` and skips alerts for the components
the watchdog owns (`WATCHDOG_OWNED`). `heal_fn` stays for callers that still want the one-shot heal.
Everything is injectable so tests never touch Herdr, systemd, or the network.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from steward import alerts, control_pane, deploy_pending
from steward.advisor_pane import find_advisor_pane
from steward.herdr_transport import HerdrApiError, HerdrUnavailable
from steward.models import JobState
from steward.store import Store

STATE_FILE = "health.json"
TTL_S = 30.0
RED_ALERT_S = 120.0
PING_TIMEOUT_S = 3.0
MAX_ALERT_TRIES = 3
UNITS = ("steward-mcp", "steward-tunnel", "steward-mcp-tunnel-local")
COMPONENTS = ("control", "advisor", "herdr", "runners", "mcp", "budget")
# Reported only once their loop has a heartbeat: `liveness` appears after the control tick's first sweep
# (spec §5.1). Before that there is nothing to judge, so the row is absent rather than a dim "unknown".
SWEEP_COMPONENTS = ("liveness",)
ALL_COMPONENTS = COMPONENTS + SWEEP_COMPONENTS
# Healed and escalated by `steward watchdog` (issue W): the dashboard only displays them, never heals or alerts.
WATCHDOG_OWNED = ("control", "runners", "herdr")
_CONTROL_KV_JOB = "_control"


@dataclass
class Component:
    name: str
    ok: bool | None  # None = could not be determined (dim dot, never alerts)
    detail: str = ""
    problems: list[str] = field(default_factory=list)


def default_socket() -> str:
    return os.environ.get("HERDR_SOCKET_PATH") or str(Path.home() / ".config" / "herdr" / "herdr.sock")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _unit_active(unit: str) -> bool:
    import subprocess

    try:
        p = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.stdout.strip() == "active"


def _ping(store: Store, api: Any, *, timeout_s: float, sleep: Callable[[float], None], clock: Callable[[], float]) -> str | None:
    """None when the control pane answered the no-op `ping`, else why not."""
    return _ping_receipt(store, api, timeout_s=timeout_s, sleep=sleep, clock=clock)[0]


def _ping_receipt(store: Store, api: Any, *, timeout_s: float, sleep: Callable[[float], None],
                  clock: Callable[[], float]) -> tuple[str | None, dict | None]:
    """(None, pong) when the control pane answered the no-op `ping`, else (why not, None). The receipt is deleted
    afterwards (a ping every 30s must not pile receipts up in the store)."""
    rid = f"health-ping-{uuid.uuid4().hex[:12]}"
    try:
        receipt = control_pane.relay(store, api, "ping", {}, origin="health", request_id=rid,
                                     timeout_s=timeout_s, sleep=sleep, clock=clock)
    except control_pane.NoControlPane as exc:
        return str(exc), None
    except control_pane.ControlTimeout:
        return "ping timed out: pane exists but is not answering", None
    finally:
        store.kv_delete(_CONTROL_KV_JOB, f"{control_pane._CONTROL_KV_PREFIX}{rid}")
    return (None, receipt) if receipt.get("accepted") else (f"ping refused: {receipt.get('detail')}", None)


def stale_code(pong: dict | None, head_fn: Callable[[str], str] | None = None) -> str | None:
    """Why a process that answered `pong` (the control `ping` receipt: `head`, `root`) runs code other than the HEAD
    deployed in the tree it imported from, else None. Unknown either side (an old pong, git failing) is not stale:
    only a proven mismatch is. Job f257aa7b: the control ran 6e76e71 for 49 min after 233d178 landed in its tree
    (runners had already re-exec'd onto it) and health stayed green."""
    from steward import stability

    loaded = str((pong or {}).get("head") or "")
    if not loaded:
        return None
    root = (pong or {}).get("root") or str(stability.REPO_ROOT)
    deployed = (head_fn or stability.repo_head)(root)
    if not deployed or deployed == loaded:
        return None
    return (f"stale code: loaded {loaded[:12]}, deployed HEAD {deployed[:12]} in {root}; "
            f"run `steward deploy-self`")


INDEX_LOCK_YELLOW_S = 60.0
INDEX_LOCK_RED_S = 600.0
SWEEP_RED_S = 300.0


def lock_metrics(store: Store, *, now: float | None = None) -> dict:
    """Merge-pipe vitals: oldest stranded `.git/index.lock` among live jobs' repos and the delivery sweep's age.
    `level` is ok/yellow/red; a stale lock that healing refused, or a stopped sweep, shows here."""
    from steward import incorporate

    now = time.time() if now is None else now
    ages: dict[str, float] = {}
    repos = {j.repo for j in store.list_jobs({JobState.ACCEPTED, JobState.RUNNING, JobState.NEEDS_HUMAN}) if j.repo}
    for repo in sorted(repos):
        try:
            lock = incorporate._index_lock(repo)
            if lock is not None:
                ages[repo] = max(0.0, now - lock.stat().st_mtime)
        except OSError:
            continue
    lock_age = max(ages.values()) if ages else 0.0
    at = store.kv_get("_delivery", "delivery_sweep_at")
    sweep_age = max(0.0, now - float(at)) if at else None
    level = "red" if lock_age > INDEX_LOCK_RED_S or (sweep_age or 0) > SWEEP_RED_S else (
        "yellow" if lock_age > INDEX_LOCK_YELLOW_S else "ok")
    return {"index_lock_age_s": round(lock_age, 1), "delivery_sweep_age_s": None if sweep_age is None else round(sweep_age, 1),
            "level": level}


def check_all(
    store: Store, *, api: Any | None = None, ping: bool = True, ping_timeout_s: float = PING_TIMEOUT_S,
    pid_alive: Callable[[int], bool] = _pid_alive, unit_active: Callable[[str], bool] = _unit_active,
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
    trajectory_on: bool | None = None, head_fn: Callable[[str], str] | None = None,
    extras: bool = False,
) -> list[Component]:
    from steward.herdr_transport import HerdrApi

    api = api or HerdrApi(default_socket())
    out: dict[str, Component] = {}

    panes: list[dict] | None
    try:
        panes = list(api.call("pane.list", {}).get("panes", []))
        out["herdr"] = Component("herdr", True, "Herdr socket answers")
    except (HerdrUnavailable, HerdrApiError, OSError) as exc:
        panes = None
        out["herdr"] = Component("herdr", False, "Herdr socket unreachable", [f"herdr: {str(exc)[:120]}"])

    def by_role(token: str, value: str) -> dict | None:
        return next((p for p in panes or [] if (p.get("tokens") or {}).get(token) == value), None)

    if panes is None:
        for name in ("control", "advisor", "runners"):
            out[name] = Component(name, None, "unknown (Herdr unreachable)")
    else:
        ctl = by_role(control_pane.CONTROL_ROLE_TOKEN, control_pane.CONTROL_ROLE_VALUE)
        if ctl is None:
            out["control"] = Component("control", False, "no control pane", ["control: no control pane is running"])
        elif ping:
            why, pong = _ping_receipt(store, api, timeout_s=ping_timeout_s, sleep=sleep, clock=clock)
            stale = None if why else stale_code(pong, head_fn)
            waiting = deploy_pending.pending(store, now=time.time()) if stale else None
            if waiting:  # the intended window: a self-merge awaiting its deploy; runners wait too (no skew)
                out["control"] = Component("control", True, f"awaiting deploy-self of a Steward self-merge "
                                           f"(job {str(waiting.get('job_id'))[:8]}): {stale}")
            else:
                why = why or stale
                out["control"] = (Component("control", True, f"answers ping ({ctl.get('pane_id')}), "
                                            f"code {str((pong or {}).get('head') or '?')[:12]}") if why is None
                                  else Component("control", False, why, [f"control: {why}"]))
        else:
            out["control"] = Component("control", True, f"pane present ({ctl.get('pane_id')}); not pinged")

        if trajectory_on is None:
            try:
                from steward import trajectory

                trajectory_on = trajectory.effective_mode(store, "_health", trajectory.load_trajectory_config()).mode != "off"
            except Exception:  # noqa: BLE001
                trajectory_on = True
        adv = by_role("steward_role", "advisor")
        if not trajectory_on:
            out["advisor"] = Component("advisor", True, "off (trajectory mode off)")
        elif adv is None:
            out["advisor"] = Component("advisor", False, "no advisor pane", ["advisor: no advisor pane is running"])
        else:
            out["advisor"] = Component("advisor", True, f"pane present ({adv.get('pane_id')})")

        ids = {p.get("pane_id") for p in panes}
        labels = {p.get("label"): p.get("pane_id") for p in panes if p.get("label")}
        problems: list[str] = []
        jobs = store.list_jobs({JobState.RUNNING})
        for job in jobs:
            j8 = job.job_id[:8]
            code = store.kv_get(job.job_id, "runner_code")
            pid = code.get("pid") if isinstance(code, dict) else None
            rpane = store.kv_get(job.job_id, "runner_pane")
            if pid is not None and not pid_alive(pid):
                problems.append(f"runner {j8}: runner process {pid} is gone")
            elif pid is None and rpane and rpane not in ids and f"runner {j8}" not in labels:
                problems.append(f"runner {j8}: runner pane {rpane} is gone")
            wpane = store.kv_get(job.job_id, "herdr_pane")
            if wpane and wpane not in ids:
                problems.append(f"runner {j8}: worker pane {wpane} is gone")
        out["runners"] = (Component("runners", False, f"{len(problems)} problem(s) across {len(jobs)} running job(s)", problems)
                          if problems else Component("runners", True, f"{len(jobs)} running job(s) alive"))

    out["mcp"] = _mcp_component(store, panes, unit_active)
    try:  # the Claude budget is advisory: never ok=False, so health is never red for it
        from steward import governor

        b = governor.budget()
        out["budget"] = Component("budget", None if b.state == "unknown" else True,
                                  ("warning: " if b.state in ("warning", "high") else "") + b.line)
    except Exception as exc:  # noqa: BLE001 - advisory, never breaks health
        out["budget"] = Component("budget", None, f"unknown ({str(exc)[:60]})")
    try:  # the sweep's own heartbeat; never ran = absent, never breaks health
        from steward import liveness

        if store.kv_get(liveness.PSEUDO, "tick_at"):
            out["liveness"] = liveness.component(store, now=clock())
    except Exception as exc:  # noqa: BLE001
        out["liveness"] = Component("liveness", None, f"unknown ({str(exc)[:60]})")
    comps = [out[n] for n in ALL_COMPONENTS if n in out]
    if extras:  # R10: the coordinator shows in `steward health`, never in COMPONENTS or the dash tick
        try:
            from steward import coordinator_link

            comps.append(coordinator_link.component(head_fn, clock()))
        except Exception as exc:  # noqa: BLE001 - advisory
            comps.append(Component("coordinator", None, f"unknown ({str(exc)[:60]})"))
    return comps


def _install_state(store: Store) -> dict | None:
    """install.json of this STEWARD_HOME, or None (no record: the repo install, or an unreadable file)."""
    from steward import installstate

    try:
        return installstate.load(store.home)
    except installstate.InstallStateError:
        return None


def _mcp_component(store: Store, panes: list[dict] | None, unit_active: Callable[[str], bool]) -> Component:
    """With no install.json: the legacy three-unit check, unchanged. With one: exactly the recorded units of the
    enabled systemd-mode components and the `svc <target>` panes of the enabled herdr-mode ones; nothing enabled is
    `off (not enabled)` (dim, never alerting)."""
    state = _install_state(store)
    if state is None:
        down = [u for u in UNITS if not unit_active(u)]
        return (Component("mcp", False, f"{len(down)} of {len(UNITS)} inactive", [f"mcp: {u} is not active" for u in down])
                if down else Component("mcp", True, "steward-mcp and tunnels active"))
    from steward import components

    units: list[str] = []
    for cid in components.COMPONENT_IDS:
        rec = (state.get("components") or {}).get(cid)
        if isinstance(rec, dict) and rec.get("enabled") and rec.get("mode") == "systemd":
            units += [u for u in rec.get("units") or [] if isinstance(u, str) and u not in units]
    targets = components.herdr_targets(state)
    if not units and not targets:
        return Component("mcp", None, "off (not enabled)")
    problems = [f"mcp: {u} is not active" for u in units if not unit_active(u)]
    if targets:
        if panes is None:
            if not problems:
                return Component("mcp", None, "unknown (Herdr unreachable)")
        else:
            labels = {p.get("label") for p in panes}
            problems += [f"mcp: pane {components.pane_label(t)} is not running" for t in targets
                         if components.pane_label(t) not in labels]
    total = len(units) + len(targets)
    if problems:
        return Component("mcp", False, f"{len(problems)} of {total} not running", problems)
    return Component("mcp", True, ", ".join(units + [components.pane_label(t) for t in targets]) + " running")


# -- persisted state: red_since, self-heal, alerts ------------------------------------------------------


def _state_path(store: Store) -> Path:
    return Path(store.home) / STATE_FILE


def load_state(store: Store) -> dict:
    try:
        data = json.loads(_state_path(store).read_text())
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def save_state(store: Store, state: dict) -> None:
    path = _state_path(store)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def update_state(state: dict, comps: list[Component], now: float) -> dict:
    """Carry `red_since` while a component stays red; clear it (and its heal/alert marks) on recovery."""
    prev = state.get("components") or {}
    cur: dict[str, dict] = {}
    for c in comps:
        old = prev.get(c.name) or {}
        red = c.ok is False
        cur[c.name] = {"ok": c.ok, "detail": c.detail, "problems": c.problems,
                       "red_since": (old.get("red_since") or now) if red else None,
                       "alerted": bool(old.get("alerted")) if red else False,
                       "alert_tries": int(old.get("alert_tries") or 0) if red else 0,
                       "healed": bool(old.get("healed")) if red else False}
    state = dict(state)
    state["components"] = cur
    state["checked_at"] = now
    return state


def red_for(state: dict, now: float, min_s: float = RED_ALERT_S) -> list[str]:
    return [n for n, c in (state.get("components") or {}).items()
            if c.get("red_since") is not None and now - float(c["red_since"]) > min_s]


def tick(
    store: Store, *, now: float | None = None, ttl_s: float = TTL_S, heal: bool = True, alert: bool = True,
    check_fn: Callable[[], list[Component]] | None = None, heal_fn: Callable[[], dict] | None = None,
    alert_fn: Callable[[str], bool] | None = None, alert_skip: tuple[str, ...] = (),
) -> dict:
    """The dashboard's 30s check. Cached in `health.json` (an earlier refresh's result is reused inside `ttl_s`).
    Heals a dead control pane once per outage and alerts on anything red for > RED_ALERT_S. Never raises."""
    now = time.time() if now is None else now
    state = load_state(store)
    if now - float(state.get("checked_at") or 0) < ttl_s:
        return state
    try:
        comps = check_fn() if check_fn else check_all(store)
        state = update_state(state, comps, now)
        state["metrics"] = lock_metrics(store, now=now)
        ctl = state["components"].get("control") or {}
        if heal and ctl.get("ok") is False and not ctl.get("healed") and heal_fn is not None:
            ctl["healed"] = True
            try:
                result = dict(heal_fn())
            except Exception as exc:  # noqa: BLE001
                result = {"error": str(exc)[:160]}
            state.setdefault("heals", []).append({"ts": now, "component": "control", **{
                k: v for k, v in result.items() if k in ("error", "created", "control_pane", "detail")}})
            state["heals"] = state["heals"][-20:]
        if alert and alert_fn is not None:
            for name in red_for(state, now):
                if name in alert_skip:
                    continue
                c = state["components"][name]
                if c.get("alerted") or int(c.get("alert_tries") or 0) >= MAX_ALERT_TRIES:
                    continue
                text = f"Steward health: {name} red for {int((now - float(c['red_since'])) // 60)}m+ — " + (
                    "; ".join(c.get("problems") or [c.get("detail", "")])[:200])
                try:
                    ok = bool(alert_fn(text))
                except Exception:  # noqa: BLE001
                    ok = False
                c["alert_tries"] = int(c.get("alert_tries") or 0) + 1
                if ok:
                    c["alerted"] = True
                    state.setdefault("alert_log", []).append({"ts": now, "component": name})
                    state["alert_log"] = state["alert_log"][-50:]
        save_state(store, state)
    except Exception:  # noqa: BLE001 - a health check never breaks the dashboard
        pass
    return state


def default_heal(store: Store, api: Any | None = None) -> Callable[[], dict]:
    """Self-heal = `steward up`: `launch.ensure_steward_up` in the workspace the cockpit was last seen in."""
    def heal() -> dict:
        from steward import launch
        from steward.herdr_transport import HerdrApi
        from steward.runner_exec import SubprocessRunner

        a = api or HerdrApi(default_socket())
        workspace = os.environ.get("HERDR_WORKSPACE_ID")
        if not workspace:
            panes = a.call("pane.list", {}).get("panes", [])
            workspace = next((p.get("workspace_id") for p in panes
                              if (p.get("tokens") or {}).get("steward_role") in ("advisor", "control")), None) \
                or next((p.get("workspace_id") for p in panes if p.get("workspace_id")), None)
        if not workspace:
            return {"error": "no Herdr workspace to restart the control pane in"}
        return launch.ensure_steward_up(SubprocessRunner(), a, default_socket(), workspace)
    return heal


def default_alert(runner_exec: Any | None = None) -> Callable[[str], bool]:
    def send(text: str) -> bool:
        from steward.runner_exec import SubprocessRunner

        return alerts.system_alert(text, runner_exec or SubprocessRunner())
    return send


# -- rendering ------------------------------------------------------------------------------------------

_GREEN, _RED, _DIM, _RST = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def render_header(state: dict, width: int = 88, *, color: bool = True) -> list[str]:
    """`HEALTH ● control ● advisor ● herdr ● runners ● mcp` plus one line per red problem."""
    from steward import jobview

    comps = state.get("components") or {}
    names = [n for n in ALL_COMPONENTS if n in comps]
    if not names:
        return []

    def dot(ok):
        ch = "●"
        if not color:
            return ch if ok else ("○" if ok is None else "✗")
        return f"{_GREEN if ok else _RED if ok is False else _DIM}{ch}{_RST}"

    label = f"\033[1mHEALTH{_RST}" if color else "HEALTH"
    lines = [label + " " + " ".join(f"{dot(comps[n]['ok'])} {n}" for n in names)]
    for n in names:
        if comps[n]["ok"] is False:
            for prob in comps[n].get("problems") or [f"{n}: {comps[n].get('detail', '')}"]:
                text = f"  {prob}"
                lines.append(f"{_RED}{jobview.fit(text, width)}{_RST}" if color else text[:width])
    bud = comps.get("budget")
    if bud and bud.get("ok") is None:
        lines.append(f"{_DIM}  budget reading stale{_RST}" if color else "  budget reading stale")
    elif bud and str(bud.get("detail", "")).startswith("warning: "):
        text = "  " + str(bud["detail"])[len("warning: "):]
        lines.append(f"\033[33m{jobview.fit(text, width)}{_RST}" if color else text[:width])
    return lines


def to_json(comps: list[Component], state: dict | None = None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    saved = (state or {}).get("components") or {}
    rows = []
    for c in comps:
        row = asdict(c)
        since = (saved.get(c.name) or {}).get("red_since")
        row["red_since"] = since if c.ok is False else None
        rows.append(row)
    return {"checked_at": now, "ok": all(c.ok is not False for c in comps), "components": rows}


def render_lines(comps: list[Component], width: int = 88, *, color: bool = False) -> list[str]:
    st = update_state({}, comps, time.time())
    return render_header(st, width, color=color)
