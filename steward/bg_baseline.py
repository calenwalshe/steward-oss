"""Background baseline (latency #5, docs/diagnostics/2026-10-02-latency).

`steward new` used to run the baseline checks synchronously before the job even had a contract: 13 of 48 jobs with
checks waited ~290 s each for it (0.94 h in 3.9 days). Now `new` pins the base sha, records `baseline_bg` as
running, and returns at once (dispatch is immediate). The baseline runs at the pinned sha in an isolated temp
worktree, never the repo's own working copy, one at a time per repo, at low priority, in a detached
`python3 -m steward.bg_baseline JOB` process. Verification is the only thing that needs it (to attribute failures as new or
pre-existing), so `wait()` blocks there, and only while the baseline is genuinely still running.

`STEWARD_BG_BASELINE=0` restores the synchronous baseline.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from steward import gitenv
from steward.models import Judgment
from steward.store import Store

KEY = "baseline_bg"
ENV = "STEWARD_BG_BASELINE"
WAIT_S = 900.0      # the old cap on the wait; a baseline whose pid is alive is now waited for up to STALE_S
STALE_S = 3600.0    # a "running" record older than this is a dead run (its process is gone)
POLL_S = 2.0
PROGRESS_S = 300.0  # while verification waits, one progress event this often


def enabled(env=None) -> bool:
    return (os.environ if env is None else env).get(ENV, "1").strip().lower() not in ("0", "off", "false", "no")


def pin_sha(repo: Path | str, start: str | None, runner) -> str:
    """The commit the job's worktree starts from: the follow-up start point, else the repo HEAD."""
    res = runner.run(["git", "rev-parse", f"{start or 'HEAD'}^{{commit}}"], cwd=Path(repo))
    return res.stdout.strip() if res.exit_code == 0 else ""


def start(store: Store, job_id: str, repo: Path | str, sha: str, *, spawn: Callable[[list[str]], object] | None = None,
          now: float | None = None) -> dict:
    """Record the baseline as running at `sha` and launch it detached. Returns immediately."""
    record = {"status": "running", "sha": sha, "repo": str(repo), "started_at": time.time() if now is None else now}
    store.kv_set(job_id, KEY, record)
    store.append_event(job_id, what="baseline started in background", evidence={"commit_sha": sha},
                       rule="background_baseline", judgment=Judgment.code)
    from steward import pkgenv

    argv = [*pkgenv.cli_argv()[:-1], "steward.bg_baseline", job_id]  # `<python> -m steward.bg_baseline JOB`
    try:
        pid = (spawn or _spawn_detached)(argv)
    except OSError as exc:
        finish(store, job_id, status="failed", error=f"spawn failed: {exc}")
        return record
    if isinstance(pid, int):
        record["pid"] = pid  # `running` treats a dead process as a dead run, so verify never waits on it
        store.kv_set(job_id, KEY, record)
    return record


def _spawn_detached(argv: list[str], *, env: dict[str, str] | None = None) -> int:
    """Low priority (`nice`), its own session: it outlives the control pane call that started it. Runs from the
    directory that holds the `steward` package so `-m steward.bg_baseline` imports whatever the caller's cwd."""
    nice = shutil.which("nice", path=(env or {}).get("PATH")) if env is not None else shutil.which("nice")
    proc = subprocess.Popen([nice, "-n", "10", *argv] if nice else argv, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                            cwd=str(Path(__file__).resolve().parents[1]), env=gitenv.merged(env))
    return proc.pid


def _alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return True  # unknown pid: rely on the staleness bound
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie (finished child of this very process) is not running
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(") ", 1)[-1][:1] != "Z"
    except OSError:
        return True


def finish(store: Store, job_id: str, *, status: str, error: str | None = None) -> None:
    record = dict(store.kv_get(job_id, KEY) or {})
    record.update(status=status, finished_at=time.time(), **({"error": error[:300]} if error else {}))
    store.kv_set(job_id, KEY, record)
    if status != "done":
        store.append_event(job_id, what="baseline failed in background", evidence={"error": (error or "")[:300]},
                           rule="background_baseline", judgment=Judgment.code)


