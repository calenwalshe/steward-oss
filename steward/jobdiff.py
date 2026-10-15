"""The job's OWN change: the one place that decides what range "what did this job change" means.

`baseline..HEAD` also counts every upstream commit the job branch merged in, so notices read "35 files" for a
6-file change. Once incorporated the job's change is its merge commit's own diff (`<merge>^1..<merge>` in the
repo); before that it is everything since the merge-base with the branch the job will be incorporated into (the
source repo's checked-out HEAD, as incorporate.preflight reads it). The recorded baseline sha is only the last
resort, when no merge-base can be found."""
from __future__ import annotations

import subprocess
from pathlib import Path

from steward import incorporate as incorporate_module
from steward.store import Store

_TIMEOUT_S = 20


def _git(args: list[str], cwd: Path | str, run=None) -> str | None:
    """`run` is an injected `run(argv, cwd)` (the runner's executor); default is a real subprocess."""
    try:
        if run is not None:
            r = run(["git", *args], cwd=str(cwd))
            code, out = r.exit_code, r.stdout
        else:
            p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=_TIMEOUT_S)
            code, out = p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return out.strip() if code == 0 and out.strip() else None


def job_diff_base(store: Store, job, run=None) -> tuple[str, Path, str] | None:
    """`(base_sha, cwd, head_ref)`: the job's own change is `base_sha..head_ref` run in `cwd`."""
    repo = Path(job.repo)
    merge = incorporate_module.is_incorporated(store, job.job_id)
    if merge:
        base = _git(["rev-parse", f"{merge}^1"], repo, run)
        return (base, repo, merge) if base else None
    wt = store.home / "worktrees" / job.job_id
    if wt.is_dir():
        cwd, head = wt, "HEAD"
    else:
        cwd, head = repo, incorporate_module.branch_name(job.job_id)
    target = _git(["rev-parse", "HEAD"], repo, run)
    base = _git(["merge-base", target, head], cwd, run) if target else None
    if base is None:
        recorded = store.get_checks(job.job_id, label="baseline")
        base = recorded[0]["commit_sha"] if recorded else None
    return (base, cwd, head) if base else None


def job_diff_range(store: Store, job) -> tuple[str, Path] | None:
    """`(range, cwd)` for `git diff <range>` giving the job's own change, or None when it cannot be told."""
    found = job_diff_base(store, job)
    if found is None:
        return None
    base, cwd, head = found
    return f"{base}..{head}", cwd
