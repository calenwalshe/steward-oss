"""Episode dedupe: one open episode per key, a cooldown after a close, a daily cap, reopen detection.

``open_or_attach`` returns ``(episode_id | None, how)`` with ``how`` one of:
``opened``, ``attached``, ``cooldown`` (episode id is None), ``ask_only`` (opened, but flagged ask-only) or
``rung_disabled_all`` (reserved for the rung ladder; this module never returns it).
"""
from __future__ import annotations

import json

_DAY = 86400.0


def episode_key(trig) -> str:
    """The dedupe key: the job id, or ``health:<component>`` for job-less triggers."""
    if trig.job_id:
        return trig.job_id
    kind = trig.kind
    return kind if kind.startswith("health:") else f"health:{kind}"


def is_ask_only(episode: dict | None) -> bool:
    """True when the episode was opened over the daily cap and may only ask the owner."""
    if not episode:
        return False
    try:
        ev = json.loads(episode.get("evidence_json") or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(ev, dict) and bool(ev.get("ask_only"))


def asks_in_cooldown(kind: str) -> bool:
    """A park (``needs_human:*``) or a liveness ask during the cooldown still owes the owner one line."""
    kind = str(kind or "")
    return kind.startswith("needs_human:") or kind == "liveness_ask"


def ask_only_why(episode: dict | None) -> str:
    """``cooldown`` or ``cap`` for an ask-only episode, else ''."""
    if not is_ask_only(episode):
        return ""
    try:
        ev = json.loads(episode.get("evidence_json") or "{}")
    except (TypeError, ValueError):
        ev = {}
    return str(ev.get("ask_only_why") or "cap") if isinstance(ev, dict) else "cap"


def open_or_attach(cdb, trig, *, now: float, settings) -> tuple[str | None, str]:
    """Attach to the key's open episode, else open one.

    The cooldown and reopen checks look at the last FULL episode (ask-only ones never acted). During the cooldown a
    ``needs_human:*`` or ``liveness_ask`` trigger opens an ask-only episode (so the park still reaches the owner as one
    line, via the hold + ``asked`` relay); any other kind (orphan, health) returns ``cooldown``.

    The daily cap allows ``episodes_per_job_day`` (3) FULL episodes per job in 24 h; the 4th and later open ask-only
    (spec §5.10 counts >= 3 before opening and §8.6 says "3 episodes per job per day"; D4's "3rd" is the inconsistent
    wording).
    """
    key = episode_key(trig)
    open_ep = cdb.open_for(key)
    if open_ep is not None:
        cdb.attach(open_ep["episode_id"], trig.seq)
        return open_ep["episode_id"], "attached"
    last = cdb.last_closed(key, full_only=True)
    closed_at = last.get("closed_at") if last is not None else None
    cooling = closed_at is not None and now - closed_at < settings.cooldown_s
    if cooling and not asks_in_cooldown(trig.kind):
        return None, "cooldown"
    reopened = bool(closed_at is not None and now - closed_at < _DAY
                    and (last.get("outcome") == "asked" or last.get("overturned_at") is not None))
    eid = cdb.open_episode(key, trig.kind, trig.seq, now, reopened=reopened and not cooling)
    if cooling:
        cdb.mark(eid, evidence_json=json.dumps({"ask_only": True, "ask_only_why": "cooldown"}))
        return eid, "ask_only"
    if cdb.episodes_for_job(key, now - _DAY, full_only=True) > settings.episodes_per_job_day:
        cdb.mark(eid, evidence_json=json.dumps({"ask_only": True, "ask_only_why": "cap"}))
        return eid, "ask_only"
    return eid, "opened"
