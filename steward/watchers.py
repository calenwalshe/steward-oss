"""`steward watchers <JOB>` — a read-only, human-readable log of everything the
supervisors (Astra, Sol, Jev) and the steering path did for one job.

Pure functions: `format_event(event, state)` turns one store event into an
optional `(role, text)` pair (None = noise, not shown); `render_line` adds the
local time, padded role tag, terminal-width truncation and optional ANSI
colour; `collect` / `follow` drive them over a store. Nothing here writes to
the store or touches Herdr.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Callable, TextIO

from steward import masking
from steward.machine import is_terminal
from steward.roles import role_label

ROLE_WIDTH = 8
_COLORS = {"watch": "34", "advisor": "35", "doctor": "33", "observer": "36", "astra": "35", "sol": "33", "jev": "36", "steer": "32", "job": "1", "sys": "2", "resolver": "32", "efficiency": "94", "question": "96"}
_TEXT_CAP = 240


def _one_line(text: object, cap: int = _TEXT_CAP) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= cap else s[: cap - 1] + "…"


def _short(review_id: object) -> str:
    return str(review_id or "")[:4]


def _origin(raw: str) -> str:
    return {"steward": "diagnosis", "cli": "cli", "human": "human"}.get(raw, raw)


def format_event(ev, state: dict | None = None) -> tuple[str, str] | None:
    """`(role, text)` for a supervisor-relevant event, else None. `state` is a
    mutable dict carrying the little memory dedupe needs (Jev's last state);
    feed events in seq order with the same dict."""
    st = state if state is not None else {}
    what, e = ev.what, ev.evidence or {}

    if what == "heartbeat class changed":
        if e.get("to") == "suspect_stall":
            return "sol", "suspect stall → diagnosing"
        if e.get("from") == "suspect_stall":
            return "sol", f"stall cleared → {e.get('to')}"
        return None
    if what == "stall diagnosis produced":
        return "sol", f"diagnosis {e.get('verdict')}: {_one_line(e.get('hypothesis', ''))}"
    if what == "retry guidance authorized by diagnosis":
        return "sol", f"retry guidance: {_one_line(e.get('guidance', ''))}"

    if what == "trajectory_review_requested":
        return "astra", f"review {_short(e.get('review_id'))} requested ({', '.join(e.get('triggers') or [])})"
    if what == "trajectory_advice_received":
        rid = _short(e.get("review_id"))
        v = e.get("verdict") or {}
        if e.get("status") != "ok" or not v:
            return "astra", f"review {rid} no usable verdict ({e.get('status')})"
        conf = v.get("confidence")
        conf_s = f" ({float(conf):.2f})" if isinstance(conf, (int, float)) else ""
        return "astra", f"review {rid} → {v.get('trajectory')}{conf_s}: {_one_line(v.get('observation', ''))}"
    if what == "trajectory_redirect_authorized":
        return "astra", f"review {_short(e.get('review_id'))} redirect authorized ({e.get('verb')}) → rev {e.get('rev')}"
    if what == "trajectory_advice_discarded":
        return "astra", f"review {_short(e.get('review_id'))} advice discarded ({e.get('reason')})"
    if what == "trajectory_escalated":
        return "astra", f"review {_short(e.get('review_id'))} escalated to human ({e.get('rule')}): {_one_line(e.get('detail') or '')}"
    if what == "trajectory_review_timed_out":
        return "astra", f"review {_short(e.get('review_id'))} timed out"
    if what == "trajectory_mode_changed":
        return "astra", f"mode → {e.get('mode')} ({e.get('scope', 'job')})"

    if what == "watcher_verdict":
        sup = e.get("suppressed") or []
        tail = f" (suppressed: {', '.join(sup)})" if sup else ""
        return "watch", f"watcher {e.get('name')} {e.get('status')}: {_one_line(e.get('note', ''))}{tail}"
    if what == "watcher_error":
        return "watch", f"watcher {e.get('name')} failed: {_one_line(e.get('detail', ''))}"
    if what == "watcher_timed_out":
        return "watch", f"watcher {e.get('name')} timed out"
    if what == "watcher_nudged":
        return "watch", f"watcher {e.get('name')} → nudge rev {e.get('rev')}"
    if what == "watcher_escalated":
        return "watch", f"watcher {e.get('name')} → escalated"
    if what == "watcher_added":
        return "watch", (f"watcher {e.get('name')} added ({e.get('model')}, every {int(e.get('every_s') or 0)}s, "
                         f"may {','.join(e.get('may') or []) or 'none'})")
    if what == "watcher_removed":
        return "watch", f"watcher {e.get('name')} removed"
    if what == "watcher_toggled":
        return "watch", f"watcher {e.get('name')} switched {'on' if e.get('previous') else 'off'} → {'on' if e.get('enabled') else 'off'}"

    if what == "resolver_decision":
        applied = {"escalate_human": "escalate"}.get(e.get("applied"), e.get("applied"))
        return "resolver", f"resolver {e.get('model')}: {applied} — {_one_line(e.get('reason', ''), 200)}"
    if what == "resolver_error":
        return "resolver", f"resolver failed: {_one_line(e.get('detail', ''))}"

    if what == "finish_line_not_met":
        return "job", (f"FINISH LINE NOT MET ({e.get('bounce')}/{e.get('max')}): "
                       f"{_one_line(e.get('first_failing_line', ''), 140)} -> nudged")
    if what == "finish_line_met":
        return "job", f"finish line met ({_one_line(e.get('command', ''), 80)})"
    if what == "finish_check_unrunnable":
        return "job", f"finish check could not run ({_one_line(e.get('command', ''), 60)}); prose finish line only"
    if what == "yield_classified":
        return "job", f"worker turn ended: {e.get('lane')} {', '.join(e.get('failed_preconditions') or [])}".rstrip()
    if what == "check_broken":
        return "job", f"TESTS UNVERIFIED: {e.get('phase')} check `{_one_line(e.get('command', ''), 50)}` broken: {e.get('reason')}"
    if what == "check_fallback":
        return "job", f"check `{_one_line(e.get('command', ''), 40)}` re-run as `{_one_line(e.get('used', ''), 40)}`"
    if what == "watcher_overspend":
        r = e.get("ratio")
        return "watch", (f"watcher {e.get('name')} costs {'%.1f' % r + 'x worker' if r else '$%.2f, worker $0' % e.get('usd', 0)}"
                         + (f" -> cadence halved to every {e.get('every_s')}s" if e.get("cadence_halved") else " (cadence already at the limit)"))
    if what == "watcher_cadence_halved":
        return "watch", f"watcher {e.get('name')} cadence {e.get('previous_every_s')}s -> {e.get('every_s')}s ({e.get('halvings')}/{e.get('max')})"
    if what == "watcher_cadence_restored":
        return "watch", f"watcher {e.get('name')} cadence restored to {e.get('every_s')}s"
    if what == "spend_rate_high":
        return "job", f"SPEND ${e.get('usd_per_hour')}/h over the ${e.get('limit')}/h limit -- needs you"
    if what == "steer_agent_action":
        verdict = "" if e.get("accepted") else f" REFUSED: {_one_line(e.get('detail', ''), 100)}"
        return "steer", f"steer-agent {e.get('op')}{' --urgent' if e.get('urgent') else ''}{verdict}"
    if what == "unblock_agent_action":
        verdict = "" if e.get("accepted") else f" REFUSED: {_one_line(e.get('detail', ''), 100)}"
        return "steer", f"unblock-agent {e.get('op')}{' --urgent' if e.get('urgent') else ''}{verdict}"
    if what == "unblock_report":
        return "steer", f"unblock-agent ({e.get('model')}): {_one_line(str(e.get('report', '')).replace(chr(10), ' / '), 260)}"
    if what == "runner_restarted":
        return "job", f"runner restarted in place ({e.get('pane')}); worker untouched"

    if what == "capability_granted":
        if e.get("capability") == "research":
            return "job", f"granted: research MCP (cap ${float(e.get('cap_usd') or 0):.2f})"
        return "job", f"granted: key {e.get('name')}"
    if what == "research_call":
        return "job", (f"research {e.get('tool')}: {_one_line(e.get('query', ''), 80)} "
                       f"(${float(e.get('spent_usd') or 0):.2f}/${float(e.get('cap_usd') or 0):.2f})")
    if what == "research_call_denied":
        return "job", f"research {e.get('tool')} denied: {_one_line(e.get('reason', ''))}"
    if what == "research_cap_tripped":
        return "job", f"research cap ${float(e.get('cap_usd') or 0):.2f} reached (${float(e.get('spent_usd') or 0):.2f}) — needs you"
    if what == "research_cap_raised":
        return "job", f"research cap raised to ${float(e.get('cap_usd') or 0):.2f}"

    if what == "sent_back":
        return "job", f"SENT BACK: {_one_line(e.get('instructions', ''), 160)}"

    if what == "incorporated":
        return "job", (f"incorporated {str(e.get('merge_commit') or '')[:7]} into {e.get('source_branch')} "
                       f"({e.get('commits')} commit(s), {e.get('files')} file(s))")
    if what == "incorporate_blocked":
        files = e.get("files") or []
        return "job", f"incorporate blocked: {_one_line(e.get('reason', ''))}" + (f" [{', '.join(files[:5])}]" if files else "")
    if what == "incorporate_noop":
        return "job", f"incorporate: nothing to merge (already in {e.get('source_branch')})"

    if what == "creator_notified":
        return "job", f"notified creator ({e.get('event')})"
    if what == "creator_notify_skipped":
        return "job", f"creator not notified ({e.get('event')}): {e.get('reason')}"
    if what == "creator_notify_failed":
        return "job", f"creator notify failed ({e.get('event')}): {_one_line(e.get('error', ''), 80)}"

    if what == "supervisor_toggled":
        role = e.get("role") if e.get("role") in ("astra", "sol", "jev") else "steer"
        return role, f"switched {e.get('previous')} → {e.get('state')}"

    if what == "jev_unavailable":
        code = e.get("code")
        if st.get("jev") == ("unavailable", code):
            return None
        st["jev"] = ("unavailable", code)
        return "jev", "observer off" if code == "disabled" else f"observer unavailable ({code})"
    if what == "jev_shadow":
        choice = e.get("choice")
        if st.get("jev") == ("shadow", choice):
            return None
        st["jev"] = ("shadow", choice)
        if choice in ("ok", "no_inspection"):
            return None  # ok verdicts are state, not news
        conf = e.get("confidence")
        return "jev", f"observes: {choice}" + (f" ({float(conf):.2f})" if isinstance(conf, (int, float)) else "")
    if what == "jev_flag":
        conf = e.get("confidence")
        c = f" ({float(conf):.2f})" if isinstance(conf, (int, float)) else ""
        if e.get("escalated_to"):
            return "jev", f"FLAG {e.get('choice')}{c} → {e.get('escalated_to')}"
        return "jev", f"flag {e.get('choice')}{c} (shadow; would → {e.get('would_escalate_to')})"
    if what == "jev_escalated":
        return "jev", f"escalated {e.get('choice')} → {_one_line('; '.join(e.get('details') or []), 160)}"

    if what.endswith(" instruction recorded"):
        origin = e.get("origin")
        if origin == "contract":
            return None
        verb = what.split(" ", 1)[0]
        if e.get("urgent"):
            return "steer", f"URGENT rev {e.get('rev')} [{_origin(str(origin))}]: {_one_line(e.get('text', ''))}"
        return "steer", f"{verb} rev {e.get('rev')} [{_origin(str(origin))}]: {_one_line(e.get('text', ''))}"
    if what == "worker_interrupted":
        return "steer", f"worker interrupted for rev {e.get('rev')}"
    if what == "instruction acknowledged (typed ack frame)":
        return "steer", f"rev {e.get('rev')} acknowledged"
    if what.startswith("efficiency_"):
        text = _efficiency_text(what, e)
        return ("efficiency", text) if text is not None else None
    if what.startswith(("question_", "questions_")):
        text = _question_text(what, e)
        return ("question", text) if text is not None else None
    return None


def _money(x: object) -> str:
    try:
        return f"${float(x):.2f}"
    except (TypeError, ValueError):
        return "$?"


def _efficiency_text(what: str, e: dict) -> str | None:
    """One line for a phase-16 `efficiency_*` event. Tolerant: a missing key prints as `?`, never raises."""
    g = e.get
    try:
        if what == "efficiency_finding":
            from steward import efficiency_watch

            return efficiency_watch.format_finding(e).removeprefix("⚠ ")
        if what == "efficiency_finding_cleared":
            return f"cleared {g('id')}"
        if what == "efficiency_error":
            return f"efficiency error: {_one_line(g('error', ''), 120)}"
        if what == "efficiency_slow":
            return f"compute took {g('cpu_s')}s CPU (over 1s)"
        if what == "efficiency_units_set":
            return f"units_total set to {g('units_total')} (was {g('previous')})"
        if what == "efficiency_would_nudge":
            blocked = ", ".join(str(x) for x in (g("blocked") or g("reasons") or []))
            return f"would nudge {g('id') or g('finding') or '?'}" + (f" (blocked: {blocked})" if blocked else "")
        if what == "efficiency_nudged":
            return f"nudged rev {g('rev')}: {_one_line(g('remedy_short') or g('id') or '', 100)}"
        if what == "efficiency_escalated":
            return f"escalated {g('id') or g('finding') or '?'} (approval {str(g('approval_id') or g('approval') or '?')[:8]})"
        if what == "efficiency_policy_changed":
            return f"policy {g('key')}: {g('before')} → {g('after')} ({g('actor')})"
        if what == "efficiency_refused":
            return f"refused: {_one_line(g('reason', ''), 140)}"
        if what == "efficiency_effect":
            return (f"effect {g('metric')}: {g('before')} → {g('after')} "
                    f"(saved {_money(g('saved_usd'))} / {g('saved_min')} min)")
        if what == "efficiency_review":
            return f"review: {g('observations')} observations, {_money(g('cost_usd') if g('cost_usd') is not None else g('cost'))}"
        if what == "efficiency_cap_tripped":
            if g("tripped") is False:
                return f"cap cleared: {_one_line(g('reason', ''), 120)} -- observe-only lifted"
            return f"cap tripped: {_one_line(g('reason', ''), 120)} -- observe-only"
        if what == "efficiency_overrides_changed":
            keys = g("keys")
            return "overrides changed: " + (", ".join(str(k) for k in keys) if isinstance(keys, (list, tuple, dict)) else str(keys))
    except Exception:  # noqa: BLE001 - a feed line never raises
        return f"{what} (unreadable evidence)"
    return None


def _anchor(qid: object) -> str:
    """`gsd_context:16-CONTEXT.md:G4` -> `G4`."""
    return str(qid or "?").rsplit(":", 1)[-1] or "?"


def _answer_text(snap: object) -> str:
    """One answer snapshot (`{option, label, other, note}`) as words."""
    if not isinstance(snap, dict):
        return "?"
    if snap.get("other") or snap.get("option") == "other":
        return f'other "{_one_line(snap.get("other") or "", 80)}"'
    return str(snap.get("option") or snap.get("label") or "?")


def _question_text(what: str, e: dict | None) -> str | None:
    """One line for a phase-17 `question_*` / `questions_*` event: who decided, from where, what it said before.
    Tolerant: a missing key prints as `?`, never raises."""
    e = e if isinstance(e, dict) else {}
    g = e.get
    try:
        a = _anchor(g("qid"))
        block = " · BLOCKS WORK" if g("blocking") is True else ""
        if what == "question_answered":
            opt = g("option")
            if opt == "other":
                ans = f'other "{_one_line(g("other") or "", 80)}"'
            else:
                ans = str(opt or "?")
            line = f"{a} answered: {ans} ({_origin(str(g('origin') or '?'))})"
            ch = g("changed")
            if isinstance(ch, dict):
                line += f" · was {_answer_text(ch.get('before'))}"
            if g("note"):
                line += f' · note: "{_one_line(g("note"), 60)}"'
            return line
        if what == "question_deferred":
            return f"{a} deferred ({_origin(str(g('origin') or '?'))})"
        if what == "question_stale":
            return f"{a} stale: the area changed after your answer"
        if what == "question_updated":
            return f"{a} updated: the area changed while it was open{block}"
        if what == "question_withdrawn":
            return f"{a} withdrawn: the marker disappeared" + (f" ({g('file')})" if g("file") else "")
        if what == "question_worker_decided":
            return f"{a} decided in the file by the worker, not by you"
        if what == "questions_found":
            qids = g("qids") if isinstance(g("qids"), list) else []
            line = f"found {len(qids)} question(s): " + (", ".join(_anchor(q) for q in qids[:12]) or "?")
            if len(qids) > 12:
                line += f" +{len(qids) - 12}"
            blocking = g("blocking") if isinstance(g("blocking"), list) else []
            if blocking:
                line += " · BLOCKS WORK: " + ", ".join(_anchor(q) for q in blocking[:8])
            return line + (f" in {g('file')}" if g("file") else "")
        if what == "questions_written":
            qids = g("qids") if isinstance(g("qids"), list) else []
            where = f"to {g('branch')} " if g("branch") else ""
            via = f" via {g('via')}" if g("via") and not g("commit") else ""
            commit = f"commit {str(g('commit'))[:7]}" if g("commit") else "?"
            by = f" ({_origin(str(g('by')))})" if g("by") else ""
            return f"wrote {len(qids)} answer(s) {where}{commit}{via}{by}".replace("  ", " ")
        if what == "questions_delivered":
            n = len(g("qids")) if isinstance(g("qids"), list) else "?"
            by = f" ({_origin(str(g('by')))})" if g("by") else ""
            if g("mode") == "send_back":
                return f"sent back with {n} answer(s) (rev {g('steering_rev', '?')}){by}"
            return f"delivered {n} answer(s) to the worker ({g('mode') or '?'}, rev {g('steering_rev', '?')}){by}"
        if what == "questions_delivery_skipped":
            return f"delivery skipped: {_one_line(g('reason') or '?', 120)}"
        if what == "questions_scan_error":
            return f"question scan error: {_one_line(g('error') or '?', 120)}"
    except Exception:  # noqa: BLE001 - a feed line never raises
        return f"{what} (unreadable evidence)"
    return None


def render_line(ev, role: str, text: str, *, width: int, color: bool) -> str:
    role = role_label(role)  # advisor/doctor/observer, not the internal keys
    color_key = role
    if len(role) > ROLE_WIDTH:
        role = role[:ROLE_WIDTH - 1] + "…"  # `efficiency` would push the text out of its column
    ts = datetime.fromisoformat(ev.ts).astimezone().strftime("%H:%M:%S")
    plain = f"{ts} {role:<{ROLE_WIDTH}} {text}"
    if width > 1 and len(plain) > width:
        plain = plain[: width - 1] + "…"
    if not color:
        return plain
    head = f"{ts} {role:<{ROLE_WIDTH}} "
    code = _COLORS.get(color_key, "0")
    if len(plain) <= len(head):
        return plain
    return f"{head[:len(ts) + 1]}\x1b[{code}m{role:<{ROLE_WIDTH}}\x1b[0m {plain[len(head):]}"


def collect(events, *, since: int = 0) -> list[tuple]:
    """`(event, role, text)` for every visible event with seq > since. The
    dedupe state is built from the whole history so a filtered view still
    shows the right Jev transitions."""
    state: dict = {}
    out = []
    for ev in events:
        item = format_event(ev, state)
        if item is not None and ev.seq > since:
            out.append((ev, item[0], item[1]))
    return out


def header_lines(store, job) -> list[str]:
    from steward import supervisors

    lines = [f"job {job.job_id}  {_one_line(job.goal, 60)}"]
    try:
        rows = supervisors.supervisor_rows(store, job.job_id)
        lines.append("  ".join(f"{r['role']} {r.get('switch') or r['status']}" for r in rows if r["role"] != "worker"))
    except Exception:  # noqa: BLE001 — a header must never stop the feed
        pass
    return lines


def run_feed(
    store,
    job_id: str,
    *,
    follow: bool = False,
    since: int = 0,
    last: int | None = None,
    out: TextIO,
    width_fn: Callable[[], int],
    color: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = 2.0,
    self_restart: Callable[[str], object] | None = None,
) -> int:
    """Print the feed. Without `follow`: the history once. With it: header,
    backlog (`last`, default 30), then poll for new events until the job is
    terminal (final line printed) or Ctrl-C. Returns 0."""
    job = store.get_job(job_id)
    masking.arm_job(store, job_id)
    if follow:
        for line in header_lines(store, job):
            print(line[: max(1, width_fn() - 1)], file=out)
        last = 30 if last is None else last

    state: dict = {}
    events = store.events(job_id)
    items = []
    for ev in events:
        item = format_event(ev, state)
        if item is not None and ev.seq > since:
            items.append((ev, *item))
    last_seq = events[-1].seq if events else 0
    if last is not None:
        items = items[-last:] if last > 0 else []
    for ev, role, text in items:
        print(render_line(ev, role, text, width=width_fn(), color=color), file=out)
    out.flush()
    if not follow:
        return 0

    try:
        while True:
            job = store.get_job(job_id)
            new = [e for e in store.events(job_id) if e.seq > last_seq]
            for ev in new:
                last_seq = ev.seq
                item = format_event(ev, state)
                if item is not None:
                    print(render_line(ev, item[0], item[1], width=width_fn(), color=color), file=out)
            if is_terminal(job.state):
                now = datetime.now().astimezone().strftime("%H:%M:%S")
                line = f"{now} {'job':<{ROLE_WIDTH}} job {job.state.value if hasattr(job.state, 'value') else job.state} — feed ended"
                print(line[: max(1, width_fn() - 1)], file=out)
                out.flush()
                return 0
            out.flush()
            if self_restart is not None:  # between polls: nothing half-printed (steward.selfrestart)
                self_restart(job_id)
            sleep(poll_s)
    except KeyboardInterrupt:
        return 0
