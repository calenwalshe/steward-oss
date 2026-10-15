"""Stable source branch identity for a job and its queued/follow-up chain."""

from __future__ import annotations

import subprocess
from pathlib import Path

from steward.models import Judgment

KEY = "chain_target_branch"


def checked_out_branch(repo: str | Path) -> str | None:
    try:
        res = subprocess.run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=repo,
                             capture_output=True, text=True)
    except OSError:  # `new` still creates a contracted job when discovery cannot enter the selected repo
        return None
    return res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else None


def target(store, job_id: str) -> str | None:
    """Return a job's recorded target, inheriting through older links as a migration fallback."""
    seen = set()
    cur = job_id
    original = store.get_job(job_id)
    while cur and cur not in seen:
        seen.add(cur)
        ancestor = store.get_job(cur)
        if original is None or ancestor is None or ancestor.repo != original.repo:
            return None
        branch = store.kv_get(cur, KEY)
        if branch:
            return str(branch)
        cur = (store.kv_get(cur, "parent_job") or store.kv_get(cur, "autopilot_followup")
               or store.kv_get(cur, "seq_after"))
    return None


def record(store, job_id: str, branch: str | None) -> None:
    if branch:
        store.kv_set(job_id, KEY, branch)


def align_staged_worktree(store, job, runner) -> str | None:
    """Fast-forward a not-yet-launched chain member to its pinned target, or explain why it is unsafe."""
    if store.kv_get(job.job_id, "parent_job") or store.kv_get(job.job_id, "autopilot_followup"):
        return None  # follow-ups deliberately start from their parent's work branch, including its unmerged work
    branch = target(store, job.job_id)
    if not branch:
        return None
    from steward import exceptions

    repo = Path(job.repo)
    wt = Path(store.home) / "worktrees" / job.job_id
    if not wt.is_dir():
        return None  # discovery may have failed; the normal launch path will report that separately
    exceptions.use(store, job.job_id, "git", {"action": "align_chain_worktree", "repo": str(repo)})
    status = runner.run(["git", "status", "--porcelain"], cwd=wt)
    head = runner.run(["git", "rev-parse", "HEAD"], cwd=wt)
    target_head = runner.run(["git", "rev-parse", "--verify", f"refs/heads/{branch}"], cwd=repo)
    if status.exit_code or head.exit_code or target_head.exit_code:
        return f"cannot verify chain worktree base against target {branch}"
    old, new = head.stdout.strip(), target_head.stdout.strip()
    if old == new:
        return None
    if status.stdout.strip():
        return f"chain worktree has local changes and cannot be aligned to target {branch}"
    ancestry = runner.run(["git", "merge-base", "--is-ancestor", old, new], cwd=repo)
    if ancestry.exit_code != 0:
        return f"chain worktree base {old[:12]} diverged from target {branch} at {new[:12]}"
    reset = runner.run(["git", "reset", "--hard", new], cwd=wt)
    if reset.exit_code != 0:
        return f"could not align chain worktree to target {branch}: {reset.stderr.strip()[:200]}"
    store.append_event(job.job_id, what="chain_worktree_aligned", evidence={"target_branch": branch,
                       "old_head": old, "new_head": new}, rule="chain_branch_inheritance", judgment=Judgment.code)
    return None
