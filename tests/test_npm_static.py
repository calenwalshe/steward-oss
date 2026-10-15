"""Static package guarantees (G3, G17, G19; T-19-34, T-19-35): private (never published to npm), MIT, no install scripts, no
dependencies, one version, an audited tarball. `npm` runs only `npm pack` and a local, offline install under tmp dirs.
No test runs any publish command, not even a dry run."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

from npm_helpers import NODE, NPM, REPO, build_dist, copy_repo, fake_python, load_module, pyproject_version, shim_env

pytestmark = pytest.mark.skipif(not NPM, reason="npm is not installed")

audit = load_module("steward_pack_audit")
VERBS = ("init", "intro", "doctor", "up", "cockpit", "dash", "enable", "disable", "components", "uninstall")


@pytest.fixture(autouse=True)
def quiet_npm(tmp_path, monkeypatch):
    """Keep npm's cache and logs out of the real home."""
    monkeypatch.setenv("npm_config_cache", str(tmp_path / "npmcache"))
    monkeypatch.setenv("npm_config_logs_dir", str(tmp_path / "npmlogs"))
    monkeypatch.setenv("npm_config_update_notifier", "false")
    monkeypatch.setenv("npm_config_audit", "false")
    monkeypatch.setenv("npm_config_fund", "false")


@pytest.fixture(scope="module")
def dist(tmp_path_factory):
    return build_dist(tmp_path_factory.mktemp("staticdist"))


def _pkg(dist):
    return json.loads((dist / "package.json").read_text())


def test_publish_is_impossible_by_accident(dist):
    pkg = _pkg(dist)
    assert pkg["private"] is True
    assert pkg["license"] == "MIT"
    text = (dist / "LICENSE").read_text()
    assert text.startswith("MIT License")
    assert not audit.scan_text("LICENSE", text)


def test_install_has_no_side_effects(dist):
    pkg = _pkg(dist)
    assert set(pkg.get("scripts", {})) == {"prepack"}
    for hook in ("preinstall", "install", "postinstall", "prepare", "preuninstall", "postuninstall", "prepublish",
                 "prepublishOnly", "postpack"):
        assert hook not in pkg.get("scripts", {})
    for key in ("dependencies", "devDependencies", "optionalDependencies", "bundledDependencies",
                "bundleDependencies", "peerDependencies"):
        assert key not in pkg
    assert pkg["bin"] == {"steward": "bin/steward.js"}
    assert set(pkg["files"]) <= {"bin/", "steward/", "herdr-plugin.toml", "README.md", "LICENSE"}
    assert pkg["engines"]["node"].startswith(">=")
    assert pkg["scripts"]["prepack"] == 'python3 "${STEWARD_PACK_AUDIT:-$npm_config_steward_pack_audit}" --prepack'
    assert "/" not in pkg["scripts"]["prepack"].replace("${STEWARD_PACK_AUDIT:-$npm_config_steward_pack_audit}", "")  # no path in the shipped file


def test_one_version(tmp_path, dist):
    def versions(root):
        stamp = (root / "steward" / "_version.py").read_text()
        return {
            "package.json": _pkg(root)["version"],
            "_version.py": re.search(r'^VERSION = "([^"]+)"', stamp, re.M).group(1),
            "herdr-plugin.toml": tomllib.loads((root / "herdr-plugin.toml").read_text())["version"],
        }

    ver = pyproject_version()
    assert set(versions(dist).values()) == {ver}
    repo = copy_repo(tmp_path)
    py = repo / "pyproject.toml"
    py.write_text(re.sub(r'(?m)^version = ".*"$', 'version = "9.8.7"', py.read_text(), count=1))
    (tmp_path / "bump").mkdir()
    bumped = build_dist(tmp_path / "bump", repo)
    assert set(versions(bumped).values()) == {"9.8.7"}


def _members(tgz: Path) -> list[str]:
    with tarfile.open(tgz) as tar:
        return sorted(m.name[len("package/"):] for m in tar.getmembers() if m.isfile())


