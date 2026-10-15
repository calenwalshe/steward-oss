"""Phase 19-04 (G10): the status pane's services block is data-driven and names every off state."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from steward import dash_services, installstate
from steward.dash_services import lines

REPO = Path(__file__).resolve().parent.parent
ANSI = re.compile(r"\033\[[0-9;]*m")


def _plain(rows):
    return [ANSI.sub("", r) for r in rows]


class World:
    """Injected dependencies pointing at an empty machine (nothing optional present)."""

    def __init__(self, tmp_path):
        self.config = tmp_path / "config"
        self.config.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.present: set[str] = set()
        self.active: dict[str, str] = {}
        self.key = "missing"
        self.probed: list[str] = []
        self.env = {"STEWARD_CLAUDE_DIR": str(tmp_path / "claude"), "STEWARD_CLAUDE_JSON": str(tmp_path / "none.json")}

    def lines(self, state, **kw):
        from steward import prereq

        checks = prereq.detect(None, env=self.env, which=lambda n: f"/bin/{n}" if n in self.present else None,
                               home=self.home, config_dir=self.config, plugin_dirs=lambda: [], light=True)

        def probe(url):
            self.probed.append(url)
            return "200"

        return lines(state, unit_active=lambda u: self.active.get(u, "inactive"), keys_status=lambda n: self.key,
                     probe=kw.pop("probe", probe), checks=checks, config_dir=self.config, env=self.env, **kw)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_CLAUDE_DIR", str(tmp_path / "claude"))
    return World(tmp_path)


def _state(**components):
    st = installstate.new_state("0.1.0", "/opt/steward", python="3.12.0", now="2026-01-01T00:00:00+00:00")
    st["components"] = components
    return st


def test_lines_for_a_fresh_install(world):
    rows = _plain(world.lines(_state()))
    text = "\n".join(rows)
    assert all("–" in r for r in rows), text  # nothing is on
    assert "●" not in text
    for needle in ("off (no key)", "off (no codex)", "off (no public connector)", "off (no tg)", "off (no cloudflared)",
                   "off (workers run without them)", "off (no research server)"):
        assert needle in text, needle
    assert not any("200" in r for r in rows) and world.probed == []
    assert not any(u in text for u in dash_services.LEGACY_UNITS)


def test_alerts_off_reads_as_named_state(world):
    world.present.add("tg")
    (world.config / "steward.toml").write_text("[alerts]\ntelegram = false\n")
    assert "off (alerts off)" in "\n".join(_plain(world.lines(_state())))


def test_enabled_components_show_their_unit_state(world):
    st = _state(mcp={"enabled": True, "mode": "systemd", "units": ["steward-mcp"]})
    world.active["steward-mcp"] = "active"
    row = world.lines(st)[0]
    assert "\033[32m●" in row and _plain([row])[0].split()[1:] == ["mcp", "active"]
    world.active["steward-mcp"] = "failed"
    row = world.lines(st)[0]
    assert "\033[31m●" in row and _plain([row])[0].split()[1:] == ["mcp", "inactive"]

    herdr = _state(mcp={"enabled": True, "mode": "herdr", "units": []})
    assert _plain([world.lines(herdr)[0]])[0].split()[1:] == ["mcp", "(services", "tab)"]


def test_disabled_components_show_no_row_of_their_own(world):
    st = _state(mcp={"enabled": False, "mode": "systemd", "units": ["steward-mcp"]})
    assert not any("steward-mcp" in r or r.split()[1:2] == ["mcp"] for r in _plain(world.lines(st)))


def test_enabled_jev_component_has_one_row_not_two(world):
    st = _state(jev={"enabled": True})
    rows = _plain(world.lines(st))
    assert [r for r in rows if "Jev" in r or r.split()[1:2] == ["jev"]] == [rows[0]]


def test_connector_probe_only_when_configured(world):
    world.present.add("tg")
    before = "\n".join(_plain(world.lines(_state())))
    assert "off (no public connector)" in before and world.probed == []
    (world.config / "mcp.json").write_text(json.dumps({"public_base_url": "https://steward.example.org", "port": 9520}))
    rows = _plain(world.lines(_state()))
    assert any(r.split()[1:] == ["public", "connector", "200"] for r in rows), rows
    assert world.probed == ["https://steward.example.org/.well-known/oauth-protected-resource/mcp"]
    assert not any("off (no public connector)" in r for r in rows)
    red = world.lines(_state(), probe=lambda url: "000")
    assert any("\033[31m●" in r and "public connector" in r for r in red)


def test_no_install_json_keeps_the_legacy_trio(world):
    world.active.update({"steward-mcp": "active", "steward-tunnel": "active"})
    rows = world.lines(None)
    plain = _plain(rows)
    assert [r.split()[1] for r in plain[:3]] == ["steward-mcp", "steward-tunnel", "steward-mcp-tunnel-local"]
    assert "\033[32m●" in rows[0] and "\033[31m●" in rows[2]


def test_dash_units_env_overrides(world):
    world.env["STEWARD_DASH_UNITS"] = "one two"
    rows = _plain(world.lines(None))
    assert [r.split()[1] for r in rows[:2]] == ["one", "two"]
    assert "steward-mcp" not in "\n".join(rows)
    world.env["STEWARD_DASH_UNITS"] = ""
    assert "steward-tunnel" not in "\n".join(_plain(world.lines(None)))


def test_jev_key_shows_where_it_is_set(world):
    world.key = "set (keys.env)"
    rows = _plain(world.lines(_state()))
    assert any(r.split()[1:] == ["Jev", "observer", "set", "(keys.env)"] for r in rows)


def test_line_count_is_reported(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("STEWARD_")}
    env.update({"STEWARD_HOME": str(tmp_path / "h"), "STEWARD_CONFIG_DIR": str(tmp_path / "c"), "HOME": str(tmp_path),
                "PYTHONPATH": str(REPO), "STEWARD_DASH_UNITS": ""})
    block = subprocess.run([sys.executable, "-m", "steward.dash_services"], env=env, capture_output=True, text=True,
                           cwd=REPO).stdout.rstrip("\n").split("\n")
    count = subprocess.run([sys.executable, "-m", "steward.dash_services", "--count"], env=env, capture_output=True,
                           text=True, cwd=REPO).stdout.strip()
    assert int(count) == len(block) > 3


def test_click_map_offset_follows_the_service_line_count():
    text = (REPO / "steward" / "templates" / "cockpit" / "steward-dash.sh").read_text()
    assert "$((8 + a))" not in text
    assert text.count("$((3 + svc_rows + a))") == 4  # row, expand, btn, toggle
