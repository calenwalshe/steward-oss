"""Issue J: the detached delivery worker must actually run, leave a log, and stop respawning when it makes no progress."""
import ast
import re
import subprocess
import sys
from pathlib import Path

from steward import autopilot, blockers, delivery, intents
from steward.models import Judgment
from steward.store import Store
from owner_surface import OwnerSurface
from test_incorporate import mkjob, scratch  # noqa: F401 - scratch is a fixture

PKG = Path(delivery.__file__).parent
_LAUNCH = re.compile(r'(?:"-m",\s*|\[:-1\],\s*|-m\s+)"?(steward\.[a-z_0-9]+)')


def test_delivery_module_without_args_prints_usage_exit_2():
    r = subprocess.run([sys.executable, "-m", "steward.delivery"], capture_output=True, text=True,
                       cwd=str(PKG.parent), timeout=60)
    assert r.returncode == 2
    assert "usage" in r.stderr


def _has_main_guard(path: Path) -> bool:
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) \
                and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__":
            return True
    return False


def test_every_module_launched_with_dash_m_has_a_main_guard():
    mods = set()
    for f in PKG.glob("*.py"):
        mods.update(_LAUNCH.findall(f.read_text()))
    assert {"steward.delivery", "steward.bg_verify", "steward.dashact"} <= mods
    missing = sorted(m for m in mods if not _has_main_guard(PKG / (m.split(".", 1)[1] + ".py")))
    assert not missing, f"launched with -m but no __main__ guard: {missing}"


def test_run_delivery_records_step_started(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    jid = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"}).job_id
    try:
        delivery.run_delivery(store, jid, runner=None, now=1000.0)
    except Exception:  # noqa: BLE001 - only the start record matters here
        pass
    assert store.events_of(jid, ("delivery_step_started",))


def test_spawn_logs_worker_output_and_rotates(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_HOME", str(steward_home))
    monkeypatch.setattr(delivery, "LOG_MAX_BYTES", 10)
    pid = delivery._spawn_delivery("jobx", "k", "o")  # no such job: worker prints a failure/usage to the log
    import os, time
    for _ in range(100):
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except OSError:
            break
    log = delivery.worker_log_path("jobx")
    assert log.exists() and "spawn" in log.read_text()
    delivery._spawn_delivery("jobx", "k", "o")
    assert log.with_name(log.name + ".1").exists()


def test_third_attempt_without_progress_raises_one_blocker_one_notice_and_stops(steward_home, scratch, tmp_path,
                                                                                monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    surf = OwnerSurface(store, monkeypatch)
    jid = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"}).job_id
    spawns = []

    def spawn(*args):
        spawns.append(args)
        return 99999999

    t = 1000.0
    for _ in range(12):
        delivery.sweep(store, now=t, spawn=spawn, notifier=surf.notes)
        t += delivery.WORKER_LEASE_S + delivery.SWEEP_EVERY_S + 1
    stuck = [b for b in blockers.open_blockers(store, jid) if b.kind == delivery.STUCK_KIND]
    assert len(stuck) == 1
    assert "attempt" in stuck[0].claim and "delivery-logs" in stuck[0].claim
    assert len(spawns) == delivery.STUCK_ATTEMPTS
    assert len(surf.owner_requests(jid)) == 1


def _drive(store, surf, spawn, t, n):
    for _ in range(n):
        delivery.sweep(store, now=t, spawn=spawn, notifier=surf.notes)
        t += delivery.WORKER_LEASE_S + delivery.SWEEP_EVERY_S + 1
    return t


def _stuck(store, jid):
    return [b for b in blockers.open_blockers(store, jid) if b.kind == delivery.STUCK_KIND]


def _key_of(store, jid):
    return intents.for_job(store, jid, "delivery-worker")[0].key


def test_old_progress_event_does_not_hide_later_silent_claims(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    surf = OwnerSurface(store, monkeypatch)
    jid = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"}).job_id
    spawns = []
    spawn = lambda *a: spawns.append(a) or 99999999  # noqa: E731
    t = _drive(store, surf, spawn, 1000.0, 1)
    key = _key_of(store, jid)
    store.append_event(jid, what="delivery_step_started", evidence={"step": "incorporate", "intent_key": key},
                       rule="delivery", judgment=Judgment.code)
    _drive(store, surf, spawn, t, 12)
    blk = _stuck(store, jid)
    assert len(blk) == 1 and "incorporate" in blk[0].claim and "delivery-logs" in blk[0].claim
    assert any(e.startswith("attempts:") for e in blk[0].evidence)
    # first claim predates the event; window opens at the claim after it, so exactly 3 further silent claims spawn
    assert len(spawns) <= 1 + delivery.STUCK_ATTEMPTS
    n = len(spawns)
    _drive(store, surf, spawn, 900000.0, 3)
    assert len(spawns) == n
    assert len(surf.owner_requests(jid)) == 1


def test_current_claim_progress_prevents_blocker(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    surf = OwnerSurface(store, monkeypatch)
    jid = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"}).job_id
    spawns = []

    def spawn(*a):
        spawns.append(a)
        key = a[1]
        store.append_event(jid, what="delivery_step_started", evidence={"step": "incorporate", "intent_key": key},
                           rule="delivery", judgment=Judgment.code)
        return 99999999

    _drive(store, surf, spawn, 1000.0, 6)
    assert not _stuck(store, jid)
    assert len(spawns) >= 5


def test_retry_after_blocker_clears_then_stalls_again(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    surf = OwnerSurface(store, monkeypatch)
    jid = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"}).job_id
    spawns = []
    spawn = lambda *a: spawns.append(a) or 99999999  # noqa: E731
    t = _drive(store, surf, spawn, 1000.0, 8)
    assert len(spawns) == 3 and len(_stuck(store, jid)) == 1
    b = _stuck(store, jid)[0]
    blockers.transition(store, b.blocker_id, "resolved", actor=delivery.CONTROLLER, reason="owner retry", now=t)
    t = _drive(store, surf, spawn, t, 8)
    assert len(spawns) == 6 and len(_stuck(store, jid)) == 1


def test_stuck_window_survives_store_restart(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    surf = OwnerSurface(store, monkeypatch)
    jid = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"}).job_id
    spawns = []
    spawn = lambda *a: spawns.append(a) or 99999999  # noqa: E731
    t = 1000.0
    for _ in range(8):
        store = Store(steward_home)  # a fresh process each sweep
        surf = OwnerSurface(store, monkeypatch)
        t = _drive(store, surf, spawn, t, 1)
    assert len(spawns) == delivery.STUCK_ATTEMPTS and len(_stuck(Store(steward_home), jid)) == 1
