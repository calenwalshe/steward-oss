"""Phase 19-03 (G2/G17): package mode is an opt-in environment layer; repo mode is byte-identical."""

from __future__ import annotations

import json
import shlex
import sys

import pytest

from steward import chat, cli, dashact, launch, pkgenv
from steward.runner_exec import CommandResult, FakeRunner

NASTY = 'Sam "Q" $(touch /tmp/pwned) `id` \'x\' ; rm -rf /'


@pytest.fixture
def repo_mode(monkeypatch):
    for name in pkgenv.FORWARD:
        if name not in ("HOME", "PATH"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)  # forwarded only when set; these strings pin the unset case
    monkeypatch.setenv("STEWARD_HOME", "/tmp/sh")
    monkeypatch.setenv("STEWARD_EXCEPTIONS", "/tmp/exc.toml")


@pytest.fixture
def package_mode(repo_mode, monkeypatch, tmp_path):
    monkeypatch.setenv("STEWARD_INSTALL_ROOT", "/opt/steward pkg")
    monkeypatch.setenv("STEWARD_CONFIG_DIR", "/cfg/dir")
    monkeypatch.setenv("STEWARD_OWNER_NAME", NASTY)
    monkeypatch.setenv("HOME", "/home/u")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)


def _exports(line: str) -> dict:
    """The NAME=value pairs of the leading `export ... &&` segments, parsed back the way a shell would."""
    tokens = shlex.split(line)
    out, i = {}, 0
    while i < len(tokens) and tokens[i] == "export":
        name, _, value = tokens[i + 1].partition("=")
        out[name] = value
        assert tokens[i + 2] == "&&"
        i += 3
    return out


def _control_line() -> str:
    runner = FakeRunner()
    launch._start_control_on(runner, "/s.sock", "p1")
    return next(c["argv"][4] for c in runner.calls if c["argv"][:3] == ["herdr", "pane", "run"])


def _advisor_line() -> str:
    class Api:
        def call(self, method, params):
            return {"panes": []}

    def respond(argv, cwd):
        out = json.dumps({"result": {"pane": {"pane_id": "pa"}}}) if argv[:3] == ["herdr", "pane", "split"] else ""
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=out, stderr="", duration_s=0.0)

    runner = FakeRunner(respond)
    launch.ensure_advisor_pane(runner, Api(), "/s.sock", "w1:caller")
    return next(c["argv"][4] for c in runner.calls if c["argv"][:3] == ["herdr", "pane", "run"])


def _shim_line() -> str:
    return cli._shim_run_command("j1", sock="/s", peer_pane="w1:p1", agent="claude", agent_script=None,
                                 model=None, extra_args=[], steward_home="/tmp/sh")


def _worker_line() -> str:
    return launch.worker_run_command("j1", worker="fabric", kind="claude", pane="p", sock="/s", agent_args=[], interval=15.0)


def _pane_lines() -> dict:
    return {"worker": _worker_line(), "watchers": launch.watchers_command("j1"), "control": _control_line(),
            "advisor": _advisor_line(), "shim": _shim_line()}


def test_repo_mode_is_empty(repo_mode):
    assert pkgenv.env_exports() == ""
    assert pkgenv.cli_argv() == ["python3", "-m", "steward.cli"]
    assert pkgenv.cli_command() == "python3 -m steward.cli"
    old_way = "export STEWARD_HOME=/tmp/sh && export STEWARD_EXCEPTIONS=/tmp/exc.toml && "
    assert launch._env_exports() == old_way


def test_package_mode_forwards_the_environment(package_mode):
    text = pkgenv.env_exports()
    assert text.endswith(" && ") and text.count("export ") == text.count(" && ")
    got = _exports(text)
    assert list(got)[0] == "PYTHONPATH" and got["PYTHONPATH"] == "/opt/steward pkg"
    assert got["STEWARD_INSTALL_ROOT"] == "/opt/steward pkg"
    assert got["STEWARD_OWNER_NAME"] == NASTY  # quoted so no character is interpreted by the shell
    assert got["HOME"] == "/home/u" and got["PATH"] == "/usr/bin:/bin"
    assert "XDG_DATA_HOME" not in got and "STEWARD_DATA_DIR" not in got  # unset ones are skipped
    order = [n for n in pkgenv.FORWARD if n in got]
    assert list(got)[1:] == order  # a fixed order
    assert pkgenv.cli_argv() == [sys.executable, "-m", "steward.cli"]
    assert pkgenv.cli_command() == " ".join(shlex.quote(p) for p in [sys.executable, "-m", "steward.cli"])
    assert _exports(pkgenv.env_exports(skip=("HOME", "PATH"))).keys() == got.keys() - {"HOME", "PATH"}


