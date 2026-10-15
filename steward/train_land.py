"""Land a green train: ff-only under the merge lock, deploy, revert with new commits if health is red."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from steward import incorporate as inc
from steward import landing_queue as q
from steward.models import JobState, Judgment
from steward.store import Store, now_iso

LAND_LOCK_WAIT_S = 120.0


def _git(cwd, *args, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=Path(cwd), capture_output=True, text=True, check=check)


def _fail(reason: str, **extra) -> dict:
    return {"ok": False, "reason": reason, "landed": [], "head": "", "reverted": False, "deploy": None, **extra}


KEY = ("_train", "landing")


def _notify(notify_fn, text: str) -> None:
    if notify_fn is None:
        return
    try:
        notify_fn(text)
    except Exception:  # noqa: BLE001 - a broken notifier must never mask the landing outcome
        pass


def _deploy(deploy_fn) -> dict | None:
    if deploy_fn is None:
        return None
    try:
        return deploy_fn() or {"ok": False, "summary": "deploy returned nothing"}
    except Exception as exc:  # noqa: BLE001 - a crashing deploy is a red deploy
        return {"ok": False, "summary": f"deploy raised {type(exc).__name__}: {exc}"}


def _deferred(dep: dict | None) -> bool:
    """A deploy that refused to run for a passing reason (another deploy running, an answer in flight) is deferred,
    not red: nothing was restarted, so there is nothing to revert. `transient: False` marks a refusal about the
    landed code itself (it does not import), which is red."""
    return bool(dep) and not dep.get("ok") and bool(dep.get("refused")) and dep.get("transient", True) is not False


def _preflight(preflight_fn) -> str:
    """"" when a deploy is possible now, else why not (a crashing pre-flight is "not possible")."""
    if preflight_fn is None:
        return ""
    try:
        return str(preflight_fn() or "")
    except Exception as exc:  # noqa: BLE001
        return f"pre-flight raised {type(exc).__name__}: {exc}"


def _defer(store: Store, rec: dict, why: str, dep: dict | None = None) -> dict:
    store.kv_set(*KEY, {**rec, "deferred": why[:200] or "deploy deferred"})
    return {**_fail("deploy deferred: " + why[:200]), "head": rec.get("train_head", ""), "deferred": True,
            "deploy": dep}


def _revert_subject(base_sha: str, head: str) -> str:
    return f"Revert landing train {base_sha[:7]}..{head[:7]}"


def _find_revert(repo, target: str, base_sha: str, head: str) -> str:
    """The train's revert commit on the target after `head`, or ""."""
    out = _git(repo, "log", "--first-parent", "--format=%H %s", f"{head}..{target}").stdout
    for line in out.splitlines():
        sha, _, subject = line.partition(" ")
        if subject.startswith(_revert_subject(base_sha, head)):
            return sha
    return ""


