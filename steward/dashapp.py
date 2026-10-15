"""The resident Steward status pane: one long-running process that owns the terminal (`deploy/cockpit/steward-dash`).

It used to be a bash loop that started a fresh Python per redraw and looked at input only between redraws. Now:
  * the UI thread owns the terminal (alternate screen, SGR mouse, no echo, cursor hidden, all restored on exit),
    waits in select() and handles every tap the moment it arrives;
  * a refresh thread re-reads the data (jobs ~1 s, tokens 10 s) into a shared cache, a probe thread the services (5 s)
    and the public connector (30 s); the store is opened once;
  * every paint is built from the cache, so local taps (fold, panels, done group) repaint without a read, and a tap
    that calls steward shows `… <what>` at once and the real outcome when it lands;
  * only the rows that changed are written.
Click-map rules are unchanged: the renderer (steward.dashrender) emits the same map the bash loop consumed.
"""

from __future__ import annotations

import os
import queue
import re
import select
import signal
import subprocess
import sys
import threading
import time

from steward import dash_services, dashact, installstate
from steward.dashrender import ANSI, MORE_PREFIX, Cache, Miss, Pane, fit
from steward.roles import role_label

SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ENTER = "\033[?1049h\033[?1000h\033[?1006h\033[?25l"  # alt screen, mouse clicks (SGR encoding) on, cursor off
LEAVE = "\033[?1000l\033[?1006l\033[?25h\033[?1049l"
# Only a well-formed SGR left-button PRESS acts: ESC [ < 0 ; col ; row M. Everything else (focus events, releases,
# other buttons) is ignored.
_SGR = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
_CSI = re.compile(rb"\x1b\[[0-9;<?]*[A-Za-z~]")
DOUBLE_TAP = 0.4  # seconds: two taps on the same cell within this are a double tap (opens a job's tab)
_DONE_LINE = re.compile(r"^([0-9a-f]{8})[0-9a-f-]{28}\s+([A-Z_]+)$")  # "<job id>  ACCEPTED" from accept/release


def run_command(argv: list[str]) -> tuple[int, str]:
    """Run one steward/herdr command: (exit code, last output line); the last line is the outcome note."""
    try:
        res = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError as exc:
        return 127, f"{argv[0]}: {exc.strerror or exc}"
    lines = res.stdout.strip().splitlines()
    return res.returncode, lines[-1] if lines else "done"


def probe_services() -> dict:
    out = {}
    for unit in dash_services.unit_names(installstate.load()):
        try:
            r = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True, timeout=5)
            out[unit] = r.stdout.strip() or "inactive"
        except (OSError, subprocess.SubprocessError):
            out[unit] = "unknown"
    return out


def probe_public() -> str:
    """The configured connector's HTTP status, or '' when none is configured (the URL comes from mcp.json, never code)."""
    url = dash_services.public_probe_url()
    if not url:
        return ""
    try:
        r = subprocess.run(["curl", "-s", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}", url],
                           capture_output=True, text=True, timeout=8)
        return r.stdout.strip() or "-"
    except (OSError, subprocess.SubprocessError):
        return "-"


def packaged_services(pub: str) -> list[str]:
    """The services block `steward dash` showed under the bash loop (`python -m steward.dash_services`); `pub` is
    the connector probe's HTTP status."""
    try:
        state = installstate.load()
    except installstate.InstallStateError:
        state = None
    return dash_services.lines(state, probe=lambda _url: pub or "-")


def data_version_probe():
    """`PRAGMA data_version` on a read connection of its own: it changes whenever another connection commits."""
    import sqlite3

    from steward.store import resolve_home

    conn = None

    def probe() -> int:
        nonlocal conn
        if conn is None:
            conn = sqlite3.connect(f"file:{resolve_home() / 'steward.db'}?mode=ro", uri=True, check_same_thread=False)
        return conn.execute("PRAGMA data_version").fetchone()[0]
    return probe


