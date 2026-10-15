"""Baseline check run at a recorded commit (SP-16).

Only authorized (`kind == "check"`) discovered commands ever run — discovery
never grants execution authority for anything else (SP-7). Pytest-shaped
commands get a `--junitxml` flag so individual tests can be told apart later
when comparing against a verification run; everything else is exit-code
only.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from steward import pytest_parallel
from steward.discovery import Discovery, explain_broken
from steward.models import Judgment
from steward.runner_exec import Runner
from steward.store import Store, resolve_home

_PYTEST_RE = re.compile(r"\bpytest\b")

# JUnit XML marks non-pass outcomes as a child element on <testcase>.
_OUTCOME_TAGS = {
    "failure": "failed",
    "error": "error",
    "skipped": "skipped",
}


_SEVERITY = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}  # duplicate testcase ids keep the most severe


@dataclass(frozen=True)
class CheckResult:
    command: str
    exit_code: int
    tests: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Baseline:
    commit_sha: str
    results: list[CheckResult] = field(default_factory=list)


_BARE_PYTEST_RE = re.compile(r"^\s*pytest\b")
_PYTEST_BROKEN = {
    2: "pytest interrupted or hit a collection error (exit 2)",
    4: "pytest usage error (exit 4)",
    5: "pytest collected no tests (exit 5)",
}


def broken_reason(line: str, exit_code: int) -> str | None:
    """Why a check command's own exit means the check itself is broken (not that the code under test
    fails): command not found / not executable, or pytest's usage/collection exits 2/4/5."""
    if exit_code == 127:
        return "command not found (exit 127)"
    if exit_code == 126:
        return "command not executable (exit 126)"
    if _PYTEST_RE.search(line) and exit_code in _PYTEST_BROKEN:
        return _PYTEST_BROKEN[exit_code]
    return None


@dataclass(frozen=True)
class CheckRun:
    exit_code: int
    tests: dict
    broken: str | None = None  # why the check itself is broken, else None
    used: str = ""  # the line that actually produced this result (differs after a python3 -m pytest fallback)
    output: str = ""  # stdout + stderr of the run that produced this result


_PROJECT_ENV_RE = re.compile(r"(^|[/\\'\"])\.?venv[/\\]|poetry run|uv run")
_IMPORT_ERROR_RE = re.compile(r"ImportError while importing|ModuleNotFoundError|No module named")


def uses_project_env(line: str) -> bool:
    return bool(_PROJECT_ENV_RE.search(line))


@contextlib.contextmanager
def check_scratch():
    """Env for one check run whose temp files (pytest's `pytest-of-<user>` basetemp included) live under the Steward
    state dir, not /tmp (a small usrquota tmpfs that these runs once filled), and are removed afterwards."""
    root = resolve_home() / "tmp"
    scratch = root / f"check-{uuid.uuid4().hex[:12]}"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        yield {**os.environ, "TMPDIR": str(scratch), "PYTEST_DEBUG_TEMPROOT": str(scratch)}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def run_check(runner: Runner, line: str, cwd: Path, xml_path: Path, python_env: str = "", *,
              timeout_s: float | None = None, fallback: bool = True) -> CheckRun:
    """Run one authorized check. Bare `pytest` failing with a usage/import/collection error (or not found)
    is retried once as `python3 -m pytest`; a result that is still broken is reported as `broken`, so the
    caller can mark verification unverified instead of counting it as a pass or a failure."""
    pytest_shaped = bool(_PYTEST_RE.search(line))
    xml_path.unlink(missing_ok=True)
    with check_scratch() as env, pytest_parallel.plan(line, cwd, env) as pl:
        full = f"{pl.line} --junitxml={xml_path}" if pytest_shaped else line
        try:
            result = runner.shell_line(full, cwd=cwd, env=pl.env, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            try:
                tests = _parse_junit(xml_path) if pytest_shaped else {}
            except (ET.ParseError, OSError):
                tests = {}
            output = "\n".join(str(v or "") for v in (exc.stdout, exc.stderr))
            return CheckRun(exit_code=124, tests=tests, broken=f"timed out after {timeout_s}s",
                            used=line, output=output)
    used = line
    broken = broken_reason(line, result.exit_code)
    not_run = pytest_shaped and result.exit_code != 0 and not xml_path.exists()  # e.g. a stale pytest shim on PATH
    if fallback and (broken or not_run) and _BARE_PYTEST_RE.match(line):
        used = _BARE_PYTEST_RE.sub("python3 -m pytest", line, count=1)
        # eligibility and xdist availability are recomputed for the fallback interpreter
        with check_scratch() as env, pytest_parallel.plan(used, cwd, env) as pl:
            retry = runner.shell_line(f"{pl.line} --junitxml={xml_path}", cwd=cwd, env=pl.env, timeout=timeout_s)
        result, broken = retry, broken_reason(used, retry.exit_code)
    try:
        tests = _parse_junit(xml_path) if pytest_shaped else {}
    except (ET.ParseError, OSError) as exc:  # empty/truncated report (e.g. a full /tmp): unverified, never a crash
        tests = {}
        broken = broken or f"junit report unreadable ({exc})"
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    off_env = pytest_shaped and not uses_project_env(used)
    if (off_env and result.exit_code != 0 and not broken and _IMPORT_ERROR_RE.search(output)
            and any(o == "error" for o in tests.values())):
        # an import/collection error under a non-project interpreter is the environment, not a failing test
        broken, tests = "pytest collection error (import failed, exit %d)" % result.exit_code, {}
    if broken and pytest_shaped:
        broken = explain_broken(broken, output, python_env if off_env else "")
    return CheckRun(exit_code=result.exit_code, tests=tests, broken=broken, used=used, output=output)


_FAILED_ID_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.M)


