"""Which code a restored Steward process starts from (issue W, self-heal spec §5).

Restore from the deployed revision, never from an untested checkout. A self-merge lands in the live checkout and is
re-tested there (up to hours, `deploy_pending`) before `deploy-self` moves the processes onto it; a dead process
restarted from the checkout meanwhile would run that untested code early.

* `record` — a fully successful `deploy-self` writes `<home>/deployed.json` = {head, root, at}.
* `resolve_head` — deployed.json, else the head the running/last control recorded in control.json (what is live),
  else the live HEAD when no merge awaits deploy, else None (unknown: the caller holds and escalates).
* `launch_root` — the live checkout when its HEAD is the deployed head; otherwise a pinned tree, a detached
  `git worktree` at the deployed head under `<home>/deployed/<sha12>` (made on demand, reused, at most KEEP kept,
  never one a process runs from). The process gets `STEWARD_LIVE_ROOT=<live checkout>` so its outdated check, its
  ping's `root` and its self-restart all follow the live checkout: it moves onto new code exactly when deploys move
  everything else.
* `prepare_reexec` — before an execv onto new code: chdir to `STEWARD_LIVE_ROOT` and drop it.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Callable

FILE = "deployed.json"
DIR = "deployed"
KEEP = 3
ENV = "STEWARD_LIVE_ROOT"


class UnknownDeployed(RuntimeError):
    """The deployed revision cannot be told, and the checkout may hold an untested merge."""


def record(home: Path | str, head: str, root: str, now: float | None = None) -> None:
    from steward import proclock

    if head:
        proclock.write_json(Path(home) / FILE, {"head": head, "root": str(root), "at": time.time() if now is None else now})


def load(home: Path | str) -> dict | None:
    from steward import proclock

    rec = proclock.read_json(Path(home) / FILE)
    return rec if rec and rec.get("head") else None


def _head(root: Path | str) -> str:
    from steward import stability

    return stability.repo_head(root)


def pending_mark(home: Path | str, *, now: float | None = None) -> bool:
    """A Steward self-merge awaits its deploy (`deploy_pending`), read through a read-only connection. An unreadable
    DB counts as pending: the safe answer when we cannot tell whether the checkout is tested."""
    from steward import deploy_pending

    db = Path(home) / "steward.db"
    if not db.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        try:  # the marks list (deploy_pending.KV_MARKS) and the legacy single slot (KV_KEY), as deploy_pending.pending
            rows = conn.execute("SELECT key, value_json FROM kv WHERE job_id = ? AND key IN (?, ?)",
                                (deploy_pending.KV_JOB, deploy_pending.KV_MARKS, deploy_pending.KV_KEY)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return True
    recs: list = []
    for key, value in rows:
        try:
            rec = json.loads(value)
        except ValueError:
            continue
        if key == deploy_pending.KV_MARKS and isinstance(rec, list):
            recs.extend(rec)
        elif key == deploy_pending.KV_KEY:
            recs.append(rec)
    now = time.time() if now is None else now
    return any(isinstance(rec, dict) and isinstance(rec.get("since"), (int, float)) and
               0 <= now - rec["since"] < deploy_pending.MAX_AGE_S for rec in recs)


def resolve_head(home: Path | str, live_root: Path | str, *, pending: bool | None = None,
                 head_fn: Callable[[Path | str], str] = _head) -> str | None:
    from steward import proclock

    rec = load(home)
    if rec:
        return str(rec["head"])
    ctl = proclock.read_json(proclock.control_file(home)) or {}
    if ctl.get("head"):
        return str(ctl["head"])
    if pending is None:
        pending = pending_mark(home)
    if not pending:
        return head_fn(live_root) or None
    return None


def launch_root(home: Path | str, live_root: Path | str, *, pending: bool | None = None,
                head_fn: Callable[[Path | str], str] = _head) -> tuple[Path, Path | None]:
    """(root to start from, live root to export or None). Raises UnknownDeployed."""
    home, live_root = Path(home), Path(live_root)
    want = resolve_head(home, live_root, pending=pending, head_fn=head_fn)
    if not want:
        raise UnknownDeployed("the deployed revision is unknown and a merge may await deploy")
    live_head = head_fn(live_root)
    if live_head and live_head == want:
        return live_root, None
    pinned = home / DIR / want[:12]
    if not (pinned.is_dir() and head_fn(pinned) == want):
        _make_tree(live_root, pinned, want)
    try:
        os.utime(pinned)
    except OSError:
        pass
    _prune(live_root, home / DIR, keep=pinned)
    return pinned, live_root


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=120)


def _drop_tree(live_root: Path, tree: Path) -> None:
    _git(live_root, "worktree", "remove", "--force", str(tree))
    if tree.exists():
        shutil.rmtree(tree, ignore_errors=True)
    _git(live_root, "worktree", "prune")


def _make_tree(live_root: Path, tree: Path, sha: str) -> None:
    if tree.exists():
        _drop_tree(live_root, tree)
    tree.parent.mkdir(parents=True, exist_ok=True)
    p = _git(live_root, "worktree", "add", "--detach", str(tree), sha)
    if p.returncode != 0:
        raise UnknownDeployed(f"cannot pin the deployed revision {sha[:12]}: {(p.stderr or p.stdout).strip()[:200]}")


def _in_use(tree: Path) -> bool:
    """Some process runs from inside `tree` (its cwd)."""
    tree = tree.resolve()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cwd = Path(os.readlink(entry / "cwd"))
        except OSError:
            continue
        if cwd == tree or tree in cwd.parents:
            return True
    return False


def _prune(live_root: Path, parent: Path, *, keep: Path) -> None:
    try:
        trees = sorted((p for p in parent.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old in trees[KEEP:]:
        if old != keep and not _in_use(old):
            _drop_tree(live_root, old)


def prepare_reexec(env=None, chdir: Callable[[str], None] = os.chdir) -> None:
    env = os.environ if env is None else env
    live = env.pop(ENV, None)
    if live:
        chdir(live)


def export_line(live_root: Path | str | None) -> str:
    import shlex

    return f"export {ENV}={shlex.quote(str(live_root))} && " if live_root else ""
