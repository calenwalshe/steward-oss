"""Bounded, sanitized receipts for Jev calls and network-isolated replay."""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import time

from steward import masking
from steward.models import Judgment
from steward.store import Store

LANES = ("triage", "risk", "lane_triage", "evidence_support")
PACK_MAX_CHARS = 8000
PURGE_EVERY_S = 3600.0
_CALL = contextvars.ContextVar("steward_jev_call", default=None)


class ReplayMiss(LookupError):
    pass


def call(store: Store, job_id: str, lane: str, *, mode: str = ""):
    from contextlib import contextmanager

    @contextmanager
    def context():
        token = _CALL.set({"store": store, "job_id": job_id, "lane": lane, "mode": mode})
        try:
            yield
        finally:
            _CALL.reset(token)
    return context()


def _digest(data: bytes | str) -> str:
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()[:16]


def pack_id(questions: dict) -> str:
    return _digest(json.dumps(questions, sort_keys=True, ensure_ascii=False))


def _bounded(obj) -> dict:
    from steward import decision_trace

    raw = json.dumps(masking.mask_obj(obj), ensure_ascii=False)
    if len(raw) <= decision_trace.FIELD_MAX_CHARS:
        return json.loads(raw)
    return {"truncated": True, "excerpt": masking.mask(raw)[:decision_trace.FIELD_MAX_CHARS - 60]}


def _answers(payload) -> dict:
    return {q: {k: a[k] for k in ("choice", "confidence", "probabilities") if k in a}
            for q, a in ((payload or {}).get("answers") or {}).items() if isinstance(a, dict)}


def _record(ctx, body, response, error, elapsed, timeout):
    from steward import decision_trace, spend_policy
    from steward.actor import Actor, ActorRef

    store, job_id, lane = ctx["store"], ctx["job_id"], ctx["lane"]
    masking.arm_job(store, job_id)
    req = json.loads(body)
    questions = req.get("questions") or {}
    pid = pack_id(questions)
    state = _bounded(req.get("state") or {})
    status, http, answers = "ok", None, {}
    if error is not None:
        status = "transport_error"
    else:
        http = int(response.status_code)
        if http != 200:
            status = "http_error"
        else:
            try:
                answers = _answers(json.loads(response.body))
                status = "ok" if answers else "malformed"
            except (TypeError, ValueError):
                status = "malformed"
    primary = next(iter(questions), lane)
    first = answers.get(primary) or {}
    now = time.time()
    did = decision_trace.record(store, job_id=job_id, actor=ActorRef(Actor.JEV, "jev"), stage="signal",
                                decision=f"{lane}: {first.get('choice', status)}"[:200], now=now,
                                versions={"lane": lane, "pack": pid, "model": str(req.get("model") or "")},
                                signal={"choice": first.get("choice"), "confidence": first.get("confidence"),
                                        "status": status})
    rid = "rcp-" + _digest(f"{job_id}|{lane}|{now}|{_digest(body)}|{did}")
    qtext = json.dumps(questions, sort_keys=True, ensure_ascii=False)
    with store.immediate() as tx:
        if len(qtext) <= PACK_MAX_CHARS:
            tx.conn.execute("INSERT OR IGNORE INTO judgment_packs (pack_id,lane,questions_json,created_epoch)"
                            " VALUES (?,?,?,?)", (pid, lane, qtext, now))
        else:
            tx.append_event(job_id, what="jev_pack_oversize", evidence={"pack_id": pid, "chars": len(qtext)},
                            rule="judgment_ledger", judgment=Judgment.code)
        tx.conn.execute("INSERT INTO judgment_receipts (receipt_id,job_id,lane,pack_id,model,config_json,"
                        "request_digest,state_json,status,http_status,response_json,latency_ms,decision_id,created_epoch)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (rid, job_id, lane, pid, str(req.get("model") or ""),
                         json.dumps({"timeout_s": timeout, "mode": ctx.get("mode", ""),
                                     "env_model": os.environ.get("TYPESAFE_MODEL")}), _digest(body),
                         json.dumps(state), status, http,
                         json.dumps(_bounded({"answers": answers})) if answers else None, elapsed, did, now))
    spend_policy.record(store, entry_id=f"jev:{rid}", job_id=job_id, source="jev", usd=None,
                        price_basis="unknown", model=str(req.get("model") or ""), outcome=status, now=now)


