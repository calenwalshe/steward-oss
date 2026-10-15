"""Zero-touch metrics (spec 2026-10-08 §5.7): mechanical touches per merged job, orphan hours, auto-land ratio."""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone

STALE_APPROVAL_KINDS = ("delivery", "tech_lead", "general", "permission")
MECHANICAL_WHATS = ("operator released", "operator stop", "incorporate_noop", "break_glass_input")
AUTO_SUBJECTS = ("Land Steward job", "Incorporate Steward job")
MANUAL_SUBJECT = "Manual merge"


def _t(iso: str) -> datetime:
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _is_touch(e) -> bool:
    ev = e.evidence
    if e.judgment.value != "human":
        return False
    if e.what in MECHANICAL_WHATS:
        return True
    if e.what == "runner_restarted":
        return ev.get("origin") not in ("watchdog", "liveness")
    if e.what in ("approval denied", "approval approved"):
        return ev.get("kind") in STALE_APPROVAL_KINDS or bool(ev.get("subject_resolved"))
    if e.what == "landing_enqueued":
        return ev.get("origin") == "manual"
    if e.what == "nudge instruction recorded":
        return bool(ev.get("urgent"))
    return False


def is_coordinator(e) -> bool:
    return str(e.evidence.get("actor", "")).startswith("operator_agent:coordinator")


def _is_decision(e) -> bool:
    return e.what in ("question_answered", "operator accepted") or (
        e.what == "approval approved" and e.evidence.get("kind") == "acceptance")


def _landings(repo: str, target: str, since: datetime) -> tuple[int, int]:
    out = subprocess.run(["git", "log", "--first-parent", f"--since={since.isoformat()}", "--format=%s", target],
                         cwd=repo, capture_output=True, text=True)
    auto = manual = 0
    for s in out.stdout.splitlines():
        if s.startswith(AUTO_SUBJECTS):
            auto += 1
        elif s.startswith(MANUAL_SUBJECT):
            manual += 1
    return auto, manual


def compute(store, *, since_iso: str, until_iso: str | None = None, targets=None, method: str = "events") -> dict:
    since = _t(since_iso)
    until = _t(until_iso) if until_iso else datetime.now(timezone.utc)
    touches = decisions = coordinator_actions = asks = 0
    incorporated: set[str] = set()
    orphan_by_job: dict[str, float] = {}
    open_since: dict[str, datetime] = {}
    for job in store.list_jobs():
        for e in store.events(job.job_id):
            ts = _t(e.ts)
            if ts < since or ts > until:
                continue
            ev = e.evidence
            if e.what == "coordinator_episode" and ev.get("outcome") in ("asked", "gave_up"):
                asks += 1
            if is_coordinator(e):
                if _is_touch(e):
                    coordinator_actions += 1
                continue
            if e.what in ("incorporated", "auto_merged", "landed_by_train", "incorporated_via_followup"):
                incorporated.add(job.job_id)
            if _is_touch(e):
                touches += 1
            elif _is_decision(e):
                decisions += 1
            if e.what == "liveness_orphan":
                open_since[job.job_id] = _t(str(ev.get("since") or e.ts))
            if e.what == "liveness_actor" and job.job_id in open_since:
                at = _t(str(ev.get("at") or e.ts))
                orphan_by_job[job.job_id] = orphan_by_job.get(job.job_id, 0.0) + (at - open_since.pop(job.job_id)).total_seconds() / 3600
        if job.job_id in open_since:  # still orphaned now
            orphan_by_job[job.job_id] = orphan_by_job.get(job.job_id, 0.0) + (until - open_since.pop(job.job_id)).total_seconds() / 3600
    auto = manual = 0
    for repo, target in targets or []:
        a, mn = _landings(repo, target, since)
        auto, manual = auto + a, manual + mn
    n = len(incorporated)
    return {"touches": touches, "decisions": decisions, "coordinator_actions": coordinator_actions,
            "asks": asks, "incorporated_jobs": n,
            "touches_per_job": (touches / n) if n else float(touches),
            "orphan": {"total_h": round(sum(orphan_by_job.values()), 3),
                       "max_h": round(max(orphan_by_job.values(), default=0.0), 3), "by_job": orphan_by_job},
            "landings": {"auto": auto, "manual": manual, "ratio": (auto / (auto + manual)) if (auto + manual) else 0.0}}


def render(out: dict) -> str:
    return (f"touches/job {out['touches_per_job']:.2f} ({out['touches']} touches, {out['decisions']} decisions, "
            f"{out['incorporated_jobs']} merged) · orphan max {out['orphan']['max_h']:.1f}h total "
            f"{out['orphan']['total_h']:.1f}h · auto-land {out['landings']['auto']}/"
            f"{out['landings']['auto'] + out['landings']['manual']} = {out['landings']['ratio']:.2f}"
            f" · coordinator {out['coordinator_actions']} actions, {out['asks']} asks")
