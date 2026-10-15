"""Decision trace and run flags (continuous job ownership §14; Plan 1).

Insert-only rows over the Store: who decided what, on which blocker episode, authority revision, evidence and
model/prompt/policy versions, and which intent's receipt proves the effect. A proposal, an attempted action, a
completed effect, a model signal and a discarded (stale) answer are distinct stages; outcomes are appended later.
Unknown causal links stay unknown: nothing here infers one. Masked and capped at write time; bounded retention:
RETENTION_DAYS, at most MAX_ROWS_PER_JOB per job, never while a run flag is open (a database trigger) and not until
RETENTION_DAYS after it closes. Expired or never-recorded input is reported missing, never reconstructed.
"""
from __future__ import annotations

import json
import uuid

from steward import masking, sanitize
from steward.actor import Actor, ActorRef
from steward.models import Judgment
from steward.store import Store

RETENTION_DAYS = 30
MAX_ROWS_PER_JOB = 5000
FIELD_MAX_CHARS = 2000
DESCRIPTION_MAX_CHARS = 1000
PURGE_EVERY_S = 3600.0
STAGES = ("signal", "proposal", "attempted", "effect", "discarded")
SIGNAL_USES = ("none", "used", "ignored", "overruled")
SIGNAL_KINDS = ("jev_concern", "flow_finding")
_PURGE_JOB, _PURGE_KEY = "_trace", "purged_at"


def run_id(store: Store, job_id: str) -> str:
    """`<job>:a<n>`: n worker attempts have started (a0 before the first)."""
    n = store._conn.execute("SELECT COUNT(*) FROM events WHERE job_id = ? AND what = 'worker attempt started'",
                            (job_id,)).fetchone()[0]
    return f"{job_id}:a{int(n)}"


def _capped(obj) -> str:
    s = json.dumps(sanitize.obj(obj), default=str, sort_keys=True)
    if len(s) > FIELD_MAX_CHARS:
        s = json.dumps({"truncated": True, "head": s[: FIELD_MAX_CHARS - 40]})
    return s


def record(store: Store, *, job_id: str, actor: ActorRef, stage: str, decision: str, now: float,
           blocker_id: str | None = None, evidence_refs=(), versions: dict | None = None, signal: dict | None = None,
           signal_use: str = "none", intent_key: str | None = None, parent_id: str | None = None,
           authority_rev: int | None = None) -> str:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    if signal_use not in SIGNAL_USES:
        raise ValueError(f"unknown signal use {signal_use!r}")
    masking.arm_job(store, job_id)
    did = "dec-" + uuid.uuid4().hex[:16]
    refs = [sanitize.text(r, max_chars=200) for r in evidence_refs][:24]
    with store._conn:
        store._conn.execute(
            "INSERT INTO decision_records (decision_id, job_id, run_id, blocker_id, actor_kind, actor_id,"
            " authority_rev, stage, decision, signal_use, evidence_json, versions_json, signal_json, intent_key,"
            " parent_id, created_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, job_id, run_id(store, job_id), blocker_id, actor.kind.value, actor.id or "-", authority_rev, stage,
             sanitize.text(decision, max_chars=400), signal_use, _capped(refs), _capped(versions or {}),
             _capped(signal) if signal is not None else None, intent_key, parent_id, now))
    return did


def safe_record(store: Store, **kw) -> str | None:
    """`record` that never raises into a controller tick (tracing is diagnostic, never control)."""
    try:
        return record(store, **kw)
    except Exception:  # noqa: BLE001
        return None


def outcome(store: Store, decision_id: str, *, observed: dict, now: float) -> None:
    r = store._conn.execute("SELECT job_id FROM decision_records WHERE decision_id = ?", (decision_id,)).fetchone()
    if r is None:
        raise KeyError(f"unknown decision {decision_id}")
    masking.arm_job(store, r["job_id"])
    with store._conn:
        store._conn.execute("INSERT INTO decision_outcomes (decision_id, job_id, observed_json, created_epoch)"
                            " VALUES (?,?,?,?)", (decision_id, r["job_id"], _capped(sanitize.capture("decision", {"observed": observed})["observed"]), now))


