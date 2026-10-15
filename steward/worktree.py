"""Git worktree isolation (SP-1, foundation for SP-14's single-writer lease).

Each job's worker operates in its own worktree + branch, never the source
checkout directly. SP-36 (14-01/NB-03): a dirty source tree never blocks a
job — the worktree is still created from HEAD (so baseline and later
verification compare against a known, exact commit, SP-16), and what was
uncommitted is simply not in it; `create_worktree` records that as a
`source_uncommitted_ignored` event instead of raising, and the caller
(`steward.cli._h_new`) turns the count into a contract ASSUMPTIONS line.

`git` is a registered non-Herdr exception (OPX-04, SP-34): every real call
site passes `store=` so `create_worktree` can call `exceptions.use` before
touching the filesystem — an unregistered/removed `git` entry raises
`UnregisteredPath` and the worktree is never created. `store` stays optional
(default `None`, skipping the check) so callers with no store on hand
(existing unit tests exercising `create_worktree` in isolation) are
unaffected.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from steward import exceptions
from steward.models import Judgment
from steward.runner_exec import Runner
from steward.store import Store, resolve_home

# NB-03: cap on how many uncommitted file names the `source_uncommitted_ignored`
# event carries (the count is always exact; only the listed names are capped).
_UNCOMMITTED_NAMES_CAP = 20


class DirtyTree(Exception):
    """Kept for import compatibility; SP-36 means `create_worktree` never
    raises this anymore — a dirty source repo is a recorded assumption, not
    a blocker."""


class WorktreeAddFailed(RuntimeError):
    """`git worktree add` itself failed (u1, 2026-10-14): unlike a missing repo or a dirty tree this is not
    recoverable by proceeding — a job with no worktree would dispatch a worker with nowhere to work. `reason`
    is the owner-facing sentence (what is wrong and what to do)."""

    def __init__(self, stderr: str, reason: str):
        super().__init__(f"git worktree add failed: {stderr}")
        self.reason = reason


def worktree_add_reason(stderr: str) -> str:
    """Owner-facing reason for a failed `git worktree add`; a repo with no commits gets the fix spelled out."""
    text = stderr.strip()
    if "invalid reference: HEAD" in text or "not a valid object name: 'HEAD'" in text:
        return "repo has no commits (git worktree add: invalid reference: HEAD) - make an initial commit, then retry"
    return f"git worktree add failed: {text}"


def _default_base_dir() -> Path:
    return resolve_home() / "worktrees"


def _porcelain_names(output: str) -> list[str]:
    """`git status --porcelain` lines are `XY <path>` (or `XY <old> -> <new>`
    for a rename) — strip the two-char status + space prefix, keep the rest
    verbatim."""
    names = []
    for line in output.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        names.append(line[3:].strip() if len(line) > 3 else line.strip())
    return names


def uncommitted_files(repo: Path | str, runner: Runner) -> list[str]:
    """Names of uncommitted files in `repo`'s source tree (not the
    worktree) — used both by `create_worktree`'s own event and by
    `steward.cli._h_new` to size the contract's assumption line. Returns
    `[]` (never raises) when `git status` itself fails; that failure is
    surfaced separately by `create_worktree`."""
    status = runner.run(["git", "status", "--porcelain"], cwd=Path(repo))
    if status.exit_code != 0:
        return []
    return _porcelain_names(status.stdout)


def create_worktree(
    repo: Path | str,
    job_id: str,
    runner: Runner,
    base_dir: Path | str | None = None,
    *,
    store: Store | None = None,
    start_point: str | None = None,
) -> Path:
    repo = Path(repo)
    if store is not None:
        exceptions.use(store, job_id, "git", {"action": "worktree_add", "repo": str(repo)})
    base = Path(base_dir) if base_dir is not None else _default_base_dir()
    base.mkdir(parents=True, exist_ok=True)

    status = runner.run(["git", "status", "--porcelain"], cwd=repo)
    if status.exit_code != 0:
        raise RuntimeError(f"git status failed in {repo}: {status.stderr}")

    dirty = _porcelain_names(status.stdout)
    if dirty and store is not None:
        # SP-36: never block on this — record it and carry on from HEAD.
        store.append_event(
            job_id,
            what="source_uncommitted_ignored",
            evidence={"files": dirty[:_UNCOMMITTED_NAMES_CAP], "total": len(dirty)},
            rule="dirty_source_not_blocking",
            judgment=Judgment.code,
        )

    worktree_path = base / job_id
    branch = f"steward/{job_id}"
    result = runner.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path), start_point or "HEAD"],
        cwd=repo,
    )
    if result.exit_code != 0:
        raise WorktreeAddFailed(result.stderr, worktree_add_reason(result.stderr))
    linked = link_user_library(worktree_path, runner)
    if linked and store is not None:
        store.append_event(
            job_id,
            what="user_library_linked",
            evidence=linked,
            rule="worker_gets_user_skills_and_agents",
            judgment=Judgment.code,
        )
    return worktree_path


def lane_path(base_dir: Path | str, job_id: str, lane_id: str) -> Path:
    """Stable per-job/per-lane worktree path, distinct from the job's own (integration) worktree."""
    return Path(base_dir) / f"{job_id}--{lane_id}"


def lane_branch(job_id: str, lane_id: str) -> str:
    """Lane branch. Not `steward/<job>/<lane>`: git cannot hold both the parent ref `steward/<job>`
    and a ref nested beneath it, and the parent branch must stay `steward/<job>`."""
    return f"steward-lanes/{job_id}/{lane_id}"


