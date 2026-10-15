#!/usr/bin/env python3
"""Run the one final whole-suite check on a clean, pinned candidate in a detached background process.

The suite uses the existing durable check-run queue in a disposable Store home. Candidate evidence is written to
docs/superpowers/evidence/final-run/; the queue database remains under ~/.cache/steward-final-run and its ephemeral
candidate checkout is removed after the run.
The receipt path is one-shot: an existing receipt is never overwritten or re-run.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# This script is also launched with ``python -I`` for the isolated candidate
# process. In both modes Python otherwise puts only ``scripts/`` on sys.path,
# which prevents imports from the repository's local package.
sys.path.insert(0, str(ROOT))
EVIDENCE = ROOT / "docs" / "superpowers" / "evidence" / "final-run"
RECEIPT = EVIDENCE / "receipt.json"
JUNIT = EVIDENCE / "junit.xml"
LOG = EVIDENCE / "log.txt"
RUNTIME = Path.home() / ".cache" / "steward-final-run"
TIMEOUT_S = 7200
FULL_SUITE_COMMAND = "exec python3 -m pytest -q"
BASELINE_TIMEOUT_S = 1800


class CandidateError(ValueError):
    pass


def isolated_test_environment(home: Path, scratch: Path) -> dict[str, str]:
    """Allow only basic process plumbing; never inherit credentials or user configuration."""
    env = {key: os.environ[key] for key in ("LANG", "LC_ALL") if key in os.environ}
    # Preserve the venv's bin directory. Resolving sys.executable follows its
    # symlink to the bare interpreter, which may not contain pytest.
    env["PATH"] = f"{Path(sys.executable).parent}:{os.defpath}"
    env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "XDG_CACHE_HOME": str(home / ".cache"), "XDG_STATE_HOME": str(home / ".local" / "state"),
                "TMPDIR": str(scratch), "TMP": str(scratch), "TEMP": str(scratch),
                "STEWARD_HOME": str(home), "STEWARD_CONFIG_DIR": str(home / ".config" / "steward"),
                "STEWARD_DATA_DIR": str(home / ".local" / "share" / "steward"),
                "STEWARD_SHARED_CHECKS": "1"})
    return env


def _git(repo: Path, *args: str, check: bool = True) -> str:
    env = isolated_test_environment(Path(repo), Path("/tmp"))
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=repo,
                            capture_output=True, text=True, env=env)
    if check and result.returncode:
        raise CandidateError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def candidate_snapshot(repo: Path, candidate: str, source_branch: str) -> dict:
    repo = Path(repo).resolve()
    head = _git(repo, "rev-parse", "HEAD")
    resolved = _git(repo, "rev-parse", f"{candidate}^{{commit}}")
    if head != resolved:
        raise CandidateError(f"candidate {resolved} is not the checked-out HEAD {head}")
    dirty = _git(repo, "status", "--porcelain")
    if dirty:
        raise CandidateError("candidate worktree is dirty")
    candidate_ref = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    source_head = _git(repo, "rev-parse", f"refs/heads/{source_branch}^{{commit}}")
    git_env = isolated_test_environment(repo, Path("/tmp"))
    git_env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    ancestry = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "merge-base", "--is-ancestor",
                               source_head, resolved], cwd=repo, capture_output=True, env=git_env)
    if ancestry.returncode != 0:
        raise CandidateError(f"candidate does not contain {source_branch} @ {source_head}")
    return {"candidate_commit": resolved, "candidate_ref": candidate_ref,
            "tree": _git(repo, "rev-parse", f"{resolved}^{{tree}}"),
            "source_branch": source_branch, "source_head": source_head}


def _write_receipt(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _atomic_write_locked(path, value)
        fcntl.flock(lock, fcntl.LOCK_UN)


def _atomic_write_locked(path: Path, value: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _set_pid_if_running(path: Path, pid: int) -> None:
    with open(path.with_suffix(".lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = json.loads(path.read_text())
        if current.get("state") == "running":
            current["pid"] = pid
            tmp = path.with_suffix(".json.tmp")
            _atomic_write_locked(path, current)
        fcntl.flock(lock, fcntl.LOCK_UN)


def start(repo: Path, candidate: str, source_branch: str) -> dict:
    snapshot = candidate_snapshot(repo, candidate, source_branch)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    run_id = "final-" + uuid.uuid4().hex[:16]
    receipt = {**snapshot, "run_id": run_id, "state": "running", "exit_code": None, "summary": "",
               "duration_s": None, "started_at": time.time(), "finished_at": None, "host": socket.gethostname(),
               "pid": None, "contains_source": True, "attribution": {"baseline_commit": snapshot["source_head"],
               "new_failures": [], "preexisting_failures": [], "fixed": [], "unattributed_failures": [],
               "baseline_status": "pending", "complete": None}}
    with open(RECEIPT.with_suffix(".lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if RECEIPT.exists() or JUNIT.exists() or LOG.exists():
            fcntl.flock(lock, fcntl.LOCK_UN)
            raise CandidateError("final-run evidence already exists; this run is one-shot")
        _atomic_write_locked(RECEIPT, receipt)
        fcntl.flock(lock, fcntl.LOCK_UN)
    from steward.bg_baseline import _spawn_detached

    argv = [sys.executable, "-I", str(Path(__file__).resolve()), "--execute", "--repo", str(Path(repo).resolve()),
            "--candidate", snapshot["candidate_commit"], "--source-branch", source_branch, "--run-id", run_id]
    try:
        run_home = RUNTIME / run_id / "steward-home"
        run_scratch = RUNTIME / run_id / "tmp"
        pid = _spawn_detached(argv, env=isolated_test_environment(run_home, run_scratch))
    except OSError as exc:
        receipt.update(state="failed", error=f"background spawn failed: {type(exc).__name__}: {exc}",
                       finished_at=time.time())
        _write_receipt(RECEIPT, receipt)
        return receipt
    _set_pid_if_running(RECEIPT, pid)
    receipt = json.loads(RECEIPT.read_text())
    return receipt


def _source_current(repo: Path, source_branch: str, expected: str) -> bool:
    return _git(repo, "rev-parse", f"refs/heads/{source_branch}^{{commit}}", check=False) == expected


def _candidate_current(repo: Path, candidate_ref: str, expected: str) -> bool:
    if not candidate_ref:
        return _git(repo, "rev-parse", "HEAD", check=False) == expected
    return _git(repo, "rev-parse", f"refs/heads/{candidate_ref}^{{commit}}", check=False) == expected


def junit_case_to_nodeid(case: str) -> str | None:
    """Convert JUnit's `tests.test_x::TestC::test_y[p]` identity to a pytest node id."""
    parts = case.split("::")
    if len(parts) < 2:
        return None
    modules = []
    classes = []
    for part in parts[:-1]:
        for segment in part.split("."):
            if segment[:1].isupper():
                classes.append(segment)
            else:
                modules.append(segment)
    if not modules:
        return None
    return "/".join(modules) + ".py::" + "::".join([*classes, parts[-1]])


