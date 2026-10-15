"""`steward phone` — a phone-sized Steward screen for Moshi (plain ANSI + raw stdin, no curses).

Layout: HOME (one compact 2-line card per active job) and JOB (state, goal,
tokens, supervisors/watchers, last watcher-feed lines, approvals), plus a
scrollable full FEED. Everything is truncated to the terminal width (never
wrapped), redrawn every 2s, and works at 40 columns.

Reads go straight to the store; every WRITE goes through the same CLI verbs as
any other surface (`python3 -m steward.cli <verb> ... --origin moshi`), so it
is relayed to the control pane and recorded with origin `moshi` (the watchers
feed shows `[moshi]`). Keys always work; a tap on a job card or a key-hint word
(SGR mouse) is a best-effort shortcut for the same key.

Also here: `steward phone --open/--link` (the `📱 phone` Herdr tab and its
`moshi://herdr?...` deep link) and the pure render/key state machine the tests drive.
"""

from __future__ import annotations

import codecs
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from steward import followup as followup_module
from steward import evidence, incorporate as incorporate_module, jobview, notify
from steward import questions as questions_module
from steward import approvals as approvals_module
from steward import tabnames
from steward import supervisors as supervisors_module
from steward import tokens as tokens_module
from steward import watchers as watchers_module
from steward.models import JobState
from steward.runner_exec import Runner as CommandRunner
from steward.store import Store

PHONE_LABEL = "📱 phone"
REDRAW_S = 2.0
TOKENS_EVERY_S = 10.0
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_STEWARD_ROOT = Path(__file__).resolve().parent.parent
_ACTIVE = frozenset(JobState) - {JobState.STOPPED, JobState.FAILED, JobState.RELEASED}
_ICON_STYLE = {"live": "32", "quiet": "33", "paused": "33", "waiting": "33", "done": "32", "bad": "31"}
_ICON_CHAR = {"quiet": "⚠", "paused": "⏸", "waiting": "◆", "done": "✓", "bad": "■"}

Row = list  # of (text, style) segments; style is an SGR code string or ""


# -- data ----------------------------------------------------------------------


@dataclass
class JobInfo:
    job_id: str
    state: str
    goal: str
    name: str
    hint: str
    kind: str  # liveness kind
    liveness: str
    ask: str | None
    pending: list[dict]
    deep_link: str | None
    tokens: dict | None = None
    follow_up: str | None = None
    questions: dict | None = None  # questions.summary of the job's queue (one kv read)
    hint_base: str = ""  # the hint without the ETA part (home_lines drops the ETA first on a narrow row)
    eta_part: str | None = None  # the stored ETA header text counted down to now (G17)
    eta_line: str | None = None  # the job-page detail line


@dataclass
class Snapshot:
    jobs: list[JobInfo] = field(default_factory=list)
    grand: dict | None = None


def gather(store: Store, tokens_data: dict | None = None, now=None) -> Snapshot:
    """Everything HOME needs, read-only. `tokens_data` is `tokens.all_tokens` output (or None)."""
    from steward import delivery, ownership

    jobs = []
    shown = store.list_jobs(_ACTIVE)
    seen = {j.job_id for j in shown}
    try:  # a RELEASED job is off the phone, unless an owner decision carried forward from it is still unanswered
        shown += [j for j in (store.get_job(jid) for jid in ownership.carried_decisions(store)) if j and j.job_id not in seen]
    except Exception:  # noqa: BLE001 - the extra rows are best effort; the active list always renders
        pass
    for job in shown:
        state = job.state.value
        events = jobview.event_dicts(store.events(job.job_id))
        proj = ownership.projection(store, job.job_id)
        kind, detail = jobview.liveness(state, events, now, ownership=proj,
                                        delivery=delivery.liveness_hint(store, job.job_id, state))
        pend = [a for a in approvals_module.list_approvals(store, job.job_id) if a.get("status") == "pending"]
        step = store.kv_get(job.job_id, "last_class")
        age = detail.rsplit(" · ", 1)[-1] if " · " in detail else detail
        hint = {"live": (f"steward reviewing {age.replace(' ago', '')}" if state == "NEEDS_HUMAN"
                         else f"{step or 'working'} {age.replace(' ago', '')}"),
                "quiet": f"quiet {age.replace('no activity ', '').replace(' ago', '')}",
                "paused": "paused", "waiting": "waiting on you", "done": state.lower(),
                "bad": state.lower()}[kind]
        qsum = questions_module.summary(questions_module.load(store, job.job_id))
        stored = jobview.live_eta(store, job.job_id, state)
        epoch = now.timestamp() if hasattr(now, "timestamp") else time.time()
        part = jobview.eta_part(stored, epoch)
        dpart = jobview.deadline_part(store, job.job_id, now if hasattr(now, "timestamp") else None)
        if dpart:  # v2.2: the deadline sign rides the base hint, so a narrow row drops the ETA first, not this
            hint = f"{hint} \u00b7 {dpart}"
        base, hint = hint, (f"{hint} \u00b7 {part}" if part else hint)
        jobs.append(JobInfo(
            job.job_id, state, job.goal, tabnames.job_name(store, job, persist=False), hint, kind, detail,
            jobview.reason(approvals_module.list_approvals(store, job.job_id), state, events, questions=qsum,
                           ownership=proj), pend,
            notify.deep_link(store.kv_get(job.job_id, "herdr_workspace"), store.kv_get(job.job_id, "herdr_tab"),
                             store.kv_get(job.job_id, "herdr_pane")),
            ((tokens_data or {}).get("jobs") or {}).get(job.job_id),
            followup_module.follows_label(store, job.job_id),
            qsum, base, part, jobview.eta_line(stored, epoch) if part else None,
        ))
    return Snapshot(jobs, (tokens_data or {}).get("grand"))


def fetch_tokens(store: Store) -> dict | None:
    try:
        ids = [j.job_id for j in store.list_jobs(_ACTIVE)]
        return tokens_module.all_tokens(store, ids) if ids else None
    except Exception:  # noqa: BLE001 - tokens are decoration; never break the screen
        return None


def money(bucket: dict | None) -> float:
    return float((bucket or {}).get("usd", 0) or 0)


# -- rendering -----------------------------------------------------------------


@dataclass
class Line:
    row: Row
    hits: list = field(default_factory=list)  # (col_start, col_end, key), 1-based inclusive
    tap: str | None = None  # key a tap anywhere on the row presses


def vwidth(row: Row) -> int:
    return sum(len(t) for t, _ in row)


def clip(row: Row, width: int) -> Row:
    """Truncate to `width` visible cells; an ellipsis marks the cut."""
    out, used = [], 0
    total = vwidth(row)
    for text, style in row:
        if used >= width:
            break
        room = width - used
        if len(text) > room:
            text = text[:room]
        out.append((text, style))
        used += len(text)
    if total > width and out and width > 0:
        text, style = out[-1]
        out[-1] = (text[:-1] + "…", style)
    return out


def ansi(row: Row) -> str:
    return "".join(f"\x1b[{s}m{t}\x1b[0m" if s else t for t, s in row)


def plain(line: Line) -> str:
    return "".join(t for t, _ in line.row)


def _hint_lines(hints: list[tuple[str, str]], width: int) -> list[Line]:
    """Pack `(key, label)` hints, each rendered `<key> <label>`, onto lines <= width; every word is tappable."""
    lines, row, hits, col = [], [], [], 0
    for key, label in hints:
        word = f"{key} {label}" if label else key
        need = len(word) + (2 if row else 0)
        if row and col + need > width:
            lines.append(Line(row, hits))
            row, hits, col = [], [], 0
            need = len(word)
        if row:
            row.append(("  ", ""))
            col += 2
        row.append((word, "2"))
        hits.append((col + 1, col + len(word), key))
        col += len(word)
    if row:
        lines.append(Line(row, hits))
    return lines


