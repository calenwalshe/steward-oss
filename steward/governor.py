"""Steward governor (issue E, spec docs/superpowers/specs/2026-10-06-steward-governor-design.md).

The machine limit is a barrier: a launch with no room waits in the sequencing queue and the sweep starts it when
room returns (one per tick). A critical reading pauses the newest running job and cancels its check runs. The
Claude budget is advisory only. Readings never raise: an unreadable signal is None and is skipped.
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Protocol

from steward import config
from steward.models import JobState

GKV = "_governor"  # pseudo job id for the governor's own kv (like health's "_control")


@dataclass(frozen=True)
class Limits:
    max_running_jobs: int = 6
    min_free_mem_gb: float = 4.0
    crit_free_mem_gb: float = 2.0
    tmp_max_pct: float = 70.0
    tmp_crit_pct: float = 90.0
    disk_max_pct: float = 85.0
    disk_crit_pct: float = 95.0
    load5_max: float = 24.0
    resume_calm_s: float = 600.0


def limits() -> Limits:
    section = config._toml_section("governor")
    base = Limits()
    kw = {}
    for f in fields(Limits):
        v = section.get(f.name)
        if type(v) in (int, float) and v > 0:
            kw[f.name] = type(getattr(base, f.name))(v)
    return Limits(**kw)


@dataclass(frozen=True)
class Breach:
    signal: str
    value: float
    limit: float
    level: str  # "wait" | "critical"


@dataclass
class Reading:
    values: dict
    breaches: list = field(default_factory=list)

    @property
    def wait(self) -> bool:
        return bool(self.breaches)

    @property
    def critical(self) -> bool:
        return any(b.level == "critical" for b in self.breaches)

    def reason(self) -> str:
        return "; ".join(f"{b.signal} {b.value:g} vs {b.limit:g}" + (" (critical)" if b.level == "critical" else "")
                         for b in self.breaches)


class SysReader(Protocol):
    def mem_available_gb(self) -> float | None: ...
    def tmp_used_pct(self) -> float | None: ...
    def disk_used_pct(self) -> float | None: ...
    def load5(self) -> float | None: ...


class ProcReader:
    """The real machine. Every method returns None when its source can't be read."""

    def mem_available_gb(self):
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) / (1024 * 1024)
        except (OSError, ValueError, IndexError):
            return None
        return None

    @staticmethod
    def _used_pct(path):
        try:
            st = os.statvfs(path)
            total = st.f_blocks * st.f_frsize
            return None if total <= 0 else 100.0 * (total - st.f_bavail * st.f_frsize) / total
        except OSError:
            return None

    def tmp_used_pct(self):
        return self._used_pct("/tmp")

    def disk_used_pct(self):
        return self._used_pct(os.path.expanduser("~"))

    def load5(self):
        try:
            return os.getloadavg()[1]
        except OSError:
            return None


def running_jobs(store) -> int:
    from steward import sequencing

    busy = store.list_jobs({JobState.RUNNING, JobState.STOPPING})
    inflight = [j for j in store.list_jobs(set(sequencing.STAGED)) if store.kv_get(j.job_id, sequencing.K_LAUNCHED)]
    return len(busy) + len(inflight)


def read(store, *, sys_reader: SysReader | None = None, lim: Limits | None = None) -> Reading:
    s = sys_reader or ProcReader()
    lim = lim or limits()
    vals = {"running_jobs": float(running_jobs(store)), "mem_available_gb": s.mem_available_gb(),
            "tmp_used_pct": s.tmp_used_pct(), "disk_used_pct": s.disk_used_pct(), "load5": s.load5()}
    out: list[Breach] = []

    def above(sig, wait, crit=None):
        v = vals[sig]
        if v is None: return
        if crit is not None and v > crit: out.append(Breach(sig, round(v, 1), crit, "critical"))
        elif v > wait: out.append(Breach(sig, round(v, 1), wait, "wait"))

    above("running_jobs", lim.max_running_jobs)
    m = vals["mem_available_gb"]
    if m is not None:
        if m < lim.crit_free_mem_gb: out.append(Breach("mem_available_gb", round(m, 1), lim.crit_free_mem_gb, "critical"))
        elif m < lim.min_free_mem_gb: out.append(Breach("mem_available_gb", round(m, 1), lim.min_free_mem_gb, "wait"))
    above("tmp_used_pct", lim.tmp_max_pct, lim.tmp_crit_pct)
    above("disk_used_pct", lim.disk_max_pct, lim.disk_crit_pct)
    above("load5", lim.load5_max)
    return Reading(vals, out)


