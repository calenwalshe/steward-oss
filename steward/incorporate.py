"""Incorporate: fold an ACCEPTED job's branch back into the source repo (human-only).

`preflight` is read-only (what `--dry-run`, the dashboard panel and the phone
screen show). `incorporate` merges `steward/<job_id>` into the source repo's
CURRENT branch with `git merge --no-ff`, never forcing, stashing or resetting
the owner's work: anything that would need that (mid-merge/rebase, staged changes,
a dirty file the merge touches, a conflict) aborts cleanly, leaves the repo as
it was and records `incorporate_blocked`. The job's worktree and branch stay
in place (cleanup is a separate concern).
"""

from __future__ import annotations

import fcntl
import contextvars
import fnmatch
import hashlib
import json
import os
import subprocess
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from steward.models import JobState, Judgment
from steward.store import Store

_MID_OPERATION = (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
                  ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert"))
_MAX_COMMITS = 10
# Worker scratch/evidence directories (any depth); a job branch must not add files under them.
_SCRATCH_DIR_GLOBS = (".tmp-*", ".evidence")
_target_locks: dict[str, threading.RLock] = {}
_target_guard = threading.Lock()
_held = threading.local()


def _target_key(repo) -> str:
    return hashlib.sha1(str(Path(repo).resolve()).encode()).hexdigest()[:12]


def lock_path(store: Store, repo) -> Path:
    """Cross-process lock file shared with every incorporation path into this repo."""
    return Path(store.home) / f"autopilot-merge-{_target_key(repo)}.lock"


class LockBusy(Exception):
    """The target merge lock stayed held past the caller's wait; `holder` is the recorded owner (may be empty)."""

    def __init__(self, holder: dict | None = None):
        self.holder = holder or {}
        super().__init__(busy_text(self.holder))


def busy_text(holder: dict) -> str:
    who = holder.get("purpose") or "another merge"
    pid = f" pid {holder['pid']}" if holder.get("pid") else ""
    since = holder.get("since")
    mins = f" for {max(0, int((time.time() - float(since)) // 60))} min" if since else ""
    return f"busy: merge lock held by {who}{pid}{mins}"


def lock_holder(path: Path) -> dict:
    """The holder record written under the flock (`{pid, purpose, job, since}`); {} when free or unreadable."""
    try:
        data = json.loads(Path(path).read_text() or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@contextmanager
def target_lock(store: Store, repo, *, timeout: float | None = None, purpose: str = "", job_id: str = ""):
    """Serialize target mutations across threads/processes; reentrant while autopilot calls incorporate.
    `timeout` bounds the wait (raises LockBusy naming the holder); None waits forever. The holder record is
    written after acquiring (the file is opened without truncation, so a waiter never wipes it)."""
    key = _target_key(repo)
    with _target_guard:
        lock = _target_locks.setdefault(key, threading.RLock())
    path = lock_path(store, repo)
    if not (lock.acquire() if timeout is None else lock.acquire(timeout=timeout)):
        raise LockBusy(lock_holder(path))
    try:
        depth = getattr(_held, key, 0)
        setattr(_held, key, depth + 1)
        try:
            if depth:
                yield
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                deadline = None if timeout is None else time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | (0 if deadline is None else fcntl.LOCK_NB))
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise LockBusy(lock_holder(path)) from None
                        time.sleep(0.05)
                try:
                    os.ftruncate(fd, 0)
                    os.pwrite(fd, json.dumps({"pid": os.getpid(), "purpose": purpose, "job": job_id,
                                              "since": time.time()}).encode(), 0)
                    yield
                finally:
                    try:
                        os.ftruncate(fd, 0)
                    except OSError:
                        pass
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        finally:
            setattr(_held, key, depth)
    finally:
        lock.release()


STALE_LOCK_AGE_S = 60.0
_default_wait: contextvars.ContextVar[float | None] = contextvars.ContextVar("incorporate_lock_wait", default=None)


@contextmanager
def bounded_lock_wait(seconds: float):
    """Inside this block `incorporate()` waits at most `seconds` for the target lock (the control pane's bound)."""
    tok = _default_wait.set(seconds)
    try:
        yield
    finally:
        _default_wait.reset(tok)


def _index_lock(repo) -> Path | None:
    r = _git(repo, "rev-parse", "--git-path", "index.lock")
    out = r.stdout.strip()
    return (Path(repo) / out).resolve() if r.returncode == 0 and out else None


def _lock_openers(path: Path) -> list[int]:
    """Pids (any uid we can read) holding `path` open, from /proc/*/fd. Raises OSError when /proc is unusable."""
    found = []
    target = str(path)
    for ent in os.scandir("/proc"):
        if not ent.name.isdigit():
            continue
        try:
            for fd in os.scandir(f"/proc/{ent.name}/fd"):
                try:
                    if os.readlink(fd.path) == target:
                        found.append(int(ent.name))
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return found


def _live_git_in(repo, lock: Path) -> list[int]:
    """Running `git` processes whose cwd is inside the repo's work tree or git dir."""
    roots = [str(Path(repo).resolve()), str(lock.parent)]
    found = []
    for ent in os.scandir("/proc"):
        if not ent.name.isdigit():
            continue
        try:
            if Path(f"/proc/{ent.name}/comm").read_text().strip() != "git":
                continue
            cwd = os.readlink(f"/proc/{ent.name}/cwd")
        except OSError:
            continue
        if any(cwd == r or cwd.startswith(r + os.sep) for r in roots):
            found.append(int(ent.name))
    return found


def heal_index_lock(store: Store, job_id: str, repo, *, now: float | None = None) -> dict:
    """Quarantine a stale `.git/index.lock` (older than STALE_LOCK_AGE_S, no live holder). Call under target_lock.
    Renamed aside, never unlinked, so the evidence stays; refuses whenever a live process may own it."""
    now = time.time() if now is None else now
    lock = _index_lock(repo)
    if lock is None:
        return {"healed": False, "reason": "no git dir"}
    try:
        st = lock.stat()
    except OSError:
        return {"healed": False, "reason": "absent"}
    age = now - st.st_mtime
    if age < STALE_LOCK_AGE_S:
        return {"healed": False, "reason": f"fresh ({int(age)}s old)"}
    if not os.path.isdir("/proc/self/fd"):
        return {"healed": False, "reason": "no /proc: cannot prove the lock is stale"}
    try:
        holders = _lock_openers(lock) or _live_git_in(repo, lock)
    except OSError as exc:
        return {"healed": False, "reason": f"cannot inspect /proc ({exc})"}
    if holders:
        return {"healed": False, "reason": "held by pid " + ",".join(map(str, holders[:5]))}
    quarantine = lock.with_name(f"index.lock.stale-{int(now)}")
    try:
        os.rename(lock, quarantine)
        if quarantine.stat().st_ino != st.st_ino:  # another healer replaced it between stat and rename
            os.rename(quarantine, lock)
            return {"healed": False, "reason": "raced"}
    except OSError as exc:
        return {"healed": False, "reason": f"rename failed ({exc})"}
    store.append_event(job_id, what="index_lock_healed",
                       evidence={"path": str(lock), "age_s": int(age), "size": st.st_size, "quarantine": str(quarantine)},
                       rule="lock_hygiene", judgment=Judgment.code)
    return {"healed": True, "path": str(lock), "quarantine": str(quarantine)}


def _tree(repo, rev: str | None) -> str | None:
    if not rev:
        return None
    r = _git(repo, "rev-parse", f"{rev}^{{tree}}")
    return r.stdout.strip() if r.returncode == 0 else None


def _record_incorporation(store: Store, job, receipt: dict, *, tested_tree: str | None, actor) -> None:
    from steward import actor as actor_mod, delivery, verify_reuse

    merge = receipt["merge_commit"]
    merged_tree = _tree(job.repo, merge)
    vr = store.kv_get(job.job_id, verify_reuse.RECEIPT_KEY) or {}
    verified_tree = _tree(job.repo, vr.get("commit_sha")) if isinstance(vr, dict) and vr.get("all_green") else None
    who = actor or actor_mod.classify("cli", actor_mod.current())
    delivery.record_receipt(store, job.job_id, "incorporation", actor=who, revision=merge, now=time.time(), evidence={
        "merge_commit": merge, "source_branch": receipt.get("source_branch"), "commits": receipt.get("commits"),
        "files": receipt.get("files"), "merged_tree": merged_tree, "tested_tree": tested_tree,
        "verified_tree": verified_tree,
        "tested_tree_matches": bool(merged_tree) and merged_tree in {tested_tree, verified_tree}})
    try:
        from steward import cleanup  # local: cleanup imports store
        cleanup.on_resolved(store, job.job_id, cause="incorporated")
    except Exception:  # noqa: BLE001 - cleanup never fails an incorporation
        pass


def _git(repo: str | Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


def branch_name(job_id: str) -> str:
    return f"steward/{job_id}"


def message_for(job) -> str:
    goal = " ".join((job.goal or "").split())[:60]
    return f"Incorporate Steward job {job.job_id[:8]}: {goal}"


@dataclass
class Preflight:
    job_id: str
    repo: str
    branch: str = ""            # the job branch
    source_branch: str = ""     # the source repo's current branch
    head: str = ""              # job branch head sha
    commits: list[str] = field(default_factory=list)  # oneline, max 10
    commit_count: int = 0
    diffstat: str = ""
    files: list[str] = field(default_factory=list)    # files the merge would touch
    overlap: list[str] = field(default_factory=list)  # dirty AND touched -> blocks
    dirty_other: list[str] = field(default_factory=list)  # dirty, untouched -> fine
    staged: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)  # predicted (git merge-tree)
    scratch: list[str] = field(default_factory=list)  # added files under .tmp-*/ or .evidence/
    contained: bool = False
    block: str = ""             # non-empty: why a merge cannot proceed
    error: str = ""             # non-empty: repo/branch unreadable

    @property
    def blocked_files(self) -> list[str]:
        return sorted(set(self.overlap) | set(self.conflicts) | set(self.scratch) | set(self.staged if self.block.startswith("staged") else []))

    def lines(self, width: int = 78) -> list[str]:
        """Human-readable preflight (the dry-run output)."""
        if self.error:
            return [f"repo {self.repo}", f"cannot incorporate: {self.error}"]
        out = [f"repo    {self.repo}  (branch {self.source_branch})",
               f"job     {self.branch} @ {self.head[:7]}"]
        if self.contained:
            out.append("nothing to merge: the job branch is already contained in " + self.source_branch)
        else:
            more = self.commit_count - len(self.commits)
            out.append(f"commits {self.commit_count} not in {self.source_branch}:")
            out += [f"  {c}" for c in self.commits]
            if more > 0:
                out.append(f"  ... and {more} more")
            out += [f"  {ln}" for ln in self.diffstat.splitlines()]
        if self.dirty_other:
            out.append(f"note: {len(self.dirty_other)} uncommitted file(s) not touched by the merge (left alone): "
                       + ", ".join(self.dirty_other[:6]))
        if self.overlap:
            out.append("WARNING: uncommitted changes overlap the merge: " + ", ".join(self.overlap))
        if self.conflicts:
            out.append("WARNING: merge would conflict in: " + ", ".join(self.conflicts))
        if self.block:
            out.append(f"BLOCKED: {self.block}")
        return [ln[:width] for ln in out] if width else out


def _porcelain(repo) -> tuple[list[str], list[str]]:
    """(all dirty paths incl. untracked files, staged paths)."""
    res = _git(repo, "status", "--porcelain", "-uall", "-z")
    dirty, staged = [], []
    parts = res.stdout.split("\0")
    i = 0
    while i < len(parts):
        ent = parts[i]
        i += 1
        if len(ent) < 4:
            continue
        xy, path = ent[:2], ent[3:]
        if xy[0] in "RC":  # rename/copy: next field is the source path
            dirty.append(parts[i]) if i < len(parts) and parts[i] else None
            i += 1
        dirty.append(path)
        if xy[0] not in " ?!":
            staged.append(path)
    return dirty, staged


def _mid_operation(repo) -> str | None:
    for name, label in _MID_OPERATION:
        res = _git(repo, "rev-parse", "--git-path", name)
        p = Path(res.stdout.strip())
        p = p if p.is_absolute() else Path(repo) / p
        if res.returncode == 0 and p.exists():
            return label
    return None


def scratch_files(paths) -> list[str]:
    """Paths that sit under a worker scratch/evidence directory (`.tmp-*/`, `.evidence/`) at any depth."""
    return sorted(p for p in paths
                  if any(fnmatch.fnmatchcase(part, g) for part in p.split("/")[:-1] for g in _SCRATCH_DIR_GLOBS))


def preflight(store: Store, job_id: str) -> Preflight:
    job = store.get_job(job_id)
    if job is None:
        raise KeyError(f"unknown job {job_id}")
    pf = Preflight(job_id=job_id, repo=job.repo, branch=branch_name(job_id))
    repo = job.repo
    if not Path(repo).is_dir() or _git(repo, "rev-parse", "--git-dir").returncode != 0:
        pf.error = f"source repo {repo} is not a git repository"
        return pf
    cur = _git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    if cur.returncode != 0:
        pf.error = "source repo has a detached HEAD (check out a branch first)"
        return pf
    pf.source_branch = cur.stdout.strip()
    from steward import chain_branch

    expected_target = chain_branch.target(store, job_id)
    if expected_target and pf.source_branch != expected_target:
        pf.error = (f"chain target mismatch: job chain is pinned to {expected_target}, but the source repo is "
                    f"checked out on {pf.source_branch}; restore {expected_target} or ask the owner to retarget "
                    "the chain")
        return pf
    head = _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{pf.branch}")
    if head.returncode != 0:
        pf.error = f"job branch {pf.branch} does not exist in {repo}"
        return pf
    pf.head = head.stdout.strip()

    pf.contained = _git(repo, "merge-base", "--is-ancestor", pf.branch, "HEAD").returncode == 0
    if not pf.contained:
        log = _git(repo, "log", "--oneline", f"HEAD..{pf.branch}")
        lines = log.stdout.splitlines()
        pf.commit_count = len(lines)
        pf.commits = lines[:_MAX_COMMITS]
        pf.diffstat = _git(repo, "diff", "--stat", f"HEAD...{pf.branch}").stdout.strip()
        pf.files = [f for f in _git(repo, "diff", "--name-only", f"HEAD...{pf.branch}").stdout.splitlines() if f]
        added = _git(repo, "diff", "--name-only", "--diff-filter=A", f"HEAD...{pf.branch}").stdout.splitlines()
        pf.scratch = scratch_files(f for f in added if f)

    dirty, pf.staged = _porcelain(repo)
    touched = set(pf.files)
    pf.overlap = sorted(set(d for d in dirty if d in touched))
    pf.dirty_other = sorted(set(d for d in dirty if d not in touched))

    if not pf.contained:
        mt = _git(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages", "HEAD", pf.branch)
        if mt.returncode == 1:  # clean=0, conflicts=1 (older git: other codes -> no prediction)
            pf.conflicts = [ln for ln in mt.stdout.splitlines()[1:] if ln and not ln.startswith(" ")]
            pf.conflicts = sorted(set(pf.conflicts))

    op = _mid_operation(repo)
    if op:
        pf.block = f"source repo is mid-{op}; finish or abort it first"
    elif not pf.contained and pf.staged:
        pf.block = "staged changes in the source repo would be swept into the merge commit: " + ", ".join(pf.staged[:6])
    elif pf.overlap:
        pf.block = "uncommitted changes overlap files the merge touches: " + ", ".join(pf.overlap)
    elif pf.conflicts:
        pf.block = "merge would conflict in: " + ", ".join(pf.conflicts)
    elif pf.scratch:
        shown = ", ".join(pf.scratch[:6]) + (f" (+{len(pf.scratch) - 6} more)" if len(pf.scratch) > 6 else "")
        pf.block = ("job branch adds worker scratch/evidence files (.tmp-*/, .evidence/) that are not source; "
                    "send the job back to remove them (git rm) before merging: " + shown)
    return pf


def is_incorporated(store: Store, job_id: str) -> str | None:
    return store.kv_get(job_id, "incorporated_commit")


RELEASE_RULE = "released_on_incorporation"
_RECEIPT_WHATS = ("incorporated", "auto_merged", "landed_by_train", "incorporated_via_followup")


def release_evidence(store: Store, job) -> dict:
    """Spec 5.3: may this ACCEPTED job be released by code? ok only with a merge receipt whose commit is reachable."""
    commit = store.kv_get(job.job_id, "incorporated_commit")
    if not commit:
        return {"ok": False, "merge_commit": "", "branch": "", "repo_gone": False, "reason": "not incorporated"}
    receipt = next((e for e in reversed(store.events(job.job_id))
                    if e.what in _RECEIPT_WHATS and e.evidence.get("merge_commit") == commit), None)
    if receipt is None:
        return {"ok": False, "merge_commit": commit, "branch": "", "repo_gone": False,
                "reason": "no merge receipt for the recorded commit"}
    branch = str(receipt.evidence.get("source_branch") or receipt.evidence.get("target") or "")
    repo = Path(job.repo)
    if not repo.exists():
        return {"ok": True, "merge_commit": commit, "branch": branch, "repo_gone": True, "reason": "repo gone"}
    if branch and _git(repo, "merge-base", "--is-ancestor", commit, branch).returncode == 0:
        return {"ok": True, "merge_commit": commit, "branch": branch, "repo_gone": False, "reason": ""}
    # No usable receipt branch: only the target checkout's own branch (HEAD) counts, never a job/scratch branch.
    head = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if head and head != "HEAD" and _git(repo, "merge-base", "--is-ancestor", commit, "HEAD").returncode == 0:
        return {"ok": True, "merge_commit": commit, "branch": head, "repo_gone": False, "reason": ""}
    return {"ok": False, "merge_commit": commit, "branch": branch, "repo_gone": False,
            "reason": f"merge {commit[:8]} not reachable from {branch or 'the target branch'}"}


def release_on_incorporation(store: Store, job_id: str) -> bool:
    job = store.get_job(job_id)
    if job is None or job.state != JobState.ACCEPTED:
        return False
    ev = release_evidence(store, job)
    if not ev["ok"]:
        return False
    store.set_state(job_id, JobState.RELEASED, what="released on incorporation", evidence=ev,
                    rule=RELEASE_RULE, judgment=Judgment.code)
    return True


def mark_parents_incorporated(store: Store, child_id: str, merge_commit: str) -> list[str]:
    """A merged fix-up carries every commit of the job it fixes up (its branch starts at that job's head), so that job
    is on the source branch too: record it incorporated via the fix-up's merge commit and release it (its work is
    done; the row would otherwise read "not merged" for ever). Follows a chain of fix-ups. Returns the parents marked."""
    marked, cur, seen = [], child_id, set()
    while cur not in seen and (parent := store.kv_get(cur, "autopilot_followup")):
        seen.add(cur)
        from steward import autopilot

        protected = autopilot.no_merge_protection(store, str(parent)) if isinstance(parent, str) else None
        if protected:
            store.append_event(parent, what="followup_incorporation_held",
                               evidence={"merge_commit": merge_commit, "via": child_id,
                                         "protected_source": protected[0], "reason": protected[1]},
                               rule="no_merge_policy", judgment=Judgment.code)
            cur = parent
            continue
        if isinstance(parent, str) and store.get_job(parent) is not None and not is_incorporated(store, parent):
            store.kv_set(parent, "incorporated_commit", merge_commit)
            store.append_event(parent, what="incorporated_via_followup",
                               evidence={"merge_commit": merge_commit, "via": child_id},
                               rule="autopilot_merge", judgment=Judgment.code)
            release_on_incorporation(store, parent)
            marked.append(parent)
        cur = parent if isinstance(parent, str) else ""
    return marked


def _blocked(store: Store, job_id: str, reason: str, files: list[str]) -> dict:
    store.append_event(job_id, what="incorporate_blocked", evidence={"reason": reason, "files": files},
                       rule="operator_incorporate", judgment=Judgment.human)
    detail = f"blocked: {reason}" + (f" [{', '.join(files)}]" if files and not reason.endswith(", ".join(files)) else "")
    return {"accepted": False, "detail": detail + " (repo left untouched)", "error_kind": "incorporate_blocked",
            "files": files}


def fleet_guard(store: Store, job_id: str) -> str | None:
    """Fleet jobs (explicit graph) only: the job must be ACCEPTED (not RELEASED) and its final persisted candidate
    SHA must equal both the integration ledger's final SHA and `refs/heads/steward/<job_id>`. None for legacy jobs
    and when every condition holds; otherwise the reason the source repo must stay untouched."""
    from steward import fleet_closeout
    from steward.fleet_store import FleetStore

    if not fleet_closeout.is_fleet(store, job_id):
        return None
    job = store.get_job(job_id)
    if job.state != JobState.ACCEPTED:
        return f"fleet job is {job.state.value}; incorporate needs the human-ACCEPTED combined candidate"
    cand = fleet_closeout.final_candidate(store, job_id)
    if cand is None:
        return "fleet job has no verified combined candidate"
    ledger = FleetStore(store).integration_head(job_id)
    if cand["final_sha"] != ledger:
        return f"stale fleet candidate: {cand['final_sha'][:12]} is not the integration ledger's final SHA"
    head = _git(job.repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch_name(job_id)}")
    if head.returncode != 0 or head.stdout.strip() != cand["final_sha"]:
        return (f"stale fleet candidate: accepted {cand['final_sha'][:12]} differs from "
                f"{branch_name(job_id)} HEAD {head.stdout.strip()[:12] or 'missing'}")
    return None


def _incorporate(store: Store, job_id: str, *, expected_head: str | None = None,
                 expected_branch: str | None = None) -> dict:
    """Merge the job branch into the source repo's current branch. Returns a relay-shaped receipt.
    `expected_head`/`expected_branch` pin the tested inputs: when the target HEAD or the job branch moved since, nothing
    merges and nothing is recorded (`error_kind` "moved": the caller re-tests); the branch merged is that exact sha."""
    job = store.get_job(job_id)
    if job is None:
        return {"accepted": False, "detail": f"unknown job: {job_id}", "error_kind": "unknown_job"}
    from steward import replace as replace_module  # lazy: replace reads this module's kv

    replaced = replace_module.refusal(store, job_id, "incorporate")  # issue M: before any write, every merge route
    if replaced:
        return {"accepted": False, "detail": replaced + " (repo left untouched)", "error_kind": "replaced"}
    # Releasing only clears a job from the pane; its accepted work can still be merged.
    if job.state not in (JobState.ACCEPTED, JobState.RELEASED):
        return {"accepted": False, "error_kind": "illegal_transition",
                "detail": f"job {job_id[:8]} is {job.state.value}; incorporate needs ACCEPTED (or RELEASED)"}
    block = fleet_guard(store, job_id)  # before anything can write: a fleet merge needs the accepted current SHA
    if block:
        return _blocked(store, job_id, block, [])
    from steward import questions_delivery  # lazy: questions_delivery imports this module
    questions_delivery.flush_now(store, job_id)  # answered-but-unwritten questions go onto the job branch first
    pf = preflight(store, job_id)
    if pf.error:
        return _blocked(store, job_id, pf.error, [])
    if pf.contained:
        store.append_event(job_id, what="incorporate_noop",
                           evidence={"source_branch": pf.source_branch, "head": pf.head},
                           rule="operator_incorporate", judgment=Judgment.human)
        at = _git(job.repo, "rev-parse", "HEAD").stdout.strip()
        if at and not is_incorporated(store, job_id):  # already merged some other way: record it so the row clears
            store.kv_set(job_id, "incorporated_commit", at)
            mark_parents_incorporated(store, job_id, at)
        return {"accepted": True, "noop": True,
                "detail": f"nothing to merge: {pf.branch} is already in {pf.source_branch}"}
    repo = job.repo
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    moved = [what for what, want, got in (("target HEAD", expected_head, before), ("job branch", expected_branch, pf.head))
             if want is not None and want != got]
    if moved:
        return {"accepted": False, "error_kind": "moved",
                "detail": " and ".join(moved) + " moved since the merged tree was tested (repo left untouched)"}
    if pf.block:
        return _blocked(store, job_id, pf.block, pf.blocked_files)
    rev = expected_branch or pf.branch
    res = _git(repo, "merge", "--no-ff", rev, "-m", message_for(job))
    if res.returncode != 0 and "index.lock" in (res.stderr or res.stdout):  # a stranded lock: heal once, retry once
        healed = heal_index_lock(store, job_id, repo)
        if healed["healed"]:
            res = _git(repo, "merge", "--no-ff", rev, "-m", message_for(job))
        else:
            return _blocked(store, job_id, f"index.lock held: {healed['reason']}", [])
    if res.returncode != 0:
        conflicted = sorted(set(_git(repo, "diff", "--name-only", "--diff-filter=U").stdout.split()))
        if _mid_operation(repo) == "merge":
            _git(repo, "merge", "--abort")
        text = " ".join((res.stderr or res.stdout).split())[:200]
        return _blocked(store, job_id, f"git merge failed ({text})" if not conflicted
                        else "merge conflict", conflicted)
    merge_commit = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if merge_commit == before:  # defensive: nothing happened
        return _blocked(store, job_id, "git merge made no commit", [])
    store.append_event(job_id, what="incorporated",
                       evidence={"merge_commit": merge_commit, "source_branch": pf.source_branch,
                                 "commits": pf.commit_count, "files": len(pf.files)},
                       rule="operator_incorporate", judgment=Judgment.human)
    store.kv_set(job_id, "incorporated_commit", merge_commit)
    mark_parents_incorporated(store, job_id, merge_commit)
    from steward import repair_episode

    repair_episode.on_incorporated(store, job_id, merge_commit, now=time.time())
    questions_delivery.flush_now(store, job_id)  # merged: anything still unwritten waits for --write-source
    return {"accepted": True, "merge_commit": merge_commit, "source_branch": pf.source_branch,
            "detail": (f"incorporated {merge_commit[:7]} into {pf.source_branch} "
                       f"({pf.commit_count} commit(s), {len(pf.files)} file(s)); "
                       f"worktree and branch {pf.branch} left in place")}


def incorporate(store: Store, job_id: str, *, tested_tree: str | None = None, actor=None,
                lock_timeout: float | None = None, expected_head: str | None = None,
                expected_branch: str | None = None) -> dict:
    """Merge once per source target and write an immutable attribution receipt for real merges.
    `lock_timeout` bounds the wait on the target lock: a busy lock returns `merge_lock_busy` (nothing recorded).
    `expected_head`/`expected_branch`: see `_incorporate` (a moved input returns `moved`, nothing recorded)."""
    job = store.get_job(job_id)
    if job is None:
        return _incorporate(store, job_id)
    lock_timeout = _default_wait.get() if lock_timeout is None else lock_timeout
    try:
        with ExitStack() as stack:
            stack.enter_context(target_lock(store, job.repo, timeout=lock_timeout,
                                            purpose=f"incorporate {job_id[:8]}", job_id=job_id))
            receipt = _incorporate(store, job_id, expected_head=expected_head, expected_branch=expected_branch)
            current = store.get_job(job_id)
            if receipt.get("accepted") and receipt.get("merge_commit") and not receipt.get("noop") and current:
                _record_incorporation(store, current, receipt, tested_tree=tested_tree, actor=actor)
    except LockBusy as busy:
        return {"accepted": False, "error_kind": "merge_lock_busy",
                "detail": f"{busy}; delivery will merge it, or retry", "holder": busy.holder}
    if receipt.get("accepted"):
        from steward import herdr_state  # lazy: herdr_state imports this module

        herdr_state.sync(store, None, job_id)
    return receipt