def _seg_row(*segs) -> Row:
    return [s for s in segs if s[0]]


def _icon(kind: str, frame: int) -> tuple[str, str]:
    return (SPINNER[frame % len(SPINNER)] if kind == "live" else _ICON_CHAR[kind], _ICON_STYLE[kind])


def header_line(snap: Snapshot) -> Line:
    g = snap.grand or {}
    n = len(snap.jobs)
    total, hour = money(g.get("total")), money(g.get("last_hour"))
    return Line(_seg_row(("STEWARD", "1"), (f"  {n} job{'s' if n != 1 else ''} · ${total:.2f} · 1h ${hour:.2f}", "")))


def home_lines(snap: Snapshot, width: int, frame: int = 0) -> list[Line]:
    lines = [header_line(snap), Line([])]
    if not snap.jobs:
        lines.append(Line([("no active jobs", "2")]))
        lines.append(Line([("say: Run Steward on <repo>: <goal>", "2")]))
    for i, j in enumerate(snap.jobs[:9], 1):
        icon, style = _icon(j.kind, frame)
        prefix = f"[{i}] "
        hint = j.hint
        if j.eta_part:  # the ETA is dropped first when the row is narrow (same rule as the pane header)
            hint = jobview.with_eta(j.hint_base, j.eta_part, width - len(prefix) - 2 - 3 - min(len(j.name), 12))
        room = max(width - len(prefix) - 2 - len(hint) - 1, 4)
        name = j.name[:room] if len(j.name) <= room else j.name[: room - 1] + "…"
        lines.append(Line(_seg_row((prefix, "1"), (icon + " ", style), (name, ""), ("  " + hint, "2")), tap=str(i)))
        if j.ask:
            lines.append(Line(_seg_row(("    ◆ ", "33"), (j.ask, "33")), tap=str(i)))
        else:
            lines.append(Line(_seg_row(("    ", ""), (j.liveness, "2")), tap=str(i)))
    if len(snap.jobs) > 9:
        lines.append(Line([(f"+{len(snap.jobs) - 9} more (not openable here)", "2")]))
    return lines


HOME_HINTS = [("1-9", "open"), ("a", "approve"), ("r", "refresh"), ("q", "quit")]
JOB_HINTS = [("s", "steer"), ("u", "urgent"), ("f", "focus"), ("a", "approve"), ("d", "deny"), ("t", "toggle"),
             ("w", "feed"), ("v", "worker"), ("c", "chat"), ("p", "pause"), ("x", "stop"), ("b", "back")]
FEED_HINTS = [("j", "newer"), ("k", "older"), ("b", "back")]
Q_HINTS = [("1-4", "pick"), ("n", "note"), ("o", "other"), ("d", "defer"), ("enter", "save+next"), ("s", "skip"),
           ("e", "evidence"), ("A", "accept all ★"), ("b", "back")]
Q_SUMMARY_HINTS = [("enter", "next round"), ("f", "write now"), ("c", "write+continue"), ("b", "back")]
TRANSCRIPT_TURNS = 30


@dataclass
class JobDetail:
    info: JobInfo
    sup_rows: list[dict]
    feed: list[tuple]  # (event, role, text)
    panel: list[str] = field(default_factory=list)  # evidence for a pending decision ([] when none)
    decision: dict | None = None
    incorporate: list[str] = field(default_factory=list)  # ACCEPTED job: preflight or "✓ incorporated <sha>"


def gather_detail(store: Store, info: JobInfo) -> JobDetail:
    try:
        rows = supervisors_module.supervisor_rows(store, info.job_id)
    except Exception:  # noqa: BLE001
        rows = []
    feed = watchers_module.collect(store.events(info.job_id))
    try:
        dec = evidence.decision(store, info.job_id)
        panel = evidence.panel_cached(store, info.job_id, 120, dec=dec) if dec else []
    except Exception:  # noqa: BLE001 - evidence is decoration; never break the screen
        dec, panel = None, []
    inc: list[str] = []
    if info.state == "ACCEPTED":
        try:
            done = incorporate_module.is_incorporated(store, info.job_id)
            inc = [f"✓ incorporated {done[:7]}"] if done else incorporate_module.preflight(store, info.job_id).lines(width=0)
        except Exception as exc:  # noqa: BLE001
            inc = [f"incorporate preflight failed: {exc}"]
    return JobDetail(info, rows, feed, panel, dec, inc)


def _feed_row(ev, role: str, text: str) -> Row:
    from datetime import datetime

    ts = datetime.fromisoformat(ev.ts).astimezone().strftime("%H:%M")
    return _seg_row((ts + " ", "2"), (f"{role:<5} ", watchers_module._COLORS.get(role, "0")), (text, ""))


def sup_line(r: dict, frame: int) -> Row:
    disp = r["display"].replace("{SPIN}", SPINNER[frame % len(SPINNER)])
    if r["role"] == "worker":
        return _seg_row((f"{'worker':<7}", "1"), (disp, ""))
    disp = re.sub(r"^\[[^\]]*\] ?", "", disp)
    sw = r.get("switch") or r["status"]
    style = "2" if r["status"] == "off" else ""
    return _seg_row((f"{r.get('label', r['role'])[:8]:<8}", "1"), (f"[{sw}] ", "32" if r["status"] != "off" else "2"), (disp, style))


def _decision_hints(dec: dict) -> list[tuple[str, str]]:
    acc = dec["kind"] == "acceptance"
    if acc:
        return [("A", "accept anyway" if dec.get("deliverable") else "accept"), ("S", "send back"), ("x", "stop"), ("C", "chat")]
    return [("A", "approve"), ("R", "deny"), ("?", "why")]


def job_lines(detail: JobDetail, width: int, frame: int = 0) -> list[Line]:
    j = detail.info
    icon, style = _icon(j.kind, frame)
    lines = [Line(_seg_row(("STEWARD ", "1"), (j.job_id[:8], ""), ("  " + j.state, "2"))),
             Line(_seg_row((icon + " ", style), (j.liveness, "")))]
    if j.eta_line:
        lines += [Line([(w, "2")]) for w in textwrap.wrap(j.eta_line, max(width, 10), subsequent_indent="  ") or [""]]
    if j.ask:
        lines.append(Line(_seg_row(("◆ ", "33"), (j.ask, "33"))))
        lines += [Line([(ln, "2")]) for ln in detail.panel]
        if detail.decision:
            lines += _hint_lines(_decision_hints(detail.decision), width)
    if detail.incorporate:
        lines.append(Line([("incorporate", "1;4")]))
        for ln in detail.incorporate:
            lines += [Line([(w, "2")]) for w in textwrap.wrap(ln, max(width, 10), subsequent_indent="  ") or [""]]
    goal = textwrap.wrap(" ".join(j.goal.split()), max(width, 10)) or [""]
    for k, g in enumerate(goal[:3]):
        if k == 2 and len(goal) > 3:
            g = g[: max(width - 1, 1)] + "…"
        lines.append(Line([(g, "")]))
    if j.follow_up:
        lines.append(Line([(j.follow_up, "2")]))
    t = j.tokens
    if t:
        tot, hr = t["total"], t["last_hour"]
        lines.append(Line([(f"tok {tokens_module.humanize(tot['all'])} eff {tokens_module.humanize(tot.get('effective', 0))} "
                            f"${money(tot):.2f} · 1h ${money(hr):.2f}", "2")]))
    lines.append(Line([("supervisors", "1;4")]))
    for r in detail.sup_rows:
        lines.append(Line(sup_line(r, frame)))
    lines.append(Line([("feed", "1;4")]))
    for ev, role, text in detail.feed[-6:]:
        lines.append(Line(_feed_row(ev, role, text)))
    if not detail.feed:
        lines.append(Line([("(nothing yet)", "2")]))
    if j.pending:
        lines.append(Line([("approvals", "1;4")]))
        for n, a in enumerate(j.pending, 1):
            lines.append(Line(_seg_row((f"[{n}] ", "1"), (a["question"].removeprefix("worker permission: "), "33"))))
    return lines


