"""Pause-review metrics (spec docs/superpowers/specs/2026-10-13-steward-pause-review-design.md §4.6, §9).

`steward metrics pause-review`: reviews per job, review wait p50/p90, 5-minute timeouts, provisional units later sent
back, the outcome mix, and the trial numbers (owner interventions per job, time to done, $ per job), split into the
pause arm (contract pause unit|natural) and the off arm. Read-only: events, kv, and optionally spend reports."""
from __future__ import annotations

import math
from collections import Counter

from steward import pause_checkpoint
from steward import zero_touch_metrics as ztm

DONE_WHATS = ("completion ruled finished; code guards held", "operator accepted")
ARMS = ("pause", "off")


def _pct(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile; None for no data."""
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(q * len(s)) - 1)]


def _intervention(e) -> bool:
    """An owner intervention: a human mechanical touch or a human decision (zero-touch definitions)."""
    return e.judgment.value == "human" and (ztm._is_touch(e) or ztm._is_decision(e))


def compute(store, *, since_iso: str, report_fn=None, spend: bool = True) -> dict:
    if spend and report_fn is None:
        from steward import spend_policy

        report_fn = lambda st, jid: spend_policy.report(st, jid)  # noqa: E731
    since = ztm._t(since_iso)
    raw = {arm: {"jobs": 0, "reviews": 0, "waits": [], "timeouts": 0, "provisional_sent_back": 0,
                 "outcomes": Counter(), "interventions": 0, "done_min": [], "usd": []} for arm in ARMS}
    for job in store.list_jobs():
        if ztm._t(job.created_at) < since:
            continue
        a = raw["off" if pause_checkpoint.pause_mode(store, job.job_id) == "off" else "pause"]
        events = store.events(job.job_id)
        verdicts = [e for e in events if e.what == "pause_verdict"]
        a["jobs"] += 1
        a["reviews"] += len(verdicts)
        a["waits"] += [float(e.evidence["wait_s"]) for e in verdicts if e.evidence.get("wait_s") is not None]
        a["timeouts"] += sum(1 for e in events if e.what == "pause_review_timeout")
        a["provisional_sent_back"] += sum(1 for e in verdicts
                                          if e.evidence.get("provisional") and e.evidence.get("outcome") == "send_back")
        a["outcomes"].update(str(e.evidence.get("outcome")) for e in verdicts)
        a["interventions"] += sum(1 for e in events if _intervention(e))
        done = next((e for e in events if e.what in DONE_WHATS), None)
        if done is not None:
            a["done_min"].append((ztm._t(done.ts) - ztm._t(job.created_at)).total_seconds() / 60.0)
        if spend:
            try:
                rep = report_fn(store, job.job_id)
                a["usd"].append(float(rep.worker_usd or 0.0) + float(rep.supervision_usd or 0.0))
            except Exception:  # noqa: BLE001 - an unreadable spend picture is reported as unknown, never 0
                pass
    return {"since": since_iso, "arms": {arm: _summary(v) for arm, v in raw.items()}}


def _summary(a: dict) -> dict:
    n = a["jobs"]
    return {"jobs": n, "reviews": a["reviews"], "reviews_per_job": round(a["reviews"] / n, 2) if n else None,
            "wait_p50_s": _pct(a["waits"], 0.5), "wait_p90_s": _pct(a["waits"], 0.9), "timeouts": a["timeouts"],
            "provisional_sent_back": a["provisional_sent_back"], "outcomes": dict(sorted(a["outcomes"].items())),
            "interventions_per_job": round(a["interventions"] / n, 2) if n else None,
            "time_to_done_p50_min": None if not a["done_min"] else round(_pct(a["done_min"], 0.5), 1),
            "usd_per_job": round(sum(a["usd"]) / len(a["usd"]), 2) if a["usd"] else None}


def render(out: dict) -> str:
    lines = [f"pause-review since {out['since']}"]
    for arm in ARMS:
        s = out["arms"][arm]
        lines.append(f"{arm} arm: {s['jobs']} jobs · reviews/job {s['reviews_per_job']} · wait p50 {s['wait_p50_s']}s "
                     f"p90 {s['wait_p90_s']}s · 5-min timeouts {s['timeouts']} · provisional sent back "
                     f"{s['provisional_sent_back']} · outcomes {s['outcomes'] or '-'} · owner interventions/job "
                     f"{s['interventions_per_job']} · time to done p50 {s['time_to_done_p50_min']} min · $/job "
                     f"{s['usd_per_job']}")
    return "\n".join(lines)
