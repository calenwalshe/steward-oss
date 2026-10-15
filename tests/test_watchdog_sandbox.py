"""THE PROOF for issue W (self-heal spec §11): in a sandbox with an isolated STEWARD_HOME, a scratch git repo, an
in-process fake Herdr and REAL stub processes holding the REAL `proclock` locks, the real watchdog passes, the real
control `runner_restart` handler (inside a real `ControlPane` with its journal), the real `heal_runner` and the real
control start all recover every case with no human. After each case: there was never a second runner (at most one
runner stub alive per job at every check, duplicates refused by the lock), and the worker's identity (pane id + pid)
is unchanged.

Only the Herdr transport and "what process a pane command starts" are faked: `herdr pane run <pane> <cmd>` runs
<cmd> through `sh -c` with `python -m steward.cli` swapped for `python tests/support/stubproc.py steward.cli`.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from steward import cli, control_journal, control_pane, deploy_pending, deployed, launch, proclock, stability, watchdog
from steward.models import JobState
from steward.store import Store

from herdr_fake import FakeHerdr

STUB = Path(__file__).resolve().parent / "support" / "stubproc.py"
REPO = Path(__file__).resolve().parent.parent


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True).stdout.strip()


class SandboxHerdr(FakeHerdr):
    """FakeHerdr whose `pane run` really starts the command (with the stub in place of steward.cli)."""

    def __init__(self, home: Path):
        super().__init__([{"tab_id": "w1:t1", "label": "steward"}, {"tab_id": "w1:t2", "label": "job"}],
                         [{"pane_id": "w1:p1", "tab_id": "w1:t1", "label": "steward control",
                           "tokens": {"steward_role": "control"}},
                          {"pane_id": "w1:p3", "tab_id": "w1:t2", "label": "worker"}], workspace="w1")
        self.home = home
        self.procs: dict[str, list[subprocess.Popen]] = {}
        self.down = False
        self.lock = threading.Lock()

    def call(self, method, params):
        if self.down:
            raise OSError("herdr socket: connection refused")
        return super().call(method, params)

    def run(self, argv, cwd, *, timeout=None, env=None):
        with self.lock:
            res = super().run(argv, cwd, timeout=timeout, env=env)
            if argv[:3] == ["herdr", "pane", "run"] and "steward.cli watchers" not in argv[4]:
                pane, cmd = argv[3], argv[4]
                assert any(p["pane_id"] == pane for p in self.panes), f"pane {pane} does not exist"
                py = shlex.quote(sys.executable)
                assert f"{py} -m steward.cli" in cmd, cmd
                cmd = cmd.replace(f"{py} -m steward.cli", f"{py} {shlex.quote(str(STUB))} steward.cli", 1)
                p = subprocess.Popen(["sh", "-c", cmd], env=dict(os.environ, STUB_PANE=pane),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.procs.setdefault(pane, []).append(p)
            return res

    def close_pane(self, pane: str) -> None:
        """Closing a Herdr pane kills what runs in it."""
        self.panes = [p for p in self.panes if p["pane_id"] != pane]
        for p in self.procs.pop(pane, []):
            _kill_tree(p)

    def reap(self):
        for ps in self.procs.values():
            for p in ps:
                p.poll()

    def kill_all(self):
        for ps in self.procs.values():
            for p in ps:
                _kill_tree(p)


def _kill_tree(p: subprocess.Popen) -> None:
    for pid in _children(p.pid) + [p.pid]:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _children(pid: int) -> list[int]:
    out = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                stat = (entry / "stat").read_text()
            except OSError:
                continue
            if int(stat[stat.rfind(")") + 2:].split()[1]) == pid:
                out.append(int(entry.name))
    return out


class Sandbox:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.home = tmp_path / "home"
        self.home.mkdir()
        monkeypatch.setenv("STEWARD_HOME", str(self.home))
        monkeypatch.delenv("STEWARD_LIVE_ROOT", raising=False)
        # the live checkout: a scratch repo whose HEAD is deployed
        self.live = tmp_path / "live"
        self.live.mkdir()
        for a in (("init", "-q"), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
            _git(self.live, *a)
        (self.live / "f").write_text("A")
        _git(self.live, "add", "f")
        _git(self.live, "commit", "-q", "-m", "A")
        self.deployed_head = _git(self.live, "rev-parse", "HEAD")
        deployed.record(self.home, self.deployed_head, str(self.live))
        monkeypatch.setattr(stability, "LIVE_ROOT", self.live)

        self.store = Store(self.home)
        self.herdr = SandboxHerdr(self.home)
        monkeypatch.setattr(cli, "SubprocessRunner", lambda: self.herdr)
        monkeypatch.setattr(control_pane, "_RECEIPT_RETRY_DELAYS", (0.05,))
        self.handlers = cli._build_control_handlers(self.store)
        self.notices: list[str] = []
        self.relayed: list[dict] = []
        self.ctx = watchdog.Ctx(home=self.home, api=self.herdr, runner_exec=self.herdr, relay_fn=self.relay,
                                notify_fn=lambda t: self.notices.append(t) or True, sock="/sandbox.sock",
                                store_fn=lambda: self.store, live_root=self.live, ready_wait_s=8.0,
                                stop_fn=lambda ident: proclock.stop_process(ident, sleep=time.sleep, wait_s=3.0))

        # one RUNNING job with its runner pane and a live worker (never to be touched)
        job = self.store.create_job("sandbox job", str(self.live))
        self.job = job.job_id
        with self.store._conn:
            self.store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (JobState.RUNNING.value, self.job))
        self.herdr.panes.insert(1, {"pane_id": "w1:p2", "tab_id": "w1:t2", "label": f"runner {self.job[:8]}"})
        self.store.kv_set(self.job, "runner_pane", "w1:p2")
        self.store.kv_set(self.job, "herdr_pane", "w1:p3")
        self.store.kv_set(self.job, "herdr_tab", "w1:t2")
        self.store.kv_set(self.job, "launch_args", {"worker": "fabric", "interval": 15.0})
        self.worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", "worker-shim"])
        self.worker_ident = proclock.identity(self.worker.pid)

        # bring the system up the normal way: control in its pane, the runner in its pane
        launch.start_control_on(self.herdr, "/sandbox.sock", "w1:p1", root=self.live, live_root=None)
        cmd = launch.worker_run_command(self.job, worker="fabric", kind="claude", pane="w1:p2", sock="/sandbox.sock",
                                        agent_args=[], interval=15.0, root=self.live, live_root=None)
        self.herdr.run(["herdr", "pane", "run", "w1:p2", cmd], cwd=self.live)
        assert self.wait(lambda: self.control_up() and self.runner_up()), "sandbox did not come up"
        self.max_runners = 1

    # -- the control relay: a real ControlPane executing the real handler (control stays the one actor) ----------
    def relay(self, op: str, args: dict) -> dict:
        if not proclock.is_held(proclock.control_lock(self.home)):
            raise control_pane.NoControlPane("no control pane")
        self.relayed.append(dict(args))
        pane = control_pane.ControlPane(self.store, self.handlers, print_fn=lambda _l: None,
                                        journal=control_journal.Journal(self.home))
        return pane.execute({"request_id": f"wd-{uuid.uuid4().hex[:8]}", "op": op, "args": args,
                             "origin": "watchdog"})

    # -- observation ------------------------------------------------------------------------------------------
    def started(self, kind):
        try:
            lines = (self.home / "stub-started.log").read_text().splitlines()
        except OSError:
            return []
        return [r for r in map(json.loads, lines) if r["kind"] == kind]

    def alive(self, kind):
        self.herdr.reap()
        return [r for r in self.started(kind)
                if proclock.identity_alive({"pid": r["pid"], "start_ticks": r["start_ticks"]})]

    def control_up(self):
        return proclock.is_held(proclock.control_lock(self.home)) and len(self.alive("control")) == 1

    def runner_up(self):
        return proclock.is_held(proclock.runner_lock(self.home, self.job)) and len(self.alive("run")) == 1

    def wait(self, cond, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.1)
        return cond()

    def check(self):
        """The invariants, at every observation: never a second runner / control; the worker untouched."""
        runners = self.alive("run")
        self.max_runners = max(self.max_runners, len(runners))
        assert len(runners) <= 1, f"two runners alive: {runners}"
        assert len(self.alive("control")) <= 1
        assert proclock.identity_alive(self.worker_ident), "the worker was touched"
        assert self.store.kv_get(self.job, "herdr_pane") == "w1:p3"
        assert any(p["pane_id"] == "w1:p3" for p in self.herdr.panes)

    def passes(self, until, n=8):
        for _ in range(n):
            watchdog.run_once(self.ctx)
            self.check()
            if self.wait(until, timeout=1.5):
                self.check()
                return True
        return False

    def kill(self, kind):
        (r,) = self.alive(kind)
        os.kill(r["pid"], signal.SIGKILL)
        assert self.wait(lambda: not self.alive(kind))
        return r

    def close(self):
        self.herdr.kill_all()
        self.worker.kill()
        self.worker.wait()


@pytest.fixture
def sb(tmp_path, monkeypatch):
    s = Sandbox(tmp_path, monkeypatch)
    yield s
    s.close()


def _finish(sb):
    """Every case ends with: everything up, exactly one runner, worker unchanged, and nothing escalated to a human."""
    assert sb.control_up() and sb.runner_up()
    sb.check()
    assert sb.max_runners == 1
    assert sb.notices == []
    assert proclock.identity_alive(sb.worker_ident) and sb.worker.poll() is None


# -- the six proof cases (spec §11) ----------------------------------------------------------------------------

def test_case1_killed_control_comes_back_in_its_pane(sb):
    old = sb.kill("control")
    assert sb.passes(sb.control_up)
    (new,) = sb.alive("control")
    assert new["pid"] != old["pid"] and new["pane"] == "w1:p1"
    assert len(sb.started("control")) == 2
    _finish(sb)
    assert "watchdog_healed" in (sb.home / watchdog.EVENTS_FILE).read_text()


def test_case2_killed_runner_comes_back_through_control_and_the_worker_is_untouched(sb):
    old = sb.kill("run")
    assert sb.passes(sb.runner_up)
    (new,) = sb.alive("run")
    assert new["pid"] != old["pid"] and new["pane"] == "w1:p2"
    assert sb.relayed == [{"job_id": sb.job, "mode": "heal"}]
    sb.passes(lambda: False, n=1)  # the readiness pass records the heal
    whats = [e.what for e in sb.store.events(sb.job)]
    assert "runner_restarted" in whats and "watchdog_healed" in whats
    ev = [e for e in sb.store.events(sb.job) if e.what == "runner_restarted"][-1]
    assert ev.judgment.value == "code" and ev.evidence["origin"] == "watchdog"
    _finish(sb)


def test_case3_locked_db_control_still_restored_events_spooled_then_flushed(sb):
    for conn in (sb.store._conn,):
        conn.execute("PRAGMA busy_timeout = 300")
    blocker = sqlite3.connect(sb.home / "steward.db", timeout=1, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")  # another writer holds the write lock for the whole outage
    try:
        sb.kill("control")
        sb.kill("run")
        assert sb.passes(lambda: sb.control_up() and sb.runner_up(), n=10)
        st = watchdog.load_state(sb.home)
        assert "last_error" not in st and st["spooled"] >= 1  # job events wait in the spool
        # control's own receipt could not be saved: it is journaled, not lost
        assert "control-journal" in " ".join(os.listdir(sb.home))
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    sb.store._conn.execute("PRAGMA busy_timeout = 30000")
    st = watchdog.run_once(sb.ctx)
    assert st["spooled"] == 0
    assert any(e.what.startswith("watchdog_") for e in sb.store.events(sb.job))
    pane = control_pane.ControlPane(sb.store, sb.handlers, print_fn=lambda _l: None,
                                    journal=control_journal.Journal(sb.home))
    pane._save_unsaved()
    assert (sb.home / control_journal.FILE).read_text() == ""
    _finish(sb)


def test_case4_deleted_runner_pane_is_recreated_and_the_worker_is_untouched(sb):
    sb.herdr.close_pane("w1:p2")
    assert sb.wait(lambda: not sb.alive("run"))
    assert sb.passes(sb.runner_up)
    (new,) = sb.alive("run")
    assert new["pane"] not in ("w1:p2", "w1:p3")
    assert sb.store.kv_get(sb.job, "runner_pane") == new["pane"]
    assert any(c[:3] == ["herdr", "tab", "create"] for c in sb.herdr.calls)
    _finish(sb)


def test_case5_duplicate_restart_requests_never_make_a_second_runner(sb):
    sb.kill("run")
    # (a) concurrent requests racing past the recheck: the runner lock refuses the loser
    job = sb.store.get_job(sb.job)
    errors = []

    def racer():  # separate launchers = separate store connections, like separate processes
        try:
            launch.heal_runner(sb.herdr, Store(sb.home), job)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=racer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sb.wait(sb.runner_up)
    time.sleep(1.0)  # let any duplicate start and be refused
    sb.check()
    # (b) repeated requests through control and repeated watchdog passes: no-ops
    for _ in range(5):
        r = sb.relay("runner_restart", {"job_id": sb.job, "mode": "heal"})
        assert r["accepted"] and r.get("noop"), r
    for _ in range(3):
        watchdog.run_once(sb.ctx)
        sb.check()
    time.sleep(0.5)
    refused = (sb.home / "stub-refused.log").read_text().splitlines() if (sb.home / "stub-refused.log").exists() else []
    assert len(sb.started("run")) == 2  # the original + exactly one restore
    assert all(json.loads(r)["kind"] == "run" for r in refused)
    typed = sum(1 for c in sb.herdr.calls if c[:3] == ["herdr", "pane", "run"] and "steward.cli run" in c[4])
    assert len(refused) == typed - 2  # every racing duplicate that got typed was refused by the runner lock
    _finish(sb)


def test_case6_pending_deploy_does_not_block_and_restores_from_the_deployed_tree(sb):
    (sb.live / "f").write_text("B - an untested self-merge")
    _git(sb.live, "commit", "-q", "-am", "B")
    deploy_pending.mark(sb.store, "merging-job")
    sb.kill("control")
    sb.kill("run")
    assert sb.passes(lambda: sb.control_up() and sb.runner_up(), n=10)
    pinned = sb.home / "deployed" / sb.deployed_head[:12]
    for kind in ("control", "run"):
        (r,) = sb.alive(kind)
        assert Path(r["cwd"]).resolve() == pinned.resolve(), (kind, r["cwd"])
        assert r["live_root"] == str(sb.live)
    assert _git(pinned, "rev-parse", "HEAD") == sb.deployed_head
    assert (pinned / "f").read_text() == "A"  # never the untested checkout
    _finish(sb)


# -- also proven in the sandbox: wedge, Herdr down, breaker -------------------------------------------------------

def test_wedged_control_is_confirmed_stopped_then_replaced(sb):
    sb.ctx.cfg.wedged_s = 1.0
    (old,) = sb.alive("control")
    (sb.home / "stub-wedge").write_text(str(old["pid"]))
    time.sleep(1.5)
    assert sb.passes(lambda: sb.control_up() and sb.alive("control")[0]["pid"] != old["pid"])
    assert not proclock.identity_alive({"pid": old["pid"], "start_ticks": old["start_ticks"]})
    assert "watchdog_control_wedged" in (sb.home / watchdog.EVENTS_FILE).read_text()
    _finish(sb)


def test_herdr_down_holds_healing_then_recovers(sb):
    sb.ctx.cfg.limits.herdr_escalate_s = 0.0
    sb.herdr.down = True
    sb.kill("control")
    for _ in range(3):
        watchdog.run_once(sb.ctx)
        sb.check()
    assert len(sb.started("control")) == 1 and len(sb.notices) == 1  # held; one notice
    sb.herdr.down = False
    assert sb.passes(sb.control_up)
    assert watchdog.load_state(sb.home).get("notice") == {}
    assert sb.control_up() and sb.runner_up()
    sb.check()


def test_breaker_cooldown_blocker_then_retry(sb):
    from steward import blockers

    sb.ctx.cfg.limits.cooldown_s = 2.0
    for _ in range(2):  # dies twice, healed twice
        sb.kill("run")
        assert sb.passes(sb.runner_up)
    sb.kill("run")  # third death inside the window: breaker opens, no restart, a tech-lead blocker
    for _ in range(3):
        watchdog.run_once(sb.ctx)
        sb.check()
    assert not sb.alive("run")
    (b,) = [b for b in blockers.open_blockers(sb.store, sb.job) if b.kind == watchdog.BREAKER_KIND]
    assert b.responsible == "tech_lead"
    time.sleep(2.2)  # cooldown over: one half-open attempt, never a permanent give-up
    assert sb.passes(sb.runner_up)
    sb.check()
    assert sb.notices == []  # a runner never pages the owner
