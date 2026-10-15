"""Landing-train morning report: one dated markdown file per day + one tg notice after report_hour."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from steward.store import Store

PSEUDO = "_train"


def _dir(store: Store) -> Path:
    d = Path(store.home) / "train-report"
    d.mkdir(parents=True, exist_ok=True)
    return d


def append(store: Store, line: str, *, now: float) -> Path:
    t = datetime.fromtimestamp(now)
    p = _dir(store) / f"{t:%Y-%m-%d}.md"
    with p.open("a") as fh:
        fh.write(f"- {t:%H:%M} {line}\n")
    return p


LIMIT = 3500
HEADER = "Steward landing train — morning report\n\n"


def due(store: Store, *, now: float, report_hour: int) -> bool:
    t = datetime.fromtimestamp(now)
    return t.hour >= report_hour and store.kv_get(PSEUDO, "report_sent") != f"{t:%Y-%m-%d}"


def _fit(body: str, limit: int) -> str:
    """Keep the newest lines (the tail) within `limit`, saying how many earlier lines were left out."""
    if len(body) <= limit:
        return body
    lines = body.splitlines()
    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        if size + len(line) + 1 > limit - 80:      # room for the "earlier lines" marker
            break
        kept.append(line)
        size += len(line) + 1
    cut = len(lines) - len(kept)
    return f"… {cut} earlier lines (full report in the train-report files)\n" + "\n".join(reversed(kept))


def _pending_carried(store: Store) -> list[str]:
    """Ids from `owner_decision_carried` events (cleanup.py) whose approval is still pending / blocker still open."""
    from steward import approvals, blockers
    out: list[str] = []
    for job in store.list_jobs():
        ids = [i for e in store.events_of(job.job_id, ("owner_decision_carried",)) for i in e.evidence.get("ids", [])]
        for i in dict.fromkeys(ids):
            rec = approvals.get_approval(store, job.job_id, i)
            b = None if rec is not None else blockers.get(store, i)
            if (rec is not None and rec.get("status") == "pending") or (b is not None and b.is_open):
                out.append(f"{job.job_id[:8]}:{i}")
    return out


def _zero_touch_section(store: Store, now: float) -> str:
    from steward import config, zero_touch_metrics as ztm
    lines = []
    try:
        s = config.train_settings()
        targets = [(s["repo"], s["target"])] if s.get("repo") else []
        since = datetime.fromtimestamp(now - 86400, timezone.utc).isoformat()
        until = datetime.fromtimestamp(now, timezone.utc).isoformat()
        lines.append("zero-touch (24h): " + ztm.render(ztm.compute(store, since_iso=since, until_iso=until, targets=targets)))
    except Exception as exc:  # noqa: BLE001 - the report never fails on its metrics
        lines.append(f"zero-touch metrics unavailable: {str(exc)[:120]}")
    try:
        from steward import failure_modes as fmod
        since = datetime.fromtimestamp(now - 86400, timezone.utc).isoformat()
        lines.append(fmod.summary_line(fmod.compute(store, since_iso=since)))
    except Exception as exc:  # noqa: BLE001 - the report never fails on its metrics
        lines.append(f"failure-mode summary unavailable: {str(exc)[:120]}")
    try:
        pending = _pending_carried(store)
    except Exception:  # noqa: BLE001
        pending = []
    if pending:
        lines.append("still waiting on you: " + ", ".join(pending))
    return "\n".join(lines)


def maybe_send_daily(store: Store, *, now: float, report_hour: int, send: Callable[[str], bool]) -> bool:
    t = datetime.fromtimestamp(now)
    today = f"{t:%Y-%m-%d}"
    if not due(store, now=now, report_hour=report_hour):
        return False
    yday = f"{t - timedelta(days=1):%Y-%m-%d}"
    parts = []
    for day in (yday, today):
        p = _dir(store) / f"{day}.md"
        if p.exists():
            parts.append(f"{day}\n{p.read_text().strip()}")
    body = "\n\n".join(parts) or "no trains ran"
    tail = "\n\n" + _zero_touch_section(store, now)
    if not send(HEADER + _fit(body, LIMIT - len(HEADER) - len(tail)) + tail):
        return False
    store.kv_set(PSEUDO, "report_sent", today)
    return True
