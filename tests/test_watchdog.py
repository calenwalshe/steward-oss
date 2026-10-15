"""The watchdog's healer registry, structured states, breaker, spool and notice (issue W, spec §2-§9).
Fakes only; `test_watchdog_sandbox.py` proves the same with real processes."""
import os
import sqlite3
import subprocess
import sys
import time

import pytest

from steward import blockers, control_pane, deployed, proclock, watchdog
from steward.models import JobState
from steward.store import Store

from herdr_fake import FakeHerdr


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Api:
    def __init__(self, herdr):
        self.herdr = herdr
        self.down = False

    def call(self, method, params):
        if self.down:
            raise OSError("herdr socket refused")
        return self.herdr.call(method, params)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("STEWARD_HOME", str(home))
    store = Store(home)
    herdr = FakeHerdr([{"tab_id": "w4Q:t1", "label": "steward"}],
                      [{"pane_id": "w4Q:p1", "tab_id": "w4Q:t1", "label": "steward control",
                        "tokens": {"steward_role": "control"}}])
    clock = Clock()
    held = {}

    def on_run(argv):  # `herdr pane run <pane> <cmd>` starts control: it takes the lock
        if argv[:3] == ["herdr", "pane", "run"] and "steward.cli control" in argv[4]:
            held["control"] = proclock.acquire(proclock.control_lock(home))
            proclock.write_json(proclock.control_file(home), proclock.identity(pane=argv[3], beat_at=clock()))

    orig = herdr.run

    def run(argv, cwd, **kw):
        res = orig(argv, cwd, **kw)
        on_run(argv)
        return res
    herdr.run = run
    relays = []
    notices = []

    def relay(op, args):
        if not proclock.is_held(proclock.control_lock(home)):
            raise control_pane.NoControlPane("no control pane")
        relays.append((op, dict(args)))
        return {"accepted": True, "detail": "restored", "pane": "w4Q:p2"}

    deployed.record(home, "abc", str(tmp_path))
    ctx = watchdog.Ctx(home=home, api=Api(herdr), runner_exec=herdr, relay_fn=relay,
                       notify_fn=lambda text: notices.append(text) or True, sock="/s", clock=clock, sleep=clock.sleep,
                       ready_wait_s=0.0, store_fn=lambda: store, live_root=tmp_path)
    monkeypatch.setattr(deployed, "launch_root", lambda h, live, **k: (tmp_path, None))
    real_find = watchdog.ControlHealer.find

    def find(self, ctx):  # a live fake control keeps beating, like the real loop does
        if held.get("control") is not None and held["control"].fh is not None:
            proclock.beat(home, clock())
        return real_find(self, ctx)
    monkeypatch.setattr(watchdog.ControlHealer, "find", find)
    yield type("Env", (), dict(home=home, store=store, herdr=herdr, clock=clock, ctx=ctx, relays=relays,
                               notices=notices, held=held, tmp=tmp_path))
    for h in held.values():
        if h:
            h.release()


def _control_up(e):
    e.held["control"] = proclock.acquire(proclock.control_lock(e.home))
    proclock.write_json(proclock.control_file(e.home), proclock.identity(pane="w4Q:p1", beat_at=e.clock()))


def _kill_control(e):
    h = e.held.pop("control", None)
    if h:
        h.release()


def _starts(e):
    return [c for c in e.herdr.calls if c[:3] == ["herdr", "pane", "run"] and "steward.cli control" in c[4]]


def _running_job(e, wpane="w4Q:p3"):
    job = e.store.create_job("g", str(e.tmp))
    with e.store._conn:
        e.store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (JobState.RUNNING.value, job.job_id))
    e.store.kv_set(job.job_id, "herdr_pane", wpane)
    e.herdr.panes.append({"pane_id": wpane, "tab_id": "w4Q:t2", "label": "worker"})
    return job.job_id


def test_dead_control_needs_two_observations_then_restarts_in_its_pane(env):
    proclock.write_json(proclock.control_file(env.home), {"pid": 1, "pane": "w4Q:p1"})
    st = watchdog.run_once(env.ctx)
    assert _starts(env) == [] and st["targets"]["control"]["dead_seen"] == 1
    env.clock.t += 30
    st = watchdog.run_once(env.ctx)
    (start,) = _starts(env)
    assert start[3] == "w4Q:p1"
    assert st["targets"]["control"]["state"] == "control_ok"
    env.clock.t += 30
    watchdog.run_once(env.ctx)
    assert len(_starts(env)) == 1  # never a second control
    events = (env.home / watchdog.EVENTS_FILE).read_text()
    assert "watchdog_healed" in events


def test_control_with_no_pane_left_gets_a_new_steward_tab(env):
    env.herdr.panes.clear()
    env.herdr.panes.append({"pane_id": "w4Q:p9", "tab_id": "w4Q:t9", "label": "x"})
    for _ in range(2):
        watchdog.run_once(env.ctx)
        env.clock.t += 30
    assert any(c[:3] == ["herdr", "tab", "create"] for c in env.herdr.calls)
    (start,) = _starts(env)
    assert start[3] != "w4Q:p9"


