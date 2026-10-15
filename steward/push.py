"""Push a job's outcome to the Claude session that created it.

ONE grammar for every pane line (and, via `phone_text`, for Moshi / ChatGPT / Android notices):

    [steward] <glyph> <short name> (<id8>) — <what happened, plain words> · <what to do, or nothing>

The name is `tabnames.job_name` (never the raw goal), no CLI hints. Glyphs: ✓ done, ◆ needs you, ✗ failed/stopped,
↻ in progress. ONE line per outcome: accept / merge / deploy events do not push on their own, they are *held*
(facts accumulate in kv `notice`) and the terminal event (or `settle`, called when autopilot finishes) emits a single
coalesced line. A merge that needs a fix-up stays silent unless it is still unmerged after TROUBLE_AFTER_S
(`flush_due`). Only done / needs-you / failed-stopped / long merge trouble reach the pane; everything else stays in
the feed. Dedupe key per job+outcome; two lines of the same glyph for one job within COALESCE_S collapse.

`steward new` records `created_by` {session, herdr_pane, cwd}. The control pane types the line into the creator's
Herdr pane so it arrives as a user turn. Delivery happens ONLY when the pane still exists and still hosts the same
Claude session. Best-effort: never raises into the caller. Opt-out: kv `notify_creator` = false.
"""

from __future__ import annotations

import json
import time

from steward import tabnames
from steward.diagnosis import redact
from steward.models import Judgment
from steward.runner_exec import Runner
from steward.store import Store

EVENTS = ("accepted", "incorporated", "stopped", "auto_merged", "auto_followup", "auto_escalated", "restart_needed", "auto_deployed", "no_change", "needs_you")
DONE, NEEDS, FAILED, PROGRESS = "✓", "◆", "✗", "↻"
COALESCE_S = 90.0
TROUBLE_AFTER_S = 600.0
_MAX = 240
_KV = "notice"
_TIMEOUT = 8.0


def display_name(name: str | None, job_id: str) -> str:
    """`<label> (<id8>)`, or just `<id8>` with no label: two jobs sharing a tab label stay distinguishable."""
    id8 = job_id[:8]
    return f"{name} ({id8})" if name and name.strip() else id8


def line(name: str | None, glyph: str, what: str, action: str = "nothing", *, job_id: str) -> str:
    """The one grammar. Redacted, single-spaced, capped; the cap shortens `what`, never the name or id."""
    head, tail = f"[steward] {glyph} {display_name(name, job_id)} — ", f" · {action or 'nothing'}"
    what = " ".join(redact(what).split())
    room = _MAX - len(head) - len(tail)
    if len(what) > room:
        what = what[:max(room - 1, 0)].rstrip() + "…"
    return " ".join(redact(head + what + tail).split())


def phone_text(name: str | None, glyph: str, what: str, action: str = "nothing", *, job_id: str,
               title_cap: int | None = None) -> tuple[str, str]:
    """`(title, body)` for Moshi / ChatGPT / Android notices in the same grammar, without the `[steward]` tag.
    With `title_cap`, a long label is shortened so the id8 stays whole."""
    label = " ".join((name or "").split())
    title = f"{glyph} {display_name(label, job_id)}"
    if title_cap and len(title) > title_cap and label:
        over = len(title) - title_cap
        title = f"{glyph} {display_name(label[:max(len(label) - over - 1, 1)].rstrip() + '…', job_id)}"
    return title, " ".join(redact(f"{what} · {action or 'nothing'}").split())


_PHONE_GLYPH = {"RUNNING": PROGRESS, "RECOVERY": PROGRESS, "NEEDS_YOU": NEEDS, "BUDGET": NEEDS, "CHECKS_FAILED": FAILED,
                "READY": DONE}
_PHONE_ACTION = {"NEEDS_YOU": "tap to answer", "BUDGET": "raise the budget or stop it", "CHECKS_FAILED": "look at the checks"}