class App:
    def __init__(self, in_fd=0, out_fd=1, *, state_path=None, cols=None, rows=None, run=run_command,
                 services=probe_services, public=probe_public, pane_kwargs=None, jobs_every=1.0, services_every=5.0,
                 public_every=30.0, reexec_file=None, exec_fn=os.execv, services_block=None, db_version=None,
                 idle_every=5.0):
        self.in_fd, self.out_fd = in_fd, out_fd
        self.run_cmd = run
        self.services_fn, self.public_fn = services, public
        # `steward dash` (the packaged pane) shows dash_services' block: services_block(public_status) -> lines.
        self.services_block = services_block
        # db_version() -> PRAGMA data_version of a connection of its own (None: no gate). While no commit lands and
        # nothing was tapped, the per-pass data is refetched only every `idle_every` s (the old bash loop's cadence).
        self.db_version, self.idle_every = db_version, idle_every
        self.jobs_every, self.services_every, self.public_every = jobs_every, services_every, public_every
        self.state_path = state_path
        self._fixed_size = (cols, rows) if cols else None
        self.cache = Cache()
        kw = dict(state_path=state_path, cache=self.cache, **(pane_kwargs or {}))
        self.pane = Pane(**kw)      # the UI thread's renderer (passive: reads the cache)
        self.fresh = Pane(**kw)     # the refresh thread's renderer
        self.cols, self.rows = self._size()
        self.pane.cols = self.fresh.cols = self.cols
        self.pane.rows = self.fresh.rows = self.rows
        self.events: queue.Queue = queue.Queue()   # callables that run on the UI thread (action outcomes)
        self.wake_r, self.wake_w = os.pipe()
        self.stop = threading.Event()
        self.kick = threading.Event()              # "refresh the jobs now"
        self.note = ""
        self.pending_confirm = ""
        self.clock = time.monotonic
        self.pending_tap = None                    # (col, row, deadline, fn, tab): a single tap on a [n] waiting out a double tap
        self.lines: list[str] = []                 # the last body lines drawn (for diffing and hit-testing)
        self.drawn: list[str] = []                 # the last full frame, row by row
        self.cmap = _Map()
        self.buf = b""
        self.seen_version = -1
        self.last_tick = 0
        self.last_paint_at = 0.0
        self._resized = False
        self.paints = 0                            # frames built (tests)
        self.writes = 0
        self.first_data = threading.Event()
        self.threads: list[threading.Thread] = []
        self.lock = threading.Lock()
        self.started_at = time.time()
        self.reexec_file = reexec_file
        self.exec_fn = exec_fn

    def check_reexec(self) -> None:
        """`steward deploy-self` touches the signal file: re-exec in place (same pid/pane) onto the new code."""
        try:
            fresh = self.reexec_file is not None and os.stat(self.reexec_file).st_mtime > self.started_at
        except OSError:
            return
        if fresh:
            self.restore_terminal()
            from steward import selfrestart

            argv = selfrestart.original_argv()
            try:
                self.exec_fn(argv[0], argv)
            except OSError as exc:  # a failed reload must never kill the pane: stay on the old code and say so
                self.started_at = time.time()
                self.setup_terminal()
                self.note = f"reload failed ({exc.__class__.__name__}); still on the previous code"

    # -- terminal ----------------------------------------------------------------------------------------------
    def _size(self):
        if self._fixed_size:
            return self._fixed_size
        try:
            sz = os.get_terminal_size(self.out_fd)
            return sz.columns, sz.lines
        except OSError:
            return int(os.environ.get("STEWARD_DASH_COLS") or 80), int(os.environ.get("STEWARD_DASH_ROWS") or 0)

    def write(self, text: str) -> None:
        data = text.encode()
        while data:
            n = os.write(self.out_fd, data)
            data = data[n:]
        self.writes += 1

    def setup_terminal(self):
        self.saved_tty = None
        if os.isatty(self.in_fd):
            import termios

            self.saved_tty = termios.tcgetattr(self.in_fd)
            new = termios.tcgetattr(self.in_fd)
            new[3] &= ~(termios.ECHO | termios.ICANON)  # no echo: stray mouse bytes are never printed; byte-at-a-time
            new[6][termios.VMIN], new[6][termios.VTIME] = 1, 0
            termios.tcsetattr(self.in_fd, termios.TCSADRAIN, new)
        self.write(ENTER)
        self.drain_input(0.1)

    def restore_terminal(self):
        try:
            self.write(LEAVE)
        finally:
            if self.saved_tty is not None:
                import termios

                termios.tcsetattr(self.in_fd, termios.TCSADRAIN, self.saved_tty)

    def drain_input(self, quiet: float) -> None:
        """Drop input queued before we started (no phantom clicks): read until nothing arrives for `quiet` seconds."""
        while select.select([self.in_fd], [], [], quiet)[0]:
            if not os.read(self.in_fd, 4096):
                break

    # -- background work ---------------------------------------------------------------------------------------
    def wake(self) -> None:
        try:
            os.write(self.wake_w, b"x")
        except OSError:
            pass

    def post(self, fn) -> None:
        self.events.put(fn)
        self.wake()

    def _jobs_rows(self) -> int:
        """The renderer budgets for 5 service rows (plus the blank line after them); give it the real count."""
        block = (self.cache.get("svcblock") or (0, True, None))[2]
        if self.services_block is None or block is None or not self.rows:
            return self.rows
        return max(self.rows - (len(block) + 1 - 5), 1)

    def refresh_loop(self) -> None:
        seen, kicked = None, True
        while not self.stop.is_set():
            self.fresh.cols, self.fresh.rows = self.cols, self._jobs_rows()
            if self.db_version is not None:
                try:
                    now = self.db_version()
                except Exception:  # noqa: BLE001 - no gate this pass: refetch as usual
                    now = None
                idle = not kicked and now is not None and now == seen
                self.fresh.jobs_ttl = self.idle_every if idle else 1.0
                seen = now
            kicked = False
            try:
                self.fresh.render(refresh=True)
            except Miss:
                pass
            except Exception:  # noqa: BLE001 - a bad pass never kills the refresher
                pass
            self.first_data.set()
            self.wake()
            done = time.monotonic()
            while not self.stop.is_set() and time.monotonic() < done + self.jobs_every:  # re-read: a new cadence applies at once
                if self.kick.wait(0.05):
                    self.kick.clear()
                    kicked = True
                    break

    def probe_loop(self) -> None:
        last_pub = -1e9
        while not self.stop.is_set():
            if self.services_block is None:
                self.cache.put("services", True, self.services_fn())
            if time.monotonic() - last_pub >= self.public_every:
                self.cache.put("public", True, self.public_fn())
                last_pub = time.monotonic()
            if self.services_block is not None:
                pub = (self.cache.get("public") or (0, True, "-"))[2]
                try:
                    block = list(self.services_block(pub))
                except Exception as exc:  # noqa: BLE001 - a probe bug shows as a line; it never kills the pane
                    block = [f"  \033[31m●\033[0m services: {type(exc).__name__}"]
                self.cache.put("svcblock", True, block)
            self.wake()
            self.stop.wait(self.services_every)

    def start_threads(self) -> None:
        for fn in (self.refresh_loop, self.probe_loop):
            t = threading.Thread(target=fn, daemon=True, name=fn.__name__)
            t.start()
            self.threads.append(t)

    def background(self, pending: str, argv: list[str], outcome=None) -> None:
        """Run `argv` off the UI thread: note `pending` now, the real outcome when it lands (`outcome(rc)` may replace
        the command's own last line)."""
        self.note = pending
        self.paint()

        def work():
            rc, out = self.run_cmd(argv)
            self.post(lambda: self.finished((outcome(rc) if outcome else None) or out))

        threading.Thread(target=work, daemon=True).start()

    def finished(self, out: str) -> None:
        m = _DONE_LINE.match(out)
        self.note = "→ " + (f"✓ {m.group(2).lower()} {m.group(1)}" if m else out)
        self.kick.set()  # the outcome is in: re-read the rows now so switches show their new state
        self.paint()

    # -- taps --------------------------------------------------------------------------------------------------
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
                    continue
                if self.buf[1:2] in (b"", b"[") and len(self.buf) < 32:
                    return  # an incomplete sequence: wait for the rest
                self.buf = self.buf[1:]  # a lone ESC (or garbage), not a sequence
                continue
            ch = self.buf[:1]
            self.buf = self.buf[1:]
            if ch in b"123456789":
                self.goto(self.cmap.keys.get(ch.decode(), ""))

    def tap_deadline(self):
        return self.pending_tap[2] if self.pending_tap else None

    def flush_tap(self, force: bool = False) -> None:
        """Run the single tap that waited for a possible second one (when its window is over, or `force`)."""
        p = self.pending_tap
        if p and (force or self.clock() >= p[2]):
            self.pending_tap = None
            if p[3]:
                p[3]()

    def click(self, col: int, row: int) -> None:
        p = self.pending_tap
        if p and (p[0], p[1]) == (col, row) and self.clock() < p[2]:
            self.pending_tap = None  # the second tap on the same cell: a double tap
            self.goto(p[4])
            return
        self.flush_tap(force=True)  # any other tap: the waiting single tap acts now
        self.click_now(col, row)

    def click_now(self, col: int, row: int) -> None:
        cm = self.cmap
        note_row = len(self.drawn)
        # The note under an armed button is itself the confirm target (people tap the words they read).
        if row == note_row and self.pending_confirm:
            action, job, appr = self.pending_confirm.split(" ", 2)
            self.pending_confirm = ""
            self.press(action, job, appr)
            return
        tog = cm.toggles.get(row)
        if tog and tog[0] <= col <= tog[1]:
            _, _, job, role, nxt = tog
            self.background(f"… switching {role} to {nxt} for {job[:8]}", ["python3", "-m", "steward.cli", "supervisor", job, role, "toggle"])
            return
        num = cm.nums.get(row)
        if num and num[0] <= col <= num[1]:  # the [n]: single tap folds (after the window), double tap opens the tab
            fold = next(((a, j, ap) for b0, b1, a, j, ap in cm.btns.get(row, ()) if a == "fold" and b0 <= col <= b1), None)
            self.pending_tap = (col, row, self.clock() + DOUBLE_TAP, (lambda: self.press(*fold)) if fold else None, num[2])
            return
        for b0, b1, action, job, appr in cm.btns.get(row, ()):
            if b0 <= col <= b1:
                self.press(action, job, appr)
                return
        for c0, c1, job, role in cm.chatbtns.get(row, ()):  # a supervisor row's [ chat ] button
            if c0 <= col <= c1:
                self.chat(job, role)
                return
        if row in cm.expands:
            dashact.toggle_expand(self.state_path, cm.expands[row])
            self.paint()
            return
        if row in cm.mores:  # a line cut with "…": show it in full / cut it again
            dashact.toggle_expand(self.state_path, MORE_PREFIX + cm.mores[row])
            self.paint()
            return
        verb, x, y = (cm.rows.get(row, "").split(" ") + ["", "", ""])[:3]
        if verb == "focus" and x:
            self.goto(x)
        elif verb == "chat" and x and y:
            self.chat(x, y)
        elif verb == "jobchat":
            if x:
                self.goto(x)
            if y:
                self.chat(y, "job")

    def goto(self, tab: str) -> None:
        if tab.startswith("@"):  # a row with no tab: `@action:target` (unfold it / open its explanation)
            action, _, target = tab[1:].partition(":")
            self.press(action, target, "-")
            return
        if tab:
            self.background(f"… opening {tab}", ["herdr", "tab", "focus", tab],
                            lambda rc: f"opened {tab}" if rc == 0 else f"couldn't open {tab}")

    def chat(self, job: str, role: str) -> None:
        self.background(f"… opening {role_label(role)} chat for {job[:8]}", ["python3", "-m", "steward.cli", "chat", job, role])

    def press(self, action: str, job: str, approval: str) -> None:
        """Only the bulk answers (accept all ★, accept ★ + accept) need a second tap; everything else runs at once."""
        if action in dashact.CONFIRM:
            self.note = f"… {action} {job[:8]}"
            self.paint()

            def work():
                review = dashact._review(job)
                status, cmd = dashact.press(self.state_path, action, job, "" if approval == "-" else approval, review=review)
                self.post(lambda: self.pressed(status, cmd, action, job, approval))

            threading.Thread(target=work, daemon=True).start()
            return
        try:
            status, cmd = dashact.press(self.state_path, action, job, "" if approval == "-" else approval)
        except ValueError:
            self.note = f"couldn't {action} {job[:8]}"
            self.paint()
            return
        self.pressed(status, cmd, action, job, approval)

    def pressed(self, status: str, cmd: list[str], action: str, job: str, approval: str) -> None:
        if status == "armed":
            review = cmd[0] if cmd else ""
            label = {"accept_rec": "accept all ★", "accept_rec_accept": "accept ★ + accept"}.get(action, action)
            self.note = (f"→ {review} — tap again (or [ confirm {label} ]) within 20s" if review
                         else f"→ tap here (or [ confirm {label} ]) within 20s to {action}")
            self.pending_confirm = f"{action} {job} {approval}"
            self.paint()
            return
        if status == "busy":  # a second tap on a launch already in flight: nothing to do
            self.note = f"→ {job[:8]} is already launching"
            self.paint()
            return
        if cmd[:4] == ["python3", "-m", "steward.dashact", "expand"]:  # fold / incorporate panel / cancel: local, instant
            dashact.toggle_expand(self.state_path, cmd[4])
            self.note = ""
            self.paint()
            return
        self.background(f"… launching {job[:8]}" if action == "play" else f"… {action} {job[:8]}", cmd)

    # -- drawing -----------------------------------------------------------------------------------------------
    def frame(self) -> list[str]:
        """The full screen, row by row: header, blank, services, blank, the jobs output, the note."""
        self.pane.cols, self.pane.rows = self.cols, self._jobs_rows()
        self.pane.passive = True
        try:
            try:
                out, cmap = self.pane.render()
            except Miss:
                self.pane.passive = False  # something an unfold needs was never loaded: read it now, once
                try:
                    out, cmap = self.pane.render()
                finally:
                    self.pane.passive = True
        except Exception as exc:  # noqa: BLE001 - a render bug shows as a line; it never kills the pane
            out, cmap = [f"\033[31m⚠ pane render error: {type(exc).__name__}: {str(exc)[:80]} (still running)\033[0m"], []
            self.pane.passive = True
        spin = SPIN[int(time.time()) % len(SPIN)]
        head = f"\033[1mSTEWARD\033[0m  {time.strftime('%H:%M:%S')} {spin}  \033[2mclick or 1-9 to open\033[0m"
        svc = (self.cache.get("services") or (0, True, None))[2]
        pub = (self.cache.get("public") or (0, True, "-"))[2]
        rows = [head, ""]
        block = (self.cache.get("svcblock") or (0, True, None))[2] if self.services_block is not None else None
        if block is not None:
            rows.extend(block)
        else:
            for unit, s in (svc or {}).items():
                rows.append(f"  \033[{32 if s == 'active' else 31}m●\033[0m {unit:<26} {s}")
            if pub == "":  # no connector configured: a dim row, so the block keeps its height
                rows.append(f"  \033[2m○ {'public connector':<26} off (no public connector)\033[0m")
            else:
                rows.append(f"  \033[{32 if pub == '200' else 31}m●\033[0m {'public connector':<26} {pub}")
        rows.append("")
        row0 = len(rows) + 1  # 1-based screen row of the jobs output's first line
        rows.extend(ln.replace("{SPIN}", spin) for ln in out)
        note = self.note
        if self.cols and len(note) > self.cols:
            note = fit(note, self.cols)
        rows.append(f"\033[2m{note}\033[0m")
        self.cmap = _Map.parse(cmap, row0)
        self.lines = out
        return rows

    def paint(self) -> None:
        """Repaint from the cache; writes only the rows that changed."""
        if not self.first_data.is_set():
            rows = [f"\033[1mSTEWARD\033[0m  \033[2mloading…\033[0m"]
        else:
            rows = self.frame()
        self.seen_version, self.last_tick = self.cache.version, int(time.time())
        buf = []
        if not self.drawn or self._resized:
            buf.append("\033[2J")
            self.drawn = []
            self._resized = False
        buf.extend(f"\033[{i + 1};1H\033[K" for i in range(len(rows), len(self.drawn)))  # rows the frame no longer has
        for i, ln in enumerate(rows):
            if i >= len(self.drawn) or self.drawn[i] != ln:
                buf.append(f"\033[{i + 1};1H{ln}\033[0m\033[K")
        if buf:
            self.write("".join(buf))
        self.drawn = rows
        self.last_paint_at = time.monotonic()
        self.paints += 1  # last: a reader that sees the count move also sees this frame's rows and time

    # -- main loop ---------------------------------------------------------------------------------------------
    def drain_events(self) -> None:
        while True:
            try:
                self.events.get_nowait()()
            except queue.Empty:
                return

    def run(self) -> int:
        self.setup_terminal()
        self.start_threads()
        try:
            self.paint()
            while not self.stop.is_set():
                timeout = max(1.0 - (time.time() % 1.0), 0.01)  # next whole second: the spinner ticks
                due = self.tap_deadline()
                if due is not None:
                    timeout = min(timeout, max(due - self.clock(), 0.0) + 0.005)
                ready = select.select([self.in_fd, self.wake_r], [], [], timeout)[0]
                if self.wake_r in ready:
                    os.read(self.wake_r, 4096)
                if self.in_fd in ready:
                    data = os.read(self.in_fd, 4096)
                    if not data:
                        break
                    self.feed(data)
                self.flush_tap()
                self.check_reexec()
                self.drain_events()
                size = self._size()
                if size != (self.cols, self.rows):
                    self.cols, self.rows = size
                    self._resized = True
                    self.kick.set()
                    self.paint()
                    continue
                if self.cache.version != self.seen_version or int(time.time()) != self.last_tick:
                    self.paint()
        finally:
            self.stop.set()
            self.restore_terminal()
        return 0


