"""Deterministic hung-call detection (lost-approval hang, job 8a641f9b retro 2026-09-30).

A worker sat 2.5 h on one permission after a runner restart: Jev ran 1,043 checks with 0 flags and the doctor's
steer could not reach a worker blocked on a permission. This is the code-backed finding that does not depend on a
model noticing:

* `hung_call`        — a single tool call has been running > `tool_s` (30 min; env `STEWARD_HUNG_TOOL_S`,
                       per-job kv `hung_tool_s`).
* `permission_stuck` — a permission request has been waiting > `perm_s` (5 min; env `STEWARD_PERM_STUCK_S`,
                       per-job kv `hung_perm_s`): undecided, or decided but never delivery-acked by the shim.

Each finding (re-raised at most every `REPEAT_S`): (a) re-relays the pending permission answer if one exists,
else (b) routes to the doctor with the evidence (plus the permission fix path for an undecided request); and
(c) once the call is older than `escalate_s` (60 min), escalates NEEDS_YOU once with a one-line cause.
Authority: code; it never decides a permission itself.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Callable, Protocol

from steward import approvals as approvals_module
from steward.models import Judgment
from steward.store import Store

TOOL_HUNG_S = 1800.0
PERM_STUCK_S = 300.0
ESCALATE_S = 3600.0
REPEAT_S = 300.0
CHECK_EVERY_S = 30.0
RELAY_GRACE_S = 60.0  # a permission decided and relayed this recently is never "stuck" (job d3477a2a)
STATE_KEY = "hung_calls"


class Actions(Protocol):
    def rerelay(self, job_id: str, approval_id: str) -> str | None: ...
    def doctor(self, job_id: str, evidence: dict | None = None) -> str | None: ...
    def permission(self, job_id: str) -> str | None: ...
    def needs_you(self, job_id: str, text: str) -> str | None: ...


def _epoch(ts) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _num(v, default: float) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def thresholds(store: Store, job_id: str, env=None) -> tuple[float, float]:
    env = os.environ if env is None else env
    tool_s = _num(store.kv_get(job_id, "hung_tool_s"), _num(env.get("STEWARD_HUNG_TOOL_S"), TOOL_HUNG_S))
    perm_s = _num(store.kv_get(job_id, "hung_perm_s"), _num(env.get("STEWARD_PERM_STUCK_S"), PERM_STUCK_S))
    return tool_s, perm_s


def running_tool(store: Store, job_id: str, now: float, activity_fn: Callable | None = None) -> dict | None:
    """The tool call the worker is waiting on, with its age: {"tool", "age_s"} or None."""
    try:
        if activity_fn is not None:
            turns = (activity_fn(store, job_id) or {}).get("turns") or []
            last_tool = max((i for i, t in enumerate(turns) if t.get("kind") == "tool"), default=-1)
            last_res = max((i for i, t in enumerate(turns) if t.get("kind") in ("result", "error")), default=-1)
            if last_tool < 0 or last_tool < last_res:
                return None
            tool, since = turns[last_tool].get("text"), turns[last_tool].get("ts")
        else:
            from steward import transcript

            live = transcript.live_line(store, job_id)
            tool, since = live.get("running_tool"), live.get("since")
    except Exception:  # noqa: BLE001 - a transcript read failure is not a finding
        return None
    started = _epoch(since)
    if not tool or started is None:
        return None
    return {"tool": str(tool)[:120], "age_s": max(0, int(now - started))}


def pending_permission(store: Store, job_id: str, now: float) -> dict | None:
    """The oldest permission the worker may still be waiting on: undecided, or answered but not delivery-acked.
    {"approval_id", "state": pending|unacked, "age_s", "what"} or None. An answer relayed in the last
    `RELAY_GRACE_S` is still on its way: not waiting, whatever the request's own age."""
    oldest = None
    for a in approvals_module.list_approvals(store, job_id):
        if a.get("kind") != "permission":
            continue
        if a["status"] == "pending":
            state = "pending"
        elif approvals_module.answer_in_flight(a):
            relayed = _epoch(a.get("relayed_at"))
            if relayed is not None and now - relayed < RELAY_GRACE_S:
                continue
            state = "unacked"
        else:
            continue
        started = _epoch(a.get("created_at"))
        if started is None:
            continue
        item = {"approval_id": a["id"], "state": state, "age_s": max(0, int(now - started)),
                "what": str(a.get("question", "")).replace("worker permission: ", "")[:80]}
        if oldest is None or item["age_s"] > oldest["age_s"]:
            oldest = item
    return oldest


