"""Verification against the exact commit a worker's checks ran at (SP-16).

`verify` never commits on the worker's behalf: a dirty worktree means the
worker's changes are not yet at a resolvable, citable commit, so verification
records `dirty=True` and skips running checks entirely — the worker must
commit first (SP-16: completion references exact code state). Otherwise it
re-runs every authorized check command exactly as `steward.baseline` did, and
diffs the result per-test (or, for exit-code-only commands, by exit code)
against the stored baseline: pre-existing failures never block progress —
only *new* failures do.
"""

from __future__ import annotations

import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from steward.baseline import CheckResult, describe_failure, record_check_events, run_check
from steward.discovery import Discovery
from steward.models import Judgment
from steward.runner_exec import Runner
from steward.store import Store

_PYTEST_RE = re.compile(r"\bpytest\b")

_OUTCOME_TAGS = {
    "failure": "failed",
    "error": "error",
    "skipped": "skipped",
}


@dataclass(frozen=True)
class Verification:
    commit_sha: str
    results: list[CheckResult] = field(default_factory=list)
    new_failures: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    preexisting_failures: list[str] = field(default_factory=list)
    dirty: bool = False
    # Checks that could not run at all (command not found / pytest usage or collection error): the run
    # proves nothing, so the verification is unverified rather than "yes" (`steward.stability`).
    broken: list[dict] = field(default_factory=list)
    # The diff against the baseline touched only docs: the test gate was skipped (nothing it could catch).
    docs_only: bool = False
    # Tests failed while the background baseline was still running, so none could be called new or pre-existing:
    # closeout parks the job and re-attributes once the baseline lands (`closeout.reattribute_after_baseline`).
    unattributed: bool = False

    @property
    def unverified(self) -> bool:
        return bool(self.broken)

    @property
    def unverified_reason(self) -> str:
        return "; ".join(f"`{b['command']}`: {b['reason']}" for b in self.broken)


def failure_messages(xml_path: Path, names: list[str], *, cap: int = 300) -> dict[str, str]:
    """`{test: message}` for the named failing tests, from the JUnit report (v2.6: what the first failing test said
    goes to the worker and the tech lead, not only its name). Best effort: an unreadable report gives {}."""
    out: dict[str, str] = {}
    want = set(names)
    try:
        tree = ET.parse(xml_path)
    except (ET.ParseError, OSError):
        return out
    for case in tree.getroot().iter("testcase"):
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        if name not in want:
            continue
        el = case.find("failure") if case.find("failure") is not None else case.find("error")
        if el is not None:
            msg = " ".join((el.get("message") or el.text or "").split())
            out[name] = msg[:cap]
    return out


def _parse_junit(xml_path: Path) -> dict[str, str]:
    tests: dict[str, str] = {}
    if not xml_path.exists():
        return tests
    try:
        tree = ET.parse(xml_path)
    except (ET.ParseError, OSError):
        return tests
    for case in tree.getroot().iter("testcase"):
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        outcome = "passed"
        for tag, mapped in _OUTCOME_TAGS.items():
            if case.find(tag) is not None:
                outcome = mapped
                break
        tests[name] = outcome
    return tests


_DOC_SUFFIXES = (".md", ".rst", ".txt")
_DOC_DIRS = ("docs/", ".planning/", ".steward/")


def is_docs_path(path: str) -> bool:
    return path.endswith(_DOC_SUFFIXES) or path.startswith(_DOC_DIRS)


def _merge_base_with_repo_head(store, job_id: str, runner: Runner, worktree: Path) -> str | None:
    """The job's diff base when no baseline was recorded (a repo with no discovered checks): the merge-base of the
    worktree HEAD with the repo's HEAD."""
    job = store.get_job(job_id)
    if job is None:
        return None
    try:
        head = runner.run(["git", "rev-parse", "HEAD"], cwd=Path(job.repo))
        target = head.stdout.strip() if head.exit_code == 0 else ""
        if not target:
            return None
        mb = runner.run(["git", "merge-base", target, "HEAD"], cwd=worktree)
    except OSError:  # the repo path is gone or unreadable: no base, the gate runs as before
        return None
    return (mb.stdout.strip() or None) if mb.exit_code == 0 else None


