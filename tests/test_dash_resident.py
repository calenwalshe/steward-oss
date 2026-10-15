"""The resident status pane (steward/dashapp.py): taps paint from the cache without a data read, data refreshes in the
background, the terminal is restored on exit. Driven in-process over pipes (and a pty for the terminal), fake runners."""
import os
import pty
import re
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from steward import dashact
from steward.dashapp import DOUBLE_TAP, ENTER, LEAVE, App
from steward.models import JobState
from steward.store import Store

_REPO = Path(__file__).resolve().parent.parent
ANSI = re.compile(r"\033\[[0-9;]*[A-Za-z]")


def _job(store, goal="Ship the widget", state=JobState.RUNNING, tab="w1:t2"):
    job = store.create_job(goal, "/tmp/somerepo")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job.job_id))
    store.kv_set(job.job_id, "herdr_workspace", "w1")
    store.kv_set(job.job_id, "herdr_tab", tab)
    store.kv_set(job.job_id, "worker_model", "claude-sonnet-5-5")
    return job


class Rig:
    """An App on pipes: `tap(col, row)` writes a click, `screen()` replays the bytes it painted."""

    def __init__(self, tmp_path, **kw):
        self.in_r, self.in_w = os.pipe()
        self.out_r, self.out_w = os.pipe()
        os.set_blocking(self.out_r, False)
        self.state = dashact.state_path(str(tmp_path / "map"))
        self.ran = []
        self.outcomes = {}
        kw.setdefault("run", self.run)
        kw.setdefault("services", lambda: {"steward-mcp": "active", "steward-tunnel": "active", "steward-mcp-tunnel-local": "active"})
        kw.setdefault("public", lambda: "200")
        self.app = App(self.in_r, self.out_w, state_path=self.state, cols=100, rows=0, **kw)
        self.thread = None

    def run(self, argv):
        self.ran.append(argv)
        time.sleep(self.outcomes.get("delay", 0))
        return 0, self.outcomes.get("out", "done")

    def start(self):
        self.thread = threading.Thread(target=self.app.run, daemon=True)
        self.thread.start()
        return self

    def tap(self, col, row):
        os.write(self.in_w, f"\033[<0;{col};{row}M".encode())

    def key(self, ch):
        os.write(self.in_w, ch.encode())

    def read(self):
        out = b""
        while True:
            try:
                chunk = os.read(self.out_r, 65536)
            except BlockingIOError:
                return out.decode()
            if not chunk:
                return out.decode()
            out += chunk

    def wait_for(self, pred, timeout=5.0):
        """Accumulate painted bytes until `pred(text)`; returns (text, seconds)."""
        t0, text = time.monotonic(), ""
        while time.monotonic() - t0 < timeout:
            text += self.read()
            if pred(text):
                return text, time.monotonic() - t0
            time.sleep(0.002)
        raise AssertionError(f"timed out; painted so far: {ANSI.sub('', text)[-400:]!r}")

    def row_of(self, needle):
        for i, ln in enumerate(self.app.drawn, 1):
            if needle in ANSI.sub("", ln):
                return i
        raise AssertionError(f"{needle!r} not on screen: {[ANSI.sub('', x) for x in self.app.drawn]}")

    def stop(self):
        self.app.stop.set()
        self.app.wake()
        self.thread.join(3)
        for fd in (self.in_r, self.in_w, self.out_r, self.out_w):
            os.close(fd)


