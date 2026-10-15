"""`steward watchdog` CLI and the dashboard's display-only health (issue W, spec §2 D5, §10)."""
import inspect
from types import SimpleNamespace

from steward import cli, dashrender, health, proclock, watchdog


def _args(**kw):
    base = dict(once=False, status=False, reset=None, check_env=False, sock="/nonexistent.sock")
    base.update(kw)
    return SimpleNamespace(**base)


class _Api:
    def call(self, method, params):
        return {"panes": []}


def _factory(home, sock):
    return watchdog.Ctx(home=home, api=_Api(), runner_exec=None, relay_fn=lambda op, a: {"accepted": True},
                        notify_fn=lambda t: True, sock=sock, ready_wait_s=0.0)


def test_once_runs_one_pass_and_writes_state(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path))
    held = proclock.acquire(proclock.control_lock(tmp_path))  # a live control
    try:
        assert cli._cmd_watchdog(_args(once=True), ctx_factory=_factory) == 0
    finally:
        held.release()
    st = watchdog.load_state(tmp_path)
    assert st["targets"]["control"]["state"] == "control_ok" and st["herdr"] == "up"
    assert not proclock.is_held(proclock.lock_path(tmp_path, "watchdog"))


def test_a_second_watchdog_exits_3(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path))
    first = watchdog.single_instance(tmp_path)
    try:
        assert cli._cmd_watchdog(_args(once=True), ctx_factory=_factory) == 3
    finally:
        first.release()
    assert "already running" in capsys.readouterr().err


def test_reset_and_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path))
    watchdog.save_state(tmp_path, {"targets": {"runner:abc": {"breaker": {"exits": [1, 2, 3], "open_until": 9e9}}}})
    assert cli._cmd_watchdog(_args(reset="runner:abc")) == 0
    assert cli._cmd_watchdog(_args(reset="runner:nope")) == 1
    assert cli._cmd_watchdog(_args(status=True)) == 0
    assert '"runner:abc"' in capsys.readouterr().out


def test_check_env_reports_each_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path))
    monkeypatch.setattr(watchdog, "check_env", lambda home, sock: [("herdr socket", False, sock), ("repo", True, "x")])
    assert cli._cmd_watchdog(_args(check_env=True)) == 1
    out = capsys.readouterr().out
    assert "FAIL herdr socket: /nonexistent.sock" in out and "ok   repo: x" in out


def test_check_env_rows(tmp_path):
    rows = dict((n, ok) for n, ok, _d in watchdog.check_env(
        tmp_path, str(tmp_path / "no.sock"), which=lambda t: f"/usr/bin/{t}",
        run=lambda argv: SimpleNamespace(stdout="Linger=yes\n")))
    assert rows["interpreter"] and rows["STEWARD_HOME"] and rows["lingering"] and not rows["herdr socket"]
    assert rows["tg on PATH"] and rows["herdr on PATH"]


def test_parser_knows_watchdog():
    args = cli._parser().parse_args(["watchdog", "--once"])
    assert args.command == "watchdog" and args.once


def test_dash_only_displays():
    src = inspect.getsource(dashrender)
    assert "heal_fn=None" in src and "default_heal(" not in src


def test_tick_skips_alerts_for_watchdog_owned_components(tmp_path):
    from steward.store import Store

    store = Store(tmp_path)
    comps = [health.Component(n, False, "down", [f"{n}: down"]) for n in health.COMPONENTS]
    sent = []
    health.tick(store, now=1000.0, ttl_s=0, check_fn=lambda: comps, heal_fn=None, alert_fn=sent.append)
    health.tick(store, now=2000.0, ttl_s=0, check_fn=lambda: comps, heal_fn=None, alert_fn=lambda t: sent.append(t) or True,
                alert_skip=health.WATCHDOG_OWNED)
    names = {t.split(" red for")[0].split()[-1] for t in sent}
    assert names == {"advisor", "mcp", "budget"}  # budget: fleet-registry's advisory component (forced red here)
