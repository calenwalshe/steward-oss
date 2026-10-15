"""Coordinator's own sqlite state: episodes, tries, budget, cursors, rung state."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes(
  episode_id TEXT PRIMARY KEY, job_id TEXT, trigger TEXT, trigger_seq INTEGER,
  opened_at REAL, acted_at REAL, unstuck_at REAL, closed_at REAL, outcome TEXT,
  diagnosis TEXT, evidence_json TEXT, actions_json TEXT, rungs_json TEXT,
  model_calls INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0, ask TEXT,
  rule_proposal_id TEXT, reopened INTEGER DEFAULT 0, last_seq INTEGER,
  overturned_at REAL, overturn_why TEXT);
CREATE INDEX IF NOT EXISTS episodes_job ON episodes(job_id, opened_at);
CREATE TABLE IF NOT EXISTS tries(job_id TEXT, rung TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS budget(
  day TEXT PRIMARY KEY, usd REAL DEFAULT 0, codex_calls INTEGER DEFAULT 0,
  tg_sent INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cursor(name TEXT PRIMARY KEY, value);
CREATE TABLE IF NOT EXISTS rung_state(rung TEXT PRIMARY KEY, disabled_at REAL, why TEXT);
"""

_DAY = 86400.0
# a "full" episode: not flagged ask-only in its evidence (CASE keeps json_extract off malformed JSON)
_FULL_SQL = ("(CASE WHEN json_valid(evidence_json) THEN COALESCE(json_extract(evidence_json, '$.ask_only'), 0)"
             " ELSE 0 END) = 0")
_MARK_COLS = {
    "acted_at", "unstuck_at", "closed_at", "outcome", "diagnosis", "evidence_json",
    "actions_json", "rungs_json", "model_calls", "cost_usd", "ask", "rule_proposal_id",
    "reopened", "last_seq", "overturned_at", "overturn_why", "trigger", "trigger_seq",
}