class _Map:
    """The click map in screen coordinates (1-based rows) built from the renderer's `cmap` lines."""

    def __init__(self):
        self.keys, self.rows, self.toggles, self.btns, self.expands = {}, {}, {}, {}, {}
        self.mores, self.nums, self.chatbtns = {}, {}, {}

    @classmethod
    def parse(cls, lines: list[str], row0: int) -> "_Map":
        m = cls()
        for ln in lines:
            kind, *a = ln.split(" ")
            if kind == "key":
                m.keys[a[0]] = a[1]
                continue
            row = row0 + int(a[0])
            if kind == "row":
                m.rows[row] = " ".join(a[1:])
            elif kind == "expand":
                m.expands[row] = a[1]
            elif kind == "more":
                m.mores[row] = a[1]
            elif kind == "num":
                m.nums[row] = (int(a[1]), int(a[2]), a[3])
            elif kind == "chatbtn":
                m.chatbtns.setdefault(row, []).append((int(a[1]), int(a[2]), a[3], a[4]))
            elif kind == "btn":
                m.btns.setdefault(row, []).append((int(a[1]), int(a[2]), a[3], a[4], a[5]))
            elif kind == "toggle":
                m.toggles[row] = (int(a[1]), int(a[2]), a[3], a[4], a[5])
        return m


def main() -> int:
    repo = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    os.chdir(os.environ.get("STEWARD_DASH_HOME") or repo)
    map_path = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", f"steward-dash.{os.getpid()}.map")
    state_path = dashact.state_path(map_path)
    os.environ["STEWARD_DASH_MAP"] = map_path
    from steward import deploy_self

    packaged = bool(os.environ.get("STEWARD_INSTALL_ROOT"))  # started by `steward dash` (the packaged pane)
    app = App(state_path=state_path, reexec_file=deploy_self.signal_path(),
              services_block=packaged_services if packaged else None, db_version=data_version_probe())

    def bye(*_):
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, bye)
    signal.signal(signal.SIGWINCH, lambda *_: app.wake())
    try:
        return app.run()
    except KeyboardInterrupt:
        return 0
    finally:
        for suffix in ("", ".note", ".expanded", ".expanded.tmp"):
            try:
                os.unlink(map_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