def phone_event(store: Store, job_id: str, kind: str, body: str) -> tuple[str, str]:
    """Moshi / ChatGPT / Android notice text in the pane grammar: title `<glyph> <short name>` (<= 40),
    body `<what happened> · <what to do, or nothing>` (<= 140). Never the raw goal or a CLI hint."""
    job = store.get_job(job_id)
    name = tabnames.job_name(store, job) if job else None
    title, text = phone_text(name, _PHONE_GLYPH.get(kind, PROGRESS), body.strip().rstrip(".") or kind.lower(),
                             _PHONE_ACTION.get(kind, "nothing"), job_id=job_id, title_cap=40)
    return title, text if len(text) <= 140 else text[:139].rstrip() + "…"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _state(store: Store, job_id: str) -> dict:
    st = store.kv_get(job_id, _KV, default=None)
    return st if isinstance(st, dict) else {"facts": {}, "held": [], "held_t": None, "trouble": None, "sent": [], "last": None}


def _save(store: Store, job_id: str, st: dict) -> None:
    store.kv_set(job_id, _KV, st)


def _compose(name: str, kind: str, facts: dict) -> tuple[str, str, str]:
    """`(glyph, what, action)` for a terminal outcome of `kind` in done | needs | failed."""
    if kind == "failed":
        return FAILED, facts.get("reason") or "stopped", "nothing"
    if kind == "needs":
        return NEEDS, f"needs you: {facts.get('reason') or 'a decision'}", facts.get("action") or "tap [ fix merge ] / answer"
    if facts.get("no_change"):
        return DONE, f"no change needed: {facts.get('reason') or 'nothing to change'}", "nothing"
    parts = []
    if facts.get("merged"):
        n = facts.get("files")
        parts.append(f"merged {_plural(n, 'file')}" if n else "merged")
    else:
        n = facts.get("files")
        parts.append(f"accepted, {_plural(n, 'file')} changed" if n else "accepted")
    if facts.get("tests"):
        parts.append(f"{int(facts['tests']):,} tests pass")
    if facts.get("deployed"):
        parts.append("Steward reloaded")
    return DONE, "done: " + ", ".join(parts), "nothing"


def _record(store: Store, job_id: str, what: str, evidence_: dict) -> None:
    store.append_event(job_id, what=what, evidence=evidence_, rule="creator_push", judgment=Judgment.code)


def _find_pane(store: Store, pane_id: str, runner_exec: Runner) -> tuple[dict | None, str | None]:
    """`(pane, error)`; pane None + error None means the pane is gone."""
    ws = pane_id.split(":", 1)[0]
    res = runner_exec.run(["herdr", "pane", "list", "--workspace", ws], cwd=store.home, timeout=_TIMEOUT)
    if res.exit_code != 0:
        return None, f"herdr pane list failed: {(res.stderr or res.stdout).strip()[:120]}"
    try:
        panes = json.loads(res.stdout)["result"]["panes"]
    except (ValueError, KeyError, TypeError):
        return None, "herdr pane list: unparseable output"
    return next((p for p in panes if p.get("pane_id") == pane_id), None), None


def _deliver(store: Store, job_id: str, event: str, runner_exec: Runner, msg: str) -> str:
    """Type `msg` into the creator's pane: delivered | skipped:<reason> | failed."""
    from steward import deadline

    if deadline.notices_local(store, job_id):  # DL-17: a deadline job's creator notices stay local
        _record(store, job_id, "creator_notify_local", {"event": event, "reason": "deadline_job", "line": msg[:200]})
        return "skipped:deadline_local"
    if store.kv_get(job_id, "notify_creator", default=True) is False:
        _record(store, job_id, "creator_notify_skipped", {"event": event, "reason": "opted_out"})
        return "skipped:opted_out"
    created = store.kv_get(job_id, "created_by") or {}
    pane_id, session = created.get("herdr_pane"), created.get("session")
    if not pane_id or not session:
        _record(store, job_id, "creator_notify_skipped", {"event": event, "reason": "no_creator"})
        return "skipped:no_creator"
    pane, err = _find_pane(store, pane_id, runner_exec)
    if err:
        _record(store, job_id, "creator_notify_failed", {"event": event, "pane": pane_id, "error": err})
        return "failed"
    if pane is None:
        _record(store, job_id, "creator_notify_skipped", {"event": event, "reason": "pane_gone"})
        return "skipped:pane_gone"
    if (pane.get("agent_session") or {}).get("value") != session:
        _record(store, job_id, "creator_notify_skipped", {"event": event, "reason": "different_session"})
        return "skipped:different_session"
    for argv in (["herdr", "pane", "send-text", pane_id, msg], ["herdr", "pane", "send-keys", pane_id, "Enter"]):
        res = runner_exec.run(argv, cwd=store.home, timeout=_TIMEOUT)
        if res.exit_code != 0:
            _record(store, job_id, "creator_notify_failed",
                    {"event": event, "pane": pane_id, "error": redact((res.stderr or res.stdout).strip())[:120]})
            return "failed"
    _record(store, job_id, "creator_notified", {"event": event, "pane": pane_id})
    return "delivered"


