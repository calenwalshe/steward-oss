"""Read-only view of the Steward DB and the trigger scan the coordinator polls.

``RoStore`` never writes (``mode=ro`` + ``query_only``) and is duck-compatible with what ``worker_view``,
``ownership.park_event`` and ``deploy_pending`` need from a store (``home``, ``kv_get``, ``get_job``, ``events``,
``events_of``).
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..machine import TERMINAL_STATES
from ..models import Event, Job, JobState, Judgment
from ..ownership import PARK_RULES, PARK_WHATS

COORD_RULES = frozenset({"recovery_budget_exhausted", "diagnosis_verdict_needs_human", "verify_rounds_exhausted",
                         "bg_verify_unverified", "finish_line_bounces_exhausted", "worker_start_blocked"})

ORPHAN_STATES = (JobState.RUNNING, JobState.WORKER_FINISHED, JobState.CHECKS_PASSED, JobState.STOPPING)

# ZT spec 5.1 noise (plus Steward's own efficiency_*, trajectory_* and TL idle-pause records, RC-5), as SQL LIKE
# patterns (escape char is backslash).
_NOISE_LIKE = ("runner\\_self\\_restart%", "heartbeat%", "tech\\_lead\\_review\\_deferred", "jev\\_%",
               "ownership\\_status", "efficiency\\_%", "trajectory\\_%", "tech\\_lead\\_idle\\_paused")

_RUNNER_GONE = re.compile(r"runner (\w{8}): runner process \d+ is gone")


def _epoch(ts: str) -> float:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _job(row) -> Job:
    return Job(job_id=row["job_id"], goal=row["goal"], repo=row["repo"], state=JobState(row["state"]),
               created_at=row["created_at"], updated_at=row["updated_at"])


def _event(row) -> Event:
    return Event(seq=row["seq"], job_id=row["job_id"], ts=row["ts"], what=row["what"],
                 evidence=json.loads(row["evidence"]), rule=row["rule"], judgment=Judgment(row["judgment"]))


class RoStore:
    def __init__(self, db_path: Path, home: Path):
        self.home = Path(home)
        self.db_path = Path(db_path)
        self._conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5,
                                     check_same_thread=False)
        self._conn.execute("PRAGMA query_only=1")
        self._conn.row_factory = sqlite3.Row

    def _q(self, sql, args=()):
        return self._conn.execute(sql, args).fetchall()

    def kv_get(self, job_id, key, default=None):
        rows = self._q("SELECT value_json FROM kv WHERE job_id = ? AND key = ?", (job_id, key))
        return json.loads(rows[0]["value_json"]) if rows else default

    kv = kv_get

    def kv_rows(self, key) -> list[tuple[str, object]]:
        return [(r["job_id"], json.loads(r["value_json"]))
                for r in self._q("SELECT job_id, value_json FROM kv WHERE key = ?", (key,))]

    def get_job(self, job_id) -> Job | None:
        rows = self._q("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        return _job(rows[0]) if rows else None

    def list_jobs(self, states=None) -> list[Job]:
        if states is None:
            rows = self._q("SELECT * FROM jobs ORDER BY created_at ASC")
        else:
            vals = [s.value for s in states]
            rows = self._q(f"SELECT * FROM jobs WHERE state IN ({','.join('?' * len(vals))})"
                           " ORDER BY created_at ASC", vals)
        return [_job(r) for r in rows]

    def events(self, job_id) -> list[Event]:
        return [_event(r) for r in self._q("SELECT * FROM events WHERE job_id = ? ORDER BY seq ASC", (job_id,))]

    def events_of(self, job_id, whats, rules=()) -> list[Event]:
        whats, rules = list(whats), list(rules)
        rows = self._q(f"SELECT * FROM events WHERE job_id = ? AND (what IN ({','.join('?' * len(whats))})"
                       f" OR rule IN ({','.join('?' * len(rules))})) ORDER BY seq ASC", (job_id, *whats, *rules))
        return [_event(r) for r in rows]

    def events_after(self, seq, limit=5000) -> list[Event]:
        return [_event(r) for r in self._q("SELECT * FROM events WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                                           (seq, limit))]

    def last_event_seq(self, job_id) -> int:
        return self._q("SELECT COALESCE(MAX(seq), 0) AS m FROM events WHERE job_id = ?", (job_id,))[0]["m"]

    def max_seq(self) -> int:
        return self._q("SELECT COALESCE(MAX(seq), 0) AS m FROM events")[0]["m"]

    def latest_of(self, what) -> dict[str, Event]:
        """The newest event named `what` per job."""
        rows = self._q("SELECT * FROM events WHERE seq IN (SELECT MAX(seq) FROM events WHERE what = ?"
                       " GROUP BY job_id)", (what,))
        return {r["job_id"]: _event(r) for r in rows}

    def approvals(self, job_id, status="pending") -> list[dict]:
        if status is None:
            rows = self._q("SELECT record_json FROM approvals WHERE job_id = ? ORDER BY rowid", (job_id,))
        else:
            rows = self._q("SELECT record_json FROM approvals WHERE job_id = ? AND status = ? ORDER BY rowid",
                           (job_id, status))
        return [json.loads(r["record_json"]) for r in rows]

    def checks(self, job_id, label) -> list[dict]:
        rows = self._q("SELECT commit_sha, exit_code, ts, command, label FROM checks"
                       " WHERE job_id = ? AND label = ? ORDER BY rowid DESC", (job_id, label))
        return [dict(r) for r in rows]

    def last_non_noise_event_ts(self, job_id) -> float | None:
        cond = " AND ".join("what NOT LIKE ? ESCAPE '\\'" for _ in _NOISE_LIKE)
        rows = self._q(f"SELECT ts FROM events WHERE job_id = ? AND {cond} ORDER BY seq DESC LIMIT 1",
                       (job_id, *_NOISE_LIKE))
        return _epoch(rows[0]["ts"]) if rows else None


@dataclass
class Trigger:
    kind: str
    job_id: str | None
    seq: int
    detail: dict = field(default_factory=dict)


def _components(health) -> dict[str, dict]:
    """Normalise both health shapes to {name: component dict}."""
    comps = (health or {}).get("components") if isinstance(health, dict) else None
    if isinstance(comps, dict):
        return {str(k): v for k, v in comps.items() if isinstance(v, dict)}
    if isinstance(comps, list):
        return {str(c["name"]): c for c in comps if isinstance(c, dict) and "name" in c}
    return {}


def _red_since(cdb, name, comp, now) -> float | None:
    rs = comp.get("red_since")
    if isinstance(rs, (int, float)) and not isinstance(rs, bool):
        return float(rs)
    if isinstance(rs, str) and rs:
        try:
            return _epoch(rs)
        except ValueError:
            pass
    cur = cdb.cursor_get(f"red_since:{name}", 0)
    if not cur:
        cdb.cursor_set(f"red_since:{name}", now)
        return now
    return float(cur)


def _health_triggers(ro, cdb, now, health, settings, taken) -> list[Trigger]:
    out: list[Trigger] = []
    for name, comp in _components(health).items():
        if comp.get("ok") is not False:
            if cdb.cursor_get(f"red_since:{name}", 0):
                cdb.cursor_set(f"red_since:{name}", 0)
            continue
        since = _red_since(cdb, name, comp, now)
        if since is None or now - since < settings.health_red_s:
            continue
        if name == "control":
            out.append(Trigger("health:control", None, 0, {"component": name, "detail": comp.get("detail"),
                                                           "red_since": since}))
        elif name == "runners":
            for problem in comp.get("problems") or []:
                m = _RUNNER_GONE.search(str(problem))
                if not m:
                    continue
                job = next((j for j in ro.list_jobs() if j.job_id.startswith(m.group(1))), None)
                if job is None or job.state in TERMINAL_STATES or job.job_id in taken:
                    continue
                taken.add(job.job_id)
                out.append(Trigger("orphan:RUNNING", job.job_id, 0,
                                   {"component": name, "problem": str(problem), "state": job.state.value}))
    return out


def _orphan_triggers(ro, cdb, now, runner_alive, settings, taken) -> list[Trigger]:
    last = cdb.cursor_get("orphan_scan_at", 0)
    if last and now - last < settings.orphan_scan_s:
        return []
    cdb.cursor_set("orphan_scan_at", now)
    out: list[Trigger] = []
    if cdb.cursor_get("zt_p3", 0) == 0:
        for job in ro.list_jobs(set(ORPHAN_STATES)):
            if job.job_id in taken:
                continue
            ts = ro.last_non_noise_event_ts(job.job_id)
            if ts is None or now - ts < settings.orphan_s:
                continue
            if runner_alive(job.job_id):
                continue
            bg = ro.kv_get(job.job_id, "bg_verify")
            if isinstance(bg, dict) and bg.get("status") == "running":
                continue
            taken.add(job.job_id)
            out.append(Trigger(f"orphan:{job.state.value}", job.job_id, ro.last_event_seq(job.job_id),
                               {"state": job.state.value, "idle_s": now - ts}))
        return out
    actors = ro.latest_of("liveness_actor")
    for job_id, ev in ro.latest_of("liveness_orphan").items():
        act = actors.get(job_id)
        if job_id in taken or (act is not None and act.seq > ev.seq):
            continue
        if now - _epoch(ev.ts) < settings.orphan_s:
            continue
        job = ro.get_job(job_id)
        if job is None or job.state in TERMINAL_STATES:
            continue
        taken.add(job_id)
        out.append(Trigger(f"orphan:{job.state.value}", job_id, ev.seq,
                           {"state": job.state.value, "liveness_orphan_seq": ev.seq}))
    return out


def scan(ro, cdb, *, now, health, runner_alive, settings) -> tuple[list[Trigger], int]:
    """One poll. Returns ``(triggers, new_cursor)``.

    The events cursor (``cdb.cursor_get("events")``) is NOT persisted here: the caller (daemon) stores the returned
    value with ``cursor_set("events", new_cursor)`` once the triggers are handled. On the very first run (no cursor)
    the cursor is ``ro.max_seq()`` and no event is replayed (health and orphan checks still run). ``scan`` itself persists ``zt_p3`` (set on a
    ``liveness_orphan`` event), ``orphan_scan_at`` and the ``red_since:<component>`` entries.
    """
    cursor = cdb.cursor_get("events", None)
    first_run = cursor is None
    cursor = ro.max_seq() if first_run else int(cursor)
    new_cursor = cursor
    by_job: dict[str, Trigger] = {}
    for e in ([] if first_run else ro.events_after(cursor)):
        new_cursor = max(new_cursor, e.seq)
        if e.what == "liveness_orphan":
            cdb.cursor_set("zt_p3", 1)
        trig = None
        if e.what in PARK_WHATS or e.rule in PARK_RULES or e.rule in COORD_RULES:
            if e.rule in COORD_RULES:
                job = ro.get_job(e.job_id)
                if job is not None and job.state is JobState.NEEDS_HUMAN:
                    trig = Trigger(f"needs_human:{e.rule}", e.job_id, e.seq,
                                   {"what": e.what, "rule": e.rule, "evidence": e.evidence})
        elif e.what == "liveness_ask":
            trig = Trigger("liveness_ask", e.job_id, e.seq,
                           {"what": e.what, "rule": e.rule, "evidence": e.evidence})
        if trig is not None:
            by_job.pop(e.job_id, None)
            by_job[e.job_id] = trig
    out = list(by_job.values())
    taken = set(by_job)
    out += _health_triggers(ro, cdb, now, health, settings, taken)
    out += _orphan_triggers(ro, cdb, now, runner_alive, settings, taken)
    return out, new_cursor