@pytest.fixture
def rig(steward_home, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_HEALTH", "0")
    store = Store(steward_home)
    job = _job(store)
    r = Rig(tmp_path).start()
    r.job = job
    r.wait_for(lambda t: "RUNNING" in t)
    yield r
    r.stop()


def test_first_paint_shows_the_jobs_and_services(rig):
    shown = "\n".join(ANSI.sub("", ln) for ln in rig.app.drawn)
    assert "STEWARD" in shown and rig.job.job_id[:8] in shown and "steward-mcp" in shown


def _tap_and_wait(rig, col, row, settled, timeout=5.0):
    """Tap, then wait for the pane's next paint after which `settled()` holds; returns tap-to-paint seconds."""
    paints = rig.app.paints
    t0 = time.monotonic()
    rig.tap(col, row)
    while time.monotonic() - t0 < timeout:
        if rig.app.paints > paints and settled():
            return rig.app.last_paint_at - t0
        time.sleep(0.0005)
    raise AssertionError("tap never painted: " + repr([ANSI.sub("", x) for x in rig.app.drawn]))


def _folded(rig):
    return dashact.is_expanded(dashact.load(rig.state), dashact.FOLD_PREFIX + rig.job.job_id)


def test_fold_tap_repaints_without_a_data_read(rig):
    rig.app.jobs_every = 3600  # the background refresh is idle: any read during the tap would be the tap's own
    time.sleep(1.2)
    # on a busy box the startup pass can still be reading: wait until reads have been quiet for 0.6 s (max 15 s)
    deadline, last, quiet_since = time.monotonic() + 15, rig.app.cache.fetches, time.monotonic()
    while time.monotonic() < deadline and time.monotonic() - quiet_since < 0.6:
        time.sleep(0.05)
        if rig.app.cache.fetches != last:
            last, quiet_since = rig.app.cache.fetches, time.monotonic()
    before_rows = len(rig.app.drawn)
    fetches = rig.app.cache.fetches
    row = rig.row_of(rig.job.job_id[:8])
    # a single tap on [n] waits out the double-tap window, then folds from the cache
    dt = _tap_and_wait(rig, 2, row, lambda: _folded(rig) and len(rig.app.drawn) < before_rows)
    # the fold waits out the double-tap window; the upper bound is generous because the suite runs on a busy box
    # (a 100 ms bound failed 2 in 3 at load 110). What matters is the next assert: no data read.
    assert DOUBLE_TAP <= dt < DOUBLE_TAP + 1.5, f"fold tap-to-paint {dt * 1000:.0f} ms"
    assert rig.app.cache.fetches == fetches, "a fold tap must repaint from the cache"
    dt = _tap_and_wait(rig, 2, row, lambda: not _folded(rig) and len(rig.app.drawn) == before_rows)  # unfold
    assert rig.app.cache.fetches == fetches and dt < DOUBLE_TAP + 1.5


def _accept_job(store):
    from steward import approvals

    job = _job(store, "Fix the parser bug", JobState.NEEDS_HUMAN, tab="w1:t3")
    approvals.open_approval(store, job.job_id, approvals.ACCEPTANCE_QUESTION, kind="acceptance")
    return job


def _button(rig, job, action):
    for row, btns in rig.app.cmap.btns.items():
        for b0, b1, act, jid, appr in btns:
            if act == action and jid == job.job_id:
                return b0 + 1, row
    raise AssertionError(f"no {action} button: {rig.app.cmap.btns}")


def test_accept_shows_a_pending_note_then_the_outcome(steward_home, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_HEALTH", "0")
    job = _accept_job(Store(steward_home))
    rig = Rig(tmp_path)
    rig.outcomes.update(delay=0.3, out=f"{job.job_id}  ACCEPTED")
    rig.start()
    try:
        rig.wait_for(lambda t: "NEEDS_HUMAN" in t)
        ask_row = next(r for r, j in rig.app.cmap.expands.items() if j == job.job_id)
        _tap_and_wait(rig, 6, ask_row, lambda: dashact.is_expanded(dashact.load(rig.state), job.job_id))
        col, row = _button(rig, job, "accept")
        dt = _tap_and_wait(rig, col, row, lambda: rig.app.note.startswith("… accept"))
        assert dt < 0.05, f"accept tap-to-pending-note {dt * 1000:.0f} ms"
        # the note is set at once; its repaint can land a moment later on a loaded box
        deadline = time.monotonic() + 3
        while "…" not in ANSI.sub("", rig.app.drawn[-1] if rig.app.drawn else "") and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "…" in ANSI.sub("", rig.app.drawn[-1])
        rig.wait_for(lambda t: rig.app.note == f"→ ✓ accepted {job.job_id[:8]}")
        assert rig.ran == [["python3", "-m", "steward.cli", "accept", job.job_id, "--origin", "dash"]]
    finally:
        rig.stop()


def _chats(rig):
    return [a for a in rig.ran if a[:4] == ["python3", "-m", "steward.cli", "chat"]]


def test_single_tap_on_header_goal_or_tokens_never_navigates(rig):
    time.sleep(1.2)
    for needle in (rig.job.job_id[:8], "Ship the widget"):
        row = rig.row_of(needle)
        for col in (6, 20, 30):  # [n] excluded: its single tap is a fold
            rig.tap(col, row)
    time.sleep(DOUBLE_TAP + 0.3)
    assert rig.ran == [], rig.ran


def test_double_tap_on_number_focuses_the_tab_and_does_not_fold(rig):
    time.sleep(1.2)
    row = rig.row_of(rig.job.job_id[:8])
    rig.tap(2, row)
    rig.tap(2, row)
    rig.wait_for(lambda t: "→ opened w1:t2" in ANSI.sub("", t))
    time.sleep(DOUBLE_TAP + 0.3)  # the window passes: no fold follows the double tap
    assert rig.ran == [["herdr", "tab", "focus", "w1:t2"]] and not _folded(rig)


def test_taps_on_different_cells_are_not_a_double_tap(rig):
    time.sleep(1.2)
    row = rig.row_of(rig.job.job_id[:8])
    rig.tap(2, row)
    rig.tap(3, row)  # a different cell: the first folds at once, the second starts a new window
    rig.wait_for(lambda t: _folded(rig))
    assert rig.ran == []


def test_supervisor_chat_opens_only_from_its_chat_button(rig):
    time.sleep(1.2)
    rows = list(rig.app.cmap.chatbtns)
    assert rows, rig.app.cmap
    row = rows[0]
    rig.tap(10, row)  # the row's text
    time.sleep(0.2)
    assert rig.ran == []
    b0, b1, job, role = rig.app.cmap.chatbtns[row][0]
    rig.tap(b0 + 1, row)
    rig.wait_for(lambda t: bool(_chats(rig)))
    assert _chats(rig)[0][4:] == [job, role]


def test_number_key_focuses_the_job_tab(rig):
    rig.key("3")  # the first job is tab 3: ops and steward are tabs 1 and 2
    rig.wait_for(lambda t: "→ opened w1:t2" in ANSI.sub("", t))
    assert rig.ran == [["herdr", "tab", "focus", "w1:t2"]]


def test_background_refresh_picks_up_store_changes_without_input(rig, steward_home):
    rig.app.jobs_every = 0.2
    second = _job(Store(steward_home), "A second job appears", tab="w1:t9")
    text, dt = rig.wait_for(lambda t: second.job_id[:8] in ANSI.sub("", t), timeout=6)
    assert dt < 3
    assert rig.app.cmap.keys.get("4") == "w1:t9"


def test_services_and_public_probe_show_up(steward_home, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_HEALTH", "0")
    rig = Rig(tmp_path, services=lambda: {"steward-mcp": "active", "steward-tunnel": "failed", "steward-mcp-tunnel-local": "active"},
              public=lambda: "503").start()
    try:
        rig.wait_for(lambda t: "failed" in ANSI.sub("", t) and "503" in ANSI.sub("", t))
    finally:
        rig.stop()


def test_idle_repaint_writes_only_the_changed_rows(rig):
    rig.app.jobs_every = 3600
    time.sleep(1.2)
    rig.read()
    full = sum(len(ln) for ln in rig.app.drawn)
    tick, _ = rig.wait_for(lambda t: len(t) > 0, timeout=3)  # the next spinner tick
    time.sleep(0.05)
    tick += rig.read()
    assert 0 < len(tick) < full
    assert "\033[2J" not in tick


def test_phantom_clicks_queued_before_start_are_dropped(steward_home, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_HEALTH", "0")
    _job(Store(steward_home))
    rig = Rig(tmp_path)
    for row in range(1, 30):
        rig.tap(2, row)
    rig.start()
    try:
        rig.wait_for(lambda t: "RUNNING" in ANSI.sub("", t))
        time.sleep(0.3)
        assert rig.ran == [] and not dashact.load(rig.state)["expanded"]
    finally:
        rig.stop()


def test_split_and_ignored_input_bytes(rig):
    rig.key("\033[<0;2")          # half a click: nothing yet
    time.sleep(0.05)
    row = rig.row_of(rig.job.job_id[:8])
    rig.key(f";{row}M")           # ...completed: folds
    end = time.monotonic() + 2
    while not _folded(rig) and time.monotonic() < end:
        time.sleep(0.01)
    assert _folded(rig)
    rig.key("\033[<0;2;%dm" % row)  # a release, focus-in, other button: ignored
    rig.key("\033[I\033[O\033[<2;2;%dM" % row)
    time.sleep(0.2)
    assert _folded(rig) and rig.ran == []


# -- terminal ownership ---------------------------------------------------------------------------------------------

def _spawn_in_pty(tmp_path, steward_home):
    import termios

    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    env = {**os.environ, "STEWARD_HOME": str(steward_home), "STEWARD_HEALTH": "0", "PYTHONPATH": str(_REPO),
           "XDG_RUNTIME_DIR": str(tmp_path)}
    import signal
    parent_sigint = signal.signal(signal.SIGINT, signal.default_int_handler)  # a background-launched suite has SIGINT=SIG_IGN, inherited across exec; a handler resets to default
    proc = subprocess.Popen([sys.executable, "-m", "steward.dashapp"], stdin=slave, stdout=slave, stderr=slave,
                            env=env, cwd=_REPO, start_new_session=True)
    signal.signal(signal.SIGINT, parent_sigint)
    return master, slave, proc, before


def _read_until(master, needle, timeout=10):
    out, end = b"", time.monotonic() + timeout
    while time.monotonic() < end:
        if select.select([master], [], [], 0.1)[0]:
            try:
                out += os.read(master, 65536)
            except OSError:
                break
            if needle in out:
                break
    return out.decode(errors="replace")


@pytest.mark.parametrize("how", ["sigterm", "sigint", "sighup"])
def test_terminal_is_restored_on_exit(steward_home, tmp_path, how):
    import signal
    import termios

    _job(Store(steward_home))
    master, slave, proc, before = _spawn_in_pty(tmp_path, steward_home)
    try:
        out = _read_until(master, b"RUNNING")
        assert ENTER in out, "alternate screen, SGR mouse and hidden cursor on at start"
        live = termios.tcgetattr(slave)
        assert not live[3] & termios.ECHO and not live[3] & termios.ICANON, "no echo while the pane owns the tty"
        proc.send_signal({"sigterm": signal.SIGTERM, "sigint": signal.SIGINT, "sighup": signal.SIGHUP}[how])
        tail = _read_until(master, LEAVE.encode(), timeout=5)
        assert proc.wait(5) in (0, -signal.SIGINT, -signal.SIGHUP)
        assert LEAVE in tail, "mouse off, cursor back, alternate screen left"
        assert termios.tcgetattr(slave) == before, "tty modes restored"
        assert not list(tmp_path.glob("steward-dash.*")), "click-map/state files removed"
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
        os.close(slave)


def test_tapping_a_cut_line_expands_and_collapses_it(steward_home, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_HEALTH", "0")
    long = ("Rework the status pane tap model so a line cut with an ellipsis opens in full by one tap and closes "
            "again by another tap, keeping the final word zebra-crossing available")
    _job(Store(steward_home), long)
    rig = Rig(tmp_path)
    rig.app.cols = 60
    rig.app.pane.cols = rig.app.fresh.cols = 60
    rig.app._fixed_size = (60, 0)
    rig.start()
    try:
        rig.wait_for(lambda t: "RUNNING" in t)
        shown = lambda: "\n".join(ANSI.sub("", x) for x in rig.app.drawn)  # noqa: E731
        assert "zebra-crossing" not in shown()
        row = next(r for r, k in rig.app.cmap.mores.items() if k.endswith(":goal"))
        _tap_and_wait(rig, 8, row, lambda: "zebra-crossing" in shown())
        assert rig.ran == []  # opening text navigates nowhere
        row = next(r for r, k in rig.app.cmap.mores.items() if k.endswith(":goal"))
        _tap_and_wait(rig, 8, row, lambda: "zebra-crossing" not in shown())
    finally:
        rig.stop()