def test_legacy_lockless_control_counts_as_alive(env):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "steward.cli", "control"],
                         env=dict(os.environ, STEWARD_HOME=str(env.home)))
    try:
        time.sleep(0.2)
        for _ in range(3):
            st = watchdog.run_once(env.ctx)
            env.clock.t += 30
        assert _starts(env) == [] and st["targets"]["control"]["state"] == "control_ok"
    finally:
        p.kill()
        p.wait()


def test_wedged_control_is_stopped_before_replacement(env):
    code = ("import sys, time; from steward import proclock; h = proclock.acquire(proclock.control_lock(sys.argv[1]));"
            " print('ok', flush=True); time.sleep(60)")
    p = subprocess.Popen([sys.executable, "-c", code, str(env.home)], stdout=subprocess.PIPE, text=True,
                         cwd=str(proclock.Path(proclock.__file__).resolve().parent.parent))
    try:
        assert p.stdout.readline().strip() == "ok"
        proclock.write_json(proclock.control_file(env.home),
                            proclock.identity(p.pid, pane="w4Q:p1", beat_at=env.clock() - 400))
        env.ctx.stop_fn = lambda ident: False  # cannot confirm the stop: nothing may start
        watchdog.run_once(env.ctx)
        assert _starts(env) == [] and p.poll() is None
        env.ctx.stop_fn = lambda ident: proclock.stop_process(ident, sleep=time.sleep, wait_s=3.0)
        env.clock.t += 30
        st = watchdog.run_once(env.ctx)
        assert p.poll() is not None or not proclock.identity_alive(proclock.identity(p.pid))
        assert len(_starts(env)) == 1 and st["targets"]["control"]["state"] == "control_ok"
        assert "watchdog_control_wedged" in (env.home / watchdog.EVENTS_FILE).read_text()
    finally:
        if p.poll() is None:
            p.kill()
        p.wait()


def test_active_deploy_holds_control_heal_for_a_bounded_time(env):
    deploy = proclock.acquire(env.home / "deploy-self.lock")
    try:
        for _ in range(4):  # 0..90 s: held
            st = watchdog.run_once(env.ctx)
            env.clock.t += 30
        assert _starts(env) == [] and "deploy" in st["targets"]["control"]["detail"]
        for _ in range(4):  # past deploy_hold_s (180 s): healed anyway
            watchdog.run_once(env.ctx)
            env.clock.t += 30
        assert len(_starts(env)) == 1
        b = watchdog.load_state(env.home)["targets"]["control"]["breaker"]
        assert b["exits"] == []  # a death around a deploy is not an unexpected exit
    finally:
        deploy.release()


def test_herdr_down_holds_everything_and_notifies_once(env):
    env.ctx.api.down = True
    for _ in range(15):
        st = watchdog.run_once(env.ctx)
        env.clock.t += 30
    assert _starts(env) == [] and env.relays == []
    assert len(env.notices) == 1 and "Herdr" in env.notices[0]
    env.ctx.api.down = False
    _control_up(env)
    watchdog.run_once(env.ctx)
    assert watchdog.load_state(env.home).get("notice") == {}


def test_dead_runner_is_restored_only_through_the_control_relay(env):
    jid = _running_job(env)
    watchdog.run_once(env.ctx)  # control dead too: nothing yet (both seen once; the runner waits for control)
    assert env.relays == []
    env.clock.t += 30
    watchdog.run_once(env.ctx)  # control restored first, then the runner through it, in the same pass
    assert len(_starts(env)) == 1
    assert env.relays == [("runner_restart", {"job_id": jid, "mode": "heal"})]
    env.held["runner"] = proclock.acquire(proclock.runner_lock(env.home, jid))  # the runner came up
    env.clock.t += 30
    st = watchdog.run_once(env.ctx)
    assert st["targets"][f"runner:{jid}"]["state"] == "runner_ok"
    ev = [e for e in env.store.events(jid) if e.what == "watchdog_healed"]
    assert ev and ev[0].judgment.value == "code"


def test_runner_heals_are_rate_limited(env):
    _control_up(env)
    jobs = [_running_job(env, wpane=f"w4Q:p{30 + i}") for i in range(5)]
    for _ in range(2):
        watchdog.run_once(env.ctx)
        env.clock.t += 30
    assert len(env.relays) == 2
    watchdog.run_once(env.ctx)
    assert len(env.relays) == 4
    assert len({a["job_id"] for _, a in env.relays}) == 4 and set(a["job_id"] for _, a in env.relays) <= set(jobs)


def test_governor_paused_and_missing_worker_are_not_healed(env):
    _control_up(env)
    jid = _running_job(env)
    env.store.kv_set(jid, "governor_paused", {"at": 1})
    for _ in range(3):
        watchdog.run_once(env.ctx)
        env.clock.t += 30
    assert env.relays == []
    env.store.kv_set(jid, "governor_paused", None)
    lk = proclock.acquire(proclock.runner_lock(env.home, jid))
    env.herdr.panes = [p for p in env.herdr.panes if p["pane_id"] != "w4Q:p3"]
    st = watchdog.run_once(env.ctx)
    assert st["targets"][f"runner:{jid}"]["state"] == "missing_worker" and env.relays == []
    lk.release()


