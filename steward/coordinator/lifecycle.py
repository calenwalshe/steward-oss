"""Episode bookkeeping shared by the daemon's main tick and its worker step (spec 5.16 steps 4-6, 6.3).

Everything here is a pure helper over the coordinator's own DB and read-only Steward reads, plus the one close path:
rule proposal (``rules.file``) -> ``cdb.close`` -> relay ``coordinator episode`` -> the hold's ``delivery``/``how``
into the evidence -> ``apply_precision``.
"""
from __future__ import annotations

import json
import logging

from .. import coordinator_link
from ..models import Judgment
from . import rules, rungs, verbs

log = logging.getLogger("steward.coordinator.daemon")

WEEK_S = 7 * 86400.0
ASK_MAX = 160
ACTIONS_KEPT = 8
MODEL_RUNG = "model"
KNOWN_RUNGS = (*rungs.RUNG_ORDER, rungs.REAL_RUNG, MODEL_RUNG)
ENABLED_AT = "rung_enabled_at:"   # cursor name prefix: when a rung was re-enabled by hand
BUDGET_TEXT = "coordinator model budget exhausted"


# ---------- episode row helpers ----------

def is_jobless(key: str | None) -> bool:
    return not key or str(key).startswith("health:")


def job_of(ep: dict) -> str | None:
    key = ep.get("job_id")
    return None if is_jobless(key) else key


def _loads(raw, default):
    try:
        val = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return val if isinstance(val, type(default)) else default


def ev_of(ep: dict) -> dict:
    return _loads(ep.get("evidence_json"), {})


def rungs_of(ep: dict) -> list:
    return _loads(ep.get("rungs_json"), [])


def actions_of(ep: dict) -> list:
    return _loads(ep.get("actions_json"), [])


def merge_ev(cdb, ep: dict, **fields) -> dict:
    """Merge `fields` into the episode's evidence (never overwrite: the ask-only flag lives there too)."""
    ev = cdb.merge_evidence(ep["episode_id"], fields)   # atomic: never drops another thread's keys
    ep["evidence_json"] = json.dumps(ev)
    return ev


def append_rung(cdb, ep: dict, rung: str, classification) -> None:
    items = rungs_of(ep) + [{"rung": rung, "classification": classification}]
    raw = json.dumps(items)
    cdb.mark(ep["episode_id"], rungs_json=raw)
    ep["rungs_json"] = raw


def add_actions(cdb, ep: dict, labels: list[str]) -> None:
    items = (actions_of(ep) + list(labels))[-ACTIONS_KEPT:]
    raw = json.dumps(items)
    cdb.mark(ep["episode_id"], actions_json=raw)
    ep["actions_json"] = raw


def label(verb, job: str | None) -> str:
    """`<verb> <job8> …` for the relay and the digest: `approve 1a2b3c4d ap-3`, `nudge 1a2b3c4d`."""
    argv = list(verb.argv)
    parts = [verb.label, (job or "")[:8]] if job else [verb.label]
    if argv[:1] in (["approve"], ["deny"]) and len(argv) > 2:
        parts.append(argv[2])
    elif argv[:1] == ["up"] and len(argv) > 2:
        parts.append(argv[2])
    return " ".join(p for p in parts if p)


def trigger_of(ep: dict):
    """The episode's current trigger (the last attached one, else the opening one) as a `triggers.Trigger`."""
    from .triggers import Trigger

    ev = ev_of(ep)
    last = ev.get("last_trigger") if isinstance(ev.get("last_trigger"), dict) else {}
    kind = last.get("kind") or ep.get("trigger") or ""
    seq = last.get("seq") if last.get("seq") is not None else ep.get("trigger_seq") or 0
    detail = last.get("detail") if isinstance(last.get("detail"), dict) else ev.get("trigger_detail") or {}
    return Trigger(kind, job_of(ep), int(seq or 0), detail if isinstance(detail, dict) else {})


# ---------- R17 preemption ----------

def preempted(ro, ep: dict) -> bool:
    """An owner event on the job after the trigger: judgment `human`, no `actor` in evidence, not our own echo."""
    job = job_of(ep)
    if job is None:
        return False
    floor = int(ep.get("trigger_seq") or 0) or int(ev_of(ep).get("open_seq") or 0)
    for e in reversed(ro.events(job)):
        if e.seq <= floor:
            break
        if e.judgment is not Judgment.human or e.rule == "coordinator":
            continue
        if not (isinstance(e.evidence, dict) and "actor" in e.evidence):
            return True
    return False


# ---------- re-park after acting (6.3) ----------

def newest_park_seq(ro, job: str) -> int:
    from ..ownership import PARK_RULES, PARK_WHATS, park_event

    try:
        park = park_event(ro.events_of(job, sorted(PARK_WHATS), sorted(PARK_RULES)))
    except Exception:  # noqa: BLE001 - unknown is "no newer park"
        return 0
    return park.seq if park is not None else 0


# ---------- the ask / diagnosis (6.3) ----------

def _clean(text) -> str:
    return " ".join(str(text or "").split()).lstrip("-").strip()


def diagnosis(ro, ep: dict, found: str) -> str:
    """`<classification or park reason>; coordinator: <what it found>; tried <rungs>`."""
    ev = ev_of(ep)
    reason = ev.get("classification")
    job = job_of(ep)
    if not reason and job is not None:
        try:
            reason = coordinator_link._park_reason(ro, job)
        except Exception:  # noqa: BLE001
            reason = None
    reason = _clean(reason or ep.get("trigger") or "stuck")
    tried = [str(r.get("rung")) for r in rungs_of(ep) if isinstance(r, dict) and r.get("rung")]
    return f"{reason}; coordinator: {_clean(found) or 'nothing it can do'}; tried {', '.join(tried) or 'no rung'}"


