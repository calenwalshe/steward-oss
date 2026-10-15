"""Background candidate verification (v2.6, SUP-02; the bg_baseline pattern, keyed by the candidate SHA).

Job 14305aeb's suite ran ~30 minutes. Verifying a candidate inline would hold the runner tick for that long, so
when the predicted verification time (the baseline's measured duration, else the last inline verification's)
exceeds `INLINE_BUDGET_S`, closeout starts `python3 -m steward.bg_verify JOB` detached and the tick returns at once
(`WORKER_FINISHED`, "verifying in background"). The detached run verifies the exact candidate SHA in an isolated
temp worktree; a later tick consumes the result and closeout proceeds unchanged (new failures go back to the
worker with the first failing test named). A result whose SHA no longer matches the worktree HEAD is discarded and
re-run; a crashed run is `unverified` and goes to NEEDS_HUMAN with the reason, never a silent pass. A LOST run (the
process vanished, or its shared check run was lost) is re-queued by the runner up to `runner.VERIFY_REQUEUE_MAX`
times before the ladder's ask (zero-touch §5.5) — still never a pass.

`STEWARD_BG_VERIFY=0` keeps verification inline.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from steward import bg_baseline
from steward.baseline import CheckResult
from steward.models import Judgment
from steward.store import Store
from steward.verify import Verification

KEY = "verify_bg"
DURATION_KEY = "verify_duration_s"     # the last inline verification's wall time (a prediction for the next)
BASELINE_DURATION_KEY = "baseline_duration_s"  # a synchronous baseline's wall time (steward.baseline)
ENV = "STEWARD_BG_VERIFY"
INLINE_BUDGET_S = 120.0  # the finish-check budget (stability.FINISH_TIMEOUT_S): longer than this never runs inline
STALE_S = 4 * 3600.0     # a "running" record older than this is a dead run
VANISHED = "background verification process vanished"
LOST_PREFIX = "Lost:"    # `run` records a `check_runs.Lost` as "Lost: check run <id> lost: <why>"


def enabled(env=None) -> bool:
    return (os.environ if env is None else env).get(ENV, "1").strip().lower() not in ("0", "off", "false", "no")


def predicted_s(store: Store, job_id: str) -> float | None:
    """How long verifying a candidate is expected to take: the longest measured baseline or verification."""
    seen: list[float] = []
    rec = store.kv_get(job_id, bg_baseline.KEY)
    if isinstance(rec, dict) and isinstance(rec.get("duration_s"), (int, float)):
        seen.append(float(rec["duration_s"]))
    for key in (BASELINE_DURATION_KEY, DURATION_KEY):
        val = store.kv_get(job_id, key)
        if isinstance(val, (int, float)):
            seen.append(float(val))
    return max(seen) if seen else None


def needed(store: Store, job_id: str, *, budget_s: float = INLINE_BUDGET_S, env=None) -> bool:
    if not enabled(env):
        return False
    pred = predicted_s(store, job_id)
    return pred is not None and pred > budget_s


def record(store: Store, job_id: str) -> dict | None:
    rec = store.kv_get(job_id, KEY)
    return rec if isinstance(rec, dict) else None


def pending(store: Store, job_id: str) -> bool:
    """A background verification was started and its outcome has not been consumed by closeout yet."""
    rec = record(store, job_id)
    return bool(rec) and rec.get("status") in ("running", "done", "failed")


def to_dict(v: Verification) -> dict:
    return asdict(v)


def from_dict(d: dict) -> Verification:
    d = dict(d)
    d["results"] = [CheckResult(**r) for r in d.get("results") or []]
    return Verification(**d)


def start(store: Store, job_id: str, worktree: Path | str, sha: str, *,
          spawn: Callable[[list[str]], object] | None = None, now: float | None = None) -> dict:
    """Record the verification as running at `sha` and launch it detached. Returns immediately."""
    rec = {"status": "running", "sha": sha, "worktree": str(worktree), "started_at": time.time() if now is None else now}
    store.kv_set(job_id, KEY, rec)
    store.append_event(job_id, what="verification started in background",
                       evidence={"commit_sha": sha, "predicted_s": predicted_s(store, job_id),
                                 "budget_s": INLINE_BUDGET_S},
                       rule="background_verification", judgment=Judgment.code)
    from steward import pkgenv

    argv = [*pkgenv.cli_argv()[:-1], "steward.bg_verify", job_id]  # `<python> -m steward.bg_verify JOB`
    try:
        pid = (spawn or bg_baseline._spawn_detached)(argv)
    except OSError as exc:
        finish(store, job_id, status="failed", error=f"spawn failed: {exc}")
        return rec
    if isinstance(pid, int):
        rec["pid"] = pid
        store.kv_set(job_id, KEY, rec)
    return rec


def finish(store: Store, job_id: str, *, status: str, verification: Verification | None = None,
           error: str | None = None, sha: str | None = None) -> None:
    rec = dict(record(store, job_id) or {})
    if sha is not None and (rec.get("sha") != sha or rec.get("status") == "consumed"):
        return  # a newer candidate's run owns the record now; this (superseded) run's outcome is moot
    rec.update(status=status, finished_at=time.time())
    if verification is not None:
        rec["result"] = to_dict(verification)
    if error:
        rec["error"] = error[:300]
    store.kv_set(job_id, KEY, rec)


def run(store: Store, job_id: str, runner, *, discover_fn=None, verify_fn=None, check_fn=None) -> str:
    """The detached process body: verify the exact candidate SHA in an isolated temp worktree."""
    from steward.discovery import discover
    from steward.verify import verify

    rec = record(store, job_id) or {}
    sha, worktree = rec.get("sha") or "", Path(rec.get("worktree") or "")
    if not sha:
        finish(store, job_id, status="failed", error="no candidate sha recorded")
        return "failed"
    tmp = Path(tempfile.mkdtemp(prefix="steward-verify-"))
    tree = tmp / "tree"
    t0 = time.monotonic()
    try:
        add = runner.run(["git", "worktree", "add", "--detach", str(tree), sha], cwd=worktree)
        if add.exit_code != 0:
            finish(store, job_id, status="failed", error=f"temp worktree failed: {add.stderr or add.stdout}", sha=sha)
            return "failed"
        discovery = (discover_fn or discover)(tree)
        if verify_fn is not None:
            verification = verify_fn(store, job_id, tree, discovery, runner)
        else:
            from steward import check_runs
            verification = verify(store, job_id, tree, discovery, runner,
                                  check_fn=check_fn or check_runs.shared_check_fn(
                                      store, job_id, purpose="verify", predicted_s=predicted_s(store, job_id)))
        store.kv_set(job_id, DURATION_KEY, round(time.monotonic() - t0, 1))
        finish(store, job_id, status="done", verification=verification, sha=sha)
        return "done"
    except Exception as exc:  # noqa: BLE001 - a crashed run is recorded and becomes `unverified`, never a pass
        finish(store, job_id, status="failed", error=f"{type(exc).__name__}: {exc}", sha=sha)
        return "failed"
    finally:
        runner.run(["git", "worktree", "remove", "--force", str(tree)], cwd=worktree)
        shutil.rmtree(tmp, ignore_errors=True)


def poll(store: Store, job_id: str, head_sha: str, now: float | None = None) -> tuple[str, Verification | None, str]:
    """(status, verification, reason): `running`, `done` (verification at `head_sha`), `stale` (the candidate
    moved: discard and re-run), `lost` (the process vanished or its check run was lost: re-queue), `crashed` (the run
    failed: unverified), or `none`."""
    rec = record(store, job_id)
    if not rec or rec.get("status") not in ("running", "done", "failed"):
        return "none", None, ""
    now = time.time() if now is None else now
    if rec["status"] == "failed":
        error = str(rec.get("error") or "background verification failed")
        if head_sha and rec.get("sha") and head_sha != rec.get("sha"):  # a moved candidate's failure is moot
            return "stale", None, f"candidate moved {str(rec.get('sha'))[:12]} -> {head_sha[:12]} ({error[:120]})"
        if error.startswith(LOST_PREFIX):
            return "lost", None, error
        return "crashed", None, error
    if rec["status"] == "running":
        alive = now - float(rec.get("started_at") or 0) < STALE_S and bg_baseline._alive(rec.get("pid"))
        if not alive:
            return "lost", None, f"{VANISHED} before recording a result"
        if head_sha and head_sha != rec.get("sha"):
            return "stale", None, f"candidate moved {str(rec.get('sha'))[:12]} -> {head_sha[:12]}"
        return "running", None, ""
    if head_sha != rec.get("sha"):
        return "stale", None, f"candidate moved {str(rec.get('sha'))[:12]} -> {head_sha[:12]}"
    try:
        return "done", from_dict(rec.get("result") or {"commit_sha": rec.get("sha")}), ""
    except (TypeError, ValueError) as exc:
        return "crashed", None, f"background result unreadable: {exc}"


def consume(store: Store, job_id: str, outcome: str) -> None:
    rec = dict(record(store, job_id) or {})
    rec.update(status="consumed", outcome=outcome, consumed_at=time.time())
    store.kv_set(job_id, KEY, rec)


def main(argv: list[str] | None = None) -> int:
    """`python3 -m steward.bg_verify JOB`: the detached process `start` launches."""
    import sys

    from steward.runner_exec import SubprocessRunner

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python3 -m steward.bg_verify JOB", file=sys.stderr)
        return 2
    return 0 if run(Store(), args[0], SubprocessRunner()) == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
