"""Failure-mode recurrence (observation log 2026-10-09, F1-F11 + OTHER): classify incidents from the events table
(read-only) and count them per day, with distinct jobs, median time-to-resolution and who resolved each.

A mode is a signature over one job's ordered events. Modes are independent (one event can be both an F3 and an F6);
OTHER holds human touches no mode claimed."""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

DEFAULT_SINCE = "2026-09-28T00:00:00+00:00"
MODES = ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "OTHER")
TITLES = {
    "F1": "liveness misses idle worker", "F2": "yield same_as_last_ruled", "F3": "test-weakening acceptance hold",
    "F4": "tech-lead answer then human ask", "F5": "NEEDS_HUMAN, nothing pending", "F6": "acceptance park, no coordinator",
    "F7": "health stale/not green blocks train", "F8": "operator_agent approval counted as decision",
    "F9": "carried question already merged", "F10": "no checks ran", "F11": "stray index.lock", "OTHER": "other human touch",
}
RESOLVERS = ("code", "coordinator", "TL", "agent", "human")
ACCEPT_HOLD = "checks passed; acceptance unresolved"
ASK_WINDOW_S = 3600.0        # F4: a human ask this soon after a TL answer means the answer did not settle it
COORD_WINDOW_S = 1800.0      # F6: a coordinator episode this soon after the park counts as handled
IDLE_S = 1800.0              # F1: an orphan record this recent means liveness saw the worker
NH_WINDOW_S = 600.0          # F5: NEEDS_HUMAN this soon after an approve
HEALTH_WHATS = ("wait_parked", "landing_skipped", "train_skipped", "autopilot_skipped", "autopilot_blocked")
RESOLUTION_WHATS = ("approval approved", "approval denied", "question_answered", "operator accepted", "operator released",
                    "operator stop", "incorporated", "auto_merged", "landed_by_train", "released_by_train", "sent_back",
                    "runner_restarted", "coordinator_action", "coordinator_hold_closed", "tech_lead_action",
                    "liveness_actor", "index_lock_healed")
LOAD_WHATS = tuple(dict.fromkeys(RESOLUTION_WHATS + HEALTH_WHATS + (
    ACCEPT_HOLD, "yield_classified", "tech_lead_answered", "tech_lead_escalated", "approval opened", "approval stale",
    "coordinator_episode", "coordinator_hold_opened", "liveness_orphan", "nudge instruction recorded",
    "owner_decision_carried", "recovery escalated to human", "needs_human", "approvals_superseded")))


@dataclass(frozen=True)
class Ev:
    seq: int
    job_id: str
    ts: datetime
    what: str
    ev: dict
    rule: str
    judgment: str

    @property
    def actor(self) -> str:
        return str(self.ev.get("actor", ""))


@dataclass
class Incident:
    mode: str
    job_id: str
    ts: datetime
    resolved_at: datetime | None = None
    resolver: str | None = None
    ttr_s: float | None = None


def _t(iso: str) -> datetime:
    d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def load(store, *, whats=LOAD_WHATS) -> dict[str, list[Ev]]:
    """job_id -> its events (oldest first), only rows whose `what` is in `whats`. SELECT only."""
    rows = store._conn.execute(
        f"SELECT seq, job_id, ts, what, evidence, rule, judgment FROM events WHERE what IN ({','.join('?' * len(whats))})"
        " OR what LIKE 'coordinator%' ORDER BY job_id, seq", tuple(whats)).fetchall()
    out: dict[str, list[Ev]] = defaultdict(list)
    for seq, job_id, ts, what, evidence, rule, judgment in rows:
        try:
            ev = json.loads(evidence) if evidence else {}
        except ValueError:
            ev = {}
        out[job_id].append(Ev(seq, job_id, _t(ts), what, ev if isinstance(ev, dict) else {}, rule or "", judgment))
    return out


def _is_coordinator(e: Ev) -> bool:
    return e.actor.startswith("operator_agent:coordinator") or e.what.startswith("coordinator")


def _is_operator_agent(e: Ev) -> bool:
    return e.actor.startswith("operator_agent:") and not _is_coordinator(e)


def _by_class(by: str) -> str | None:
    """Resolver class of a free-text actor label (`tech_lead:*` -> TL, any agent prefix -> agent), else None."""
    if by.startswith("tech_lead"):
        return "TL"
    if by.startswith(("operator_agent:", "unblock", "agent:")):
        return "agent"
    return None


def _sent_back_by(e: Ev, events: list[Ev] | None) -> str:
    """The `by` of the sent_back event that follows an `approval denied` with reason "sent back"."""
    if events is None or e.what != "approval denied" or e.ev.get("reason") != "sent back":
        return ""
    nxt = next((x for x in events if x.seq > e.seq and x.what == "sent_back"), None)
    return str(nxt.ev.get("by", "")) if nxt else ""