def create_lane_worktree(
    repo: Path | str,
    job_id: str,
    lane_id: str,
    runner: Runner,
    base_dir: Path | str,
    *,
    base_sha: str,
    store: Store | None = None,
) -> Path:
    """Create (or idempotently re-find) one lane's isolated worktree, rooted at the pinned `base_sha`.

    Never the parent worktree and never another lane's: the path/branch are derived from
    (job, lane) only. An existing path is reused only if its checked-out branch is exactly this
    lane's branch (identity-safe); anything else raises."""
    repo = Path(repo)
    if store is not None:
        exceptions.use(store, job_id, "git", {"action": "lane_worktree_add", "repo": str(repo), "lane": lane_id})
    path, branch = lane_path(base_dir, job_id, lane_id), lane_branch(job_id, lane_id)
    Path(base_dir).mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _head_branch(path, runner) != branch:
            raise RuntimeError(f"{path} exists but is not lane worktree {branch}")
        return path
    result = runner.run(["git", "worktree", "add", "-b", branch, str(path), base_sha], cwd=repo)
    if result.exit_code != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr}")
    return path


def _head_branch(path: Path, runner: Runner) -> str | None:
    res = runner.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    return res.stdout.strip() if res.exit_code == 0 else None


def remove_lane_worktree(repo: Path | str, base_dir: Path | str, job_id: str, lane_id: str, runner: Runner) -> bool:
    """Remove a lane worktree only when the path is this lane's derived path and its branch matches."""
    path = lane_path(base_dir, job_id, lane_id)
    if not path.exists() or _head_branch(path, runner) != lane_branch(job_id, lane_id):
        return False
    remove_worktree(repo, path, runner)
    return True


def path_within(root: Path | str, candidate: Path | str) -> bool:
    root, candidate = Path(root).resolve(), Path(candidate).resolve()
    return candidate == root or root in candidate.parents


def _claude_dir() -> Path:
    return Path(os.environ.get("STEWARD_CLAUDE_DIR") or Path.home() / ".claude")


def link_user_library(worktree_path: Path | str, runner: Runner | None = None) -> dict:
    """Give the worker the owner's skill and agent library (not their hooks or permission settings).

    Workers run `claude -p --setting-sources project`, so user-level skills and agents are
    invisible to them. Symlinking each one into the worktree's project `.claude/` makes any
    skill usable by name in a job's goal ("use GSD to...") while Steward's permission gate
    still sees every action. The repo's own entries win; links are git-excluded so they are
    never committed. `STEWARD_USER_LIBRARY=0` turns it off."""
    if os.environ.get("STEWARD_USER_LIBRARY", "1") == "0":
        return {}
    src, wt = _claude_dir(), Path(worktree_path)
    counts, excluded = {}, []
    for kind, pattern in (("skills", "*"), ("agents", "*.md")):
        source_dir = src / kind
        if not source_dir.is_dir():
            continue
        dest_dir = wt / ".claude" / kind
        dest_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        for entry in sorted(source_dir.glob(pattern)):
            dest = dest_dir / entry.name
            if dest.exists() or dest.is_symlink():
                continue  # the repo's own skill/agent wins
            dest.symlink_to(entry.resolve() if entry.is_symlink() else entry)
            excluded.append(f"/.claude/{kind}/{entry.name}")
            n += 1
        counts[kind] = n
    if excluded:
        _exclude_from_git(wt, excluded, runner)
    return counts


def _exclude_from_git(worktree_path: Path, patterns: list[str], runner: Runner | None) -> None:
    """Append patterns to the worktree's git exclude file (never committed, never staged)."""
    try:
        if runner is not None:
            res = runner.run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=worktree_path)
            path = res.stdout.strip() if res.exit_code == 0 else ""
        else:
            path = ""
        exclude = Path(path) if path else worktree_path / ".git" / "info" / "exclude"
        if not exclude.is_absolute():
            exclude = worktree_path / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text().splitlines() if exclude.exists() else []
        new = [p for p in patterns if p not in existing]
        if new:
            with open(exclude, "a") as fh:
                fh.write("\n".join(["# steward: linked user skills/agents", *new]) + "\n")
    except OSError:
        pass


def remove_worktree(repo: Path | str, worktree_path: Path | str, runner: Runner) -> None:
    repo = Path(repo)
    runner.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)], cwd=repo
    )


def user_plugin_dirs() -> list[str]:
    """Install paths of the owner's enabled Claude Code plugins (superpowers etc.).

    Workers run with `--setting-sources project`, which skips user plugins; passing each
    one with `--plugin-dir` lets a job just name a plugin skill ("use superpowers
    brainstorming") with no extra setup. Only plugins enabled in ~/.claude/settings.json
    are loaded (all installed ones if the file lists none). `STEWARD_USER_LIBRARY=0`
    turns this off along with the skill/agent links."""
    if os.environ.get("STEWARD_USER_LIBRARY", "1") == "0":
        return []
    base = _claude_dir()
    try:
        installed = json.loads((base / "plugins" / "installed_plugins.json").read_text())
    except (OSError, ValueError):
        return []
    plugins = installed.get("plugins", installed) if isinstance(installed, dict) else {}
    try:
        enabled = json.loads((base / "settings.json").read_text()).get("enabledPlugins")
    except (OSError, ValueError):
        enabled = None
    dirs = []
    for name, entries in sorted(plugins.items()):
        if isinstance(enabled, dict) and enabled and not enabled.get(name):
            continue
        for entry in entries if isinstance(entries, list) else [entries]:
            path = (entry or {}).get("installPath")
            if path and Path(path).is_dir():
                dirs.append(path)
                break
    return dirs
