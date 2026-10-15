"""The Node shim `bin/steward.js` (G1, G2, G3, T-19-36, T-19-37). Fake interpreters and a fake `uv` on a tmp PATH; the
real interpreter is used only in the last two tests. Nothing is installed anywhere."""

from __future__ import annotations

import os
import pty
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from npm_helpers import (NODE, build_dist, fake_python, fake_uv, link_tool, pyproject_version, read_record, run_shim,
                         shim_env)

pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")


@pytest.fixture(scope="module")
def dist(tmp_path_factory):
    return build_dist(tmp_path_factory.mktemp("shimdist")).resolve()


def _env(tmp_path, dirs, **extra):
    return shim_env(tmp_path, dirs, FAKE_PY_RECORD=str(tmp_path / "rec.json"), **extra)


def test_picks_the_first_python_at_least_3_12(tmp_path, dist):
    bin_dir = tmp_path / "bin"
    fake_python(bin_dir, "python3", "3.11")
    fake_python(bin_dir, "python3.12", "3.12")
    cwd = tmp_path / "work"
    cwd.mkdir()
    res = run_shim(dist, ["jobs", "--all"], _env(tmp_path, [bin_dir]), cwd=cwd)
    assert res.returncode == 0, res.stderr
    rec = read_record(tmp_path / "rec.json")
    assert rec["name"] == "python3.12"
    assert rec["argv"] == ["-P", "-m", "steward.cli", "jobs", "--all"]
    assert os.path.realpath(rec["cwd"]) == os.path.realpath(cwd)


def test_python313_is_tried_before_312(tmp_path, dist):
    bin_dir = tmp_path / "bin"
    fake_python(bin_dir, "python3.12", "3.12")
    fake_python(bin_dir, "python3.13", "3.13")
    res = run_shim(dist, ["x"], _env(tmp_path, [bin_dir]))
    assert res.returncode == 0, res.stderr
    assert read_record(tmp_path / "rec.json")["name"] == "python3.13"


def test_steward_python_env_wins_when_good(tmp_path, dist):
    bin_dir = tmp_path / "bin"
    fake_python(bin_dir, "python3.12", "3.12")
    mine = fake_python(tmp_path / "mine", "my-python", "3.12")
    res = run_shim(dist, ["x"], _env(tmp_path, [bin_dir], STEWARD_PYTHON=str(mine)))
    assert res.returncode == 0, res.stderr
    rec = read_record(tmp_path / "rec.json")
    assert rec["name"] == "my-python"
    assert rec["env"]["STEWARD_PYTHON"] == str(mine)


def test_a_bad_steward_python_falls_through(tmp_path, dist):
    bin_dir = tmp_path / "bin"
    fake_python(bin_dir, "python3.12", "3.12")
    old = fake_python(tmp_path / "old", "my-python", "3.9")
    res = run_shim(dist, ["x"], _env(tmp_path, [bin_dir], STEWARD_PYTHON=str(old)))
    assert res.returncode == 0, res.stderr
    assert read_record(tmp_path / "rec.json")["name"] == "python3.12"


def test_only_old_python_and_no_uv_is_a_clear_error(tmp_path, dist):
    bin_dir = tmp_path / "bin"
    fake_python(bin_dir, "python3", "3.11")
    res = run_shim(dist, ["x"], _env(tmp_path, [bin_dir]))
    assert res.returncode == 1
    assert "Python 3.12" in res.stderr and "install Python 3.12 or uv" in res.stderr
    assert not (tmp_path / "rec.json").exists()


def test_uv_fallback_finds_a_python(tmp_path, dist):
    bin_dir = tmp_path / "bin"
    fake_python(bin_dir, "python3", "3.11")
    fake_uv(bin_dir)
    managed = fake_python(tmp_path / "managed", "cpython-3.12", "3.12")
    log = tmp_path / "uv.log"
    res = run_shim(dist, ["x"], _env(tmp_path, [bin_dir], FAKE_UV_LOG=str(log), FAKE_UV_PYTHON=str(managed)))
    assert res.returncode == 0, res.stderr
    assert read_record(tmp_path / "rec.json")["name"] == "cpython-3.12"
    assert log.read_text().splitlines() == ["python find 3.12"]