def attribute_failed_cases(candidate_tests: dict[str, str], baseline_tests: dict[str, str], *,
                           baseline_broken: str | None = None, exit_code: int = 0) -> dict:
    failures = sorted(case for case, result in candidate_tests.items() if result in ("failed", "error"))
    if exit_code == 0 and not failures:
        return {"new_failures": [], "preexisting_failures": [], "fixed": [], "unattributed_failures": [],
                "baseline_status": "not_needed_no_failures", "complete": True}
    new, old, unknown = [], [], []
    if exit_code != 0 and not failures:
        unknown.append("candidate exited nonzero without per-test failure details")
    for case in failures:
        if baseline_broken:
            unknown.append(case)
        elif baseline_tests.get(case) in ("failed", "error"):
            old.append(case)
        elif baseline_tests.get(case) == "passed":
            new.append(case)
        else:
            unknown.append(case)
    complete = not unknown and len(failures) > 0 and not baseline_broken
    return {"new_failures": new, "preexisting_failures": old, "fixed": [],
            "unattributed_failures": unknown,
            "baseline_status": "targeted_failed_tests_compared" if complete else "unavailable_not_compared",
            "complete": complete}


def compare_candidate_failures(store, job_id: str, repo: Path, source_commit: str, candidate_tests: dict[str, str],
                               candidate_exit: int, runtime: Path) -> tuple[dict, str | None]:
    """Re-run only candidate failures at the pinned source commit; never repeat the full suite."""
    failed_cases = sorted(case for case, result in candidate_tests.items() if result in ("failed", "error"))
    if not failed_cases:
        return attribute_failed_cases(candidate_tests, {}, exit_code=candidate_exit), None
    nodeids = [junit_case_to_nodeid(case) for case in failed_cases]
    if any(nodeid is None for nodeid in nodeids):
        return attribute_failed_cases(candidate_tests, {}, baseline_broken="unmapped JUnit case",
                                       exit_code=candidate_exit), None
    baseline_tree = runtime / "source-baseline"
    baseline_xml = runtime / "baseline-junit.xml"
    add = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "worktree", "add", "--detach",
                          str(baseline_tree), source_commit], cwd=repo, capture_output=True, text=True,
                         env=os.environ.copy())
    baseline_broken = None
    baseline_run_id = None
    baseline_tests = {}
    try:
        if add.returncode:
            baseline_broken = f"baseline worktree creation failed: {add.stderr.strip()[:300]}"
        else:
            from steward.check_runs import shared_check_fn
            from steward.runner_exec import SubprocessRunner

            command = "exec python3 -m pytest -q " + shlex.join(nodeids)
            check = shared_check_fn(store, job_id, purpose="final_baseline", wait=True, predicted_s=60,
                                    timeout_s=BASELINE_TIMEOUT_S)
            baseline_result = check(SubprocessRunner(), command, baseline_tree, baseline_xml)
            waiter = store._conn.execute(
                "SELECT c.run_id, c.state, c.exit_code, c.broken, c.tests_json FROM check_run_waiters w "
                "JOIN check_runs c ON c.run_id=w.run_id WHERE w.job_id=? AND w.purpose='final_baseline' "
                "ORDER BY w.attached_epoch DESC LIMIT 1", (job_id,)).fetchone()
            if waiter is None or waiter["state"] != "done" or not waiter["tests_json"]:
                baseline_broken = "baseline check has no durable completed check-run receipt"
            else:
                baseline_run_id = waiter["run_id"]
                baseline_tests = json.loads(waiter["tests_json"])
                if baseline_result.broken or waiter["broken"]:
                    baseline_broken = (baseline_result.broken or waiter["broken"])[:300]
                elif baseline_tests != baseline_result.tests or waiter["exit_code"] != baseline_result.exit_code:
                    baseline_broken = "baseline in-memory result does not match its durable check-run receipt"
    except Exception as exc:  # noqa: BLE001 - inability to compare is explicitly unattributed
        baseline_broken = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        if baseline_tree.exists():
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "worktree", "remove", "--force",
                            str(baseline_tree)], cwd=repo, capture_output=True, text=True, env=os.environ.copy())
    return attribute_failed_cases(candidate_tests, baseline_tests, baseline_broken=baseline_broken,
                                  exit_code=candidate_exit), baseline_run_id