def failed_ids(output: str) -> list[str]:
    """Test ids pytest names in its short summary (`FAILED tests/x.py::t - ...`), deduped, in order."""
    seen: list[str] = []
    for m in _FAILED_ID_RE.finditer(output or ""):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def describe_failure(line: str, exit_code: int, output: str) -> list[str]:
    """What to call a failing check that produced no per-test report: the failing tests pytest named, else
    the command with its exit code and last output line, never the bare command (an opaque `['pytest']`)."""
    ids = failed_ids(output)
    if ids:
        return ids
    tail = next((ln.strip() for ln in reversed((output or "").splitlines()) if ln.strip()), "")
    return [f"{line} (exit {exit_code}" + (f": {tail[:120]}" if tail else "") + ")"]


def pre_existing_failure_count(baseline: Baseline) -> int:
    """Count baseline failures, deduped by test id across check commands.

    Two authorized check commands can exercise the same test (e.g. `pytest`
    and `make test` both running the suite) — a single failing test must not
    be counted twice. Commands with no per-test detail (exit-code only) have
    no test id to dedupe by, so each such failing command is counted once.
    """
    failed_tests: set[str] = set()
    exit_only_failures = 0
    for result in baseline.results:
        if result.tests:
            for name, outcome in result.tests.items():
                if outcome in ("failed", "error"):
                    failed_tests.add(name)
        elif result.exit_code != 0:
            exit_only_failures += 1
    return len(failed_tests) + exit_only_failures


def record_check_events(store: Store, job_id: str, phase: str, line: str, run: CheckRun) -> None:
    """`check_fallback` when bare pytest was rerun as `python3 -m pytest`; `check_broken` when the
    check still cannot run (the verification is then unverified, never "yes")."""
    if run.used != line:
        store.append_event(job_id, what="check_fallback",
                           evidence={"phase": phase, "command": line, "used": run.used, "broken": bool(run.broken)},
                           rule="check_fallback_python_m_pytest", judgment=Judgment.code)
    if run.broken:
        store.append_event(job_id, what="check_broken",
                           evidence={"phase": phase, "command": line, "exit": run.exit_code, "reason": run.broken},
                           rule="check_command_broken", judgment=Judgment.code)


def _git_head(repo: Path, runner: Runner) -> str:
    result = runner.run(["git", "rev-parse", "HEAD"], cwd=repo)
    return result.stdout.strip()


def _parse_junit(xml_path: Path) -> dict[str, str]:
    tests: dict[str, str] = {}
    if not xml_path.exists():
        return tests
    tree = ET.parse(xml_path)
    for case in tree.getroot().iter("testcase"):
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        outcome = "passed"
        for tag, mapped in _OUTCOME_TAGS.items():
            if case.find(tag) is not None:
                outcome = mapped
                break
        if _SEVERITY[outcome] >= _SEVERITY[tests.get(name, "passed")]:
            tests[name] = outcome
    return tests


def run_baseline(
    store: Store,
    job_id: str,
    repo: Path | str,
    discovery: Discovery,
    runner: Runner,
    *, check_fn=None,
) -> Baseline:
    repo = Path(repo)
    commit_sha = _git_head(repo, runner)
    results: list[CheckResult] = []
    import time

    t0 = time.monotonic()

    with tempfile.TemporaryDirectory() as tmp:
        for idx, cmd in enumerate(discovery.commands):
            if cmd.kind != "check":
                continue
            run = (check_fn or run_check)(runner, cmd.line, repo, Path(tmp) / f"baseline-{idx}.xml", discovery.python_env)
            result, tests = run, run.tests
            check = CheckResult(command=cmd.line, exit_code=result.exit_code, tests=tests)
            results.append(check)
            record_check_events(store, job_id, "baseline", cmd.line, run)
            store.add_check(
                job_id,
                commit_sha=commit_sha,
                command=cmd.line,
                exit_code=result.exit_code,
                results=tests,
                label="baseline",
            )

    # v2.6 (SUP-02): how long the checks take predicts whether a candidate's verification fits the tick budget
    store.kv_set(job_id, "baseline_duration_s", round(time.monotonic() - t0, 1))
    store.append_event(
        job_id,
        what="baseline recorded",
        evidence={
            "commit_sha": commit_sha,
            "commands": [r.command for r in results],
        },
        rule="discovered_check_authorized",
        judgment=Judgment.code,
    )

    return Baseline(commit_sha=commit_sha, results=results)
