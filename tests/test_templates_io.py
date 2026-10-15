"""Phase 19-03 (G6): never-overwrite config templates."""

from __future__ import annotations

import os
import stat
import tomllib

import pytest

from steward import closeout, installstate, owner, supervisors, templates_io as tio, trajectory, alerts


@pytest.fixture
def state():
    return installstate.new_state("0.1.0", "/r", "3.12.4")


def _mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def test_templates_parse_and_are_understood_by_the_loaders(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("STEWARD_OWNER_NAME", raising=False)
    monkeypatch.delenv("STEWARD_ACCEPTANCE", raising=False)
    tio.write_templates(cfg, installstate.new_state("0.1.0", "/r"), "Priya", "0.1.0")
    data = tomllib.loads((cfg / "steward.toml").read_text())
    assert data["schema"] == 1 and data["acceptance"] == "auto"
    owner.reset_cache()
    assert closeout.acceptance_mode() == "auto"
    assert supervisors.global_jev_mode() == "off"  # Jev is optional: off until `steward enable jev`
    assert alerts.telegram_enabled() is False
    assert owner.name() == "Priya"
    tc = trajectory.load_trajectory_config(cfg / "trajectory.toml")
    assert tc.mode == "shadow" and tc == trajectory.TrajectoryConfig()


def test_first_write_creates_and_records(tmp_path, state):
    cfg = tmp_path / "a" / "b" / "cfg"
    out = tio.write_templates(cfg, state, "Priya", "0.1.0")
    assert [(o.name, o.action) for o in out] == [("steward.toml", "created"), ("trajectory.toml", "created"),
                                                 ("roster.toml", "created")]
    text = (cfg / "steward.toml").read_text()
    assert 'name = "Priya"' in text and "Steward 0.1.0" in text and "{" not in text
    assert "Steward 0.1.0" in (cfg / "trajectory.toml").read_text()
    assert set(state["template_hashes"]) == {"steward.toml", "trajectory.toml", "roster.toml"}
    assert {f["path"] for f in state["files"]} == {str(cfg / n) for n in ("steward.toml", "trajectory.toml", "roster.toml")}
    for d in (cfg, cfg.parent, cfg.parent.parent):
        assert _mode(d) == 0o700
    assert _mode(cfg / "steward.toml") == 0o600


def test_rerun_changes_nothing(tmp_path, state):
    cfg = tmp_path / "cfg"
    tio.write_templates(cfg, state, "Priya", "0.1.0")
    before = {p.name: p.stat().st_mtime_ns for p in cfg.iterdir()}
    snapshot = repr(state)
    out = tio.write_templates(cfg, state, "Priya", "0.1.0")
    assert [o.action for o in out] == ["unchanged", "unchanged", "unchanged"]
    assert {p.name: p.stat().st_mtime_ns for p in cfg.iterdir()} == before
    assert repr(state) == snapshot


def test_user_edit_is_never_overwritten(tmp_path, state):
    cfg = tmp_path / "cfg"
    tio.write_templates(cfg, state, "Priya", "0.1.0")
    mine = (cfg / "steward.toml").read_text().replace('acceptance = "auto"', 'acceptance = "human"')
    (cfg / "steward.toml").write_text(mine)
    for _ in range(2):
        out = tio.write_templates(cfg, state, "Priya", "0.1.0")
        assert {o.name: o.action for o in out}["steward.toml"] == "skipped_user_edit"
        assert (cfg / "steward.toml").read_text() == mine
        assert not (cfg / "steward.toml.new").exists()


def test_an_unrecorded_existing_file_is_left_alone_and_recorded(tmp_path, state):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "steward.toml").write_text('acceptance = "auto"\n')
    out = tio.write_templates(cfg, state, "Priya", "0.1.0")
    assert {o.name: o.action for o in out}["steward.toml"] == "skipped_user_edit"
    assert (cfg / "steward.toml").read_text() == 'acceptance = "auto"\n'
    assert not (cfg / "steward.toml.new").exists()
    assert state["template_hashes"]["steward.toml"] == tio.template_hash("steward.toml")
    assert str(cfg / "steward.toml") not in {f["path"] for f in state["files"]}  # not ours to uninstall


