"""Tier 2 of G20: a real, audited tarball installed OFFLINE into an empty machine, then the INSTALLED `steward` run.

Real processes (npm, node, python) under a tmp directory only: the environment is built from scratch by
`tests/pack_env.py`, never inherited, and the install is `--offline` from a local tarball with no dependencies.
Nothing is published, nothing contacts a registry, and the live `~/.config/steward`, `~/.local/state/steward` and
`~/bin` are never read or written. Skipped with a reason when node, npm or Python 3.12 is missing.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import pytest

import pack_env

_SKIP = pack_env.skip_reason()
pytestmark = pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")

VERSION_A = pack_env.pyproject_version()
VERSION_B = pack_env.bump(VERSION_A)


@pytest.fixture(scope="module")
def tarballs(tmp_path_factory):
    """The tarball for the current version (A) and one for a bumped version (B), each built once with the audit."""
    tmp = tmp_path_factory.mktemp("pack")
    return {"A": pack_env.build_tarball(tmp), "B": pack_env.build_tarball(tmp, version=VERSION_B)}


@pytest.fixture
def box(tmp_path, tarballs):
    """A fresh empty machine with tarball A installed into its tmp prefix."""
    env = pack_env.machine(tmp_path / "machine")
    res = pack_env.install_tarball(env, tarballs["A"])
    assert res.returncode == 0, f"install failed:\n{res.stdout}\n{res.stderr}"
    return env


def _rows(res) -> dict:
    return {row["id"]: row for row in json.loads(res.stdout)}


def _assert_nothing_written(env: dict) -> None:
    for name, path in pack_env.state_dirs(env).items():
        assert pack_env.tree(path) == [], f"{name} is not empty: {pack_env.tree(path)}"


def test_installed_version_names_root_and_python(box):
    res = pack_env.run_installed(box, "--version")
    assert res.returncode == 0, res.stderr
    root = pack_env.install_root(box)
    m = re.fullmatch(rf"steward {re.escape(VERSION_A)} \(install root: {re.escape(str(root))}, python 3\.(1[2-9])\.\d+\)\n",
                     res.stdout)
    assert m, res.stdout
    assert str(root).startswith(str(pack_env.prefix_of(box)))
    assert (root / "steward" / "cli.py").is_file()


def test_install_had_no_side_effects(box, tarballs):
    _assert_nothing_written(box)  # no postinstall wrote anything
    ls = pack_env.subprocess.run(
        [pack_env.NPM, "ls", "-g", "--prefix", str(pack_env.prefix_of(box)), "--json", "--depth=1"], env=box,
        capture_output=True, text=True, timeout=60)
    listing = json.loads(ls.stdout)
    assert list(listing["dependencies"]) == [pack_env.package_name()]
    assert not listing["dependencies"][pack_env.package_name()].get("dependencies")
    manifest = json.loads((pack_env.install_root(box) / "package.json").read_text())
    assert not manifest.get("dependencies") and manifest["version"] == VERSION_A
    assert not {"preinstall", "install", "postinstall"} & set(manifest.get("scripts", {}))


def test_doctor_with_fake_claude_and_codex(box):
    res = pack_env.run_installed(box, "doctor", "--json")
    assert res.returncode == 1, res.stderr  # not inside a Herdr pane
    rows = _rows(res)
    for ok in ("claude", "codex", "git", "python", "herdr_version"):
        assert rows[ok]["status"] == "ok", rows[ok]
    missing = [r["id"] for r in rows.values() if r["level"] == "required" and r["status"] != "ok"]
    assert missing == ["herdr_pane"]
    assert rows["jev"]["detail"] == "off (no key)"
    assert rows["systemd"]["detail"] == "off (no systemd user manager)"
    assert rows["connector"]["detail"] == "off (no public connector)"
    _assert_nothing_written(box)  # doctor is read-only


def test_init_yes_outside_herdr_refuses_and_writes_nothing(box):
    res = pack_env.run_installed(box, "init", "--yes")
    assert res.returncode == 1, (res.stdout, res.stderr)
    assert "Herdr pane" in res.stdout  # the checklist
    _assert_nothing_written(box)


def test_init_dry_run_prints_the_plan(box):
    # Outside Herdr the plan IS the refusal (19-05: a missing required prerequisite refuses before any plan is shown);
    # the plan with its dirs, templates and tabs is asserted inside a pane by the live test (tier 3).
    res = pack_env.run_installed(box, "init", "--dry-run")
    assert res.returncode == 1, (res.stdout, res.stderr)  # the Herdr requirement is missing
    assert "will not set up this workspace" in res.stdout and "Herdr pane" in res.stdout
    assert "✓ claude CLI" in res.stdout and "✓ codex" in res.stdout
    _assert_nothing_written(box)


def test_uninstall_without_a_record_is_a_noop(box):
    res = pack_env.run_installed(box, "uninstall")
    assert res.returncode == 1 and "nothing to uninstall" in res.stdout
    _assert_nothing_written(box)


def test_path_conflict_is_announced(box, tmp_path):
    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    decoy = decoy_dir / "steward"
    decoy.write_text("#!/bin/sh\nexit 0\n")
    decoy.chmod(decoy.stat().st_mode | stat.S_IXUSR)
    env = dict(box, PATH=box["PATH"] + os.pathsep + str(decoy_dir))
    res = pack_env.run_installed(env, "--version")
    lines = [ln for ln in res.stderr.splitlines() if "another steward on PATH" in ln]
    assert len(lines) == 1 and str(decoy) in lines[0], res.stderr
    quiet = pack_env.run_installed(dict(env, STEWARD_SHIM_QUIET="1"), "--version")
    assert quiet.stderr == ""


def test_cwd_decoy_cannot_shadow_the_vendored_package(box, tmp_path):
    decoy = tmp_path / "elsewhere"
    (decoy / "steward").mkdir(parents=True)
    (decoy / "steward" / "__init__.py").write_text("raise SystemExit('the decoy package was imported')\n")
    (decoy / "steward" / "cli.py").write_text("raise SystemExit('the decoy cli was imported')\n")
    res = pack_env.run_installed(box, "--version", cwd=decoy)
    assert res.returncode == 0, res.stderr
    assert f"install root: {pack_env.install_root(box)}" in res.stdout


def test_upgrade_by_reinstall(box, tarballs):
    assert VERSION_A in pack_env.run_installed(box, "--version").stdout
    res = pack_env.install_tarball(box, tarballs["B"])
    assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
    out = pack_env.run_installed(box, "--version").stdout
    assert out.startswith(f"steward {VERSION_B} "), out
    _assert_nothing_written(box)


def test_missing_python_message(tmp_path, tarballs):
    env = pack_env.machine(tmp_path / "machine", with_python=False)
    assert pack_env.install_tarball(env, tarballs["A"]).returncode == 0
    res = pack_env.run_installed(env, "--version")
    assert res.returncode == 1
    assert "install Python 3.12 or uv" in res.stderr
    _assert_nothing_written(env)


def test_build_version_override_must_be_semver(tmp_path):
    build = pack_env.load_module("steward_pack_build")
    out = build.build(tmp_path / "dist", version="9.8.7")
    assert json.loads((out / "package.json").read_text())["version"] == "9.8.7"
    assert 'VERSION = "9.8.7"' in (out / "steward" / "_version.py").read_text()
    for bad in ("", "1.2", "latest", "1.2.3; rm -rf x", "../1.2.3"):
        if bad:  # an empty string means "no override": pyproject's version
            with pytest.raises(ValueError):
                build.build(tmp_path / "bad", version=bad)
    assert build.main(["--out", str(tmp_path / "bad2"), "--version", "nonsense"]) == 2
    assert not (tmp_path / "bad2").exists()