def feed_lines(detail: JobDetail, width: int, rows: int, offset: int) -> list[Line]:
    """The full feed, newest last; `offset` = lines scrolled back from the bottom."""
    page = max(rows - 4, 3)
    items = detail.feed
    end = max(len(items) - offset, min(page, len(items)))
    chunk = items[max(end - page, 0):end]
    lines = [Line(_seg_row(("FEED ", "1"), (detail.info.job_id[:8], ""),
                           (f"  {end}/{len(items)}", "2")))]
    lines += [Line(_feed_row(ev, role, text)) for ev, role, text in chunk]
    return lines


def transcript_lines(act: dict, job_id: str, width: int, rows: int, offset: int) -> list[Line]:
    """The worker transcript (last 30 turns) wrapped to `width`, newest last;
    `offset` = lines scrolled back from the bottom."""
    import textwrap

    from steward import transcript as tr

    body: list[Line] = []
    for t in act["turns"]:
        kind = t["kind"]
        label = tr._LABEL.get(kind, kind)
        if kind == "steer":
            m = tr._STEER_RE.match(t["text"])
            label = f"STEER {m.group(1)}" if m else "STEER"
        style = tr._COLORS.get(kind, "0")
        head = f"{tr._hms(t['ts'])[:5]} {label} "
        text = " ".join(t["text"].split())
        wrapped = textwrap.wrap(text, max(width - 2, 10), initial_indent="  ", subsequent_indent="  ") or ["  "]
        body.append(Line(_seg_row((head, style))))
        body += [Line(_seg_row((w, "2" if kind in ("result", "thinking") else ""))) for w in wrapped[:6]]
    page = max(rows - 4, 3)
    end = max(len(body) - offset, min(page, len(body)))
    chunk = body[max(end - page, 0):end]
    head_lines = [Line(_seg_row(("WORKER ", "1"), (job_id[:8], ""), (f"  {end}/{len(body)}", "2"))),
                  Line(_seg_row((act["now"], "36")))]
    return head_lines + chunk[-max(page - 1, 1):]


# -- the question queue (Q mode) -------------------------------------------------


_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
EVIDENCE_CAP = 8


def _safe(text) -> str:
    """Job-authored text for the screen: whitespace collapsed, every control character (escape sequences) dropped (T-17-17)."""
    return _CTRL.sub("", " ".join(str(text or "").split()))


def _wrap(text, width: int, first: str = "", rest: str = "", cap: int = 2) -> list[str]:
    """`text` wrapped to `width`, at most `cap` lines (an ellipsis marks the cut), control characters removed (T-17-20)."""
    w = max(width, 10)
    lines = textwrap.wrap(_safe(text), w, initial_indent=first, subsequent_indent=rest) or [first.rstrip()]
    if len(lines) > cap:
        lines = lines[:cap]
        lines[-1] = lines[-1][: max(w - 1, 1)].rstrip() + "…"
    return lines


def _answer_label(card: dict, answer: dict | None) -> str:
    """What an earlier answer said, as a short label (for the stale line)."""
    a = answer or {}
    opt = a.get("option")
    if opt == "other":
        return a.get("other") or "other"
    if opt == "recommended":
        return card.get("recommended_text") or "recommended"
    label = next((o["label"] for o in card["options"] if o["key"] == opt), "")
    return label or a.get("label") or str(opt or "")


def q_lines(card: dict, *, pos: int, n: int, summ: dict, choice: dict, evidence: bool, width: int,
            round_no: int = 1, rounds: int = 1, job_id: str = "") -> list[Line]:
    """One question card: header, title, badges, options (star first), the picked note/other text, why, evidence."""
    head = f"QUESTIONS {job_id[:8]} {pos}/{n} · {summ['waiting']} waiting · {summ['starred']} ★"
    lines = [Line(_seg_row((head, "1")))]
    if rounds > 1:
        lines.append(Line([(f"round {round_no}/{rounds}", "2")]))
    anchor = card.get("anchor") or ""
    title = f"{anchor}  {card['title']}" if anchor else card["title"]
    lines += [Line([(t, "1")]) for t in _wrap(title, width)]
    badges = [b for b, on in (("blocks work", card["blocking"]), ("(updated)", card["flags"].get("updated"))) if on]
    if badges:
        lines.append(Line([("  ".join(badges), "33")]))
    if card["status"] == "stale" and card.get("answer"):
        lines.append(Line([(_wrap("stale: was " + _answer_label(card, card["answer"]), width, cap=1)[0], "33")]))
    sel = choice.get("option")
    opts = card["options"]
    if opts:
        default = card["starred"] and not sel
        for i, o in enumerate(opts, 1):
            on = (sel == o["key"]) or (sel == "recommended" and o["recommended"]) or (default and i == 1 and o["recommended"])
            first = ("▶" if on else " ") + f"[{i}]" + ("★ " if o["recommended"] else "  ")
            lines += [Line([(t, "1" if on else "")], tap=str(i)) for t in _wrap(o["label"], width, first, " " * 6)]
    elif card["starred"]:
        on = sel in (None, "recommended")
        lines += [Line([(t, "1" if on else "")], tap="1")
                  for t in _wrap(card["recommended_text"], width, ("▶" if on else " ") + "[1]★ ", " " * 6)]
    if sel == "other":
        lines += [Line([(t, "1")]) for t in _wrap(choice.get("other", ""), width, "▶other: ", " " * 8)]
    if choice.get("note"):
        lines += [Line([(t, "36")]) for t in _wrap(choice["note"], width, " note: ", " " * 7)]
    if card["rationale"]:
        lines += [Line([(t, "2")]) for t in _wrap(card["rationale"], width, "why: ", "     ")]
    if evidence:
        refs = card["evidence"]
        lines += [Line([(t, "2")]) for ref in refs[:EVIDENCE_CAP] for t in _wrap(ref, width, " · ", "   ", cap=1)]
        if len(refs) > EVIDENCE_CAP:
            lines.append(Line([(f" +{len(refs) - EVIDENCE_CAP} more", "2")]))
        if not refs:
            lines.append(Line([(" no evidence refs", "2")]))
    return lines


def compose(body: list[Line], footer: list[Line], width: int, rows: int) -> list[Line]:
    """Clip every row to `width` (fixing the tap columns), and body to leave room for the footer."""
    room = max(rows - len(footer), 1)
    out = []
    for ln in body[:room]:
        out.append(Line(clip(ln.row, width), [h for h in ln.hits if h[0] <= width], ln.tap))
    out += [Line(clip(ln.row, width), [(a, min(b, width), k) for a, b, k in ln.hits if a <= width], ln.tap)
            for ln in footer]
    return out


