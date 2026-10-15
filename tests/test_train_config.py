# tests/test_train_config.py
from pathlib import Path

from steward import config


def test_defaults_are_off():
    s = config.train_settings(env={})
    assert s["enabled"] is False
    assert s["target"] == "fleet-registry"
    assert s["every_s"] == 180.0
    assert s["max_batch"] == 8
    assert s["resolver_timeout_s"] == 1200.0


def test_toml_section(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path))
    (tmp_path / "steward.toml").write_text('[train]\nenabled = true\nrepo = "/r"\nmax_batch = 4\n')
    s = config.train_settings(env={})
    assert s["enabled"] is True and s["repo"] == "/r" and s["max_batch"] == 4


def test_env_overrides_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path))
    (tmp_path / "steward.toml").write_text('[train]\nenabled = true\nrepo = "/r"\n')
    assert config.train_settings(env={"STEWARD_TRAIN": "0"})["enabled"] is False
    assert config.train_settings(env={"STEWARD_TRAIN_REPO": "/x"})["repo"] == "/x"


def test_train_owns_matches_resolved_path(tmp_path):
    env = {"STEWARD_TRAIN": "1", "STEWARD_TRAIN_REPO": str(tmp_path)}
    assert config.train_owns(str(tmp_path) + "/", env=env) is True
    assert config.train_owns(str(tmp_path / "other"), env=env) is False
    assert config.train_owns(str(tmp_path), env={"STEWARD_TRAIN": "0", "STEWARD_TRAIN_REPO": str(tmp_path)}) is False
