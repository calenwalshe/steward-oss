"""G20 tier 4: the clean-container smoke script (`scripts/container_smoke.sh`) and the steps it shares between its two
modes (`scripts/container/smoke_inside.sh`).

The default tests never need Docker: they read the script and run it with `--dry-run`, which prints the exact commands
and runs nothing. The two `integration` tests run it for real: clean-home mode (an `env -i` throwaway HOME plus a
throwaway named Herdr session) always where herdr, node and npm exist; docker mode only where `docker info` succeeds.
Nothing is published or pushed and the live home, config, state and Herdr sessions are never touched.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import pack_env
from herdr_session import herdr_available

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "container_smoke.sh"
INSIDE = REPO / "scripts" / "container" / "smoke_inside.sh"
DOCKERFILE = REPO / "scripts" / "container" / "Dockerfile.smoke"


def _run(args, tmp_path, *, path=None, extra_env=None, timeout=60):
    env = {"PATH": path or os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), "TMPDIR": str(tmp_path)}
    env.update(extra_env or {})
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=timeout,
                          cwd=str(REPO))


def _lines(res):
    return [ln for ln in res.stdout.splitlines() if ln.startswith("+")]


def _docker_run_line(res):
    hit = [ln for ln in _lines(res) if " docker run " in ln or ln.startswith("+ docker run")]
    assert len(hit) == 1, res.stdout
    return hit[0]


def test_dry_run_docker_commands(tmp_path):
    tarball = tmp_path / "pkg.tgz"
    tarball.write_text("not a real tarball: a dry run never opens it")
    res = _run(["--mode", "docker", "--dry-run", "--tarball", str(tarball)], tmp_path)
    assert res.returncode == 0, res.stderr
    build = [ln for ln in _lines(res) if " build " in ln and "docker" in ln]
    assert len(build) == 1 and "scripts/container/Dockerfile.smoke" in build[0]
    run = _docker_run_line(res)
    for needed in ("--network none", "--rm", "--read-only", "--cap-drop ALL", "no-new-privileges"):
        assert needed in run, (needed, run)
    words = shlex.split(run[1:])
    mounts = [words[i + 1] for i, w in enumerate(words) if w in ("-v", "--volume")]
    assert len(mounts) == 2 and all(m.endswith(":ro") for m in mounts), mounts  # the tarball and the script, read-only
    assert mounts[0].startswith(str(tarball) + ":")
    home = os.environ.get("HOME", "/nonexistent")
    for forbidden in ("--privileged", "docker.sock", ".ssh", ".config", "--net host", "--net=host", "--pid host",
                      "--pid=host", "--device", "--cap-add", "--userns host", "--ipc host"):
        assert forbidden not in run, (forbidden, run)
    assert not any(m.split(":")[0] in (home, home + "/") for m in mounts)
    assert "--network" in words and words[words.index("--network") + 1] == "none"


def test_dry_run_prints_every_command_and_runs_none(tmp_path):
    marker = tmp_path / "docker-was-called"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    docker.chmod(0o755)
    res = _run(["--mode", "docker", "--dry-run", "--tarball", str(tmp_path / "x.tgz")], tmp_path,
               path=f"{fake_bin}:{os.environ['PATH']}")
    assert res.returncode == 0, res.stderr
    assert not marker.exists()


def test_dry_run_clean_home_commands(tmp_path):
    before = set(Path("/tmp").glob("steward-smoke.*"))
    res = _run(["--mode", "clean-home", "--dry-run", "--tarball", str(tmp_path / "x.tgz")], tmp_path)
    assert res.returncode == 0, res.stderr
    assert "docker" not in res.stdout.replace("no docker", "")
    cmd = [ln for ln in _lines(res) if "env -i" in ln]
    assert len(cmd) == 1, res.stdout
    words = shlex.split(cmd[0][1:])
    assigned = dict(w.split("=", 1) for w in words if re.match(r"^[A-Z_]+=", w))
    # the throwaway roots sit under TMPDIR when it is short, else under /tmp (Herdr's socket path must stay short)
    bases = ("/tmp/", str(tmp_path) + "/")
    real_home = os.environ.get("HOME", "/nonexistent") + "/"
    assert assigned["HOME"].startswith(bases) and not assigned["HOME"].startswith(real_home)
    assert all(p.startswith(bases) for p in assigned["PATH"].split(":")), assigned["PATH"]
    assert assigned["SMOKE_SESSION"].startswith("steward-test-smoke-")
    assert assigned["SMOKE_TARBALL"] == str(tmp_path / "x.tgz")
    assert "smoke_inside.sh" in cmd[0]
    assert list(tmp_path.glob("steward-smoke.*")) == []  # a dry run creates nothing
    assert set(Path("/tmp").glob("steward-smoke.*")) == before


def test_auto_mode_picks_clean_home_when_docker_is_unusable(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text("#!/bin/sh\necho 'permission denied while trying to connect to the docker daemon' >&2\nexit 1\n")
    docker.chmod(0o755)
    res = _run(["--mode", "auto", "--dry-run", "--tarball", str(tmp_path / "x.tgz")], tmp_path,
               path=f"{fake_bin}:{os.environ['PATH']}")
    assert res.returncode == 0, res.stderr
    assert re.search(r"auto: .*clean-home", res.stdout), res.stdout
    assert "permission denied" in res.stdout  # says why
    assert any("env -i" in ln for ln in _lines(res)) and not any("docker run" in ln for ln in _lines(res))


def test_auto_mode_picks_docker_when_it_works(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n")
    docker.chmod(0o755)
    res = _run(["--mode", "auto", "--dry-run", "--tarball", str(tmp_path / "x.tgz")], tmp_path,
               path=f"{fake_bin}:{os.environ['PATH']}")
    assert res.returncode == 0, res.stderr
    assert re.search(r"auto: docker", res.stdout) and "docker run" in res.stdout


def test_bad_arguments_are_refused(tmp_path):
    for args in (["--mode", "vm"], ["--bogus"], ["--mode"]):
        res = _run(args, tmp_path)
        assert res.returncode == 2, (args, res.stdout, res.stderr)


def test_script_is_safe_shell():
    for path in (SCRIPT, INSIDE):
        text = path.read_text()
        assert subprocess.run(["bash", "-n", str(path)], capture_output=True).returncode == 0, path
        assert "set -euo pipefail" in text, path
        for forbidden in ("sudo", "git push", "npm publish", "curl", "wget", "--privileged", "docker.sock"):
            assert forbidden not in text, (path.name, forbidden)
    assert stat.S_IMODE(SCRIPT.stat().st_mode) & stat.S_IXUSR
    docker = DOCKERFILE.read_text()
    assert "FROM node:24" in docker and "FROM python:3.12" in docker and "COPY herdr /usr/local/bin/herdr" in docker
    assert "USER " in docker  # never root


def test_smoke_inside_asserts_the_contract():
    text = INSIDE.read_text()
    for needle in ("npm install -g", "--offline", "--ignore-scripts", "init --yes", "doctor", "uninstall",
                   "steward", "cockpit", "install.json", "SMOKE PASS", "SMOKE FAIL:", "HERDR_HEADLESS_UNAVAILABLE",
                   "exit 78", "steward-test-smoke-"):
        assert needle in text, needle
    assert re.search(r"re-run|second", text, re.I)  # idempotence is asserted
    assert "sha256sum" in text and "session stop" in text and "session delete" in text
    assert 'trap' in text


def test_gitignore_keeps_the_copied_herdr_binary_out_of_git():
    assert "scripts/container/herdr" in (REPO / ".gitignore").read_text().split()


def _real_mode_available():
    return herdr_available() and pack_env.skip_reason() is None


@pytest.mark.integration
@pytest.mark.skipif(not _real_mode_available(), reason="herdr, node, npm and Python 3.12 are required")
def test_clean_home_mode_real_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("smoke")
    tarball = pack_env.build_tarball(tmp)
    res = subprocess.run(["bash", str(SCRIPT), "--mode", "clean-home", "--tarball", str(tarball)], capture_output=True,
                         text=True, cwd=str(REPO), timeout=420,
                         env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "TMPDIR": str(tmp)})
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert out.rstrip().splitlines()[-1] == "SMOKE PASS", out
    sessions = subprocess.run(["herdr", "session", "list"], capture_output=True, text=True).stdout
    assert "steward-test-smoke-" not in sessions
    assert pack_env.leftover_processes("steward-test-smoke-") == []
    root = re.search(r"(/\S*?/steward-smoke\.\w{6})/home", out)
    assert root and not Path(root.group(1)).exists()  # the throwaway home is removed


@pytest.mark.integration
@pytest.mark.skipif(pack_env.skip_reason() is not None, reason=pack_env.skip_reason() or "")
def test_headless_herdr_unavailable_exits_78(tmp_path_factory):
    """A herdr that cannot serve makes the smoke exit 78 with HERDR_HEADLESS_UNAVAILABLE (not a pass, not a plain
    failure), and leaves nothing behind."""
    tmp = tmp_path_factory.mktemp("smoke78")
    tarball = pack_env.build_tarball(tmp)
    fake = tmp / "fakeherdr"
    fake.mkdir()
    herdr = fake / "herdr"
    herdr.write_text("#!/bin/sh\nexit 1\n")
    herdr.chmod(0o755)
    res = subprocess.run(["bash", str(SCRIPT), "--mode", "clean-home", "--tarball", str(tarball)], capture_output=True,
                         text=True, cwd=str(REPO), timeout=420,
                         env={"PATH": f"{fake}:{os.environ['PATH']}", "HOME": os.environ["HOME"], "TMPDIR": str(tmp)})
    out = res.stdout + res.stderr
    assert res.returncode == 78, out
    assert "HERDR_HEADLESS_UNAVAILABLE" in out and "SMOKE PASS" not in out


def _docker_usable() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0


@pytest.mark.integration
@pytest.mark.skipif(not _docker_usable(), reason="docker unusable: `docker info` does not succeed for this user")
def test_docker_mode_real_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("smoke-docker")
    tarball = pack_env.build_tarball(tmp)
    res = subprocess.run(["bash", str(SCRIPT), "--mode", "docker", "--tarball", str(tarball)], capture_output=True,
                         text=True, cwd=str(REPO), timeout=1200,
                         env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "TMPDIR": str(tmp)})
    out = res.stdout + res.stderr
    if res.returncode == 78 and "HERDR_HEADLESS_UNAVAILABLE" in out:
        pytest.skip("HERDR_HEADLESS_UNAVAILABLE: herdr did not run headless in the container; use clean-home mode")
    assert res.returncode == 0, out
    assert out.rstrip().splitlines()[-1] == "SMOKE PASS", out
