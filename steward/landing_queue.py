"""Landing-train queue (2026-10-08 spec): ACCEPTED jobs waiting to land, FIFO by enqueue time."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from steward import incorporate as inc
from steward.models import JobState, Judgment
from steward.store import Store, now_iso

MAX_DROPS = 3
RETRY_PARKS = 1   # parks retried on a moved target; from the next park on the train's one rebase fix-up takes over


@dataclass(frozen=True)
class Entry:
    job_id: str
    repo: str
    status: str
    attempts: int
    last_reason: str
    landed_commit: str
    priority: int = 0
    last_target: str = ""     # target head at the last drop / park (the parked retry compares it)
    parks: int = 0            # times the entry entered `parked`
    fixup: str = ""           # the one rebase fix-up the train started for it (a job id, or a FIXUP_* marker)


def _key(repo) -> str:
    return str(Path(str(repo)).resolve())


def _row(r) -> Entry:
    return Entry(r["job_id"], r["repo"], r["status"], r["attempts"], r["last_reason"], r["landed_commit"], r["priority"], r["last_target"],
                 r["parks"], r["fixup"])


def enqueue(store: Store, job_id: str, repo, *, origin: str = "accept", priority: int = 0) -> bool:
    """Queue a job (a `removed` row comes back as a fresh entry: the job was re-accepted)."""
    now = now_iso()
    with store._conn:
        cur = store._conn.execute(
            "INSERT INTO landing_queue(job_id, repo, enqueued_at, updated_at, priority) VALUES (?,?,?,?,?) "
            "ON CONFLICT(job_id) DO UPDATE SET status='queued', attempts=0, last_reason='', last_target='', parks=0, "
            "fixup='', repo=excluded.repo, "
            "enqueued_at=excluded.enqueued_at, updated_at=excluded.updated_at, priority=excluded.priority WHERE landing_queue.status='removed'",
            (job_id, _key(repo), now, now, int(priority)))
    if cur.rowcount != 1:
        if int(priority) <= 0:
            return False
        with store._conn:  # already queued: a fix-merge / incorporate moves it to the head
            bump = store._conn.execute(
                "UPDATE landing_queue SET priority=?, updated_at=? WHERE job_id=? AND status='queued' AND priority<?",
                (int(priority), now, job_id, int(priority)))
        if bump.rowcount != 1:
            return False
        store.append_event(job_id, what="landing_enqueued", evidence={"origin": origin, "priority": int(priority), "bumped": True},
                           rule="landing_queue", judgment=Judgment.code)
        return True
    store.append_event(job_id, what="landing_enqueued", evidence={"origin": origin, "priority": int(priority)},
                       rule="landing_queue", judgment=Judgment.code)
    return True


def sync_accepted(store: Store, repo) -> list[str]:
    key, added = _key(repo), []
    for job in store.list_jobs({JobState.ACCEPTED}):
        if _key(job.repo) != key or inc.is_incorporated(store, job.job_id):
            continue
        if enqueue(store, job.job_id, key, origin="sync"):
            added.append(job.job_id)
    return added


def get(store: Store, job_id: str) -> Entry | None:
    r = store._conn.execute("SELECT * FROM landing_queue WHERE job_id=?", (job_id,)).fetchone()
    return _row(r) if r else None


def queued(store: Store, repo, limit: int) -> list[Entry]:
    rows = store._conn.execute(
        "SELECT * FROM landing_queue WHERE repo=? AND status='queued' ORDER BY priority DESC, enqueued_at, job_id LIMIT ?",
        (_key(repo), int(limit))).fetchall()
    return [_row(r) for r in rows]


def parked(store: Store, repo) -> list[Entry]:
    rows = store._conn.execute(
        "SELECT * FROM landing_queue WHERE repo=? AND status='parked' ORDER BY updated_at", (_key(repo),)).fetchall()
    return [_row(r) for r in rows]


def mark_landed(store: Store, job_id: str, commit: str) -> None:
    with store._conn:
        store._conn.execute(
            "UPDATE landing_queue SET status='landed', landed_commit=?, updated_at=? WHERE job_id=?",
            (commit, now_iso(), job_id))


def mark_dropped(store: Store, job_id: str, reason: str, *, target_head: str = "") -> Entry:
    """One more drop; the MAX_DROPS-th parks the entry (`parks` counts entries into parked). `target_head` is the
    target the drop happened against ("" = unknown: the parked retry then treats any head as moved)."""
    with store._conn:
        store._conn.execute(
            "UPDATE landing_queue SET attempts=attempts+1, last_reason=?, updated_at=?, last_target=?, "
            "parks=parks + CASE WHEN status<>'parked' AND attempts+1 >= ? THEN 1 ELSE 0 END, "
            "status=CASE WHEN attempts+1 >= ? THEN 'parked' ELSE status END WHERE job_id=?",
            (reason, now_iso(), str(target_head or ""), MAX_DROPS, MAX_DROPS, job_id))
    return get(store, job_id)


def park(store: Store, job_id: str, reason: str, *, target_head: str = "") -> Entry:
    """Take a job out of the queue at once (e.g. its train was reverted): status parked, reason and target recorded."""
    with store._conn:
        store._conn.execute(
            "UPDATE landing_queue SET parks=parks + CASE WHEN status<>'parked' THEN 1 ELSE 0 END, status='parked', "
            "last_reason=?, last_target=?, updated_at=? WHERE job_id=?",
            (reason, str(target_head or ""), now_iso(), job_id))
    return get(store, job_id)


def set_parks(store: Store, job_id: str, n: int) -> None:
    with store._conn:
        store._conn.execute("UPDATE landing_queue SET parks=?, updated_at=? WHERE job_id=?", (int(n), now_iso(), job_id))


def set_fixup(store: Store, job_id: str, child_id: str) -> None:
    with store._conn:
        store._conn.execute("UPDATE landing_queue SET fixup=?, updated_at=? WHERE job_id=?",
                            (str(child_id or ""), now_iso(), job_id))


def claim_fixup(store: Store, job_id: str, marker: str) -> bool:
    """Take the entry's one rebase fix-up slot (only while it has none): True for the caller that took it."""
    with store._conn:
        cur = store._conn.execute("UPDATE landing_queue SET fixup=?, updated_at=? WHERE job_id=? AND fixup=''",
                                  (marker, now_iso(), job_id))
    return cur.rowcount == 1


