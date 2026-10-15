"""The longitudinal trajectory ledger (TRJ-02, TRJ-03, SP-32).

kv-backed JSON under key `trajectory_ledger` (precedent: steering and
approvals — no schema migration). Holds only longitudinal facts: intent,
acceptance revisions, plan snapshots, decisions, discarded approaches,
milestones, diff samples, test points, cost points and reviews (12-CONTEXT
D3). There is deliberately no field anywhere in this module that accepts a
raw pane tail, assistant text or raw tool output — every string this module
ever writes is a short summary, capped at 200 chars and redacted
(`steward.diagnosis.redact`) before it is persisted. The outer trajectory
loop must never learn what the inner loop saw on the screen (design doc:
"never the inner transcript").

Every list is capped (`cap`, default 200): once a section exceeds the cap,
its oldest half is thinned (kept every other entry) rather than dropped
outright, and a per-section counter records how many entries were thinned
away, so growth is bounded for a multi-hour job without silently losing all
history of its early hours.
"""

from __future__ import annotations

import copy
import dataclasses
from typing import Any

from steward.diagnosis import redact
from steward.models import Judgment
from steward.store import Store

_KEY = "trajectory_ledger"
_STR_CAP = 200
_LIST_ELEMENT_CAP = 200  # defensive cap on elements within one entry's own lists

_SECTIONS = (
    "acceptance_revisions",
    "plan",
    "decisions",
    "discarded_approaches",
    "milestones",
    "diff_samples",
    "test_points",
    "cost_points",
    "failed_approaches",
    "reviews",
)

_MILESTONE_KINDS = frozenset({"done", "closeout_round", "summary_commit", "contract_revision"})
_COST_SOURCES = frozenset({"worker", "supervision"})


def _cap_str(text: Any) -> str:
    return redact(str(text))[:_STR_CAP]


def _cap_value(value: Any) -> Any:
    """Recursively redact + cap every string this module is about to
    persist. Never called on anything larger than a short summary field by
    contract (callers pass slugs/summaries/refs, not pane text)."""
    if isinstance(value, str):
        return _cap_str(value)
    if isinstance(value, list):
        return [_cap_value(v) for v in value[:_LIST_ELEMENT_CAP]]
    if isinstance(value, dict):
        return {k: _cap_value(v) for k, v in value.items()}
    return value


def _judgment_value(judgment: Judgment | str) -> str:
    return judgment.value if isinstance(judgment, Judgment) else str(judgment)


