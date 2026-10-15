"""Idempotent action intents, claims and receipts (continuous job ownership §4; Plan 1).

Every side-effecting ownership action (review lock, reopen, worker launch, owner notice) claims a deterministic key
inside one IMMEDIATE transaction before acting and records a receipt after. A repeat of the same snapshot finds the
key and does nothing. A claim whose lease expired in another process is UNCERTAIN: its effect may or may not have
happened, so it is reconciled by a per-kind checker against receipts and actual worker/store state — never blindly
retried, never a second writer (spec §4). `reclaimable=True` turns a key into a re-entrant lock (one review per job).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Callable

from steward.store import Store

PROCESS_OWNER = uuid.uuid4().hex
STATES = ("claimed", "done", "failed", "abandoned", "uncertain")
FINAL = ("done", "failed", "abandoned")
_CHECKERS: dict[str, Callable] = {}
_RECLAIMABLE: set[str] = set()


@dataclass(frozen=True)
class Intent:
    key: str
    job_id: str
    kind: str
    blocker_id: str | None
    decision_id: str | None
    params: dict
    state: str
    owner: str
    lease_expires: float
    attempt: int
    authority_rev: int
    evidence_digest: str
    created_epoch: float
    updated_epoch: float
    receipt: dict | None
    reason: str | None


def _row(r) -> Intent:
    return Intent(r["intent_key"], r["job_id"], r["kind"], r["blocker_id"], r["decision_id"],
                  json.loads(r["params_json"] or "{}"), r["state"], r["owner"], float(r["lease_expires"]),
                  int(r["attempt"]), int(r["authority_rev"]), r["evidence_digest"], float(r["created_epoch"]),
                  float(r["updated_epoch"]), json.loads(r["receipt_json"]) if r["receipt_json"] else None, r["reason"])


def intent_key(job_id: str, kind: str, *parts: object) -> str:
    h = hashlib.sha256(json.dumps([str(p) for p in parts]).encode()).hexdigest()[:16]
    return f"{kind}:{job_id}:{h}"


def get(store: Store, key: str) -> Intent | None:
    r = store._conn.execute("SELECT * FROM action_intents WHERE intent_key = ?", (key,)).fetchone()
    return None if r is None else _row(r)


def for_job(store: Store, job_id: str, kind: str | None = None) -> list[Intent]:
    q, a = "SELECT * FROM action_intents WHERE job_id = ?", [job_id]
    if kind:
        q, a = q + " AND kind = ?", [job_id, kind]
    return [_row(r) for r in store._conn.execute(q + " ORDER BY created_epoch, rowid", a).fetchall()]


def claim(store: Store, *, key: str, job_id: str, kind: str, lease_s: float, now: float, owner: str = PROCESS_OWNER,
          params: dict | None = None, authority_rev: int = 0, evidence_digest: str = "", blocker_id: str | None = None,
          decision_id: str | None = None, reclaimable: bool = False) -> Intent | None:
    """The claimed intent, or None when this key must not be acted on now (held, done, failed or uncertain)."""
    with store.immediate() as tx:
        r = tx.conn.execute("SELECT * FROM action_intents WHERE intent_key = ?", (key,)).fetchone()
        if r is None:
            tx.conn.execute(
                "INSERT INTO action_intents (intent_key, job_id, kind, blocker_id, decision_id, params_json, state,"
                " owner, lease_expires, attempt, authority_rev, evidence_digest, created_epoch, updated_epoch)"
                " VALUES (?,?,?,?,?,?,'claimed',?,?,1,?,?,?,?)",
                (key, job_id, kind, blocker_id, decision_id, json.dumps(params or {}), owner, now + lease_s,
                 int(authority_rev), evidence_digest, now, now))
        else:
            state, holder, expires = r["state"], r["owner"], float(r["lease_expires"])
            if state == "claimed" and holder == owner and reclaimable:
                tx.conn.execute("UPDATE action_intents SET lease_expires = ?, updated_epoch = ? WHERE intent_key = ?",
                                (now + lease_s, now, key))
            elif state == "claimed" and expires > now:
                return None
            elif state == "claimed" and not reclaimable:
                tx.conn.execute("UPDATE action_intents SET state = 'uncertain', reason = 'lease_expired',"
                                " updated_epoch = ? WHERE intent_key = ?", (now, key))
                return None
            elif state == "uncertain" or (state in ("done", "failed") and not reclaimable):
                return None
            else:  # reclaimable final/expired lock, or a retryable (abandoned) action
                tx.conn.execute(
                    "UPDATE action_intents SET state = 'claimed', owner = ?, lease_expires = ?, attempt = attempt + 1,"
                    " params_json = ?, authority_rev = ?, evidence_digest = ?, updated_epoch = ?, receipt_json = NULL,"
                    " reason = NULL WHERE intent_key = ?",
                    (owner, now + lease_s, json.dumps(params or {}), int(authority_rev), evidence_digest, now, key))
        r = tx.conn.execute("SELECT * FROM action_intents WHERE intent_key = ?", (key,)).fetchone()
    return _row(r)


def _finish(store: Store, key: str, state: str, *, owner: str, now: float, receipt=None, reason=None) -> Intent | None:
    with store.immediate() as tx:
        cur = tx.conn.execute("UPDATE action_intents SET state = ?, receipt_json = ?, reason = ?, updated_epoch = ?"
                              " WHERE intent_key = ? AND state = 'claimed' AND owner = ?",
                              (state, json.dumps(receipt) if receipt is not None else None, reason, now, key, owner))
        if cur.rowcount != 1:
            return None
    return get(store, key)


def complete(store: Store, key: str, *, receipt: dict, now: float, owner: str = PROCESS_OWNER) -> Intent | None:
    return _finish(store, key, "done", owner=owner, now=now, receipt=receipt)


def fail(store: Store, key: str, *, reason: str, now: float, owner: str = PROCESS_OWNER,
         retryable: bool = False) -> Intent | None:
    return _finish(store, key, "abandoned" if retryable else "failed", owner=owner, now=now, reason=reason[:300])


def register_checker(kind: str, fn: Callable[[Store, Intent], tuple[str, dict]], *, reclaimable: bool = False) -> None:
    """Register a reconciliation checker; reclaimable actions are safe to resume after a lost lease."""
    _CHECKERS[kind] = fn
    (_RECLAIMABLE.add if reclaimable else _RECLAIMABLE.discard)(kind)


def reconcile_uncertain(store: Store, *, now: float, owner: str = PROCESS_OWNER) -> list[Intent]:
    """Reconcile expired/uncertain actions. Checker errors and unknown outcomes fail closed."""
    out = []
    rows = store._conn.execute("SELECT * FROM action_intents WHERE state = 'uncertain' OR"
                               " (state = 'claimed' AND lease_expires <= ?)", (now,)).fetchall()
    for r in rows:
        it = _row(r)
        if it.state == "claimed" and it.owner == owner:
            continue
        fn = _CHECKERS.get(it.kind)
        try:
            verdict, detail = fn(store, it) if fn else ("unknown", {})
        except Exception:  # noqa: BLE001 - unreadable fate is unknown
            verdict, detail = "unknown", {}
        if verdict in ("done", "abandoned"):
            with store.immediate() as tx:
                tx.conn.execute("UPDATE action_intents SET state = ?, receipt_json = ?, reason = ?, updated_epoch = ?"
                                " WHERE intent_key = ? AND state IN ('claimed','uncertain')",
                                (verdict, json.dumps(detail) if verdict == "done" else None,
                                 None if verdict == "done" else str(detail.get("reason", "reconciled"))[:300],
                                 now, it.key))
            fin = get(store, it.key)
            if fin is not None:
                out.append(fin)
        elif it.state == "claimed" and it.kind not in _RECLAIMABLE:
            with store.immediate() as tx:
                tx.conn.execute("UPDATE action_intents SET state='uncertain', reason='lease_expired', updated_epoch=?"
                                " WHERE intent_key=? AND state='claimed'", (now, it.key))
            fin = get(store, it.key)
            if fin is not None:
                out.append(fin)
    return out
