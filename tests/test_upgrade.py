"""Phase 19-05 (G5, G15, G17): idempotent re-runs, in-place revival, and the upgrade path, on a fake Herdr."""

from __future__ import annotations

import json

import pytest

from fake_herdr import FakeHerdr
from steward import advisor_pane, cli, installstate, migrations, prereq, scaffold, templates_io
from steward.models import JobState, Judgment
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store
from test_scaffold import checks


@pytest.fixture
def env(tmp_path, monkeypatch):
    work = tmp_path / "proj"
    work.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(tmp_path / "config" / "exceptions.toml"))
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Sam")
    monkeypatch.delenv("STEWARD_INSTALL_ROOT", raising=False)
    monkeypatch.delenv("STEWARD_TRAJECTORY", raising=False)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/fake-herdr.sock")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w1")
    monkeypatch.chdir(work)
    fake = FakeHerdr("w1")
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: fake.api)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: fake.runner)
    monkeypatch.setattr(prereq, "detect", lambda *a, **k: checks())
    probes = {"control": True, "advisor": True, "sleeps": []}
    monkeypatch.setattr(scaffold, "control_idle", lambda ctx, pane: probes["control"])
    monkeypatch.setattr(scaffold, "advisor_idle", lambda ctx, pane: probes["advisor"])
    monkeypatch.setattr(scaffold, "_sleep", lambda s: probes["sleeps"].append(s))
    results = []
    real_apply = scaffold.apply_scaffold
    monkeypatch.setattr(scaffold, "apply_scaffold", lambda a, c: results.append(real_apply(a, c)) or results[-1])

    class Env:
        pass

    e = Env()
    e.tmp, e.work, e.fake, e.probes, e.results = tmp_path, work, fake, probes, results
    e.home, e.config = tmp_path / "state", tmp_path / "config"
    return e


def _manifest(env):
    return json.loads((env.home / "install.json").read_text())


def _set_installed_version(env, value):
    state = _manifest(env)
    state["version"] = value
    installstate.save(state, env.home)


def _add_job_tab(fake):
    """A job's tab (runner, worker, watchers) and a chat tab: never touched by an upgrade."""
    _tab, panes = fake.add_tab("w1", "⚙ abcd1234 goal", panes=("runner abcd1234", "worker abcd1234",
                                                            "watchers abcd1234"))
    for pane in panes:
        fake.set_live(pane, argv=["python3", "-m", "steward.cli", "run", "abcd1234"])
    _tab, (chat,) = fake.add_tab("w1", "chat", panes=("chat astra",))
    fake.set_live(chat, argv=["claude"])
    return [*panes, chat]


def _calls_on(fake, pane, since=0):
    return [c for c in fake.calls[since:] if pane in c[3:5] and c[1] == "pane" and c[2] in ("run", "send-keys")]


# -- idempotence -----------------------------------------------------------------------------------------------------


def test_second_init_creates_nothing(env, capsys):
    assert cli.main(["init", "--yes"]) == 0
    first = _manifest(env)
    n = len(env.fake.calls)
    capsys.readouterr()

    assert cli.main(["init", "--yes"]) == 0
    out = capsys.readouterr().out
    assert [c for c in env.fake.mutating_calls() if c in env.fake.calls[n:]] == []
    assert all(c[1:3] in (["tab", "list"], ["pane", "list"], ["pane", "process-info"], ["workspace", "list"],
                          ["tab", "focus"]) for c in env.fake.calls[n:])
    second = env.results[-1]
    assert second["templates"] == ["unchanged", "unchanged", "unchanged"]
    assert second["exceptions_added"] == []
    again = _manifest(env)
    assert {k: v for k, v in again.items() if k != "updated_at"} == {k: v for k, v in first.items() if k != "updated_at"}
    assert "control pane already running" in out and "advisor pane already running" in out
    assert "operator pane already running" in out and "dash pane already running" in out
    assert "created" not in out and "started" not in out


