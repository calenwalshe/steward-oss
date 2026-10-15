"""`[ explain ]` / `steward explain JOB` / MCP `explain_job`: a STANDARDIZED, deterministic explanation of one job.

No model is involved: every line is built from Steward's own data (worker transcript tail, stored ETA, events, git log,
approvals, question queue, token spend, detector findings). The shape never changes, so the owner learns where to look:

  Explain <id8> <name> · <STATE>
  Now:       what the worker is doing, one plain sentence (current tool/helper and how long)
  Progress:  units done/total, current unit, ETA range or the finishing stage
  Milestones: the last 3 commits / verifications / finish-line results, with how long ago
  Blocking:  approvals, questions, conflicts waiting on the owner, else "nothing"
  Spend:     so far, and per hour
  Health:    flags, else "ok"

`data()` returns the same thing as a dict (the MCP tool); `text()` renders it; `primer()` wraps it for a chat.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from steward import approvals as approvals_module
from steward import jobview
from steward.diagnosis import redact
from steward.store import Store

MILESTONES = 3
_MILESTONE_EVENTS = {
    "verification recorded": "verification recorded",
    "checks passed at exact commit": "checks passed",
    "checks passed; acceptance unresolved": "checks passed, acceptance unresolved",
    "finish_line_met": "finish line met",
    "finish_line_not_met": "finish line not met",
    "worker reported done": "worker reported done",
    "worker attempt started": "worker attempt started",
    "incorporated": "merged into its branch",
    "operator accepted": "accepted",
}
_INCOMPLETE_UNITS = "no unit count yet"


def _cap(text: object, n: int = 110) -> str:
    one = redact(" ".join(str(text or "").split()))
    return one if len(one) <= n else one[: n - 1] + "…"


def _epoch(ts: str | None) -> float | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _age(now: float, at: float | None) -> str:
    return f"{jobview.ago(max(now - at, 0))} ago" if at is not None else "earlier"


def _now_sentence(store: Store, job_id: str, state: str, now: float, projects_dir) -> str:
    from steward import transcript

    if state == "PAUSED":
        return "The job is paused; the worker does nothing until it is resumed."
    if state == "NEEDS_HUMAN":
        return "The worker has stopped and the job is waiting on a decision."
    if state not in jobview.LIVE:
        return f"The job is {state.lower().replace('_', ' ')}; no worker is running."
    try:
        cur = transcript.live_line(store, job_id, projects_dir=projects_dir)
    except Exception:  # noqa: BLE001 - an unreadable transcript is a fact, not a failure
        return "The worker is running, but its transcript could not be read."
    tool = cur.get("running_tool")
    if tool:
        since = _epoch(cur.get("since"))
        how_long = f" for {jobview.ago(max(now - since, 0))}" if since is not None else ""
        return f"The worker is running {_cap(tool, 140)}{how_long}."
    if cur.get("last_activity_ts"):
        at = _epoch(cur["last_activity_ts"])
        return f"The worker is between tool calls (last activity {_age(now, at)}); {_cap(cur.get('now'), 120)}."
    return "The worker has no transcript activity yet."


def _progress(store: Store, job_id: str, now: float) -> str:
    from steward import eta as eta_module
    from steward import efficiency_watch

    try:
        stored = store.kv_get(job_id, efficiency_watch.STATE_KEY)
    except Exception:  # noqa: BLE001
        stored = None
    eta = stored.get("eta") if isinstance(stored, dict) else None
    if not isinstance(eta, dict):
        return "no progress data yet (no ETA stored for this job)"
    parts = []
    total = eta.get("units_total")
    if total is not None:
        parts.append(f"units {eta.get('units_done', 0)}/{'' if eta.get('units_exact') else '~'}{total}")
    else:
        parts.append(_INCOMPLETE_UNITS)
    if eta.get("unit_label"):
        parts.append(f"current unit {_cap(eta['unit_label'], 60)}")
    if eta.get("fin_stage"):
        parts.append(f"finishing stage: {_cap(eta['fin_stage'], 60)}")
    elif eta.get("stage"):
        parts.append(f"stage {_cap(eta['stage'], 60)}")
    try:
        shown = eta_module.render(eta, now)["text"]
    except Exception:  # noqa: BLE001
        shown = None
    if shown:
        parts.append(f"eta {shown}")
    return " · ".join(parts)


def _milestones(store: Store, job_id: str, events, now: float, runner) -> list[str]:
    found: list[tuple[float, str]] = []
    for e in events:
        label = _MILESTONE_EVENTS.get(e.what)
        at = _epoch(e.ts)
        if label and at is not None:
            found.append((at, label))
    wt = store.home / "worktrees" / job_id
    if wt.is_dir():
        try:
            if runner is None:
                from steward.runner_exec import SubprocessRunner

                runner = SubprocessRunner()
            res = runner.run(["git", "log", "-8", "--format=%ct%x09%h%x09%s"], cwd=wt, timeout=15)
            for ln in (res.stdout or "").splitlines():
                ct, _, rest = ln.partition("\t")
                sha, _, subject = rest.partition("\t")
                if ct.strip().isdigit():
                    found.append((float(ct), f"commit {sha} {_cap(subject, 80)}"))
        except Exception:  # noqa: BLE001 - no git history is just fewer milestones
            pass
    found.sort(key=lambda x: x[0], reverse=True)
    return [f"{_age(now, at)}: {label}" for at, label in found[:MILESTONES]]


def _blocking(store: Store, job_id: str, state: str, events) -> list[str]:
    out = []
    try:
        for a in approvals_module.list_approvals(store, job_id, status="pending"):
            kind = "acceptance decision" if a.get("kind") == "acceptance" else "approval"
            out.append(f"{kind} {a['id']}: {_cap(a.get('question'), 100)}")
    except Exception:  # noqa: BLE001
        pass
    try:
        from steward import questions

        qs = questions.pane_summary(store, job_id)
        if qs.get("waiting"):
            out.append(f"{qs['waiting']} open question{'s' if qs['waiting'] != 1 else ''}"
                       f" ({qs.get('needs_you', 0)} need an owner answer, {qs.get('starred', 0)} have a recorded default)")
    except Exception:  # noqa: BLE001
        pass
    last = next((e for e in reversed(events) if e.what in ("incorporate_blocked", "incorporated", "incorporate_noop")), None)
    if last is not None and last.what == "incorporate_blocked":
        out.append("a merge conflict blocks incorporation: " + _cap(last.evidence.get("reason") or last.evidence.get("detail") or "see merge state", 100))
    if state == "PAUSED":
        out.append("the job is paused and needs a resume")
    return out


def _spend(store: Store, job_id: str, job, now: float, projects_dir) -> tuple[str, dict | None]:
    try:
        from steward import tokens

        t = tokens.job_tokens(store, job_id, now=now, projects_dir=projects_dir)
    except Exception:  # noqa: BLE001
        return "spend unavailable", None
    usd = t["total"].get("usd", 0.0)
    hour = t["last_hour"].get("usd", 0.0)
    born = _epoch(job.created_at)
    hours = max((now - born) / 3600.0, 1 / 60) if born else None
    avg = f" · ${usd / hours:.2f}/h average over its life" if hours else ""
    return f"${usd:.2f} so far · ${hour:.2f} in the last hour{avg}", t


def _health(store: Store, job_id: str, state: str, events, tj, now: float) -> list[str]:
    flags = []
    from steward import delivery

    kind, detail = jobview.liveness(state, jobview.event_dicts(events), datetime.fromtimestamp(now, timezone.utc),
                                    delivery=delivery.liveness_hint(store, job_id, state))
    if kind in ("quiet", "bad"):
        flags.append(detail)
    try:
        from steward import stability

        if state in ("RUNNING", "PAUSED"):
            behind = stability.runner_outdated(store, job_id)
            if behind:
                flags.append(f"runner outdated ({behind} commit{'s' if behind != 1 else ''} behind)")
        flags += [_cap(w, 140) for w in stability.spend_warnings(tj)]
    except Exception:  # noqa: BLE001
        pass
    try:
        from steward import efficiency_watch

        flags += [_cap(w, 140) for w in efficiency_watch.pane_warnings(store, job_id)]
    except Exception:  # noqa: BLE001
        pass
    for e in events[-40:]:
        if e.what in ("hung_call_escalated", "worker pane closed", "worker error frame", "recovery escalated to human"):
            flags.append(f"{e.what} {_age(now, _epoch(e.ts))}")
    seen, uniq = set(), []
    for f in flags:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def _name(store: Store, job) -> str:
    try:
        from steward import tabnames

        return tabnames.job_name(store, job, persist=False)
    except Exception:  # noqa: BLE001
        return ""


def data(store: Store, job_id: str, *, now: float | None = None, projects_dir: Path | None = None, runner=None) -> dict:
    """The explanation as fields. Raises `KeyError` for an unknown job."""
    now = time.time() if now is None else now
    job = store.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    state = job.state.value if hasattr(job.state, "value") else str(job.state)
    events = store.events(job_id)
    spend, tj = _spend(store, job_id, job, now, projects_dir)
    blocking = _blocking(store, job_id, state, events)
    health = _health(store, job_id, state, events, tj, now)
    return {
        "job_id": job_id, "name": _name(store, job), "state": state,
        "now": _now_sentence(store, job_id, state, now, projects_dir),
        "progress": _progress(store, job_id, now),
        "milestones": _milestones(store, job_id, events, now, runner),
        "blocking": blocking or ["nothing"],
        "spend": spend,
        "health": health or ["ok"],
        **({"deadline": dl} if (dl := _deadline(store, job_id)) else {}),
    }


def _deadline(store: Store, job_id: str) -> str | None:
    try:
        from steward import deadline_status

        return deadline_status.line(store, job_id)
    except Exception:  # noqa: BLE001 - a read never breaks explain
        return None


def render(d: dict) -> str:
    lines = [f"Explain {d['job_id'][:8]}{' ' + d['name'] if d.get('name') else ''} · {d['state']}",
             f"Now: {d['now']}",
             f"Progress: {d['progress']}",
             *([f"Deadline: {d['deadline']}"] if d.get("deadline") else []),
             "Last milestones:"]
    lines += [f"  - {m}" for m in d["milestones"]] or ["  - none yet"]
    lines.append("Blocking / waiting on you: " + ("nothing" if d["blocking"] == ["nothing"] else ""))
    if d["blocking"] != ["nothing"]:
        lines += [f"  - {b}" for b in d["blocking"]]
    lines.append(f"Spend: {d['spend']}")
    lines.append("Health: " + ("ok" if d["health"] == ["ok"] else "; ".join(d["health"])))
    return "\n".join(lines)


def text(store: Store, job_id: str, **kw) -> str:
    return render(data(store, job_id, **kw))
