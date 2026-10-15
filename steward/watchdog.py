"""`steward watchdog`: no Steward process stays dead without a human (issue W, self-heal spec).

A loop (one pass every `interval_s`, run by the `steward-watchdog` systemd --user unit, outside Herdr and Steward)
over an extensible registry of healers. Each healer has `find(ctx)` -> structured observations, `heal(ctx, obs)`, and
its limits (two-observation rule, breaker, rate limit). The first two:

* `control` — restored DIRECTLY (nothing else can): dead (no lock holder, no legacy process) or wedged (lock held,
  heartbeat older than `wedged_s`; confirmed stopped before replacement). Started in its old pane when it is still
  there, else in a new "steward" tab, from the deployed revision (`steward.deployed`).
* `runners` — a RUNNING job whose runner is dead is restored ONLY through control's normal `runner_restart` relay
  (`mode=heal`, `origin=watchdog`): control stays the one actor on jobs, rechecks and launches. The worker is never
  touched (SP-14); a missing worker is reported, not healed.

Held, never healed: anything while Herdr is down (escalated once after `herdr_escalate_s`); control while a deploy is
actively changing processes (`deploy-self.lock`), for at most `deploy_hold_s`; a paused job. A merge awaiting deploy
never blocks healing — it only decides the code tree.

Breaker per target: unexpected exits (not during/just after a deploy) within `breaker_window_s`; `breaker_max` of them
open it for `cooldown_s` and raise a durable tech-lead blocker (runner) or send the one owner notice (control). After
the cooldown one half-open attempt; `reset_after_s` healthy closes it. Never a permanent give-up.

Works with a locked DB: state is `<home>/watchdog.json`; jobs are read read-only (cached on failure); every heal is
appended to `<home>/watchdog-events.jsonl`, and job events/blockers go through `<home>/watchdog-spool.jsonl`.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from steward import deployed, proclock

STATE_FILE = "watchdog.json"
CONFIG_FILE = "watchdog.toml"
EVENTS_FILE = "watchdog-events.jsonl"
SPOOL_FILE = "watchdog-spool.jsonl"
BREAKER_KIND = "watchdog_breaker"
PREDICATE = "watchdog_target_healthy"
DEAD_OBSERVATIONS = 2
READY_WAIT_S = 20.0
HEALS_KEPT = 50


# -- configuration -----------------------------------------------------------------------------------------------

@dataclass
class Limits:
    breaker_max: int = 3
    breaker_window_s: float = 600.0
    cooldown_s: float = 900.0
    reset_after_s: float = 600.0
    deploy_hold_s: float = 180.0
    herdr_escalate_s: float = 300.0


@dataclass
class Config:
    interval_s: float = 30.0
    notify: bool = True
    control_enabled: bool = True
    wedged_s: float = 300.0
    runners_enabled: bool = True
    max_runner_heals_per_pass: int = 2
    limits: Limits = field(default_factory=Limits)
    notes: list[str] = field(default_factory=list)


def load_config(home: Path | str) -> Config:
    """`<home>/watchdog.toml`; every key optional; a broken file means the defaults (noted, never fatal)."""
    cfg = Config()
    path = Path(home) / CONFIG_FILE
    if not path.exists():
        return cfg
    try:
        data = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError) as exc:
        cfg.notes.append(f"{CONFIG_FILE} ignored: {exc}")
        return cfg
    try:
        cfg.interval_s = float(data.get("interval_s", cfg.interval_s))
        cfg.notify = bool(data.get("notify", cfg.notify))
        healers = data.get("healers") or {}
        ctl = healers.get("control") or {}
        cfg.control_enabled = bool(ctl.get("enabled", True))
        cfg.wedged_s = float(ctl.get("wedged_s", cfg.wedged_s))
        run = healers.get("runners") or {}
        cfg.runners_enabled = bool(run.get("enabled", True))
        cfg.max_runner_heals_per_pass = int(run.get("max_heals_per_pass", cfg.max_runner_heals_per_pass))
        lim = data.get("limits") or {}
        for k in Limits.__dataclass_fields__:
            if k in lim:
                setattr(cfg.limits, k, type(getattr(cfg.limits, k))(lim[k]))
    except (TypeError, ValueError, AttributeError) as exc:
        cfg.notes.append(f"{CONFIG_FILE} partly ignored: {exc}")
    return cfg


# -- state -------------------------------------------------------------------------------------------------------

def load_state(home: Path | str) -> dict:
    return proclock.read_json(Path(home) / STATE_FILE) or {}


def save_state(home: Path | str, state: dict) -> None:
    proclock.write_json(Path(home) / STATE_FILE, state)


def _target(state: dict, name: str) -> dict:
    return state.setdefault("targets", {}).setdefault(name, {"dead_seen": 0, "breaker": {"exits": []}})


# -- audit -------------------------------------------------------------------------------------------------------

class Audit:
    """Every heal goes to `watchdog-events.jsonl` (always). Job events and blockers are spooled and written to the
    store when it accepts writes."""

    def __init__(self, home: Path | str, store_fn: Callable[[], Any], clock: Callable[[], float]):
        self.home = Path(home)
        self.store_fn = store_fn
        self.clock = clock

    def _append(self, name: str, rec: dict) -> None:
        try:
            with (self.home / name).open("a") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass

    def record(self, what: str, evidence: dict, *, job_id: str | None = None) -> None:
        rec = {"at": self.clock(), "what": what, "job_id": job_id, "evidence": evidence}
        self._append(EVENTS_FILE, rec)
        if job_id:
            self._append(SPOOL_FILE, {"kind": "event", **rec})

    def blocker(self, job_id: str, target: str, claim: str, evidence: list[str]) -> None:
        self._append(SPOOL_FILE, {"kind": "blocker", "at": self.clock(), "job_id": job_id, "target": target,
                                  "claim": claim, "evidence": evidence})

    def pending(self) -> int:
        try:
            return sum(1 for ln in (self.home / SPOOL_FILE).read_text().splitlines() if ln.strip())
        except OSError:
            return 0

    def flush(self) -> int:
        """Write spooled entries to the store, oldest first; stop at the first failure (kept for the next pass)."""
        path = self.home / SPOOL_FILE
        try:
            lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
        except OSError:
            return 0
        if not lines:
            return 0
        done = 0
        try:
            store = self.store_fn()
            for ln in lines:
                try:
                    rec = json.loads(ln)
                except ValueError:
                    done += 1
                    continue
                _apply_spooled(store, rec)
                done += 1
        except (sqlite3.Error, OSError):
            pass
        except Exception:  # noqa: BLE001 - an unknown job id etc.: drop that entry, never wedge the spool
            done += 1
        rest = lines[done:]
        try:
            path.write_text("".join(ln + "\n" for ln in rest))
        except OSError:
            pass
        return done


def _apply_spooled(store: Any, rec: dict) -> None:
    from steward.models import Judgment

    job_id = rec.get("job_id")
    if not job_id or store.get_job(job_id) is None:
        return
    if rec.get("kind") == "blocker":
        from steward import blockers
        from steward.actor import Actor, ActorRef

        blockers.raise_blocker(store, job_id, kind=BREAKER_KIND, subject=str(rec.get("target")),
                               claim=str(rec.get("claim"))[:400], evidence=list(rec.get("evidence") or []),
                               origin_actor=ActorRef(Actor.CONTROLLER, "watchdog"), now=float(rec.get("at") or 0),
                               origin_rule="watchdog_breaker", responsible="tech_lead",
                               clearing={"predicate": PREDICATE, "params": {"target": rec.get("target")}})
        return
    store.append_event(job_id, what=str(rec.get("what")), evidence=dict(rec.get("evidence") or {}) | {
        "watchdog_at": rec.get("at")}, rule="watchdog", judgment=Judgment.code)


# -- context -----------------------------------------------------------------------------------------------------

@dataclass
class Obs:
    target: str
    state: str
    detail: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class Ctx:
    home: Path
    api: Any                                    # Herdr socket API: .call("pane.list", {})
    runner_exec: Any                            # herdr CLI runner (tab create / pane run ...)
    relay_fn: Callable[[str, dict], dict]       # control relay; raises on no control
    notify_fn: Callable[[str], bool]
    sock: str = ""
    cfg: Config = field(default_factory=Config)
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], Any] = time.sleep
    store_fn: Callable[[], Any] | None = None
    jobs_fn: Callable[[], list[dict]] | None = None
    stop_fn: Callable[[dict], bool] | None = None
    live_root: Path | None = None
    ready_wait_s: float = READY_WAIT_S
    state: dict = field(default_factory=dict)
    panes: list[dict] | None = None
    audit: Audit | None = None
    pass_heals: int = 0

    def __post_init__(self):
        self.home = Path(self.home)
        if self.store_fn is None:
            cache: dict = {}

            def store_fn():
                if "s" not in cache:
                    from steward.store import Store

                    cache["s"] = Store(self.home)
                return cache["s"]
            self.store_fn = store_fn
        if self.jobs_fn is None:
            self.jobs_fn = lambda: running_jobs(self.home)
        if self.stop_fn is None:
            self.stop_fn = lambda ident: proclock.stop_process(ident, sleep=self.sleep)
        if self.live_root is None:
            from steward import stability

            self.live_root = stability.LIVE_ROOT
        self.audit = Audit(self.home, self.store_fn, self.clock)


def running_jobs(home: Path | str) -> list[dict]:
    """RUNNING jobs + what the runners healer needs, read through a read-only connection (raises sqlite3.Error)."""
    db = Path(home) / "steward.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
    try:
        rows = conn.execute("SELECT job_id FROM jobs WHERE state = 'RUNNING'").fetchall()
        out = []
        for (job_id,) in rows:
            kv = dict(conn.execute("SELECT key, value_json FROM kv WHERE job_id = ? AND key IN "
                                   "('herdr_pane', 'governor_paused')", (job_id,)).fetchall())
            out.append({"job_id": job_id, "herdr_pane": _j(kv.get("herdr_pane")),
                        "governor_paused": bool(_j(kv.get("governor_paused")))})
        return out
    finally:
        conn.close()


def _j(raw):
    try:
        return json.loads(raw) if raw is not None else None
    except ValueError:
        return None


class Healer(Protocol):
    name: str

    def find(self, ctx: Ctx) -> list[Obs]: ...

    def heal(self, ctx: Ctx, obs: Obs) -> dict: ...


# -- breaker -----------------------------------------------------------------------------------------------------

def breaker_allows(ctx: Ctx, t: dict) -> tuple[bool, str]:
    b = t.setdefault("breaker", {"exits": []})
    now = ctx.clock()
    until = b.get("open_until")
    if until is not None and now < float(until):
        return False, f"breaker open (cooldown until {time.strftime('%H:%M:%S', time.localtime(float(until)))})"
    if until is not None:  # cooldown over: half-open, one attempt
        b.update({"open_until": None, "probing": True, "probe_used": False, "exits": []})
    return True, ""


def count_exit(ctx: Ctx, t: dict, *, counted: bool) -> bool:
    """Record one confirmed death about to be healed. True when it opens the breaker (then: no heal now)."""
    b = t.setdefault("breaker", {"exits": []})
    now = ctx.clock()
    b["healthy_since"] = None
    if not counted:
        return False
    lim = ctx.cfg.limits
    if b.get("probing"):
        if not b.get("probe_used"):  # the one half-open attempt
            b["probe_used"] = True
            b["exits"] = [now]
            return False
        reopen = True  # died again after the half-open attempt: another cooldown (never a permanent give-up)
    else:
        b["exits"] = [x for x in b.get("exits", []) if now - float(x) < lim.breaker_window_s] + [now]
        reopen = len(b["exits"]) >= lim.breaker_max
    if reopen:
        b.update({"open_until": now + lim.cooldown_s, "probing": False, "probe_used": False,
                  "opened": int(b.get("opened") or 0) + 1})
        return True
    return False


def note_healthy(ctx: Ctx, name: str, t: dict) -> None:
    b = t.setdefault("breaker", {"exits": []})
    now = ctx.clock()
    t["dead_seen"] = 0
    if not b.get("exits") and b.get("open_until") is None and not b.get("probing"):
        return
    if b.get("healthy_since") is None:
        b["healthy_since"] = now
    elif now - float(b["healthy_since"]) >= ctx.cfg.limits.reset_after_s:
        was_open = b.get("open_until") is not None or b.get("opened")
        t["breaker"] = {"exits": []}
        ctx.audit.record("watchdog_breaker_reset", {"target": name, "was_open": bool(was_open)},
                         job_id=t.get("job_id"))


def reset(home: Path | str, target: str) -> bool:
    state = load_state(home)
    t = (state.get("targets") or {}).get(target)
    if t is None:
        return False
    t["breaker"] = {"exits": []}
    t["dead_seen"] = 0
    save_state(home, state)
    return True


def target_healthy_predicate(store: Any, job_id: str, b: Any, facts: dict) -> str:
    """Ownership clearing predicate for a `watchdog_breaker` blocker: cleared once the target is healthy and its
    breaker has reset."""
    target = ((getattr(b, "clearing", None) or {}).get("params") or {}).get("target") or f"runner:{job_id}"
    t = (load_state(store.home).get("targets") or {}).get(target)
    if t is None:
        return "cleared"  # the job no longer has a watched runner (not RUNNING any more)
    bk = t.get("breaker") or {}
    healthy = t.get("state") in ("runner_ok", "control_ok")
    clean = not bk.get("exits") and bk.get("open_until") is None and not bk.get("probing")
    return "cleared" if healthy and clean else "holds"


# -- owner notice ------------------------------------------------------------------------------------------------

def notice(ctx: Ctx, why: str) -> None:
    """The ONE owner message per outage, only when control cannot be restored."""
    n = ctx.state.setdefault("notice", {})
    if n.get("sent") or not ctx.cfg.notify:
        return
    text = f"Steward watchdog: control cannot be restored without you - {why}"[:400]
    try:
        ok = bool(ctx.notify_fn(text))
    except Exception:  # noqa: BLE001
        ok = False
    n.update({"sent": ok, "at": ctx.clock(), "why": why, "tries": int(n.get("tries") or 0) + 1})
    ctx.audit.record("watchdog_owner_notice", {"why": why, "delivered": ok})


# -- control healer ----------------------------------------------------------------------------------------------

class ControlHealer:
    name = "control"

    def find(self, ctx: Ctx) -> list[Obs]:
        ident = proclock.read_json(proclock.control_file(ctx.home)) or {}
        if proclock.is_held(proclock.control_lock(ctx.home)):
            beat = ident.get("beat_at")
            age = ctx.clock() - float(beat) if isinstance(beat, (int, float)) else None
            if age is not None and age > ctx.cfg.wedged_s:
                return [Obs("control", "control_wedged", f"heartbeat {age:.0f}s old", {"ident": ident})]
            detail = f"pid {ident.get('pid')}"
            want = None
            try:
                want = deployed.resolve_head(ctx.home, ctx.live_root)
            except Exception:  # noqa: BLE001
                pass
            if ident.get("head") and want and ident["head"] != want:
                return [Obs("control", "control_ok", f"{detail}; stale code: runs {str(ident['head'])[:12]}, "
                                                     f"deployed {want[:12]}", {"stale": True})]
            return [Obs("control", "control_ok", detail)]
        legacy = proclock.control_processes(ctx.home)
        if legacy:
            return [Obs("control", "control_ok", f"lock-less control pid {legacy[0]}", {"legacy": True})]
        return [Obs("control", "control_dead", "no control process", {"ident": ident})]

    def heal(self, ctx: Ctx, obs: Obs) -> dict:
        ident = obs.data.get("ident") or {}
        if obs.state == "control_wedged":
            ctx.audit.record("watchdog_control_wedged", {"ident": ident, "detail": obs.detail,
                                                         "proc_state": proclock.proc_state(ident["pid"])
                                                         if isinstance(ident.get("pid"), int) else None})
            if not ctx.stop_fn(ident) or not self._lock_free(ctx):
                return {"ok": False, "detail": "wedged control could not be confirmed stopped; nothing started"}
        if not self._lock_free(ctx):
            return {"ok": True, "noop": True, "detail": "a control came back by itself"}
        try:
            root, live = deployed.launch_root(ctx.home, ctx.live_root)
        except deployed.UnknownDeployed as exc:
            notice(ctx, f"deployed revision unknown ({exc})")
            return {"ok": False, "held": True, "detail": str(exc)}
        panes = ctx.panes or []
        ids = {p.get("pane_id") for p in panes}
        pane = ident.get("pane") if ident.get("pane") in ids else None
        if pane is None:
            from steward import control_pane

            pane = next((p.get("pane_id") for p in panes if (p.get("tokens") or {}).get(
                control_pane.CONTROL_ROLE_TOKEN) == control_pane.CONTROL_ROLE_VALUE), None)
        created_tab = None
        if pane is None:
            ws = _workspace(ident, panes)
            if not ws:
                return {"ok": False, "detail": "no Herdr workspace to start control in"}
            from steward import launch

            res = ctx.runner_exec.run(["herdr", "tab", "create", "--workspace", ws, "--label",
                                       launch.STEWARD_TAB_LABEL, "--no-focus"], cwd=Path.cwd())
            if res.exit_code != 0:
                return {"ok": False, "detail": f"herdr tab create failed: {(res.stderr or '').strip()[:120]}"}
            data = json.loads(res.stdout)
            created_tab, pane = data["result"]["tab"]["tab_id"], data["result"]["root_pane"]["pane_id"]
        from steward import launch

        launch.start_control_on(ctx.runner_exec, ctx.sock, pane, root=root, live_root=live)
        ready = _wait(ctx, lambda: proclock.is_held(proclock.control_lock(ctx.home)))
        return {"ok": ready, "pane": pane, "tab": created_tab, "root": str(root), "pinned": live is not None,
                "detail": f"control {'restored' if ready else 'started but not ready'} in {pane}"}

    @staticmethod
    def _lock_free(ctx: Ctx) -> bool:
        return not proclock.is_held(proclock.control_lock(ctx.home)) and not proclock.control_processes(ctx.home)


def _workspace(ident: dict, panes: list[dict]) -> str | None:
    pane = ident.get("pane")
    if isinstance(pane, str) and ":" in pane:
        return pane.split(":", 1)[0]
    return next((p.get("workspace_id") for p in panes if p.get("workspace_id")), None) or \
        next((str(p.get("pane_id")).split(":", 1)[0] for p in panes if ":" in str(p.get("pane_id"))), None)


def _wait(ctx: Ctx, cond: Callable[[], bool], poll_s: float = 0.25) -> bool:
    deadline = ctx.clock() + ctx.ready_wait_s
    while True:
        if cond():
            return True
        if ctx.clock() >= deadline:
            return False
        ctx.sleep(poll_s)


# -- runners healer ----------------------------------------------------------------------------------------------

class RunnersHealer:
    name = "runners"

    def find(self, ctx: Ctx) -> list[Obs]:
        from steward import launch

        try:
            jobs = ctx.jobs_fn()
            ctx.state["jobs_cache"] = jobs
            ctx.state["jobs_cached"] = False
        except (sqlite3.Error, OSError) as exc:
            jobs = ctx.state.get("jobs_cache") or []
            ctx.state["jobs_cached"] = True
            ctx.audit.record("watchdog_jobs_unreadable", {"error": str(exc)[:160], "cached": len(jobs)})
        ids = {p.get("pane_id") for p in ctx.panes or []}
        out = []
        for j in jobs:
            jid = j["job_id"]
            name = f"runner:{jid}"
            if j.get("governor_paused"):
                out.append(Obs(name, "held", "paused by the governor", {"job_id": jid}))
                continue
            if launch.runner_alive(ctx.home, jid):
                wp = j.get("herdr_pane")
                if wp and wp not in ids:
                    out.append(Obs(name, "missing_worker", f"worker pane {wp} is gone (runner recovery owns it)",
                                   {"job_id": jid}))
                else:
                    out.append(Obs(name, "runner_ok", "", {"job_id": jid}))
                continue
            out.append(Obs(name, "runner_dead", "no runner process", {"job_id": jid}))
        return out

    def heal(self, ctx: Ctx, obs: Obs) -> dict:
        from steward import control_pane

        jid = obs.data["job_id"]
        try:
            receipt = ctx.relay_fn("runner_restart", {"job_id": jid, "mode": "heal"})
        except (control_pane.NoControlPane, control_pane.ControlTimeout) as exc:
            return {"ok": False, "deferred": True, "detail": f"control unavailable: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "deferred": True, "detail": f"relay failed: {type(exc).__name__}: {exc}"}
        if not receipt.get("accepted"):
            return {"ok": False, "detail": str(receipt.get("detail"))[:200], "receipt": receipt}
        return {"ok": True, "noop": bool(receipt.get("noop")), "pane": receipt.get("pane"),
                "detail": str(receipt.get("detail"))[:200], "await_ready": not receipt.get("noop")}


REGISTRY: dict[str, type] = {"control": ControlHealer, "runners": RunnersHealer}


# -- one pass ----------------------------------------------------------------------------------------------------

def _herdr_panes(ctx: Ctx) -> list[dict] | None:
    try:
        return list(ctx.api.call("pane.list", {}).get("panes", []))
    except Exception:  # noqa: BLE001 - HerdrUnavailable, socket errors, anything: Herdr is down for this pass
        return None


def _deploy_active(ctx: Ctx) -> bool:
    from steward import deploy_self

    return proclock.is_held(ctx.home / deploy_self.LOCK_NAME)


def _log_heal(ctx: Ctx, rec: dict) -> None:
    heals = ctx.state.setdefault("heals", [])
    heals.append({"at": ctx.clock(), **rec})
    ctx.state["heals"] = heals[-HEALS_KEPT:]


def _process(ctx: Ctx, healer: Any, obs: Obs, *, deploy_recent: bool, rate: dict) -> None:
    t = _target(ctx.state, obs.target)
    t["state"], t["detail"] = obs.state, obs.detail
    if obs.data.get("job_id"):
        t["job_id"] = obs.data["job_id"]
    now = ctx.clock()
    pend = t.pop("await_ready", None)
    if obs.state in ("control_ok", "runner_ok", "missing_worker"):
        if pend:
            ctx.audit.record("watchdog_healed", {"target": obs.target, "after_s": round(now - pend["at"], 1),
                                                 "pane": pend.get("pane")}, job_id=t.get("job_id"))
            _log_heal(ctx, {"target": obs.target, "result": "healed"})
        if obs.state == "missing_worker" and t.get("reported") != "missing_worker":
            ctx.audit.record("watchdog_missing_worker", {"target": obs.target, "detail": obs.detail},
                             job_id=t.get("job_id"))
        t["reported"] = obs.state
        note_healthy(ctx, obs.target, t)
        return
    if obs.state not in ("control_dead", "control_wedged", "runner_dead"):
        t["dead_seen"] = 0
        return
    if pend:  # the last heal did not come up: a failed attempt
        ctx.audit.record("watchdog_failed", {"target": obs.target, "detail": "not running after the heal"},
                         job_id=t.get("job_id"))
        _log_heal(ctx, {"target": obs.target, "result": "failed"})
    if obs.state != "control_wedged":  # a wedge is confirmed by its heartbeat age; a death by two observations
        t["dead_seen"] = int(t.get("dead_seen") or 0) + 1
        if t["dead_seen"] < DEAD_OBSERVATIONS:
            t["detail"] = f"{obs.detail} (seen once; confirming)"
            return
    ok, why = breaker_allows(ctx, t)
    if not ok:
        t["detail"] = f"{obs.detail}; {why}"
        return
    if healer.name == "runners" and rate["runners"] >= ctx.cfg.max_runner_heals_per_pass:
        t["detail"] = f"{obs.detail}; queued (at most {ctx.cfg.max_runner_heals_per_pass} runner heals per pass)"
        return
    if count_exit(ctx, t, counted=not deploy_recent):
        lim = ctx.cfg.limits
        ev = {"target": obs.target, "exits": len(t["breaker"]["exits"]), "window_s": lim.breaker_window_s,
              "cooldown_s": lim.cooldown_s}
        ctx.audit.record("watchdog_breaker_open", ev, job_id=t.get("job_id"))
        _log_heal(ctx, {"target": obs.target, "result": "breaker_open"})
        if healer.name == "runners":
            ctx.audit.blocker(t["job_id"], obs.target,
                              f"runner for {t['job_id'][:8]} died {len(t['breaker']['exits'])} times in "
                              f"{lim.breaker_window_s / 60:.0f} min; watchdog pauses restarts for "
                              f"{lim.cooldown_s / 60:.0f} min, then retries", [f"watchdog:{obs.target}"])
        else:
            notice(ctx, f"control died {len(t['breaker']['exits'])} times in {lim.breaker_window_s / 60:.0f} min "
                        f"(breaker open; retrying after {lim.cooldown_s / 60:.0f} min)")
        return
    if healer.name == "runners":
        rate["runners"] += 1
    try:
        res = healer.heal(ctx, obs)
    except Exception as exc:  # noqa: BLE001 - one heal never ends the pass
        res = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:200]}
    t["dead_seen"] = 0
    ev = {"target": obs.target, "state": obs.state, **{k: v for k, v in res.items() if k not in ("receipt",)}}
    if res.get("deferred"):
        t["breaker"]["exits"] = t["breaker"]["exits"][:-1] if not deploy_recent else t["breaker"]["exits"]
        t["dead_seen"] = DEAD_OBSERVATIONS - 1  # still confirmed dead: retry next pass
        t["detail"] = f"{obs.detail}; deferred: {res.get('detail')}"
        return
    if res.get("ok") and res.get("await_ready"):
        t["await_ready"] = {"at": now, "pane": res.get("pane")}
        ctx.audit.record("watchdog_heal_requested", ev, job_id=t.get("job_id"))
        _log_heal(ctx, {"target": obs.target, "result": "requested"})
    elif res.get("ok"):
        ctx.audit.record("watchdog_healed", ev, job_id=t.get("job_id"))
        _log_heal(ctx, {"target": obs.target, "result": "noop" if res.get("noop") else "healed"})
        t["state"] = "control_ok" if healer.name == "control" else "runner_ok"
    else:
        ctx.audit.record("watchdog_failed", ev, job_id=t.get("job_id"))
        _log_heal(ctx, {"target": obs.target, "result": "failed"})
    t["detail"] = str(res.get("detail"))[:200]


def run_once(ctx: Ctx) -> dict:
    """One pass. Never raises."""
    ctx.state = load_state(ctx.home)
    now = ctx.clock()
    try:
        from steward import stability

        ctx.state.update({"pass_at": now, "pid": os.getpid(), "head": stability.PROCESS_HEAD,
                          "notes": ctx.cfg.notes})
        ctx.panes = _herdr_panes(ctx)
        if ctx.panes is None:
            since = ctx.state.get("herdr_down_since") or now
            ctx.state["herdr_down_since"] = since
            ctx.state["herdr"] = "down"
            if now - float(since) >= ctx.cfg.limits.herdr_escalate_s:
                notice(ctx, f"Herdr is down for {int((now - float(since)) // 60)} min (healing held)")
            ctx.audit.flush()
            return ctx.state
        ctx.state["herdr_down_since"] = None
        ctx.state["herdr"] = "up"
        deploy_now = _deploy_active(ctx)
        if deploy_now:
            ctx.state["deploy_seen_at"] = now
            ctx.state.setdefault("deploy_held_since", now)
        else:
            ctx.state["deploy_held_since"] = None
        last = ctx.state.get("deploy_seen_at")
        deploy_recent = bool(last is not None and now - float(last) < ctx.cfg.limits.deploy_hold_s)
        rate = {"runners": 0}

        control_ok = True
        if ctx.cfg.control_enabled:
            healer = ControlHealer()
            for obs in _safe_find(ctx, healer):
                if deploy_now and obs.state in ("control_dead", "control_wedged") and \
                        now - float(ctx.state["deploy_held_since"]) < ctx.cfg.limits.deploy_hold_s:
                    t = _target(ctx.state, obs.target)
                    t["state"], t["detail"] = obs.state, f"{obs.detail}; held: a deploy is changing processes"
                    continue
                _process(ctx, healer, obs, deploy_recent=deploy_recent, rate=rate)
            ctl = _target(ctx.state, "control")
            control_ok = ctl.get("state") == "control_ok"
            if control_ok and (ctx.state.get("notice") or {}).get("sent"):
                ctx.state["notice"] = {}
        if ctx.cfg.runners_enabled:
            healer = RunnersHealer()
            seen = set()
            for obs in _safe_find(ctx, healer):
                seen.add(obs.target)
                if obs.state == "runner_dead" and not control_ok:
                    t = _target(ctx.state, obs.target)
                    t["state"], t["detail"], t["job_id"] = obs.state, "held: waiting for control", obs.data["job_id"]
                    t["dead_seen"] = max(int(t.get("dead_seen") or 0), 1)
                    continue
                _process(ctx, healer, obs, deploy_recent=deploy_recent, rate=rate)
            for name in [n for n in (ctx.state.get("targets") or {}) if n.startswith("runner:") and n not in seen]:
                del ctx.state["targets"][name]  # no longer RUNNING: an intentional stop, never healed
        ctx.audit.flush()
    except Exception as exc:  # noqa: BLE001 - a pass never kills the watchdog
        ctx.state["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        ctx.state["spooled"] = ctx.audit.pending()
        try:
            save_state(ctx.home, ctx.state)
        except OSError:
            pass
    return ctx.state


def _safe_find(ctx: Ctx, healer: Any) -> list[Obs]:
    try:
        return healer.find(ctx)
    except Exception as exc:  # noqa: BLE001 - an isolated probe failure: that target is unknown this pass
        ctx.audit.record("watchdog_probe_failed", {"healer": healer.name, "error": f"{type(exc).__name__}: {exc}"[:200]})
        return [Obs(healer.name, "unknown", str(exc)[:200])]


def single_instance(home: Path | str) -> proclock.Held | None:
    return proclock.acquire(proclock.lock_path(home, "watchdog"))


def should_reexec(own_head: str, live_head: str, deployed_head: str | None) -> bool:
    """Move the watchdog itself onto new code only when it is the deployed code (never an untested checkout)."""
    return bool(own_head and live_head and deployed_head and live_head == deployed_head and own_head != live_head)


def run_forever(ctx: Ctx, *, running: Callable[[], bool] = lambda: True,
                reexec_fn: Callable[[], None] | None = None) -> None:
    while running():
        run_once(ctx)
        if reexec_fn is not None:
            reexec_fn()
        ctx.sleep(ctx.cfg.interval_s)


# -- environment qualification -----------------------------------------------------------------------------------

def check_env(home: Path | str, sock: str, *, which: Callable[[str], str | None] | None = None,
              api_fn: Callable[[str], Any] | None = None, run: Callable[[list[str]], Any] | None = None) -> list[tuple[str, bool, str]]:
    import shutil
    import subprocess
    import sys

    from steward import stability

    which = which or shutil.which
    out: list[tuple[str, bool, str]] = []
    out.append(("interpreter", os.path.isabs(sys.executable), sys.executable))
    root = stability.LIVE_ROOT
    out.append(("repo", (Path(root) / ".git").exists(), str(root)))
    out.append(("STEWARD_HOME", Path(home).is_dir(), str(home)))
    sock_ok, detail = Path(sock).exists(), sock
    if sock_ok:
        try:
            from steward.herdr_transport import HerdrApi

            (api_fn or HerdrApi)(sock).call("pane.list", {})
        except Exception as exc:  # noqa: BLE001
            sock_ok, detail = False, f"{sock}: {exc}"[:200]
    out.append(("herdr socket", sock_ok, detail))
    for tool in ("herdr", "tg", "git"):
        path = which(tool)
        out.append((f"{tool} on PATH", bool(path), path or "missing"))
    user = os.environ.get("USER") or ""
    try:
        res = (run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=5)))(
            ["loginctl", "show-user", user, "-p", "Linger"])
        linger = "Linger=yes" in (res.stdout or "")
    except (OSError, subprocess.SubprocessError):
        linger = False
    out.append(("lingering", linger, "on" if linger else f"off: `loginctl enable-linger {user}` (needs the owner's OK)"))
    return out
