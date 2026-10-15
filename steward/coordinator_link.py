"""The shared contract between Steward and the coordinator daemon (spec 5.1): state dir, heartbeat, KILL file, the hold
record, the ask delivery, the dash line and the health component. The hold sweep (`holding`, `still_stuck`, `sweep`) delivers the ask when the coordinator cannot."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

FRESH_S = 120.0
HOLD_S = 1200.0
HOLD_KEY = "coordinator_hold"


def state_dir() -> Path:
    """$STEWARD_COORDINATOR_STATE, else a sibling of the Steward home (R16)."""
    env = os.environ.get("STEWARD_COORDINATOR_STATE")
    if env:
        return Path(env).expanduser()
    from steward.store import resolve_home

    return resolve_home().parent / "steward-coordinator"


def kill_file() -> Path:
    return state_dir() / "KILL"


def killed() -> bool:
    return kill_file().exists()


def _heartbeat_file() -> Path:
    return state_dir() / "heartbeat"


def write_heartbeat(*, cursor: int, head: str, root: str, now: float | None = None) -> None:
    """Atomic write (tmp + os.replace) so a reader never sees half a file."""
    path = _heartbeat_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"ts": time.time() if now is None else now, "cursor": int(cursor), "head": head, "root": root,
            "pid": os.getpid()}
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(body))
    os.replace(tmp, path)


def read_heartbeat() -> dict | None:
    try:
        data = json.loads(_heartbeat_file().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def heartbeat_fresh(now: float | None = None, max_age_s: float = FRESH_S) -> bool:
    hb = read_heartbeat()
    if not hb:
        return False
    try:
        age = (time.time() if now is None else now) - float(hb["ts"])
    except (KeyError, TypeError, ValueError):
        return False
    return age < max_age_s


def component(head_fn=None, now: float | None = None):
    """The coordinator as a health Component (shown by `steward health`, never by the dash tick; R10)."""
    from steward import health

    if killed():
        return health.Component("coordinator", None, "stopped by KILL file")
    hb = read_heartbeat()
    if not hb:
        return health.Component("coordinator", None, "never started")
    try:
        age = int((time.time() if now is None else now) - float(hb["ts"]))
    except (KeyError, TypeError, ValueError):
        return health.Component("coordinator", False, "heartbeat unreadable", ["heartbeat unreadable"])
    if age >= FRESH_S:
        return health.Component("coordinator", False, f"silent for {age}s", [f"silent for {age}s"])
    stale = health.stale_code({"head": hb.get("head"), "root": hb.get("root")}, head_fn)
    if stale:
        return health.Component("coordinator", False, stale, [stale])
    return health.Component("coordinator", True, f"ok, cursor {hb.get('cursor')}")


# -- hold record (kv `coordinator_hold` on the job; R1) ---------------------------------------------------------------


def _record_event(store, job_id: str, what: str, evidence: dict) -> None:
    from steward.models import Judgment

    try:
        store.append_event(job_id, what=what, evidence=evidence, rule="coordinator", judgment=Judgment.code)
    except Exception:  # noqa: BLE001 - the audit line never breaks the hold
        pass


def open_hold(store, job_id: str, *, episode_id: str, trigger: str, now: float) -> dict:
    """Record that the coordinator is working on a stuck job. Overwrites any earlier record for the job; an open hold
    for a different episode is closed `superseded` first (audit event)."""
    prior = hold(store, job_id)
    if prior is not None and prior.get("episode_id") != episode_id:
        close_hold(store, job_id, "superseded", now=now)
    job = store.get_job(job_id)
    rec = {"state": "open", "episode_id": episode_id, "trigger": trigger, "opened_at": now,
           "job_state": job.state.value if job else None, "job_updated_at": job.updated_at if job else None,
           "closed_at": None, "how": None, "delivery": None}
    store.kv_set(job_id, HOLD_KEY, rec)
    _record_event(store, job_id, "coordinator_hold_opened", {"episode_id": episode_id, "trigger": trigger})
    return rec


def hold(store, job_id: str) -> dict | None:
    """The hold record when it is still open, else None."""
    rec = store.kv_get(job_id, HOLD_KEY)
    return rec if isinstance(rec, dict) and rec.get("state") == "open" else None


def close_hold(store, job_id: str, how: str, *, now: float, delivery: str | None = None) -> None:
    """Close an open hold (no-op when there is none). `delivery` is the ask outcome when a line was sent."""
    rec = hold(store, job_id)
    if rec is None:
        return
    rec = {**rec, "state": "delivered" if delivery == "delivered" else "closed", "closed_at": now, "how": how,
           "delivery": delivery}
    store.kv_set(job_id, HOLD_KEY, rec)
    _record_event(store, job_id, "coordinator_hold_closed",
                  {"episode_id": rec.get("episode_id"), "how": how, "delivery": delivery})


def deliver_ask(store, job_id: str, runner, *, reason: str, episode_id: str, now: float) -> str:
    """The one line to the owner (R2): the creator push line, then the phone notifier once. Never raises; returns the push
    outcome (delivered | skipped:<r> | failed). `now` is accepted for symmetry with the sweep."""
    from steward import notify, push

    try:
        out = push.notify_creator(store, job_id, "needs_you", runner,
                                  facts={"reason": reason[:160], "action": "your call; see the dash"})
    except Exception:  # noqa: BLE001
        out = "failed"
    if out != "delivered" and out != "failed" and not out.startswith("skipped:"):
        out = f"skipped:{out}"
    try:
        notify.emit(store, notify.notifier_from_env(runner), notify.PhoneEvent(
            kind="NEEDS_YOU", job_id=job_id, title=f"Steward: needs you ({job_id[:8]})"[:40], body=reason[:140],
            deep_link=None, revision=f"coordinator:{episode_id}"))
    except Exception:  # noqa: BLE001
        pass
    return out


def dash_line(store, job_id: str, now: float | None = None) -> str:
    """One dash line: an open hold, else the latest episode within 24 h, else ''."""
    from datetime import datetime

    now = time.time() if now is None else now
    rec = store.kv_get(job_id, HOLD_KEY)
    rec = rec if isinstance(rec, dict) else None
    if rec and rec.get("state") == "open":
        opened = float(rec.get("opened_at") or now)
        by = time.strftime("%H:%M", time.localtime(opened + HOLD_S))
        return f"⚙ coordinator: working on it ({max(0, int((now - opened) // 60))}m; tells you by {by} if unsettled)"
    evs = store.events_of(job_id, ("coordinator_episode",))
    if not evs:
        return ""
    ev = evs[-1]
    try:
        if now - datetime.fromisoformat(ev.ts).timestamp() > 86400:
            return ""
    except (TypeError, ValueError):
        pass
    e = ev.evidence or {}
    out = f"⚙ coordinator: {e.get('outcome')} — {str(e.get('diagnosis') or '')[:80]} ({', '.join(e.get('actions') or [])})"
    delivery = (rec or {}).get("delivery")
    if rec and delivery and delivery != "delivered" and rec.get("episode_id") == e.get("episode_id"):
        out += f" · ask not delivered ({delivery})"
    return out


_TERMINAL = ("STOPPED", "ACCEPTED", "RELEASED", "FAILED")


def holding(store, job_id: str, now: float | None = None) -> bool:
    """An open hold AND a fresh heartbeat AND no KILL AND the job still where the hold found it (an orphan hold never
    suppresses a notice: the sweep would close it silently): a needs-you notice for this job waits for the coordinator."""
    rec = hold(store, job_id)
    if rec is None or killed():
        return False
    job = store.get_job(job_id)
    if job is None or not still_stuck(store, job, rec) or job.updated_at != rec.get("job_updated_at"):
        return False
    return heartbeat_fresh(now)


def still_stuck(store, job, rec: dict) -> bool:
    """Is the job still in the state the hold was opened for?"""
    if job is None:
        return False
    if str(rec.get("trigger") or "").startswith("needs_human:"):
        return job.state.value == "NEEDS_HUMAN"
    return job.state.value not in _TERMINAL and job.updated_at == rec.get("job_updated_at")


def _park_reason(store, job_id: str) -> str:
    """Why the job parked: the park event (`ownership.park_event`), else the last NEEDS_HUMAN state change."""
    try:
        from steward import ownership

        events = store.events(job_id)
        park = ownership.park_event(events)
        if park is None:
            job = store.get_job(job_id)
            stamp = job.updated_at if job else None
            park = next((e for e in reversed(events) if e.rule != "coordinator" and e.ts == stamp), None)
        if park is None:
            park = next((e for e in reversed(events) if e.rule != "coordinator"), None)
        if park is None:
            return ""
        e = park.evidence or {}
        text = e.get("detail") or e.get("reason")
        return (text.strip() if isinstance(text, str) and text.strip() else park.what or "")[:100]
    except Exception:  # noqa: BLE001
        return ""


def sweep(store, runner, *, now: float | None = None) -> int:
    """Control TickStep (R1): deliver the held ask when the coordinator cannot settle it (KILL, silent heartbeat, 20 min),
    close holds whose job moved on. Returns the number of lines delivered. One attempt per hold; never raises."""
    now = time.time() if now is None else now
    sent = 0
    try:
        rows = store.kv_rows(HOLD_KEY)
    except Exception:  # noqa: BLE001
        return 0
    for job_id, rec in rows:
        try:
            if not isinstance(rec, dict) or rec.get("state") != "open":
                continue
            job = store.get_job(job_id)
            if job is None or not still_stuck(store, job, rec):
                close_hold(store, job_id, "superseded", now=now)
                continue
            if killed():
                how, why = "killed", "coordinator stopped (KILL)"
            elif not heartbeat_fresh(now):
                how, why = "coordinator_down", "coordinator not running"
            elif now - float(rec.get("opened_at") or now) >= HOLD_S:
                how, why = "expired", "coordinator could not settle it in 20 min"
            else:
                continue
            reason = f"{_park_reason(store, job_id)}; {why}".lstrip("; ")
            out = deliver_ask(store, job_id, runner, reason=reason, episode_id=str(rec.get("episode_id") or ""),
                              now=now)
            close_hold(store, job_id, how, now=now, delivery=out)
            sent += 1
        except Exception:  # noqa: BLE001 - one bad hold never blocks the tick
            continue
    return sent
