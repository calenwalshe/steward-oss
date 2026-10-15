"""Phase 19-06 (G13, G14): `steward enable mcp` end to end: plan, confirm, generated unit, services-tab fallback.

Every external call (systemctl, herdr) goes through a FakeRunner; HOME, XDG_CONFIG_HOME, STEWARD_HOME and the config
dir are temporary, so no real unit, key or Herdr session is ever touched.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from fake_herdr import FakeHerdr
from steward import cli, components, installstate
from steward.runner_exec import CommandResult, FakeRunner

SYSTEMD_OK = "running\n"


class Env:
    """A temp install: directories, install.json, a fake systemctl + herdr behind one runner."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.state_dir = tmp_path / "state"
        self.config = tmp_path / "config"
        self.xdg = tmp_path / "xdg"
        self.unit_dir = self.xdg / "systemd" / "user"
        for name, value in (("HOME", self.home), ("STEWARD_HOME", self.state_dir), ("STEWARD_CONFIG_DIR", self.config),
                            ("XDG_CONFIG_HOME", self.xdg), ("STEWARD_DATA_DIR", tmp_path / "data")):
            monkeypatch.setenv(name, str(value))
        for name in ("STEWARD_INSTALL_ROOT", "STEWARD_NODE", "STEWARD_SHIM", "HERDR_PANE_ID", "STEWARD_API_KEYS_FILE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("STEWARD_LEGACY_KEYS_FILE", str(tmp_path / "none.env"))
        monkeypatch.setenv("HERDR_WORKSPACE_ID", "w1")
        self.monkeypatch = monkeypatch
        self.home.mkdir()
        self.state_dir.mkdir()
        self.config.mkdir()
        self.fake = FakeHerdr("w1")
        self.misc: list[list[str]] = []
        self.fail: dict[str, CommandResult] = {}
        self.systemd_running = True
        self.runner = FakeRunner(self._handle)
        self.out: list[str] = []
        self.answers: dict[str, str] = {}
        self.secrets: dict[str, str] = {}
        self.save_state(installstate.new_state("0.1.0", "/pkg", "3.12.3"))

    def _handle(self, argv, cwd):
        if argv[0] == "herdr" and argv[1:2] != ["plugin"]:
            return self.fake._handle(argv, cwd)
        self.misc.append(list(argv))
        key = " ".join(argv)
        for needle, res in self.fail.items():
            if needle in key:
                return res
        if argv[:3] == ["systemctl", "--user", "is-system-running"]:
            return CommandResult(list(argv), ".", 0 if self.systemd_running else 1,
                                 SYSTEMD_OK if self.systemd_running else "offline\n", "", 0.0)
        return CommandResult(list(argv), ".", 0, "", "", 0.0)

    def save_state(self, state):
        installstate.save(state, self.state_dir)

    @property
    def state(self):
        return installstate.load(self.state_dir)

    def systemctl(self):
        return [" ".join(a[2:]) for a in self.misc if a[:2] == ["systemctl", "--user"]]

    def ask(self, prompt):
        for needle, value in self.answers.items():
            if needle in prompt:
                return value
        raise AssertionError(f"unexpected prompt: {prompt!r}")

    def ask_secret(self, prompt):
        for needle, value in self.secrets.items():
            if needle in prompt:
                return value
        raise AssertionError(f"unexpected secret prompt: {prompt!r}")

    def ctx(self, **kw):
        base = dict(runner=self.runner, ask=self.ask, ask_secret=self.ask_secret, out=self.out.append,
                    which=lambda n: f"/usr/bin/{n}", systemd=True, workspace="w1")
        base.update(kw)
        return components.make_ctx(**base)

    def text(self):
        return "\n".join(self.out)


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


# -- status / plan ---------------------------------------------------------------------------------------------------


def test_everything_is_off_after_init(env, capsys):
    rows = components.status(env.state)
    assert [r[0] for r in rows] == list(components.COMPONENT_IDS)
    assert len(rows) == 8
    assert all(not on and text == "off (not enabled)" for _id, on, text in rows)
    assert cli.main(["components"]) == 0
    out = capsys.readouterr().out
    for cid in components.COMPONENT_IDS:
        line = next(line for line in out.splitlines() if line.startswith(cid + " "))
        assert "off (not enabled)" in line


def test_plan_text_lists_writes_starts_and_exposure(env):
    plan = components.plan("mcp", env.ctx())
    text = plan.text()
    assert str(env.unit_dir / "steward-mcp.service") in text
    assert "systemctl --user daemon-reload" in text
    assert "systemctl --user enable --now steward-mcp" in text
    assert "127.0.0.1" in text and "8765" in text
    assert "nothing beyond loopback" in text
    assert plan.blockers == []


def test_plan_blocker_carries_the_exact_fix(env):
    plan = components.plan("mcp", env.ctx(systemd=False, workspace=None))
    assert len(plan.blockers) == 1
    assert "no systemd user manager" in plan.blockers[0] and "Herdr" in plan.blockers[0]
    assert "steward enable mcp" in plan.blockers[0]


# -- enable mcp (systemd) --------------------------------------------------------------------------------------------


def _default_dirs(env):
    """Move the fixture's Steward dirs to their DEFAULT locations under the fixture's HOME / XDG dirs, so a unit
    carries no Environment= line for them. Otherwise the unit embeds tmp_path, and the 'no home path' checks would
    depend on where pytest's basetemp lives (it fails when TMPDIR is under /home)."""
    import shutil
    state = env.home / ".local" / "state" / "steward"
    shutil.copytree(env.state_dir, state)
    env.state_dir = state
    env.monkeypatch.setenv("STEWARD_HOME", str(state))
    env.monkeypatch.setenv("STEWARD_CONFIG_DIR", str(env.xdg / "steward"))
    env.monkeypatch.setenv("XDG_DATA_HOME", str(env.tmp / "xdg-data"))
    env.monkeypatch.setenv("STEWARD_DATA_DIR", str(env.tmp / "xdg-data" / "steward"))


def test_unit_has_no_environment_lines_for_default_dirs_of_the_installing_home(env):
    _default_dirs(env)
    text = components.unit_text(env.ctx(), "mcp")
    assert "STEWARD_HOME" not in text and "STEWARD_CONFIG_DIR" not in text and "STEWARD_DATA_DIR" not in text


def test_enable_mcp_with_systemd(env):
    _default_dirs(env)
    env.monkeypatch.setenv("STEWARD_INSTALL_ROOT", "/opt/steward-pkg")
    env.monkeypatch.setenv("STEWARD_NODE", "/opt/node/bin/node")
    env.monkeypatch.setenv("STEWARD_SHIM", "/opt/steward-pkg/bin/steward.js")
    code = components.enable("mcp", env.ctx(interactive=True, ask=lambda p: ""))
    assert code == 0, env.text()
    unit = env.unit_dir / "steward-mcp.service"
    text = unit.read_text()
    assert oct(unit.stat().st_mode & 0o777) == "0o644"
    assert "ExecStart=/opt/node/bin/node /opt/steward-pkg/bin/steward.js service run mcp" in text
    assert "WorkingDirectory" not in text
    assert "EnvironmentFile" not in text
    # The verifier may place pytest's temporary directory under STEWARD_HOME,
    # so isolated test paths can legitimately contain /home/<user>. Assert the
    # unit carries exactly the fixture's sandbox paths instead of banning that
    # prefix (which also occurs in verifier-generated temp paths).
    environment = {
        line.split("=", 2)[1]: line.split("=", 2)[2]
        for line in text.splitlines()
        if line.startswith("Environment=")
    }
    # _default_dirs moved every Steward dir to its default, so none needs an Environment= line.
    assert environment == {"PYTHONUNBUFFERED": "1"}
    assert "NoNewPrivileges=yes" in text and "UMask=0077" in text
    assert env.systemctl() == ["daemon-reload", "enable --now steward-mcp"]
    rec = env.state["components"]["mcp"]
    assert rec["enabled"] is True and rec["mode"] == "systemd" and rec["units"] == ["steward-mcp"]
    assert rec["files"] == [str(unit)] and rec["port"] == 8765
    assert any(f["path"] == str(unit) and f["kind"] == "unit" for f in env.state["files"])
    assert "loginctl enable-linger" in env.text()


def test_unit_text_in_repo_mode_uses_python_and_working_directory(env):
    assert components.enable("mcp", env.ctx(interactive=True, ask=lambda p: "y")) == 0
    text = (env.unit_dir / "steward-mcp.service").read_text()
    root = str(Path(components.version.install_root()))
    assert f"ExecStart={sys.executable} -m steward.cli service run mcp" in text
    assert f"WorkingDirectory={root}" in text
    assert "EnvironmentFile" not in text


def test_percent_and_spaces_are_escaped(env):
    env.monkeypatch.setenv("STEWARD_INSTALL_ROOT", "/opt/my pkg%x")
    env.monkeypatch.setenv("STEWARD_NODE", "/opt/n ode/node")
    env.monkeypatch.setenv("STEWARD_SHIM", "/opt/my pkg%x/steward.js")
    assert components.enable("mcp", env.ctx(yes=True)) == 0
    text = (env.unit_dir / "steward-mcp.service").read_text()
    assert 'ExecStart="/opt/n ode/node" "/opt/my pkg%%x/steward.js" service run mcp' in text


def test_unit_environment_lines_only_for_set_non_default_dirs(env):
    assert components.enable("mcp", env.ctx(yes=True)) == 0
    text = (env.unit_dir / "steward-mcp.service").read_text()
    assert f"Environment=STEWARD_HOME={env.state_dir}" in text
    assert f"Environment=STEWARD_CONFIG_DIR={env.config}" in text
    assert "API_KEY" not in text and "SECRET" not in text and "TOKEN" not in text


def test_enable_declined_or_blocked_writes_nothing(env):
    before = (env.state_dir / "install.json").read_text()
    assert components.enable("mcp", env.ctx(interactive=True, ask=lambda p: "n")) == 1
    assert not env.unit_dir.exists() and env.systemctl() == []
    assert (env.state_dir / "install.json").read_text() == before
    env.out.clear()
    assert components.enable("mcp", env.ctx(systemd=False, workspace=None, yes=True)) == 1
    assert "no systemd user manager" in env.text()
    assert (env.state_dir / "install.json").read_text() == before


def test_enable_needs_yes_or_a_terminal(env):
    assert components.enable("mcp", env.ctx(interactive=False)) == 1
    assert "--yes" in env.text() and not env.unit_dir.exists()


def test_enable_is_idempotent(env):
    assert components.enable("mcp", env.ctx(yes=True)) == 0
    calls = len(env.misc)
    env.out.clear()
    assert components.enable("mcp", env.ctx(yes=True)) == 0
    assert "already enabled" in env.text()
    assert len(env.misc) == calls


def test_existing_foreign_unit_is_never_overwritten(env):
    env.unit_dir.mkdir(parents=True)
    foreign = env.unit_dir / "steward-mcp.service"
    foreign.write_text("# somebody's own unit\n")
    assert components.enable("mcp", env.ctx(yes=True)) == 1
    assert "unit steward-mcp.service already exists and was not installed by this package" in env.text()
    assert foreign.read_text() == "# somebody's own unit\n"
    assert env.systemctl() == []
    assert "mcp" not in (env.state["components"] or {})


def test_a_failing_systemctl_rolls_the_files_back(env):
    env.fail["enable --now steward-mcp"] = CommandResult([], ".", 1, "", "boom", 0.0)
    assert components.enable("mcp", env.ctx(yes=True)) == 1
    assert "boom" in env.text()
    assert not (env.unit_dir / "steward-mcp.service").exists()
    assert env.state["files"] == [] and "mcp" not in env.state["components"]


def test_enable_refuses_without_an_install_record(env):
    (env.state_dir / "install.json").unlink()
    assert components.enable("mcp", env.ctx(yes=True)) == 1
    assert "steward init" in env.text() and not env.unit_dir.exists()


def test_unknown_component(env):
    assert components.enable("nope", env.ctx(yes=True)) == 2
    assert "unknown component" in env.text()


# -- enable mcp without systemd: the services tab --------------------------------------------------------------------


def test_enable_without_systemd_uses_the_services_tab(env):
    assert components.enable("mcp", env.ctx(systemd=False, yes=True)) == 0, env.text()
    rec = env.state["components"]["mcp"]
    assert rec["enabled"] and rec["mode"] == "herdr" and rec["targets"] == ["mcp"]
    assert not env.unit_dir.exists() and env.systemctl() == []
    (tab,) = env.fake.tabs_labelled("services")
    (pane,) = env.fake.panes_in(tab["tab_id"])
    assert pane["label"] == "svc mcp"
    (cmd,) = env.fake.runs()[pane["pane_id"]]
    assert "service run mcp" in cmd
    assert "services" in env.state["tabs"]
    assert any("(services tab)" in text for _id, on, text in components.status(env.state) if on)


def test_ensure_services_tab_is_idempotent_and_revives_in_place(env):
    components.enable("mcp", env.ctx(systemd=False, yes=True))
    state = env.state
    before = len(env.fake.mutating_calls())
    res = components.ensure_services_tab(env.runner, "w1", state, "/w")
    assert res["created"] == [] and res["revived"] == []
    assert len(env.fake.mutating_calls()) == before
    pane = env.fake.pane_by_label("svc mcp")
    pane["foreground"] = [["bash"]]  # the process died
    res = components.ensure_services_tab(env.runner, "w1", state, "/w")
    assert res["revived"] == ["mcp"] and res["created"] == []
    assert len(env.fake.tabs_labelled("services")) == 1
    assert len(env.fake.runs()[pane["pane_id"]]) == 2


def test_no_herdr_mode_components_no_tab(env):
    components.enable("mcp", env.ctx(systemd=True, yes=True))
    res = components.ensure_services_tab(env.runner, "w1", env.state, "/w")
    assert res["created"] == [] and env.fake.tabs_labelled("services") == []


def test_up_and_init_call_ensure_services_tab(env):
    from steward import scaffold

    components.enable("mcp", env.ctx(systemd=False, yes=True))
    env.fake.tabs = [t for t in env.fake.tabs if t["label"] != "services"]
    env.fake.panes = [p for p in env.fake.panes if p["label"] != "svc mcp"]
    state = env.state
    d = scaffold.Detected(home=env.state_dir, config_dir=env.config, version="0.1.0", install_root="/pkg",
                          python="3.12.3", cwd="/w", sock="/tmp/s", workspace="w1", install=state)
    ctx = scaffold.Ctx(runner=env.runner, herdr_api=env.fake.api, sock="/tmp/s", detected=d)
    out = scaffold.upgrade_if_needed(ctx)
    assert env.fake.pane_by_label("svc mcp") is not None, out
    assert "services" in installstate.load(env.state_dir)["tabs"]


def test_disable_undoes_exactly_the_record(env):
    components.enable("mcp", env.ctx(yes=True))
    unit = env.unit_dir / "steward-mcp.service"
    env.misc.clear()
    env.out.clear()
    assert components.disable("mcp", env.ctx()) == 0
    assert env.systemctl() == ["disable --now steward-mcp", "daemon-reload"]
    assert not unit.exists()
    rec = env.state["components"]["mcp"]
    assert rec["enabled"] is False
    assert env.state["files"] == []
    assert components.status(env.state)[0][2] == "off (not enabled)"


def test_disable_leaves_a_file_the_user_changed(env):
    components.enable("mcp", env.ctx(yes=True))
    unit = env.unit_dir / "steward-mcp.service"
    unit.write_text(unit.read_text() + "# my edit\n")
    env.out.clear()
    assert components.disable("mcp", env.ctx()) == 0
    assert unit.exists() and "changed since install" in env.text() and str(unit) in env.text()


def test_disable_in_herdr_mode_closes_the_pane_and_empty_tab(env):
    components.enable("mcp", env.ctx(systemd=False, yes=True))
    assert components.disable("mcp", env.ctx()) == 0
    assert env.fake.pane_by_label("svc mcp") is None
    assert env.fake.tabs_labelled("services") == []
    assert env.state["components"]["mcp"]["enabled"] is False


def test_disable_of_a_never_enabled_component(env):
    before = (env.state_dir / "install.json").read_text()
    assert components.disable("mcp", env.ctx()) == 0
    assert "not enabled" in env.text() and env.misc == []
    assert (env.state_dir / "install.json").read_text() == before


def test_cli_enable_and_disable(env, monkeypatch):
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: env.runner)
    monkeypatch.setattr(components, "_detect_systemd", lambda ctx: True)
    assert cli.main(["enable", "mcp", "--yes"]) == 0
    assert (env.unit_dir / "steward-mcp.service").exists()
    assert cli.main(["disable", "mcp"]) == 0
    assert not (env.unit_dir / "steward-mcp.service").exists()
    assert json.loads((env.state_dir / "install.json").read_text())["components"]["mcp"]["enabled"] is False


def test_cli_enable_with_no_component_lists_them(env, capsys):
    assert cli.main(["enable"]) == 0
    out = capsys.readouterr().out
    assert all(cid in out for cid in components.COMPONENT_IDS)