def _run_on_pty(env, dist, args, answer):
    """Run the shim with stdin and stderr on a real pseudo-terminal; send `answer` once the prompt is shown."""
    master, slave = pty.openpty()
    proc = subprocess.Popen([NODE, str(dist / "bin" / "steward.js"), *args], env=env, stdin=slave, stderr=slave,
                            stdout=subprocess.PIPE, text=True)
    os.close(slave)
    seen = b""
    sent = False
    deadline = time.time() + 30
    while time.time() < deadline and proc.poll() is None:
        ready, _, _ = select.select([master], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            seen += chunk
        if not sent and b"[y/N]" in seen:
            os.write(master, answer.encode())
            sent = True
    out, _ = proc.communicate(timeout=30)
    try:
        while select.select([master], [], [], 0.1)[0]:
            seen += os.read(master, 4096)
    except OSError:
        pass
    os.close(master)
    return proc.returncode, seen.decode(errors="replace"), out


def _uv_setup(tmp_path, with_sudo=False):
    bin_dir = tmp_path / "bin"
    fake_python(bin_dir, "python3", "3.11")
    fake_uv(bin_dir)
    link_tool(bin_dir, "stty")
    managed = fake_python(tmp_path / "managed", "cpython-3.12", "3.12")
    if with_sudo:
        (bin_dir / "sudo").write_text(f"#!/bin/sh\necho sudo >> {tmp_path / 'sudo.log'}\n")
        (bin_dir / "sudo").chmod(0o755)
    env = _env(tmp_path, [bin_dir], FAKE_UV_LOG=str(tmp_path / "uv.log"), FAKE_UV_PYTHON=str(managed),
               FAKE_UV_MARKER=str(tmp_path / "installed"))
    return env, managed


def test_uv_offer_y_installs_then_retries(tmp_path, dist):
    env, managed = _uv_setup(tmp_path, with_sudo=True)
    code, screen, _ = _run_on_pty(env, dist, ["x"], "y")
    assert code == 0, screen
    assert "[y/N]" in screen and "uv python install 3.12" in screen
    assert (tmp_path / "uv.log").read_text().splitlines() == ["python find 3.12", "python install 3.12",
                                                              "python find 3.12"]
    assert read_record(tmp_path / "rec.json")["name"] == "cpython-3.12"
    assert not (tmp_path / "sudo.log").exists()


def test_uv_offer_n_never_installs(tmp_path, dist):
    env, _ = _uv_setup(tmp_path)
    code, screen, _ = _run_on_pty(env, dist, ["x"], "n")
    assert code == 1, screen
    assert (tmp_path / "uv.log").read_text().splitlines() == ["python find 3.12"]
    assert "install Python 3.12 or uv" in screen
    assert not (tmp_path / "installed").exists()


def test_uv_offer_never_happens_off_a_tty(tmp_path, dist):
    env, _ = _uv_setup(tmp_path)
    res = run_shim(dist, ["x"], env, stdin="y\n")
    assert res.returncode == 1
    assert (tmp_path / "uv.log").read_text().splitlines() == ["python find 3.12"]
    assert "[y/N]" not in res.stderr
    assert not (tmp_path / "installed").exists()


def test_environment_contract(tmp_path, dist):
    bin_dir = tmp_path / "bin"
    py = fake_python(bin_dir, "python3.12", "3.12")
    env = _env(tmp_path, [bin_dir], PYTHONPATH="/somewhere/else")
    res = run_shim(dist, ["x"], env)
    assert res.returncode == 0, res.stderr
    seen = read_record(tmp_path / "rec.json")["env"]
    assert seen["STEWARD_INSTALL_ROOT"] == str(dist)
    assert seen["STEWARD_SHIM"] == str(dist / "bin" / "steward.js")
    assert os.path.realpath(seen["STEWARD_NODE"]) == os.path.realpath(NODE)
    assert seen["STEWARD_PYTHON"] == str(py)
    assert seen["PYTHONPATH"].split(os.pathsep) == [str(dist), "/somewhere/else"]
    assert seen["PYTHONPYCACHEPREFIX"].startswith(str(tmp_path / "cache"))
    # a caller's own bytecode prefix is left alone
    env2 = _env(tmp_path, [bin_dir], PYTHONPYCACHEPREFIX="/mine/pyc")
    assert run_shim(dist, ["x"], env2).returncode == 0
    seen2 = read_record(tmp_path / "rec.json")["env"]
    assert seen2["PYTHONPYCACHEPREFIX"] == "/mine/pyc"
    assert seen2["PYTHONPATH"] == str(dist)


def test_cache_prefix_falls_back_to_home_cache(tmp_path, dist):
    bin_dir = tmp_path / "bin"
    fake_python(bin_dir, "python3.12", "3.12")
    env = _env(tmp_path, [bin_dir])
    env.pop("XDG_CACHE_HOME")
    assert run_shim(dist, ["x"], env).returncode == 0
    prefix = read_record(tmp_path / "rec.json")["env"]["PYTHONPYCACHEPREFIX"]
    assert prefix.startswith(str(tmp_path / "home" / ".cache"))


def test_cwd_steward_dir_cannot_shadow(tmp_path, dist):
    bin_dir = tmp_path / "bin"
    fake_python(bin_dir, "python3.12", "3.12")
    decoy = tmp_path / "decoy"
    (decoy / "steward").mkdir(parents=True)
    (decoy / "steward" / "__init__.py").write_text("")
    (decoy / "steward" / "cli.py").write_text("print('decoy')\n")
    res = run_shim(dist, ["x"], _env(tmp_path, [bin_dir]), cwd=decoy)
    assert res.returncode == 0, res.stderr
    argv = read_record(tmp_path / "rec.json")["argv"]
    assert argv[:3] == ["-P", "-m", "steward.cli"]
    # and the real interpreter, run exactly as the shim runs it, finds the vendored package, not the decoy
    env = {**os.environ, "PYTHONPATH": str(dist), "PYTHONDONTWRITEBYTECODE": "1", "STEWARD_INSTALL_ROOT": str(dist),
           "STEWARD_HOME": str(tmp_path / "sh"), "HOME": str(tmp_path / "home")}
    real = subprocess.run([sys.executable, "-P", "-m", "steward.cli", "--version"], cwd=decoy, env=env,
                          capture_output=True, text=True)
    assert real.returncode == 0, real.stderr
    assert "decoy" not in real.stdout and real.stdout.startswith("steward ")
    where = subprocess.run([sys.executable, "-P", "-c", "import steward; print(steward.__file__)"], cwd=decoy, env=env,
                           capture_output=True, text=True)
    assert where.stdout.strip().startswith(str(dist))


def test_exit_status_passes_through(tmp_path, dist):
    bin_dir = tmp_path / "bin"
    fake_python(bin_dir, "python3.12", "3.12")
    res = run_shim(dist, ["x"], _env(tmp_path, [bin_dir], FAKE_PY_EXIT="3"))
    assert res.returncode == 3


def test_child_killed_by_a_signal_kills_the_shim_the_same_way(tmp_path, dist):
    bin_dir = tmp_path / "bin"
    fake_python(bin_dir, "python3.12", "3.12")
    res = run_shim(dist, ["x"], _env(tmp_path, [bin_dir], FAKE_PY_SIGNAL="SIGTERM"))
    assert res.returncode == -signal.SIGTERM


def test_conflicting_steward_on_path_is_announced(tmp_path, dist):
    bin_dir = tmp_path / "bin"
    fake_python(bin_dir, "python3.12", "3.12")
    other = tmp_path / "other"
    other.mkdir()
    (other / "steward").write_text("#!/bin/sh\nexit 0\n")
    (other / "steward").chmod(0o755)
    for dirs in ([other, bin_dir], [bin_dir, other]):
        res = run_shim(dist, ["x"], _env(tmp_path, dirs))
        lines = res.stderr.strip().splitlines()
        assert len(lines) == 1, res.stderr
        assert str(other / "steward") in lines[0] and str(dist) in lines[0]
    quiet = run_shim(dist, ["x"], _env(tmp_path, [other, bin_dir], STEWARD_SHIM_QUIET="1"))
    assert quiet.stderr == ""


def test_no_conflict_is_silent(tmp_path, dist):
    bin_dir = tmp_path / "bin"
    fake_python(bin_dir, "python3.12", "3.12")
    assert run_shim(dist, ["x"], _env(tmp_path, [bin_dir])).stderr == ""
    # the global-install symlink to this very shim is not a conflict
    linkdir = tmp_path / "prefix" / "bin"
    linkdir.mkdir(parents=True)
    (linkdir / "steward").symlink_to(dist / "bin" / "steward.js")
    res = run_shim(dist, ["x"], _env(tmp_path, [linkdir, bin_dir]))
    assert res.stderr == ""


def test_real_version_from_dist(tmp_path, dist):
    py = sys.executable
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3.12").symlink_to(py)
    env = shim_env(tmp_path, [bin_dir], PYTHONDONTWRITEBYTECODE="1")
    res = run_shim(dist, ["--version"], env)
    assert res.returncode == 0, res.stderr
    m = re.match(rf"steward {re.escape(pyproject_version())} \(install root: {re.escape(str(dist))}, python 3\.1[2-9]",
                 res.stdout)
    assert m, res.stdout
    assert "another steward on PATH" not in res.stdout