def _docs_only_diff(runner: Runner, worktree: Path, base_sha: str | None) -> bool:
    """True when base..HEAD changed files and every one is documentation."""
    if not base_sha:
        return False
    res = runner.run(["git", "diff", "--name-only", f"{base_sha}..HEAD"], cwd=worktree)
    paths = [p for p in res.stdout.splitlines() if p.strip()] if res.exit_code == 0 else []
    return bool(paths) and all(is_docs_path(p) for p in paths)


def recorded_base(store: Store, job_id: str, baseline_rows: list[dict]) -> str | None:
    """The job's recorded base commit: the baseline check rows' sha, else the `baseline recorded` event's own
    `commit_sha` (u6: a baseline with no check commands writes no rows but still records the base it pinned)."""
    if baseline_rows:
        return baseline_rows[0]["commit_sha"]
    ev = store.last_event(job_id, ["baseline recorded"])
    sha = (ev.evidence or {}).get("commit_sha") if ev is not None else None
    return str(sha) if sha else None


def _merge_base_tests(runner: Runner, worktree: Path, base_sha: str, line: str, check_fn=None) -> dict | None:
    """Run `line` (the very same check command) on a clean detached checkout of the merge-base, so the
    baseline has the same test selection as the verification run. None when that cannot be done."""
    import shutil

    tmp = Path(tempfile.mkdtemp(prefix="steward-mbbase-"))
    tree = tmp / "tree"
    try:
        add = runner.run(["git", "worktree", "add", "--detach", str(tree), base_sha], cwd=worktree)
        if add.exit_code != 0:
            return None
        run = (check_fn or run_check)(runner, line, tree, tmp / "mb.xml", "")
        return None if run.broken else run.tests
    finally:
        runner.run(["git", "worktree", "remove", "--force", str(tree)], cwd=worktree)
        shutil.rmtree(tmp, ignore_errors=True)


