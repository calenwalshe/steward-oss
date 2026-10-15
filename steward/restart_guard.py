"""Runner-restart debounce (lost-approval hang, job 8a641f9b retro 2026-09-30).

A permission was approved at 19:53:15 and the runner was restarted at 19:53:26 and again at 19:53:45; the answer
was lost in between. Every runner restart path — the dash button and the CLI (`steward runner-restart`, both
through the control pane's `runner_restart` handler) and the runner's own self-restart — asks `check` first:

* ignored when another restart of the same job happened in the last `DEBOUNCE_S` (30 s);
* never while a permission answer is in flight (decided but not yet relayed, or relayed over the fabric and
  not yet delivery-acked by the shim): waits up to `INFLIGHT_WAIT_S` (10 s) for it to land, else refuses.

Only answers decided within `INFLIGHT_WINDOW_S` count as in flight, so one answer an old shim will never ack
cannot block restarts forever (the restarted runner re-sends it anyway).
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Callable

from steward import approvals as approvals_module
from steward.models import Judgment
from steward.store import Store

DEBOUNCE_S = 30.0
INFLIGHT_WAIT_S = 10.0
INFLIGHT_WINDOW_S = 120.0
POLL_S = 0.5
STAMP_KEY = "runner_restart_last_at"


def _epoch(ts) -> float | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def answers_in_flight(store: Store, job_id: str, now: float) -> list[str]:
    """Approval ids whose permission answer may not have reached the worker yet."""
    out = []
    for a in approvals_module.list_approvals(store, job_id):
        if not approvals_module.answer_in_flight(a):
            continue
        decided = _epoch(a.get("decided_at"))
        if decided is not None and now - decided > INFLIGHT_WINDOW_S:
            continue
        out.append(a["id"])
    return out


def check(store: Store, job_id: str, *, origin: str, clock: Callable[[], float] = time.time,
          sleep: Callable[[float], None] = time.sleep, debounce_s: float = DEBOUNCE_S,
          wait_s: float = INFLIGHT_WAIT_S) -> str | None:
    """None = go ahead (then call `stamp` once the restart is really happening); else why it was refused."""
    now = clock()
    last = store.kv_get(job_id, STAMP_KEY)
    if isinstance(last, (int, float)) and 0 <= now - last < debounce_s:
        why = f"restart ignored: the runner was restarted {now - last:.0f}s ago (debounce {debounce_s:.0f}s)"
        store.append_event(job_id, what="runner_restart_debounced",
                           evidence={"origin": origin, "since_last_s": round(now - last, 1), "debounce_s": debounce_s},
                           rule="runner_restart_debounce", judgment=Judgment.code)
        return why
    deadline = now + wait_s
    while True:
        flying = answers_in_flight(store, job_id, clock())
        if not flying:
            return None
        if clock() >= deadline:
            store.append_event(job_id, what="runner_restart_deferred",
                               evidence={"origin": origin, "approval_ids": flying, "waited_s": wait_s},
                               rule="runner_restart_debounce", judgment=Judgment.code)
            return f"restart refused: a permission answer is still in flight after {wait_s:.0f}s; try again shortly"
        sleep(POLL_S)


def stamp(store: Store, job_id: str, now: float | None = None) -> None:
    store.kv_set(job_id, STAMP_KEY, time.time() if now is None else now)


def check_all(store: Store, *, clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
              wait_s: float = INFLIGHT_WAIT_S) -> str | None:
    """The same in-flight rule for a Steward-wide restart (`steward deploy-self`): across every RUNNING job. None =
    go ahead; else why it was refused. (No per-job debounce: a deploy is serialized by its own lock.)"""
    from steward.models import JobState

    deadline = clock() + wait_s
    while True:
        flying = [a for j in store.list_jobs({JobState.RUNNING}) for a in answers_in_flight(store, j.job_id, clock())]
        if not flying:
            return None
        if clock() >= deadline:
            return f"deploy refused: a permission answer is still in flight after {wait_s:.0f}s; try again shortly"
        sleep(POLL_S)
