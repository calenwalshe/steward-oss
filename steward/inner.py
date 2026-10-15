"""Typed inner-loop verdict (TRJ-01, SP-12, SP-13, SP-21).

Every tick, the deterministic heartbeat classifier (`steward.heartbeat`) and
the bounded recovery policy (`steward.recovery`) already decide what happens.
`inner_verdict` is a pure, second read of that same tick's outcome into a
small enum-shaped shape the outer trajectory loop (12-03) can compare
cheaply, without ever re-deciding anything or calling a model itself. Sol is
still consulted only on `suspect_stall`, once per stall episode — that
decision is made in `steward.recovery`, not here.
"""

from __future__ import annotations

from dataclasses import dataclass

from steward.heartbeat import Class
from steward.models import Judgment
from steward.recovery import Action
from steward.store import Store

_LAST_INNER_VERDICT_KEY = "last_inner_verdict"

_STATE_MAP: dict[Class, str] = {
    Class.progressing: "executing",
    Class.waiting: "waiting",
    Class.quiet_known_process: "quiet",
    Class.suspect_stall: "stalled",
    Class.dead: "dead",
    Class.idle: "idle",
}

_ACTION_MAP: dict[str, str] = {
    "none": "continue",
    "guide": "repair",
    "replace": "retry",
    "needs_human": "escalate",
}


@dataclass(frozen=True)
class InnerVerdict:
    state: str  # executing | waiting | quiet | stalled | dead | idle
    action: str  # continue | repair | retry | focused_diagnosis | escalate
    reason: str
    judgment: Judgment


def inner_verdict(cls: Class | str, action: Action, diagnosed_this_tick: bool) -> InnerVerdict:
    """Pure mapping from (heartbeat class, recovery action) to `InnerVerdict`.

    A `suspect_stall` tick whose action was produced by a fresh diagnosis
    this very tick reports the meta-action `focused_diagnosis` — the
    informative fact for that tick is "Sol was just consulted", not what the
    diagnosis went on to decide. The one exception is `needs_human`: it keeps
    the more severe, more informative `escalate` label even on a fresh
    diagnosis, since an escalation should never be masked.
    """
    cls = Class(cls) if not isinstance(cls, Class) else cls
    state = _STATE_MAP[cls]

    if cls == Class.suspect_stall and diagnosed_this_tick and action.kind != "needs_human":
        verdict_action = "focused_diagnosis"
    else:
        verdict_action = _ACTION_MAP[action.kind]

    return InnerVerdict(
        state=state,
        action=verdict_action,
        reason=action.rule,
        judgment=action.judgment,
    )


def record_if_changed(store: Store, job_id: str, verdict: InnerVerdict) -> bool:
    """Append an `inner_verdict` event only when (state, action) changed
    since the last recorded verdict for this job. Returns whether it wrote.

    A verdict that stays `continue` / `no_recovery_needed` while only the state moves (the heartbeat flapping
    between quiet and executing) is remembered but not logged (TUI speed plan 10c)."""
    current = {"state": verdict.state, "action": verdict.action}
    last = store.kv_get(job_id, _LAST_INNER_VERDICT_KEY)
    if last == current:
        return False
    store.kv_set(job_id, _LAST_INNER_VERDICT_KEY, current)
    if (verdict.action == "continue" and verdict.reason == "no_recovery_needed"
            and isinstance(last, dict) and last.get("action") == "continue"):
        return False
    store.append_event(
        job_id,
        what="inner_verdict",
        evidence={"state": verdict.state, "action": verdict.action, "reason": verdict.reason},
        rule="inner_loop",
        judgment=verdict.judgment,
    )
    return True