def requeue_parked(store: Store, repo, target_head: str) -> list[str]:
    """Parked entries of `repo` parked against another target head go back to `queued`, attempts and parks kept
    (one more drop parks them again). Only the first park is retried: from the second the entry waits on its one
    rebase fix-up (an active target that moves every train would otherwise retry it for ever, never asking).
    Returns the job ids."""
    if not target_head:
        return []
    key, now = _key(repo), now_iso()
    with store._conn:
        rows = store._conn.execute(
            "SELECT job_id, last_target, attempts FROM landing_queue WHERE repo=? AND status='parked' "
            "AND last_target<>? AND fixup='' AND parks<=? ORDER BY updated_at, job_id",
            (key, target_head, RETRY_PARKS)).fetchall()
        for r in rows:
            store._conn.execute("UPDATE landing_queue SET status='queued', updated_at=? WHERE job_id=? AND status='parked'",
                                (now, r["job_id"]))
    for r in rows:
        store.append_event(r["job_id"], what="landing_enqueued",
                           evidence={"origin": "parked_retry", "priority": 0, "requeued_from": "parked",
                                     "last_target": r["last_target"], "target_head": target_head,
                                     "attempts": r["attempts"]},
                           rule="landing_queue", judgment=Judgment.code)
    return [r["job_id"] for r in rows]


def remove(store: Store, job_id: str, reason: str) -> Entry | None:
    """Take an entry out of consideration because its job no longer wants landing (not ACCEPTED any more).
    Re-accepting the job queues it again (`enqueue`)."""
    with store._conn:
        store._conn.execute(
            "UPDATE landing_queue SET status='removed', last_reason=?, updated_at=? WHERE job_id=?",
            (reason, now_iso(), job_id))
    return get(store, job_id)


def prune(store: Store, repo) -> list[str]:
    """Re-check every queued entry: a job incorporated some other way is marked landed (its commit), a job that is
    gone or no longer ACCEPTED is removed. Returns the job ids taken out."""
    out = []
    for e in queued(store, repo, 1_000_000):
        commit = inc.is_incorporated(store, e.job_id)
        if commit:
            mark_landed(store, e.job_id, str(commit))
            inc.release_on_incorporation(store, e.job_id)
            out.append(e.job_id)
            continue
        job = store.get_job(e.job_id)
        if job is None or job.state != JobState.ACCEPTED:
            remove(store, e.job_id, "job is gone" if job is None else f"job is {job.state.value}")
            out.append(e.job_id)
    return out


def route_to_train(store: Store, job_id: str, repo, *, origin: str) -> dict:
    """fix-merge / incorporate on a train repo: queue the job at the head instead of merging directly."""
    cur = get(store, job_id)
    if cur is not None and cur.status == "landed":
        return {"accepted": False, "queued": False, "detail": f"already landed ({cur.landed_commit[:8] or 'no commit recorded'})"}
    if cur is not None and cur.status == "parked":  # back to the head; counters are left to the park logic
        with store._conn:
            store._conn.execute("UPDATE landing_queue SET status='queued', priority=1, updated_at=? WHERE job_id=?",
                                (now_iso(), job_id))
        store.append_event(job_id, what="landing_enqueued", evidence={"origin": origin, "priority": 1, "requeued_from": "parked"},
                           rule="landing_queue", judgment=Judgment.code)
    elif cur is None or cur.status in ("queued", "removed"):
        enqueue(store, job_id, repo, origin=origin, priority=1)
    else:
        return {"accepted": False, "queued": False, "detail": f"cannot queue: entry is {cur.status}"}
    ids = [e.job_id for e in queued(store, repo, 1_000_000)]
    position = ids.index(job_id) + 1 if job_id in ids else 0
    return {"accepted": True, "queued": True, "position": position, "via": "landing train",
            "detail": "queued for the next train (head of queue)"}
