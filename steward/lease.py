"""Single-writer lease over the worktree (SP-14).

A replacement worker may not write until the previous attempt and its
children are confirmed stopped. Persisted through `Store.kv_get`/`kv_set`
(the `kv` table) rather than a dedicated `attempts` store API — no
`steward/store.py` change is needed for this phase, and the lease still
survives a controller restart because `kv` is durable SQLite.
"""

from __future__ import annotations

from steward.models import Judgment
from steward.store import Store, now_iso

_ATTEMPTS_KEY = "attempts"

_HELD_STATUSES = ("running", "stopping")


class LeaseHeld(Exception):
    """Raised when a previous attempt has not been confirmed stopped."""


def _load(store: Store, job_id: str) -> list[dict]:
    return store.kv_get(job_id, _ATTEMPTS_KEY, default=[])


def _save(store: Store, job_id: str, attempts: list[dict]) -> None:
    store.kv_set(job_id, _ATTEMPTS_KEY, attempts)


def acquire(store: Store, job_id: str, worker_ref: str) -> int:
    """Acquire the next attempt number, raising `LeaseHeld` if unavailable."""
    held = takeover_holder(store, job_id)
    if held:  # v2.6: a tech-lead takeover is the worktree's one writer until it releases
        raise LeaseHeld(f"tech-lead takeover by {held.get('holder')} holds the worktree of job {job_id}")
    attempts = _load(store, job_id)
    for attempt in attempts:
        if attempt["status"] in _HELD_STATUSES and not attempt["confirmed_stopped"]:
            raise LeaseHeld(
                f"attempt {attempt['attempt_no']} not confirmed stopped for job {job_id}"
            )

    attempt_no = len(attempts) + 1
    ts = now_iso()
    attempts.append(
        {
            "attempt_no": attempt_no,
            "worker_ref": worker_ref,
            "status": "running",
            "started_at": ts,
            "stopped_at": None,
            "confirmed_stopped": False,
        }
    )
    _save(store, job_id, attempts)
    store.append_event(
        job_id,
        what="attempt lease acquired",
        evidence={"attempt_no": attempt_no, "worker_ref": worker_ref},
        rule="single_writer_lease",
        judgment=Judgment.code,
    )
    return attempt_no


def release(store: Store, job_id: str, attempt_no: int, confirmed: bool) -> None:
    """Release `attempt_no`. An unconfirmed stop keeps the lease held (SP-11)."""
    attempts = _load(store, job_id)
    ts = now_iso()
    for attempt in attempts:
        if attempt["attempt_no"] != attempt_no:
            continue
        attempt["status"] = "stopped" if confirmed else "stopping"
        attempt["stopped_at"] = ts
        attempt["confirmed_stopped"] = confirmed
        _save(store, job_id, attempts)
        if confirmed:
            store.append_event(
                job_id,
                what="attempt lease released",
                evidence={"attempt_no": attempt_no},
                rule="single_writer_lease",
                judgment=Judgment.code,
            )
        else:
            store.append_event(
                job_id,
                what="stop_unconfirmed",
                evidence={"attempt_no": attempt_no},
                rule="single_writer_lease",
                judgment=Judgment.code,
            )
        return
    raise KeyError(f"no attempt {attempt_no} for job {job_id}")


# -- v2.6 (PRD D-02, SUP-12): the tech lead's takeover is the worktree's ONE writer ------------------------------

_TAKEOVER_KEY = "takeover_lease"


def takeover_holder(store: Store, job_id: str) -> dict | None:
    rec = store.kv_get(job_id, _TAKEOVER_KEY)
    return rec if isinstance(rec, dict) and rec.get("holder") else None


def takeover_acquire(store: Store, job_id: str, holder: str, *, worker_writing: bool) -> dict:
    """Take the worktree for a tech-lead takeover. Refused (`LeaseHeld`) while another takeover holds it or while the
    worker's attempt is a live writer (`worker_writing`: a turn or a tool call still running after the pause). The
    worker's attempt itself stays recorded; while this is held no new attempt may start and the runner neither
    dispatches nor delivers to the worker."""
    cur = takeover_holder(store, job_id)
    if cur:
        raise LeaseHeld(f"takeover already held by {cur['holder']}")
    if worker_writing:
        raise LeaseHeld("the worker's attempt is still a live writer")
    running = [a for a in _load(store, job_id) if a["status"] in _HELD_STATUSES and not a["confirmed_stopped"]]
    rec = {"holder": holder, "at": now_iso(), "worker_attempt": running[-1]["attempt_no"] if running else None}
    store.kv_set(job_id, _TAKEOVER_KEY, rec)
    store.append_event(job_id, what="takeover lease acquired", evidence=rec, rule="single_writer_lease",
                       judgment=Judgment.code)
    return rec


def takeover_release(store: Store, job_id: str, holder: str) -> None:
    cur = takeover_holder(store, job_id)
    if not cur:
        return
    if cur["holder"] != holder:
        raise LeaseHeld(f"takeover is held by {cur['holder']}, not {holder}")
    store.kv_set(job_id, _TAKEOVER_KEY, {})
    store.append_event(job_id, what="takeover lease released", evidence={"holder": holder},
                       rule="single_writer_lease", judgment=Judgment.code)
