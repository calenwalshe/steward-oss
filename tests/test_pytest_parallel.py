"""Parallel (xdist) full-suite checks: eligibility, -n injection, off switch, fallbacks, host-wide worker cap."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from steward import autopilot, check_runs, pytest_parallel as pp, verify_reuse
from steward.baseline import CheckRun, _parse_junit, run_check
from steward.runner_exec import CommandResult, FakeRunner
from steward.stability import is_full_suite


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in (pp.WORKERS_ENV, pp.NESTED_ENV, pp.CAP_ENV, pp.WAIT_ENV):
        monkeypatch.delenv(var, raising=False)
    pp._probe_cache.clear()
    monkeypatch.setattr(pp, "xdist_available", lambda *a, **k: True)


@pytest.fixture
def steward_tree(tmp_path):
    root = tmp_path / "wt"
    (root / "steward").mkdir(parents=True)
    (root / "steward" / "pytest_parallel.py").write_text("")
    (root / "pyproject.toml").write_text('[project]\nname = "steward"\n')
    return root


class Rec(FakeRunner):
    def shell_line(self, line, cwd, *, timeout=None, env=None):
        self.calls.append({"line": line, "env": env, "timeout": timeout})
        return CommandResult(argv=["bash", "-lc", line], cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0)


@pytest.mark.parametrize("line,ok", [
    ("pytest", True), ("pytest -q", True), ("python3 -m pytest", True), ("python3 -m pytest -q", True),
    ("pytest tests", True), ("pytest tests/", True), (".venv/bin/pytest -q", True),
    ("pytest tests/test_a.py", False), ("pytest tests/test_a.py::t", False), ("pytest -k foo", False),
    ("pytest -k=foo", False), ("pytest -kfoo", False), ("pytest -m slow", False), ("pytest -m=slow", False),
    ("pytest -n 2", False), ("pytest -n2", False), ("pytest -n auto", False), ("pytest --numprocesses=3", False),
    ("pytest -p no:xdist", False), ("pytest -pno:xdist", False),
    ("pytest && echo hi", False), ("cd x; pytest", False), ("pytest | tee log", False), ("make test", False),
    ("poetry run pytest", False), ("npm test", False),
])
def test_eligibility(line, ok):
    assert pp.eligible(line) is ok


def test_is_full_suite_fixes():
    assert is_full_suite("pytest tests") and is_full_suite("python3 -m pytest")
    assert not is_full_suite("pytest -k=foo") and not is_full_suite("pytest tests/test_x.py")
    assert not is_full_suite("pytest -m=slow")


def test_injects_n_on_the_cli_not_in_addopts(steward_tree, tmp_path):
    runner = Rec()
    run_check(runner, "pytest -q", steward_tree, tmp_path / "j.xml")
    call = runner.calls[0]
    assert call["line"] == f"pytest -q -n 8 --junitxml={tmp_path / 'j.xml'}"
    assert "PYTEST_ADDOPTS" not in call["env"] or "-n" not in call["env"].get("PYTEST_ADDOPTS", "")
    assert call["env"][pp.NESTED_ENV] == "1"


def test_worker_count_and_off_switch(steward_tree, tmp_path, monkeypatch):
    monkeypatch.setenv(pp.WORKERS_ENV, "3")
    r = Rec(); run_check(r, "pytest", steward_tree, tmp_path / "j.xml")
    assert " -n 3 " in r.calls[0]["line"]
    monkeypatch.setenv(pp.WORKERS_ENV, "0")
    r = Rec(); run_check(r, "pytest", steward_tree, tmp_path / "j.xml")
    assert "-n" not in r.calls[0]["line"]


def test_other_repos_stay_serial_unless_opted_in(tmp_path, monkeypatch):
    other = tmp_path / "other"; other.mkdir()
    r = Rec(); run_check(r, "pytest", other, tmp_path / "j.xml")
    assert " -n " not in r.calls[0]["line"]
    monkeypatch.setenv(pp.WORKERS_ENV, "2")
    r = Rec(); run_check(r, "pytest", other, tmp_path / "j.xml")
    assert " -n 2 " in r.calls[0]["line"]


def test_explicit_n_and_selected_runs_are_untouched(steward_tree, tmp_path):
    for line in ("pytest -n 2", "pytest tests/test_x.py", "pytest -k foo"):
        r = Rec(); run_check(r, line, steward_tree, tmp_path / "j.xml")
        assert r.calls[0]["line"] == f"{line} --junitxml={tmp_path / 'j.xml'}"


def test_missing_xdist_runs_serial(steward_tree, tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "xdist_available", lambda *a, **k: False)
    r = Rec(); run_check(r, "pytest", steward_tree, tmp_path / "j.xml")
    assert r.calls[0]["line"] == f"pytest --junitxml={tmp_path / 'j.xml'}"


def test_real_probe_uses_the_commands_interpreter(tmp_path, monkeypatch):
    monkeypatch.undo()  # drop the autouse stub: probe for real
    pp._probe_cache.clear()
    assert pp.xdist_available(f"{sys.executable} -m pytest", tmp_path) is True
    fake = tmp_path / "bin"; fake.mkdir()
    bare = fake / "python3"; bare.write_text("#!/bin/sh\nexit 1\n"); bare.chmod(0o755)
    assert pp.xdist_available(f"{bare} -m pytest", tmp_path) is False


def test_python_m_fallback_recomputes_eligibility_and_availability(steward_tree, tmp_path, monkeypatch):
    probes = []
    monkeypatch.setattr(pp, "xdist_available", lambda line, cwd, env=None: probes.append(line) or line.startswith("python3"))
    calls = []

    class R(FakeRunner):
        def shell_line(self, line, cwd, *, timeout=None, env=None):
            calls.append(line)
            code = 127 if line.startswith("pytest") else 0
            return CommandResult(argv=[], cwd=str(cwd), exit_code=code, stdout="", stderr="", duration_s=0)
    run_check(R(), "pytest", steward_tree, tmp_path / "j.xml")
    assert probes == ["pytest", "python3 -m pytest"]
    assert calls[0] == f"pytest --junitxml={tmp_path / 'j.xml'}"  # no xdist for the bare shim
    assert calls[1] == f"python3 -m pytest -n 8 --junitxml={tmp_path / 'j.xml'}"


def test_nested_run_never_auto_parallelizes(steward_tree, tmp_path, monkeypatch):
    monkeypatch.setenv(pp.NESTED_ENV, "1")
    monkeypatch.setenv(pp.WORKERS_ENV, "4")
    r = Rec(); run_check(r, "pytest", steward_tree, tmp_path / "j.xml")
    assert " -n " not in r.calls[0]["line"]
    assert pp.execution_label(["pytest"]) == "serial"


def test_non_pytest_line_is_untouched(steward_tree, tmp_path):
    r = Rec(); run_check(r, "make test", steward_tree, tmp_path / "j.xml")
    assert r.calls[0]["line"] == "make test" and pp.NESTED_ENV not in r.calls[0]["env"]


# -- host-wide cap ------------------------------------------------------------------------------------------------

_HOLDER = textwrap.dedent("""
    import sys
    from steward import pytest_parallel as pp
    lease = pp.try_acquire(int(sys.argv[1]))
    print("held" if lease else "refused", flush=True)
    sys.stdin.readline()
