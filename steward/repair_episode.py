"""Moving-target merge repair (continuous job ownership §9; Plan 4; merge-housekeeping Defect A).

One episode per root job (the original, non-fix-up job). When its merge — or its current attempt's merge — fails,
`decide` re-reads the target and chooses: retry in place (the target moved while the attempt ran), coalesce (a live
attempt already carries the repair), strategy review (the same conflict at the same target content came back, or the
attempt allowance is used: unchanged evidence buys no new job), or a new attempt for a genuinely new conflict.
Attempts are children of the ROOT, started from the latest attempt's branch so earlier resolution work is kept;
never a fix-up of a fix-up. Resolution marks earlier attempts incorporated only when git proves their commits landed.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass

from steward import incorporate as inc
from steward.models import JobState, Judgment
from steward.store import Store

ATTEMPTS_BEFORE_STRATEGY = 2
HISTORY_MAX = 50
OPEN_STATES = ("open", "repairing", "strategy_review")
START_REF_KEY = "repair_start_ref"
EPISODE_KEY = "repair_episode"
_LIVE = (JobState.CREATED, JobState.CONTRACTED, JobState.RUNNING, JobState.PAUSED, JobState.WORKER_FINISHED,
         JobState.CHECKS_PASSED, JobState.NEEDS_HUMAN)


@dataclass(frozen=True)
class Episode:
    episode_id: str
    root_job: str
    repo: str
    target_branch: str
    state: str
    current_job: str | None
    attempts: int
    target_head: str | None
    conflict_digest: str | None
    conflicts: list
    history: list
    created_epoch: float
    updated_epoch: float
    resolved_epoch: float | None
    resolution: str | None


def enabled() -> bool:
    from steward import ownership

    return ownership.enabled()


def _row(r) -> Episode:
    return Episode(r["episode_id"], r["root_job"], r["repo"], r["target_branch"], r["state"], r["current_job"],
                   int(r["attempts"]), r["target_head"], r["conflict_digest"], json.loads(r["conflicts_json"] or "[]"),
                   json.loads(r["history_json"] or "[]"), float(r["created_epoch"]), float(r["updated_epoch"]),
                   r["resolved_epoch"], r["resolution"])


def get(store: Store, episode_id: str) -> Episode | None:
    r = store._conn.execute("SELECT * FROM repair_episodes WHERE episode_id = ?", (episode_id,)).fetchone()
    return None if r is None else _row(r)


def open_for(store: Store, root_job: str) -> Episode | None:
    r = store._conn.execute("SELECT * FROM repair_episodes WHERE root_job = ? AND state IN (?, ?, ?)",
                            (root_job, *OPEN_STATES)).fetchone()
    return None if r is None else _row(r)


def root_of(store: Store, job_id: str) -> str:
    cur, seen = job_id, set()
    while cur not in seen:
        seen.add(cur)
        parent = store.kv_get(cur, "autopilot_followup")
        if not isinstance(parent, str) or not parent or store.get_job(parent) is None:
            return cur
        cur = parent
    return cur


def ensure(store: Store, root_job: str, *, repo: str, target_branch: str, now: float) -> Episode:
    ep = open_for(store, root_job)
    if ep is not None:
        return ep
    try:
        with store._conn:
            store._conn.execute(
                "INSERT INTO repair_episodes (episode_id, root_job, repo, target_branch, state, created_epoch,"
                " updated_epoch) VALUES (?,?,?,?,'open',?,?)",
                ("rep-" + uuid.uuid4().hex[:12], root_job, repo, target_branch, now, now))
    except sqlite3.IntegrityError:  # another process opened it first: one open episode per root
        pass
    return open_for(store, root_job)


def _update(store: Store, episode_id: str, **cols) -> Episode:
    sets = ", ".join(f"{k} = ?" for k in cols)
    with store._conn:
        store._conn.execute(f"UPDATE repair_episodes SET {sets} WHERE episode_id = ?", (*cols.values(), episode_id))
    return get(store, episode_id)


def note(store: Store, episode_id: str, event: str, detail: str, *, now: float, **extra) -> Episode:
    ep = get(store, episode_id)
    hist = (ep.history + [{"at": now, "event": event, "detail": detail[:300], **extra}])[-HISTORY_MAX:]
    return _update(store, episode_id, history_json=json.dumps(hist), updated_epoch=now)


def _git(repo, *args) -> str:
    r = inc._git(repo, *args)
    return r.stdout.strip() if r.returncode == 0 else ""


def conflict_digest(repo, target_ref: str, files) -> str:
    """Identity of a conflict: the conflicting paths and the target's content of each (a moved target that changed
    those files is a new conflict; the same content coming back is unchanged evidence)."""
    parts = [f"{f}:{_git(repo, 'rev-parse', f'{target_ref}:{f}')}" for f in sorted(set(files))]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def _strategy_resolved(store: Store, ep: Episode) -> bool:
    from steward import blockers

    ids = [h.get("blocker_id") for h in ep.history if h.get("event") == "strategy_review" and h.get("blocker_id")]
    if not ids:
        return True
    b = blockers.get(store, ids[-1])
    return b is None or not b.is_open


def _allowance(ep: Episode) -> int:
    return ATTEMPTS_BEFORE_STRATEGY * (1 + sum(1 for h in ep.history if h.get("event") == "strategy_review_resolved"))


def _request_strategy(store: Store, ep: Episode, reason: str, *, now: float) -> dict:
    from steward import ownership

    b = ownership.request_strategy_review(store, ep.root_job, blocker_id=None, reason=reason,
                                          signature=f"repair:{ep.episode_id}", now=now)
    ep = _update(store, ep.episode_id, state="strategy_review", updated_epoch=now)
    ep = note(store, ep.episode_id, "strategy_review", reason, now=now, blocker_id=b.blocker_id)
    return {"action": "strategy_review", "episode": ep, "start_ref": None, "reason": reason, "digest": None}


def decide(store: Store, job, pf, *, detail: str, files, now: float, pf_target_head: str | None,
           max_depth: int | None = None) -> dict:
    """`max_depth` caps the attempts of the episode (the landing train passes 1): once that many were started, a
    further new attempt is refused with action "ask" (the caller's owner ask). None = no cap (today's rules)."""
    root = root_of(store, job.job_id)
    ep = ensure(store, root, repo=job.repo, target_branch=pf.source_branch, now=now)
    head = _git(job.repo, "rev-parse", "HEAD")
    if pf_target_head and head != pf_target_head:
        ep = note(store, ep.episode_id, "retry_in_place", f"target moved {pf_target_head[:12]} -> {head[:12]}", now=now)
        return {"action": "retry_in_place", "episode": ep, "start_ref": None, "digest": None,
                "reason": "the target moved while this attempt ran; retrying the merge in place"}
    cur = store.get_job(ep.current_job) if ep.current_job else None
    if cur is not None and cur.job_id != job.job_id and cur.state in _LIVE:
        ep = note(store, ep.episode_id, "coalesced", f"attempt {cur.job_id[:8]} is still {cur.state.value}", now=now)
        return {"action": "wait_attempt", "episode": ep, "start_ref": None, "digest": None,
                "reason": f"repair attempt {cur.job_id[:8]} is already on it"}
    if ep.state == "strategy_review":
        if not _strategy_resolved(store, ep):
            return {"action": "strategy_review", "episode": ep, "start_ref": None, "digest": None,
                    "reason": "a strategy review of this repair is pending"}
        ep = note(store, ep.episode_id, "strategy_review_resolved", "the lead decided; attempts may continue", now=now)
        ep = _update(store, ep.episode_id, state="repairing", updated_epoch=now)
    if max_depth is not None and ep.attempts >= int(max_depth):
        reason = f"repair depth cap reached ({ep.attempts}/{int(max_depth)} attempts): {detail}"
        ep = note(store, ep.episode_id, "depth_cap", reason, now=now)
        return {"action": "ask", "episode": ep, "start_ref": None, "digest": None, "reason": reason}
    digest = conflict_digest(job.repo, "HEAD", files) if files else \
        hashlib.sha256(f"red:{detail}:{_git(job.repo, 'rev-parse', 'HEAD^{tree}')}".encode()).hexdigest()[:16]
    if ep.attempts and digest == ep.conflict_digest:
        return _request_strategy(store, ep, f"attempt {ep.attempts} did not resolve the same failure ({detail}); "
                                            "unchanged evidence buys no new attempt", now=now)
    if ep.attempts >= _allowance(ep):
        return _request_strategy(store, ep, f"{ep.attempts} repair attempts used; independent diagnosis before more",
                                 now=now)
    start_ref = inc.branch_name(job.job_id) if job.job_id != root and _git(
        job.repo, "rev-parse", "--verify", "-q", f"refs/heads/{inc.branch_name(job.job_id)}") else None
    return {"action": "new_attempt", "episode": ep, "start_ref": start_ref, "digest": digest,
            "reason": f"new failure: {detail}"}


def record_attempt(store: Store, episode_id: str, child_id: str, *, start_ref: str | None, conflicts, digest: str,
                   now: float) -> Episode:
    ep = get(store, episode_id)
    head = _git(ep.repo, "rev-parse", "HEAD")
    _update(store, episode_id, state="repairing", current_job=child_id, attempts=ep.attempts + 1, target_head=head,
            conflict_digest=digest, conflicts_json=json.dumps(sorted(set(conflicts))), updated_epoch=now)
    store.kv_set(child_id, EPISODE_KEY, episode_id)
    return note(store, episode_id, "attempt", f"attempt {ep.attempts + 1}: {child_id[:8]} from {start_ref or 'root'}",
                now=now, child=child_id, start_ref=start_ref)


def _attempt_jobs(ep: Episode) -> list[str]:
    return [h["child"] for h in ep.history if h.get("event") == "attempt" and h.get("child")]


def resolve(store: Store, episode_id: str, *, merge_commit: str, now: float) -> list[str]:
    """The repair landed: mark every earlier attempt whose commits git proves are in `merge_commit` incorporated
    (and release it when ACCEPTED, like `incorporate.mark_parents_incorporated`). Returns the attempts marked."""
    from steward import autopilot

    ep = get(store, episode_id)
    marked = []
    for child in _attempt_jobs(ep):
        job = store.get_job(child)
        if job is None or inc.is_incorporated(store, child) or autopilot.no_merge_protection(store, child):
            continue
        if inc._git(ep.repo, "merge-base", "--is-ancestor", inc.branch_name(child), merge_commit).returncode != 0:
            continue
        store.kv_set(child, "incorporated_commit", merge_commit)
        store.append_event(child, what="incorporated_via_repair_episode",
                           evidence={"merge_commit": merge_commit, "episode": episode_id},
                           rule="autopilot_merge", judgment=Judgment.code)
        if job.state == JobState.ACCEPTED:
            store.set_state(child, JobState.RELEASED, what="auto-released: merged via its repair episode",
                            evidence={"episode": episode_id, "merge_commit": merge_commit},
                            rule="autopilot_merge", judgment=Judgment.human)  # owner rule: release when the fix merges
        marked.append(child)
    _update(store, episode_id, state="resolved", resolved_epoch=now, resolution=f"merged {merge_commit[:12]}",
            updated_epoch=now)
    note(store, episode_id, "resolved", f"merged {merge_commit[:12]}", now=now)
    return marked


def on_incorporated(store: Store, job_id: str, merge_commit: str, *, now: float) -> list[str]:
    ep = open_for(store, root_of(store, job_id))
    return resolve(store, ep.episode_id, merge_commit=merge_commit, now=now) if ep else []