class TrajectoryLedger:
    def __init__(self, store: Store, job_id: str, cap: int = 200):
        self.store = store
        self.job_id = job_id
        self.cap = cap

    # -- storage -------------------------------------------------------

    def _load(self) -> dict:
        data = self.store.kv_get(self.job_id, _KEY)
        if data is None:
            data = {"started_at": None, "intent": None, "lists": {}, "dropped": {}}
        data.setdefault("started_at", None)
        data.setdefault("intent", None)
        lists = data.setdefault("lists", {})
        for section in _SECTIONS:
            lists.setdefault(section, [])
        data.setdefault("dropped", {})
        return data

    def _save(self, data: dict) -> None:
        self.store.kv_set(self.job_id, _KEY, data)

    def _touch_start(self, data: dict, now: float) -> None:
        if data["started_at"] is None:
            data["started_at"] = now

    def _append(self, data: dict, section: str, item: dict, now: float) -> None:
        self._touch_start(data, now)
        lst = data["lists"][section]
        lst.append(item)
        if len(lst) > self.cap:
            half = len(lst) // 2
            old, rest = lst[:half], lst[half:]
            thinned = old[::2]
            dropped_n = len(old) - len(thinned)
            data["lists"][section] = thinned + rest
            data["dropped"][section] = data["dropped"].get(section, 0) + dropped_n

    # -- fact APIs -------------------------------------------------------

    def set_intent(self, goal: str, done_when: str, now: float) -> None:
        """First call sets the intent; later calls record an acceptance
        revision instead of silently overwriting the original goal."""
        data = self._load()
        self._touch_start(data, now)
        goal_c, done_c = _cap_str(goal), _cap_str(done_when)
        if data["intent"] is None:
            data["intent"] = {"goal": goal_c, "done_when": done_c, "set_at": now}
        else:
            self._append(
                data,
                "acceptance_revisions",
                {"text": _cap_str(f"goal={goal_c}; done_when={done_c}"), "source": "intent_update", "ts": now},
                now,
            )
        self._save(data)

    def add_acceptance_revision(self, text: str, source: str, now: float) -> None:
        data = self._load()
        self._append(data, "acceptance_revisions", {"text": _cap_str(text), "source": _cap_str(source), "ts": now}, now)
        self._save(data)

    def add_plan(self, items: list[str], now: float) -> None:
        data = self._load()
        normalized = [_cap_str(str(item).strip()) for item in items]
        self._append(data, "plan", {"items": normalized, "ts": now}, now)
        self._save(data)

    def add_decision(self, kind: str, summary: str, origin: str, judgment: Judgment | str, ref: str, now: float) -> None:
        data = self._load()
        self._append(
            data,
            "decisions",
            {
                "kind": _cap_str(kind),
                "summary": _cap_str(summary),
                "origin": _cap_str(origin),
                "judgment": _judgment_value(judgment),
                "ref": _cap_str(ref) if ref else "",
                "ts": now,
            },
            now,
        )
        self._save(data)

    def add_discarded_approach(self, slug: str, reason: str, now: float) -> None:
        data = self._load()
        self._append(data, "discarded_approaches", {"slug": _cap_str(slug), "reason": _cap_str(reason), "ts": now}, now)
        self._save(data)

    def add_milestone(self, kind: str, ref: str, now: float) -> None:
        if kind not in _MILESTONE_KINDS:
            raise ValueError(f"unknown milestone kind: {kind!r}")
        data = self._load()
        self._append(data, "milestones", {"kind": kind, "ref": _cap_str(ref) if ref else "", "ts": now}, now)
        self._save(data)

    def add_diff_sample(self, sample: Any, now: float) -> None:
        data = self._load()
        raw = dataclasses.asdict(sample) if dataclasses.is_dataclass(sample) else dict(sample)
        capped = {key: _cap_value(value) for key, value in raw.items()}
        capped["ts"] = now
        self._append(data, "diff_samples", capped, now)
        self._save(data)

    def add_test_point(self, passed: int, failed: int, errors: int, source: str, now: float) -> None:
        data = self._load()
        self._append(
            data,
            "test_points",
            {"passed": int(passed), "failed": int(failed), "errors": int(errors), "source": _cap_str(source), "ts": now},
            now,
        )
        self._save(data)

    def add_cost_point(self, cost_usd: float, attempt_no: int, source: str, now: float) -> None:
        if source not in _COST_SOURCES:
            raise ValueError(f"unknown cost point source: {source!r}")
        data = self._load()
        self._append(
            data,
            "cost_points",
            {"cost_usd": float(cost_usd), "attempt_no": int(attempt_no), "source": source, "ts": now},
            now,
        )
        self._save(data)

    def add_failed_approach(self, kind: str, ref: str, now: float) -> None:
        data = self._load()
        self._append(data, "failed_approaches", {"kind": _cap_str(kind), "ref": _cap_str(ref) if ref else "", "ts": now}, now)
        self._save(data)

    def add_review(self, review_id: str, triggers: list[str], now: float) -> None:
        data = self._load()
        self._append(
            data, "reviews", {"review_id": _cap_str(review_id), "triggers": _cap_value(list(triggers)), "ts": now}, now
        )
        self._save(data)

    # -- accounting -------------------------------------------------------

    def worker_spend_usd(self) -> float:
        """Per attempt: non-decreasing points are treated as a cumulative
        session total (take the max); otherwise summed as per-turn deltas.
        Attempt totals are then summed across attempts (D11, INFERRED)."""
        data = self._load()
        by_attempt: dict[int, list[float]] = {}
        for point in data["lists"]["cost_points"]:
            if point["source"] != "worker":
                continue
            by_attempt.setdefault(point["attempt_no"], []).append(point["cost_usd"])
        total = 0.0
        for values in by_attempt.values():
            non_decreasing = all(values[i] <= values[i + 1] for i in range(len(values) - 1))
            total += max(values) if non_decreasing else sum(values)
        return total

    def supervision_spend_usd(self) -> float:
        data = self._load()
        return sum(p["cost_usd"] for p in data["lists"]["cost_points"] if p["source"] == "supervision")

    def elapsed_s(self, now: float) -> float:
        data = self._load()
        start = data["started_at"]
        if start is None:
            return 0.0
        return max(0.0, now - start)

    def spend_rate(self, window_s: float, now: float) -> float:
        """$/s spent (worker + supervision) within the trailing `window_s`."""
        if window_s <= 0:
            return 0.0
        data = self._load()
        cutoff = now - window_s
        total = sum(p["cost_usd"] for p in data["lists"]["cost_points"] if p["ts"] >= cutoff)
        return total / window_s

    # -- read models -------------------------------------------------------

    def snapshot(self) -> dict:
        return copy.deepcopy(self._load())

    def since(self, review_marker: float) -> dict:
        """Deltas (per section) added strictly after `review_marker` — the
        input `steward.triggers.evaluate` detects over."""
        data = self._load()
        return {section: [e for e in entries if e.get("ts", 0) > review_marker] for section, entries in data["lists"].items()}