# ---------- precision (D13, §8.8) ----------

def apply_precision(cdb, now: float) -> list[str]:
    """Disable every rung overturned twice in 7 days (overturns before a hand re-enable no longer count)."""
    disabled = []
    for rung in KNOWN_RUNGS:
        try:
            if cdb.rung_disabled(rung):
                continue
            window = WEEK_S
            enabled_at = cdb.cursor_get(ENABLED_AT + rung, 0) or 0
            if enabled_at:
                window = max(0.0, min(WEEK_S, now - float(enabled_at)))
            if window and cdb.overturns_in(window, now, rung=rung) >= 2:
                cdb.disable_rung(rung, "overturned twice in 7 d", now)
                disabled.append(rung)
                log.warning("rung %s disabled: overturned twice in 7 d", rung,
                            extra={"episode": "-", "job8": "-"})
        except Exception:  # noqa: BLE001 - precision is bookkeeping, never fatal
            log.exception("precision check for %s failed", rung, extra={"episode": "-", "job8": "-"})
    return disabled


def enable_rung(cdb, rung: str, now: float) -> None:
    cdb.enable_rung(rung)
    cdb.cursor_set(ENABLED_AT + rung, now)


# ---------- close (spec 5.16 step 6) ----------

def _still_open(cdb, ep: dict) -> bool:
    cur = cdb.open_for(ep.get("job_id"))
    return cur is not None and cur.get("episode_id") == ep["episode_id"]


def close(cdb, ro, ep: dict, outcome: str, found: str, *, now: float, settings, workspace, run,
          ask: bool | None = None, **ev_fields) -> dict | None:
    """Close the episode: rule proposal before the relay (resolved and novel or model-used), the row, the relayed
    `coordinator episode` (job episodes only, R18) and the hold's delivery copied into the evidence, then the
    precision check. Returns `{eid, outcome}`, or None when
    the episode was already closed (by the other thread): then no proposal, no relay, no precision check."""
    eid = ep["episode_id"]
    job = job_of(ep)
    if not _still_open(cdb, ep):
        log.info("close %s skipped: already closed", outcome, extra=_extra(eid, job))
        return None
    if ev_fields:
        merge_ev(cdb, ep, **ev_fields)
    ev = ev_of(ep)
    diag = diagnosis(ro, ep, found)
    wants_ask = outcome in ("asked", "gave_up") if ask is None else ask
    ask_text = diag[:ASK_MAX] if wants_ask and job is not None else None
    model_calls = int(ep.get("model_calls") or 0)
    actions = actions_of(ep)
    rule_id = None
    if outcome == "resolved":
        rule_text = ev.get("rule") or ev.get("model_rule")
        last = next((r for r in reversed(rungs_of(ep)) if isinstance(r, dict)), None)
        try:
            is_novel = last is not None and rules.novel(cdb, last.get("rung"), last.get("classification"), now)
        except Exception:  # noqa: BLE001
            is_novel = False
        if model_calls > 0 or is_novel:
            rung = str((last or {}).get("rung") or "unknown")
            if not rule_text:  # every new kind of unblock gets a proposal (spec §1, step 6)
                rule_text = f"{rung}: Steward should settle this itself — {diag[:120]} (coordinator episode {eid[:8]})"
            rule_id = rules.file({**ep, "classification": ev.get("classification"), "rung": rung, "diagnosis": diag,
                                  "where": ev.get("where")}, str(rule_text), run=run)
            log.info("rule proposal %s", rule_id or "not filed", extra=_extra(eid, job))
    if not cdb.close(eid, outcome, diag, actions, now, rule_proposal_id=rule_id, ask=ask_text):
        log.info("close %s lost the race: already closed", outcome, extra=_extra(eid, job))
        return None
    log.info("closed %s: %s", outcome, diag, extra=_extra(eid, job))
    if job is not None:
        res = verbs.run(verbs.episode(job, eid, trigger_of(ep).kind or ep.get("trigger") or "", outcome,
                                      diag[:300], actions, rule_task=rule_id, model_used=model_calls > 0,
                                      ask=ask_text),
                        settings=settings, workspace=workspace, run=run)
        if not res["ok"]:
            log.warning("episode relay failed rc=%s: %s", res["rc"], res["err"][:200], extra=_extra(eid, job))
        _merge_delivery(cdb, ro, ep, job)
    apply_precision(cdb, now)
    return {"eid": eid, "outcome": outcome}


def _merge_delivery(cdb, ro, ep: dict, job: str) -> None:
    """After the `coordinator episode` relay (the handler writes the ask outcome onto the hold during it), copy the
    hold's `delivery` and `how` for THIS episode into the episode evidence (the digest's "ask delivery" column)."""
    try:
        rec = ro.kv_get(job, coordinator_link.HOLD_KEY)
        if isinstance(rec, dict) and rec.get("episode_id") == ep["episode_id"]:
            merge_ev(cdb, ep, delivery=rec.get("delivery"), how=rec.get("how"))
    except Exception:  # noqa: BLE001 - bookkeeping only
        log.exception("delivery merge failed", extra=_extra(ep.get("episode_id"), job))


def _extra(eid, job) -> dict:
    return {"episode": str(eid or "-")[:12], "job8": str(job or "-")[:8]}