def _day(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def _row(r) -> dict | None:
    return dict(r) if r is not None else None


def _rung_names(rungs_json) -> set[str]:
    try:
        items = json.loads(rungs_json or "[]")
    except (TypeError, ValueError):
        return set()
    out = set()
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and "rung" in it:
            out.add(str(it["rung"]))
        elif isinstance(it, str):
            out.add(it)
    return out


class CoordDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._c = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        self._c.row_factory = sqlite3.Row
        self._run(lambda c: c.execute("PRAGMA journal_mode=WAL"))
        self._run(lambda c: c.executescript(_SCHEMA))

    def _run(self, fn):
        """Run fn(conn) as one transaction; retry a locked DB 3x, 0.2 s apart."""
        for attempt in range(3):
            try:
                with self._c:
                    return fn(self._c)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 2:
                    raise
                time.sleep(0.2)

    def close_db(self) -> None:
        self._c.close()

    # episodes
    def open_episode(self, job_id, trigger, seq, now, reopened=False) -> str:
        eid = uuid.uuid4().hex[:12]
        self._run(lambda c: c.execute(
            "INSERT INTO episodes(episode_id, job_id, trigger, trigger_seq, last_seq,"
            " opened_at, reopened) VALUES(?,?,?,?,?,?,?)",
            (eid, job_id, trigger, seq, seq, now, 1 if reopened else 0)))
        return eid

    def open_for(self, job_id) -> dict | None:
        return self._run(lambda c: _row(c.execute(
            "SELECT * FROM episodes WHERE job_id=? AND closed_at IS NULL"
            " ORDER BY opened_at DESC LIMIT 1", (job_id,)).fetchone()))

    def attach(self, eid, seq, note=None) -> None:
        self._run(lambda c: c.execute(
            "UPDATE episodes SET last_seq=? WHERE episode_id=?", (seq, eid)))

    def mark(self, eid, **cols) -> None:
        bad = set(cols) - _MARK_COLS
        if bad:
            raise ValueError(f"unknown episode columns: {sorted(bad)}")
        if not cols:
            return
        sets = ", ".join(f"{k}=?" for k in cols)
        self._run(lambda c: c.execute(
            f"UPDATE episodes SET {sets} WHERE episode_id=?", (*cols.values(), eid)))

    def close(self, eid, outcome, diagnosis, actions, now, *, rungs=None,
              rule_proposal_id=None, ask=None) -> bool:
        """Close an OPEN episode; False (and nothing written) when it is unknown or already closed."""
        return self._run(lambda c: c.execute(
            "UPDATE episodes SET closed_at=?, outcome=?, diagnosis=?, actions_json=?,"
            " rungs_json=COALESCE(?, rungs_json),"
            " rule_proposal_id=COALESCE(?, rule_proposal_id), ask=COALESCE(?, ask)"
            " WHERE episode_id=? AND closed_at IS NULL",
            (now, outcome, diagnosis, json.dumps(list(actions or [])),
             None if rungs is None else json.dumps(rungs),
             rule_proposal_id, ask, eid)).rowcount > 0)

    def merge_evidence(self, eid, fields: dict) -> dict:
        """Set (value not None) or remove (None) top-level keys of `evidence_json` in ONE statement, so writers on
        different connections never lose each other's keys. Returns the evidence as stored afterwards."""
        sets, dels, args = [], [], []
        for key, val in fields.items():
            path = '$."' + str(key).replace('"', '') + '"'
            if val is None:
                dels.append(path)
            else:
                sets.append(path)
                args.append(json.dumps(val, default=str))
        expr = "COALESCE(NULLIF(evidence_json, ''), '{}')"
        if sets:
            expr = f"json_set({expr}, " + ", ".join(f"'{p}', json(?)" for p in sets) + ")"
        if dels:
            expr = f"json_remove({expr}, " + ", ".join(f"'{p}'" for p in dels) + ")"

        def go(c):
            c.execute(f"UPDATE episodes SET evidence_json={expr} WHERE episode_id=?", (*args, eid))
            r = c.execute("SELECT evidence_json FROM episodes WHERE episode_id=?", (eid,)).fetchone()
            return r["evidence_json"] if r is not None else None
        raw = self._run(go)
        try:
            ev = json.loads(raw or "{}")
        except (TypeError, ValueError):
            ev = {}
        return ev if isinstance(ev, dict) else {}

    def episodes_since(self, ts) -> list[dict]:
        return self._run(lambda c: [dict(r) for r in c.execute(
            "SELECT * FROM episodes WHERE opened_at>=? ORDER BY opened_at, rowid", (ts,))])

    def episodes_for_job(self, job_id, since, *, full_only: bool = False) -> int:
        """Episodes for the key opened since `since`; `full_only` skips ask-only ones (they never act)."""
        extra = f" AND {_FULL_SQL}" if full_only else ""
        return self._run(lambda c: c.execute(
            "SELECT COUNT(*) FROM episodes WHERE job_id=? AND opened_at>=?" + extra,
            (job_id, since)).fetchone()[0])

    def episodes_for_job_today(self, job_id, now) -> int:
        return self.episodes_for_job(job_id, now - _DAY)

    def last_closed(self, job_id, *, full_only: bool = False) -> dict | None:
        extra = f" AND {_FULL_SQL}" if full_only else ""
        return self._run(lambda c: _row(c.execute(
            "SELECT * FROM episodes WHERE job_id=? AND closed_at IS NOT NULL" + extra +
            " ORDER BY closed_at DESC, rowid DESC LIMIT 1", (job_id,)).fetchone()))

    def last_closed_at(self, job_id) -> float | None:
        r = self.last_closed(job_id)
        return r["closed_at"] if r else None

    # tries
    def try_count(self, job_id, rung, now, window_s=86400) -> int:
        return self._run(lambda c: c.execute(
            "SELECT COUNT(*) FROM tries WHERE job_id=? AND rung=? AND ts>?",
            (job_id, rung, now - window_s)).fetchone()[0])

    def record_try(self, job_id, rung, now) -> None:
        self._run(lambda c: c.execute(
            "INSERT INTO tries(job_id, rung, ts) VALUES(?,?,?)", (job_id, rung, now)))

    # budget
    def budget_today(self, now) -> dict:
        r = self._run(lambda c: c.execute(
            "SELECT usd, codex_calls, tg_sent FROM budget WHERE day=?", (_day(now),)).fetchone())
        if r is None:
            return {"usd": 0, "codex_calls": 0, "tg_sent": 0}
        return {"usd": r["usd"], "codex_calls": r["codex_calls"], "tg_sent": r["tg_sent"]}

    def _bump(self, now, usd=0.0, codex_calls=0, tg_sent=0) -> None:
        day = _day(now)

        def go(c):
            c.execute("INSERT OR IGNORE INTO budget(day) VALUES(?)", (day,))
            c.execute("UPDATE budget SET usd=usd+?, codex_calls=codex_calls+?,"
                      " tg_sent=tg_sent+? WHERE day=?", (usd, codex_calls, tg_sent, day))
        self._run(go)

    def spend(self, now, usd=0, codex_calls=0) -> None:
        self._bump(now, usd=usd, codex_calls=codex_calls)

    def note_tg(self, now) -> None:
        self._bump(now, tg_sent=1)

    # cursor
    def cursor_get(self, name, default=0):
        r = self._run(lambda c: c.execute(
            "SELECT value FROM cursor WHERE name=?", (name,)).fetchone())
        return default if r is None else r["value"]

    def cursor_set(self, name, value) -> None:
        self._run(lambda c: c.execute(
            "INSERT INTO cursor(name, value) VALUES(?,?)"
            " ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, value)))

    # overturn / rungs
    def overturn(self, eid, why, now) -> bool:
        def go(c):
            cur = c.execute(
                "UPDATE episodes SET overturned_at=?, overturn_why=?"
                " WHERE episode_id=? AND overturned_at IS NULL", (now, why, eid))
            return cur.rowcount > 0
        return self._run(go)

    def overturns_in(self, window_s, now, rung=None) -> int:
        rows = self._run(lambda c: c.execute(
            "SELECT rungs_json FROM episodes WHERE overturned_at IS NOT NULL"
            " AND overturned_at>? AND overturned_at<=?", (now - window_s, now)).fetchall())
        if rung is None:
            return len(rows)
        return sum(1 for r in rows if rung in _rung_names(r["rungs_json"]))

    def actions_in(self, window_s, now) -> int:
        rows = self._run(lambda c: c.execute(
            "SELECT actions_json FROM episodes WHERE closed_at IS NOT NULL"
            " AND closed_at>? AND closed_at<=?", (now - window_s, now)).fetchall())
        n = 0
        for r in rows:
            try:
                if json.loads(r["actions_json"] or "[]"):
                    n += 1
            except (TypeError, ValueError):
                pass
        return n

    def disable_rung(self, rung, why, now) -> None:
        self._run(lambda c: c.execute(
            "INSERT INTO rung_state(rung, disabled_at, why) VALUES(?,?,?)"
            " ON CONFLICT(rung) DO UPDATE SET disabled_at=excluded.disabled_at,"
            " why=excluded.why", (rung, now, why)))

    def enable_rung(self, rung) -> None:
        self._run(lambda c: c.execute("DELETE FROM rung_state WHERE rung=?", (rung,)))

    def rung_disabled(self, rung) -> bool:
        return self._run(lambda c: c.execute(
            "SELECT 1 FROM rung_state WHERE rung=? AND disabled_at IS NOT NULL",
            (rung,)).fetchone()) is not None
