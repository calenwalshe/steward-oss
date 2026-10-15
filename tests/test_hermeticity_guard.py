import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent


def _run_inner(tmp_path, body):
    """Run a throwaway test under a copy of this conftest, with the guard aimed at a throwaway repo (hermetic and
    xdist-safe: the real checkout's index is never touched)."""
    main = tmp_path / "main"
    main.mkdir()
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "STEWARD_GUARD_MAIN": str(main),
           "PYTHONPATH": os.pathsep.join([str(TESTS), str(TESTS.parent)])}
    for k in ("PYTEST_XDIST_WORKER", "PYTEST_CURRENT_TEST"):
        env.pop(k, None)
    git = ["git", "-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True, env=env)
    (main / "f").write_text("x")
    subprocess.run([*git, "add", "f"], check=True, env=env)
    subprocess.run([*git, "commit", "-qm", "i"], check=True, env=env)
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "conftest.py").write_text((TESTS / "conftest.py").read_text())
    t = inner / "test_bad.py"
    t.write_text(textwrap.dedent(body.format(main=str(main))))
    return subprocess.run(["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", str(t)],
                          cwd=inner, capture_output=True, text=True, env=env)


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
def test_guard_fails_a_test_that_rewrites_the_index(tmp_path):
    r = _run_inner(tmp_path, """
        import subprocess
        def test_touch():
            open({main!r} + "/g", "w").write("y")
            subprocess.run(["git", "-C", {main!r}, "add", "g"], check=True)
    """)
    assert r.returncode != 0 and "main checkout .git touched by" in r.stdout and "index rewritten" in r.stdout


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
def test_guard_fails_a_test_that_leaves_an_index_lock(tmp_path):
    r = _run_inner(tmp_path, """
        def test_lock():
            open({main!r} + "/.git/index.lock", "w").close()
    """)
    assert r.returncode != 0 and "index.lock left behind" in r.stdout


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
def test_guard_passes_a_clean_test(tmp_path):
    r = _run_inner(tmp_path, """
        def test_ok():
            assert True
    """)
    assert r.returncode == 0, r.stdout


def test_live_root_is_not_the_main_checkout():
    from steward import stability
    assert "work-20260926" not in str(stability.LIVE_ROOT)


def test_optional_locks_off():
    assert os.environ.get("GIT_OPTIONAL_LOCKS") == "0"