def test_pack_produces_a_tarball_named_from_the_version(tmp_path):
    build = load_module("steward_pack_build")
    out = tmp_path / "dist" / "npm"
    assert build.main(["--out", str(out), "--pack"]) == 0
    tarballs = list((tmp_path / "dist").glob("*.tgz"))
    assert len(tarballs) == 1
    assert pyproject_version() in tarballs[0].name
    members = _members(tarballs[0])
    assert members == sorted(audit.read_manifest(audit.MANIFEST))
    assert not [m for m in members if ".git" in m.split("/") or "node_modules" in m.split("/")]

    # a bare `npm pack` without the audit's location fails closed and leaves no tarball
    env = {k: v for k, v in os.environ.items() if k != "STEWARD_PACK_AUDIT"}
    dest = tmp_path / "bare"
    dest.mkdir()
    res = subprocess.run(["npm", "pack", "--pack-destination", str(dest)], cwd=out, env=env, capture_output=True,
                         text=True)
    assert res.returncode != 0
    assert not list(dest.glob("*.tgz"))

    # with the variable, prepack runs the audit (its output is in npm's), and the tarball is the same set
    env["STEWARD_PACK_AUDIT"] = str(audit.HERE / "steward_pack_audit.py")
    dest2 = tmp_path / "with"
    dest2.mkdir()
    res = subprocess.run(["npm", "pack", "--pack-destination", str(dest2)], cwd=out, env=env, capture_output=True,
                         text=True)
    assert res.returncode == 0, res.stderr
    assert "pack audit ok" in res.stdout + res.stderr
    assert len(list(dest2.glob("*.tgz"))) == 1


def test_prepack_blocks_a_leak_added_after_the_audit(tmp_path):
    out = build_dist(tmp_path)
    (out / "steward" / "late.py").write_text("MAIL = 'someone" + "@" + "example.org'\n")
    env = {**os.environ, "STEWARD_PACK_AUDIT": str(audit.HERE / "steward_pack_audit.py")}
    dest = tmp_path / "blocked"
    dest.mkdir()
    res = subprocess.run(["npm", "pack", "--pack-destination", str(dest)], cwd=out, env=env, capture_output=True,
                         text=True)
    assert res.returncode != 0
    assert "steward/late.py" in res.stdout + res.stderr
    assert not list(dest.glob("*.tgz"))


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_install_from_the_local_tarball_has_no_side_effects(tmp_path):
    build = load_module("steward_pack_build")
    out = tmp_path / "dist" / "npm"
    assert build.main(["--out", str(out), "--pack"]) == 0
    tgz = next((tmp_path / "dist").glob("*.tgz"))
    prefix = tmp_path / "prefix"
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "XDG_CACHE_HOME": str(tmp_path / "xdgcache"),
           "XDG_CONFIG_HOME": str(tmp_path / "xdgconfig")}
    res = subprocess.run(["npm", "install", "-g", "--prefix", str(prefix), "--offline", "--no-audit", "--no-fund",
                          str(tgz)], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert list(home.iterdir()) == []  # installing wrote nothing into the user's home
    linked = prefix / "bin" / "steward"
    assert linked.is_symlink()
    root = prefix / "lib" / "node_modules" / "@steward-local" / "steward"
    assert (root / "steward" / "cli.py").is_file()

    py_dir = tmp_path / "py"
    py_dir.mkdir()
    (py_dir / "python3.12").symlink_to(sys.executable)
    run_env = shim_env(tmp_path, [prefix / "bin", Path(NODE).parent, py_dir], PYTHONDONTWRITEBYTECODE="1")
    res = subprocess.run([str(linked), "--version"], env=run_env, capture_output=True, text=True, cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith(f"steward {pyproject_version()} (install root: {root.resolve()}, python 3.")
    assert "another steward on PATH" not in res.stdout and res.stderr == ""


def test_readme_is_generic(dist):
    from steward import cli

    text = (dist / "README.md").read_text()
    assert audit.scan_text("README.md", text) == []
    assert "npm i -g ./steward-" in text
    assert "Run `steward` inside a Herdr pane" in text
    choices = set()
    for action in cli._build_parser()._actions:
        if getattr(action, "choices", None):
            choices |= set(action.choices)
    for verb in VERBS:
        assert verb in choices, verb
        assert f"`steward {verb}" in text, verb
    assert "steward --version" in text


def test_no_publish_path_in_packaging():
    bad = [
        "npm" + " publish", "yarn" + " publish", "pnpm" + " publish", "git" + " push", "twine" + " upload",
        "npm" + " login", "npm" + " adduser", "gh" + " release", "npm" + " dist-tag", "npm" + " deprecate",
    ]
    hits = []
    for base in (REPO / "packaging", REPO / "scripts"):
        for path in sorted(base.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            text = path.read_text(errors="replace")
            hits += [f"{path.relative_to(REPO)}: {phrase}" for phrase in bad if phrase in text]
    assert hits == []
