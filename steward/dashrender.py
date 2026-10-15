"""The Steward status pane's renderer, as a module: `Pane(...).render()` -> `(lines, click_map)`.

It used to be a script run once per redraw (`deploy/cockpit/steward-dash-jobs.py`, now a thin wrapper around this).
The resident pane (`steward.dashapp`) keeps one `Pane` alive: every read (`steward <verb> --json`, store queries) goes
through `Pane.src(key, ttl, fn)`, a cache. A background pass (`refresh=True`) refetches whatever is older than its ttl;
a tap's repaint (`refresh=False`) only reads the cache, so fold/expand paint without touching the store.

Click map lines (the same grammar as before):
  key <n> <tab>            number-key jump to a job's tab
  row <idx> focus <tab>    clicking printed line <idx> (0-based) focuses <tab>  (the renderer no longer emits row verbs:
                           a stray tap on a header, goal or token line must never navigate; the app still honours them)
  more <idx> <key>         printed line <idx> is (part of) a text cut with "…": a tap expands / collapses it in full
  num <idx> <c0> <c1> <tab>  the "[n]" cell of line <idx>: a DOUBLE tap focuses <tab> (a single tap folds, see btn fold)
  expand <idx> <job>       clicking printed line <idx> (a job's yellow question) toggles its evidence panel
  btn <idx> <col_start> <col_end> <action> <job> <approval id|->   a click on line <idx>, columns col_start..col_end
  chatbtn <idx> <col_start> <col_end> <job> <role>   a supervisor row's [ chat ] button (the only way a supervisor chat opens)
  toggle <idx> <col_start> <col_end> <job> <role> <next>   a click on line <idx> switches that supervisor
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from steward import alerts, dashact, evidence, herdr_tabs, incorporate, jobview
from steward import export as export_module
from steward import health as health_module
from steward import tabnames
from steward import stability
from steward.jobview import LIVE, QUIET_AFTER, SUPERVISED, ago  # noqa: F401  (shared with `steward phone`)
from steward.store import Store

ANSI = re.compile(r"\033\[[0-9;]*m")
vlen, fit = jobview.vlen, jobview.fit  # ANSI-aware width / never-mid-word ellipsis: used for every line
STAGED = ("CREATED", "CONTRACTED")  # created but never launched
STALE_S = 24 * 3600  # a staged job older than this gets a dim "stale" tag
PLAY_BUTTON = "[ ▶ play ]"
DONE = ("ACCEPTED", "RELEASED", "STOPPED", "FAILED")  # finished: collapsed into one "done" line at the bottom
RECENT_DAYS_ENV = "STEWARD_DASH_RECENT_DAYS"  # how far back the done group reaches (default 7 days)
RECENT_DAYS = 7.0


def recent_days():
    try:
        return max(float(os.environ.get(RECENT_DAYS_ENV, RECENT_DAYS)), 0.0)
    except ValueError:
        return RECENT_DAYS
STEER_BUTTON = "[ steer ]"  # opens the quick-steer chat (`steward steer-chat JOB`)
EXPLAIN_BUTTON = "[ explain ]"  # toggles the explanation panel; rides the header row when it fits
UNBLOCK_STATES = ("RUNNING", "PAUSED", "NEEDS_HUMAN")  # [ unblock now ] works on these
# ONE clickable style, derived from the click map (`style_clickable`) so it can never drift from what taps do:
# buttons, [n] fold cells and supervisor switches (cmap btn/toggle spans) are bold bright cyan. Tappable text lines
# (cmap row/expand lines) get no styling at all, and nothing else on the pane may use bold cyan or underline.
CLICK_BUTTON = "\033[1;96m"
CLICK_BUTTON_OFF = "\033[22;39m"
_SGR = re.compile(r"\033\[([0-9;]*)m")
GROUP_PREFIX = "grp:"  # dashact expand key for a queue group (flips its default fold)
MORE_PREFIX = "more:"  # dashact expand key prefix of a text opened in full ("tap to expand" on a "…" line)
CHAT_BUTTON = "[ chat ]"  # a supervisor row's chat button (the only way a supervisor chat opens)
DONE_KEY = "done:"  # dashact expand key for the done group
FIXED_TTL = {"tokens": 10.0, "slow": 5.0}  # everything else refreshes every pass (~1 s)

_cli_lock = threading.Lock()  # redirect_stdout is process-global: one in-process CLI call at a time


def cli(*args):
    """A read-only `steward <args> --json`, run in-process: a subprocess per call (a fresh Python each) made every
    redraw take ~1.7 s. Falls back to a subprocess if the in-process call raises."""
    buf = io.StringIO()
    with _cli_lock:
        try:
            from steward import cli as steward_cli

            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                try:
                    steward_cli.main(list(args))
                except SystemExit:
                    pass
            out = buf.getvalue()
        except Exception:  # noqa: BLE001 - never break the pane; the old path still works
            out = subprocess.run([sys.executable, "-m", "steward.cli", *args], capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except ValueError:
        return None


def hum(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e4 else f"{n / 1e3:.1f}k" if n >= 1e3 else str(n)


def split(b):
    """cached = re-read from Claude's prompt cache (cheap); fresh = new input + cache writes."""
    return (f"cached {hum(b['cache_read'])} · fresh {hum(b['input'] + b['cache_creation'])} "
            f"· out {hum(b['output'])}")


def switch_span(text, switch):
    """1-based inclusive visible columns of `[switch]` in the ANSI-stripped line, or None."""
    stripped = ANSI.sub("", text).replace("{SPIN}", "x")
    i = stripped.find(f"[{switch}]") if switch else -1
    return (i + 1, i + len(switch) + 2) if i >= 0 else None


def _label(label):
    """A tree label in 8 cells: longer ones become their first 7 characters plus an ellipsis."""
    return label if len(label) <= 8 else label[:7] + "…"


def _plural(n, one, many):
    return f"{n} {one if n == 1 else many}"


def _short(text, width):
    return text if len(text) <= width else text[: max(width - 1, 1)] + "…"


def staged_age(j, now):
    """Seconds since the job was created, or None when unreadable."""
    try:
        t = datetime.fromisoformat(j.get("created_at") or "")
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return now - t.timestamp()


def wrap_full(text, width):
    """`text` wrapped at word boundaries to `width` cells over as many lines as it needs (a word longer than the room is
    hard-cut). Never ellipsized: this is the expanded view of a cut line."""
    text = " ".join((text or "").split()).replace("{SPIN}", "\u2800")
    out, cur = [], ""
    for w in text.split(" "):
        while len(w) > width:
            if cur:
                out.append(cur)
                cur = ""
            out.append(w[:width])
            w = w[width:]
        if cur and len(cur) + 1 + len(w) > width:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    out.append(cur)
    return [ln.replace("\u2800", "{SPIN}") for ln in out if ln] or [""]


SERIES_WORDS = {"job", "step", "part", "task", "phase"}
SERIES_ID = re.compile(r"^[A-Za-z]{1,3}\d{1,3}$")  # M6, N1, G1
SERIES_NUM = re.compile(r"^\d{1,3}$")  # the 2 of "Job 2" (only right after a series word)
BURST_S = 6 * 3600  # jobs of one series are created within hours of each other


def _series_key(title):
    """`("V3", "M6")` for "V3 M6 recipe pages", `("Recipe book", "2")` for "Recipe book Job 2": the shared prefix and the
    job's own id, or None when the title is not part of a numbered series."""
    words = title.split()
    for k in range(1, min(len(words), 6)):
        w = words[k].strip(".:,;-")
        if SERIES_ID.match(w) or (SERIES_NUM.match(w) and words[k - 1].lower().strip(".:,;-") in SERIES_WORDS):
            head = words[:k]
            while head and head[-1].lower().strip(".:,;-") in SERIES_WORDS:
                head.pop()
            return (" ".join(head), w) if head else None
    return None


