"""The advisory supervision allowance (Steward v2.7, SRD-11, decision D6, owner decision 2026-10-06).

Per job: `allowance_usd = max($3, 0.5 x worker spend)`. Beyond it, non-completion tech-lead reviews queue silently
(`pending_facts`, retried every `RETRY_S`); so do reviews beyond `MAX_REVIEWS_PER_HOUR` in a rolling hour. The allowance
never notifies, never pauses the worker and never touches the completion ruling. There is NO default hard cap: a hard
cap exists only as an owner-provenance `spend_limits` row, enforced by `spend_policy.check_hard` (a different path).
`spend_policy.report` parses transcripts (`tokens.job_tokens`), so the figure is cached for `CACHE_S` per job.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

FLOOR_USD = 3.0
WORKER_SHARE = 0.5
CACHE_S = 60.0
RETRY_S = 300.0
MAX_REVIEWS_PER_HOUR = 4
WINDOW_S = 3600.0
IDLE_S = 1800.0
MIN_PLAUSIBLE_TS = 946684800.0  # 2000-01-01: an older progress_ts comes from a synthetic clock, not a real epoch

_CACHE: dict[tuple[str, str], tuple[float, float, float]] = {}


@dataclass(frozen=True)
class Standing:
    allowance_usd: float
    supervision_usd: float

    @property
    def over(self) -> bool:
        return self.supervision_usd >= self.allowance_usd


def allowance_for(worker_usd: float) -> float:
    return max(FLOOR_USD, WORKER_SHARE * float(worker_usd or 0.0))


def standing(store, job_id: str, *, now: float | None = None, report_fn=None) -> Standing:
    """The job's allowance and what supervision has cost so far, recomputed at most once per `CACHE_S`."""
    now = time.time() if now is None else now
    key = (str(store.home), job_id)
    hit = _CACHE.get(key)
    if hit is not None and 0 <= now - hit[0] < CACHE_S:
        return Standing(hit[1], hit[2])
    if report_fn is None:
        from steward import spend_policy

        report_fn = lambda: spend_policy.report(store, job_id, now=now)  # noqa: E731
    rep = report_fn()
    st = Standing(allowance_for(rep.worker_usd), float(rep.supervision_usd or 0.0))
    _CACHE[key] = (now, st.allowance_usd, st.supervision_usd)
    return st


def reviews_in_window(starts: list, now: float) -> list[float]:
    return [float(t) for t in starts or [] if 0 <= now - float(t) < WINDOW_S]


def defer_reason(store, job_id: str, starts: list, *, now: float, report_fn=None) -> str | None:
    """`rate` / `allowance` when a non-completion review must wait, else None. Fails open: an unreadable spend picture
    never blocks supervision."""
    if len(reviews_in_window(starts, now)) >= MAX_REVIEWS_PER_HOUR:
        return "rate"
    try:
        return "allowance" if standing(store, job_id, now=now, report_fn=report_fn).over else None
    except Exception:  # noqa: BLE001 - advisory
        return None


def reset_cache() -> None:
    _CACHE.clear()


def idle_reason(store, job_id: str, *, now: float, idle_s: float = IDLE_S) -> str | None:
    """Spec §5.2: "idle" when the worker has no open turn and has made no progress for `idle_s`. No record: not idle."""
    rec = store.kv_get(job_id, "liveness") or {}
    if not rec or rec.get("turn_open"):
        return None
    ts = float(rec.get("progress_ts") or now)
    if ts < MIN_PLAUSIBLE_TS <= now:
        return None  # no real progress timestamp: unknown, never idle
    return "idle" if now - ts >= idle_s else None
