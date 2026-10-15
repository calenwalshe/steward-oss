"""The completed-jobs tab: `steward done-log`, a resident terminal UI listing every finished job, newest first.

Same resident-app pattern as steward.dashapp: one process owns the terminal (alternate screen, SGR mouse, no echo),
a refresh thread keeps a cache, every paint is built from the cache so keys and taps are instant. Read-only on job
state: the only side effects are the `[ export ] [ explain ] [ transcript ]` links, which run existing CLI verbs.

Layers (each testable alone):
  * `collect` / `ordered` / `day_label` / `apply_filters` / `totals`: pure data over a Store;
  * `load_detail`: one job's unfolded detail, built lazily and cached;
  * `App`: state (cursor, folds, filters, overlay), `frame()` (pure render), `feed()` (keys + taps), `run()`.
"""

from __future__ import annotations

import os
import queue
import re
import select
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from steward import pkgenv, tabnames
from steward.models import JobState

TAB_LABEL = "✓ done"  # the pane label the tab is found by (like `📱 phone`); also the tab's own title
DONE_STATES = frozenset({"ACCEPTED", "RELEASED", "STOPPED", "FAILED"})
MIN_POLL_S = 5.0  # the store is asked "did anything change?" at most this often
FINAL_LINES = 6
OUTCOMES = ("all", "merged", "accepted", "released", "stopped", "failed")
HIDDEN_BY_DEFAULT_OFF = frozenset({"released", "stopped"})  # what `a` hides

ENTER = "\033[?1049h\033[?1000h\033[?1006h\033[?25l"
LEAVE = "\033[?1000l\033[?1006l\033[?25h\033[?1049l"
_SGR = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
_CSI = re.compile(rb"\x1b\[[0-9;<?]*[A-Za-z~]")
DIM, BOLD, GREEN, RED, REV, OFF = "\033[2m", "\033[1m", "\033[32m", "\033[31m", "\033[7m", "\033[0m"


# ---- data ----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    job_id: str
    name: str
    goal: str
    repo: str
    state: str
    outcome: str  # merged | accepted | released | stopped | failed
    finished_at: datetime  # tz-aware
    created_at: datetime
    commit: str | None
    usd: float | None = None

    @property
    def duration_s(self) -> float:
        return max((self.finished_at - self.created_at).total_seconds(), 0.0)

    @property
    def repo_name(self) -> str:
        return Path(self.repo.rstrip("/")).name or self.repo