def test_dead_panes_are_revived_in_place(env, capsys):
    fake = env.fake
    _t, (ctl, adv) = fake.add_tab("w1", "steward", panes=("steward control", "steward advisor"))
    _t, (op, dash) = fake.add_tab("w1", "cockpit", panes=("operator", "steward dash"))
    fake.set_live(op, argv=["claude", "hello"])  # live: never re-run
    assert cli.main(["init", "--yes"]) == 0
    out = capsys.readouterr().out

    assert len(fake.tabs_labelled("steward")) == 1 and len(fake.tabs_labelled("cockpit")) == 1
    assert fake.of("tab", "create") == [] and fake.of("pane", "split") == []
    runs = fake.runs()
    assert len(runs[ctl]) == 1 and " steward.cli control " in runs[ctl][0]
    assert len(runs[adv]) == 1 and " steward.cli advisor " in runs[adv][0]
    assert len(runs[dash]) == 1 and " steward.cli dash" in runs[dash][0]
    assert op not in runs
    assert fake.by_role("control")["pane_id"] == ctl and fake.by_role("advisor")["pane_id"] == adv
    assert f"control pane restarted in place: {ctl}" in out
    assert f"advisor pane restarted in place: {adv}" in out
    assert f"dash pane restarted in place: {dash}" in out


def test_labelled_pane_is_not_revived_while_another_control_is_live(env, capsys):
    fake = env.fake
    _t, (ctl, adv) = fake.add_tab("w1", "steward", panes=("steward control", "steward advisor"))
    _t, (other,) = fake.add_tab("w1", "elsewhere", panes=("x",))
    fake.set_live(other, role="control", argv=["python3", "-m", "steward.cli", "control"],
                  foreground_cwd=str(scaffold.version_module.install_root()))
    assert cli.main(["init", "--yes"]) == 0
    runs = fake.runs()
    assert ctl not in runs  # one controller only: the live one is used
    assert len(runs[adv]) == 1


def test_live_but_unreported_control_is_adopted_not_duplicated(env):
    fake = env.fake
    _t, (ctl,) = fake.add_tab("w1", "steward", panes=("steward control",))
    fake.set_live(ctl, argv=["python3", "-m", "steward.cli", "control", "--sock", "s"])  # no token yet
    assert cli.main(["init", "--yes"]) == 0
    assert len(fake.tabs_labelled("steward")) == 1
    assert ctl not in fake.runs()
    assert fake.pane_by_label("steward advisor")["split_from"] == ctl


# -- upgrades ---------------------------------------------------------------------------------------------------------


def _add_schema_2(monkeypatch):
    def add_key(text):
        return text + "\n[upgrade_test]\nadded = true\n", ["added upgrade_test.added"]

    monkeypatch.setattr(migrations, "MIGRATIONS", {2: {"steward.toml": add_key}})
    monkeypatch.setattr(migrations, "CURRENT_SCHEMA", 2)


def test_version_change_restarts_control_advisor_and_dash(env, monkeypatch, capsys):
    fake = env.fake
    assert cli.main(["init", "--yes"]) == 0
    untouched = _add_job_tab(fake)
    ctl, adv = fake.by_role("control")["pane_id"], fake.by_role("advisor")["pane_id"]
    dash, op = fake.pane_by_label("steward dash")["pane_id"], fake.pane_by_label("operator")["pane_id"]
    _set_installed_version(env, "0.0.9")
    _add_schema_2(monkeypatch)
    n = len(fake.calls)
    capsys.readouterr()

    assert cli.main(["init", "--yes"]) == 0
    out = capsys.readouterr().out

    assert (env.config / "steward.toml.bak-0.0.9").exists()
    assert "[upgrade_test]" in (env.config / "steward.toml").read_text()
    lines = out.splitlines()
    migrated = next(i for i, line in enumerate(lines) if line.startswith("config migration:"))
    for role, pane in (("control", ctl), ("advisor", adv), ("dash", dash)):
        calls = _calls_on(fake, pane, n)
        assert [c[1:3] for c in calls] == [["pane", "send-keys"], ["pane", "run"]], (role, calls)
        assert calls[0][4] == "ctrl+c"
        assert calls[0][3] == calls[1][3] == pane  # same pane id
        assert lines.index(f"{role} pane restarted: {pane}") > migrated
    assert " steward.cli control " in _calls_on(fake, ctl, n)[1][4]
    assert " steward.cli advisor " in _calls_on(fake, adv, n)[1][4]
    for pane in [op, *untouched]:
        assert _calls_on(fake, pane, n) == [], pane
    assert env.probes["sleeps"]
    assert _manifest(env)["version"] == scaffold.version_module.version()
    assert len(fake.tabs_labelled("steward")) == 1


