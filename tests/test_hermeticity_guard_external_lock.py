"""u7 (stall fixes 2026-10-14): the main-checkout guard tells a lock the test left from a live process's lock.

Evidence: `python -m pytest -n 8 -q` in the main checkout failed 5-16 random tests with "main checkout .git touched by
<test>: index.lock left behind"; each passed alone. A read-only watcher on the main `.git` saw `index.lock` appear
13 times in 35 s with NO suite running, and caught the creator: `git -C <main> status --porcelain` run every few
seconds by the Claude Code status line of the sessions whose cwd is the main checkout (no GIT_OPTIONAL_LOCKS=0; and
`timeout=2` SIGKILLs git under load, leaving the holderless stale locks the liveness sweep keeps quarantining).
The guard sampled `index.lock` at teardown, so any test ending inside one of those windows was blamed.

Same throwaway-repo harness as tests/test_hermeticity_guard.py: the real checkout is never touched."""
import shutil

import pytest

from test_hermeticity_guard import _run_inner

# A live process outside the test run, started as the box's live processes run: umask 002, none of the run's marks.
_EXTERNAL = '''
import os, subprocess, sys, time
def _external(code):
    env = {{k: v for k, v in os.environ.items()
           if k not in ("STEWARD_TEST_RUN_MARK", "PYTEST_XDIST_TESTRUNUID")}}
    p = subprocess.Popen([sys.executable, "-c", code], env=env, preexec_fn=lambda: os.umask(0o002))
    lock = {main!r} + "/.git/index.lock"
    for _ in range(200):
        if os.path.exists(lock):
            return p
        time.sleep(0.01)
    raise AssertionError("external process never took the lock")
'''


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
def test_a_transient_external_lock_at_teardown_does_not_fail_the_test(tmp_path):
    """A git reader elsewhere holds the lock for a moment while the test finishes (the status-line case)."""
    r = _run_inner(tmp_path, _EXTERNAL + '''
def test_innocent():
    _external("import os, time; l = {main!r} + '/.git/index.lock'; open(l, 'w').close(); time.sleep(0.6); os.remove(l)")
''')
    assert r.returncode == 0, r.stdout


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
def test_a_lock_held_by_a_live_external_process_does_not_fail_the_test(tmp_path):
    r = _run_inner(tmp_path, _EXTERNAL + '''
def test_innocent():
    _external("import os, time; l = {main!r} + '/.git/index.lock'; f = open(l, 'w'); time.sleep(6); f.close(); os.remove(l)")
''')
    assert r.returncode == 0, r.stdout


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
def test_a_lock_held_by_the_runs_own_leaked_process_still_fails(tmp_path):
    r = _run_inner(tmp_path, '''
import subprocess, sys, time, os
def test_leaks_a_holder():
    l = {main!r} + "/.git/index.lock"
    subprocess.Popen([sys.executable, "-c", f"import time; f = open({{l!r}}, 'w'); time.sleep(8)"])
    for _ in range(200):
        if os.path.exists(l):
            break
        time.sleep(0.01)
''')
    assert r.returncode != 0 and "index.lock left behind" in r.stdout and "held by" in r.stdout, r.stdout


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
def test_a_test_owned_lock_removed_during_the_grace_still_fails(tmp_path):
    """The test's own (in-process) lock is still there at teardown; something of the test removes it a moment later.
    Clearing within the grace is no evidence that anyone else owned it."""
    r = _run_inner(tmp_path, '''
import os, threading, time
def test_own_lock():
    l = {main!r} + "/.git/index.lock"
    open(l, "w").close()
    threading.Timer(0.5, lambda: os.remove(l)).start()
''')
    assert r.returncode != 0 and "index.lock left behind" in r.stdout, r.stdout


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
def test_a_holderless_lock_is_not_excused_by_an_unrelated_external_git_on_main(tmp_path):
    """An external git aimed at main (by cwd and -C) that does NOT hold the lock proves nothing about who left it."""
    r = _run_inner(tmp_path, _EXTERNAL + '''
import os, shutil, subprocess, time
def test_left_lock():
    fake_git = os.path.join(os.path.dirname({main!r}), "git")
    os.symlink(sys.executable, fake_git)  # a process named git, cwd main, `-C main` in argv, no fd on the lock
    env = {{k: v for k, v in os.environ.items() if k not in ("STEWARD_TEST_RUN_MARK", "PYTEST_XDIST_TESTRUNUID")}}
    p = subprocess.Popen([fake_git, "-c", "import time; time.sleep(6)", "-C", {main!r}], cwd={main!r}, env=env)
    time.sleep(0.3)
    assert p.poll() is None and os.readlink(f"/proc/{{p.pid}}/cwd") == {main!r}
    open({main!r} + "/.git/index.lock", "w").close()  # left behind by the test itself
''')
    assert r.returncode != 0 and "index.lock left behind" in r.stdout, r.stdout


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
def test_an_index_refresh_by_a_process_outside_the_run_does_not_fail_the_test(tmp_path):
    r = _run_inner(tmp_path, _EXTERNAL + '''
import os, shutil, subprocess
def test_innocent():
    env = {{k: v for k, v in os.environ.items() if k not in ("STEWARD_TEST_RUN_MARK", "PYTEST_XDIST_TESTRUNUID")}}
    open({main!r} + "/f", "a").write("z")  # the live checkout's file changes; an outside git stages it
    subprocess.run(["sh", "-c", "umask 002; git -C {main} add f"], env=env, check=True)
''')
    assert r.returncode == 0, r.stdout


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
def test_a_test_writing_the_index_in_process_still_fails(tmp_path):
    r = _run_inner(tmp_path, '''
import os
def test_writes_index():
    p = {main!r} + "/.git/index"
    data = open(p, "rb").read()
    os.remove(p)
    with open(p, "wb") as fh:
        fh.write(data)
''')
    assert r.returncode != 0 and "index rewritten" in r.stdout, r.stdout


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
def test_a_child_that_creates_and_closes_the_lock_then_exits_still_fails(tmp_path):
    """The lock's creator is a short-lived child of the test (gone before any /proc sampling) and something of the
    test removes the lock during the grace: nothing outside the run made it, so the test is flagged."""
    r = _run_inner(tmp_path, '''
import os, subprocess, sys, threading
def test_child_lock():
    l = {main!r} + "/.git/index.lock"
    subprocess.run([sys.executable, "-c", f"open({{l!r}}, 'w').close()"], check=True)  # created, closed, exited
    threading.Timer(0.5, lambda: os.remove(l)).start()
''')
    assert r.returncode != 0 and "index.lock left behind" in r.stdout and "umask" in r.stdout, r.stdout


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
def test_absolute_path_git_holding_the_lock_still_fails(tmp_path):
    """A git invoked by absolute path (no PATH lookup) still holds the lock when the test ends."""
    r = _run_inner(tmp_path, '''
import os, shutil, subprocess, time
def test_abs_git():
    git = os.path.realpath(shutil.which("git", path=os.defpath))  # the system git, never a PATH lookup
    subprocess.Popen([git, "-C", {main!r}, "-c", "core.editor=sleep 1", "commit", "-q", "--allow-empty"])
    for _ in range(300):
        if os.path.exists({main!r} + "/.git/index.lock"):
            break
        time.sleep(0.01)
''')
    assert r.returncode != 0 and "index.lock left behind" in r.stdout, r.stdout


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
def test_absolute_path_git_rewriting_the_index_still_fails(tmp_path):
    r = _run_inner(tmp_path, '''
import os, shutil, subprocess
def test_abs_git_add():
    git = os.path.realpath(shutil.which("git", path=os.defpath))  # the system git, never a PATH lookup
    open({main!r} + "/g", "w").write("y")
    subprocess.run([git, "-C", {main!r}, "add", "g"], check=True)
''')
    assert r.returncode != 0 and "index rewritten" in r.stdout, r.stdout


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
def test_a_child_writing_the_index_in_place_still_fails(tmp_path):
    """Same file rewritten in place by a short-lived child (its mode untouched): never an outside git's rename."""
    r = _run_inner(tmp_path, '''
import subprocess, sys
def test_child_in_place():
    p = {main!r} + "/.git/index"
    subprocess.run([sys.executable, "-c", f"d = open({{p!r}}, 'rb').read(); open({{p!r}}, 'r+b').write(d)"], check=True)
''')
    assert r.returncode != 0 and "index rewritten" in r.stdout, r.stdout
