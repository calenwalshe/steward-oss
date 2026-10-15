"""Phase 19-03 (G17): version sources, install root, and the `steward --version` line."""

from __future__ import annotations

import platform

import pytest

from steward import cli, version


def test_version_sources(tmp_path):
    stamp = tmp_path / "_version.py"
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "steward"\nversion = "0.3.0"\n')
    stamp.write_text('__version__ = "0.9.9"\n')
    assert version.version(stamp=stamp, pyproject=pyproject) == "0.9.9"
    stamp.unlink()
    assert version.version(stamp=stamp, pyproject=pyproject) == "0.3.0"
    pyproject.unlink()
    assert version.version(stamp=stamp, pyproject=pyproject) == "0.0.0+unknown"
    pyproject.write_text("not toml = = =")
    assert version.version(stamp=stamp, pyproject=pyproject) == "0.0.0+unknown"
    assert version.major("0.1.0") == 0
    assert version.major("12.4.1") == 12
    assert version.major("junk") == 0


def test_repo_version_is_pyproject(monkeypatch):
    monkeypatch.delenv("STEWARD_INSTALL_ROOT", raising=False)
    assert version.version() != "0.0.0+unknown"


def test_install_root_env_and_default(monkeypatch, tmp_path):
    monkeypatch.delenv("STEWARD_INSTALL_ROOT", raising=False)
    assert version.install_root() == version._package_dir().parent
    assert not version.is_package_install()
    monkeypatch.setenv("STEWARD_INSTALL_ROOT", str(tmp_path))
    assert version.install_root() == tmp_path
    assert version.is_package_install()
    assert version.install_root({"STEWARD_INSTALL_ROOT": "/elsewhere"}).as_posix() == "/elsewhere"
    assert version.is_package_install({}) is False


def test_describe_names_the_root_python_and_other_install(tmp_path):
    root = tmp_path / "root"
    (root / "bin").mkdir(parents=True)
    env = {"STEWARD_INSTALL_ROOT": str(root)}
    line = version.describe(which=lambda _n: None, environ=env, version_text="0.1.0")
    assert line == f"steward 0.1.0 (install root: {root}, python {platform.python_version()})"
    inside = version.describe(which=lambda _n: str(root / "bin" / "steward"), environ=env, version_text="0.1.0")
    assert "another steward" not in inside
    outside = version.describe(which=lambda _n: "/opt/other/steward", environ=env, version_text="0.1.0")
    assert outside.endswith("; another steward on PATH: /opt/other/steward")
    assert "0.1.0" in outside and str(root) in outside


def test_cli_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("steward ") and "install root:" in out and "python" in out
