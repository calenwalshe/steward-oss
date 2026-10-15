"""Phase 19-03 (G5/G14/G16/G17): install.json, the one manifest."""

from __future__ import annotations

import os
import stat

import pytest

from steward import installstate as ist


def test_load_missing_is_none_and_save_roundtrips(tmp_path, monkeypatch):
    home = tmp_path / "fresh_home"
    assert ist.load(home) is None
    state = ist.new_state("0.1.0", "/root", "3.12.4", now="2026-09-30T00:00:00+00:00")
    p = ist.save(state, home, now="2026-09-30T01:00:00+00:00")
    assert p == home / "install.json"
    assert ist.load(home) == state and state["updated_at"] == "2026-09-30T01:00:00+00:00"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert [f.name for f in home.iterdir()] == ["install.json"]


def test_default_home_is_steward_home(steward_home):
    ist.save(ist.new_state("0.1.0", "/r"))
    assert (steward_home / "install.json").is_file() and ist.load() is not None


def test_a_crash_between_temp_and_replace_keeps_the_old_file(tmp_path, monkeypatch):
    home = tmp_path / "h"
    ist.save(ist.new_state("0.1.0", "/r"), home)
    before = (home / "install.json").read_text()

    def boom(src, dst):
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        ist.save(ist.new_state("9.9.9", "/r"), home)
    monkeypatch.undo()
    assert (home / "install.json").read_text() == before
    assert [f.name for f in home.iterdir()] == ["install.json"]  # no stray temp file


def test_new_state_fields():
    s = ist.new_state("0.1.0", "/opt/steward", "3.12.4", now="2026-09-30T00:00:00+00:00")
    assert s == {
        "schema": 1, "version": "0.1.0", "install_root": "/opt/steward", "python": "3.12.4",
        "installed_at": "2026-09-30T00:00:00+00:00", "updated_at": "2026-09-30T00:00:00+00:00",
        "intro_seen": None, "config_schema": 1, "template_hashes": {}, "files": [], "tabs": [],
        "components": {}, "plugin_links": [],
    }


def test_record_file_dedupes_by_path():
    s = ist.new_state("0.1.0", "/r")
    ist.record_file(s, "/a/steward.toml", "config")
    ist.record_file(s, "/a/other", "config")
    ist.record_file(s, "/a/steward.toml", "config-new")
    assert s["files"] == [{"path": "/a/other", "kind": "config"}, {"path": "/a/steward.toml", "kind": "config-new"}]


def test_record_component_merges():
    s = ist.new_state("0.1.0", "/r")
    ist.record_component(s, "mcp", unit="steward-mcp.service")
    ist.record_component(s, "mcp", port=8765)
    ist.record_component(s, "phone", tab="phone")
    assert s["components"] == {"mcp": {"unit": "steward-mcp.service", "port": 8765}, "phone": {"tab": "phone"}}


def test_needs_intro():
    assert ist.needs_intro(None, "0.1.0") == (True, "first")
    s = ist.new_state("0.1.0", "/r")
    assert ist.needs_intro(s, "0.1.0") == (True, "first")
    ist.mark_intro_seen(s, "0.1.0")
    assert s["intro_seen"] == "0.1.0"
    assert ist.needs_intro(s, "0.1.4") == (False, None)
    assert ist.needs_intro(s, "1.0.0") == (True, "major")


def test_corrupt_state_is_an_error_not_a_reset(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    (home / "install.json").write_text("{not json")
    with pytest.raises(ist.InstallStateError):
        ist.load(home)
    assert (home / "install.json").read_text() == "{not json"
    (home / "install.json").write_text("[1, 2]")
    with pytest.raises(ist.InstallStateError):
        ist.load(home)