def who(e: Ev, events: list[Ev] | None = None) -> str:
    """Who actually resolved: the real actor, not the `human` judgment label a TL/agent action also writes."""
    if _is_coordinator(e):
        return "coordinator"
    if e.what.startswith("tech_lead") or e.rule.startswith("tech_lead"):
        return "TL"
    if _is_operator_agent(e):
        return "agent"
    by = _sent_back_by(e, events)
    if by and _by_class(by):
        return _by_class(by)
    if e.judgment == "human":
        return "human"
    return "code"


def _permission(e: Ev) -> bool:
    return str(e.ev.get("question", "")).startswith("worker permission")


def _is_resolution(e: Ev) -> bool:
    if e.what not in RESOLUTION_WHATS:
        return False
    return not (e.what in ("approval approved", "approval denied") and _permission(e))


def _reasons(e: Ev) -> str:
    return " ".join(str(r) for r in e.ev.get("reasons", []))


def _carried_ids(events: list[Ev]) -> set[str]:
    return {i for e in events if e.what == "owner_decision_carried" for i in e.ev.get("ids", [])}


def classify_job(events: list[Ev]) -> tuple[list[tuple[str, Ev]], set[int]]:
    """(mode, anchor event) hits for one job's ordered events, plus the seqs claimed by modes F1/F5/F8/F9."""
    hits: list[tuple[str, Ev]] = []
    claimed: set[int] = set()
    carried = _carried_ids(events)
    merged_at: datetime | None = None
    pending: set[str] = set()
    last_orphan: datetime | None = None
    last_tl_answer: Ev | None = None
    last_approve: Ev | None = None
    for i, e in enumerate(events):
        w = e.what
        if w == "index_lock_healed" or "index.lock.stale-" in str(e.ev.get("quarantine", "")):
            hits.append(("F11", e))
        if w in HEALTH_WHATS and any(s in json.dumps(e.ev).lower() for s in ("health not green", "stale-code", "stale code")):
            hits.append(("F7", e))
        if w == "yield_classified" and "same_as_last_ruled" in (e.ev.get("failed_preconditions") or []):
            hits.append(("F2", e))
        if w == ACCEPT_HOLD:
            r = _reasons(e)
            if "test-weakening" in r and "test_config_changed" in r:
                hits.append(("F3", e))
            if "no checks ran" in r:
                hits.append(("F10", e))
            if not any(x.what.startswith("coordinator") and 0 <= (x.ts - e.ts).total_seconds() <= COORD_WINDOW_S
                       for x in events[i + 1:]):
                hits.append(("F6", e))
        if w == "tech_lead_answered":
            last_tl_answer = e
        if w == "tech_lead_escalated" and last_tl_answer is not None \
                and (e.ts - last_tl_answer.ts).total_seconds() <= ASK_WINDOW_S:
            hits.append(("F4", e))
            last_tl_answer = None
        if w == "approval opened":
            pending.add(str(e.ev.get("approval_id", e.seq)))
        elif w in ("approval approved", "approval denied", "approval stale") and not _permission(e):
            pending.discard(str(e.ev.get("approval_id", "")))
        elif w == "approvals_superseded":
            pending.clear()
        if w == "approval approved":
            last_approve = e
        if w in ("recovery escalated to human", "needs_human") or e.ev.get("to_state") == "NEEDS_HUMAN" \
                or e.ev.get("state") == "NEEDS_HUMAN":
            if last_approve is not None and (e.ts - last_approve.ts).total_seconds() <= NH_WINDOW_S and not pending:
                hits.append(("F5", e))
                claimed.add(e.seq)
        if w in ("incorporated", "auto_merged", "landed_by_train") and merged_at is None:
            merged_at = e.ts
        if w == "orphan" or w == "liveness_orphan":
            last_orphan = e.ts
        if w in ("runner_restarted", "nudge instruction recorded") and e.judgment == "human" and not _is_coordinator(e):
            urgent = w == "runner_restarted" and e.ev.get("origin") not in ("watchdog", "liveness") \
                or (w != "runner_restarted" and bool(e.ev.get("urgent")))
            if urgent and (last_orphan is None or (e.ts - last_orphan).total_seconds() > IDLE_S):
                hits.append(("F1", e))
                claimed.add(e.seq)
        if w in ("approval approved", "question_answered") and _is_operator_agent(e) and not _permission(e):
            hits.append(("F8", e))
            claimed.add(e.seq)
        if w == "question_answered" and merged_at is not None and str(e.ev.get("qid", "")) in carried:
            hits.append(("F9", e))
            claimed.add(e.seq)
    return hits, claimed