def implicit_groups(jobs):
    """Jobs one session created in a burst with a shared title prefix ("V3 M6 ...", "V3 M7 ...", "Recipe book Job 2")
    form a group even with no explicit queue. Stamps a `group` dict (same shape as sequencing's, plus `implicit`) on each
    member of an unfinished series of 2+ and returns the groups. Members keep creation order."""
    cand = [j for j in jobs if j.get("state") not in DONE and not j.get("group")]
    cand.sort(key=lambda j: j.get("created_at") or "")
    series = {}
    for j in cand:
        for title in ((j.get("goal") or "").strip().splitlines()[0] if j.get("goal") else "", j.get("name") or ""):
            key = _series_key(" ".join(title.split()))
            if key:
                series.setdefault(key[0].lower(), []).append((key[0], key[1], j))
                break
    out = []
    for _, items in series.items():
        run = []
        runs = [run]
        for it in items:  # split into bursts
            if run and _gap(run[-1][2], it[2]) > BURST_S:
                run = []
                runs.append(run)
            run.append(it)
        for run in runs:
            if len(run) < 2 or len({i for _, i, _ in run}) < len(run):
                continue
            ms = [j for _, _, j in run]
            live = next((m for m in ms if m.get("state") in ("RUNNING", "NEEDS_HUMAN", "PAUSED")), ms[0])
            g = {"root": "imp:" + ms[0].get("job_id", ""), "name": run[0][0], "members": [m.get("job_id", "") for m in ms],
                 "size": len(ms), "done": 0, "now": live.get("job_id", ""), "implicit": True,
                 "ids": {m.get("job_id", ""): i for (_, i, _), m in zip(run, ms)}}
            for m in ms:
                m["group"] = g
            out.append(g)
    return out


def _gap(a, b):
    try:
        ta, tb = (datetime.fromisoformat((x.get("created_at") or "").replace("Z", "+00:00")) for x in (a, b))
        return abs((tb - ta).total_seconds())
    except ValueError:
        return 0.0


def tab_of(j):
    return parse_qs(urlparse(j.get("deep_link") or "").query).get("tab", [""])[0]


def tokens_cell(src):
    """`<total>/<1h> $usd` in at most 16 cells (the 1h part goes first when it will not fit), else `-`."""
    if not (src and src.get("all")):
        return "-"
    usd = f"${src.get('usd', 0):.2f}"
    full = f"{hum(src['all'])}/{hum(src['hour'])} {usd}"
    return full if len(full) <= 16 else f"{hum(src['all'])} {usd}"


class Miss(BaseException):
    """A passive render needed data that is not cached yet (BaseException: `safe()` must not swallow it)."""


class Cache:
    """Fetched data shared by the panes of one process: key -> (fetched_at, ok, value-or-exception)."""

    def __init__(self):
        self.data: dict = {}
        self.lock = threading.Lock()
        self.fetches = 0  # real fetches run (tests assert that a tap's repaint does none)
        self.version = 0  # bumped whenever a fetch changed a value: the resident pane repaints on it
        self.store = None

    def get(self, key):
        with self.lock:
            return self.data.get(key)

    def put(self, key, ok, value):
        with self.lock:
            old = self.data.get(key)
            self.data[key] = (time.monotonic(), ok, value)
            self.fetches += 1
            if not old or old[1] != ok or old[2] != value:
                self.version += 1