K_ROOM = "seq_room"  # on a job: why it waits for room (set by gate, cleared by sequencing.on_manual_launch)


def gate(store, job_id: str, args: dict, *, reading: Reading | None = None) -> str | None:
    """Called by every launch path before anything is opened. No room: park the job in the sequencing queue
    (reason + launch args kept, one event per new reason) and return the reason. Room: return None."""
    from steward import sequencing
    from steward.models import Judgment

    r = reading if reading is not None else read(store)
    if not r.wait:
        return None
    why = r.reason()
    keep = {k: v for k, v in (args or {}).items() if k != "job_id"}
    if keep:
        store.kv_set(job_id, sequencing.K_ARGS, keep)
    if store.kv_get(job_id, K_ROOM) is None:
        store.append_event(job_id, what="governor: waiting for room", evidence={"reason": why, "values": r.values},
                           rule="governor_wait", judgment=Judgment.code)
    store.kv_set(job_id, K_ROOM, why)
    return why


def relieve(store, *, pause, resume, reading: Reading, now: float, cancel=None) -> dict:
    """Control-pane tick. Critical: pause the newest RUNNING job (one per tick) and cancel its check runs.
    Calm (no breach at all) for resume_calm_s: resume the jobs the governor itself paused. Never raises."""
    from steward import check_runs
    from steward.models import Judgment

    lim = limits()
    cancel = cancel or (lambda jid: check_runs.cancel_runs_for(store, jid, now=now, reason="governor: critical load"))
    ours = list(store.kv_get(GKV, "paused") or [])
    out = {"paused": [], "resumed": []}
    if reading.critical:
        store.kv_set(GKV, "calm_since", None)
        running = sorted(store.list_jobs({JobState.RUNNING}), key=lambda j: j.created_at)
        if running:
            newest = running[-1].job_id
            if (pause(newest) or {}).get("accepted"):
                runs = cancel(newest)
                ours.append(newest)
                store.kv_set(GKV, "paused", ours)
                store.append_event(newest, what="governor: paused (critical)",
                                   evidence={"reason": reading.reason(), "runs": runs}, rule="governor_paused",
                                   judgment=Judgment.code)
                out["paused"].append(newest)
        return out
    if reading.wait:
        store.kv_set(GKV, "calm_since", None)
        return out
    if not ours:
        return out
    since = store.kv_get(GKV, "calm_since")
    if since is None:
        store.kv_set(GKV, "calm_since", now)
        return out
    if now - float(since) < lim.resume_calm_s:
        return out
    left = []
    for jid in ours:
        job = store.get_job(jid)
        if job is None or job.state != JobState.PAUSED:
            continue  # someone else moved it on: forget it
        if (resume(jid) or {}).get("accepted"):
            store.append_event(jid, what="governor: resumed", evidence={"calm_s": now - float(since)},
                               rule="governor_resumed", judgment=Judgment.code)
            out["resumed"].append(jid)
        else:
            left.append(jid)
    store.kv_set(GKV, "paused", left)
    store.kv_set(GKV, "calm_since", None)
    return out


USAGE_DB = Path.home() / "projects" / "token_efficiency" / "data" / "usage.db"
WARN_PCT, HIGH_PCT, STALE_S = 75.0, 90.0, 6 * 3600.0


@dataclass(frozen=True)
class Budget:
    state: str
    accounts: dict
    best: str | None
    advice: str
    line: str