def outcomes_for_blocker(store: Store, blocker_id: str, *, observed: dict, now: float) -> int:
    ids = [r[0] for r in store._conn.execute("SELECT decision_id FROM decision_records WHERE blocker_id = ?",
                                              (blocker_id,)).fetchall()]
    for did in ids:
        outcome(store, did, observed=observed, now=now)
    return len(ids)


def safe_outcomes(store: Store, blocker_id: str, *, observed: dict, now: float) -> int:
    try:
        return outcomes_for_blocker(store, blocker_id, observed=observed, now=now)
    except Exception:  # noqa: BLE001
        return 0


def _row(r) -> dict:
    d = dict(r)
    d["evidence"] = json.loads(d.pop("evidence_json") or "[]")
    d["versions"] = json.loads(d.pop("versions_json") or "{}")
    d["signal"] = json.loads(d["signal_json"]) if d.get("signal_json") else None
    d.pop("signal_json", None)
    return d


def trace(store: Store, job_id: str, *, run: str | None = None) -> list[dict]:
    q, a = "SELECT * FROM decision_records WHERE job_id = ?", [job_id]
    if run:
        q, a = q + " AND run_id = ?", [job_id, run]
    rows = [_row(r) for r in store._conn.execute(q + " ORDER BY created_epoch, rowid", a).fetchall()]
    for d in rows:
        d["outcomes"] = [{"observed": json.loads(o["observed_json"]), "at": o["created_epoch"]}
                         for o in store._conn.execute("SELECT * FROM decision_outcomes WHERE decision_id = ?"
                                                      " ORDER BY seq", (d["decision_id"],)).fetchall()]
    return rows


# -- run flags ---------------------------------------------------------------------------------------------------


def _flag(store: Store, flag_id: str) -> dict | None:
    r = store._conn.execute("SELECT * FROM run_flags WHERE flag_id = ?", (flag_id,)).fetchone()
    return None if r is None else dict(r)


def flag_run(store: Store, job_id: str, *, description: str, actor: ActorRef, now: float,
             run: str | None = None) -> tuple[dict, bool]:
    """Open a flag on the job's current run, or add a note to the open one (no duplicate investigation)."""
    run = run or run_id(store, job_id)
    masking.arm_job(store, job_id)
    desc = sanitize.text(" ".join(str(description).split()), max_chars=DESCRIPTION_MAX_CHARS)
    with store.immediate() as tx:
        r = tx.conn.execute("SELECT * FROM run_flags WHERE job_id = ? AND run_id = ? AND state != 'closed'"
                            " ORDER BY created_epoch LIMIT 1", (job_id, run)).fetchone()
        if r is not None:
            tx.conn.execute("INSERT INTO run_flag_notes (flag_id, actor_kind, actor_id, note, created_epoch)"
                            " VALUES (?,?,?,?,?)", (r["flag_id"], actor.kind.value, actor.id or "-", desc, now))
            tx.append_event(job_id, what="run_flag_evidence_added",
                            evidence={"flag_id": r["flag_id"], "by": actor.label()}, rule="run_flag",
                            judgment=Judgment.code)
            return dict(r), False
        fid = "flag-" + uuid.uuid4().hex[:12]
        tx.conn.execute("INSERT INTO run_flags (flag_id, job_id, run_id, actor_kind, actor_id, description, state,"
                        " created_epoch) VALUES (?,?,?,?,?,?,'open',?)",
                        (fid, job_id, run, actor.kind.value, actor.id or "-", desc, now))
        tx.append_event(job_id, what="run_flagged",
                        evidence={"flag_id": fid, "run_id": run, "by": actor.label(), "description": desc[:200]},
                        rule="run_flag", judgment=Judgment.human if actor.kind == Actor.OWNER else Judgment.code)
    return _flag(store, fid), True


