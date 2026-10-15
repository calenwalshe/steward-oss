"""Yield classification (v2.7 Phase 40, SRD-01/02): a worker turn end is a yield, not a completion.

`classify` is pure over the store, git (an injected runner) and the frame's facts. Exactly one lane:

  WAITING    a registered wait is open (or, later, live children): park, no model call
  ASKING     an open blocking question is queued for the owner
  CANDIDATE  every completion precondition holds: no tracked changes, a new commit, not already ruled at that SHA,
             no unacknowledged steering. Only this lane reaches WORKER_FINISHED.
  STOPPED    anything else, with the failed preconditions named. Uncertainty (git error, unreadable store)
             is STOPPED, never CANDIDATE.

Untracked files never make the tree dirty (RC-4, 2026-10-11): verification runs at the exact committed SHA, so a
forgotten untracked source file fails there; scratch (logs, temp profiles) must not block a done. They are listed,
capped, as evidence instead.

Never calls a model and never writes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

WAITING, ASKING, CANDIDATE, STOPPED = "WAITING", "ASKING", "CANDIDATE", "STOPPED"
LANES = (WAITING, ASKING, CANDIDATE, STOPPED)
LAST_RULED_KEY = "last_ruled_sha"
STOP_LABELS = ("asking", "waiting_external", "claims_done", "stuck", "errored")
UNTRACKED_CAP = 20


@dataclass
class Classification:
    lane: str
    sha: str | None = None
    failed_preconditions: list[str] = field(default_factory=list)
    label: str | None = None  # a label code already knows (claims_done / errored); else Jev is asked
    untracked: list[str] = field(default_factory=list)  # evidence only, capped at UNTRACKED_CAP; never a precondition


def _git(run: Callable, argv: list[str], cwd) -> str | None:
    try:
        res = run(argv, cwd=cwd)
    except Exception:  # noqa: BLE001 - git trouble is uncertainty -> STOPPED
        return None
    return res.stdout if res.exit_code == 0 else None


def classify(store, job_id: str, *, run: Callable, worktree, start_sha: str | None, unacked=(),
             errored: bool = False) -> Classification:
    failed: list[str] = []
    sha: str | None = None
    try:
        from steward import questions, waits

        waiting = bool(waits.waiting(store, job_id))
        blocking = questions.summary(questions.load(store, job_id))["blocking"] > 0
        last_ruled = store.kv_get(job_id, LAST_RULED_KEY)
    except Exception:  # noqa: BLE001 - an unreadable store can never produce a CANDIDATE
        return Classification(STOPPED, None, ["store_unreadable"])
    head = _git(run, ["git", "rev-parse", "HEAD"], worktree)
    sha = head.strip() if head and head.strip() else None
    if errored:
        return Classification(STOPPED, sha, ["errored"], label="errored")
    if waiting:
        return Classification(WAITING, sha, [])
    if blocking:
        return Classification(ASKING, sha, [])
    status = _git(run, ["git", "status", "--porcelain", "--untracked-files=no"], worktree)
    listing = _git(run, ["git", "status", "--porcelain", "--untracked-files=all"], worktree) or ""
    untracked = [line[3:] for line in listing.splitlines() if line.startswith("?? ")][:UNTRACKED_CAP]
    if sha is None or status is None:
        failed.append("git_error")
    else:
        if status.strip():
            failed.append("dirty_worktree")
        if not start_sha:
            failed.append("start_sha_unknown")
        elif sha == start_sha:
            failed.append("no_new_commit")
        if last_ruled and sha == last_ruled:
            failed.append("same_as_last_ruled")
    if unacked:
        failed.append("unacked_steering")
    if not failed:
        return Classification(CANDIDATE, sha, [], untracked=untracked)
    label = "claims_done" if failed == ["same_as_last_ruled"] else None
    return Classification(STOPPED, sha, failed, label=label, untracked=untracked)
