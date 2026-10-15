"""Per-call allowances: a bounded call is an attempt, never a job budget or verdict."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from steward import masking
from steward.store import Store

OUTCOMES = ("completed", "allowance_exhausted", "over_allowance", "interrupted", "failed", "invalid")
CONTINUABLE = ("allowance_exhausted", "interrupted")
MAX_AUTO_CONTINUATIONS = 1
MAX_CONTINUATIONS = 3
ALLOWANCE_GROWTH = 2.0
PARTIAL_MAX_CHARS = 2000


@dataclass(frozen=True)
class CallResult:
    outcome: str
    text: str | None
    cost_usd: float | None
    allowance_usd: float | None
    reason: str = ""


class AllowanceExhausted(RuntimeError):
    """An invoker's call ended at its allowance and may contain useful partial output."""

    def __init__(self, result: CallResult):
        super().__init__(f"allowance_exhausted: {result.reason}")
        self.result = result


def classify_claude_envelope(stdout: str, *, exit_code: int, allowance_usd: float | None) -> CallResult:
    """Classify Claude's JSON result. Budget subtype detection is based on the approved plan's recorded assumption."""
    try:
        env = json.loads(stdout)
    except (TypeError, ValueError):
        return CallResult("failed" if exit_code else "invalid", None, None, allowance_usd,
                          f"exit_{exit_code}" if exit_code else "invalid_json")
    if not isinstance(env, dict):
        return CallResult("invalid", None, None, allowance_usd, "not_object")
    cost = env.get("total_cost_usd")
    cost = float(cost) if type(cost) in (int, float) else None
    text = env.get("result") if isinstance(env.get("result"), str) else None
    subtype = str(env.get("subtype") or "")
    if env.get("is_error") or exit_code != 0:
        if "budget" in subtype.lower() or (allowance_usd is not None and cost is not None
                                             and cost >= allowance_usd):
            return CallResult("allowance_exhausted", text, cost, allowance_usd, subtype or "budget")
        return CallResult("failed", text, cost, allowance_usd, subtype or f"exit_{exit_code}")
    if allowance_usd is not None and cost is not None and cost > allowance_usd:
        return CallResult("over_allowance", text, cost, allowance_usd,
                          f"{cost:.2f} > {allowance_usd:.2f}")
    return CallResult("completed", text, cost, allowance_usd)


def _partial(partial: dict | None) -> dict | None:
    if partial is None:
        return None
    text = json.dumps(masking.mask_obj(partial), ensure_ascii=False)
    if len(text) <= PARTIAL_MAX_CHARS:
        return json.loads(text)
    return {"text": masking.mask(str(partial.get("text", "")))[:PARTIAL_MAX_CHARS - 40], "truncated": True}


def checkpoint(store: Store, job_id: str, *, call_site: str, subject_key: str, result: CallResult,
               partial: dict | None, now: float) -> str:
    if result.outcome not in OUTCOMES:
        raise ValueError(f"unknown call outcome {result.outcome!r}")
    attempt = len(checkpoints(store, job_id, call_site, subject_key)) + 1
    cid = "cp-" + hashlib.sha256(f"{job_id}|{call_site}|{subject_key}|{attempt}".encode()).hexdigest()[:12]
    body = _partial(partial)
    with store.immediate() as tx:
        tx.conn.execute(
            "INSERT OR IGNORE INTO call_checkpoints (checkpoint_id, job_id, call_site, subject_key, attempt, outcome,"
            " allowance_usd, cost_usd, partial_json, reason, created_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, job_id, call_site, subject_key, attempt, result.outcome, result.allowance_usd, result.cost_usd,
             json.dumps(body) if body is not None else None, result.reason[:200], now))
    return cid


def checkpoints(store: Store, job_id: str, call_site: str, subject_key: str) -> list[dict]:
    rows = store._conn.execute("SELECT * FROM call_checkpoints WHERE job_id = ? AND call_site = ? AND subject_key = ?"
                               " ORDER BY attempt", (job_id, call_site, subject_key)).fetchall()
    return [{"checkpoint_id": r["checkpoint_id"], "attempt": r["attempt"], "outcome": r["outcome"],
             "allowance_usd": r["allowance_usd"], "cost_usd": r["cost_usd"], "reason": r["reason"],
             "partial": json.loads(r["partial_json"]) if r["partial_json"] else None,
             "created_epoch": r["created_epoch"]} for r in rows]


def _digest(cp: dict) -> str:
    return hashlib.sha256(json.dumps(cp.get("partial"), sort_keys=True).encode()).hexdigest()[:16]


def next_step(store: Store, job_id: str, *, call_site: str, subject_key: str, max_allowance_usd: float,
              source: str = "flow_investigator", now: float | None = None) -> dict:
    from steward import spend_policy

    cps = checkpoints(store, job_id, call_site, subject_key)
    if not cps or cps[-1]["outcome"] not in CONTINUABLE:
        return {"action": "stop", "allowance_usd": None, "resume_from": None,
                "reason": cps[-1]["outcome"] if cps else "no_attempt"}
    last = cps[-1]
    stops = [c for c in cps if c["outcome"] in CONTINUABLE]
    progressed = len(stops) < 2 or _digest(stops[-1]) != _digest(stops[-2])
    if len(stops) > MAX_AUTO_CONTINUATIONS and (not progressed or len(stops) > MAX_CONTINUATIONS):
        return {"action": "strategy_review", "allowance_usd": None, "resume_from": last["checkpoint_id"],
                "reason": (f"{len(stops)} interruptions; repeated exhaustion without progress" if not progressed
                           else f"{len(stops)} interruptions; continuation limit reached")}
    base = float(last["allowance_usd"] or max_allowance_usd / ALLOWANCE_GROWTH)
    allowance = round(min(float(max_allowance_usd), base * ALLOWANCE_GROWTH), 4)
    if spend_policy.enabled():
        chk = spend_policy.check_hard(store, job_id, source=source, bound_usd=allowance,
                                      now=now if now is not None else float(last["created_epoch"]))
        if not chk.ok:
            return {"action": "wait", "allowance_usd": allowance, "resume_from": last["checkpoint_id"],
                    "reason": chk.reason}
    return {"action": "continue", "allowance_usd": allowance, "resume_from": last["checkpoint_id"],
            "reason": f"{last['outcome']} at ${float(last['allowance_usd'] or 0):.2f}; partial evidence kept; "
                      f"continuing with ${allowance:.2f}"}
