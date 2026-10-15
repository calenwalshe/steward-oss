"""`steward run` / `steward control` hold their exclusive locks; control beats (issue W, spec §4)."""
from types import SimpleNamespace

from steward import cli, control_pane, proclock
from steward.store import Store


def _job(store, tmp_path):
    return store.create_job("goal", str(tmp_path)).job_id


def test_run_refuses_when_the_runner_lock_is_held(tmp_path, capsys):
    store = Store(tmp_path / "home")
    jid = _job(store, tmp_path)
    before = len(store.events(jid))
    held = proclock.acquire(proclock.runner_lock(store.home, jid))
    try:
        rc = cli._cmd_run(store, SimpleNamespace(job=jid, worker="fake", pane=None, sock=None))
    finally:
        held.release()
    assert rc == 3
    assert "already running" in capsys.readouterr().err
    assert len(store.events(jid)) == before


def test_run_releases_its_lock_on_exit(tmp_path, monkeypatch):
    store = Store(tmp_path / "home")
    jid = _job(store, tmp_path)
    seen = []

    def inner(store_, args):
        seen.append(proclock.is_held(proclock.runner_lock(store_.home, args.job)))
        return 0

    monkeypatch.setattr(cli, "_cmd_run_locked", inner)
    args = SimpleNamespace(job=jid, worker="fake", pane="w:p9", sock=None)
    assert cli._cmd_run(store, args) == 0
    assert cli._cmd_run(store, args) == 0
    assert seen == [True, True]
    assert not proclock.is_held(proclock.runner_lock(store.home, jid))
    ident = proclock.read_json(proclock.runner_file(store.home, jid))
    assert ident["pane"] == "w:p9" and ident["pid"] > 0 and "start_ticks" in ident


def test_control_refuses_when_the_control_lock_is_held(tmp_path, capsys, monkeypatch):
    store = Store(tmp_path / "home")
    monkeypatch.setattr(cli, "_sync_exceptions_registry", lambda: None)
    held = proclock.acquire(proclock.control_lock(store.home))
    try:
        rc = cli._cmd_control(store, SimpleNamespace(sock="/nonexistent.sock", pane="w:p1", ensure=False))
    finally:
        held.release()
    assert rc == 3
    assert "already running" in capsys.readouterr().err


def test_run_forever_beats_every_pass():
    beats = []
    n = [0]

    class Box:
        def read(self, timeout):
            return []

    def running():
        n[0] += 1
        return n[0] <= 3

    pane = control_pane.ControlPane(Store.__new__(Store), {}, print_fn=lambda s: None)
    pane._save_unsaved = lambda: None
    pane.run_forever(Box(), None, running=running, beat_fn=lambda: beats.append(1),
                     tick=control_pane.TickSteps(lambda: None, lambda: None), tick_every_s=0)
    assert len(beats) >= 3
