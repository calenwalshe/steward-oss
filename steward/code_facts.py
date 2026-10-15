"""The deterministic code-fact floor (v2.6, SUP-06): model-free detectors that turn store events into Arbiter facts.

Job 14305aeb sat in NEEDS_HUMAN with Jev down (TypeSafe HTTP 402) and nothing else watching for the trouble Jev
would have flagged. These detectors read only the Store — no transcript, no model — so they fire whether or not
Jev is available:

  finish_check_unverified    the finish check timed out or could not run (steward.stability, SUP-01)
  finish_bounces_exhausted   the finish line was bounced MAX_FINISH_BOUNCES times and the job escalated
  new_failures_repeated      the same failing test survived a verification send-back AND the verify-round budget is spent (SRD-08)
  permission_ask_repeated    the same normalized permission ask was opened >= PERMISSION_REPEAT times
  review_watchdog_expired    a tech-lead review did not finish inside its time limit (SUP-13)

Every fact goes to the Arbiter with source `code_facts` (route: `outer`, the tech lead) and is idempotent per
(job, kind, evidence key): `step()` remembers what it already submitted in kv and never submits it again.
"""

from __future__ import annotations

import hashlib
import re

from steward import approvals as approvals_module
from steward.models import Judgment
from steward.store import Store

SOURCE = "code_facts"
KINDS = ("finish_check_unverified", "finish_bounces_exhausted", "new_failures_repeated", "permission_ask_repeated",
         "review_watchdog_expired")
PERMISSION_REPEAT = 3
SEEN_KEY = "code_facts_seen"
CURSOR_KEY = "code_facts_cursor"
MAX_SEEN = 500


def normalize_ask(question: str) -> str:
    q = re.sub(r"^\s*worker permission:\s*", "", str(question or ""), flags=re.I)
    return " ".join(q.split()).lower()[:200]


def _key(kind: str, evidence_key: str) -> str:
    return f"{kind}:{hashlib.sha1(evidence_key.encode()).hexdigest()[:16]}"


def _verify_rounds_exhausted(store: Store, job_id: str) -> bool:
    """A failed verification goes back to the worker on its own (`closeout._send_back_to_running`, no tech-lead call) until the
    verify-round budget is spent; only then is a repeated failure a tech-lead fact (SRD-08)."""
    from steward import closeout

    return int(store.kv_get(job_id, closeout._VERIFY_ROUNDS_KEY, default=0) or 0) > closeout._MAX_VERIFY_ROUNDS


def detect(store: Store, job_id: str, state: dict | None = None) -> list[dict]:
    """Every fact the job's events justify: `[{kind, key, data}]` (a read; no dedupe). With `state` (the kv cursor
    `step()` keeps) only events after `state["cursor"]` are scanned and `state` is advanced in place."""
    state = state if state is not None else {}
    out: list[dict] = []
    prev_failures = set(state.get("prev_failures") or [])
    unverified_seen = bool(state.get("unverified_seen"))
    cursor = int(state.get("cursor") or 0)
    for e in store.events_since(job_id, cursor):
        cursor = max(cursor, e.seq)
        ev = e.evidence or {}
        if e.what == "finish_check_unverified":
            unverified_seen = True
            out.append({"kind": "finish_check_unverified", "key": f"event:{e.seq}",
                        "data": {"command": ev.get("command"), "reason": ev.get("reason"), "event": e.seq}})
        elif e.what == "check_broken":
            unverified_seen = True
        elif e.rule == "finish_line_bounces_exhausted":
            out.append({"kind": "finish_bounces_exhausted", "key": f"event:{e.seq}",
                        "data": {"reason": str(ev.get("reason") or e.what)[:200], "event": e.seq}})
        elif e.what == "verification recorded":
            now_failing = [str(t) for t in ev.get("new_failures") or []]
            for test in now_failing:
                if test in prev_failures and _verify_rounds_exhausted(store, job_id):  # SRD-08: only then a TL fact
                    out.append({"kind": "new_failures_repeated", "key": f"test:{test}:{e.seq}",
                                "data": {"test": test, "event": e.seq}})
            prev_failures = set(now_failing)
        elif e.what == "tech_lead_review_expired":
            out.append({"kind": "review_watchdog_expired", "key": f"event:{e.seq}",
                        "data": {"review": ev.get("review"), "lead": ev.get("lead"), "event": e.seq}})
    state.update(cursor=cursor, prev_failures=sorted(prev_failures)[:50], unverified_seen=unverified_seen)
    asks: dict[str, int] = {}
    for a in approvals_module.list_approvals(store, job_id):
        if a.get("kind") == "permission":
            n = normalize_ask(a.get("question", ""))
            asks[n] = asks.get(n, 0) + 1
    for ask, n in asks.items():
        if n >= PERMISSION_REPEAT:
            out.append({"kind": "permission_ask_repeated", "key": f"ask:{ask}", "data": {"ask": ask, "count": n}})
    return out


def step(store: Store, job_id: str, arbiter) -> list[str]:
    """Submit every not-yet-submitted fact to `arbiter`. Returns the kinds submitted now. Never calls a model."""
    seen = list(store.kv_get(job_id, SEEN_KEY) or [])
    state = dict(store.kv_get(job_id, CURSOR_KEY) or {})
    before = dict(state)
    submitted: list[str] = []
    for fact in detect(store, job_id, state):
        k = _key(fact["kind"], fact["key"])
        if k in seen:
            continue
        arbiter.submit(job_id, SOURCE, fact["kind"], key=fact["key"], **fact["data"])
        seen.append(k)
        submitted.append(fact["kind"])
        store.append_event(job_id, what="code_fact", evidence={"kind": fact["kind"], "key": fact["key"],
                                                               **fact["data"]},
                           rule="code_fact_floor", judgment=Judgment.code)
    if submitted:
        store.kv_set(job_id, SEEN_KEY, seen[-MAX_SEEN:])
    if state != before:
        store.kv_set(job_id, CURSOR_KEY, state)
    return submitted