def _other(events: list[Ev], claimed: set[int]) -> list[Ev]:
    out = []
    for e in events:
        if e.seq in claimed or _is_coordinator(e):
            continue
        human = e.judgment == "human" or _is_operator_agent(e)
        if not human:
            continue
        if e.what in ("approval approved", "approval denied") and not _permission(e):
            out.append(e)
        elif e.what in ("operator accepted", "operator released", "operator stop", "runner_restarted"):
            if e.what == "runner_restarted" and e.ev.get("origin") in ("watchdog", "liveness"):
                continue
            out.append(e)
    return out


def _resolve(events: list[Ev], inc: Incident, anchor: Ev) -> None:
    if inc.mode == "F11":
        inc.resolved_at, inc.resolver = anchor.ts, "code"
        age = anchor.ev.get("age_s")
        inc.ttr_s = float(age) if isinstance(age, (int, float)) else 0.0
        return
    if inc.mode in ("F8", "OTHER"):  # the touch is itself the resolution; time-to-resolve is how long it sat open
        inc.resolved_at, inc.resolver = anchor.ts, who(anchor, events)
        opened = next((o for o in reversed(events) if o.what == "approval opened" and o.seq < anchor.seq
                       and str(o.ev.get("approval_id")) == str(anchor.ev.get("approval_id"))), None)
        inc.ttr_s = (anchor.ts - opened.ts).total_seconds() if opened else None
        return
    for x in events:
        if x.seq > anchor.seq and _is_resolution(x) and not (inc.mode == "F4" and who(x, events) == "TL"):
            inc.resolved_at, inc.resolver = x.ts, who(x, events)
            inc.ttr_s = (x.ts - anchor.ts).total_seconds()
            return


def incidents(store, *, since_iso: str = DEFAULT_SINCE) -> list[Incident]:
    since = _t(since_iso)
    out: list[Incident] = []
    for job_id, events in load(store).items():
        hits, claimed = classify_job(events)
        hits += [("OTHER", e) for e in _other(events, claimed)]
        for mode, anchor in hits:
            if anchor.ts < since:
                continue
            inc = Incident(mode, job_id, anchor.ts)
            _resolve(events, inc, anchor)
            out.append(inc)
    return sorted(out, key=lambda i: (i.ts, i.mode))


def _summ(items: list[Incident]) -> dict:
    ttrs = [i.ttr_s for i in items if i.ttr_s is not None]
    res = Counter(i.resolver for i in items if i.resolver)
    return {"count": len(items), "jobs": len({i.job_id for i in items}),
            "median_ttr_s": statistics.median(ttrs) if ttrs else None,
            "resolved_by": {r: res[r] for r in RESOLVERS if res[r]},
            "unresolved": sum(1 for i in items if i.resolver is None)}


def compute(store, *, since_iso: str = DEFAULT_SINCE, by_day: bool = False) -> dict:
    incs = incidents(store, since_iso=since_iso)
    out: dict = {"since": since_iso, "modes": {m: _summ([i for i in incs if i.mode == m]) for m in MODES}}
    if by_day:
        days: dict[str, Counter] = defaultdict(Counter)
        for i in incs:
            days[i.ts.astimezone(timezone.utc).strftime("%Y-%m-%d")][i.mode] += 1
        out["by_day"] = {d: {m: days[d][m] for m in MODES if days[d][m]} for d in sorted(days)}
    return out


def _dur(s: float | None) -> str:
    if s is None:
        return "-"
    return f"{s:.0f}s" if s < 120 else f"{s / 60:.0f}m" if s < 7200 else f"{s / 3600:.1f}h"


def render(out: dict) -> str:
    lines = [f"failure modes since {out['since']}",
             f"{'mode':<6}{'count':>6}{'jobs':>6}{'median ttr':>12}  resolved by"]
    for m in MODES:
        s = out["modes"][m]
        by = ", ".join(f"{r} {n}" for r, n in s["resolved_by"].items()) or "-"
        if s["unresolved"]:
            by += f" (+{s['unresolved']} open)"
        lines.append(f"{m:<6}{s['count']:>6}{s['jobs']:>6}{_dur(s['median_ttr_s']):>12}  {by}  {TITLES[m]}")
    if "by_day" in out:
        lines += ["", "per day:"]
        for d, counts in out["by_day"].items():
            lines.append(f"{d}  " + " ".join(f"{m}={n}" for m, n in counts.items()))
    return "\n".join(lines)


def summary_line(out: dict) -> str:
    """One line for the daily landing-train report: `F2 3/2j · F7 1/1j`; only modes that fired."""
    parts = [f"{m} {s['count']}/{s['jobs']}j" for m, s in out["modes"].items() if s["count"]]
    return "failure modes (24h): " + (" · ".join(parts) if parts else "none")
