"""Shared read-side helpers for the human-facing job views (dashboard rows,
the phone screen, alerts): why a job wants the owner, and whether it is alive.

Pure functions over plain dicts (approval records and event dicts as
`steward approvals --json` / `steward events --json` print them) so the
bash-driven dashboard and the in-process phone screen share one definition.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# Liveness: live states get a spinner.
LIVE = frozenset({"CONTRACTED", "RUNNING", "WORKER_FINISHED", "CHECKS_PASSED"})
SUPERVISED = LIVE | {"PAUSED", "NEEDS_HUMAN"}  # states that get supervisor rows
QUIET_AFTER = 600  # seconds without a Steward event before a live job shows as quiet
OWNERSHIP_REVIEW = ("awaiting_technical_review", "technical_review_unavailable", "waiting_external")
_QUESTION_ONLY = frozenset({"owner_question"})
_ACCEPTANCE_ONLY = frozenset({"acceptance_decision", "owner_question"})
CARRIED_PREFIX = "decision carried forward"  # ownership.carried_label: an owner question outliving its finished job


def ago(secs: float) -> str:
    secs = int(secs)
    return f"{secs}s" if secs < 60 else f"{secs // 60}m" if secs < 3600 else f"{secs // 3600}h{secs % 3600 // 60:02d}m"


def event_dicts(events) -> list[dict]:
    """Store `Event`s -> the dict shape `steward events --json` prints."""
    return [{"seq": e.seq, "ts": e.ts, "what": e.what, "evidence": e.evidence,
             "rule": e.rule, "judgment": getattr(e.judgment, "value", e.judgment)} for e in events]


def pending_ask(approvals: list[dict]) -> dict | None:
    """The first pending approval that is a worker permission ask (not the acceptance one)."""
    pending = [a for a in approvals or [] if a.get("status") == "pending" and not a.get("awaiting_review")]
    return next((a for a in pending if a.get("kind") != "acceptance" and "acceptance" not in a["question"]), None)


def _resolver_age(ask: dict) -> int:
    try:
        created = datetime.fromisoformat(ask["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - created).total_seconds()))
    except (KeyError, ValueError, TypeError):
        return 0


def ownership_status(events: list[dict]) -> dict | None:
    for e in reversed(events or []):
        if e.get("what") == "ownership_status":
            return e.get("evidence") or {}
    return None


def _own(events: list[dict], ownership: dict | None) -> dict | None:
    own = ownership if ownership else ownership_status(events)
    return own if isinstance(own, dict) and own.get("status") not in (None, "none") else None


def reason(approvals: list[dict], state: str, events: list[dict], questions: dict | None = None,
           ownership: dict | None = None) -> str | None:
    """Why the job wants the owner, in a few words; None when it doesn't. `questions` (a questions.summary dict) adds the
    waiting question queue as the third, lowest-precedence reason: only the phone screen passes it."""
    ask = pending_ask(approvals)
    if ask:
        if ask.get("resolver_pending"):  # a resolver is still deciding: the owner is only asked if it can't
            return f"resolver deciding ({_resolver_age(ask)}s)"
        return "approve? " + ask["question"].removeprefix("worker permission: ")
    if state != "NEEDS_HUMAN":
        return carried(state, events, ownership) or _questions_reason(questions)
    own = _own(events, ownership)
    if own is not None:
        kinds = set(own.get("owner_kinds") or [])
        if own["status"] in OWNERSHIP_REVIEW or (own["status"] == "needs_owner" and kinds and kinds <= _QUESTION_ONLY):
            return _questions_reason(questions)
        if own["status"] == "needs_owner" and not kinds <= _ACCEPTANCE_ONLY:
            return str(own.get("text") or "")[:200] or None
    unresolved = next((e for e in reversed(events) if e["what"] in ("checks passed; acceptance unresolved", "checks unverified; acceptance unresolved")), None)
    why = "; ".join(unresolved["evidence"].get("reasons", [])) if unresolved else (events[-1]["what"] if events else "")
    return "accept or reject? " + (why or "checks passed, no auto-accept rule")


def carried(state: str, events: list[dict], ownership: dict | None = None) -> str | None:
    """The ownership text of a finished (ACCEPTED/RELEASED) job whose unanswered owner question carried forward."""
    if state not in ("ACCEPTED", "RELEASED"):
        return None
    own = _own(events, ownership)
    text = str((own or {}).get("text") or "")
    return text[:200] if own and own["status"] == "needs_owner" and text.startswith(CARRIED_PREFIX) else None


def _questions_reason(questions: dict | None) -> str | None:
    n = (questions or {}).get("waiting") or 0
    if not n:
        return None
    return f"answer {n} question{'s' if n != 1 else ''} ({questions.get('starred', 0)} ★)"


_HB_WORDS = {"progressing": "working", "quiet_known_process": "in a long step", "active": "working"}


def liveness(state: str, events: list[dict], now: datetime | None = None, ownership: dict | None = None,
             *, delivery: dict | None = None) -> tuple[str, str]:
    """`(kind, detail)`; kind is live|quiet|paused|waiting|done|bad."""
    age, hb = None, None
    if events:
        now = now or datetime.now(timezone.utc)
        age = (now - datetime.fromisoformat(events[-1]["ts"])).total_seconds()
        hb = next((e["evidence"].get("to") for e in reversed(events) if e["what"] == "heartbeat class changed"), None)
    when = f"{ago(age)} ago" if age is not None else "no events"
    if state in LIVE:
        if age is not None and age < QUIET_AFTER:
            return "live", f"{_HB_WORDS.get(hb, hb or 'working')} · {when}"
        return "quiet", f"quiet · no activity {when}"
    if state == "PAUSED":
        return "paused", f"paused · {when}"
    if state == "NEEDS_HUMAN":
        own = _own(events, ownership)
        st = own.get("status") if own else None
        if st == "awaiting_technical_review":
            return "live", f"steward reviewing · {when}"
        if st == "technical_review_unavailable":
            retry = own.get("next_retry_at")
            hhmm = datetime.fromtimestamp(float(retry)).strftime("%H:%M") if retry else "?"
            return "quiet", f"technical review unavailable · retry {hhmm}"
        if st == "waiting_external":
            return "paused", f"{str(own.get('text') or 'waiting')[:60]} · {when}"
        return "waiting", f"waiting on you · {when}"
    decision = carried(state, events, ownership)
    if decision:
        return "waiting", f"{decision[:60]} · {when}"
    if state == "ACCEPTED" and delivery and not delivery.get("delivered"):
        return "waiting", f"{delivery.get('text') or 'delivery pending'} · {when}"
    if state in ("ACCEPTED", "RELEASED"):
        return "done", when
    return "bad", when


def deadline_part(store, job_id: str, now=None) -> str | None:
    """v2.2: the deadline sign (or overrun banner) beside the ETA, from the persisted canonical object. Pure read."""
    try:
        from steward import deadline, deadline_status

        if store.kv_get(job_id, deadline.K_COMMIT) is None:
            return None
        p = deadline_status.project(store, job_id, now)
        if not p or not p.get("active"):
            return None
        return "\u23f1 " + (p["banner"] or f"{p['sign']} · {p['left']}")
    except Exception:  # noqa: BLE001 - decoration; never breaks a row
        return None


def eta_part(state_eta, now: float | None = None) -> str | None:
    """The ETA text of a job header: the stored efficiency_state `eta` counted down to `now` (epoch seconds), or
    None when there is no stored ETA. The same string is the phone hint part and the API `shown` (G13, G16)."""
    if not isinstance(state_eta, dict):
        return None
    import time
    from steward import eta as eta_module
    try:
        return eta_module.render(state_eta, time.time() if now is None else now)["header"]
    except Exception:  # noqa: BLE001 - the ETA is decoration; a bad stored object never breaks a view
        return None


def live_eta(store, job_id: str, state: str) -> dict | None:
    """The stored ETA dict of a live job with the efficiency switch on, else None (G13: surfaces only read it)."""
    if state not in LIVE:
        return None
    try:
        from steward import efficiency_watch
        if efficiency_watch.mode(store, job_id) == "off":
            return None
        stored = store.kv_get(job_id, efficiency_watch.STATE_KEY)
        eta = stored.get("eta") if isinstance(stored, dict) else None
        return eta if isinstance(eta, dict) else None
    except Exception:  # noqa: BLE001 - the ETA is decoration
        return None


def eta_line(stored, now: float | None = None) -> str | None:
    """The one-line ETA detail of a job page: `eta ~1h40 (1h20–2h10) · units 3/7 · from cycle times`, plus
    ` (no Jev)` when Jev would have labelled it and did not (G11)."""
    if not isinstance(stored, dict):
        return None
    import time
    from steward import eta as eta_module
    try:
        detail = eta_module.render(stored, time.time() if now is None else now)["detail"]
    except Exception:  # noqa: BLE001
        return None
    note = eta_module.no_jev_note(stored)
    return detail if detail.endswith(note) else detail + note


def with_eta(detail: str, part: str | None, room: int) -> str:
    """`detail · part` when both fit in `room` visible cells, else `detail` alone: the ETA is dropped first, the
    liveness detail is never cut to make room for it."""
    if not part:
        return detail
    both = f"{detail} · {part}"
    return both if vlen(both) <= room else detail


_TOKEN = re.compile(r"\033\[[0-9;]*m|\{SPIN\}|.", re.S)
_TRAIL = set(" \u00b7,;:-")


def _tw(tok: str) -> int:
    return 0 if tok.startswith("\033") else 1  # ANSI is free; {SPIN} is one cell


def vlen(text: str) -> int:
    """Visible width: ANSI colours are free and the {SPIN} placeholder is one cell."""
    return sum(_tw(t) for t in _TOKEN.findall(text))


def fit(text: str, width: int) -> str:
    """`text` within `width` visible cells. Too long: cut at the last word boundary and end with
    "…" (a lone word longer than the room is hard-cut, the only way to obey the width).
    ANSI-aware; a cut coloured text is reset so colour never leaks."""
    if width <= 0:
        return ""
    if vlen(text) <= width:
        return text
    toks, kept, w, nxt = _TOKEN.findall(text), [], 0, None
    for i, t in enumerate(toks):
        if w + _tw(t) > width - 1:
            nxt = t
            break
        kept.append(t)
        w += _tw(t)
    if nxt is not None and nxt != " ":
        spaces = [i for i, t in enumerate(kept) if t == " "]
        if spaces:
            kept = kept[:spaces[-1]]
    while True:  # drop dangling separators before the ellipsis
        vis = [i for i, t in enumerate(kept) if not t.startswith("\033")]
        if vis and kept[vis[-1]] in _TRAIL:
            del kept[vis[-1]]
        else:
            break
    styled = any(t.startswith("\033") for t in kept)
    return "".join(kept) + "\u2026" + ("\033[0m" if styled else "")


def wrap(text: str, width: int, max_lines: int = 2) -> list[str]:
    """Plain `text` wrapped at word boundaries to at most `max_lines` lines; the last is ellipsized."""
    rem, lines = " ".join((text or "").split()).split(" "), []
    rem = [x for x in rem if x]
    while rem and len(lines) < max_lines:
        if len(lines) == max_lines - 1:
            lines.append(fit(" ".join(rem), width))
            break
        cur = rem.pop(0)
        while rem and len(cur) + 1 + len(rem[0]) <= width:
            cur += " " + rem.pop(0)
        lines.append(fit(cur, width))
    return lines


def short_model(name: str) -> str:
    """A model name that fits the 6-cell model column: sonnet/opus/haiku/jev/mcp, else fitted."""
    n = (name or "").lower()
    for key in ("sonnet", "opus", "haiku", "jev", "mcp"):
        if key in n:
            return key
    return fit(name or "-", 6)