def execute(repo: Path, candidate: str, source_branch: str, run_id: str) -> int:
    if not RECEIPT.is_file():
        return 2
    receipt = json.loads(RECEIPT.read_text())
    deadline = time.monotonic() + 10
    while receipt.get("pid") is None and receipt.get("state") == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        receipt = json.loads(RECEIPT.read_text())
    if (receipt.get("state") != "running" or receipt.get("pid") != os.getpid()
            or receipt.get("run_id") != run_id or receipt.get("candidate_commit") != candidate):
        return 2
    runtime = RUNTIME / run_id
    runtime.mkdir(parents=True, exist_ok=False)
    tree = runtime / "candidate"
    home = runtime / "steward-home"
    junit_tmp = runtime / "junit.xml"
    scratch = runtime / "tmp"
    scratch.mkdir()
    isolated_env = isolated_test_environment(home, scratch)
    isolated_env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    os.environ.clear()
    os.environ.update(isolated_env)
    start_epoch = time.time()
    result = None
    err = None
    store = None
    try:
        if not _source_current(repo, source_branch, receipt["source_head"]):
            raise CandidateError("source branch moved before the final run started")
        if not _candidate_current(repo, receipt.get("candidate_ref", ""), candidate):
            raise CandidateError("candidate branch moved before the final run started")
        add = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "worktree", "add", "--detach",
                              str(tree), candidate], cwd=repo, capture_output=True, text=True, env=isolated_env)
        if add.returncode:
            raise CandidateError(f"candidate worktree creation failed: {add.stderr.strip()}")
        if _git(tree, "status", "--porcelain"):
            raise CandidateError("isolated candidate checkout is dirty")

        # The test job and its check-run queue are isolated from credentials and user config.
        sys.path.insert(0, str(tree))
        from steward.check_runs import shared_check_fn
        from steward.runner_exec import SubprocessRunner
        from steward.store import Store

        store = Store(home)
        job = store.create_job("one-shot final candidate qualification", str(tree))
        check = shared_check_fn(store, job.job_id, purpose="final", wait=True, predicted_s=2400,
                                timeout_s=TIMEOUT_S)
        result = check(SubprocessRunner(), FULL_SUITE_COMMAND, tree, junit_tmp)
        row = store._conn.execute("SELECT run_id FROM check_run_waiters WHERE job_id=? AND purpose='final'",
                                  (job.job_id,)).fetchone()
        receipt["check_run_id"] = row["run_id"] if row else None
        receipt["tests"] = result.tests
        receipt["exit_code"] = result.exit_code
        receipt["summary"] = next((line.strip() for line in reversed(result.output.splitlines()) if line.strip()), "")
        if result.broken:
            receipt["attribution"] = {"baseline_commit": receipt["source_head"], "new_failures": [],
                                       "preexisting_failures": [], "fixed": [],
                                       "unattributed_failures": sorted(
                                           node for node, outcome in result.tests.items()
                                           if outcome in ("failed", "error")),
                                       "baseline_status": "candidate_check_broken", "complete": False}
        else:
            attribution, baseline_run_id = compare_candidate_failures(
                store, job.job_id, repo, receipt["source_head"], result.tests, result.exit_code, runtime)
            attribution["baseline_commit"] = receipt["source_head"]
            receipt["attribution"] = attribution
            receipt["baseline_check_run_id"] = baseline_run_id
        if result.broken and "timed out" in result.broken:
            receipt["state"] = "timeout"
        elif result.broken:
            receipt["state"] = "failed"
            receipt["error"] = result.broken[:500]
        else:
            receipt["state"] = "completed"
        receipt["used"] = result.used
        receipt["log_excerpt"] = result.output[-12000:]
        if junit_tmp.is_file():
            shutil.copyfile(junit_tmp, JUNIT)
        LOG.write_text(result.output[-12000:])
        if _git(tree, "rev-parse", "HEAD") != candidate or _git(tree, "status", "--porcelain"):
            receipt["state"] = "candidate_changed"
        if not _source_current(repo, source_branch, receipt["source_head"]):
            receipt["state"] = "source_moved"
        if not _candidate_current(repo, receipt.get("candidate_ref", ""), candidate):
            receipt["state"] = "candidate_changed"
    except Exception as exc:  # noqa: BLE001 - every interruption is a durable non-pass
        err = f"{type(exc).__name__}: {exc}"[:500]
        receipt["state"] = "failed"
        receipt["error"] = err
    finally:
        if store is not None:
            store.close()
        subprocess.run(["git", "worktree", "remove", "--force", str(tree)], cwd=repo,
                       capture_output=True, text=True)
        receipt["finished_at"] = time.time()
        receipt["duration_s"] = receipt["finished_at"] - start_epoch
        _write_receipt(RECEIPT, receipt)
    return 0 if receipt["state"] == "completed" and receipt.get("exit_code") == 0 else 1