""")


def _holder(n, home, cap="16"):
    env = {**__import__("os").environ, "STEWARD_HOME": str(home), pp.CAP_ENV: cap,
           "PYTHONPATH": str(Path(pp.__file__).resolve().parent.parent)}
    return subprocess.Popen([sys.executable, "-c", _HOLDER, str(n)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, env=env)


def test_semaphore_capacity_is_shared_across_processes(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_HOME", str(steward_home))
    first = _holder(8, steward_home)
    assert first.stdout.readline().strip() == "held"
    second = _holder(8, steward_home)
    assert second.stdout.readline().strip() == "held"
    assert pp.try_acquire(8) is None  # 16 of 16 held by two other processes
    first.stdin.write("\n"); first.stdin.flush(); first.wait(10)
    lease = pp.try_acquire(8)
    assert lease is not None  # a finished process frees its slots
    assert pp.try_acquire(8) is None
    pp.release(lease)
    second.kill(); second.wait(10)  # a killed holder's lease is reclaimed (pid gone)
    lease = pp.try_acquire(16)
    assert lease is not None
    pp.release(lease)


def test_no_capacity_within_the_wait_runs_serial(steward_tree, steward_home, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_HOME", str(steward_home))
    monkeypatch.setenv(pp.WAIT_ENV, "0")
    held = pp.try_acquire(16)
    assert held is not None
    r = Rec(); run_check(r, "pytest", steward_tree, tmp_path / "j.xml")
    assert " -n " not in r.calls[0]["line"]  # bounded wait, then serial
    pp.release(held)
    r = Rec(); run_check(r, "pytest", steward_tree, tmp_path / "j.xml")
    assert " -n 8 " in r.calls[0]["line"]
    assert pp.try_acquire(16) is not None  # the run released its lease


def test_acquire_waits_for_capacity(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_HOME", str(steward_home))
    held = pp.try_acquire(16)
    ticks = []

    def sleep(s):
        ticks.append(s)
        pp.release(held)
    lease = pp.acquire(8, wait_s=60, sleep=sleep)
    assert lease is not None and ticks
    pp.release(lease)


# -- fingerprints -------------------------------------------------------------------------------------------------


def test_fingerprints_differ_serial_vs_parallel(monkeypatch):
    monkeypatch.setenv(pp.WORKERS_ENV, "0")
    serial_tc, serial_env = verify_reuse.toolchain_fingerprint(["pytest"]), check_runs.env_digest("", pp.execution_label(["pytest"]))
    monkeypatch.setenv(pp.WORKERS_ENV, "8")
    par_tc, par_env = verify_reuse.toolchain_fingerprint(["pytest"]), check_runs.env_digest("", pp.execution_label(["pytest"]))
    monkeypatch.setenv(pp.WORKERS_ENV, "4")
    assert serial_tc != par_tc and serial_env != par_env
    assert verify_reuse.toolchain_fingerprint(["pytest"]) != par_tc  # the worker count counts too
    assert verify_reuse.toolchain_fingerprint(["make test"]) == verify_reuse.toolchain_fingerprint(["make test"])


def test_spec_for_carries_execution_mode(steward_tree, monkeypatch):
    class Git(FakeRunner):
        def run(self, argv, cwd, **kw):
            out = "tree1\n" if argv[:2] == ["git", "rev-parse"] else ""
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=out, stderr="", duration_s=0)
    monkeypatch.setenv(pp.WORKERS_ENV, "0")
    serial = check_runs.spec_for(Git(), steward_tree, "pytest", toolchain_fn=lambda c: "t")
    monkeypatch.setenv(pp.WORKERS_ENV, "8")
    par = check_runs.spec_for(Git(), steward_tree, "pytest", toolchain_fn=lambda c: "t")
    assert serial.env_digest != par.env_digest


# -- junit --------------------------------------------------------------------------------------------------------


def test_junit_duplicates_keep_the_most_severe(tmp_path):
    xml = tmp_path / "j.xml"
    xml.write_text('<testsuites><testsuite>'
                   '<testcase classname="a" name="x"><failure/></testcase><testcase classname="a" name="x"/>'
                   '<testcase classname="a" name="y"/><testcase classname="a" name="y"><skipped/></testcase>'
                   '<testcase classname="a" name="z"><skipped/></testcase><testcase classname="a" name="z"><error/></testcase>'
                   '<testcase classname="a" name="w"><error/></testcase><testcase classname="a" name="w"><failure/></testcase>'
                   '</testsuite></testsuites>')
    assert _parse_junit(xml) == {"a::x": "failed", "a::y": "skipped", "a::z": "error", "a::w": "error"}


# -- autopilot _run_checks ----------------------------------------------------------------------------------------


def test_run_checks_default_path_goes_through_run_check(steward_tree, monkeypatch):
    r = Rec()
    assert autopilot._run_checks(steward_tree, r, ["pytest -q"]) is None
    assert " -n 8 " in r.calls[0]["line"] and r.calls[0]["timeout"] == autopilot.CHECK_TIMEOUT_S
    assert "--junitxml=" in r.calls[0]["line"]


def test_run_checks_default_path_reports_red_and_timeout(steward_tree):
    class Red(Rec):
        def shell_line(self, line, cwd, *, timeout=None, env=None):
            return CommandResult(argv=[], cwd=str(cwd), exit_code=1, stdout="", stderr="", duration_s=0)
    assert "red" in autopilot._run_checks(steward_tree, Red(), ["pytest"])

    class Slow(Rec):
        def shell_line(self, line, cwd, *, timeout=None, env=None):
            raise subprocess.TimeoutExpired(line, timeout)
    assert autopilot.TIMED_OUT in autopilot._run_checks(steward_tree, Slow(), ["pytest"])


def test_run_checks_check_fn_path_gets_timeout_and_keeps_the_seam(tmp_path):
    seen = {}

    def with_timeout(runner, line, cwd, xml, env, *, timeout_s=None):
        seen["t"] = timeout_s
        return CheckRun(exit_code=0, tests={"a::b": "passed"})

    assert autopilot._run_checks(tmp_path, FakeRunner(), ["pytest"], check_fn=with_timeout) is None
    assert seen["t"] == autopilot.CHECK_TIMEOUT_S and autopilot._tl.passed == 1

    def legacy(runner, line, cwd, xml, env):  # an old seam without a timeout parameter still works
        return CheckRun(exit_code=1, tests={})
    assert "red" in autopilot._run_checks(tmp_path, FakeRunner(), ["pytest"], check_fn=legacy)

    def timed_out(runner, line, cwd, xml, env, *, timeout_s=None):
        return CheckRun(exit_code=124, tests={}, broken=f"timed out after {timeout_s}s")
    assert autopilot.TIMED_OUT in autopilot._run_checks(tmp_path, FakeRunner(), ["pytest"], check_fn=timed_out)