# -- state machine -------------------------------------------------------------


@dataclass
class Result:
    quit: bool = False


class Phone:
    """Screen state + key handling. `run_cli(argv) -> (rc, last_line)` performs writes."""

    def __init__(self, store: Store, run_cli: Callable[[list[str]], tuple[int, str]] | None = None,
                 tokens_fn: Callable[[Store], dict | None] | None = None, clock=time.time):
        self.store = store
        self.run_cli = run_cli or run_steward_cli
        self.tokens_fn = tokens_fn or fetch_tokens
        self.clock = clock
        self.screen = "home"  # home | job | feed | transcript | q
        self.job_id: str | None = None
        self.mode: str | None = None  # input | confirm | pick
        self.purpose = ""
        self.buf = ""
        self.items: list = []
        self.pending_text = ""
        self.note = ""
        self.feed_off = 0
        self.frame = 0
        self.rows = 30
        self._tok: dict | None = None
        self._tok_at = 0.0
        self.snap = Snapshot()
        self.q_job: str | None = None
        self.q_ids: list[str] = []  # the qids of the current round
        self.q_pos = 0
        self.q_choice: dict[str, dict] = {}  # qid -> {"option", "other", "note"}
        self.q_stage = "card"  # card | summary
        self.q_round = 0
        self.q_rounds = 1
        self.q_saved = 0  # writes in the current round
        self.q_session = 0  # answers saved since entering Q mode and not yet flushed with --done
        self.q_evidence = False
        self.refresh(force_tokens=True)

    # data
    def refresh(self, force_tokens: bool = False) -> None:
        now = self.clock()
        if force_tokens or now - self._tok_at >= TOKENS_EVERY_S:
            self._tok, self._tok_at = self.tokens_fn(self.store), now
        self.snap = gather(self.store, self._tok)

    def job(self) -> JobInfo | None:
        return next((j for j in self.snap.jobs if j.job_id == self.job_id), None)

    def _all_pending(self) -> list[tuple[str, dict]]:
        return [(j.job_id, a) for j in self.snap.jobs for a in j.pending]

    # rendering
    def lines(self, width: int, rows: int) -> list[Line]:
        self.rows = rows
        if self.screen != "home" and self.job() is None:
            self.screen, self.job_id = "home", None
        if self.screen == "home":
            body = home_lines(self.snap, width, self.frame)
            hints = ([("Q", "questions")] if self._q_jobs() else []) + HOME_HINTS
        elif self.screen == "job":
            body = job_lines(gather_detail(self.store, self.job()), width, self.frame)
            hints = JOB_HINTS
            if self.job().state == "ACCEPTED":
                hints = [("I", "incorporate"), ("L", "release"), *JOB_HINTS]
            if (self.job().questions or {}).get("waiting"):
                hints = [("Q", "answer"), *hints]
        elif self.screen == "q":
            body, hints = self._q_body(width)
        elif self.screen == "transcript":
            from steward import transcript as tr

            act = tr.worker_activity(self.store, self.job_id, last=TRANSCRIPT_TURNS)
            body = transcript_lines(act, self.job_id, width, rows, self.feed_off)
            hints = FEED_HINTS
        else:
            body = feed_lines(gather_detail(self.store, self.job()), width, rows, self.feed_off)
            hints = FEED_HINTS
        footer = self._footer(width, hints)
        if self.mode == "pick":
            body = body + [Line([(f"[{n}] {label}", "1")], tap=str(n)) for n, (label, _) in enumerate(self.items, 1)]
        return compose(body, footer, width, rows)

    def _footer(self, width: int, hints) -> list[Line]:
        note = [Line([(self.note, "36")])] if self.note else []
        if self.mode == "input":
            tail = self.buf[-max(width - len(self._prompt()) - 2, 1):]
            help_ = "Enter defers · Esc cancels" if self.purpose == "qdefer" else "Enter save · empty = cancel"
            if self.purpose not in ("qnote", "qother", "qdefer"):
                help_ = "Enter send · empty = cancel"
            return note + [Line(_seg_row((self._prompt(), "33"), (tail + "▏", ""))),
                           Line([(help_, "2")])]
        if self.mode == "confirm":
            return note + [Line([(self._prompt(), "33")])] + _hint_lines([("y", "yes"), ("n", "no")], width)
        if self.mode == "pick":
            return note + [Line([(f"{self._prompt()} (number, Esc cancels)", "33")])]
        return note + _hint_lines(hints, width)

    def _prompt(self) -> str:
        if self.mode == "input":
            return {"steer": "steer> ", "urgent": "URGENT> ", "focus": "focus> ", "sendback": "send back> ",
                    "qnote": "note> ", "qother": "other> ",
                    "qdefer": "defer reason (Enter = none)> "}.get(self.purpose, "> ")
        if self.mode == "confirm":
            if self.purpose == "urgent":
                return f"send URGENT (interrupts the worker): \"{self.pending_text[:20]}\"? y/n"
            if self.purpose == "acceptrec":
                return self._q_review() + "? y/n"
            if self.purpose in ("accept", "reject", "approve", "deny"):
                return f"{self.purpose} {(self.job_id or '')[:8]}? y/n"
            return "stop this job? y/n"
        return {"approve": "approve which?", "deny": "deny which?", "toggle": "toggle which?",
                "questions": "questions which job?"}.get(self.purpose, "pick")

    # keys
    def handle(self, key: str) -> Result:
        """`key`: a printable char, or enter/esc/backspace/up/down."""
        self.note = ""
        if self.mode == "input":
            return self._key_input(key)
        if self.mode == "confirm":
            return self._key_confirm(key)
        if self.mode == "pick":
            return self._key_pick(key)
        if self.screen == "home":
            return self._key_home(key)
        if self.screen == "q":
            return self._key_q(key)
        if self.screen in ("feed", "transcript"):
            return self._key_feed(key)
        return self._key_job(key)

    def _key_home(self, key: str) -> Result:
        if key == "q":
            return Result(quit=True)
        if key == "r":
            self.refresh(force_tokens=True)
            self.note = "refreshed"
        elif key.isdigit() and key != "0":
            self._open(int(key))
        elif key == "a":
            self._start_pick("approve", self._all_pending(), whole=True)
        elif key == "Q":
            self._key_home_q()
        return Result()

    def _q_jobs(self) -> list[JobInfo]:
        return [j for j in self.snap.jobs[:9] if (j.questions or {}).get("waiting")]

    def _key_home_q(self) -> None:
        jobs = self._q_jobs()
        if not jobs:
            self.note = "no open questions"
        elif len(jobs) == 1:
            self.open_questions(jobs[0].job_id)
        else:
            self.mode, self.purpose = "pick", "questions"
            self.items = [(f"{j.job_id[:4]} {j.name} ({j.questions['waiting']})", ("questions", j.job_id)) for j in jobs]

    def _open(self, n: int) -> None:
        if 1 <= n <= len(self.snap.jobs[:9]):
            self.job_id, self.screen = self.snap.jobs[n - 1].job_id, "job"
        else:
            self.note = f"no job {n}"

    def _key_job(self, key: str) -> Result:
        j = self.job()
        if key in ("b", "esc", "q"):
            self.screen, self.job_id = "home", None
        elif key == "s":
            self._start_input("steer")
        elif key == "u":
            self._start_input("urgent")
        elif key == "f":
            self._start_input("focus")
        elif key in ("a", "d"):
            self._start_pick("approve" if key == "a" else "deny", [(j.job_id, a) for a in j.pending])
        elif key == "t":
            rows = [r for r in gather_detail(self.store, j).sup_rows if r.get("next")]
            self._start_pick("toggle", [(r["role"], r) for r in rows], toggle=True)
        elif key == "w":
            self.screen, self.feed_off = "feed", 0
        elif key == "v":
            self.screen, self.feed_off = "transcript", 0
        elif key == "c":
            rc, out = self.run_cli(["chat", j.job_id, "worker"])
            self.note = (out or "chat failed")[:200] if rc else f"{out} — look in tab ⚙ {j.job_id[:8]}"
        elif key == "p":
            verb = "resume" if j.state == "PAUSED" else "pause"
            self._write([verb, j.job_id, "--origin", "moshi"], f"{verb}d" if verb == "pause" else "resumed")
        elif key == "x":
            self.mode, self.purpose = "confirm", "stop"
        elif key in ("A", "R", "?"):
            self._decide(j, key)
        elif key == "C":
            rc, out = self.run_cli(["chat", j.job_id, "job"])
            self.note = (out or "chat failed")[:200] if rc else f"{out} — look in tab ⚙ {j.job_id[:8]}"
        elif key == "S":
            if j.state != "NEEDS_HUMAN":
                self.note = "only a NEEDS_HUMAN job can be sent back"
            else:
                self._start_input("sendback")
        elif key == "Q":
            self.open_questions(j.job_id)
        elif key in ("I", "L"):
            if j.state != "ACCEPTED":
                self.note = "only an ACCEPTED job can be incorporated/released"
            elif key == "I":  # one key merges (owner: one-tap decisions); the preflight is on screen
                self._write(["incorporate", j.job_id, "--origin", "moshi"], "incorporated")
            else:
                self._write(["release", j.job_id, "--origin", "moshi"], "released")
        return Result()

    # -- question queue (Q mode, G3) --------------------------------------------------
    def _q_waiting(self) -> list[dict]:
        return [r for r in questions_module.ordered(questions_module.load(self.store, self.q_job))
                if r.get("status") in questions_module.WAITING]

    def open_questions(self, job_id: str) -> bool:
        """Start Q mode for a job at its first round; False (with a note) when nothing waits."""
        self.refresh()
        if not any(j.job_id == job_id for j in self.snap.jobs) or not any(
                r.get("status") in questions_module.WAITING for r in questions_module.load(self.store, job_id)):
            self.note = "no open questions"
            return False
        self.q_job, self.job_id, self.screen = job_id, job_id, "q"
        self.q_round, self.q_session, self.q_choice, self.q_evidence = 0, 0, {}, False
        self._q_start_round()
        return True

    def _q_start_round(self) -> None:
        waiting = self._q_waiting()
        self.q_round += 1
        self.q_ids = [r["qid"] for r in waiting[: questions_module.ROUND_SIZE]]
        self.q_rounds = self.q_round - 1 + -(-len(waiting) // questions_module.ROUND_SIZE)
        self.q_pos, self.q_saved, self.q_stage = 0, 0, "card" if self.q_ids else "summary"

    def _q_card(self) -> dict | None:
        """The card for q_ids[q_pos] from a fresh view, so a card just answered can still be shown."""
        if not (0 <= self.q_pos < len(self.q_ids)):
            return None
        qid = self.q_ids[self.q_pos]
        return next((c for c in questions_module.view(self.store, self.q_job, include_all=True)["questions"]
                     if c["qid"] == qid), None)

    def _q_selection(self, card: dict) -> dict:
        """The current choice for a card; a stale card starts on its old answer so `enter` re-confirms it (G10)."""
        ch = self.q_choice.get(card["qid"])
        if ch is None:
            ch = {"option": None, "other": "", "note": ""}
            old = card.get("answer")
            if card["status"] == "stale" and old:
                ch["option"] = old.get("option") or None
                ch["other"] = old.get("other") or ""
                ch["note"] = old.get("note") or ""
            self.q_choice[card["qid"]] = ch
        return ch

    def _q_body(self, width: int) -> tuple[list[Line], list]:
        if self.q_stage == "summary":
            return self._q_summary(), Q_SUMMARY_HINTS
        card = self._q_card()
        if card is None:  # the card left the store (withdrawn): move on
            self._q_advance()
            return self._q_body(width) if self.q_stage == "summary" else ([Line([("(question gone)", "2")])], Q_HINTS)
        summ = questions_module.summary(questions_module.load(self.store, self.q_job))
        return q_lines(card, pos=self.q_pos + 1, n=len(self.q_ids), summ=summ, choice=self._q_selection(card),
                       evidence=self.q_evidence, width=width, round_no=self.q_round, rounds=self.q_rounds,
                       job_id=self.q_job or ""), Q_HINTS

    def _q_summary(self) -> list[Line]:
        waiting = self._q_waiting()
        left_ids = {r["qid"] for r in waiting}
        skipped = sum(1 for q in self.q_ids if q in left_ids)
        head = f"ROUND {self.q_round} DONE · {self.q_saved} answered" + (f" · {skipped} skipped" if skipped else "")
        head += f" · {len(waiting)} left"
        lines = [Line(_seg_row(("QUESTIONS " + (self.q_job or "")[:8], "1"))), Line([(head, "1")])]
        lines.append(Line([("all answered" if not waiting else "more waiting: enter starts the next round", "2")]))
        return lines

    def _q_advance(self) -> None:
        self.q_pos += 1
        if self.q_pos >= len(self.q_ids):
            self.q_stage = "summary"

    def _q_send(self, qid: str, sel: dict, *, defer: bool = False, note: str = "") -> bool:
        """One answer through the relayed verb; True when written."""
        argv = ["answer", self.q_job, qid]
        if defer:
            argv.append("--defer")
        elif sel.get("option") == "other":
            argv += ["--other", sel.get("other", "")]
        else:
            argv += ["--option", sel["option"]]
        if note:
            argv += ["--note", note]
        rc, out = self.run_cli([*argv, "--origin", "moshi"])
        if rc != 0:
            self.note = f"failed: {out}"[:200]
            return False
        self.note = "deferred" if defer else "saved"
        self.q_saved += 1
        self.q_session += 1
        self.q_choice.pop(qid, None)
        self.refresh()
        return True

    def _q_commit(self) -> None:
        card = self._q_card()
        if card is None:
            return self._q_advance()
        sel = self._q_selection(card)
        if not sel.get("option"):
            if not card["starred"]:
                self.note = "pick an option, or o for other text"
                return
            sel = {**sel, "option": "recommended"}
        if self._q_send(card["qid"], sel, note=sel.get("note", "")):
            self._q_advance()

    def _q_input_done(self, text: str) -> None:
        purpose, card = self.purpose, self._q_card()
        self._cancel("")
        if card is None:
            return
        sel = self._q_selection(card)
        if purpose == "qnote":
            sel["note"] = text
            self.note = "note set" if text else "note cleared"
        elif purpose == "qother":
            if not text:
                self.note = "cancelled"
            else:
                sel["option"], sel["other"] = "other", text
        elif self._q_send(card["qid"], sel, defer=True, note=text):
            self._q_advance()

    def _q_flush(self, *, and_continue: bool = False) -> bool:
        argv = ["answer", self.q_job, "--done", *(["--and-continue"] if and_continue else []), "--origin", "moshi"]
        rc, out = self.run_cli(argv)
        self.note = ("written" + (f": {out}" if out else "")) if rc == 0 else f"failed: {out}"[:200]
        if rc == 0:
            self.q_session = 0
        self.refresh()
        return rc == 0

    def _q_review(self) -> str:
        return questions_module.review_line(questions_module.load(self.store, self.q_job))

    def _q_review_accept(self) -> None:
        """A: accept every waiting starred card. Shows the review line first; only `y` writes (G4)."""
        if self._q_review() == questions_module.NOTHING_TO_ACCEPT:
            self.note = questions_module.NOTHING_TO_ACCEPT
            return
        self.mode, self.purpose = "confirm", "acceptrec"

    def _q_accept_all(self) -> None:
        before = {r["qid"] for r in questions_module.load(self.store, self.q_job) if r.get("status") in questions_module.WAITING}
        rc, out = self.run_cli(["answer", self.q_job, "--accept-recommended", "--origin", "moshi"])
        if rc != 0:
            self.note = f"failed: {out}"[:200]
            return
        self.note = out or "accepted"
        waiting_now = {r["qid"] for r in questions_module.load(self.store, self.q_job) if r.get("status") in questions_module.WAITING}
        done = len(before - waiting_now)
        self.q_saved += done
        self.q_session += max(done, 1)
        self.q_stage = "summary"
        self.refresh()

    def _q_leave(self) -> None:
        if self.q_session > 0:
            self._q_flush()
        self.screen = "job"
        self.q_ids, self.q_session = [], 0

    def _key_q(self, key: str) -> Result:
        if self.q_stage == "summary":
            return self._key_q_summary(key)
        card = self._q_card()
        if key in ("b", "esc", "q"):
            self._q_leave()
        elif card is None:
            self._q_advance()
        elif key.isdigit() and key != "0":
            n = int(key)
            if card["options"] and n <= len(card["options"]):
                self._q_selection(card)["option"] = card["options"][n - 1]["key"]
            elif not card["options"] and n == 1 and card["starred"]:
                self._q_selection(card)["option"] = "recommended"
            else:
                self.note = f"no option {n}"
        elif key == "enter":
            self._q_commit()
        elif key in ("s", "j", "down"):
            self._q_advance()
        elif key in ("k", "up"):
            self.q_pos = max(self.q_pos - 1, 0)
        elif key == "e":
            self.q_evidence = not self.q_evidence
        elif key == "A":
            self._q_review_accept()
        elif key == "n":
            self._start_input("qnote")
        elif key == "o":
            self._start_input("qother")
        elif key == "d":
            self._start_input("qdefer")
        return Result()

    def _key_q_summary(self, key: str) -> Result:
        if key in ("b", "esc", "q"):
            self._q_leave()
        elif key == "enter":
            if self._q_waiting():
                self._q_start_round()
            else:
                self.note = "all answered"
        elif key == "A":
            self._q_review_accept()
        elif key == "f":
            self._q_flush()
        elif key == "c":
            if self.job() is None or self.job().state != "NEEDS_HUMAN":
                self.note = "only a NEEDS_HUMAN job can continue"
            else:
                self._q_flush(and_continue=True)
        return Result()

    def _decide(self, j: JobInfo, key: str) -> None:
        """A accept/approve, R reject/deny (each then `y`), ? why-chat, for the job's pending decision."""
        dec = evidence.decision(self.store, j.job_id)
        if dec is None:
            self.note = "nothing waiting on you"
            return
        if key == "?":
            rc, out = self.run_cli(["chat", j.job_id, "why"])
            self.note = (out or "chat failed")[:200] if rc else f"{out} — look in tab ⚙ {j.job_id[:8]}"
            return
        acc, rec = dec["kind"] == "acceptance", dec.get("approval")
        purpose = ("accept" if acc else "approve") if key == "A" else ("reject" if acc else "deny")
        if rec is None and purpose != "accept":
            self.note = "no approval to " + purpose
            return
        # One key decides (owner decision, 2026-09-28) — no y/n step for accept/reject/approve/deny.
        self.purpose, self.pending_text = purpose, rec["id"] if rec else ""
        self._key_confirm("y")

    def _key_feed(self, key: str) -> Result:
        page = max(self.rows - 4, 3)
        if key in ("b", "esc", "q"):
            self.screen = "job"
        elif key in ("k", "up"):
            self.feed_off += 1
        elif key in ("j", "down"):
            self.feed_off = max(self.feed_off - 1, 0)
        elif key == "K":
            self.feed_off += page
        elif key == "J":
            self.feed_off = max(self.feed_off - page, 0)
        j = self.job()
        if j is not None and self.screen == "feed":
            self.feed_off = min(self.feed_off, max(len(gather_detail(self.store, j).feed) - 1, 0))
        return Result()

    # input / confirm / pick
    def _start_input(self, purpose: str) -> None:
        self.mode, self.purpose, self.buf = "input", purpose, ""

    def _cancel(self, note: str = "cancelled") -> None:
        self.mode, self.purpose, self.buf, self.items, self.pending_text = None, "", "", [], ""
        self.note = note

    def _key_input(self, key: str) -> Result:
        if key == "esc":
            self._cancel()
        elif key == "backspace":
            self.buf = self.buf[:-1]
        elif key == "enter":
            text = self.buf.strip()
            if self.purpose in ("qnote", "qother", "qdefer"):
                self._q_input_done(text)
            elif not text:
                self._cancel()
            elif self.purpose == "urgent":
                self.mode, self.pending_text, self.buf = "confirm", text, ""
            elif self.purpose == "sendback":
                jid = self.job_id
                self._cancel("")
                self._write(["send-back", jid, text, "--origin", "moshi"], "sent back; new worker starting")
            else:
                verb = "focus" if self.purpose == "focus" else "nudge"
                jid = self.job_id
                self._cancel("")
                self._write(["steer", jid, text, "--verb", verb, "--origin", "moshi"], f"{verb} sent")
        elif len(key) == 1 and key.isprintable():
            self.buf += key
        return Result()

    def _key_confirm(self, key: str) -> Result:
        purpose, text, jid = self.purpose, self.pending_text, self.job_id
        if purpose == "acceptrec":
            self._cancel("")
            if key == "y":
                self._q_accept_all()
            else:
                self.note = "cancelled"
            return Result()
        if key == "y":
            self._cancel("")
            if purpose == "urgent":
                self._write(["steer", jid, text, "--verb", "nudge", "--urgent", "--origin", "moshi"], "urgent steer sent")
            elif purpose == "stop":
                self._write(["stop", jid, "--origin", "moshi"], "stopped")
            elif purpose == "accept":
                self._write(["accept", jid, "--origin", "moshi"], "accepted")
            elif purpose in ("approve", "deny", "reject"):
                verb = "approve" if purpose == "approve" else "deny"
                self._write([verb, jid, text, "--origin", "moshi"], {"approve": "approved", "deny": "denied", "reject": "stopped"}[purpose])
        else:
            self._cancel("cancelled")
        return Result()

    def _start_pick(self, purpose: str, entries: list, *, whole: bool = False, toggle: bool = False) -> None:
        if not entries:
            self.note = "nothing pending" if purpose != "toggle" else "nothing to toggle"
            return
        if toggle:
            items = [(f"{r.get('label', r['role'])} [{r.get('switch')}] → {r['next']}", ("toggle", jid_role)) for jid_role, r in entries]
        else:
            items = [((f"{jid[:4]} " if whole else "") + a["question"].removeprefix("worker permission: ")[:40],
                      (purpose, jid, a["id"])) for jid, a in entries]
        if len(items) == 1:
            self._do_pick(purpose, items[0][1])
        else:
            self.mode, self.purpose, self.items = "pick", purpose, items

    def _key_pick(self, key: str) -> Result:
        if key == "esc":
            self._cancel()
        elif key.isdigit() and 1 <= int(key) <= len(self.items):
            _, payload = self.items[int(key) - 1]
            purpose = self.purpose
            self._cancel("")
            self._do_pick(purpose, payload)
        return Result()

    def _do_pick(self, purpose: str, payload: tuple) -> None:
        if purpose == "questions":
            self.open_questions(payload[1])
        elif purpose == "toggle":
            self._write(["supervisor", self.job_id, payload[1], "toggle", "--origin", "moshi"], f"{payload[1]} toggled")
        else:
            _, jid, aid = payload
            self._write([purpose, jid, aid, "--origin", "moshi"], f"{purpose}d")

    def _write(self, argv: list[str], ok: str) -> None:
        rc, out = self.run_cli(argv)
        self.note = ok if rc == 0 else f"failed: {out}"[:200]
        self.refresh()

    # mouse
    def tap(self, col: int, row: int, lines: list[Line]) -> None:
        """A tap at 1-based (col,row) on the last-drawn `lines`: hint word -> its key, card/row -> its tap key."""
        if not (1 <= row <= len(lines)):
            return
        ln = lines[row - 1]
        for c0, c1, key in ln.hits:
            if c0 <= col <= c1:
                self._tap_key(key)
                return
        if ln.tap:
            self._tap_key(ln.tap)

    def _tap_key(self, key: str) -> None:
        if self.mode == "input":
            return  # taps never edit text or send it
        if key in ("1-9", "1-4"):
            return
        self.handle(key)


# -- CLI bridge ----------------------------------------------------------------


def run_steward_cli(argv: list[str]) -> tuple[int, str]:
    """Run `python3 -m steward.cli ...` in the repo (same env); `(exit code, last output line)`."""
    try:
        p = subprocess.run([sys.executable, "-m", "steward.cli", *argv], capture_output=True, text=True,
                           cwd=_STEWARD_ROOT, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    text = (p.stdout if p.returncode == 0 else (p.stderr or p.stdout)).strip().splitlines()
    return p.returncode, (text[-1] if text else "")


# -- terminal input ------------------------------------------------------------

_MOUSE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
_CSI = {"A": "up", "B": "down"}


class InputParser:
    """Bytes from stdin -> events: ('key', name) and ('mouse', col, row). Text arriving in bursts
    (phone keyboards, paste) is fine: every char is its own key, Enter (CR/LF) submits."""

    def __init__(self):
        self.dec = codecs.getincrementaldecoder("utf-8")("ignore")
        self.carry = ""

    def feed(self, data: bytes) -> list[tuple]:
        s = self.carry + self.dec.decode(data)
        self.carry, out, i = "", [], 0
        while i < len(s):
            c = s[i]
            if c == "\x1b":
                if i + 1 >= len(s):
                    out.append(("key", "esc"))
                    i += 1
                elif s[i + 1] == "[":
                    m = _MOUSE.match(s, i)
                    if m:
                        b, x, y, kind = int(m[1]), int(m[2]), int(m[3]), m[4]
                        if kind == "M" and b == 0:
                            out.append(("mouse", x, y))
                        elif kind == "M" and b in (64, 65):
                            out.append(("key", "up" if b == 64 else "down"))
                        i = m.end()
                        continue
                    j = i + 2
                    while j < len(s) and not (0x40 <= ord(s[j]) <= 0x7e):
                        j += 1
                    if j >= len(s):
                        self.carry = s[i:]  # incomplete sequence: wait for the rest
                        break
                    if s[j] in _CSI and j == i + 2:
                        out.append(("key", _CSI[s[j]]))
                    i = j + 1
                else:
                    out.append(("key", "esc"))
                    i += 1
            elif c in "\r\n":
                out.append(("key", "enter"))
                i += 1
            elif c in "\x7f\x08":
                out.append(("key", "backspace"))
                i += 1
            elif c == "\x03":
                out.append(("key", "ctrl-c"))
                i += 1
            elif c.isprintable():
                out.append(("key", c))
                i += 1
            else:
                i += 1
        return out


def dispatch(phone: Phone, events: list[tuple], last_lines: list[Line]) -> bool:
    """Feed parsed events to the state machine; True means quit."""
    for ev in events:
        if ev[0] == "mouse":
            phone.tap(ev[1], ev[2], last_lines)
        elif ev[1] == "ctrl-c":
            return True
        elif phone.handle(ev[1]).quit:
            return True
    return False


def draw(phone: Phone, out, size: tuple[int, int]) -> list[Line]:
    width, rows = max(size[0], 10), max(size[1], 5)
    lines = phone.lines(width, rows)
    buf = ["\x1b[?7l"]
    for r, ln in enumerate(lines, 1):
        buf.append(f"\x1b[{r};1H{ansi(ln.row)}\x1b[K")
    buf.append(f"\x1b[{len(lines) + 1};1H\x1b[J")
    out.write("".join(buf))
    out.flush()
    return lines


def start_phone(questions_job: str | None = None, store: Store | None = None) -> Phone:
    """The Phone `run_tui` drives; with `questions_job` it starts on that job's Q screen (HOME + a note when nothing waits)."""
    phone = Phone(store or Store())
    if questions_job:
        phone.open_questions(questions_job)
    return phone


def run_tui(questions_job: str | None = None) -> int:
    import select
    import shutil
    import termios
    import tty

    if not sys.stdin.isatty():
        print("steward phone needs a terminal (run it in a Herdr pane / Moshi)", file=sys.stderr)
        return 2
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    out = sys.stdout
    phone, parser = start_phone(questions_job), InputParser()
    out.write("\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1006h")
    try:
        tty.setcbreak(fd)
        termios.tcflush(fd, termios.TCIFLUSH)  # no phantom taps from input queued before we started
        lines = draw(phone, out, tuple(shutil.get_terminal_size((40, 30))))
        next_draw = time.monotonic() + REDRAW_S
        while True:
            ready, _, _ = select.select([fd], [], [], max(next_draw - time.monotonic(), 0))
            if ready:
                data = os.read(fd, 4096)
                if not data:
                    return 0
                if dispatch(phone, parser.feed(data), lines):
                    return 0
                lines = draw(phone, out, tuple(shutil.get_terminal_size((40, 30))))
            if time.monotonic() >= next_draw:
                phone.frame += 1
                phone.refresh()
                lines = draw(phone, out, tuple(shutil.get_terminal_size((40, 30))))
                next_draw = time.monotonic() + REDRAW_S
    except KeyboardInterrupt:
        return 0
    finally:
        out.write("\x1b[?1000l\x1b[?1006l\x1b[?7h\x1b[?25h\x1b[?1049l")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


# -- the `📱 phone` Herdr tab ---------------------------------------------------


def phone_command(questions_job: str | None = None) -> str:
    """Shell line run in the tab: same env export + cd prefix the runner uses; `--questions JOB` starts in Q mode."""
    from steward import launch

    argv = [sys.executable, "-m", "steward.cli", "phone", *(["--questions", questions_job] if questions_job else [])]
    return (launch._env_exports() + f"cd {shlex.quote(str(_STEWARD_ROOT))} && " + " ".join(shlex.quote(p) for p in argv))


def _herdr_json(runner: CommandRunner, argv: list[str]) -> dict | None:
    res = runner.run(["herdr", *argv], cwd=_STEWARD_ROOT)
    if res.exit_code != 0:
        return None
    try:
        return json.loads(res.stdout).get("result")
    except (ValueError, AttributeError):
        return None


def find_tab(runner: CommandRunner, workspace: str | None, label: str) -> dict | None:
    """`{"workspace", "tab", "pane"}` of the tab holding a pane labelled `label`, else None. Tabs are found by
    their panes, never by the tab's own label: the owner renames tabs (and Steward titles job tabs itself)."""
    panes = (_herdr_json(runner, ["pane", "list"]) or {}).get("panes") or []
    tabs = {t.get("tab_id"): t for t in (_herdr_json(runner, ["tab", "list"]) or {}).get("tabs") or []}
    for pane in panes:
        tab = tabs.get(pane.get("tab_id"))
        if pane.get("label") == label and tab and (workspace is None or tab.get("workspace_id") == workspace):
            return {"workspace": tab.get("workspace_id"), "tab": tab["tab_id"], "pane": pane["pane_id"]}
    return None


def find_phone_tab(runner: CommandRunner, workspace: str | None) -> dict | None:
    """`{"workspace", "tab", "pane"}` of the existing `📱 phone` tab (by its pane), else None."""
    return find_tab(runner, workspace, PHONE_LABEL)


def link_for(found: dict | None) -> str | None:
    return notify.deep_link(found["workspace"], found["tab"], found["pane"]) if found else None


def phone_link(runner: CommandRunner, store: Store | None = None, job_id: str | None = None) -> str | None:
    """The phone-tab deep link for an alert; falls back to the job's own tab link."""
    workspace = os.environ.get("HERDR_WORKSPACE_ID") or (store.kv_get(job_id, "herdr_workspace") if store and job_id else None)
    try:
        link = link_for(find_phone_tab(runner, workspace))
    except Exception:  # noqa: BLE001
        link = None
    if link or not (store and job_id):
        return link
    return notify.deep_link(store.kv_get(job_id, "herdr_workspace"), store.kv_get(job_id, "herdr_tab"),
                            store.kv_get(job_id, "herdr_pane"))


def _open_tab(runner: CommandRunner, workspace: str | None, label: str, command: str, dry_run: bool) -> tuple[int, str]:
    if not workspace:
        return 2, "error: no Herdr workspace ($HERDR_WORKSPACE_ID or --workspace)"
    create = ["herdr", "tab", "create", "--workspace", workspace, "--label", label,
              "--cwd", str(_STEWARD_ROOT), "--no-focus"]
    if dry_run:
        return 0, ("would focus an existing tab labelled " + label + ", else:\n  " + shlex.join(create)
                   + f"\n  herdr pane run <root pane> {shlex.quote(command)}\n"
                   f"link: moshi://herdr?workspace={workspace}&tab=<new tab>&pane=<new pane>")
    found = find_tab(runner, workspace, label)
    if found:
        runner.run(["herdr", "tab", "focus", found["tab"]], cwd=_STEWARD_ROOT)
        return 0, f"focused existing {label} tab\nlink: {link_for(found)}"
    res = runner.run(create, cwd=_STEWARD_ROOT)
    if res.exit_code != 0:
        return 1, f"error: herdr tab create failed: {res.stderr.strip()}"
    try:
        result = json.loads(res.stdout)["result"]
        tab, pane = result["tab"]["tab_id"], result["root_pane"]["pane_id"]
    except (ValueError, KeyError, TypeError) as exc:
        return 1, f"error: unexpected herdr tab create output: {exc}"
    runner.run(["herdr", "pane", "rename", pane, label], cwd=_STEWARD_ROOT)
    runner.run(["herdr", "pane", "run", pane, command], cwd=_STEWARD_ROOT)
    return 0, f"opened {label} tab {tab}\nlink: {notify.deep_link(workspace, tab, pane)}"


def open_phone_tab(runner: CommandRunner, workspace: str | None, *, dry_run: bool = False) -> tuple[int, str]:
    """Create (or focus) the phone tab; `(exit code, message)`. `--dry-run` touches no Herdr."""
    return _open_tab(runner, workspace, PHONE_LABEL, phone_command(), dry_run)


def questions_label(job_id: str) -> str:
    return f"? questions {job_id[:8]}"


def open_questions_tab(runner: CommandRunner, workspace: str | None, job_id: str, *,
                       dry_run: bool = False) -> tuple[int, str]:
    """Create (or focus) `? questions <job8>`: the phone component in Q mode for one job (the pane's `answer` button)."""
    return _open_tab(runner, workspace, questions_label(job_id), phone_command(job_id), dry_run)


def questions_pane_label(job_id: str) -> str:
    return f"questions {job_id[:8]}"


def open_questions_pane(runner: CommandRunner, workspace: str | None, job_id: str, *, store: Store | None = None,
                        dry_run: bool = False) -> tuple[int, str]:
    """Open (or focus) `questions <job8>` as a PANE in the job's own tab: the phone component in Q mode, split down off
    the right column (the `watchers` pane, else the `runner` pane, like `chat.open_chat`). Falls back to the old
    separate `? questions` tab when the job has no tab of its own (e.g. `steward run --pane`)."""
    label = questions_pane_label(job_id)
    job_tab = store.kv_get(job_id, "herdr_tab") if store is not None else None
    if not job_tab:
        return open_questions_tab(runner, workspace, job_id, dry_run=dry_run)
    if dry_run:
        return 0, (f"would focus an existing pane labelled {label}, else split one down off the watchers pane of tab {job_tab}:\n"
                   f"  herdr pane run <new pane> {shlex.quote(phone_command(job_id))}")
    panes = (_herdr_json(runner, ["pane", "list"]) or {}).get("panes") or []
    existing = next((p for p in panes if p.get("label") == label), None)
    if existing:
        runner.run(["herdr", "tab", "focus", existing["tab_id"]], cwd=_STEWARD_ROOT)
        return 0, f"focused existing {label} pane\nlink: {notify.deep_link(workspace, existing['tab_id'], existing['pane_id'])}"
    id8 = job_id[:8]
    in_tab = [p for p in panes if p.get("tab_id") == job_tab]
    anchor = next((p for p in in_tab if p.get("label") == f"watchers {id8}"), None) \
        or next((p for p in in_tab if p.get("label") == f"runner {id8}"), None)
    if anchor is None:
        return open_questions_tab(runner, workspace, job_id)
    direction = "down" if anchor.get("label", "").startswith("watchers") else "right"
    split = ["herdr", "pane", "split", "--pane", anchor["pane_id"], "--direction", direction,
             "--cwd", str(_STEWARD_ROOT), "--focus"]
    res = runner.run(split, cwd=_STEWARD_ROOT)
    try:
        new_pane = json.loads(res.stdout)["result"]["pane"]["pane_id"]
    except (ValueError, KeyError, TypeError):
        return 1, f"error: herdr pane split failed: {getattr(res, 'stderr', '').strip()}"
    runner.run(["herdr", "pane", "rename", new_pane, label], cwd=_STEWARD_ROOT)
    runner.run(["herdr", "pane", "run", new_pane, phone_command(job_id)], cwd=_STEWARD_ROOT)
    runner.run(["herdr", "tab", "focus", job_tab], cwd=_STEWARD_ROOT)
    return 0, f"opened {label} pane {new_pane} in tab {job_tab}\nlink: {notify.deep_link(workspace, job_tab, new_pane)}"


def link_message(runner: CommandRunner, workspace: str | None) -> tuple[int, str]:
    found = find_phone_tab(runner, workspace)
    return (0, link_for(found)) if found else (1, f"no {PHONE_LABEL} tab (run: steward phone --open)")