def status() -> int:
    if not RECEIPT.is_file():
        print("no final-run receipt")
        return 1
    from steward.bg_baseline import _alive

    with open(RECEIPT.with_suffix(".lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rec = json.loads(RECEIPT.read_text())
        if rec.get("state") == "running" and not _alive(rec.get("pid")):
            rec["state"] = "lost"
            rec["error"] = "background final-candidate process disappeared; this is not passing evidence"
            rec["finished_at"] = time.time()
            tmp = RECEIPT.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
            os.replace(tmp, RECEIPT)
        fcntl.flock(lock, fcntl.LOCK_UN)
    print(json.dumps({k: rec.get(k) for k in ("state", "candidate_commit", "source_head", "pid", "exit_code",
                                              "summary", "error")}, sort_keys=True))
    att = rec.get("attribution") or {}
    passed = (rec.get("state") == "completed" and rec.get("exit_code") == 0
              and att.get("complete") is True and not att.get("unattributed_failures")
              and not att.get("new_failures"))
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--candidate")
    parser.add_argument("--source-branch", default="fleet-registry")
    parser.add_argument("--start", action="store_true", help="validate candidate and launch the one background run")
    parser.add_argument("--status", action="store_true", help="show durable receipt status; never starts work")
    parser.add_argument("--execute", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-id", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.status:
            return status()
        if args.execute:
            if not args.repo or not args.candidate or not args.run_id:
                return 2
            return execute(args.repo, args.candidate, args.source_branch, args.run_id)
        if args.start:
            if not args.repo or not args.candidate:
                parser.error("--start requires --repo and --candidate")
            receipt = start(args.repo, args.candidate, args.source_branch)
            print(json.dumps(receipt, sort_keys=True))
            return 0 if receipt.get("state") == "running" else 1
        parser.print_help()
        return 2
    except CandidateError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
