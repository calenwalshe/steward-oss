"""Phase 19-06 (G14): the hidden `steward service run <component>` and the pure argv it execs."""

from __future__ import annotations

import os
import sys

import pytest

from steward import cli, components, version


def test_service_argv_mcp(tmp_path):
    state = {"components": {"mcp": {"enabled": True, "port": 8765}}}
    assert components.service_argv("mcp", state, tmp_path) == [
        sys.executable, "-m", "steward.cli", "mcp", "serve", "--port", "8765"]
    (tmp_path / "mcp.json").write_text("{}")
    assert components.service_argv("mcp", state, tmp_path) == [
        sys.executable, "-m", "steward.cli", "mcp", "serve", "--config", str(tmp_path / "mcp.json")]


def test_service_argv_is_pure(tmp_path):
    before = sorted(os.listdir(tmp_path))
    components.service_argv("mcp", None, tmp_path, python="/x/python")
    assert sorted(os.listdir(tmp_path)) == before
    assert components.service_argv("mcp", None, tmp_path, python="/x/python")[0] == "/x/python"


def test_service_run_loads_keys_then_execs(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("STEWARD_LEGACY_KEYS_FILE", str(tmp_path / "none"))
    execs = []
    monkeypatch.setattr(os, "execvp", lambda file, argv: execs.append((file, list(argv))))
    assert cli.main(["service", "run", "mcp"]) == 0
    assert execs == [(sys.executable, [sys.executable, "-m", "steward.cli", "mcp", "serve", "--port", "8765"])]


def test_service_is_hidden_from_help(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "service" not in out.replace("services", "")
    assert "enable" in out and "components" in out


def test_service_run_unknown_target(capsys):
    assert cli.main(["service", "run", "nope"]) == 2
    assert "unknown service" in capsys.readouterr().err


def test_version_npm_package(tmp_path):
    stamp = tmp_path / "_version.py"
    assert version.npm_package(stamp) == "the package you installed"
    stamp.write_text('__version__ = "0.1.0"\nNPM_PACKAGE = "@someone/steward"\n')
    assert version.npm_package(stamp) == "@someone/steward"