def test_runner_breaker_cooldown_blocker_half_open_and_reset(env):
    _control_up(env)
    jid = _running_job(env)
    name = f"runner:{jid}"
    for _ in range(8):  # dies every time: heals 1 and 2, the 3rd death opens the breaker
        watchdog.run_once(env.ctx)
        env.clock.t += 30
    assert len(env.relays) == 2
    st = watchdog.load_state(env.home)
    assert st["targets"][name]["breaker"]["open_until"] is not None
    (b,) = [b for b in blockers.open_blockers(env.store, jid) if b.kind == watchdog.BREAKER_KIND]
    assert b.responsible == "tech_lead"
    for _ in range(25):  # inside the 15 min cooldown: no heal
        watchdog.run_once(env.ctx)
        env.clock.t += 30
    assert len(env.relays) == 2
    env.clock.t += 120
    watchdog.run_once(env.ctx)  # half-open: one attempt
    assert len(env.relays) == 3
    lk = proclock.acquire(proclock.runner_lock(env.home, jid))  # this time it stays up
    for _ in range(25):
        watchdog.run_once(env.ctx)
        env.clock.t += 30
    t = watchdog.load_state(env.home)["targets"][name]
    assert t["breaker"] == {"exits": []}
    from steward import ownership

    ownership.reconcile_job(env.store, jid, now=env.clock())
    assert not [b for b in blockers.open_blockers(env.store, jid) if b.kind == watchdog.BREAKER_KIND]
    lk.release()


def test_control_breaker_sends_the_single_owner_notice(env):
    for _ in range(12):  # control dies after every start
        watchdog.run_once(env.ctx)
        _kill_control(env)
        env.clock.t += 30
    assert len(_starts(env)) == 2
    assert len(env.notices) == 1 and "control" in env.notices[0]


def test_locked_db_never_breaks_a_pass_and_events_are_spooled(env):
    _control_up(env)
    jid = _running_job(env)
    lock = sqlite3.connect(env.home / "steward.db", timeout=0.1, isolation_level=None)
    lock.execute("BEGIN EXCLUSIVE")
    slow = Store.__new__(Store)

    def locked_store():
        raise sqlite3.OperationalError("database is locked")
    env.ctx.store_fn = locked_store
    env.ctx.audit.store_fn = locked_store
    try:
        for _ in range(3):
            st = watchdog.run_once(env.ctx)
            env.clock.t += 30
        assert "last_error" not in st
        assert st["spooled"] >= 1 and env.relays  # the runner heal was still requested through control
    finally:
        lock.execute("ROLLBACK")
        lock.close()
    env.ctx.store_fn = lambda: env.store
    env.ctx.audit.store_fn = lambda: env.store
    st = watchdog.run_once(env.ctx)
    assert st["spooled"] == 0
    assert any(e.what == "watchdog_heal_requested" for e in env.store.events(jid))


def test_broken_config_and_state_fall_back_to_defaults(env):
    (env.home / watchdog.CONFIG_FILE).write_text("interval_s = [")
    (env.home / watchdog.STATE_FILE).write_text("{nope")
    cfg = watchdog.load_config(env.home)
    assert cfg.interval_s == 30.0 and cfg.notes
    (env.home / watchdog.CONFIG_FILE).write_text("[healers.runners]\nmax_heals_per_pass = 5\n[limits]\ncooldown_s = 60\n")
    cfg = watchdog.load_config(env.home)
    assert cfg.max_runner_heals_per_pass == 5 and cfg.limits.cooldown_s == 60.0
    assert isinstance(watchdog.run_once(env.ctx), dict)


def test_a_failing_probe_is_isolated(env, monkeypatch):
    _control_up(env)
    monkeypatch.setattr(watchdog.RunnersHealer, "find", lambda self, ctx: 1 / 0)
    st = watchdog.run_once(env.ctx)
    assert "last_error" not in st and st["targets"]["control"]["state"] == "control_ok"
    assert "watchdog_probe_failed" in (env.home / watchdog.EVENTS_FILE).read_text()


def test_single_instance(tmp_path):
    a = watchdog.single_instance(tmp_path)
    assert a is not None and watchdog.single_instance(tmp_path) is None
    a.release()


def test_reset_clears_a_breaker(env):
    env.ctx.state = {}
    t = watchdog._target(env.ctx.state, "runner:x")
    t["breaker"] = {"exits": [1, 2, 3], "open_until": 9e9}
    watchdog.save_state(env.home, env.ctx.state)
    assert watchdog.reset(env.home, "runner:x")
    assert watchdog.load_state(env.home)["targets"]["runner:x"]["breaker"] == {"exits": []}
    assert not watchdog.reset(env.home, "runner:nope")


def test_should_reexec_only_onto_deployed_code():
    assert watchdog.should_reexec("old", "new", "new")
    assert not watchdog.should_reexec("old", "new", "older")  # live HEAD untested: stay
    assert not watchdog.should_reexec("new", "new", "new")
    assert not watchdog.should_reexec("old", "new", None)
