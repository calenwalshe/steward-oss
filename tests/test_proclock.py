"""Process identity + exclusivity without the DB (issue W, spec §4)."""
import os
import subprocess
import sys
import time

from steward import proclock

SLEEPER = "import sys, time; time.sleep(60)"


def _spawn(args, env_home, extra_env=None):
    env = dict(os.environ, STEWARD_HOME=str(env_home), **(extra_env or {}))
    return subprocess.Popen([sys.executable, "-c", SLEEPER, *args], env=env)


def test_acquire_is_exclusive_and_released(tmp_path):
    path = proclock.runner_lock(tmp_path, "job1")
    first = proclock.acquire(path)
    assert first is not None
    assert proclock.acquire(path) is None
    assert proclock.is_held(path)
    first.release()
    assert not proclock.is_held(path)
    again = proclock.acquire(path)
    assert again is not None
    again.release()


def test_lock_held_by_other_process_and_freed_when_it_dies(tmp_path):
    path = proclock.control_lock(tmp_path)
    code = ("import sys, time; from steward import proclock; h = proclock.acquire(sys.argv[1]); "
            "print('ok' if h else 'held', flush=True); time.sleep(60)")
    p = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True,
                         cwd=str(proclock.Path(proclock.__file__).resolve().parent.parent))
    try:
        assert p.stdout.readline().strip() == "ok"
        assert proclock.is_held(path)
        assert proclock.acquire(path) is None
    finally:
        p.kill()
        p.wait()
    assert not proclock.is_held(path)


def test_identity_detects_recycled_pid():
    me = proclock.identity()
    assert proclock.identity_alive(me)
    assert not proclock.identity_alive({**me, "start_ticks": me["start_ticks"] + 1})
    assert not proclock.identity_alive(None)
    assert not proclock.identity_alive({"pid": 2 ** 22 + 7, "start_ticks": 1})


def test_find_processes_matches_home_and_cmdline(tmp_path):
    other = tmp_path / "other"
    a = _spawn(["steward.cli", "run", "jobA"], tmp_path)
    b = _spawn(["steward.cli", "run", "jobA"], other)
    try:
        time.sleep(0.2)
        found = proclock.find_processes("steward.cli run jobA", tmp_path)
        assert a.pid in found and b.pid not in found
        assert proclock.find_processes("steward.cli run jobB", tmp_path) == []
    finally:
        for p in (a, b):
            p.kill()
            p.wait()


def test_starting_marker_respects_grace(tmp_path):
    proclock.mark_starting(tmp_path, "j", "w:p1", now=100.0)
    assert proclock.starting(tmp_path, "j", now=120.0)["pane"] == "w:p1"
    assert proclock.starting(tmp_path, "j", now=100.0 + proclock.STARTING_GRACE_S + 1) is None
    assert proclock.starting(tmp_path, "other", now=120.0) is None


def test_stop_process_confirms_stop(tmp_path):
    p = subprocess.Popen([sys.executable, "-c", SLEEPER])
    try:
        time.sleep(0.1)
        ident = proclock.identity(p.pid)
        assert proclock.stop_process(ident, sleep=time.sleep, wait_s=3.0)
        assert p.poll() is not None or not proclock.identity_alive(ident)
    finally:
        if p.poll() is None:
            p.kill()
        p.wait()
    assert proclock.stop_process({"pid": p.pid, "start_ticks": -1}, sleep=time.sleep)


def test_beat_and_files_roundtrip(tmp_path):
    proclock.write_json(proclock.control_file(tmp_path), {"pid": 1})
    proclock.beat(tmp_path, now=42.0)
    assert proclock.read_json(proclock.control_file(tmp_path)) == {"pid": 1, "beat_at": 42.0}
    (tmp_path / "bad.json").write_text("{nope")
    assert proclock.read_json(tmp_path / "bad.json") is None


def test_stop_process_never_signals_itself():
    assert proclock.stop_process(proclock.identity(), sleep=lambda s: None) is False


def test_starting_marker_ends_once_the_runner_took_its_lock(tmp_path):
    proclock.mark_starting(tmp_path, "j", "w:p1", now=time.time() - 1)
    assert proclock.starting(tmp_path, "j")
    proclock.write_json(proclock.runner_file(tmp_path, "j"), proclock.identity())  # the start completed
    assert proclock.starting(tmp_path, "j") is None