def findings(store: Store, job_id: str, now: float, *, activity_fn: Callable | None = None, env=None) -> list[dict]:
    tool_s, perm_s = thresholds(store, job_id, env)
    out = []
    perm = pending_permission(store, job_id, now)
    if perm is not None and perm["age_s"] > perm_s:
        out.append({"kind": "permission_stuck", "key": f"perm:{perm['approval_id']}", "age_s": perm["age_s"],
                    "what": perm["what"], "approval_id": perm["approval_id"], "state": perm["state"],
                    "threshold_s": perm_s})
    tool = running_tool(store, job_id, now, activity_fn)
    if tool is not None and tool["age_s"] > tool_s:
        out.append({"kind": "hung_call", "key": f"tool:{tool['tool']}", "age_s": tool["age_s"],
                    "what": tool["tool"], "threshold_s": tool_s})
    return out


def measured(store: Store, job_id: str, now: float, *, activity_fn: Callable | None = None, env=None) -> list[dict]:
    """The measured ages that make a hung call real: a tool running > `tool_s`, or a permission undecided /
    undelivered > `perm_s`. A model verdict (Jev's `hung_call`) routes only when this is non-empty."""
    try:
        return findings(store, job_id, now, activity_fn=activity_fn, env=env)
    except Exception:  # noqa: BLE001 - unmeasurable is not a finding
        return []


def cause_line(f: dict, perm: dict | None) -> str:
    mins = f["age_s"] // 60
    if f["kind"] == "permission_stuck":
        why = "answer sent but never acked by the worker" if f.get("state") == "unacked" else "nobody has answered it"
        return f"worker waiting {mins}m on permission {f['what'][:60]}: {why}"
    if perm is not None:
        return f"tool {f['what'][:60]} running {mins}m; a permission is outstanding ({perm['state']})"
    return f"tool {f['what'][:60]} running {mins}m with no result"


class HungCallWatch:
    def __init__(self, store: Store, *, clock: Callable[[], float] = time.time, activity_fn: Callable | None = None,
                 escalate_s: float = ESCALATE_S, repeat_s: float = REPEAT_S, every_s: float = CHECK_EVERY_S,
                 env=None):
        self.store = store
        self.clock = clock
        self.activity_fn = activity_fn
        self.escalate_s = escalate_s
        self.repeat_s = repeat_s
        self.every_s = every_s
        self.env = env
        self._last: dict[str, float] = {}

    def step(self, job_id: str, actions: Actions, *, force: bool = False) -> list[dict]:
        now = self.clock()
        if not force and now - self._last.get(job_id, float("-inf")) < self.every_s:
            return []
        self._last[job_id] = now
        found = findings(self.store, job_id, now, activity_fn=self.activity_fn, env=self.env)
        state = self.store.kv_get(job_id, STATE_KEY) or {}
        live = {f["key"] for f in found}
        state = {k: v for k, v in state.items() if k in live}  # a call that finished forgets its episode
        perm = pending_permission(self.store, job_id, now)
        raised = []
        for f in found:
            st = state.setdefault(f["key"], {"routed_at": None, "escalated": False})
            if st["routed_at"] is None or now - float(st["routed_at"]) >= self.repeat_s:
                st["routed_at"] = now
                f["routes"] = self._route(job_id, f, perm, actions)
                self.store.append_event(job_id, what=f["kind"], evidence={k: v for k, v in f.items() if k != "key"},
                                        rule="hung_call_detector", judgment=Judgment.code)
                raised.append(f)
            if f["age_s"] >= self.escalate_s and not st["escalated"]:
                st["escalated"] = True
                line = cause_line(f, perm)
                detail = actions.needs_you(job_id, line)
                self.store.append_event(job_id, what="hung_call_escalated",
                                        evidence={"kind": f["kind"], "age_s": f["age_s"], "cause": line,
                                                  "detail": detail},
                                        rule="hung_call_detector", judgment=Judgment.code)
        self.store.kv_set(job_id, STATE_KEY, state)
        return raised

    def _route(self, job_id: str, f: dict, perm: dict | None, actions: Actions) -> list[str]:
        out: list[str] = []
        try:
            if perm is not None and perm["state"] == "unacked":
                out.append("rerelay: " + str(actions.rerelay(job_id, perm["approval_id"])))
                return out
            if perm is not None and perm["state"] == "pending":
                out.append("permission: " + str(actions.permission(job_id)))
            evidence = {"finding": f["kind"], "age_s": f["age_s"], "what": f["what"],
                        "pending_permission": perm}
            out.append("doctor: " + str(actions.doctor(job_id, evidence)))
        except Exception as exc:  # noqa: BLE001 - a routing failure is recorded, never fatal (SP-6)
            out.append(f"error: {type(exc).__name__}")
        return out