def test_pane_commands_carry_the_exports_in_package_mode(package_mode):
    for name, line in _pane_lines().items():
        assert " && cd " in line, name
        before_cd = line.split(" && cd ", 1)[0] + " && "
        got = _exports(before_cd)
        assert got["PYTHONPATH"] == "/opt/steward pkg", name
        assert got["STEWARD_OWNER_NAME"] == NASTY, name
        assert got["STEWARD_HOME"] == "/tmp/sh", name
        assert before_cd.count("export STEWARD_HOME=") == 1, name  # not exported twice


def test_pane_commands_in_repo_mode_are_unchanged(repo_mode):
    root = str(launch._STEWARD_ROOT)
    home = "export STEWARD_HOME=/tmp/sh && export STEWARD_EXCEPTIONS=/tmp/exc.toml && "
    py = sys.executable
    lines = _pane_lines()
    assert lines["watchers"] == f"{home}cd {root} && {py} -m steward.cli watchers j1 --follow"
    assert lines["control"] == f"{home}cd {root} && {py} -m steward.cli control --sock /s.sock --pane p1"
    assert lines["advisor"].startswith(f"{home}cd {root} && {py} -m steward.cli advisor --sock /s.sock --pane pa")
    shim_path = f"export STEWARD_PYTHON={py} && export PATH={root}/bin:\"$PATH\" && "  # RC-4: `steward` for the agent
    assert lines["shim"].startswith(f"export STEWARD_HOME=/tmp/sh && {shim_path}cd {root} && {py} -m steward.cli shim j1")
    assert lines["worker"].startswith(f"{home}cd {root} && {py} -m steward.cli run j1 --worker fabric")


def test_phone_tab_goes_through_launch(package_mode):
    from steward import phone

    line = phone.phone_command()
    assert _exports(line.split(" && cd ", 1)[0] + " && ")["PYTHONPATH"] == "/opt/steward pkg"


def test_dashact_uses_the_selected_interpreter(repo_mode, monkeypatch):
    assert dashact.argv_for("accept", "j1")[:3] == ["python3", "-m", "steward.cli"]
    monkeypatch.setenv("STEWARD_INSTALL_ROOT", "/opt/steward")
    assert dashact.argv_for("accept", "j1")[:3] == [sys.executable, "-m", "steward.cli"]
    assert dashact.argv_for("accept", "j1")[3:] == ["accept", "j1", "--origin", "dash"]


def test_job_chat_commands_follow_package_mode(repo_mode, monkeypatch):
    import owner_golden as og

    repo = og.chat_text()
    assert repo["allowed_tools"][3] == "Bash(python3 -m steward.cli status JOB123*)"
    assert "`python3 -m steward.cli <verb> " in repo["primer_job"]
    assert "python3 -m steward.cli answer" in repo["questions_block"][2]

    monkeypatch.setenv("STEWARD_INSTALL_ROOT", "/opt/steward")
    prefix = pkgenv.cli_command()
    pkg = og.chat_text()
    assert pkg["allowed_tools"][3] == f"Bash({prefix} status JOB123*)"
    # the permission pattern always matches what the model is told to run
    assert all(r.startswith(f"Bash({prefix} ") for r in pkg["allowed_tools"][3:])
    assert f"`{prefix} <verb> " in pkg["primer_job"]
    assert " python3 -m steward.cli" not in pkg["primer_job"].replace(prefix, "")
    assert f"{prefix} answer" in pkg["questions_block"][2]
    assert chat.job_allowed_tools("J")[3] == f"Bash({prefix} status J*)"
