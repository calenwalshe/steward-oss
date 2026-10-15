"""Phase 19-03 (G10): the prerequisite model behind `steward doctor`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from steward import prereq
from steward.runner_exec import CommandResult, FakeRunner

POD_ENV = {"HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/tmp/herdr.sock"}
SECRET_STATUS = json.dumps({"loggedIn": True, "email": "jane.doe@example.com", "accountName": "Jane Doe Labs"})


def _res(stdout="", code=0):
    return CommandResult(argv=[], cwd=".", exit_code=code, stdout=stdout, stderr="", duration_s=0.0)


def _answers():
    base = {
        "herdr --version": _res("herdr 0.9.1\n"),
        "git --version": _res("git version 2.43.0\n"),
        "claude --version": _res("2.1.0 (Claude Code)\n"),
        "claude auth status --json": _res(json.dumps({"loggedIn": True})),
        "codex login status": _res("Logged in\n"),
        "systemctl --user is-system-running": _res("running\n"),
    }
    return base


class Env:
    """Everything detect() takes, pointing at a temp tree with every optional thing present."""

    def __init__(self, tmp_path: Path):
        self.home = tmp_path / "home"
        self.config = tmp_path / "config"
        self.home.mkdir()
        self.config.mkdir()
        self.env = dict(POD_ENV)
        self.present = {"herdr", "git", "claude", "codex", "systemctl", "tg", "cloudflared", "uv"}
        self.answers = _answers()
        self.plugins = ["/plugins/superpowers/5.0"]
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"research": {"command": "x"}}}))
        (self.config / "mcp.json").write_text(json.dumps({"public_base_url": "https://steward.example.com"}))
        (self.config / "steward.toml").write_text("[alerts]\ntelegram = true\n")
        (self.config / "keys.env").write_text("TYPESAFE_API_KEY=fake-value\n")
        (self.config / "keys.env").chmod(0o600)

    def run(self, python_version=(3, 12, 4), runner=None):
        runner = runner or FakeRunner(dict(self.answers))
        return {c.id: c for c in prereq.detect(
            runner, env=self.env, which=lambda n: f"/usr/bin/{n}" if n in self.present else None,
            home=self.home, config_dir=self.config, python_version=python_version,
            plugin_dirs=lambda: list(self.plugins),
        )}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("STEWARD_CLAUDE_JSON", raising=False)
    monkeypatch.setenv("STEWARD_CLAUDE_DIR", str(tmp_path / "home" / ".claude"))
    return Env(tmp_path)


def test_required_all_ok(world):
    got = world.run()
    required = [c for c in got.values() if c.level == prereq.REQUIRED]
    assert {c.id for c in required} == {"herdr_pane", "herdr_version", "python", "git", "claude"}
    assert all(c.status == prereq.OK for c in required)
    assert got["herdr_version"].detail == "0.9.1" and got["python"].detail == "3.12.4"
    assert got["claude"].detail == "logged in"
    assert prereq.required_missing(list(got.values())) == []


def test_each_required_missing_has_a_fix(world):
    world.env.pop("HERDR_ENV")
    c = world.run()["herdr_pane"]
    assert c.status == prereq.MISSING and c.fix.kind == "command" and c.fix.text == "run steward inside a Herdr pane"

    world.env.update(POD_ENV)
    world.answers["herdr --version"] = _res("herdr 0.8.9\n")
    c = world.run()["herdr_version"]
    assert c.status == prereq.MISSING and prereq.TESTED_HERDR in c.fix.text

    world.answers["herdr --version"] = _res("herdr 0.9.1\n")
    c = world.run(python_version=(3, 11, 9))["python"]
    assert c.status == prereq.MISSING and (c.fix.kind, c.fix.id, c.fix.text) == ("local", "uv_python", "uv python install 3.12")
    world.present.discard("uv")
    c = world.run(python_version=(3, 11, 9))["python"]
    assert c.fix.kind == "command" and c.fix.id == ""
    world.present.add("uv")

    world.present.discard("git")
    c = world.run()["git"]
    assert c.status == prereq.MISSING and c.fix.text == "install git"
    world.present.add("git")

    world.present.discard("claude")
    c = world.run()["claude"]
    assert c.status == prereq.MISSING and c.fix.text == "install the Claude Code CLI"
    world.present.add("claude")

    world.answers["claude auth status --json"] = _res(json.dumps({"loggedIn": False}))
    c = world.run()["claude"]
    assert c.status == prereq.MISSING and c.fix.text == "claude auth login"


def test_claude_output_is_never_kept(world):
    world.answers["claude auth status --json"] = _res(SECRET_STATUS)
    checks = world.run()
    blob = json.dumps(prereq.to_json(list(checks.values()))) + prereq.render_text(list(checks.values()))
    blob += repr(list(checks.values()))
    for secret in ("jane.doe", "example.com", "Jane Doe", "Labs"):
        assert secret not in blob


def test_optional_states_are_named(world):
    got = world.run()
    assert got["jev"].status == prereq.OK and got["jev"].detail == "set (keys.env)"
    (world.config / "keys.env").unlink()
    assert (world.run()["jev"].status, world.run()["jev"].detail) == (prereq.OFF, "off (no key)")
    assert world.run()["jev"].fix.id == "keys_env"

    world.present.discard("codex")
    assert world.run()["codex"].detail == "off (no codex)"
    world.present.add("codex")
    world.answers["codex login status"] = _res("", 1)
    assert world.run()["codex"].detail == "off (not logged in)"

    (world.home / ".claude.json").write_text(json.dumps({"mcpServers": {}}))
    assert world.run()["research"].detail == "off (no research server)"

    world.present.discard("systemctl")
    assert world.run()["systemd"].detail == "off (no systemd user manager)"
    world.present.add("systemctl")
    world.answers["systemctl --user is-system-running"] = _res("offline\n", 1)
    assert world.run()["systemd"].detail == "off (no systemd user manager)"

    (world.config / "mcp.json").write_text("{}")
    assert world.run()["connector"].detail == "off (no public connector)"

    (world.config / "steward.toml").write_text("[alerts]\ntelegram = false\n")
    assert world.run()["telegram"].detail == "off (alerts off)"
    world.present.discard("tg")
    assert world.run()["telegram"].detail == "off (no tg)"

    world.plugins = []
    assert world.run()["plugins"].detail == "off (workers run without them)"
    (world.home / ".claude" / "skills" / "gsd-plan-phase").mkdir(parents=True)
    assert world.run()["plugins"].status == prereq.OK

    world.present.discard("cloudflared")
    assert world.run()["cloudflared"].detail == "off (no cloudflared)"
    assert all(c.status in (prereq.OK, prereq.OFF) for c in world.run().values() if c.level == prereq.OPTIONAL)


def test_a_failing_check_never_raises(world):
    def answer(argv, cwd):
        if argv[:2] == ["git", "--version"]:
            raise TimeoutError("slow")
        return world.answers.get(" ".join(argv), _res())

    got = world.run(runner=FakeRunner(answer))
    assert got["git"].detail == "check failed: TimeoutError" and got["git"].status == prereq.MISSING
    assert got["herdr_version"].status == prereq.OK and len(got) == len(prereq.CHECKS)


def test_subprocess_calls_carry_a_timeout(world):
    runner = FakeRunner(dict(world.answers))
    world.run(runner=runner)
    assert runner.calls and all(call["timeout"] == prereq.TIMEOUT_S for call in runner.calls)


def test_render_text_shape(world):
    world.present.discard("codex")
    world.present.discard("git")
    checks = list(world.run().values())
    text = prereq.render_text(checks)
    lines = text.splitlines()
    assert len(lines) == len(checks)
    assert all(line[0] in "✓✗–" for line in lines)
    assert all(len(line) <= 76 for line in lines)
    marks = "".join(line[0] for line in lines)
    assert marks[:5] == "✓✓✓✗✓"  # the five required rows come first (git is missing here)
    assert "✗" not in marks[5:]
    git_line = next(line for line in lines if line.startswith("✗ git"))
    assert "→ install git" in git_line
    assert any(line.startswith("– codex") and "off (no codex)" in line for line in lines)
    assert json.loads(json.dumps(prereq.to_json(checks)))[0]["id"] == "herdr_pane"


def test_detect_creates_nothing(world, tmp_path):
    before = sorted(p for p in tmp_path.rglob("*"))
    world.run()
    assert sorted(p for p in tmp_path.rglob("*")) == before


def test_render_text_never_truncates_a_fix(world):
    for tool in list(world.present):
        world.present.discard(tool)
    world.env.clear()
    world.home.joinpath(".claude.json").unlink()
    for f in world.config.iterdir():
        f.unlink()
    text = prereq.render_text(list(world.run(python_version=(3, 11, 0)).values()))
    assert "…" not in text and all(len(line) <= 76 for line in text.splitlines())


def test_light_detect_spawns_nothing_and_returns_the_subset(world):
    class Boom:
        def run(self, *a, **k):
            raise AssertionError("light detect must not run a subprocess")

        shell_line = run

    got = {c.id: c for c in prereq.detect(
        Boom(), env=world.env, which=lambda n: f"/usr/bin/{n}" if n in world.present else None,
        home=world.home, config_dir=world.config, plugin_dirs=lambda: list(world.plugins), light=True)}
    assert set(got) == set(prereq.LIGHT_IDS)
    assert got["codex"].status == prereq.OK and got["codex"].detail == "found"
    assert got["connector"].status == prereq.OK and got["telegram"].status == prereq.OK
    world.present.discard("codex")
    off = prereq.detect(None, env=world.env, which=lambda n: None, home=world.home, config_dir=world.config,
                        plugin_dirs=lambda: [], light=True)
    assert {c.id for c in off} == set(prereq.LIGHT_IDS)
    assert {c.id: c.detail for c in off}["codex"] == "off (no codex)"
