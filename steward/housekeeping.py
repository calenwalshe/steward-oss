"""Housekeeping: reclaim the disk that finished jobs' worktrees hold (Issue H).

A daily sweep, run from the runner tick (`maybe_sweep`, rate-limited by a kv timestamp) and on demand by
`steward housekeeping [--dry-run]`. For jobs in a terminal state it removes the worktree directory
(`git worktree remove`, then `git worktree prune`):

  * merged/incorporated jobs, a day after they were merged;
  * ACCEPTED-but-not-merged, STOPPED, RELEASED and FAILED jobs, a week after their terminal state.

Nothing is lost on the way: a worktree with uncommitted or untracked changes first gets them committed to the job's
`steward/<job_id>` branch ("steward: archive uncommitted work"). The branch of a job that was not merged is never
deleted (the owner may cherry-pick from it); a merged job's branch goes after 30 days, and only when git confirms it
is fully merged into the repo's default branch. A worktree is only touched when it is `<worktrees>/<job_id>` for a
terminal job of this store AND git lists it as a worktree of the job's repo on branch `steward/<job_id>`: live jobs
and directories Steward did not create are left alone. Every removal and branch deletion records an event.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from steward.models import JobState, Judgment
from steward.runner_exec import Runner, SubprocessRunner
from steward.store import Store

log = logging.getLogger("steward.housekeeping")

DAY_S = 86400.0
MERGED_WORKTREE_AGE_S = 1 * DAY_S
UNMERGED_WORKTREE_AGE_S = 7 * DAY_S
MERGED_BRANCH_AGE_S = 30 * DAY_S
SWEEP_INTERVAL_S = DAY_S

TERMINAL = frozenset({JobState.ACCEPTED, JobState.STOPPED, JobState.RELEASED, JobState.FAILED})
ARCHIVE_MESSAGE = "steward: archive uncommitted work"

# The daily-sweep timestamp lives under a sentinel job id (the kv table is keyed by job).
KV_JOB = "_housekeeping"
KV_KEY = "worktree_sweep_last_ts"


@dataclass
class Action:
    job_id: str
    kind: str  # "worktree" | "branch"
    path: str = ""
    branch: str = ""
    bytes: int = 0
    archived: bool = False  # uncommitted work was (or, in a dry run, would be) committed first
    done: bool = False


@dataclass
class Report:
    dry_run: bool
    actions: list[Action] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def worktrees(self) -> list[Action]:
        return [a for a in self.actions if a.kind == "worktree"]

    @property
    def branches(self) -> list[Action]:
        return [a for a in self.actions if a.kind == "branch"]

    @property
    def freed_bytes(self) -> int:
        return sum(a.bytes for a in self.worktrees if a.done or self.dry_run)

    def summary(self) -> str:
        verb = "would remove" if self.dry_run else "removed"
        bverb = "would delete" if self.dry_run else "deleted"
        archived = sum(1 for a in self.worktrees if a.archived)
        return (f"housekeeping: {verb} {len(self.worktrees)} worktrees ({self.freed_bytes / 1e9:.2f} GB freed), "
                f"{archived} archived, {bverb} {len(self.branches)} merged branches, {len(self.errors)} errors")


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _dir_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _git(runner: Runner, cwd: Path | str, *args: str):
    return runner.run(["git", *args], cwd=cwd)


def _default_branch(runner: Runner, repo: Path) -> str | None:
    """The branch Steward merges into: a local main/master, else origin's HEAD, else the checked-out branch."""
    for name in ("main", "master"):
        if _git(runner, repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").exit_code == 0:
            return name
    res = _git(runner, repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if res.exit_code == 0 and res.stdout.strip():
        return res.stdout.strip()
    res = _git(runner, repo, "symbolic-ref", "--short", "HEAD")
    return (res.stdout.strip() or None) if res.exit_code == 0 else None


def _registered_worktrees(runner: Runner, repo: Path) -> dict[str, str]:
    """{realpath of worktree: branch ref} as git lists them for `repo`."""
    res = _git(runner, repo, "worktree", "list", "--porcelain")
    out: dict[str, str] = {}
    if res.exit_code != 0:
        return out
    path = None
    for line in res.stdout.splitlines():
        if line.startswith("worktree "):
            path = os.path.realpath(line[len("worktree "):])
            out[path] = ""
        elif line.startswith("branch ") and path:
            out[path] = line[len("branch "):].strip()
    return out


def _merged_at(store: Store, job_id: str, fallback: float) -> float | None:
    """When the job was merged (None = it was not)."""
    if not store.kv_get(job_id, "incorporated_commit"):
        return None
    evs = store.events_of(job_id, ("incorporated", "incorporated_via_followup"))
    return _ts(evs[-1].ts) if evs else fallback


def sweep(store: Store, *, dry_run: bool = False, now: float | None = None, base_dir: Path | str | None = None,
          runner: Runner | None = None) -> Report:
    """One housekeeping pass over this store's job worktrees. Never raises for one job's failure."""
    now = time.time() if now is None else now
    runner = runner or SubprocessRunner()
    base = Path(base_dir) if base_dir is not None else store.home / "worktrees"
    report = Report(dry_run=dry_run)
    repos: dict[str, tuple[dict[str, str], str | None]] = {}
    pruned: set[str] = set()

    for state in TERMINAL:
        for job in store.list_jobs({state}):
            try:
                _sweep_job(store, job, base, runner, now, dry_run, report, repos, pruned)
            except Exception as exc:  # noqa: BLE001 - housekeeping never stops on one job
                report.errors.append(f"{job.job_id}: {type(exc).__name__}: {exc}"[:300])
    for repo in pruned:
        _git(runner, repo, "worktree", "prune")
    return report


def _sweep_job(store, job, base: Path, runner: Runner, now: float, dry_run: bool, report: Report,
               repos: dict, pruned: set) -> None:
    job_id = job.job_id
    terminal_at = _ts(job.updated_at)
    merged_at = _merged_at(store, job_id, terminal_at)
    merged = merged_at is not None
    repo = Path(job.repo)
    if not repo.is_dir():
        return
    key = str(repo)
    if key not in repos:
        repos[key] = (_registered_worktrees(runner, repo), _default_branch(runner, repo))
    registered, default = repos[key]
    branch = f"steward/{job_id}"
    wt = base / job_id

    if wt.is_dir() and not wt.is_symlink():
        age = now - (merged_at if merged else terminal_at)
        limit = MERGED_WORKTREE_AGE_S if merged else UNMERGED_WORKTREE_AGE_S
        # Ours only: git lists it as a worktree of the job's repo, on the job's own branch.
        if age >= limit and registered.get(os.path.realpath(wt)) == f"refs/heads/{branch}":
            _remove_worktree(store, job_id, wt, branch, repo, runner, dry_run, report, pruned, merged)

    if merged and now - merged_at >= MERGED_BRANCH_AGE_S and default and not wt.exists():
        has = _git(runner, repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").exit_code == 0
        if has and _git(runner, repo, "merge-base", "--is-ancestor", branch, default).exit_code == 0:
            act = Action(job_id, "branch", branch=branch)
            report.actions.append(act)
            if not dry_run:
                res = _git(runner, repo, "branch", "-D", branch)
                if res.exit_code != 0:
                    report.errors.append(f"{job_id}: branch delete failed: {res.stderr.strip()[:200]}")
                    report.actions.remove(act)
                    return
                act.done = True
                store.append_event(job_id, what="branch_deleted", evidence={"branch": branch, "into": default},
                                   rule="housekeeping_merged_branch", judgment=Judgment.code)


def _remove_worktree(store, job_id, wt: Path, branch: str, repo: Path, runner: Runner, dry_run: bool,
                     report: Report, pruned: set, merged: bool) -> None:
    status = _git(runner, wt, "status", "--porcelain")
    if status.exit_code != 0:
        report.errors.append(f"{job_id}: git status failed, left in place: {status.stderr.strip()[:200]}")
        return
    act = Action(job_id, "worktree", path=str(wt), branch=branch, bytes=_dir_bytes(wt),
                 archived=bool(status.stdout.strip()))
    if dry_run:
        report.actions.append(act)
        return
    if act.archived:
        add = _git(runner, wt, "add", "-A")
        commit = _git(runner, wt, "-c", "user.name=Steward", "-c", "user.email=steward@localhost", "commit",
                      "--no-verify", "-m", ARCHIVE_MESSAGE) if add.exit_code == 0 else add
        if commit.exit_code != 0:  # never remove what we could not save
            report.errors.append(f"{job_id}: archive commit failed, left in place: "
                                 f"{(commit.stderr or commit.stdout).strip()[:200]}")
            return
    res = _git(runner, repo, "worktree", "remove", "--force", str(wt))
    if res.exit_code != 0:
        report.errors.append(f"{job_id}: worktree remove failed: {res.stderr.strip()[:200]}")
        return
    act.done = True
    report.actions.append(act)
    pruned.add(str(repo))
    store.append_event(job_id, what="worktree_removed",
                       evidence={"path": str(wt), "branch": branch, "bytes": act.bytes,
                                 "archived_uncommitted": act.archived, "merged": merged},
                       rule="housekeeping_terminal_worktree", judgment=Judgment.code)


def maybe_sweep(store: Store, *, now: float | None = None, **kw) -> Report | None:
    """The runner-tick entry: at most one sweep per day (kv timestamp, claimed before the sweep so a slow or failing
    pass is not retried on every tick). Returns the report, or None when it is not yet due. Never raises."""
    if os.environ.get("STEWARD_HOUSEKEEPING", "1") == "0":
        return None
    now = time.time() if now is None else now
    try:
        last = store.kv_get(KV_JOB, KV_KEY, default=0) or 0
        if now - float(last) < SWEEP_INTERVAL_S:
            return None
        store.kv_set(KV_JOB, KV_KEY, now)
        report = sweep(store, now=now, **kw)
        log.info(report.summary())
        return report
    except Exception as exc:  # noqa: BLE001 - housekeeping must never break the tick
        log.warning("housekeeping failed: %s: %s", type(exc).__name__, exc)
        return None