def run(store: Store, job_id: str, runner, *, discover_fn=None, run_baseline_fn=None,
        lock_dir: Path | None = None, check_fn=None) -> str:
    """The detached process body: one baseline per repo at a time, at the pinned sha, in a temp worktree."""
    from steward.baseline import run_baseline
    from steward.discovery import discover

    record = store.kv_get(job_id, KEY) or {}
    repo, sha = Path(record.get("repo") or ""), record.get("sha") or ""
    if not sha or not repo.is_dir():
        finish(store, job_id, status="failed", error="no pinned sha or repo")
        return "failed"
    lock_dir = lock_dir or (store.home / "locks")
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ("baseline-" + hashlib.sha1(str(repo.resolve()).encode()).hexdigest()[:12] + ".lock")
    tmp = Path(tempfile.mkdtemp(prefix="steward-baseline-"))
    tree = tmp / "tree"
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            add = runner.run(["git", "worktree", "add", "--detach", str(tree), sha], cwd=repo)
            if add.exit_code != 0:
                finish(store, job_id, status="failed", error=f"temp worktree failed: {add.stderr or add.stdout}")
                return "failed"
            discovery = (discover_fn or discover)(tree)
            from steward import check_runs
            (run_baseline_fn or run_baseline)(store, job_id, tree, discovery, runner,
                                              check_fn=check_fn or check_runs.shared_check_fn(
                                                  store, job_id, purpose="baseline"))
            finish(store, job_id, status="done")
            _reattribute(store, job_id)
            return "done"
        except Exception as exc:  # noqa: BLE001 - a broken baseline is recorded; verification proceeds without it
            finish(store, job_id, status="failed", error=f"{type(exc).__name__}: {exc}")
            return "failed"
        finally:
            runner.run(["git", "worktree", "remove", "--force", str(tree)], cwd=repo)
            shutil.rmtree(tmp, ignore_errors=True)
            fcntl.flock(fh, fcntl.LOCK_UN)


def _reattribute(store: Store, job_id: str) -> None:
    """A verification that ran while this baseline was still going parked the job: re-attribute it now (stored junit,
    no second pytest run). Best effort: a failure here leaves the job parked, which is what it already was."""
    try:
        from steward import closeout
        from steward.contract import load_contract

        closeout.reattribute_after_baseline(store, job_id, load_contract(store, job_id))
    except Exception:  # noqa: BLE001
        pass


def running(store: Store, job_id: str, now: float | None = None) -> bool:
    record = store.kv_get(job_id, KEY)
    if not isinstance(record, dict) or record.get("status") != "running":
        return False
    now = time.time() if now is None else now
    return now - float(record.get("started_at") or 0) < STALE_S and _alive(record.get("pid"))


def wait(store: Store, job_id: str, *, timeout_s: float = STALE_S, sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.time) -> str:
    """Verification needs the baseline for attribution: block only while it is genuinely running.
    Returns "none" (no background baseline), "done", "failed", "stale" or "timeout"."""
    record = store.kv_get(job_id, KEY)
    if not isinstance(record, dict):
        return "none"
    start_t = clock()
    waited = False
    next_progress = start_t + PROGRESS_S
    while running(store, job_id, now=clock()):
        if clock() - start_t >= timeout_s:
            store.append_event(job_id, what="baseline wait timed out", evidence={"waited_s": round(clock() - start_t)},
                               rule="background_baseline", judgment=Judgment.code)
            return "timeout"
        waited = True
        if clock() >= next_progress:
            store.append_event(job_id, what="verification waiting for the background baseline",
                               evidence={"waited_s": round(clock() - start_t)}, rule="background_baseline",
                               judgment=Judgment.code)
            next_progress += PROGRESS_S
        sleep(POLL_S)
    status = (store.kv_get(job_id, KEY) or {}).get("status", "none")
    if status == "running":
        return "stale"
    if waited:
        store.append_event(job_id, what="verification waited for the background baseline",
                           evidence={"waited_s": round(clock() - start_t, 1), "status": status},
                           rule="background_baseline", judgment=Judgment.code)
    return status


def main(argv: list[str] | None = None) -> int:
    """`python3 -m steward.bg_baseline JOB`: the detached process `start` launches."""
    import sys

    from steward.runner_exec import SubprocessRunner

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python3 -m steward.bg_baseline JOB", file=sys.stderr)
        return 2
    return 0 if run(Store(), args[0], SubprocessRunner()) == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