def add_flag_evidence(store: Store, flag_id: str, *, note: str, actor: ActorRef, now: float) -> None:
    f = _flag(store, flag_id)
    if f is None:
        raise KeyError(f"unknown flag {flag_id}")
    masking.arm_job(store, f["job_id"])
    with store._conn:
        store._conn.execute("INSERT INTO run_flag_notes (flag_id, actor_kind, actor_id, note, created_epoch)"
                            " VALUES (?,?,?,?,?)", (flag_id, actor.kind.value, actor.id or "-",
                                                    sanitize.text(note, max_chars=DESCRIPTION_MAX_CHARS), now))


def mark_investigating(store: Store, flag_id: str, *, actor: ActorRef, now: float) -> dict:
    with store._conn:
        store._conn.execute("UPDATE run_flags SET state = 'investigating' WHERE flag_id = ? AND state = 'open'",
                            (flag_id,))
    return _flag(store, flag_id)


def close_flag(store: Store, flag_id: str, *, actor: ActorRef, resolution: str, now: float) -> dict:
    f = _flag(store, flag_id)
    if f is None:
        raise KeyError(f"unknown flag {flag_id}")
    masking.arm_job(store, f["job_id"])
    with store._conn:
        store._conn.execute("UPDATE run_flags SET state = 'closed', closed_epoch = ?, resolution = ?"
                            " WHERE flag_id = ? AND state != 'closed'",
                            (now, masking.mask(str(resolution))[:DESCRIPTION_MAX_CHARS], flag_id))
    store.append_event(f["job_id"], what="run_flag_closed", evidence={"flag_id": flag_id, "by": actor.label()},
                       rule="run_flag", judgment=Judgment.human if actor.kind == Actor.OWNER else Judgment.code)
    return _flag(store, flag_id)


def open_flags(store: Store, job_id: str | None = None) -> list[dict]:
    q, a = "SELECT * FROM run_flags WHERE state != 'closed'", []
    if job_id:
        q, a = q + " AND job_id = ?", [job_id]
    return [dict(r) for r in store._conn.execute(q + " ORDER BY created_epoch", a).fetchall()]


def preserved(store: Store, job_id: str, *, now: float) -> bool:
    keep_after = now - RETENTION_DAYS * 86400.0
    r = store._conn.execute("SELECT 1 FROM run_flags WHERE job_id = ? AND (state != 'closed' OR closed_epoch >= ?)"
                            " LIMIT 1", (job_id, keep_after)).fetchone()
    return r is not None


def purge(store: Store, *, now: float) -> int:
    """Bounded retention: rows older than RETENTION_DAYS, and the oldest beyond MAX_ROWS_PER_JOB, of jobs no flag
    preserves. Returns the number of decision rows removed (their outcomes go with them)."""
    cutoff = now - RETENTION_DAYS * 86400.0
    removed = 0
    for (jid,) in store._conn.execute("SELECT DISTINCT job_id FROM decision_records").fetchall():
        if preserved(store, jid, now=now):
            continue
        old = {r[0] for r in store._conn.execute("SELECT decision_id FROM decision_records WHERE job_id = ? AND"
                                                 " created_epoch < ?", (jid, cutoff)).fetchall()}
        extra = {r[0] for r in store._conn.execute("SELECT decision_id FROM decision_records WHERE job_id = ?"
                                                   " ORDER BY created_epoch DESC, rowid DESC LIMIT -1 OFFSET ?",
                                                   (jid, MAX_ROWS_PER_JOB)).fetchall()}
        ids = sorted(old | extra)
        if not ids:
            continue
        marks = ",".join("?" * len(ids))
        with store._conn:
            store._conn.execute(f"DELETE FROM decision_outcomes WHERE decision_id IN ({marks})", ids)
            store._conn.execute(f"DELETE FROM decision_records WHERE decision_id IN ({marks})", ids)
        removed += len(ids)
    return removed


def maybe_purge(store: Store, *, now: float) -> int:
    try:
        last = store.kv_get(_PURGE_JOB, _PURGE_KEY)
        if last is not None and now - float(last) < PURGE_EVERY_S:
            return 0
        store.kv_set(_PURGE_JOB, _PURGE_KEY, now)
        return purge(store, now=now)
    except Exception:  # noqa: BLE001 - retention never breaks the sweep
        return 0