def _file_count(store: Store, job_id: str) -> int | None:
    from steward import followup
    job = store.get_job(job_id)
    try:
        ns = followup.changed_files(store, job) if job else None
    except Exception:  # noqa: BLE001
        ns = None
    return len(ns[2]) if ns is not None else None


def notify_creator(store: Store, job_id: str, event: str, runner_exec: Runner, facts: dict | None = None,
                   clock=time.time) -> str:
    """Returns the outcome: delivered | held | skipped:<reason> | failed | duplicate. Never raises."""
    try:
        return _notify(store, job_id, event, runner_exec, facts or {}, clock)
    except Exception as exc:  # noqa: BLE001 — best-effort, never break the caller
        try:
            _record(store, job_id, "creator_notify_failed", {"event": event, "error": redact(str(exc))[:200]})
        except Exception:  # noqa: BLE001
            pass
        return "failed"


def _notify(store: Store, job_id: str, event: str, runner_exec: Runner, facts: dict, clock) -> str:
    job = store.get_job(job_id)
    if event not in EVENTS or job is None:
        return "skipped:bad_event"
    now = clock()
    st = _state(store, job_id)
    # needs_you's ask text is this line's only: never persisted into the job's facts (a later stopped/auto_escalated line)
    persist = {k: v for k, v in facts.items() if v is not None and not (event == "needs_you" and k in ("reason", "action"))}
    st["facts"].update(persist)
    f = st["facts"]
    if event in ("accepted", "auto_merged", "auto_followup", "incorporated") and f.get("files") is None:
        f["files"] = _file_count(store, job_id)
    if event == "accepted":
        f.setdefault("cost", None)
    if event in ("accepted", "auto_merged", "auto_followup"):
        if event == "auto_merged":
            f["merged"] = True
        if event == "auto_followup":
            prev = st.get("trouble") or {}
            st["trouble"] = {"t": prev.get("t", now), "pushed": prev.get("pushed", False),
                             "detail": f.get("detail") or "merge blocked", "n": f.get("attempt"), "max": f.get("max"),
                             "conflict": f.get("conflict_files") or []}
        st["held"] = [*st["held"], event]
        st["held_t"] = st["held_t"] or now
        _save(store, job_id, st)
        _record(store, job_id, "creator_notify_held", {"event": event})
        return "held"
    if event in ("incorporated", "auto_deployed"):
        f["merged"] = True
        if event == "auto_deployed":
            f["deployed"] = True
        return _terminal(store, job_id, event, runner_exec, st, "done", now)
    if event == "no_change":
        f["no_change"] = True
        return _terminal(store, job_id, event, runner_exec, st, "done", now)
    if event == "restart_needed":
        f["merged"] = True
        f["reason"] = f.get("reason") or "merged, but Steward was not reloaded"
        f["action"] = "restart Steward"
        return _terminal(store, job_id, event, runner_exec, st, "needs", now)
    if event == "needs_you":  # the coordinator's ask (R2)
        extra = {"reason": facts.get("reason") or "a decision", "action": facts.get("action") or "your call; see the dash"}
        return _terminal(store, job_id, event, runner_exec, st, "needs", now, extra=extra)
    if event == "auto_escalated":
        f["reason"] = f.get("reason") or "auto-merge could not finish"
        return _terminal(store, job_id, event, runner_exec, st, "needs", now)
    f["reason"] = f.get("reason") or "stopped"  # stopped
    return _terminal(store, job_id, event, runner_exec, st, "failed", now)