def test_restart_is_deferred_when_a_frame_is_in_flight(env, capsys):
    fake = env.fake
    assert cli.main(["init", "--yes"]) == 0
    ctl, adv = fake.by_role("control")["pane_id"], fake.by_role("advisor")["pane_id"]
    dash = fake.pane_by_label("steward dash")["pane_id"]
    _set_installed_version(env, "0.0.9")
    env.probes["control"] = env.probes["advisor"] = False
    n = len(fake.calls)
    capsys.readouterr()

    assert cli.main(["init", "--yes"]) == 0
    out = capsys.readouterr().out
    assert _calls_on(fake, ctl, n) == [] and _calls_on(fake, adv, n) == []
    assert len(_calls_on(fake, dash, n)) == 2
    assert out.count("control pane busy: restart deferred, run steward up again") == 1
    assert out.count("advisor pane busy: restart deferred, run steward up again") == 1
    assert _manifest(env)["version"] == "0.0.9"

    env.probes["control"] = env.probes["advisor"] = True
    n = len(fake.calls)
    assert cli.main(["init", "--yes"]) == 0
    assert len(_calls_on(fake, ctl, n)) == 2 and len(_calls_on(fake, adv, n)) == 2
    assert _manifest(env)["version"] == scaffold.version_module.version()


def test_running_jobs_note(env, capsys):
    assert cli.main(["init", "--yes"]) == 0
    _set_installed_version(env, "0.0.9")
    capsys.readouterr()
    assert cli.main(["init", "--yes"]) == 0
    assert "running jobs keep their old worker code" not in capsys.readouterr().out

    store = Store(env.home)
    job = store.create_job("goal", str(env.work))
    store.set_state(job.job_id, JobState.CONTRACTED, what="c", evidence={}, rule="r", judgment=Judgment.code)
    store.set_state(job.job_id, JobState.RUNNING, what="r", evidence={}, rule="r", judgment=Judgment.code)
    _set_installed_version(env, "0.0.9")
    assert cli.main(["init", "--yes"]) == 0
    assert "running jobs keep their old worker code until their next launch" in capsys.readouterr().out


def test_changed_template_is_reported_as_dot_new(env, monkeypatch, capsys):
    assert cli.main(["init", "--yes"]) == 0
    capsys.readouterr()
    real = templates_io._raw
    monkeypatch.setattr(templates_io, "_raw",
                        lambda name: real(name) + ("\n# a knob added by a newer version\n" if name == "steward.toml" else ""))
    assert cli.main(["init", "--yes"]) == 0
    out = capsys.readouterr().out
    assert (env.config / "steward.toml.new").exists()
    assert "steward.toml.new" in out
    assert "a knob added" not in (env.config / "steward.toml").read_text()

    assert cli.main(["init", "--yes"]) == 0
    assert ".new" not in capsys.readouterr().out


# -- steward up --------------------------------------------------------------------------------------------------------


def test_up_upgrade_hook_only_with_install_json(env, capsys):
    fake = env.fake
    assert cli.main(["init", "--yes"]) == 0
    ctl, dash = fake.by_role("control")["pane_id"], fake.pane_by_label("steward dash")["pane_id"]
    _set_installed_version(env, "0.0.9")
    n = len(fake.calls)
    capsys.readouterr()
    assert cli.main(["up"]) == 0
    out = capsys.readouterr().out
    assert f"control pane already running: {ctl}" in out
    assert f"control pane restarted: {ctl}" in out and f"dash pane restarted: {dash}" in out
    assert len(_calls_on(fake, ctl, n)) == 2
    assert _manifest(env)["version"] == scaffold.version_module.version()


