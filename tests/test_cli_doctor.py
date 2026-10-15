"""Phase 19-03 (G10): `steward doctor` is read-only, never opens the store, and exits 1 only for a required gap."""

from __future__ import annotations

import json

import pytest

from steward import cli, prereq
from steward.runner_exec import CommandResult, FakeRunner


def _res(stdout="", code=0):
    return CommandResult(argv=[], cwd=".", exit_code=code, stdout=stdout, stderr="", duration_s=0.0)


ANSWERS = {
    "herdr --version": _res("herdr 0.9.1\n"),
    "git --version": _res("git version 2.43.0\n"),
    "claude auth status --json": _res(json.dumps({"loggedIn": True})),
}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    answers = dict(ANSWERS)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: FakeRunner(dict(answers)))
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/x.sock")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("herdr", "git", "claude"):
        (bindir / tool).write_text("#!/bin/sh\n")
        (bindir / tool).chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    return answers


def test_doctor_is_read_only(fake, tmp_path, monkeypatch, capsys):
    home, cfg = tmp_path / "no_home", tmp_path / "no_config"
    monkeypatch.setenv("STEWARD_HOME", str(home))
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(cfg))

    def boom(*a, **k):
        raise AssertionError("doctor must not open the store")

    monkeypatch.setattr(cli, "Store", boom)
    assert cli.main(["doctor"]) == 0
    assert not home.exists() and not cfg.exists()
    assert not list(tmp_path.glob("**/*.db"))
    assert "✓ claude CLI" in capsys.readouterr().out


def test_doctor_exit_codes_and_json(fake, capsys, monkeypatch):
    assert cli.main(["doctor"]) == 0  # optional rows are off; that does not matter
    capsys.readouterr()
    assert cli.main(["doctor", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {"id", "level", "status", "detail", "fix"} <= set(rows[0])
    assert any(r["level"] == "optional" and r["status"] == "off" for r in rows)

    fake["claude auth status --json"] = _res(json.dumps({"loggedIn": False}))
    assert cli.main(["doctor", "--json"]) == 1
    rows = {r["id"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["claude"]["status"] == "missing" and rows["claude"]["fix"] == "claude auth login"
    assert cli.main(["doctor"]) == 1
    assert "✗ claude CLI" in capsys.readouterr().out