def _epoch(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return None


def budget(*, db: Path | None = None, now: float | None = None) -> Budget:
    """Advisory only. Newest non-NULL seven_day reading per account; the account with the most room drives state."""
    now = time.time() if now is None else now
    db = Path(db or USAGE_DB)
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = c.execute("SELECT account_label, utilization, resets_at, captured_at FROM snapshots"
                             " WHERE bucket='seven_day' AND utilization IS NOT NULL ORDER BY captured_at DESC LIMIT 200").fetchall()
        finally:
            c.close()
    except sqlite3.Error:
        rows = []
    acc: dict = {}
    for label, pct, resets, captured in rows:
        if label in acc:
            continue
        t = _epoch(captured)
        if t is None or now - t > STALE_S:
            continue
        acc[label] = (float(pct), resets)
    if not acc:
        return Budget("unknown", {}, None, "", "Claude budget: reading stale")
    best = min(acc, key=lambda k: acc[k][0])
    low, worst = acc[best][0], max(v[0] for v in acc.values())
    state = "high" if low >= HIGH_PCT else "warning" if (low >= WARN_PCT or worst >= HIGH_PCT) else "ok"
    advice = ("consider --kind codex" if state == "high"
              else f"switch to {best}" if worst >= HIGH_PCT and low < WARN_PCT else "")
    parts = " · ".join(f"{k} {v[0]:.0f}%" for k, v in sorted(acc.items(), key=lambda kv: -kv[1][0]))
    line = f"Claude weekly: {parts}" + (f" → {advice}" if advice else "")
    return Budget(state, acc, best, advice, line)


def budget_notice(store, b: Budget, *, send=None) -> bool:
    """One operational notice per reset week, the first time a reading is high or says switch."""
    from steward import notify

    if b.state == "unknown" or not b.advice:
        return False
    week = f"{b.best}:{(b.accounts.get(b.best) or (0, ''))[1]}"
    if store.kv_get(GKV, "budget_noticed") == week:
        return False
    if not (send or notify.tg_send_text)(f"Steward: {b.line}"):
        return False
    store.kv_set(GKV, "budget_noticed", week)
    return True


def launch_hint(b: Budget, kind: str) -> str | None:
    if kind == "codex" or not b.advice:
        return None
    return f"{b.line}"


def render(store, reading: Reading, b: Budget) -> list[str]:
    from steward import tabnames

    lim = limits()
    caps = {"running_jobs": (lim.max_running_jobs, None), "mem_available_gb": (lim.min_free_mem_gb, lim.crit_free_mem_gb),
            "tmp_used_pct": (lim.tmp_max_pct, lim.tmp_crit_pct), "disk_used_pct": (lim.disk_max_pct, lim.disk_crit_pct),
            "load5": (lim.load5_max, None)}
    hit = {x.signal: x.level for x in reading.breaches}
    out = ["MACHINE"]
    for sig, (wait, crit) in caps.items():
        v = reading.values.get(sig)
        mark = {"critical": "CRIT", "wait": "WAIT"}.get(hit.get(sig), "ok  ")
        shown = "?" if v is None else f"{v:.1f}"
        out.append(f"  {mark} {sig:<17} {shown:>7}   wait {wait:g}" + (f" · critical {crit:g}" if crit else ""))
    waiting = [j for j in store.list_jobs() if store.kv_get(j.job_id, K_ROOM) is not None]
    out.append(f"WAITING FOR ROOM ({len(waiting)})")
    for j in waiting:
        out.append(f"  {j.job_id[:8]} {store.kv_get(j.job_id, tabnames.NAME_KEY) or ''}  waiting for room: "
                   f"{store.kv_get(j.job_id, K_ROOM)}")
    paused = store.kv_get(GKV, "paused") or []
    out.append(f"PAUSED BY GOVERNOR ({len(paused)})")
    out += [f"  {p[:8]}" for p in paused]
    out.append("BUDGET (advisory)")
    out.append(f"  {b.line}")
    return out