def _ts(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def outcome_of(state: str, merged: bool) -> str:
    return "merged" if state == "ACCEPTED" and merged else state.lower()


def glyph(outcome: str) -> str:
    return "✗" if outcome in ("stopped", "failed") else "✓"


def collect(store, usd_by_job: dict[str, float] | None = None) -> list[Entry]:
    """Every finished job as an `Entry`, newest finish first. Finish time = the last state change, or the merge if later."""
    usd_by_job = usd_by_job or {}
    out = []
    for job in store.list_jobs({JobState(s) for s in DONE_STATES}):
        commit = store.kv_get(job.job_id, "incorporated_commit") or None
        stamps = [_ts(job.updated_at)]
        if commit:
            stamps += [_ts(e.ts) for e in store.events(job.job_id) if e.what == "incorporated"]
        out.append(Entry(job.job_id, tabnames.job_name(store, job, persist=False), job.goal, job.repo, job.state.value,
                         outcome_of(job.state.value, bool(commit)), max(stamps), _ts(job.created_at), commit,
                         usd_by_job.get(job.job_id)))
    return ordered(out)


def ordered(entries: list[Entry]) -> list[Entry]:
    return sorted(entries, key=lambda e: (e.finished_at, e.job_id), reverse=True)


def day_label(dt: datetime, now: datetime | None = None) -> str:
    """`Today`, `Yesterday`, else `Mon 28 Sep` (the year is added for another year)."""
    now = now or datetime.now(timezone.utc)
    d, today = _local(dt).date(), _local(now).date()
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    return d.strftime("%a %d %b" + ("" if d.year == today.year else " %Y"))


@dataclass(frozen=True)
class Filters:
    query: str = ""
    repo: str | None = None
    outcome: str = "all"
    show_all: bool = True  # False hides released/stopped

    def describe(self) -> str:
        parts = []
        if self.query:
            parts.append(f"/{self.query}")
        if self.repo:
            parts.append(f"repo={self.repo}")
        if self.outcome != "all":
            parts.append(f"outcome={self.outcome}")
        if not self.show_all:
            parts.append("hiding released/stopped")
        return " · ".join(parts)


def apply_filters(entries: list[Entry], f: Filters) -> list[Entry]:
    q = f.query.lower()
    out = []
    for e in entries:
        if q and q not in e.name.lower() and q not in e.goal.lower():
            continue
        if f.repo and e.repo_name != f.repo:
            continue
        if f.outcome != "all" and e.outcome != f.outcome:
            continue
        if not f.show_all and e.outcome in HIDDEN_BY_DEFAULT_OFF:
            continue
        out.append(e)
    return out


def totals(entries: list[Entry]) -> dict:
    return {"jobs": len(entries), "merged": sum(e.outcome == "merged" for e in entries),
            "usd": sum(e.usd or 0.0 for e in entries), "hours": sum(e.duration_s for e in entries) / 3600}


def cycle(values: list, current):
    """The value after `current` in `values` (wrapping); the first when `current` is not in it."""
    return values[(values.index(current) + 1) % len(values)] if current in values else values[0]


def human_duration(secs: float) -> str:
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{secs % 3600 // 60:02d}m"


def usd_text(usd: float | None) -> str:
    return "-" if usd is None else f"${usd:.2f}"


# ---- unfolded detail -------------------------------------------------------------------------------------------------


def load_detail(store, entry: Entry, runner=None, projects_dir: Path | None = None) -> list[str]:
    """The plain-text lines of one job's unfolded detail. Every section degrades to `(unavailable)` on its own."""
    from steward import evidence, export, questions, tokens, transcript
    from steward.api import StewardAPI
    from steward.diagnosis import redact

    job = store.get_job(entry.job_id)
    lines: list[str] = []

    def section(title: str, body: list[str] | str) -> None:
        lines.append(f"{title}:")
        body = body.splitlines() if isinstance(body, str) else body
        lines.extend("  " + b for b in body or ["(none)"])

    def safe(title: str, fn) -> None:
        try:
            section(title, fn())
        except Exception:  # noqa: BLE001 - one unreadable section never blanks the rest
            section(title, "(unavailable)")

    goal, finish = evidence.split_goal(redact(job.goal))
    section("goal", _wrap(goal, 100))

    def finish_line():
        items = StewardAPI(store)._finish_line_items(entry.job_id, store.events(entry.job_id))
        return [f"{'✓' if i['met'] else '✗' if i['met'] is False else '·'} {i['text']}" for i in items]

    safe("finish line", finish_line if finish else (lambda: []))

    def final():
        text = export._final_message(transcript.worker_file(store, entry.job_id, projects_dir))
        return [ln for ln in text.splitlines() if ln.strip()][:FINAL_LINES]

    safe("worker's final message", final)
    git = {}

    def gitpart():
        if not git:
            if runner is None:
                from steward.runner_exec import SubprocessRunner
                git["v"] = export._git_section(store, job, SubprocessRunner())
            else:
                git["v"] = export._git_section(store, job, runner)
        return git["v"]

    safe("commits", lambda: gitpart()[1].splitlines())

    def files():
        stat = gitpart()[0].splitlines()
        return stat[:-1][:40] if stat else []

    safe("files changed", files)

    def qs():
        out = []
        for r in questions.ordered(questions.load(store, entry.job_id)):
            if r.get("status") not in ("answered", "resolved") and not r.get("answer"):
                continue
            ans = r.get("answer")
            ans = (ans.get("text") or ans.get("choice") or str(ans)) if isinstance(ans, dict) else ans
            out.append(f"{r.get('title') or r.get('qid')}: {ans}")
        return out

    safe("questions answered", qs)

    def cost():
        tj = tokens.job_tokens(store, entry.job_id, projects_dir=projects_dir)
        rows = [f"{src}: {tokens.humanize(int(s.get('all', 0)))} tok · ${float(s.get('usd', 0) or 0):.2f}"
                for src, s in sorted((tj.get("sources") or {}).items()) if s.get("all") or s.get("usd")]
        return rows + [f"total: ${float((tj.get('total') or {}).get('usd', 0) or 0):.2f}"]

    safe("cost", cost)
    return [redact(ln) for ln in lines]


def _wrap(text: str, width: int) -> list[str]:
    out, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    return out + ([cur] if cur else [])


def run_command(argv: list[str]) -> tuple[int, str]:
    """Run one steward command; (exit code, full output)."""
    try:
        res = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError as exc:
        return 127, f"{argv[0]}: {exc.strerror or exc}"
    return res.returncode, res.stdout.strip() or "done"


LINKS = (("export", "export"), ("explain", "explain-state"), ("transcript", "transcript"))


def link_argv(verb: str, job_id: str) -> list[str]:
    extra = ["--last", "40", "--no-color"] if verb == "transcript" else []
    return [*pkgenv.cli_argv(), verb, job_id, *extra]


# ---- the app ---------------------------------------------------------------------------------------------------------


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[: max(n - 1, 0)] + "…"


class App:
    def __init__(self, in_fd=0, out_fd=1, *, store_factory=None, usd_fn=None, detail_fn=None, run=run_command,
                 cols=None, rows=None, now_fn=None, poll_s=MIN_POLL_S):
        self.in_fd, self.out_fd = in_fd, out_fd
        self.store_factory = store_factory or _default_store
        self.usd_fn = usd_fn or _default_usd
        self.detail_fn = detail_fn or load_detail
        self.run_cmd = run
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.poll_s = max(poll_s, MIN_POLL_S) if poll_s is not None else MIN_POLL_S
        self._fixed = (cols, rows) if cols else None
        self.cols, self.rows = self._size()
        self.entries: list[Entry] = []
        self.usd: dict[str, float] = {}
        self.filters = Filters()
        self.cursor = 0           # index into visible()
        self.top = 0              # first body line drawn
        self.open: set[str] = set()
        self.details: dict[str, list[str]] = {}
        self.searching = False
        self.overlay: list[str] | None = None
        self.overlay_top = 0
        self.note = ""
        self.buf = b""
        self.map: dict[int, tuple] = {}  # screen row -> ("row", idx) | ("link", [(c0, c1, verb)], job_id)
        self.drawn: list[str] = []
        self.events: queue.Queue = queue.Queue()
        self.wake_r, self.wake_w = os.pipe()
        self.stop = threading.Event()
        self.kick = threading.Event()
        self.version = None
        self.loads = 0
        self.paints = 0
        self.lock = threading.Lock()
        self._store = None

    # -- data ------------------------------------------------------------------------------------------------------
    def _size(self):
        if self._fixed:
            return self._fixed
        try:
            sz = os.get_terminal_size(self.out_fd)
            if sz.columns and sz.lines:  # a fresh pty reports 0x0 until the first resize
                return sz.columns, sz.lines
        except OSError:
            pass
        return 100, 30

    def store(self):
        if self._store is None:
            self._store = self.store_factory()
        return self._store

    def data_version(self) -> int | None:
        try:
            return self.store()._conn.execute("PRAGMA data_version").fetchone()[0]
        except Exception:  # noqa: BLE001
            return None

    def load(self) -> None:
        """Re-read the finished jobs (and the cost of any not yet priced) into the cache."""
        store = self.store()
        entries = collect(store, self.usd)
        missing = [e.job_id for e in entries if e.job_id not in self.usd]
        if missing:
            try:
                self.usd.update(self.usd_fn(store, missing))
            except Exception:  # noqa: BLE001 - cost is decoration
                pass
            entries = [replace(e, usd=self.usd.get(e.job_id)) for e in entries]
        with self.lock:
            keep = self.visible()[self.cursor].job_id if self.visible() and self.cursor < len(self.visible()) else None
            self.entries = entries
            vis = self.visible()
            self.cursor = next((i for i, e in enumerate(vis) if e.job_id == keep), min(self.cursor, max(len(vis) - 1, 0)))
        self.loads += 1
        self.version = self.data_version()

    def refresh_loop(self) -> None:
        while not self.stop.is_set():
            if self.kick.wait(self.poll_s) or self.data_version() != self.version:
                self.kick.clear()
                try:
                    self.load()
                except Exception:  # noqa: BLE001 - a bad pass never kills the refresher
                    pass
                self.post(self.paint)

    def post(self, fn) -> None:
        self.events.put(fn)
        try:
            os.write(self.wake_w, b"x")
        except OSError:
            pass

    def visible(self) -> list[Entry]:
        return apply_filters(self.entries, self.filters)

    def repos(self) -> list[str]:
        return sorted({e.repo_name for e in self.entries})

    # -- render ----------------------------------------------------------------------------------------------------
    def header(self, vis: list[Entry]) -> list[str]:
        t = totals(vis)
        l1 = (f"{BOLD}✓ DONE{OFF}  {t['jobs']} jobs · {t['merged']} merged · ${t['usd']:.2f} · {t['hours']:.1f}h")
        if self.searching:
            l2 = f"search: {self.filters.query}█  {DIM}(Enter keep · Esc clear){OFF}"
        else:
            l2 = DIM + (self.filters.describe() or "no filters") + OFF
        return [l1, l2]

    def body(self, vis: list[Entry]) -> tuple[list[str], list[tuple], dict[int, int]]:
        """`(lines, per-line map, entry index -> first line)`."""
        lines, maps, first = [], [], {}
        now, last_day, w = self.now_fn(), None, self.cols
        namew = max(8, min(22, w // 4))
        repow = max(6, min(14, w // 7))
        for i, e in enumerate(vis):
            day = day_label(e.finished_at, now)
            if day != last_day:
                lines.append(f"{DIM}── {day} " + "─" * max(w - len(day) - 4, 0) + OFF)
                maps.append(("sep",))
                last_day = day
            fin = e.finished_at.astimezone()
            col = GREEN if glyph(e.outcome) == "✓" else RED
            marker = "▶" if i == self.cursor else " "
            fold = "▾" if e.job_id in self.open else " "
            row = (f"{marker}{fold}{fin:%m-%d %H:%M} {col}{glyph(e.outcome)}{OFF} {_cut(e.name, namew):<{namew}} "
                   f"{_cut(e.repo_name, repow):<{repow}} {human_duration(e.duration_s):>6} {usd_text(e.usd):>7} "
                   f"{e.commit[:7] if e.commit else e.outcome}")
            if i == self.cursor:
                row = REV + row + OFF
            first[i] = len(lines)
            lines.append(row)
            maps.append(("row", i))
            if e.job_id in self.open:
                for ln in self.details.get(e.job_id) or ["  loading…"]:
                    lines.append(DIM + "    " + _cut(ln, w - 5) + OFF)
                    maps.append(("detail", i))
                spans, text, c = [], "    ", 4
                for _, (label, _verb) in enumerate(LINKS):
                    chunk = f"[ {label} ]"
                    spans.append((c, c + len(chunk), label))
                    text += chunk + "  "
                    c += len(chunk) + 2
                lines.append(BOLD + text.rstrip() + OFF)
                maps.append(("link", spans, e.job_id, i))
        return lines, maps, first

    def frame(self) -> list[str]:
        if self.overlay is not None:
            h = self.rows - 2
            lines = self.overlay[self.overlay_top: self.overlay_top + h]
            self.map = {}
            return [f"{BOLD}output{OFF}  {DIM}Esc/q close · j/k scroll{OFF}", *(_cut(ln, self.cols) for ln in lines)]
        with self.lock:
            vis = self.visible()
            self.cursor = min(self.cursor, max(len(vis) - 1, 0))
            head = self.header(vis)
            lines, maps, first = self.body(vis)
        room = max(self.rows - len(head) - 1, 1)
        if vis:
            start = first[self.cursor]
            end = start + 1
            while end < len(lines) and maps[end][0] in ("detail", "link"):
                end += 1
            if start < self.top:
                self.top = start - (1 if start and maps[start - 1][0] == "sep" else 0)
            elif end > self.top + room:
                self.top = min(end - room, start)
        self.top = max(0, min(self.top, max(len(lines) - room, 0)))
        shown = lines[self.top: self.top + room]
        self.map = {}
        for r, m in enumerate(maps[self.top: self.top + room], start=len(head) + 1):
            self.map[r] = m
        if not vis:
            shown = [f"{DIM}no finished jobs match{OFF}"]
        foot = self.note or f"{DIM}j/k move · Enter open · / search · r repo · o outcome · a released/stopped · g/G · q{OFF}"
        return [*head, *(_cut_ansi(ln, self.cols) for ln in shown), foot]

    def paint(self) -> None:
        rows = self.frame()
        self.paints += 1
        buf = []
        if not self.drawn:
            buf.append("\033[2J")
        buf.extend(f"\033[{i + 1};1H\033[K" for i in range(len(rows), len(self.drawn)))
        for i, ln in enumerate(rows):
            if i >= len(self.drawn) or self.drawn[i] != ln:
                buf.append(f"\033[{i + 1};1H{ln}{OFF}\033[K")
        if buf:
            self.write("".join(buf))
        self.drawn = rows

    def write(self, text: str) -> None:
        data = text.encode()
        while data:
            data = data[os.write(self.out_fd, data):]

    # -- input -----------------------------------------------------------------------------------------------------
    def feed(self, data: bytes) -> None:
        self.buf += data
        while self.buf:
            if self.buf[:1] == b"\x1b":
                m = _SGR.match(self.buf)
                if m:
                    self.buf = self.buf[m.end():]
                    if m.group(1) == b"0" and m.group(4) == b"M":
                        self.click(int(m.group(2)), int(m.group(3)))
                    continue
                m = _CSI.match(self.buf)
                if m:
                    self.buf = self.buf[m.end():]
                    self.key({"A": "up", "B": "down", "5~": "pgup", "6~": "pgdn", "H": "g", "F": "G"}.get(m.group(0)[2:].decode()))
                    continue
                if len(self.buf) == 1:
                    self.buf = b""
                    self.key("esc")
                    continue
                if self.buf[1:2] == b"[" and len(self.buf) < 32:
                    return
                self.buf = self.buf[1:]
                continue
            ch = self.buf[:1]
            self.buf = self.buf[1:]
            self.key({b"\r": "enter", b"\n": "enter", b"\x7f": "bs", b"\x08": "bs"}.get(ch, ch.decode("latin-1")))
        self.paint()

    def key(self, k: str | None) -> None:
        if k is None:
            return
        if self.overlay is not None:
            h = max(self.rows - 2, 1)
            if k in ("esc", "q"):
                self.overlay = None
            elif k in ("j", "down"):
                self.overlay_top = min(self.overlay_top + 1, max(len(self.overlay) - h, 0))
            elif k in ("k", "up"):
                self.overlay_top = max(self.overlay_top - 1, 0)
            elif k == "pgdn":
                self.overlay_top = min(self.overlay_top + h, max(len(self.overlay) - h, 0))
            elif k == "pgup":
                self.overlay_top = max(self.overlay_top - h, 0)
            return
        if self.searching:
            if k == "esc":
                self.searching = False
                self.filters = replace(self.filters, query="")
            elif k == "enter":
                self.searching = False
            elif k == "bs":
                self.filters = replace(self.filters, query=self.filters.query[:-1])
            elif len(k) == 1 and k.isprintable():
                self.filters = replace(self.filters, query=self.filters.query + k)
            self.cursor, self.top = 0, 0
            return
        self.note = ""
        n, page = len(self.visible()), max(self.rows - 4, 1)
        if k in ("j", "down"):
            self.cursor = min(self.cursor + 1, max(n - 1, 0))
        elif k in ("k", "up"):
            self.cursor = max(self.cursor - 1, 0)
        elif k == "pgdn":
            self.cursor = min(self.cursor + page, max(n - 1, 0))
        elif k == "pgup":
            self.cursor = max(self.cursor - page, 0)
        elif k == "g":
            self.cursor = 0
        elif k == "G":
            self.cursor = max(n - 1, 0)
        elif k in ("enter", " "):
            self.toggle(self.cursor)
        elif k == "/":
            self.searching = True
        elif k == "r":
            self.filters = replace(self.filters, repo=cycle([None, *self.repos()], self.filters.repo))
            self.cursor = self.top = 0
        elif k == "o":
            self.filters = replace(self.filters, outcome=cycle(list(OUTCOMES), self.filters.outcome))
            self.cursor = self.top = 0
        elif k == "a":
            self.filters = replace(self.filters, show_all=not self.filters.show_all)
            self.cursor = self.top = 0
        elif k == "q":
            self.stop.set()

    def toggle(self, idx: int) -> None:
        vis = self.visible()
        if idx >= len(vis):
            return
        jid = vis[idx].job_id
        self.cursor = idx
        if jid in self.open:
            self.open.discard(jid)
            return
        self.open.add(jid)
        if jid not in self.details:  # built off the UI thread; the row says `loading…` until it lands
            entry = vis[idx]

            def work():
                try:
                    lines = self.detail_fn(self.store_factory(), entry)
                except Exception:  # noqa: BLE001
                    lines = ["(detail unavailable)"]
                self.details[jid] = lines
                self.post(self.paint)

            threading.Thread(target=work, daemon=True).start()

    def click(self, col: int, row: int) -> None:
        m = self.map.get(row)
        if not m:
            return
        if m[0] == "link":
            for c0, c1, label in m[1]:
                if c0 < col <= c1:
                    return self.link(label, m[2])
            return
        if m[0] in ("row", "detail"):
            idx = m[1]
            if m[0] == "row":
                self.toggle(idx)
            else:
                self.cursor = idx

    def link(self, label: str, job_id: str) -> None:
        verb = dict(LINKS)[label]
        self.note = f"… {label} {job_id[:8]}"
        self.paint()

        def work():
            rc, out = self.run_cmd(link_argv(verb, job_id))

            def land():
                if label == "export":
                    self.note = "→ " + (out.splitlines()[-1] if out else "done")
                else:
                    self.overlay, self.overlay_top, self.note = out.splitlines() or ["(no output)"], 0, ""
                self.paint()

            self.post(land)

        threading.Thread(target=work, daemon=True).start()

    # -- terminal --------------------------------------------------------------------------------------------------
    def setup_terminal(self):
        self.saved_tty = None
        if os.isatty(self.in_fd):
            import termios

            self.saved_tty = termios.tcgetattr(self.in_fd)
            new = termios.tcgetattr(self.in_fd)
            new[3] &= ~(termios.ECHO | termios.ICANON)
            new[6][termios.VMIN], new[6][termios.VTIME] = 1, 0
            termios.tcsetattr(self.in_fd, termios.TCSADRAIN, new)
        self.write(ENTER)
        while select.select([self.in_fd], [], [], 0.1)[0]:
            if not os.read(self.in_fd, 4096):
                break

    def restore_terminal(self):
        try:
            self.write(LEAVE)
        finally:
            if self.saved_tty is not None:
                import termios

                termios.tcsetattr(self.in_fd, termios.TCSADRAIN, self.saved_tty)

    def run(self) -> int:
        self.setup_terminal()
        try:
            self.load()
            t = threading.Thread(target=self.refresh_loop, daemon=True)
            t.start()
            self.paint()
            while not self.stop.is_set():
                ready = select.select([self.in_fd, self.wake_r], [], [], 1.0)[0]
                if self.wake_r in ready:
                    os.read(self.wake_r, 4096)
                if self.in_fd in ready:
                    data = os.read(self.in_fd, 4096)
                    if not data:
                        break
                    self.feed(data)
                while True:
                    try:
                        self.events.get_nowait()()
                    except queue.Empty:
                        break
                size = self._size()
                if size != (self.cols, self.rows):
                    self.cols, self.rows = size
                    self.drawn = []
                    self.paint()
        finally:
            self.stop.set()
            self.restore_terminal()
        return 0


def _local(now: datetime) -> datetime:
    return now.astimezone() if now.tzinfo else now


_ANSI = re.compile(r"\033\[[0-9;]*m")


def _cut_ansi(text: str, n: int) -> str:
    """Cut to `n` visible cells, keeping escape sequences intact."""
    out, vis, i = [], 0, 0
    while i < len(text):
        m = _ANSI.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        if vis >= n:
            break
        out.append(text[i])
        vis += 1
        i += 1
    return "".join(out)


def _default_store():
    from steward.store import Store

    return Store()


def _default_usd(store, ids: list[str]) -> dict[str, float]:
    from steward import tokens

    jobs = tokens.all_tokens(store, ids)["jobs"]
    return {j: float((t.get("total") or {}).get("usd") or 0.0) for j, t in jobs.items()}


# ---- the pinned tab --------------------------------------------------------------------------------------------------


def done_command() -> str:
    return shlex.join([*pkgenv.cli_argv(), "done-log"])


def open_done_tab(runner, workspace: str | None, *, dry_run: bool = False) -> tuple[int, str]:
    """Create (or focus) the `✓ done` tab, found by its pane label like the phone tab. Idempotent."""
    from steward import phone

    return phone._open_tab(runner, workspace, TAB_LABEL, done_command(), dry_run)


def find_done_tab(runner, workspace: str | None) -> dict | None:
    from steward import phone

    return phone.find_tab(runner, workspace, TAB_LABEL)


def main() -> int:
    app = App()

    def bye(*_):
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, bye)
    signal.signal(signal.SIGWINCH, lambda *_: app.post(lambda: None))
    try:
        return app.run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