def _is_ancestor(repo, commit: str, target: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", commit, target).returncode == 0


def _revert(repo, base_sha: str, head: str) -> str:
    """All-or-nothing: revert every first-parent train merge (newest first) into ONE commit. Returns the revert
    commit sha, or "" when anything failed (the tree is then restored; no revert commit exists)."""
    merges = _git(repo, "rev-list", "--first-parent", "--merges", f"{base_sha}..{head}").stdout.split()
    if not merges:
        return ""
    for m in merges:
        if _git(repo, "revert", "--no-commit", "-m", "1", m).returncode != 0:
            _git(repo, "revert", "--abort")
            _git(repo, "reset", "-q", "--merge")
            return ""
    msg = f"{_revert_subject(base_sha, head)} (deploy red)"
    if _git(repo, "commit", "-q", "-m", msg).returncode != 0:
        _git(repo, "revert", "--abort")
        _git(repo, "reset", "-q", "--merge")
        return ""
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _ff(store: Store, repo, target: str, train_head: str, base_sha: str, job_ids: list[str]) -> dict | None:
    """Fast-forward target to the train under the merge lock, writing the durable intent record first.
    Returns a failure dict, or None when the ff happened."""
    try:
        with inc.target_lock(store, repo, timeout=LAND_LOCK_WAIT_S, purpose="landing train"):
            if store.kv_get(*KEY):
                return _fail("pending landing unresolved")
            current = _git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
            if current != target:
                return _fail(f"target not checked out (on {current or 'detached'})")
            if _git(repo, "rev-parse", target).stdout.strip() != base_sha:
                return _fail("moved")
            store.kv_set(*KEY, {"train_head": train_head, "base": base_sha, "job_ids": list(job_ids),
                                "target": target, "repo": str(repo), "at": now_iso()})
            ff = _git(repo, "merge", "--ff-only", "-q", train_head)
            if ff.returncode != 0:
                store.kv_set(*KEY, {})
                reason = "ff-only refused: " + ff.stderr.strip()[:200]
                for jid in job_ids:
                    q.mark_dropped(store, jid, reason, target_head=base_sha)
                return _fail(reason)
    except inc.LockBusy as exc:
        return _fail(f"merge lock busy: {exc}")
    return None


def _record(store: Store, target: str, train_head: str, base_sha: str, job_ids: list[str]) -> None:
    for jid in job_ids:                                   # landing order
        store.kv_set(jid, "incorporated_commit", train_head)
        for parent in inc.mark_parents_incorporated(store, jid, train_head):   # a fix-up carried its parent
            if q.get(store, parent) is not None:
                q.mark_landed(store, parent, train_head)
        store.append_event(jid, what="landed_by_train",
                           evidence={"merge_commit": train_head, "base": base_sha, "target": target,
                                     "batch": len(job_ids)},
                           rule="landing_train", judgment=Judgment.code)
        inc.release_on_incorporation(store, jid)          # after the receipt event: release needs it
        q.mark_landed(store, jid, train_head)
    store.kv_set(*KEY, {})


def _park_reverted(store: Store, job_ids: list[str], train_head: str, base_sha: str, revert_sha: str) -> None:
    for jid in job_ids:                                   # idempotent: the event is written once per job
        e = q.get(store, jid)
        if e is not None and e.status == "parked" and e.last_reason == f"deploy red; reverted {revert_sha}":
            continue
        store.append_event(jid, what="train_reverted",
                           evidence={"train_head": train_head, "revert_commit": revert_sha, "base": base_sha},
                           rule="landing_train", judgment=Judgment.code)
        q.park(store, jid, f"deploy red; reverted {revert_sha}", target_head=revert_sha)


def _red(store: Store, repo, target: str, train_head: str, base_sha: str, job_ids: list[str],
         dep: dict, deploy_fn, notify_fn) -> dict:
    summary = str(dep.get("summary", ""))[:200]
    revert_sha, why = "", ""
    try:
        with inc.target_lock(store, repo, timeout=LAND_LOCK_WAIT_S, purpose="landing train revert"):
            current = _git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
            if current != target:
                why = f"target not checked out (on {current or 'detached'})"
            elif not _is_ancestor(repo, train_head, target):
                why = "train head is no longer on the target"
            else:                          # flag BEFORE the commit can exist: resume then never records it landed
                store.kv_set(*KEY, {**(store.kv_get(*KEY) or {}), "train_head": train_head, "base": base_sha,
                                    "job_ids": list(job_ids), "target": target, "repo": str(repo),
                                    "reverting": True})
                revert_sha = _revert(repo, base_sha, train_head)
                if not revert_sha:
                    why = "revert did not apply cleanly"
                else:                      # park + flag the intent BEFORE anything that can kill the process
                    _park_reverted(store, job_ids, train_head, base_sha, revert_sha)
                    rec = store.kv_get(*KEY) or {}
                    store.kv_set(*KEY, {**rec, "train_head": train_head, "base": base_sha, "job_ids": list(job_ids),
                                        "target": target, "repo": str(repo), "reverted": revert_sha})
    except inc.LockBusy as exc:
        why = f"merge lock busy: {exc}"
    if not revert_sha:
        _notify(notify_fn, f"landing train {train_head[:7]}: deploy red ({summary}) and NOT reverted ({why}); "
                           "red code may still be live")
        return {**_fail("deploy red: " + summary + f"; not reverted: {why}"), "head": train_head,
                "deploy": {"first": dep, "after_revert": None}}
    redeploy = _deploy(deploy_fn)
    store.kv_set(*KEY, {})
    after = "" if redeploy is None or redeploy.get("ok") else "; reverted but redeploy red"
    _notify(notify_fn, f"landing train {train_head[:7]}: deploy red ({summary}); reverted as {revert_sha[:7]}{after}")
    return {**_fail("deploy red: " + summary), "reverted": True, "head": revert_sha,
            "deploy": {"first": dep, "after_revert": redeploy}}


def land(store: Store, repo, target: str, train_head: str, base_sha: str, job_ids: list[str], *,
         deploy_fn: Callable[[], dict] | None, notify_fn: Callable[[str], object] | None = None,
         preflight_fn: Callable[[], str | None] | None = None) -> dict:
    why = _preflight(preflight_fn)
    if why:                                # skip this cycle: no ff, no drop, nothing recorded
        return _fail(f"deploy not possible: {why}")
    failed = _ff(store, repo, target, train_head, base_sha, job_ids)
    if failed is not None:
        return failed
    rec = store.kv_get(*KEY) or {}
    store.kv_set(*KEY, {**rec, "attempted": True})   # from here a death may leave new code running: resume treats
    dep = _deploy(deploy_fn)                            # red health as red, never as "deploy not possible"
    if _deferred(dep):                                  # a refusal restarted nothing
        return _defer(store, rec, str(dep.get("summary", "")), dep)
    if dep is not None and not dep.get("ok"):
        return _red(store, repo, target, train_head, base_sha, job_ids, dep, deploy_fn, notify_fn)
    _record(store, target, train_head, base_sha, job_ids)
    return {"ok": True, "reason": "", "landed": list(job_ids), "head": train_head, "reverted": False, "deploy": dep}


def resume_pending(store: Store, *, deploy_fn: Callable[[], dict] | None,
                   notify_fn: Callable[[str], object] | None = None,
                   preflight_fn: Callable[[], str | None] | None = None) -> dict | None:
    """Finish a landing a dead process left between the ff and the recording, or retry a deferred deploy.
    Call at the start of every run."""
    rec = store.kv_get(*KEY) or {}
    if not rec:
        return None
    repo, target, head = rec["repo"], rec["target"], rec["train_head"]
    revert_sha = rec.get("reverted") or _find_revert(repo, target, rec["base"], head)
    if revert_sha:                                        # died after the revert commit: never record it as landed
        _park_reverted(store, rec["job_ids"], head, rec["base"], revert_sha)
        store.kv_set(*KEY, {})
        _notify(notify_fn, f"landing train {head[:7]}: revert {revert_sha[:7]} was already committed; jobs parked")
        return {**_fail("deploy red: train was reverted before restart"), "reverted": True, "head": revert_sha}
    if rec.get("reverting"):                              # died before/inside the revert: redo the revert path
        if not _is_ancestor(repo, head, target):          # nothing of the red train is on the target any more
            now_head = _git(repo, "rev-parse", target).stdout.strip()
            for jid in rec["job_ids"]:
                q.park(store, jid, "deploy red; train left the target before it was reverted", target_head=now_head)
            store.kv_set(*KEY, {})
            return {**_fail("deploy red: train no longer on target"), "head": head}
        return _red(store, repo, target, head, rec["base"], rec["job_ids"],
                    {"ok": False, "summary": "revert interrupted; retried after restart"}, deploy_fn, notify_fn)
    if not _is_ancestor(repo, head, target):
        store.kv_set(*KEY, {})
        return {**_fail("stale landing record cleared (train head not on target)"), "head": head}
    # The pre-flight (incl. health) only applies while no deploy was ever attempted: after an interrupted attempt the
    # new code may be what turned health red, so it must be judged by a deploy (red -> revert), never deferred.
    # deploy-self still refuses for transient reasons itself (lock, answer in flight) -> deferred below.
    if not rec.get("attempted"):
        why = _preflight(preflight_fn)
        if why:
            return _defer(store, rec, f"deploy not possible: {why}")
    rec = {k: v for k, v in rec.items() if k != "deferred"}   # this attempt is a real one: a death during it counts
    store.kv_set(*KEY, {**rec, "attempted": True})
    dep = _deploy(deploy_fn)
    if _deferred(dep):                                    # restarted nothing: `attempted` stays what it was
        return _defer(store, rec, str(dep.get("summary", "")), dep)
    if dep is not None and not dep.get("ok"):
        return _red(store, repo, target, head, rec["base"], rec["job_ids"], dep, deploy_fn, notify_fn)
    _record(store, target, head, rec["base"], rec["job_ids"])
    return {"ok": True, "reason": "", "landed": list(rec["job_ids"]), "head": head, "reverted": False,
            "deploy": dep, "resumed": True}