def test_changed_template_is_written_beside(tmp_path, state):
    cfg = tmp_path / "cfg"
    tio.write_templates(cfg, state, "Priya", "0.1.0")
    mine = (cfg / "steward.toml").read_text() + "\n# my note\n"
    (cfg / "steward.toml").write_text(mine)
    (cfg / "steward.toml.new").write_text("an older .new")
    state["template_hashes"]["steward.toml"] = "0" * 64  # the template differs from what was recorded
    out = tio.write_templates(cfg, state, "Priya", "0.2.0")
    acts = {o.name: o.action for o in out}
    assert acts["steward.toml"] == "new_written"
    assert acts["trajectory.toml"] in ("unchanged", "skipped_user_edit")  # its template did not change: silent
    assert not (cfg / "trajectory.toml.new").exists()
    assert (cfg / "steward.toml").read_text() == mine
    beside = (cfg / "steward.toml.new").read_text()
    assert "Steward 0.2.0" in beside and "older" not in beside
    assert state["template_hashes"]["steward.toml"] == tio.template_hash("steward.toml")
    assert {"path": str(cfg / "steward.toml.new"), "kind": "config-new"} in state["files"]
    (cfg / "steward.toml.new").unlink()
    out = tio.write_templates(cfg, state, "Priya", "0.2.0")
    assert {o.name: o.action for o in out}["steward.toml"] == "skipped_user_edit"
    assert not (cfg / "steward.toml.new").exists()  # nothing more is written for the same template


def test_unchanged_file_under_changed_template_is_just_rerecorded(tmp_path, state):
    cfg = tmp_path / "cfg"
    tio.write_templates(cfg, state, "Priya", "0.1.0")
    state["template_hashes"]["steward.toml"] = "0" * 64
    out = tio.write_templates(cfg, state, "Priya", "0.1.0")  # same text as the file: nothing to offer
    assert {o.name: o.action for o in out}["steward.toml"] == "unchanged"
    assert not (cfg / "steward.toml.new").exists()


def test_owner_default_comes_from_the_owner_module(tmp_path, monkeypatch, state):
    monkeypatch.delenv("STEWARD_OWNER_NAME", raising=False)
    monkeypatch.setattr(owner, "_git_user_name", lambda: "Priya Nair")
    owner.reset_cache()
    tio.write_templates(tmp_path / "one", state)
    assert 'name = "Priya"' in (tmp_path / "one" / "steward.toml").read_text()
    monkeypatch.setattr(owner, "_git_user_name", lambda: "")
    owner.reset_cache()
    tio.write_templates(tmp_path / "two", installstate.new_state("0.1.0", "/r"))
    assert 'name = "the operator"' in (tmp_path / "two" / "steward.toml").read_text()


def test_owner_name_is_escaped_into_the_toml_string(tmp_path, state):
    tio.write_templates(tmp_path / "cfg", state, 'Sam "the man" \\ Lee', "0.1.0")
    data = tomllib.loads((tmp_path / "cfg" / "steward.toml").read_text())
    assert data["owner"]["name"] == 'Sam "the man" \\ Lee'


def test_existing_dirs_keep_their_mode(tmp_path):
    d = tmp_path / "exists"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    assert tio.ensure_dirs(d, d / "sub") == [d / "sub"]
    assert _mode(d) == 0o755 and _mode(d / "sub") == 0o700


def test_pyproject_ships_the_templates():
    import tomllib as t
    from pathlib import Path

    data = t.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())
    assert "templates/*" in data["tool"]["setuptools"]["package-data"]["steward"]
    assert (tio.TEMPLATE_DIR / "steward.toml").is_file() and (tio.TEMPLATE_DIR / "trajectory.toml").is_file()