class RecordingTransport:
    def __init__(self, inner, store_fn=None):
        self.inner = inner

    def post(self, url, body, headers, timeout):
        ctx = _CALL.get()
        started = time.monotonic()
        try:
            response = self.inner.post(url, body, headers, timeout)
        except Exception as exc:
            if ctx:
                try: _record(ctx, body, None, exc, int((time.monotonic() - started) * 1000), timeout)
                except Exception: pass  # records never alter Jev's failure behavior
            raise
        if ctx:
            try: _record(ctx, body, response, None, int((time.monotonic() - started) * 1000), timeout)
            except Exception: pass
        return response

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _row(row):
    d = dict(row)
    d["state"] = json.loads(d.pop("state_json") or "{}")
    d["response"] = json.loads(d.pop("response_json")) if d.get("response_json") else None
    d["config"] = json.loads(d.pop("config_json") or "{}")
    return d


def receipts(store: Store, *, job_id=None, lane=None, since=None) -> list[dict]:
    query, args = "SELECT * FROM judgment_receipts WHERE 1=1", []
    for col, value in (("job_id", job_id), ("lane", lane)):
        if value is not None: query, args = query + f" AND {col} = ?", args + [value]
    if since is not None: query, args = query + " AND created_epoch >= ?", args + [since]
    return [_row(r) for r in store._conn.execute(query + " ORDER BY created_epoch,rowid", args).fetchall()]


def pack(store: Store, pid: str):
    r = store._conn.execute("SELECT lane,questions_json FROM judgment_packs WHERE pack_id=?", (pid,)).fetchone()
    return None if r is None else {"pack_id": pid, "lane": r["lane"], "questions": json.loads(r["questions_json"])}


def replay_body(store: Store, receipt: dict) -> tuple[bytes, bool]:
    p = pack(store, receipt["pack_id"]) or {"questions": {}}
    body = json.dumps({"model": receipt["model"], "state": receipt["state"], "questions": p["questions"]},
                      ensure_ascii=False).encode()
    return body, _digest(body) == receipt["request_digest"]


class ReplayTransport:
    def __init__(self, recorded): self._by_digest = {r["request_digest"]: r for r in recorded}; self.served = []
    def post(self, url, body, headers, timeout):
        row = self._by_digest.get(_digest(body))
        if row is None: raise ReplayMiss("no recorded receipt for this request")
        self.served.append(row["receipt_id"])
        if row["status"] == "transport_error": raise ConnectionError("replayed transport error")
        from steward.jev_observer import TransportResponse
        payload = json.dumps(row["response"] or {}).encode()
        return TransportResponse(int(row["http_status"] or (200 if row["status"] == "ok" else 500)), payload)


def status(store: Store) -> dict:
    from steward import decision_trace
    from steward.jev_observer import DEFAULT_MODEL
    by_lane = {r["lane"]: r["n"] for r in store._conn.execute(
        "SELECT lane,COUNT(*) n FROM judgment_receipts GROUP BY lane").fetchall()}
    models = {r["model"]: ("unpinned alias" if r["model"] == DEFAULT_MODEL else "pinned") for r in store._conn.execute(
        "SELECT DISTINCT model FROM judgment_receipts").fetchall()}
    oldest = store._conn.execute("SELECT MIN(created_epoch) FROM judgment_receipts").fetchone()[0]
    return {"receipts": sum(by_lane.values()), "by_lane": by_lane, "models": models, "oldest_epoch": oldest,
            "retention": {"days": decision_trace.RETENTION_DAYS, "max_rows_per_job": decision_trace.MAX_ROWS_PER_JOB,
                          "field_max_chars": decision_trace.FIELD_MAX_CHARS}}


def purge(store: Store, *, now: float) -> int:
    from steward import decision_trace
    cutoff = now - decision_trace.RETENTION_DAYS * 86400
    total = 0
    for (jid,) in store._conn.execute("SELECT DISTINCT job_id FROM judgment_receipts").fetchall():
        if decision_trace.preserved(store, jid, now=now): continue
        ids = [r[0] for r in store._conn.execute("SELECT receipt_id FROM judgment_receipts WHERE job_id=?"
                                                  " ORDER BY created_epoch DESC,rowid DESC", (jid,)).fetchall()]
        old = {r[0] for r in store._conn.execute("SELECT receipt_id FROM judgment_receipts WHERE job_id=? AND"
                                                  " created_epoch<?", (jid, cutoff)).fetchall()}
        doomed = old | set(ids[decision_trace.MAX_ROWS_PER_JOB:])
        if doomed:
            marks = ",".join("?" * len(doomed))
            with store.immediate() as tx:
                tx.conn.execute(f"DELETE FROM eval_labels WHERE receipt_id IN ({marks})", list(doomed))
                tx.conn.execute(f"DELETE FROM judgment_receipts WHERE receipt_id IN ({marks})", list(doomed))
            total += len(doomed)
    return total