class Pane:
    """One renderer over a (shareable) data cache. Give the UI thread and the refresh thread a Pane each, sharing one
    `Cache`: `render(refresh=True)` refetches stale data; with `passive=True` a render never touches the store."""

    def __init__(self, cols=80, rows=0, state_path=None, store=None, cache=None):
        self.cols, self.rows = cols, rows
        self.state_path = state_path
        self.cache = cache or Cache()
        if store is not None:
            self.cache.store = store
        self.refresh = False
        self.passive = False
        self.jobs_ttl = 1.0  # the resident pane stretches it while no commit lands (dashapp.App.db_version)

    @property
    def fetches(self):
        return self.cache.fetches

    # -- data ---------------------------------------------------------------------------------------------------
    def store(self):
        with self.cache.lock:
            if self.cache.store is None:  # opened once, shared by every pass
                self.cache.store = Store()
            return self.cache.store

    def src(self, key, fn, ttl="jobs"):
        """Cached read. In a refresh pass a value older than its ttl is refetched; otherwise (and on a repaint) the
        cache answers. A missing key is fetched, unless the pane is passive (then `Miss`)."""
        seconds = self.jobs_ttl if ttl == "jobs" else FIXED_TTL.get(ttl, ttl)
        hit = self.cache.get(key)
        if hit and not (self.refresh and time.monotonic() - hit[0] >= seconds):
            if hit[1]:
                return hit[2]
            raise hit[2]
        if self.passive:
            raise Miss(key)
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 - a failed read keeps the last good value; the pane never breaks
            if hit and hit[1]:
                return hit[2]
            self.cache.put(key, False, exc)
            raise
        self.cache.put(key, True, value)
        return value

    def cli(self, *args, ttl="jobs"):
        return self.src(("cli", args), lambda: cli(*args), ttl)

    def safe(self, key, fn, default=None, ttl="jobs"):
        try:
            return self.src(key, fn, ttl)
        except Exception:  # noqa: BLE001
            return default

    # -- building blocks ----------------------------------------------------------------------------------------
    def row_key(self, n):
        return f"[{n}]"

    def bind_key(self, n, tab, fallback=""):
        """Digits 1-9 act on row [n]: focus its tab, else `fallback` (an `@action:target` the pane runs: unfold /
        explain). The pinned ops/steward tabs own 1 and 2 separately (render), row numbers start at 3."""
        if n < 10 and (tab or fallback):
            self.cmap.append(f"key {n} {tab or fallback}")

    def num_target(self, tab, jid):
        """What a double tap on [n] opens: the job's tab when it has one, else its explanation."""
        return tab or f"@explain_more:{jid}"

    def emit(self, text, action=None, toggle=None, p=5, more=None):
        if toggle:
            self.cmap.append(f"toggle {len(self.out)} {toggle}")
        if action:
            self.cmap.append(f"row {len(self.out)} {action}")
        if more:
            self.cmap.append(f"more {len(self.out)} {more}")
            if self.more_open(more):
                p = max(p, 7)  # asked for: outranks pruning, like an opened tip
        self.out.append(text)
        self.prio.append(p)

    # -- tap to expand ------------------------------------------------------------------------------------------
    def more_open(self, key):
        return dashact.is_expanded(self.dash_state, MORE_PREFIX + key)

    def cut_text(self, key, full, width):
        """`(shown, rest, cut)` for plain `full` in `width` cells. Fits: itself. Too long: its "…" cut, or when the owner
        opened it (tap) the whole text wrapped over as many lines as it needs (`shown` first, then `rest`). The full text
        stays in `self.fulltext[key]` whatever is on screen."""
        self.__dict__.setdefault("fulltext", {})[key] = full
        shown = fit(full, width)
        if shown == full:
            return full, [], False
        if self.more_open(key):
            lines = wrap_full(full, max(width, 4))
            return lines[0], lines[1:], True
        return shown, [], True

    def emit_cut(self, key, full, width, lead="", pre="", post="", p=5, action=None, lead_rest=None):
        """One plain-text line `lead + pre + text + post`; when the text is cut it taps open (and shut)."""
        shown, rest, cut = self.cut_text(key, full, width)
        self.emit(f"{lead}{pre}{shown}{post}", action, p=p, **({"more": key} if cut else {}))
        for ln in rest:
            self.emit(f"{lead if lead_rest is None else lead_rest}{pre}{ln}{post}", p=7, more=key)

    def buttons(self, spans, jid, approval="-"):
        self.cmap.extend(f"btn {len(self.out)} {c0} {c1} {act} {jid} {approval}" for c0, c1, act in spans)

    def fetch_pinned(self):
        """`{"ops": tab, "steward": tab}` from live Herdr, found by their panes (`herdr_tabs.pinned_tabs`)."""
        if not (os.environ.get("HERDR_PANE_ID") or os.environ.get("HERDR_SOCKET_PATH")):
            return {}  # not inside Herdr: never reach for a server that is not ours

        def herdr(*argv):
            out = subprocess.run(["herdr", *argv], capture_output=True, text=True, timeout=5).stdout
            return json.loads(out)["result"]

        return herdr_tabs.pinned_tabs(herdr("tab", "list")["tabs"], herdr("pane", "list")["panes"])

    def pinned(self):
        return self.safe(("pinned",), self.fetch_pinned, {}, ttl="slow") or {}

    def liveness(self, state, events, jid=None):
        from steward import delivery

        hint = delivery.liveness_hint(self.store(), jid, state) if jid else None
        kind, detail = jobview.liveness(state, events, delivery=hint)
        icon = {"live": "\033[32m{SPIN}\033[0m", "quiet": "\033[33m⚠\033[0m", "paused": "\033[33m⏸\033[0m",
                "waiting": "\033[33m◆\033[0m", "done": "\033[32m✓\033[0m", "bad": "\033[31m■\033[0m"}[kind]
        return icon, detail

    def eta_part(self, jid, state):
        """The stored ETA of a live job counted down to now (G16), or None: not live, efficiency off, nothing stored."""
        try:
            return jobview.eta_part(jobview.live_eta(self.store(), jid, state))
        except Exception:  # noqa: BLE001 - the ETA is decoration
            return None

    def deadline_part(self, jid):
        """The deadline sign / overrun banner from the persisted canonical object (v2.2), or None. Pure read."""
        try:
            return jobview.deadline_part(self.store(), jid)
        except Exception:  # noqa: BLE001 - decoration
            return None

    def reason(self, jid, state, events):
        return jobview.reason(self.cli("approvals", jid, "--json") or [], state, events)

    def supervisor_lines(self, jid, tab, sources=None):
        """Tree rows under a job: (text, action, toggle, chat button, more key) tuples; astra/sol/worker/watcher rows carry a
        [ chat ] button (a watcher's switch toggles it via `steward supervisor <job> <name> toggle`). Each row carries a
        token column; a status cut with "…" taps open (the `more` key)."""
        rows = [dict(r) for r in (self.cli("supervisors", jid, "--json") or [])]
        sources = sources or {}
        if sources.get("chats", {}).get("all"):
            at = 1 if rows and rows[0]["role"] == "worker" else 0
            rows.insert(at, {"role": "chats", "model": "", "status": "on", "display": "worker-chat copies you opened"})
        lines = []
        for k, r in enumerate(rows):
            branch = "└" if k == len(rows) - 1 else "├"
            tk = tokens_cell(sources.get(r.get("source") or r["role"]))
            chat = r["role"] in ("worker", "astra", "sol") or r.get("kind") == "watcher"
            avail = max(self.cols - 39 - (len(CHAT_BUTTON) + 1 if chat else 0), 0)  # room for the [ chat ] button
            sw = f"[{r['switch']}]" if r.get("switch") and r["display"].startswith(f"[{r['switch']}]") else ""
            parts = r.get("parts")
            key, rest = f"{jid}:sup:{r['role']}:{r.get('label', '')}", []
            if parts is None:
                full = r["display"]
                status = fit(full, avail)
                cut = status != full
            else:
                parts = list(parts)
                full = " ".join(x for x in (sw, " · ".join(parts)) if x)
                status = full
                while parts and vlen(status) > avail:
                    parts.pop()
                    status = " ".join(x for x in (sw, " · ".join(parts)) if x)
                status = fit(status, avail) if not sw else status
                cut = status.endswith("…")
            if cut:
                self.__dict__.setdefault("fulltext", {})[key] = full
                if self.more_open(key):  # the whole status, wrapped under the first row (which keeps its switch)
                    wrapped = wrap_full(full, max(avail, 4))
                    status, rest = wrapped[0], wrapped[1:]
            status = status.replace("{SPIN}", "\033[32m{SPIN}\033[0m")
            head = (f"    {branch} {_label(r.get('label', r['role'])):<9}{jobview.short_model(r['model']):<7}"
                    f"\033[2m{tk:<17}\033[0m")
            text = f"{head}\033[2m{status}\033[0m" if r["status"] == "off" else f"{head}{status}"
            span = switch_span(text, r.get("switch") if sw else None)
            toggle = f"{span[0]} {span[1]} {jid} {r['role']} {r['next']}" if span and r.get("next") else None
            btn = None
            if chat:  # supervisor chats open from this button only, never from a stray tap on the row
                c0 = max(vlen(text), 39 + avail) + 2
                text += " " * (c0 - 1 - vlen(text)) + CHAT_BUTTON
                btn = f"{c0} {c0 + len(CHAT_BUTTON) - 1} {jid} {r['role']}"
            lines.append((text, None, toggle, btn, key if cut else None))
            for ln in rest:
                body = ln.replace("{SPIN}", "\033[32m{SPIN}\033[0m")
                lines.append((" " * 39 + (f"\033[2m{body}\033[0m" if r["status"] == "off" else body), None, None, None, key))
        return lines

    def health_header(self):
        """One dot per component and a line for anything red (the 30s health check cached in health.json). Display only:
        `steward watchdog` heals and escalates control/runners/herdr; other components alert through the gating."""
        if os.environ.get("STEWARD_HEALTH") == "0":  # tests (and anyone who wants a quiet pane) never probe Herdr/systemd
            return

        def fetch():
            store = self.store()
            # display only: the watchdog is the one healer (issue W)
            state = health_module.tick(store, heal_fn=None, alert_skip=health_module.WATCHDOG_OWNED,
                                       alert_fn=health_module.default_alert() if alerts.telegram_enabled() else None)
            return health_module.render_header(state, self.cols)

        for ln in self.safe(("health", self.cols), fetch, [], ttl="slow"):
            self.emit(ln)

    def warn_lines(self, jid, state, tj, blockers_only=False):
        """`runner outdated  [ restart runner ]` and the spend warnings, under a job's header. `blockers_only` (a folded
        job): only what stops the job, never spend or efficiency advice."""
        if state in ("RUNNING", "PAUSED"):
            behind = self.safe(("outdated", jid), lambda: stability.runner_outdated(self.store(), jid), 0, ttl="slow")
            if behind:
                left = f"    \033[33m⚠ runner outdated ({behind} commit{'s' if behind != 1 else ''} behind)\033[0m"
                label = "[ restart runner ]"
                k = vlen(left) + 2
                if k + len(label) > self.cols:  # too narrow for one row: button on its own row
                    self.emit(left)
                    colored, plain, spans = dashact.button_row([("restart runner", "restart_runner")])
                    self.buttons(spans, jid)
                    self.emit(colored, p=9)
                else:
                    colored, plain, spans = dashact.button_row([("restart runner", "restart_runner")], indent=k)
                    self.buttons(spans, jid)
                    self.emit(left + "  " + colored[k:])
        if blockers_only:
            return
        tips = list(stability.spend_warnings(tj))
        try:  # efficiency findings from the watcher's stored state; never breaks the pane
            from steward import efficiency_watch

            tips += list(self.safe(("effwarn", jid), lambda: efficiency_watch.pane_warnings(self.store(), jid), [], ttl="slow"))
        except Exception:  # noqa: BLE001
            pass
        if not tips:
            return
        if len(tips) == 1:
            self.emit_cut(f"{jid}:tip", tips[0], self.cols - 4, "    ", "\033[33m", "\033[0m", p=3)
            return
        # Several spend/efficiency tips are advice, not blockers: one summary line, tap it to see them all.
        rate = next((m.group(1) for t in tips for m in [re.search(r"spend \$?([\d.]+)/h", t)] if m), None)
        save = sum(float(x) for t in tips for x in re.findall(r"\u2248-\$([\d.]+)", t))
        parts = [f"\u26a0 {len(tips)} tips"] + ([f"${rate}/h"] if rate else []) + ([f"save \u2248${save:.2f}"] if save else [])
        key = "tips:" + jid
        opened = dashact.is_expanded(self.dash_state, key)
        self.cmap.append(f"expand {len(self.out)} {key}")
        self.emit(f"    \033[33m{' \u00b7 '.join(parts)}\033[0m \033[2m\u00b7 tap to {'hide' if opened else 'show'}\033[0m", p=3)
        if opened:
            for k, w in enumerate(tips):  # asked for: survives pane-height pruning
                self.emit_cut(f"{jid}:tips:{k}", w, self.cols - 6, "      ", "\033[33m", "\033[0m", p=7)

    def goal_lines(self, jid, goal):
        """The goal under a job's header, at most two lines; cut with "…" it taps open to the whole goal."""
        key = f"{jid}:goal"
        goal = " ".join(goal.split())
        self.__dict__.setdefault("fulltext", {})[key] = goal
        lines = jobview.wrap(goal, self.cols - 4, 2)
        cut = bool(lines) and lines[-1].endswith("…") and lines[-1] != goal
        if cut and self.more_open(key):
            lines = wrap_full(goal, self.cols - 4)
        for gi, ln in enumerate(lines):
            self.emit(f"    {ln}", p=4 if gi == 0 else 2, **({"more": key} if cut else {}))

    def fleet_lane_lines(self, jid):
        """A fleet job's lanes (v2.5, D-08): lane -> runtime/model, state, and tokens/cost when the runtime reported
        them - unknown is shown as unknown, never 0. Legacy jobs print nothing."""
        view = self.cli("fleet", "graph", jid, "--json") or {}
        if not isinstance(view, dict) or view.get("mode") != "fleet":
            return
        for ln in view.get("lanes") or []:
            u = ln.get("usage") or {}
            tokens = "unknown" if u.get("tokens") is None else hum(u["tokens"])
            cost = "unknown" if u.get("cost_usd") is None else f"${u['cost_usd']:.2f}"
            row = (f"    {ln.get('lane')}  {ln.get('runtime') or '?'}/{ln.get('model') or '?'}  {ln.get('state')}"
                   f"  · tokens {tokens} · cost {cost}")
            self.emit_cut(f"{jid}:{ln.get('lane')}", row.strip(), self.cols - 4, "    ", "\033[2m", "\033[0m", p=3)

    def action_row(self, jid, state, explain=True):
        """`[ explain ]  [ unblock now ]` under a live or waiting job's header (folded or not); `explain=False` when
        `[ explain ]` already sits on the header row."""
        buttons = ([("explain", "explain")] if explain else []) + ([("unblock now", "unblock")] if state in UNBLOCK_STATES else [])
        if not buttons:
            return
        colored, plain, spans = dashact.button_row(buttons)
        self.buttons(spans, jid)
        self.emit(colored, p=6)

    def coordinator_line(self, jid):
        """The coordinator's hold or latest episode, one cyan line (spec 5.5)."""
        from steward import coordinator_link

        line = self.safe(("coord", jid), lambda: coordinator_link.dash_line(self.store(), jid), "")
        if line:
            self.emit(f"    \033[36m{line}\033[0m", p=8)

    def unblock_lines(self, jid, folded):
        """What the unblock agent is doing, or its last report (2-5 lines; one line when the job is folded)."""
        from steward import unblock_agent

        run = self.safe(("unblock_run", jid), lambda: unblock_agent.active_run(self.store(), jid))
        if run:
            self.emit(f"    \033[33m… unblock agent ({run.get('model')}) is working on it (since {unblock_agent.running_since(run)})\033[0m", p=8)
            return
        rec = self.safe(("unblock_note", jid), lambda: unblock_agent.note(self.store(), jid))
        if not rec:
            return
        lines = [ln for ln in str(rec.get("text", "")).splitlines() if ln.strip()]
        for k, ln in enumerate(lines[:1] if folded else lines):
            lead = f"unblock ({rec.get('model')}): " if k == 0 else "  "
            self.emit_cut(f"{jid}:unblock:{k}", lead + ln, self.cols - 4, "    ", "\033[2m", "\033[0m", p=8, lead_rest="      ")
        live = self.safe(("unblock_human", jid), lambda: unblock_agent.human_blockers(self.store(), jid), [])
        kinds = {b.get("kind") for b in live} if rec.get("human") else set()
        buttons = ([("accept", "accept")] if "accept" in kinds else []) + ([("answer", "answer")] if "answer" in kinds else [])
        if buttons and not folded:  # the report found only the owner's decision left: put that button right here
            colored, plain, spans = dashact.button_row(buttons, dashact.armed_action(self.dash_state, jid))
            self.buttons(spans, jid)
            self.emit(colored, p=9)

    def explain_panel(self, jid):
        """The standard explanation (steward.explain) under the job, then `[ more ]` (the job chat primed with it)."""
        from steward import explain as explain_module

        text = self.safe(("explain", jid, self.cols), lambda: explain_module.text(self.store(), jid), None, ttl="slow")
        for raw in (text or "explanation unavailable").splitlines():
            lead = len(raw) - len(raw.lstrip())
            for k, ln in enumerate(jobview.wrap(raw.strip(), max(self.cols - 6 - lead, 20), 2) or [""]):
                self.emit(f"\033[2m      {' ' * (lead if k == 0 else lead + 2)}{ln}\033[0m", p=7)
        colored, plain, spans = dashact.button_row([("more", "explain_more")])
        self.buttons(spans, jid)
        self.emit(colored, p=9)

    def carried_lines(self, j):
        """A finished job's unanswered owner decisions (`ownership.carried_decisions`): never folded into "done"."""
        ds = j["owner_decisions"]
        more = f" (+{len(ds) - 1} more)" if len(ds) > 1 else ""
        self.emit("    \033[33m◆ " + fit(ds[0]["text"], max(self.cols - 6 - len(more), 12)) + more + "\033[0m", p=5)
        self.question_lines(j.get("job_id", ""), j.get("state", ""))

    def question_lines(self, jid, state, blockers_only=False):
        """G12: the queue on the pane is ONE count line plus its buttons, never cards. A question the worker decided is
        flagged on its own line (G3, G10)."""
        def fetch():
            from steward import questions

            return questions.pane_summary(self.store(), jid)

        qs = self.safe(("questions", jid), fetch)
        if qs is None:
            return
        waiting, starred, needs = qs["waiting"], qs["starred"], qs["needs_you"]
        if blockers_only and not needs:  # a folded job shows the queue only when a question needs the owner
            return
        if waiting:
            parts = [f"? {_plural(waiting, 'question', 'questions')}"]
            if starred:
                parts.append(f"{starred} ★")
            if needs:
                parts.append(f"{needs} {'needs' if needs == 1 else 'need'} you")
            left = "    \033[33m" + " · ".join(parts) + "\033[0m"
            buttons = ([("accept all ★", "accept_rec")] if starred else []) + [("answer", "answer")]
            armed = dashact.armed_action(self.dash_state, jid)
            k = vlen(left) + 2
            width = len(dashact.button_row(buttons, armed, indent=0)[1])
            if k + width > self.cols:  # too narrow for one row: the count (priority 5), then the buttons (9) on their own row
                self.emit(left, p=5)
                colored, plain, spans = dashact.button_row(buttons, armed)
                self.buttons(spans, jid)
                self.emit(colored, p=9)
            else:
                colored, plain, spans = dashact.button_row(buttons, armed, indent=k)
                self.buttons(spans, jid)
                self.emit(left + "  " + colored[k:], p=9)
        worker = qs.get("worker_decided") or []
        if worker:
            shown = worker[:3] + ([f"+{len(worker) - 3}"] if len(worker) > 3 else [])
            text = (f"⚠ {_plural(len(worker), 'question', 'questions')} decided by the worker, not you "
                    f"({', '.join(shown)})")
            self.emit_cut(f"{jid}:worker", text, self.cols - 4, "    ", "\033[33m", "\033[0m", p=5)

    def decision_panel(self, jid, tj):
        """Expanded evidence panel + `[ button ]` row under a job's question line."""
        dec = self.src(("decision", jid), lambda: evidence.decision(self.store(), jid))
        if dec is None:
            return
        usd = ((tj or {}).get("total") or {}).get("usd")
        width = max(self.cols - 6, 20)
        panel = self.src(("panel", jid, width, usd), lambda: evidence.panel_cached(self.store(), jid, width, cost_usd=usd, dec=dec))
        for t in panel:
            self.emit(f"\033[2m      {fit(t, self.cols - 6)}\033[0m", p=3)
        rec = dec.get("approval")
        # one row when the buttons fit, else more (an acceptance with questions has six): every row keeps priority 9
        for colored, plain, spans in dashact.button_rows(evidence.buttons_for(dec), dashact.armed_action(self.dash_state, jid), self.cols):
            self.buttons(spans, jid, rec["id"] if rec else "-")
            self.emit(colored, p=9)

    def incorporate_panel(self, jid):
        """Expanded dry-run preflight + `[ merge into <branch> ]  [ cancel ]` under an ACCEPTED job."""
        pf = self.src(("preflight", jid), lambda: incorporate.preflight(self.store(), jid), ttl="slow")
        for t in pf.lines(width=0):
            self.emit(f"\033[2m      {fit(t, self.cols - 6)}\033[0m", p=3)
        fix = self.fixup(jid)
        if pf.error:
            buttons = [("cancel", "cancel")]
        elif pf.conflicts and not fix:
            buttons = [("fix merge", "fix_merge"), ("cancel", "cancel")]
        elif pf.block:  # a merge that is already known to fail is never offered; the way forward is the fix-up
            buttons = ([("open fix-up", "open_fixup")] if fix and fix.get("child") else []) + [("cancel", "cancel")]
        else:
            buttons = [(fit(f"merge into {pf.source_branch}", max(self.cols - 20, 12)), "merge"), ("cancel", "cancel")]
        colored, plain, spans = dashact.button_row(buttons)
        self.cmap.extend(f"btn {len(self.out)} {c0} {c1} {act} {fix['child'] if act == 'open_fixup' else jid} -"
                         for c0, c1, act in spans)
        self.emit(colored, p=9)

    def fixup(self, jid):
        """`autopilot.followup_status` for an ACCEPTED, unmerged job (None: no fix-up was ever started)."""
        from steward import autopilot

        return self.safe(("fixup", jid), lambda: autopilot.followup_status(self.store(), jid), None)

    def fix_merge_needed(self, jid):
        from steward import autopilot

        return self.safe(("fixmerge", jid), lambda: autopilot.fix_merge_needed(
            self.store(), jid, self.src(("preflight", jid), lambda: incorporate.preflight(self.store(), jid), ttl="slow")), False)

    def incorporated(self, jid):
        return self.src(("incorporated", jid), lambda: incorporate.is_incorporated(self.store(), jid))

    @staticmethod
    def finished_ago(j):
        at = j.get("finished_at")
        if not at:
            return ""
        try:
            secs = max(time.time() - datetime.fromisoformat(at).timestamp(), 0)
        except ValueError:
            return ""
        return f" · {secs / 86400:.0f}d ago" if secs >= 86400 else f" · {jobview.ago(secs)} ago"

    def exported_to(self, jid):
        return self.safe(("exported", jid), lambda: export_module.exported(self.store(), jid))

    def export_row(self, jid, events):
        """STOPPED / NEEDS_HUMAN-at-acceptance jobs sit in the active list: their `[ export ]` rides its own row."""
        gone = self.exported_to(jid)
        if gone:
            self.emit(f"\033[2m{fit(f'    exported → {gone}', self.cols)}\033[0m", p=2)
            return
        colored, plain, spans = dashact.button_row([("export", "export")])
        self.cmap.extend(f"btn {len(self.out)} {c0} {c1} {act} {jid} -" for c0, c1, act in spans)
        self.emit(colored, p=3)

    def usd_of(self, jid):
        t = ((self.tok.get("jobs") or {}).get(jid) or {}).get("total")
        return t.get("usd") or None if t else None  # no spend recorded: say nothing

    def done_line(self, j, n):
        """`[n] ✓ <id8> <goal> · $x · merged <sha7>|not merged  [ incorporate ] [ release ]`, one row, buttons right-aligned."""
        jid = j.get("job_id", "")
        tab = tab_of(j)
        key = self.row_key(n)
        inc = self.incorporated(jid)
        state = j.get("state")
        done = state in ("RELEASED", "STOPPED", "FAILED")
        fix = None if inc or done else self.fixup(jid)
        inflight = bool(fix) and fix["kind"] in ("running", "not_started", "reviewing")
        if done:
            buttons = []  # finished for good: nothing left to press but export
        elif inc:
            buttons = [("release", "release")]
        elif inflight:  # a fix-up is on it: merging by hand now would race it
            buttons = ([("launch", "launch")] if fix["kind"] == "not_started" else []) + [("release", "release")]
        elif state == "ACCEPTED" and self.fix_merge_needed(jid):  # the merge would conflict: the fix-up replaces it
            buttons = [("fix merge", "fix_merge"), ("release", "release")]
        else:
            buttons = [("incorporate", "incorporate"), ("release", "release")]
        gone = self.exported_to(jid)
        own_row = False
        if not gone:  # a finished job can be taken into its own workspace; afterwards the row says where it went
            own_row = self.cols - len(f"{key} ✓ {jid[:8]} ") - len(dashact.button_row([*buttons, ("export", "export")], indent=2)[1]) - 1 < 12
            if not own_row:  # too narrow for the goal as well: the button drops to a row of its own
                buttons.append(("export", "export"))
        colored, plain, spans = dashact.button_row(buttons, indent=2)
        usd = self.usd_of(jid)
        if inc:
            merge = f" · merged {inc[:7]}"
        elif state == "ACCEPTED":
            merge = self.merge_meta(fix)
        else:
            merge = f" · {state.lower()}"
        metas = [(f" · ${usd:.2f}" if usd is not None else ""), merge, self.finished_ago(j),
                 (f" · exported → {gone}" if gone else "")]
        glyph = "\033[32m✓\033[0m" if state in ("ACCEPTED", "RELEASED") else "\033[31m✗\033[0m"
        prefix = f"{key} {glyph} {jid[:8]} "
        room = self.cols - vlen(prefix) - len(plain) - 1
        for drop in (0, 2, 1):  # keep the goal readable: drop the $, then the age, then the merge state
            if room - sum(len(m) for m in metas) >= 12:
                break
            metas[drop] = ""
        meta = "".join(metas)
        goal, rest, cut = self.cut_text(f"{jid}:done", " ".join((j.get("goal") or "").split()), max(room - len(meta), 0))
        head = prefix + goal + f"\033[2m{meta}\033[0m"
        pad = " " * max(self.cols - vlen(head) - len(plain), 1)
        offset = vlen(head) + len(pad)
        self.cmap.extend(f"btn {len(self.out)} {offset + c0} {offset + c1} {act} {fix['child'] if act == 'launch' else jid} -"
                         for c0, c1, act in spans)
        self.cmap.append(f"num {len(self.out)} 1 {len(key)} {self.num_target(tab, jid)}")
        self.emit(head + pad + colored, **({"more": f"{jid}:done"} if cut else {}))
        for ln in rest:
            self.emit(f"    {ln}", more=f"{jid}:done")
        if own_row:
            c, _, sp = dashact.button_row([("export", "export")], indent=4)
            self.cmap.extend(f"btn {len(self.out)} {c0} {c1} {act} {jid} -" for c0, c1, act in sp)
            self.emit(c)
        if not inc and not inflight and dashact.is_expanded(self.dash_state, dashact.INC_PREFIX + jid):
            self.incorporate_panel(jid)

    @staticmethod
    def merge_meta(fix):
        if not fix:
            return " · not merged"
        who = f"fix-up {fix['child'][:8]}" if fix.get("child") else "fix-up"
        if fix["kind"] == "running":
            return f" · merge blocked · {who} running ({fix['n']}/{fix['max']})"
        if fix["kind"] == "not_started":
            return f" · merge blocked · {who} not started"
        if fix["kind"] == "reviewing":
            return " · merge repair under strategy review"
        return " · merge blocked · needs you"

    def merged(self, j):
        jid = j.get("job_id", "")
        return j.get("state") == "ACCEPTED" and bool(self.safe(
            ("incorporated", jid), lambda: incorporate.is_incorporated(self.store(), jid), None))

    def _unmerged(self, j):
        return j.get("state") == "ACCEPTED" and not self.incorporated(j.get("job_id", ""))

    def _unwritten(self, j):
        """Answers of a merged ACCEPTED job that exist only in the store (G6): `(count, branch)` or None."""
        if j.get("state") != "ACCEPTED":
            return None

        def fetch():
            from steward import questions

            jid = j.get("job_id", "")
            if not incorporate.is_incorporated(self.store(), jid):
                return None
            pending = questions.source_unwritten(self.store(), jid)
            return (len(pending), questions.merged_branch(self.store(), jid) or "the source branch") if pending else None

        return self.safe(("unwritten", j.get("job_id", "")), fetch)

    def write_answers_row(self, jid, n, branch):
        """`? 3 answers not on main  [ write answers to main ]`: one row under the job's done line; when the pane is too
        narrow the branch is shortened first, the count is kept, and the button falls back to `[ write answers ]`."""
        noun = "answer" if n == 1 else "answers"
        head = f"? {n} {noun} not on "
        room = self.cols - 4 - len(head) - 2 - len("[ write answers to  ]")  # the branch appears twice
        if room >= 2 * len(branch):
            label, shown = f"write answers to {branch}", branch
        elif room >= 2 * 4:
            shown = _short(branch, room // 2)
            label = f"write answers to {shown}"
        else:
            shown, label = _short(branch, max(self.cols - 4 - len(head), 4)), "write answers"
        left = f"    \033[33m{head}{shown}\033[0m"
        k = vlen(left) + 2
        colored, plain, spans = dashact.button_row([(label, "write_source")], indent=k)
        if len(plain) > self.cols:  # still too wide: the buttons go on their own row
            self.emit(left, p=5)
            colored, plain, spans = dashact.button_row([(label, "write_source")])
            self.buttons(spans, jid)
            self.emit(colored, p=9)
            return
        self.buttons(spans, jid)
        self.emit(left + "  " + colored[k:], p=9)

    def waiting_total(self, active):
        def fetch():
            from steward import questions

            return questions.total_waiting(self.store(), [j.get("job_id", "") for j in active])

        return self.safe(("waiting_total", tuple(j.get("job_id", "") for j in active)), fetch, 0)

    def prefetch_folded(self, jid, state):
        """A refresh pass also loads what an unfold would need, so unfolding paints from the cache."""
        self.cli("supervisors", jid, "--json")
        if state in ("RUNNING", "PAUSED"):
            self.safe(("outdated", jid), lambda: stability.runner_outdated(self.store(), jid), 0, ttl="slow")
        try:
            from steward import efficiency_watch

            self.safe(("effwarn", jid), lambda: efficiency_watch.pane_warnings(self.store(), jid), [], ttl="slow")
        except Exception:  # noqa: BLE001
            pass

    def prefetch_decision(self, jid, tj):
        dec = self.safe(("decision", jid), lambda: evidence.decision(self.store(), jid))
        if dec is not None:
            usd = ((tj or {}).get("total") or {}).get("usd")
            width = max(self.cols - 6, 20)
            self.safe(("panel", jid, width, usd), lambda: evidence.panel_cached(self.store(), jid, width, cost_usd=usd, dec=dec))

    # -- the frame ----------------------------------------------------------------------------------------------
    def job_block(self, j, ind=""):
        """One live job's rows: header (state, ETA, buttons), goal, warnings, questions, tokens, supervisors. `ind` indents
        the header of a queue-group member."""
        cols = self.cols
        tok, state_ = self.tok, self.dash_state
        refresh = self.refresh
        state, jid = j.get("state", "?"), j.get("job_id", "")
        n = self.rowno.get(jid, 0)
        tab = tab_of(j)
        self.bind_key(n, tab, f"@fold:{jid}")
        events = self.cli("events", jid, "--json") or []
        icon, detail = self.liveness(state, events, jid)
        # The ETA is the most useful fact on a live job: it goes first (owner, 2026-09-30), the liveness detail
        # follows and is what gets cut when the row is narrow. The id stays for `steward chat <id>`.
        part = self.eta_part(jid, state)
        detail = f"{part} · {detail} · {jid[:8]}" if part else f"{detail} · {jid[:8]}"  # narrow rows cut the id first
        dpart = self.deadline_part(jid)
        if dpart:  # v2.2: the deadline sign sits beside the ETA, never merged into it
            detail = f"{dpart} · {detail}"
        key = self.row_key(n)
        name = fit(j.get("name") or jid[:8], tabnames.NAME_CAP)
        shown_state = "staged" if state in ("CREATED", "CONTRACTED") and (j.get("queue") or j.get("group")) else state
        prefix = f"{ind}{key} {icon} {name}  {shown_state} "

        # the [n] folds / unfolds the job (a double tap on it opens the tab, else the explanation); the rest is inert
        self.cmap.append(f"btn {len(self.out)} {1 + len(ind)} {len(ind) + len(key)} fold {jid} -")
        self.cmap.append(f"num {len(self.out)} {1 + len(ind)} {len(ind) + len(key)} {self.num_target(tab, jid)}")
        hkey, detail = f"{jid}:header", ANSI.sub("", detail)
        on_header = False
        if state in SUPERVISED:  # the quick-steer (and, when it fits, explain) buttons ride the header row
            on_header = cols >= vlen(prefix) + 8 + 2 + len(STEER_BUTTON) + 2 + len(EXPLAIN_BUTTON)
            tail = len(STEER_BUTTON) + (2 + len(EXPLAIN_BUTTON) if on_header else 0)
            shown, rest, cut = self.cut_text(hkey, detail, max(cols - vlen(prefix) - tail - 2, 8))
            start = vlen(prefix) + vlen(shown) + 3
            self.cmap.append(f"btn {len(self.out)} {start} {start + len(STEER_BUTTON) - 1} steer {jid} -")
            row = f"{prefix}\033[2m{shown}\033[0m  {STEER_BUTTON}"
            if on_header:
                e0 = start + len(STEER_BUTTON) + 2
                self.cmap.append(f"btn {len(self.out)} {e0} {e0 + len(EXPLAIN_BUTTON) - 1} explain {jid} -")
                row += f"  {EXPLAIN_BUTTON}"
            self.emit(row, p=9, **({"more": hkey} if cut else {}))
        else:
            shown, rest, cut = self.cut_text(hkey, detail, cols - vlen(prefix))
            self.emit(f"{prefix}\033[2m{shown}\033[0m", p=9, **({"more": hkey} if cut else {}))
        for ln in rest:
            self.emit(f"    \033[2m{ln}\033[0m", p=7, more=hkey)
        tj = (tok.get("jobs") or {}).get(jid)
        if state in SUPERVISED:
            self.action_row(jid, state, explain=not on_header)
            self.unblock_lines(jid, folded=dashact.is_expanded(state_, dashact.FOLD_PREFIX + jid))
            if dashact.is_expanded(state_, dashact.EXPLAIN_PREFIX + jid):
                self.explain_panel(jid)
        if dashact.is_expanded(state_, dashact.FOLD_PREFIX + jid):
            ask = self.reason(jid, state, events)
            goal = " ".join((j.get("goal") or "").split())
            self.emit_cut(f"{jid}:goal", goal, min(cols - 4, 60), "    ", p=8)
            # blockers stay visible when folded: the decision it waits on, a held runner, questions only the owner can answer
            if ask:
                self.cmap.append(f"expand {len(self.out)} {jid}")
                self.emit(f"    \033[33m→ {fit(ask, cols - 7)}\033[0m", p=9)
                if dashact.is_expanded(state_, jid):
                    self.decision_panel(jid, tj)
                elif refresh:
                    self.prefetch_decision(jid, tj)
            self.warn_lines(jid, state, None, blockers_only=True)
            self.question_lines(jid, state, blockers_only=True)
            self.coordinator_line(jid)
            if refresh:
                self.prefetch_folded(jid, state)
            return
        self.goal_lines(jid, j.get("goal") or "")
        self.fleet_lane_lines(jid)
        self.warn_lines(jid, state, tj)
        self.question_lines(jid, state)
        self.coordinator_line(jid)
        if state == "STOPPED" or (state == "NEEDS_HUMAN" and any(
                e.get("what") in export_module._UNRESOLVED for e in events)):
            self.export_row(jid, events)
        if tj:
            line = f"    tokens {hum(tj['total']['all'])} · last 1h {hum(tj['last_hour']['all'])}  (per model: total/1h)"
            self.emit_cut(f"{jid}:tokens", line.strip(), cols - 4, "    ", "\033[2m", "\033[0m", p=2)
            eff = (f"    effective {hum(tj['total'].get('effective', 0))} (${tj['total'].get('usd', 0):.2f})"
                   f" · last 1h {hum(tj['last_hour'].get('effective', 0))} (${tj['last_hour'].get('usd', 0):.2f})")
            self.emit_cut(f"{jid}:effective", eff.strip(), cols - 4, "    ", p=1)
            self.emit_cut(f"{jid}:split", split(tj["total"]), cols - 4, "    ", "\033[2m", "\033[0m", p=1)
        ask = self.reason(jid, state, events)
        if ask:
            self.cmap.append(f"expand {len(self.out)} {jid}")
            self.emit(f"    \033[33m→ {fit(ask, cols - 7)}\033[0m")
            if dashact.is_expanded(state_, jid):
                self.decision_panel(jid, tj)
            elif refresh:
                self.prefetch_decision(jid, tj)
        if state in SUPERVISED:
            panel_open = bool(ask) and dashact.is_expanded(state_, jid)
            for text, action, toggle, btn, more in self.supervisor_lines(jid, tab, (tj or {}).get("sources")):
                if btn:
                    self.cmap.append(f"chatbtn {len(self.out)} {btn}")
                self.emit(text, action, toggle, p=2 if panel_open else 3, **({"more": more} if more else {}))

    def queue_groups(self, active, done):
        """The queue groups (job sequencing) present in this frame, in pane order: the `group` dicts `steward status
        --json` stamps on each member. A group is shown only while one of its members is unfinished (the CLI drops it
        otherwise)."""
        seen, out = set(), []
        for j in [*active, *done]:
            g = j.get("group")
            if g and g["root"] not in seen:
                seen.add(g["root"])
                out.append(g)
        return out

    def queue_row(self, j, ind=""):
        """Under a staged queued job: why it waits, and `[ ▶ play ] [ ↑ ] [ ↓ ]` (play jumps the queue; the arrows
        reorder within it). A staged head with followers gets play and down."""
        q, jid = j.get("queue"), j.get("job_id", "")
        if j.get("state") not in ("CREATED", "CONTRACTED") or not (q or j.get("queue_head")):
            return
        pad = ind + "    "
        text = (q.get("text") or "queued") if isinstance(q, dict) else "staged · queue head"
        self.emit(f"{pad}\033[2m{fit(text, self.cols - len(pad))}\033[0m", p=8)
        buttons = [("▶ play", "play")]
        if q and q.get("after_staged"):
            buttons.append(("↑", "qup"))
        if j.get("queue_next"):
            buttons.append(("↓", "qdown"))
        colored, plain, spans = dashact.button_row(buttons, indent=len(pad))
        self.buttons(spans, jid)
        self.emit(colored, p=8)

    @staticmethod
    def _short_in_group(g, m, jid):
        """A member's name with the group's own name stripped off the front ("Recipe book Job 2" -> "Job 2")."""
        name = m.get("name") or jid[:8]
        rest = name[len(g["name"]):].strip(" -:·") if name.lower().startswith(g["name"].lower()) else ""
        return fit(rest or name, tabnames.NAME_CAP)

    def group_block(self, g, ordered):
        """ONE row for a whole queue: `[ ] ▸ Recipe book · 6 jobs · 1/6 done · now: Job 2 (running) · ETA`, tapping it
        folds/unfolds the members underneath (folded by default above two members). Every member's blockers surface on
        the group row, folded or not."""
        cols, state_ = self.cols, self.dash_state
        by_id = {j.get("job_id", ""): j for j in self.jobs_all}
        members = [by_id[i] for i in g["members"] if i in by_id]
        live = [m for m in members if m.get("state") not in DONE]
        blockers, quiet_paused = [], False
        for m in live:
            mid, mstate = m.get("job_id", ""), m.get("state", "")
            who = self._short_in_group(g, m, mid)
            ask = self.reason(mid, mstate, self.cli("events", mid, "--json") or [])
            if ask:
                blockers.append((m, f"{who}: {ask}"))
            q = m.get("queue")
            if q and q["state"] == "paused":
                blockers.append((m, f"{who}: {q['reason']} - queue paused"))
        now = by_id.get(g["now"]) or (live[0] if live else None)
        nstate = (now or {}).get("state", "")
        word = {"RUNNING": "running", "NEEDS_HUMAN": "needs you", "PAUSED": "paused", "CREATED": "queued",
                "CONTRACTED": "queued"}.get(nstate, nstate.lower())
        eta = self.eta_part(now.get("job_id", ""), nstate) if now and nstate in LIVE else None
        glyph = "\033[33m!\033[0m" if blockers else ("\033[32m●\033[0m" if nstate in LIVE else "○")
        shown = dashact.is_expanded(state_, GROUP_PREFIX + g["root"]) != (g["size"] <= 2)
        arrow = "▾" if shown else "▸"
        nowname = self._short_in_group(g, now or {}, g["now"])
        if g.get("implicit"):  # no queue order to report: count the members by state, name the running one
            text = self.implicit_text(g, live) + (f" · {eta}" if eta else "")
        else:
            text = (f"{g['name']} · {g['size']} jobs · {g['done']}/{g['size']} done · now: {nowname} ({word})"
                    + (f" · {eta}" if eta else "")
                    + (f" · ⚠ {len(blockers)} need{'s' if len(blockers) == 1 else ''} you" if blockers else ""))
        n = self.grpno.get(g["root"], 0)
        key = self.row_key(n)
        self.bind_key(n, "", f"@group:{g['root']}")
        self.cmap.append(f"expand {len(self.out)} {GROUP_PREFIX}{g['root']}")
        self.emit(f"{key} {glyph} {arrow} {fit(text, cols - len(key) - 5)}", p=9)
        waiting_merge = {j.get("job_id", "") for j in self.waiting_merge_jobs}
        if not shown:
            for m, line in blockers:  # blockers never hide inside a folded group
                self.emit(f"    \033[33m→ {fit(line, cols - 7)}\033[0m", p=9)
            for m in live:
                self.question_lines(m.get("job_id", ""), m.get("state", ""), blockers_only=True)
            return
        for m in members:
            mid = m.get("job_id", "")
            if m.get("state") in DONE:
                if mid not in waiting_merge:
                    nm = fit(m.get("name") or mid[:8], tabnames.NAME_CAP)
                    self.emit(f"    \033[32m✓\033[0m \033[2m{nm}  {m.get('state', '').lower()}\033[0m", p=5)
                continue
            if g.get("implicit") and m.get("state") in STAGED:
                self.staged_row(m, time.time(), ind="  ")
                continue
            self.job_block(m, ind="  ")
            self.queue_row(m, ind="  ")

    def implicit_text(self, g, live):
        """`V3 · 9 jobs · 1 running (M6) · 1 needs you · 7 staged` - the group's members counted by state."""
        by = {}
        for m in live:
            st = m.get("state", "")
            word = {"RUNNING": "running", "NEEDS_HUMAN": "needs you", "CREATED": "staged", "CONTRACTED": "staged"}.get(st, st.lower())
            by.setdefault(word, []).append(m)
        parts = []
        for word in ("running", "needs you", "paused", "staged", *[w for w in by if w not in ("running", "needs you", "paused", "staged")]):
            ms = by.get(word)
            if not ms:
                continue
            label = f"{len(ms)} {word}"
            if len(ms) == 1 and word != "staged":
                label += f" ({g['ids'].get(ms[0].get('job_id', ''), '?')})"
            parts.append(label)
        return " · ".join([g["name"], f"{g['size']} jobs", *parts])

    def render(self, refresh=False):
        """`(lines, click_map_lines)`. `refresh` refetches stale data; without it only the cache (plus misses) is read."""
        self.refresh = refresh
        self.out, self.cmap, self.prio = [], [], []
        self.dash_state = dashact.load(self.state_path)
        d = self.cli("status", "--json", "--recent", f"{recent_days():g}")
        jobs = d if isinstance(d, list) else (d or {}).get("jobs", [d] if d else [])
        if not jobs:
            self.out.append("  no active jobs - say: Run Steward on <repo>: <goal>")
            self.prio.append(5)
        self.tok = self.cli("tokens", "--json", ttl="tokens") or {}
        tok, state_ = self.tok, self.dash_state
        # ONE order for this pane's [n] and the Herdr tab bar (herdr_tabs.order_jobs): n is the job's tab position
        ordered = herdr_tabs.order_jobs(jobs, lambda j: (j.get("state", ""), j.get("created_at") or "", self.merged(j)))
        implicit_groups(ordered)  # numbered-series jobs from one burst nest under their overall task
        done = [j for j in ordered if j.get("state") in DONE]
        queued = lambda j: bool(j.get("queue") or j.get("group") or j.get("queue_head"))  # a staged job in a sequence
        staged = [j for j in ordered if j.get("state") in STAGED and not queued(j)]  # created, never launched: own group
        active = [j for j in ordered if j.get("state") not in DONE and (j.get("state") not in STAGED or queued(j))]
        for n, kind in ((1, "ops"), (2, "steward")):  # digits 1 and 2 reach the pinned tabs
            if self.pinned().get(kind):
                self.cmap.append(f"key {n} {self.pinned()[kind]}")
        self.health_header()
        if tok.get("grand") and jobs:
            g = tok["grand"]
            qn = self.waiting_total(active)
            qs = f" · ? {qn}" if qn else ""  # never cut by the fit: the count is what the owner scans the header for
            self.emit(p=3, text=fit(f"\033[1mALL JOBS\033[0m  tokens {hum(g['total']['all'])} · last 1h {hum(g['last_hour']['all'])}", self.cols - len(qs)) + qs)
            self.emit(p=1, text=fit(f"          effective {hum(g['total'].get('effective', 0))} (${g['total'].get('usd', 0):.2f}) · last 1h {hum(g['last_hour'].get('effective', 0))} (${g['last_hour'].get('usd', 0):.2f})", self.cols))
            self.emit(p=1, text=fit(f"\033[2m          {split(g['total'])}\033[0m", self.cols))
            self.emit("", p=2)

        cols = self.cols
        self.jobs_all = jobs
        self.waiting_merge_jobs = [j for j in done if self._unmerged(j) or self._unwritten(j) or j.get("owner_decisions")]
        grouped = self.queue_groups(active, done)
        grouped_ids = {i for g in grouped for i in g["members"]}
        unwritten = {j.get("job_id", ""): self._unwritten(j) for j in done}
        waiting_merge = [j for j in done if self._unmerged(j) or unwritten.get(j.get("job_id", ""))
                         or j.get("owner_decisions")]
        done = [j for j in done if j not in waiting_merge and j.get("job_id", "") not in grouped_ids]
        done.sort(key=lambda j: j.get("finished_at") or "", reverse=True)  # newest first
        items = self.display_items(ordered, active, grouped, grouped_ids)
        self.number_rows(staged, items, waiting_merge, done)
        self.staged_group(staged)
        for kind, obj in items:
            if kind == "job":
                self.job_block(obj)
                self.queue_row(obj)
            else:
                self.group_block(obj, ordered)

        # accepted but not merged yet: the merge button must stay in view, never folded into "done";
        # so must the write-answers button of a merged job whose answers are not on the branch yet
        for j in waiting_merge:
            m = self.rowno.get(j.get("job_id", ""), 0)
            self.bind_key(m, tab_of(j), f"@explain_more:{j.get('job_id', '')}")
            self.done_line(j, m)
            if j.get("owner_decisions"):
                self.carried_lines(j)
            if unwritten.get(j.get("job_id", "")):
                self.write_answers_row(j.get("job_id", ""), *unwritten[j.get("job_id", "")])

        if done:
            total = [self.usd_of(j.get("job_id", "")) for j in done]
            cost = f" · ${sum(t for t in total if t is not None):.2f}" if any(t is not None for t in total) else ""
            opened = dashact.is_expanded(state_, DONE_KEY)
            self.cmap.append(f"expand {len(self.out)} {DONE_KEY}")
            self.emit(f"    \033[32m✓\033[0m done ({len(done)}){cost} · \033[2mtap to {'hide' if opened else 'show'}\033[0m")
            if opened:
                for j in done:
                    m = self.rowno.get(j.get("job_id", ""), 0)
                    self.bind_key(m, tab_of(j), f"@explain_more:{j.get('job_id', '')}")
                    self.done_line(j, m)
            elif refresh:
                for j in done:
                    self.incorporated(j.get("job_id", ""))
        self.fit_height()
        self.out = style_clickable(self.out, self.cmap)
        return self.out, self.cmap

    def display_items(self, ordered, active, grouped, grouped_ids):
        """The live rows in pane order: `("job", j)` for a lone job, `("group", g)` for a queue / implicit group, which
        sits where its running / needs-you member would (else its first member)."""
        at = {j.get("job_id", ""): i for i, j in enumerate(ordered)}
        by_id = {j.get("job_id", ""): j for j in ordered}
        items = [(at[j.get("job_id", "")], "job", j) for j in active if j.get("job_id", "") not in grouped_ids]
        for g in grouped:
            ms = [by_id[i] for i in g["members"] if i in by_id and by_id[i].get("state") not in DONE]
            front = [m for m in ms if m.get("state") in ("RUNNING", "NEEDS_HUMAN", "PAUSED")] or ms
            items.append((min((at[m.get("job_id", "")] for m in front), default=len(ordered)), "group", g))
        items.sort(key=lambda t: t[0])
        return [(k, o) for _, k, o in items]

    def number_rows(self, staged, items, waiting_merge, done):
        """A stable pane index [n] for EVERY row, in display order (fold state never shifts it; 1 and 2 belong to the
        pinned ops / steward tabs, so rows start at the first job position)."""
        self.rowno, self.grpno = {}, {}
        n = herdr_tabs.FIRST_JOB_POSITION
        by_id = {j.get("job_id", ""): j for j in self.jobs_all}

        def take():
            nonlocal n
            n += 1
            return n - 1

        for j in staged:
            self.rowno[j.get("job_id", "")] = take()
        for kind, obj in items:
            if kind == "job":
                self.rowno[obj.get("job_id", "")] = take()
                continue
            self.grpno[obj["root"]] = take()
            for i in obj["members"]:
                if i in by_id and by_id[i].get("state") not in DONE:
                    self.rowno[i] = take()
        for j in [*waiting_merge, *done]:
            self.rowno[j.get("job_id", "")] = take()

    def staged_group(self, staged):
        """Jobs in CREATED (baseline still running) or CONTRACTED (ready) that nobody launched and that belong to no
        group: one row each, `[ ▶ play ]` only once CONTRACTED. Older than STALE_S they get a dim `stale` tag."""
        if not staged:
            return
        self.emit(f"\033[1mstaged\033[0m ({len(staged)})", p=9)
        now = time.time()
        for j in staged:
            self.staged_row(j, now)

    def staged_row(self, j, now, ind=""):
        jid, state = j.get("job_id", ""), j.get("state", "")
        n = self.rowno.get(jid, 0)
        key = self.row_key(n)
        age = staged_age(j, now)
        ready = state == "CONTRACTED"
        what = "staged · ready" if ready else f"staged · baseline running {max(int((age or 0) // 60), 0)}m"
        stale = age is not None and age > STALE_S
        button = PLAY_BUTTON if ready else ""
        name = fit(j.get("name") or jid[:8], tabnames.NAME_CAP)
        plain = f"{ind}{key} {name}  {what}" + ("  stale" if stale else "")
        room = self.cols - len(button) - (2 if button else 0)
        plain = fit(plain, max(room, 8))
        text = plain.replace("  stale", "  \033[2mstale\033[0m") if plain.endswith("stale") else plain
        text = text.replace(what, f"\033[2m{what}\033[0m", 1) if what in text else text
        self.bind_key(n, "", f"@explain_more:{jid}")
        self.cmap.append(f"num {len(self.out)} {1 + len(ind)} {len(ind) + len(key)} @explain_more:{jid}")
        if button:
            start = vlen(plain) + 3
            self.cmap.append(f"btn {len(self.out)} {start} {start + len(button) - 1} play {jid} -")
            text += f"  {button}"
        self.emit(text, p=9)

    def fit_height(self):
        """The frame must fit the pane: if it scrolls, every tap lands on the wrong row. Drop the least important lines
        (lowest priority, latest first) until it fits, then renumber the click map. The pane draws 7 rows above this
        output and 1 note line below."""
        if not self.rows:
            return
        out, prio, cmap = self.out, self.prio, self.cmap
        budget = max(self.rows - 7 - 2, 5)
        keep = list(range(len(out)))
        while len(keep) > budget:
            worst = min(keep, key=lambda i: (prio[i], -i))
            if prio[worst] >= 9:
                break
            keep.remove(worst)
        if len(keep) < len(out):
            new_idx = {old: new for new, old in enumerate(keep)}
            self.out = [out[i] for i in keep]
            remapped = []
            for line in cmap:
                parts = line.split(" ")
                if parts[0] in ("row", "toggle", "btn", "expand", "more", "num", "chatbtn") and len(parts) > 1 and parts[1].isdigit():
                    if int(parts[1]) not in new_idx:
                        continue
                    parts[1] = str(new_idx[int(parts[1])])
                remapped.append(" ".join(parts))
            self.cmap = remapped


def click_regions(cmap):
    """`(spans, lines)` from the click map: `spans` {line: [(c0, c1)]} (1-based inclusive visible columns of every btn and
    toggle target), `lines` the set of printed lines that tap as a whole (row and expand targets)."""
    spans: dict = {}
    lines: set = set()
    for ln in cmap:
        parts = ln.split(" ")
        if parts[0] in ("btn", "toggle", "chatbtn") and len(parts) > 3:
            spans.setdefault(int(parts[1]), []).append((int(parts[2]), int(parts[3])))
        elif parts[0] in ("row", "expand", "more") and len(parts) > 1 and parts[1].isdigit():
            lines.add(int(parts[1]))
    return spans, lines


def style_clickable(out, cmap):
    """`out` with the clickable style applied to exactly the cells the click map makes tappable."""
    by_line, _ = click_regions(cmap)
    styled = []
    for i, text in enumerate(out):
        spans = by_line.get(i, [])
        if not spans:
            styled.append(text)
            continue
        buf, col, applied, dim = [], 0, False, False
        for tok in jobview._TOKEN.findall(text):
            if tok.startswith("\033"):
                params = _SGR.fullmatch(tok).group(1).split(";")
                if "0" in params or params == [""] or "22" in params:
                    dim = False
                elif "2" in params:
                    dim = True
                buf.append(tok)
                if applied:  # a reset inside the span would drop the style: say it again
                    buf.append(CLICK_BUTTON)
                continue
            col += 1
            want = any(a <= col <= b for a, b in spans)
            if want != applied:
                if applied:
                    buf.append(CLICK_BUTTON_OFF + ("\033[2m" if dim else ""))
                if want:
                    buf.append(CLICK_BUTTON)
                applied = want
            buf.append(tok)
        if applied:
            buf.append(CLICK_BUTTON_OFF)
        styled.append("".join(buf))
    return styled


def styled_cells(line):
    """`{col: "button"}` for the cells of one rendered line that carry the clickable style (the inverse of
    `style_clickable`; tests compare it with the click map). Underline is never used, so any `4` is reported."""
    cells, col, button, underline = {}, 0, False, False
    for tok in jobview._TOKEN.findall(line):
        if tok.startswith("\033"):
            for p in _SGR.fullmatch(tok).group(1).split(";"):
                if p in ("0", ""):
                    button = underline = False
                elif p == "96":
                    button = True
                elif p == "39":
                    button = False
                elif p == "4":
                    underline = True
                elif p == "24":
                    underline = False
            continue
        col += 1
        if button:
            cells[col] = "button"
        elif underline:
            cells[col] = "underline"
    return cells


def main() -> int:
    """The one-shot script: print the lines, write the click map to $STEWARD_DASH_MAP (kept for tests and tooling)."""
    pane = Pane(int(os.environ.get("STEWARD_DASH_COLS") or 80), int(os.environ.get("STEWARD_DASH_ROWS") or 0),
                dashact.state_path())
    out, cmap = pane.render(refresh=True)
    print("\n".join(out))
    map_path = os.environ.get("STEWARD_DASH_MAP")
    if map_path:
        with open(map_path, "w") as f:
            f.write("\n".join(cmap) + "\n")
    return 0
