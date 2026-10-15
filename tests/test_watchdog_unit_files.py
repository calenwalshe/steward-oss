"""The shipped (not installed) watchdog unit, wrapper and installer (issue W, spec §10)."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNIT = ROOT / "deploy" / "steward-watchdog.service"
WRAPPER = ROOT / "deploy" / "run-steward-watchdog.sh"
INSTALLER = ROOT / "deploy" / "install-steward-watchdog.sh"


def _directives(text):
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def test_unit_restarts_always_and_never_reads_api_keys():
    d = _directives(UNIT.read_text())
    assert "Restart=always" in d and "Type=simple" in d
    assert not any(x.startswith("EnvironmentFile") for x in d)
    (exec_start,) = [x for x in d if x.startswith("ExecStart=")]
    assert exec_start.endswith("/deploy/run-steward-watchdog.sh")
    assert "WantedBy=default.target" in d


def test_wrapper_sources_no_secrets_and_is_explicit():
    body = "\n".join(_directives(WRAPPER.read_text()))
    assert ".api-keys" not in body and "source" not in body and ". /" not in body
    assert "steward.cli watchdog" in body and "STEWARD_HOME" in body and "HERDR_SOCKET_PATH" in body
    assert os.access(WRAPPER, os.X_OK) and os.access(INSTALLER, os.X_OK)


def _stubs(tmp_path):
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for name, out in (("systemctl", ""), ("loginctl", "Linger=no")):
        p = bin_dir / name
        p.write_text(f"#!/bin/sh\necho \"{name} $*\" >> {log}\necho '{out}'\n")
        p.chmod(0o755)
    return bin_dir, log


def test_installer_is_a_dry_run_by_default(tmp_path):
    bin_dir, log = _stubs(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", HOME=str(home), USER="tester",
               STEWARD_HOME=str(tmp_path / "steward"), HERDR_SOCKET_PATH=str(tmp_path / "no.sock"))
    p = subprocess.run([str(INSTALLER)], env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    assert "dry run: nothing changed" in p.stdout and "loginctl enable-linger tester" in p.stdout
    assert "FAIL herdr socket" in p.stdout
    calls = log.read_text() if log.exists() else ""
    assert "systemctl" not in calls  # no daemon-reload, no enable
    assert not (home / ".config" / "systemd").exists()