def test_up_without_install_json_is_unchanged(steward_home, monkeypatch, capsys):
    """Repo mode, no install.json: exactly the calls and output tests/test_cli_up.py pins."""
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/x.sock")
    monkeypatch.delenv("STEWARD_INSTALL_ROOT", raising=False)

    class Api:
        def __init__(self):
            self.calls = []

        def call(self, method, params):
            self.calls.append(method)
            return {"panes": []} if method == "pane.list" else {}

    api = Api()

    def respond(argv, cwd):
        if argv[:3] == ["herdr", "tab", "create"]:
            payload = {"result": {"tab": {"tab_id": "w3T:tS"}, "root_pane": {"pane_id": "w3T:pctl"}}}
        elif argv[:3] == ["herdr", "pane", "split"]:
            payload = {"result": {"pane": {"pane_id": "w3T:padv"}}}
        else:
            payload = None
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=json.dumps(payload) if payload else "",
                             stderr="", duration_s=0.0)

    runner = FakeRunner(respond)
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: api)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: runner)
    assert cli.main(["up", "--workspace", "w3T"]) == 0
    assert capsys.readouterr().out == (
        "steward tab: w3T:tS\ncontrol pane started: w3T:pctl\nadvisor pane started: w3T:padv\n")
    assert [c["argv"][:3] for c in runner.calls] == [
        ["herdr", "tab", "create"], ["herdr", "pane", "rename"], ["herdr", "pane", "run"],
        ["herdr", "pane", "split"], ["herdr", "pane", "rename"], ["herdr", "pane", "run"]]
    assert api.calls == ["pane.list", "pane.list", "pane.list"]
    assert not (steward_home / "install.json").exists()


# -- the advisor idle probe --------------------------------------------------------------------------------------------


def test_pending_reviews_are_packets_without_advice_on_live_jobs(steward_home):
    store = Store(steward_home)
    live = store.create_job("g", "/r")
    store.set_state(live.job_id, JobState.CONTRACTED, what="c", evidence={}, rule="r", judgment=Judgment.code)
    store.set_state(live.job_id, JobState.RUNNING, what="r", evidence={}, rule="r", judgment=Judgment.code)
    done = store.create_job("g", "/r")
    store.kv_set(live.job_id, "trajectory_review:r1", {"p": 1})
    store.kv_set(live.job_id, "trajectory_review_meta:r1", {"requested_at": 1000.0})
    store.kv_set(done.job_id, "trajectory_review:r2", {"p": 1})
    assert advisor_pane.pending_reviews(store, now=1100.0) == [(live.job_id, "r1")]
    assert advisor_pane.pending_reviews(store, now=1000.0 + 3600) == []  # stale: never answered, not in flight
    store.kv_set(live.job_id, "trajectory_advice:r1", {"status": "ok"})
    assert advisor_pane.pending_reviews(store, now=1100.0) == []


def test_start_advisor_on_is_the_command_ensure_advisor_pane_types():
    def respond(argv, cwd):
        payload = {"result": {"pane": {"pane_id": "w:p9"}}} if argv[:3] == ["herdr", "pane", "split"] else None
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=json.dumps(payload) if payload else "",
                             stderr="", duration_s=0.0)

    class Api:
        def call(self, method, params):
            return {"panes": []}

    a, b = FakeRunner(respond), FakeRunner(respond)
    advisor_pane.ensure_advisor_pane(a, Api(), "/s.sock", "w:p1", reviewer="claude")
    advisor_pane.start_advisor_on(b, "/s.sock", "w:p9", "claude")
    assert [c["argv"] for c in a.calls[1:]] == [c["argv"] for c in b.calls]


# -- control and advisor are stopped by signal (19-09: the live test found Ctrl-C never reaches them) -----------------


class _PidRunner:
    """`herdr pane process-info` answers a pid for the pane's foreground process; every call is recorded."""

    def __init__(self, argv, pid=4242):
        self.calls, self.argv, self.pid = [], argv, pid

    def run(self, argv, cwd=None, **kw):
        self.calls.append(list(argv))
        if argv[:4] == ["herdr", "pane", "process-info", "--pane"]:
            procs = [{"argv": self.argv, "pid": self.pid}]
            out = json.dumps({"result": {"process_info": {"foreground_processes": procs}}})
            return CommandResult(argv, ".", 0, out, "", 0.0)
        return CommandResult(argv, ".", 0, "{}", "", 0.0)


