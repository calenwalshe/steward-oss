# steward/train_build.py
"""Build train <n> in the dedicated worktree (detached HEAD: no `train/<n>` branch is ever created in the deploy
repo; the name is only a label) and pick the landable subset (2026-10-08 spec)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from steward import incorporate as inc
from steward import train_resolver


@dataclass
class TrainResult:
    branch: str
    base_sha: str
    head: str
    merged: list[str] = field(default_factory=list)
    dropped: dict[str, str] = field(default_factory=dict)


def _git(cwd, *args, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=Path(cwd), capture_output=True, text=True, check=check)


def prepare_worktree(repo, root, target: str) -> Path:
    wt = Path(root) / "train"
    if not (wt / ".git").exists():
        wt.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "worktree", "prune")
        _git(repo, "worktree", "add", "--detach", str(wt), target)
    return wt


def target_sha(repo, target: str) -> str:
    return _git(repo, "rev-parse", "-q", "--verify", target, check=False).stdout.strip()


def conflicted_files(worktree) -> list[str]:
    out = _git(worktree, "diff", "--name-only", "--diff-filter=U", check=False).stdout
    return [f for f in out.splitlines() if f]


def _abort(worktree) -> None:
    _git(worktree, "merge", "--abort", check=False)
    _git(worktree, "reset", "-q", "--merge", check=False)


def _rollback(worktree, pre: str) -> None:
    _git(worktree, "merge", "--abort", check=False)
    _git(worktree, "reset", "-q", "--hard", pre)
    _git(worktree, "clean", "-fdq")


def build_train(repo, worktree, target: str, job_ids: list[str], n: int, *,
                resolve_fn: Callable[[Path, str, list[str]], bool]) -> TrainResult:
    worktree = Path(worktree)
    base = _git(repo, "rev-parse", target).stdout.strip()
    branch = f"train/{n}"
    _abort(worktree)
    _git(worktree, "checkout", "-q", "--detach", "--force", base)
    _git(worktree, "clean", "-fdq")
    res = TrainResult(branch=branch, base_sha=base, head=base)
    for jid in job_ids:
        ref = inc.branch_name(jid)
        if _git(worktree, "rev-parse", "--verify", "-q", f"refs/heads/{ref}", check=False).returncode != 0:
            res.dropped[jid] = "missing branch"
            continue
        if _git(worktree, "merge-base", "--is-ancestor", f"refs/heads/{ref}", base, check=False).returncode == 0:
            res.dropped[jid] = "already contained in target"     # a no-op merge must never count as landed
            continue
        pre = _git(worktree, "rev-parse", "HEAD").stdout.strip()
        m = _git(worktree, "merge", "--no-ff", "-q", "-m", f"Land Steward job {jid} (train {n})", ref, check=False)
        if m.returncode == 0:
            res.merged.append(jid)
            continue
        files = conflicted_files(worktree)
        merging = _git(worktree, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0
        ok, check = False, files
        if files:
            try:
                ok = bool(resolve_fn(worktree, jid, files))
            except Exception:  # noqa: BLE001 - a crashing resolver just means "unresolved"
                ok = False
        elif merging:      # rerere.autoupdate replayed a recorded resolution: rc=1, MERGE_HEAD set, nothing unmerged
            check = [f for f in _git(worktree, "diff", "--cached", "--name-only", pre, check=False).stdout.splitlines()
                     if f]
            ok = True      # the same guards as the resolver path decide (no markers in index or worktree, clean)
        if ok and not conflicted_files(worktree) and not train_resolver.has_markers(worktree, check) \
                and _git(worktree, "rev-parse", "HEAD").stdout.strip() == pre \
                and _git(worktree, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0 \
                and _git(worktree, "commit", "-q", "--no-edit", check=False).returncode == 0 \
                and not _git(worktree, "status", "--porcelain").stdout.strip():
            res.merged.append(jid)
            continue
        _rollback(worktree, pre)
        res.dropped[jid] = ("conflict: " + ", ".join(files[:5])) if files else f"merge failed: {m.stderr.strip()[:200]}"
    res.head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    return res


def select_landable(job_ids: list[str], is_green: Callable[[list[str]], bool]) -> tuple[list[str], list[str]]:
    """Bisect: grow a known-green set chunk by chunk; a chunk that turns it red is split until single culprits remain.
    The returned `good` list is exactly the argument of the last `is_green` call that returned True (or empty)."""
    good: list[str] = []
    bad: list[str] = []

    def visit(chunk: list[str]) -> None:
        if not chunk:
            return
        if is_green(good + chunk):
            good.extend(chunk)
            return
        if len(chunk) == 1:
            bad.append(chunk[0])
            return
        mid = len(chunk) // 2
        visit(chunk[:mid])
        visit(chunk[mid:])

    visit(list(job_ids))
    return good, bad