def verify(
    store: Store,
    job_id: str,
    worktree: Path | str,
    discovery: Discovery,
    runner: Runner,
    *, check_fn=None,
) -> Verification:
    check_fn = check_fn or run_check
    worktree = Path(worktree)

    status = runner.run(["git", "status", "--porcelain"], cwd=worktree)
    head_result = runner.run(["git", "rev-parse", "HEAD"], cwd=worktree)
    commit_sha = head_result.stdout.strip()

    if status.stdout.strip():
        store.append_event(
            job_id,
            what="verify_worktree_dirty",
            evidence={"commit_sha": commit_sha},
            rule="verify_requires_clean_worktree",
            judgment=Judgment.code,
        )
        return Verification(commit_sha=commit_sha, dirty=True)

    from steward import bg_baseline

    bg_baseline.wait(store, job_id)  # latency #5: attribution needs the baseline; wait only while it is running
    baseline_rows = store.get_checks(job_id, label="baseline")
    baseline_checks = {c["command"]: c for c in baseline_rows}
    base_sha = baseline_rows[0]["commit_sha"] if baseline_rows else None
    docs_base = recorded_base(store, job_id, baseline_rows) or _merge_base_with_repo_head(
        store, job_id, runner, worktree)

    if _docs_only_diff(runner, worktree, docs_base):
        store.add_check(job_id, commit_sha=commit_sha, command="(docs-only diff: test gate skipped)",
                        exit_code=0, results={}, label="verify")
        store.append_event(
            job_id, what="verify_skipped_docs_only",
            evidence={"commit_sha": commit_sha, "base": docs_base},
            rule="verify_docs_only_skips_tests", judgment=Judgment.code,
        )
        return Verification(commit_sha=commit_sha, docs_only=True)

    results: list[CheckResult] = []
    new_failures: list[str] = []
    fixed: list[str] = []
    preexisting: list[str] = []
    broken: list[dict] = []
    messages: dict[str, str] = {}

    with tempfile.TemporaryDirectory() as tmp:
        for idx, cmd in enumerate(discovery.commands):
            if cmd.kind != "check":
                continue
            run = check_fn(runner, cmd.line, worktree, Path(tmp) / f"verify-{idx}.xml", discovery.python_env)
            result, tests = run, run.tests
            check = CheckResult(command=cmd.line, exit_code=result.exit_code, tests=tests)
            results.append(check)
            record_check_events(store, job_id, "verify", cmd.line, run)
            store.add_check(
                job_id,
                commit_sha=commit_sha,
                command=cmd.line,
                exit_code=result.exit_code,
                results=tests,
                label="verify",
            )

            if run.broken:
                broken.append({"command": cmd.line, "exit": run.exit_code, "reason": run.broken})
                continue  # a check that cannot run is neither a pass nor a regression

            baseline_check = baseline_checks.get(cmd.line)
            if tests:
                baseline_tests = baseline_check["results"] if baseline_check else {}
                absent = [n for n, o in tests.items() if o in ("failed", "error") and n not in baseline_tests]
                if absent and baseline_check and base_sha:
                    # failing tests the baseline never saw: it may simply have run a different selection, so
                    # re-take the baseline at the merge-base with this exact command before calling them new
                    mb = _merge_base_tests(runner, worktree, base_sha, cmd.line, check_fn)
                    if mb:
                        baseline_tests = {**baseline_tests, **mb}
                        store.add_check(job_id, commit_sha=base_sha, command=cmd.line, exit_code=1 if any(
                            o in ("failed", "error") for o in mb.values()) else 0, results=mb, label="baseline-merge-base")
                fresh = []
                for name, outcome in tests.items():
                    now_failed = outcome in ("failed", "error")
                    was_failed = baseline_tests.get(name) in ("failed", "error")
                    if now_failed and was_failed:
                        preexisting.append(name)
                    elif now_failed and not was_failed:
                        new_failures.append(name)
                        fresh.append(name)
                    elif not now_failed and was_failed:
                        fixed.append(name)
                if fresh:
                    messages.update(failure_messages(Path(tmp) / f"verify-{idx}.xml", fresh[:5]))
            else:
                baseline_exit = baseline_check["exit_code"] if baseline_check else 0
                now_failed = result.exit_code != 0
                was_failed = baseline_exit != 0
                named = (describe_failure(cmd.line, result.exit_code, run.output)
                         if _PYTEST_RE.search(cmd.line) else [cmd.line])
                if now_failed and was_failed:
                    preexisting.append(cmd.line)
                elif now_failed and not was_failed:
                    new_failures.extend(named)
                elif not now_failed and was_failed:
                    fixed.append(cmd.line)

    unattributed = bool(
        not baseline_rows and (new_failures or preexisting)
        and isinstance(store.kv_get(job_id, bg_baseline.KEY), dict)
        and store.kv_get(job_id, bg_baseline.KEY).get("status") == "running"
    )
    store.append_event(
        job_id,
        what="verification recorded",
        evidence={
            "commit_sha": commit_sha,
            **({"baseline_missing": True} if unattributed else {}),
            "new_failures": new_failures,
            "fixed": fixed,
            "preexisting_failures": preexisting,
            "unverified": broken,
            **({"failure_messages": messages} if messages else {}),
        },
        rule="verify_at_exact_commit",
        judgment=Judgment.code,
    )

    return Verification(
        commit_sha=commit_sha,
        results=results,
        new_failures=new_failures,
        fixed=fixed,
        preexisting_failures=preexisting,
        dirty=False,
        broken=broken,
        unattributed=unattributed,
    )


def is_current(store: Store, job_id: str, head_sha: str) -> bool:
    """True only if the most recent recorded verification ran at `head_sha`.

    A later commit invalidates a prior verification (SP-16): once HEAD moves,
    `is_current` returns False until `verify` runs again at the new sha.
    """
    verify_checks = store.get_checks(job_id, label="verify")
    if not verify_checks:
        return False
    return verify_checks[-1]["commit_sha"] == head_sha
