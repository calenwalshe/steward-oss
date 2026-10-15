"""`steward launch`: one Herdr tab per job (USERLOOP-LOG bug 2c), now
fabric-native by default (OPX-05, 11-CONTEXT's closing line) and relayed
through the control pane like every other write verb (11-02). Every herdr
call goes through the injected `Runner`; no test here invokes real herdr."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import steward
from steward import cli, launch
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store

_TAB_OK = json.dumps(
    {"result": {"tab": {"tab_id": "w3T:t2"}, "root_pane": {"pane_id": "w3T:p2"}}}
)
_STEWARD_ROOT = Path(steward.__file__).resolve().parent.parent

# Captured at import time (before tests/conftest.py's autouse
# `_inprocess_control_relay` fixture monkeypatches `cli._relay` for the rest
# of the suite) — the one test below that exercises the real "no control
# pane" failure needs the genuine implementation back.
_real_relay = cli._relay


def _respond(argv, cwd):
    if argv[:3] == ["herdr", "tab", "create"]:
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=_TAB_OK, stderr="", duration_s=0.0)
    return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)


# ------------------------------------------------------------- worker_run_command


def test_worker_run_command_fabric_omits_pane_and_includes_sock():
    command = launch.worker_run_command(
        "job-1", worker="fabric", kind="claude", pane="w3T:p2", sock="/tmp/x.sock",
        agent_args=["--model", "sonnet"], interval=15.0,
    )

    assert command.removeprefix(launch._env_exports()).startswith(
        f"cd {_STEWARD_ROOT} && {sys.executable} -m steward.cli run job-1 "
        "--worker fabric --agent claude --sock /tmp/x.sock --interval 15.0"
    )
    assert "--pane" not in command
    assert command.endswith("--agent-arg=--model --agent-arg=sonnet")


def test_worker_run_command_fabric_without_sock_omits_it():
    command = launch.worker_run_command(
        "job-1", worker="fabric", kind="claude", pane="w3T:p2", sock=None, agent_args=[], interval=15.0,
    )

    assert "--sock" not in command
    assert "--pane" not in command


def test_worker_run_command_fabric_rejects_codex():
    with pytest.raises(ValueError, match="does not support --kind codex"):
        launch.worker_run_command(
            "job-1", worker="fabric", kind="codex", pane="w3T:p2", sock=None, agent_args=[], interval=15.0,
        )


def test_worker_run_command_herdr_tui_keeps_the_legacy_shape():
    command = launch.worker_run_command(
        "job-1", worker="herdr-tui", kind="claude", pane="w3T:p2", sock="/tmp/x.sock",
        agent_args=["--model", "sonnet"], interval=15.0,
    )

    assert command.removeprefix(launch._env_exports()).startswith(
        f"cd {_STEWARD_ROOT} && {sys.executable} -m steward.cli run job-1 "
        "--worker herdr --kind claude --pane w3T:p2 --interval 15.0"
    )
    assert "--sock" not in command  # herdr-tui doesn't need the API socket
    assert command.endswith("--agent-arg=--model --agent-arg=sonnet")


# ----------------------------------------------------------------- launch_tab


def test_launch_tab_creates_tab_renames_runner_and_runs_the_fabric_worker_by_default(steward_home):
    store = Store(steward_home)
    job = store.create_job("fix the slugify bug in accents", "/repo")
    runner = FakeRunner(_respond)

    tab_id, runner_pane = launch.launch_tab(
        runner, job, workspace="w3T", agent_args=[], interval=15.0, sock="/tmp/x.sock",
    )

    assert tab_id == "w3T:t2"
    assert runner_pane == "w3T:p2"
    calls = [c["argv"] for c in runner.calls]
    assert calls[0][:6] == ["herdr", "tab", "create", "--workspace", "w3T", "--cwd"]
    assert calls[0][6] == "/repo"
    assert calls[0][7] == "--label"
    assert calls[0][8] == "● slugify bug"  # `<running glyph> <short name>`, no id, no goal dump
    assert calls[0][-1] == "--no-focus"

    assert calls[1] == ["herdr", "pane", "rename", "w3T:p2", f"runner {job.job_id[:8]}"]

    assert calls[2][:4] == ["herdr", "pane", "run", "w3T:p2"]
    command = calls[2][4]
    assert "--worker fabric" in command
    assert "--sock /tmp/x.sock" in command


def test_launch_tab_with_worker_herdr_tui_runs_the_legacy_command(steward_home):
    store = Store(steward_home)
    job = store.create_job("fix the slugify bug in accents", "/repo")
    runner = FakeRunner(_respond)

    tab_id, runner_pane = launch.launch_tab(
        runner, job, workspace="w3T", worker="herdr-tui", kind="claude",
        agent_args=["--model", "sonnet"], interval=15.0,
    )

    assert tab_id == "w3T:t2"
    assert runner_pane == "w3T:p2"
    command = [c["argv"] for c in runner.calls][2][4]
    assert command.endswith("--agent-arg=--model --agent-arg=sonnet")
    assert "--worker herdr --kind claude --pane w3T:p2" in command


def test_launch_tab_raises_when_tab_create_fails():
    def respond(argv, cwd):
        if argv[:3] == ["herdr", "tab", "create"]:
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=1, stdout="", stderr="boom", duration_s=0.0)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    runner = FakeRunner(respond)
    from steward.models import Job, JobState

    job = Job(job_id="abcd1234-x", goal="g", repo="/repo", state=JobState.CONTRACTED, created_at="t", updated_at="t")

    with pytest.raises(RuntimeError):
        launch.launch_tab(runner, job, workspace="w3T", agent_args=[], interval=15.0)


# ------------------------------------------------------------ ensure_control_pane


class _FakeHerdrApi:
    def __init__(self, *, panes=None):
        self.panes = panes if panes is not None else []
        self.calls: list[tuple[str, dict]] = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "pane.list":
            return {"panes": self.panes}
        return {}


def test_ensure_control_pane_is_a_noop_when_one_already_exists(steward_home):
    api = _FakeHerdrApi(panes=[{"pane_id": "w1:existing", "tokens": {"steward_role": "control"}}])
    runner = FakeRunner(lambda argv, cwd: (_ for _ in ()).throw(AssertionError("must not split")))

    result = launch.ensure_control_pane(runner, api, "/tmp/x.sock", "w1:caller")

    assert result == {"pane_id": "w1:existing", "created": False}


def test_ensure_control_pane_splits_and_starts_one_when_none_exists(steward_home):
    api = _FakeHerdrApi(panes=[])

    def respond(argv, cwd):
        if argv[:3] == ["herdr", "pane", "split"]:
            payload = json.dumps({"result": {"pane": {"pane_id": "w1:newctl"}}})
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=payload, stderr="", duration_s=0.0)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    runner = FakeRunner(respond)

    result = launch.ensure_control_pane(runner, api, "/tmp/x.sock", "w1:caller")

    assert result == {"pane_id": "w1:newctl", "created": True}
    run_calls = [c["argv"] for c in runner.calls if c["argv"][:3] == ["herdr", "pane", "run"]]
    assert len(run_calls) == 1
    assert "control" in run_calls[0][4]
    assert "--sock /tmp/x.sock --pane w1:newctl" in run_calls[0][4]


def test_ensure_control_pane_reports_split_failure_without_raising():
    api = _FakeHerdrApi(panes=[])

    def respond(argv, cwd):
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=1, stdout="", stderr="boom", duration_s=0.0)

    runner = FakeRunner(respond)

    result = launch.ensure_control_pane(runner, api, "/tmp/x.sock", "w1:caller")

    assert result["pane_id"] is None
    assert result["created"] is False
    assert "boom" in result["error"]


# --------------------------------------------------------------------- CLI relay
#
# tests/conftest.py's autouse `_inprocess_control_relay` fixture already
# gives every `cli.main([...])` call below the real `_build_control_handlers`
# dispatch table (an in-process `ControlPane`, no real Herdr socket needed).


def test_cmd_launch_persists_herdr_tab_and_prints_summary(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    monkeypatch.setattr(cli, "SubprocessRunner", lambda: FakeRunner(_respond))

    rc = cli.main(["launch", job.job_id, "--workspace", "w3T"])

    assert rc == 0
    out = capsys.readouterr().out
    assert out == f"{job.job_id}\ttab=w3T:t2\trunner=w3T:p2\n"
    assert store.kv_get(job.job_id, "herdr_tab") == "w3T:t2"


def test_cmd_launch_requires_a_workspace_when_env_var_absent(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    monkeypatch.delenv("HERDR_WORKSPACE_ID", raising=False)

    rc = cli.main(["launch", job.job_id])

    assert rc == 2
    assert "workspace" in capsys.readouterr().err


def test_cmd_launch_uses_herdr_workspace_id_env_var_by_default(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w9")
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: FakeRunner(_respond))

    rc = cli.main(["launch", job.job_id])

    assert rc == 0
    assert store.kv_get(job.job_id, "herdr_tab") == "w3T:t2"


def test_cmd_launch_unknown_job_exits_1(steward_home, capsys):
    rc = cli.main(["launch", "no-such-job"])
    assert rc == 1


def test_cmd_launch_with_worker_herdr_tui_flag_reaches_the_legacy_command(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    runner = FakeRunner(_respond)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: runner)

    rc = cli.main(["launch", job.job_id, "--workspace", "w3T", "--worker", "herdr-tui", "--kind", "claude"])

    assert rc == 0
    run_call = [c["argv"] for c in runner.calls if c["argv"][:3] == ["herdr", "pane", "run"]][0]
    assert "--worker herdr --kind claude" in run_call[4]


def test_cmd_launch_no_control_pane_exits_4(steward_home, monkeypatch, capsys):
    """Same failure mode as every other relayed write verb: no live control
    pane means the launch itself never ran (nothing rolled back to undo)."""
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.setattr(cli, "_relay", _real_relay)

    rc = cli.main(["launch", job.job_id, "--workspace", "w3T"])

    assert rc == 4
    assert "no control pane" in capsys.readouterr().err


def test_cmd_launch_ensures_a_control_pane_exists_in_the_workspace(steward_home, monkeypatch, capsys):
    """`_h_launch` calls `launch.ensure_steward_up` (NB-04, SP-36) once sock
    is known, by workspace rather than by splitting off the job's own
    runner pane (that pane is a job tab — cockpit panes never live there)."""
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/x.sock")
    runner = FakeRunner(_respond)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: runner)

    ensured = {}

    def fake_ensure(runner_exec, herdr_api, sock, workspace, **kwargs):
        ensured["args"] = (sock, workspace)
        return {"control_pane": "w3T:ctl", "created": ["control"]}

    monkeypatch.setattr(launch, "ensure_steward_up", fake_ensure)
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: _FakeHerdrApi())

    rc = cli.main(["launch", job.job_id, "--workspace", "w3T"])

    assert rc == 0
    assert ensured["args"] == ("/tmp/x.sock", "w3T")


# --------------------------------------------------------- ensure_advisor_pane


def test_ensure_advisor_pane_is_a_noop_when_one_already_exists(steward_home):
    api = _FakeHerdrApi(panes=[{"pane_id": "w1:existing", "tokens": {"steward_role": "advisor"}}])
    runner = FakeRunner(lambda argv, cwd: (_ for _ in ()).throw(AssertionError("must not split")))

    result = launch.ensure_advisor_pane(runner, api, "/tmp/x.sock", "w1:caller")

    assert result == {"pane_id": "w1:existing", "created": False}


def test_ensure_advisor_pane_splits_and_starts_one_when_none_exists(steward_home):
    api = _FakeHerdrApi(panes=[])

    def respond(argv, cwd):
        if argv[:3] == ["herdr", "pane", "split"]:
            payload = json.dumps({"result": {"pane": {"pane_id": "w1:newadv"}}})
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=payload, stderr="", duration_s=0.0)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    runner = FakeRunner(respond)

    result = launch.ensure_advisor_pane(runner, api, "/tmp/x.sock", "w1:caller")

    assert result == {"pane_id": "w1:newadv", "created": True}
    run_calls = [c["argv"] for c in runner.calls if c["argv"][:3] == ["herdr", "pane", "run"]]
    assert len(run_calls) == 1
    assert "advisor" in run_calls[0][4]
    assert "--sock /tmp/x.sock --pane w1:newadv" in run_calls[0][4]


def test_cmd_launch_ensures_an_advisor_pane_when_mode_is_not_off(steward_home, monkeypatch):
    """`_h_launch` passes `trajectory_on` into `launch.ensure_steward_up`
    (NB-04) based on the effective trajectory mode (TRJ-12)."""
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/x.sock")
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(steward_home / "cfg-does-not-exist"))
    runner = FakeRunner(_respond)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: runner)

    ensured = {}

    def fake_ensure(runner_exec, herdr_api, sock, workspace, **kwargs):
        ensured["args"] = (sock, workspace)
        ensured["trajectory_on"] = kwargs.get("trajectory_on")
        return {"control_pane": "w3T:ctl", "advisor_pane": "w3T:adv", "created": ["control", "advisor"]}

    monkeypatch.setattr(launch, "ensure_steward_up", fake_ensure)
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: _FakeHerdrApi())

    rc = cli.main(["launch", job.job_id, "--workspace", "w3T"])

    assert rc == 0
    assert ensured["args"] == ("/tmp/x.sock", "w3T")
    assert ensured["trajectory_on"] is True  # default mode is shadow, not off


def test_cmd_launch_skips_advisor_pane_when_trajectory_mode_is_off(steward_home, monkeypatch):
    from steward import trajectory as trajectory_module

    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    trajectory_module.set_job_mode(store, job.job_id, "off")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/x.sock")
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(steward_home / "cfg-does-not-exist"))
    runner = FakeRunner(_respond)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: runner)

    ensured = {}

    def fake_ensure(runner_exec, herdr_api, sock, workspace, **kwargs):
        ensured["trajectory_on"] = kwargs.get("trajectory_on")
        return {"control_pane": "w3T:ctl", "created": ["control"]}

    monkeypatch.setattr(launch, "ensure_steward_up", fake_ensure)
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: _FakeHerdrApi())

    rc = cli.main(["launch", job.job_id, "--workspace", "w3T"])

    assert rc == 0
    assert ensured["trajectory_on"] is False


# --------------------------------------------------------- ensure_steward_up
#
# NB-04 (SP-36): `steward up` (and every self-heal caller) never lands a
# fresh control/advisor pair in the caller's own tab or a job tab — when
# neither exists yet, a dedicated "steward" tab is created first.


def test_ensure_steward_up_is_a_noop_when_both_already_exist(steward_home):
    api = _FakeHerdrApi(
        panes=[
            {"pane_id": "w1:ctl", "tokens": {"steward_role": "control"}},
            {"pane_id": "w1:adv", "tokens": {"steward_role": "advisor"}},
        ]
    )
    runner = FakeRunner(lambda argv, cwd: (_ for _ in ()).throw(AssertionError("must not create anything")))

    result = launch.ensure_steward_up(runner, api, "/tmp/x.sock", "w1")

    assert result == {"created": [], "control_pane": "w1:ctl", "advisor_pane": "w1:adv"}


def test_ensure_steward_up_creates_a_dedicated_tab_when_none_exist(steward_home):
    api = _FakeHerdrApi(panes=[])

    def respond(argv, cwd):
        if argv[:3] == ["herdr", "tab", "create"]:
            assert "--workspace" in argv and "w1" in argv
            assert "--label" in argv and "steward" in argv
            payload = json.dumps(
                {"result": {"tab": {"tab_id": "w1:tS"}, "root_pane": {"pane_id": "w1:pctl"}}}
            )
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=payload, stderr="", duration_s=0.0)
        if argv[:3] == ["herdr", "pane", "split"]:
            # the advisor split, off the freshly-created control pane
            assert argv[argv.index("--pane") + 1] == "w1:pctl"
            payload = json.dumps({"result": {"pane": {"pane_id": "w1:padv"}}})
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=payload, stderr="", duration_s=0.0)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    runner = FakeRunner(respond)

    result = launch.ensure_steward_up(runner, api, "/tmp/x.sock", "w1")

    assert result["tab_id"] == "w1:tS"
    assert result["control_pane"] == "w1:pctl"
    assert result["advisor_pane"] == "w1:padv"
    assert result["created"] == ["control", "advisor"]
    run_calls = [c["argv"] for c in runner.calls if c["argv"][:3] == ["herdr", "pane", "run"]]
    assert any("control" in c[4] for c in run_calls)
    assert any("advisor" in c[4] for c in run_calls)


def test_ensure_steward_up_skips_advisor_when_trajectory_off(steward_home):
    api = _FakeHerdrApi(panes=[])

    def respond(argv, cwd):
        if argv[:3] == ["herdr", "tab", "create"]:
            payload = json.dumps(
                {"result": {"tab": {"tab_id": "w1:tS"}, "root_pane": {"pane_id": "w1:pctl"}}}
            )
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=payload, stderr="", duration_s=0.0)
        if argv[:3] == ["herdr", "pane", "split"]:
            raise AssertionError("must not split an advisor pane when trajectory is off")
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    runner = FakeRunner(respond)

    result = launch.ensure_steward_up(runner, api, "/tmp/x.sock", "w1", trajectory_on=False)

    assert result["created"] == ["control"]
    assert "advisor_pane" not in result


def test_ensure_steward_up_reuses_existing_control_pane_in_its_own_tab(steward_home):
    """A control pane already live (wherever its tab is) is never split off
    of again — only a missing advisor gets added, off that same pane."""
    api = _FakeHerdrApi(panes=[{"pane_id": "w1:ctl", "tokens": {"steward_role": "control"}}])

    def respond(argv, cwd):
        if argv[:3] == ["herdr", "tab", "create"]:
            raise AssertionError("must not create a new tab when control already exists")
        if argv[:3] == ["herdr", "pane", "split"]:
            assert argv[argv.index("--pane") + 1] == "w1:ctl"
            payload = json.dumps({"result": {"pane": {"pane_id": "w1:padv"}}})
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=payload, stderr="", duration_s=0.0)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    runner = FakeRunner(respond)

    result = launch.ensure_steward_up(runner, api, "/tmp/x.sock", "w1")

    assert result["created"] == ["advisor"]
    assert result["control_pane"] == "w1:ctl"
    assert result["advisor_pane"] == "w1:padv"


def test_ensure_steward_up_reports_tab_create_failure_without_raising(steward_home):
    api = _FakeHerdrApi(panes=[])
    runner = FakeRunner(
        lambda argv, cwd: CommandResult(argv=argv, cwd=str(cwd), exit_code=1, stdout="", stderr="boom", duration_s=0.0)
    )

    result = launch.ensure_steward_up(runner, api, "/tmp/x.sock", "w1")

    assert result["control_pane"] is None
    assert "boom" in result["error"]


def test_worker_run_command_reexports_steward_env(monkeypatch):
    """11-03 acceptance: a runner pane launched with STEWARD_HOME set looked the job up in the default store."""
    from steward.launch import worker_run_command

    monkeypatch.setenv("STEWARD_HOME", "/tmp/sh home")
    monkeypatch.setenv("STEWARD_EXCEPTIONS", "/tmp/exc.toml")
    monkeypatch.delenv("TMPDIR", raising=False)
    line = worker_run_command("job1", worker="fabric", kind="claude", pane="w1:p1", sock="/s", agent_args=[], interval=5)
    assert line.startswith("export STEWARD_HOME='/tmp/sh home' && export STEWARD_EXCEPTIONS=/tmp/exc.toml && cd ")


def test_worker_run_command_without_env_has_no_exports(monkeypatch):
    from steward.launch import worker_run_command

    monkeypatch.delenv("STEWARD_HOME", raising=False)
    monkeypatch.delenv("STEWARD_EXCEPTIONS", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)
    # Pin the code root: the real deployed revision differs from HEAD in any merge candidate's tree, which would add a
    # STEWARD_LIVE_ROOT export and turn every landing-train suite red (2026-10-08).
    monkeypatch.setattr("steward.launch.code_root", lambda: (Path("/repo"), None))
    line = worker_run_command("job1", worker="fabric", kind="claude", pane="w1:p1", sock="/s", agent_args=[], interval=5)
    assert line.startswith("cd ")


def test_launch_tab_honours_an_explicit_name_and_stores_it(steward_home):
    store = Store(steward_home)
    job = store.create_job("fix the slugify bug in accents", "/repo")
    runner = FakeRunner(_respond)

    launch.launch_tab(runner, job, workspace="w3T", agent_args=[], interval=15.0, store=store, name="Accents fix")

    assert runner.calls[0]["argv"][8] == "● Accents fix"
    assert store.kv_get(job.job_id, "short_name") == "Accents fix"


def test_launch_tab_derives_and_stores_the_name_from_the_goal(steward_home):
    store = Store(steward_home)
    job = store.create_job('Build Steward Phase 18 "ETA" with extras', "/repo")
    runner = FakeRunner(_respond)

    launch.launch_tab(runner, job, workspace="w3T", agent_args=[], interval=15.0, store=store)

    assert runner.calls[0]["argv"][8] == "● P18 ETA"
    assert store.kv_get(job.job_id, "short_name") == "P18 ETA"
    assert launch.launch_label(store, job) == "● P18 ETA"


def test_cmd_launch_name_flag_names_the_tab_and_is_stored(steward_home, monkeypatch):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    runner = FakeRunner(_respond)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: runner)

    assert cli.main(["launch", job.job_id, "--workspace", "w3T", "--name", "P18 ETA"]) == 0

    assert runner.calls[0]["argv"][8] == "● P18 ETA"
    assert store.kv_get(job.job_id, "short_name") == "P18 ETA"
    assert store.kv_get(job.job_id, "herdr_tab_label") == "● P18 ETA"


def test_cmd_launch_without_name_derives_one_from_the_goal(steward_home, monkeypatch):
    store = Store(steward_home)
    job = store.create_job('Build Steward Phase 19 "Installable package"', "/repo")
    runner = FakeRunner(_respond)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: runner)

    assert cli.main(["launch", job.job_id, "--workspace", "w3T"]) == 0

    assert store.kv_get(job.job_id, "short_name") == "P19 Installable"


def test_worker_pane_path_resolves_steward(tmp_path, monkeypatch):
    """RC-4: the worker agent (a child of the shim pane) can run `steward check ...` as a plain command."""
    import subprocess

    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # no `steward` anywhere on the caller's PATH
    line = cli._shim_run_command("J1", sock="s", peer_pane="p", agent="claude", agent_script=None, model=None,
                                 extra_args=[], steward_home=str(tmp_path / "home"))
    prefix = line.split(" && cd ", 1)[0]
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    probe = subprocess.run(["sh", "-c", prefix + " && command -v steward && steward --version"], cwd=tmp_path,
                           env=env, capture_output=True, text=True, timeout=60)
    assert probe.returncode == 0, probe.stderr
    found, version = probe.stdout.strip().splitlines()[-2:]
    assert Path(found).resolve() == (_STEWARD_ROOT / "bin" / "steward").resolve()
    assert version.startswith("steward ") and str(_STEWARD_ROOT) in version  # this tree's CLI, not the cwd's
