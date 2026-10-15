"""The worker's in-flight tool batch, as the shim reports it (`event {kind: batch}`; latency #6/#7).

The shim knows exactly which tool calls are outstanding (tool_use issued, no tool_result yet) and numbers each
batch (`gen`). The runner keeps the latest report here so supervision can look before it acts: a stall
diagnosis is not started while a legitimate wait or build is running (the d22d6633 false positive), and the
code-initiated interrupt (#7) needs every call in the batch to be a pure wait.
"""

from __future__ import annotations

import time

from steward import wait_policy
from steward.store import Store

KEY = "inflight_batch"
# What a long silence with this call in flight most likely is: legitimately busy, not stalled.
LEGIT_LABELS = frozenset({"pure_wait", "build_test", "subagent"})


def record(store: Store, job_id: str, body: dict, now: float | None = None) -> dict:
    batch = {"gen": int(body.get("gen") or 0), "since": body.get("since"), "calls": list(body.get("calls") or []),
             "at": time.time() if now is None else now}
    store.kv_set(job_id, KEY, batch)
    return batch


def current(store: Store, job_id: str) -> dict | None:
    """The latest reported batch while it still has calls in flight, else None (unknown or empty)."""
    batch = store.kv_get(job_id, KEY)
    if not isinstance(batch, dict) or not batch.get("calls"):
        return None
    return batch


def labels(batch: dict) -> list[str]:
    return [wait_policy.classify_call(c.get("name") or "", c.get("input") or {}) for c in batch.get("calls") or []]


def legitimate(store: Store, job_id: str) -> tuple[bool, list[str]]:
    """(a legitimate wait/build/subagent is in flight?, the batch's labels)."""
    batch = current(store, job_id)
    if batch is None:
        return False, []
    found = labels(batch)
    return any(label in LEGIT_LABELS for label in found), found


def all_pure_waits(batch: dict | None) -> bool:
    """The strict rule over the whole batch: every call a pure wait (any other call vetoes)."""
    return bool(batch and batch.get("calls")) and all(label == "pure_wait" for label in labels(batch))