def _terminal(store: Store, job_id: str, event: str, runner_exec: Runner, st: dict, kind: str, now: float,
              extra: dict | None = None) -> str:
    f = {**st["facts"], **(extra or {})}
    key = f"{kind}:{f.get('reason', '') if kind != 'done' else ''}"
    glyph, what, action = _compose(tabnames.job_name(store, store.get_job(job_id)), kind, f)
    last = st.get("last") or {}
    st["held"], st["held_t"], st["trouble"] = [], None, None
    if key in st["sent"] or (last.get("glyph") == glyph and now - float(last.get("t", 0)) < COALESCE_S):
        _save(store, job_id, st)
        _record(store, job_id, "creator_notify_skipped", {"event": event, "reason": "duplicate"})
        return "duplicate"
    msg = line(tabnames.job_name(store, store.get_job(job_id)), glyph, what, action, job_id=job_id)
    out = _deliver(store, job_id, event, runner_exec, msg)
    if out == "delivered":
        st["sent"] = [*st["sent"], key]
        st["last"] = {"glyph": glyph, "t": now}
    _save(store, job_id, st)
    return out


def settle(store: Store, job_id: str, runner_exec: Runner, outcome: str | None = None, detail: str = "",
           clock=time.time) -> str:
    """Autopilot finished with the job: emit the ONE held line. A merge that is still being fixed up stays silent."""
    try:
        st = _state(store, job_id)
        if not st["held"] or st.get("trouble") and outcome in (None, "followup", "pending"):
            return "held" if st["held"] else "nothing"
        now = clock()
        if outcome in ("human", "error", "escalated"):
            st["facts"]["reason"] = st["facts"].get("reason") or (
                f"merge needs a decision: {detail}" if outcome == "human" else f"auto-merge failed: {detail}")
            st["facts"]["action"] = "merge it yourself or tell me how"
            return _terminal(store, job_id, "auto_escalated", runner_exec, st, "needs", now)
        return _terminal(store, job_id, "accepted", runner_exec, st, "done", now)
    except Exception as exc:  # noqa: BLE001
        _record(store, job_id, "creator_notify_failed", {"event": "settle", "error": redact(str(exc))[:200]})
        return "failed"


def flush_due(store: Store, job_id: str, runner_exec: Runner, clock=time.time) -> str:
    """Time-driven part: a held accept older than COALESCE_S settles as done; a merge still unmerged after
    TROUBLE_AFTER_S gets the one ↻ line. Called from the control pane's periodic sweep."""
    try:
        st = _state(store, job_id)
        now = clock()
        tr = st.get("trouble")
        if tr:
            if tr.get("pushed") or now - float(tr["t"]) < TROUBLE_AFTER_S:
                return "nothing"
            where = f"conflict in {', '.join(tr['conflict'][:2])}" if tr.get("conflict") else str(tr["detail"])[:60]
            fix = f", fix-up {tr['n']}/{tr['max']}" if tr.get("n") and tr.get("max") else ""
            msg = line(tabnames.job_name(store, store.get_job(job_id)), PROGRESS, f"merging ({where}{fix})", job_id=job_id)
            out = _deliver(store, job_id, "merge_trouble", runner_exec, msg)
            tr["pushed"] = True
            _save(store, job_id, st)
            return out
        if st["held"] and st["held_t"] is not None and now - float(st["held_t"]) >= COALESCE_S:
            return settle(store, job_id, runner_exec, clock=clock)
        return "nothing"
    except Exception as exc:  # noqa: BLE001
        _record(store, job_id, "creator_notify_failed", {"event": "flush_due", "error": redact(str(exc))[:200]})
        return "failed"


def flush_all_due(store: Store, runner_exec: Runner, clock=time.time) -> int:
    n = 0
    for job in store.list_jobs():
        st = store.kv_get(job.job_id, _KV, default=None)
        if isinstance(st, dict) and (st.get("held") or st.get("trouble")):
            n += flush_due(store, job.job_id, runner_exec, clock) == "delivered"
    return n