def _restart_ctx(runner, kills, alive_after):
    """A Ctx whose kill records the signal and whose /proc cmdline says the process is gone after `alive_after` signals."""
    d = scaffold.Detected(home="/h", config_dir="/c", version="1", install_root="/r", python="3.12", cwd=".")
    state = {"alive": True}

    def kill(pid, sig):
        kills.append((pid, sig))
        if len(kills) >= alive_after:
            state["alive"] = False

    ctx = scaffold.Ctx(runner=runner, herdr_api=None, sock="/s", detected=d, workspace="w1", sleep=lambda s: None,
                       kill=kill, cmdline=lambda pid: "python3 -m steward.cli control --pane w1:p2" if state["alive"] else None)
    return ctx


def test_stop_pane_process_signals_the_reported_pid():
    import signal

    kills = []
    runner = _PidRunner(["/py", "-m", "steward.cli", "control", "--pane", "w1:p2"])
    ctx = _restart_ctx(runner, kills, alive_after=1)
    assert scaffold._stop_pane_process(ctx, "w1:p2", "steward.cli control") is True
    assert kills == [(4242, signal.SIGINT)]  # one signal was enough; no SIGTERM, no keystroke


def test_stop_pane_process_escalates_to_sigterm():
    import signal

    kills = []
    runner = _PidRunner(["/py", "-m", "steward.cli", "control", "--pane", "w1:p2"])
    ctx = _restart_ctx(runner, kills, alive_after=2)
    assert scaffold._stop_pane_process(ctx, "w1:p2", "steward.cli control") is True
    assert kills == [(4242, signal.SIGINT), (4242, signal.SIGTERM)]


def test_stop_pane_process_never_signals_another_program():
    kills = []
    runner = _PidRunner(["/bin/bash"])  # only a shell is left in the pane: nothing to stop
    ctx = _restart_ctx(runner, kills, alive_after=1)
    assert scaffold._stop_pane_process(ctx, "w1:p2", "steward.cli control") is False
    assert kills == []
    runner = _PidRunner(["/py", "-m", "steward.cli", "control"], pid="not-a-pid")
    assert scaffold._stop_pane_process(_restart_ctx(runner, kills, alive_after=1), "w1:p2", "steward.cli control") is False
    assert kills == []


def test_restart_uses_the_signal_and_not_the_keystroke(monkeypatch):
    kills = []
    runner = _PidRunner(["/py", "-m", "steward.cli", "control", "--pane", "w1:p2"])
    ctx = _restart_ctx(runner, kills, alive_after=1)
    monkeypatch.setattr(scaffold, "_role_pane", lambda c, role: "w1:p2")
    monkeypatch.setattr(scaffold, "control_idle", lambda c, pane: True)
    monkeypatch.setattr(scaffold.launch, "start_control_on", lambda r, sock, pane: r.calls.append(["started", pane]))
    out = {"lines": [], "deferred": []}
    scaffold._restart(ctx, "control", out)
    assert kills and ["started", "w1:p2"] in runner.calls
    assert ["herdr", "pane", "send-keys", "w1:p2", "ctrl+c"] not in runner.calls
    assert out["restarted"] == ["control"]


def test_restart_falls_back_to_the_keystroke_without_a_pid(monkeypatch):
    kills = []
    runner = _PidRunner(["/py", "-m", "steward.cli", "control"])
    runner.pid = None  # Herdr gave no pid (an older Herdr): the old behaviour
    ctx = _restart_ctx(runner, kills, alive_after=1)
    monkeypatch.setattr(scaffold, "_role_pane", lambda c, role: "w1:p2")
    monkeypatch.setattr(scaffold, "control_idle", lambda c, pane: True)
    monkeypatch.setattr(scaffold.launch, "start_control_on", lambda r, sock, pane: None)
    scaffold._restart(ctx, "control", {"lines": [], "deferred": []})
    assert kills == [] and ["herdr", "pane", "send-keys", "w1:p2", "ctrl+c"] in runner.calls
