"""Transactional lane leases with epoch fencing (Phase 27, FLEET-06/08, SP-14).

Every operation is one `Store.immediate()` (`BEGIN IMMEDIATE`) transaction, so concurrent claimers
serialize in SQLite. A lane's lease lives in `kv` (`lease:<lane>`); its monotonically increasing
epoch lives in the mutable `fleet_lanes.lease_epoch` column and never goes backwards. Expiry is
advisory only: a lease whose writer is not *confirmed* stopped is never replaced, whatever the
clock says. An unconfirmed release keeps the epoch fenced (`uncertain`).

The integration-writer role has its own exclusive lease namespace (`lease:__integration__`);
Gate 3 will drive it, this module only guarantees exclusivity.
"""

from __future__ import annotations

import json

from steward import masking
from steward.store import Store, now_iso

HARD_LANE_CAP = 4  # v2.5 (FLEET-31): raised from the first-release 2 after the live multi-runtime proof
DEFAULT_LANES = 1
LANE_MAX_RECOVERIES = 2
DEFAULT_JOB_RECOVERY_CEILING = 3
INTEGRATION = "__integration__"

# States in which a process tree may still be writing (or might be): the lease is held.
HELD = ("reserved", "running", "stopping", "uncertain")


class LeaseError(Exception):
    pass


class LeaseHeld(LeaseError):
    """The lane (or integration role, or lane cap) is held; replacement is not allowed."""


class LeaseFenced(LeaseError):
    """The caller's epoch/attempt is not the current one for this lane."""


def effective_max_lanes(requested: int | None) -> int:
    return max(1, min(int(requested or DEFAULT_LANES), HARD_LANE_CAP))


def _key(lane_id: str) -> str:
    return f"lease:{lane_id}"


def _event(tx, store: Store, job_id: str, what: str, lane_id, attempt_no, evidence: dict) -> None:
    tx.conn.execute(
        "INSERT INTO fleet_events (job_id, ts, what, lane_id, attempt_no, actor, rule, judgment, evidence)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (job_id, now_iso(), masking.mask(what), lane_id, attempt_no, "controller", "lane_lease", "code",
         json.dumps(masking.mask_obj(evidence))))


def get(store: Store, job_id: str, lane_id: str) -> dict | None:
    return store.kv_get(job_id, _key(lane_id))


def live(store: Store, job_id: str) -> list[dict]:
    """Held lane leases (excluding the integration role), ordered by lane id."""
    out = []
    rows = store._conn.execute("SELECT lane_id FROM fleet_lanes WHERE job_id=? ORDER BY lane_id", (job_id,)).fetchall()
    for row in rows:
        lease = get(store, job_id, row["lane_id"])
        if lease and lease["state"] in HELD:
            out.append(lease)
    return out


def acquire_lane(store: Store, job_id: str, lane_id: str, *, owner: str = "controller",
                 max_lanes: int | None = None) -> dict:
    """Reserve the next attempt for a lane. Raises LeaseHeld if the lane's previous attempt is not
    confirmed stopped, if another lane already holds the job's concurrency allowance, or if the lane
    is unknown. Returns the new lease (state `reserved`)."""
    cap = effective_max_lanes(max_lanes)
    masking.arm_job(store, job_id)
    with store.immediate() as tx:
        row = tx.conn.execute("SELECT lease_epoch FROM fleet_lanes WHERE job_id=? AND lane_id=?",
                              (job_id, lane_id)).fetchone()
        if row is None:
            raise LeaseError(f"unknown lane {lane_id}")
        prev = tx.kv_get(job_id, _key(lane_id))
        if prev and prev["state"] in HELD:
            raise LeaseHeld(f"lane {lane_id} attempt {prev['attempt_no']} epoch {prev['epoch']} is {prev['state']};"
                            " not confirmed stopped")
        held = 0
        for other in tx.conn.execute("SELECT lane_id FROM fleet_lanes WHERE job_id=? AND lane_id<>?",
                                     (job_id, lane_id)):
            lease = tx.kv_get(job_id, _key(other["lane_id"]))
            if lease and lease["state"] in HELD:
                held += 1
        if held >= cap:
            raise LeaseHeld(f"lane cap {cap} reached for job {job_id}")
        epoch = row["lease_epoch"] + 1
        attempt_no = (prev["attempt_no"] if prev else 0) + 1
        lease = {"job_id": job_id, "lane_id": lane_id, "epoch": epoch, "attempt_no": attempt_no,
                 "state": "reserved", "owner": owner, "identity": None, "reserved_at": now_iso(),
                 "expires_at": None, "stop_evidence": None}
        tx.kv_set(job_id, _key(lane_id), lease)
        tx.conn.execute("UPDATE fleet_lanes SET lease_epoch=?, attempt_no=?, state='reserved', version=version+1"
                        " WHERE job_id=? AND lane_id=?", (epoch, attempt_no, job_id, lane_id))
        _event(tx, store, job_id, "lane lease acquired", lane_id, attempt_no, {"epoch": epoch, "owner": owner})
    return lease


def _check(lease: dict | None, epoch: int, attempt_no: int | None = None) -> dict:
    if lease is None or lease["epoch"] != epoch or (attempt_no is not None and lease["attempt_no"] != attempt_no):
        raise LeaseFenced("stale lease epoch/attempt")
    return lease


def bind(store: Store, job_id: str, lane_id: str, epoch: int, identity: dict) -> dict:
    """reserved -> running, recording the observed process/pane identity. Compare-and-swap on epoch."""
    with store.immediate() as tx:
        lease = _check(tx.kv_get(job_id, _key(lane_id)), epoch)
        if lease["state"] != "reserved":
            raise LeaseFenced(f"lease is {lease['state']}, not reserved")
        lease = {**lease, "state": "running", "identity": dict(identity), "bound_at": now_iso()}
        tx.kv_set(job_id, _key(lane_id), lease)
        tx.conn.execute("UPDATE fleet_lanes SET state='running', version=version+1 WHERE job_id=? AND lane_id=?",
                        (job_id, lane_id))
        _event(tx, store, job_id, "lane lease bound", lane_id, lease["attempt_no"],
               {"epoch": epoch, "identity": identity})
    return lease


def renew(store: Store, job_id: str, lane_id: str, epoch: int, expires_at: str) -> dict:
    """Advisory expiry bookkeeping. Expiry never makes a held lease replaceable."""
    with store.immediate() as tx:
        lease = _check(tx.kv_get(job_id, _key(lane_id)), epoch)
        lease = {**lease, "expires_at": expires_at}
        tx.kv_set(job_id, _key(lane_id), lease)
    return lease


def mark(store: Store, job_id: str, lane_id: str, epoch: int, state: str, evidence: dict) -> dict:
    """Move a held lease between held states (`stopping`/`uncertain`) with evidence; epoch-checked."""
    if state not in HELD:
        raise ValueError(state)
    with store.immediate() as tx:
        lease = _check(tx.kv_get(job_id, _key(lane_id)), epoch)
        if lease["state"] not in HELD:
            raise LeaseFenced(f"lease already {lease['state']}")
        lease = {**lease, "state": state, "stop_evidence": evidence}
        tx.kv_set(job_id, _key(lane_id), lease)
        tx.conn.execute("UPDATE fleet_lanes SET state=?, version=version+1 WHERE job_id=? AND lane_id=?",
                        (state, job_id, lane_id))
        _event(tx, store, job_id, f"lane lease {state}", lane_id, lease["attempt_no"],
               {"epoch": epoch, "evidence": evidence})
    return lease


def release(store: Store, job_id: str, lane_id: str, epoch: int, *, confirmed: bool, evidence: dict) -> dict:
    """Release a lease. Only confirmed process-tree stop frees the lane; otherwise it stays fenced
    (`uncertain`) and keeps its epoch. A stale epoch cannot release the current lease."""
    if confirmed and not evidence:
        raise ValueError("confirmed stop requires evidence")
    with store.immediate() as tx:
        lease = _check(tx.kv_get(job_id, _key(lane_id)), epoch)
        state = "stopped" if confirmed else "uncertain"
        lease = {**lease, "state": state, "stop_evidence": evidence, "released_at": now_iso()}
        tx.kv_set(job_id, _key(lane_id), lease)
        tx.conn.execute("UPDATE fleet_lanes SET state=?, version=version+1 WHERE job_id=? AND lane_id=?",
                        ("stopped" if confirmed else "uncertain", job_id, lane_id))
        _event(tx, store, job_id, "lane lease released" if confirmed else "stop_unconfirmed", lane_id,
               lease["attempt_no"], {"epoch": epoch, "evidence": evidence})
    return lease


# -- integration-writer role (exclusive per job; no integration behavior here) ---------------------------

def acquire_integration(store: Store, job_id: str, *, holder: str) -> dict:
    masking.arm_job(store, job_id)
    with store.immediate() as tx:
        cur = tx.kv_get(job_id, _key(INTEGRATION))
        if cur and cur["state"] in HELD:
            raise LeaseHeld(f"integration writer held by {cur['owner']}")
        for row in tx.conn.execute("SELECT lane_id FROM fleet_lanes WHERE job_id=?", (job_id,)):
            lease = tx.kv_get(job_id, _key(row["lane_id"]))
            if lease and lease["state"] in ("stopping", "uncertain"):
                raise LeaseHeld(f"lane {row['lane_id']} stop is {lease['state']}; integration writer blocked")
        epoch = tx.kv_get(job_id, "lease_epoch:" + INTEGRATION, 0) + 1
        tx.kv_set(job_id, "lease_epoch:" + INTEGRATION, epoch)
        lease = {"job_id": job_id, "lane_id": INTEGRATION, "epoch": epoch, "attempt_no": epoch,
                 "state": "running", "owner": holder, "identity": None, "stop_evidence": None}
        tx.kv_set(job_id, _key(INTEGRATION), lease)
        _event(tx, store, job_id, "integration lease acquired", INTEGRATION, epoch, {"epoch": epoch, "holder": holder})
    return lease


def release_integration(store: Store, job_id: str, epoch: int, *, confirmed: bool, evidence: dict) -> dict:
    with store.immediate() as tx:
        lease = _check(tx.kv_get(job_id, _key(INTEGRATION)), epoch)
        lease = {**lease, "state": "stopped" if confirmed else "uncertain", "stop_evidence": evidence}
        tx.kv_set(job_id, _key(INTEGRATION), lease)
        _event(tx, store, job_id, "integration lease released" if confirmed else "stop_unconfirmed",
               INTEGRATION, epoch, {"epoch": epoch, "evidence": evidence})
    return lease


# -- recovery accounting (per lane + job-wide, idempotent by action id) ----------------------------------

def spend_recovery(store: Store, job_id: str, lane_id: str, action_id: str, *,
                   lane_max: int = LANE_MAX_RECOVERIES, job_max: int = DEFAULT_JOB_RECOVERY_CEILING) -> tuple[bool, str]:
    """Atomically spend one recovery. Returns (granted, reason). A repeated action id returns the
    original decision without spending again."""
    with store.immediate() as tx:
        book = tx.kv_get(job_id, "fleet_recoveries", {"lanes": {}, "job": 0, "actions": {}})
        if action_id in book["actions"]:
            return tuple(book["actions"][action_id])  # type: ignore[return-value]
        used = book["lanes"].get(lane_id, 0)
        if used >= lane_max:
            result = [False, "lane_recovery_exhausted"]
        elif book["job"] >= job_max:
            result = [False, "job_recovery_ceiling"]
        else:
            book["lanes"][lane_id] = used + 1
            book["job"] += 1
            result = [True, "granted"]
        book["actions"][action_id] = result
        tx.kv_set(job_id, "fleet_recoveries", book)
        _event(tx, store, job_id, "lane recovery " + ("granted" if result[0] else "refused"), lane_id, None,
               {"action_id": action_id, "reason": result[1], "lane_used": book["lanes"].get(lane_id, 0),
                "job_used": book["job"]})
    return result[0], result[1]


def recoveries(store: Store, job_id: str) -> dict:
    return store.kv_get(job_id, "fleet_recoveries", {"lanes": {}, "job": 0, "actions": {}})


# -- fenced task-state writes ----------------------------------------------------------------------------

def set_task_state(store: Store, job_id: str, lane_id: str, epoch: int, attempt_no: int, task_id: str,
                   state: str) -> None:
    """Record a task's lane-derived state only if (lane, epoch, attempt) is still the active held lease.
    A stale epoch raises LeaseFenced and changes nothing."""
    with store.immediate() as tx:
        lease = _check(tx.kv_get(job_id, _key(lane_id)), epoch, attempt_no)
        if lease["state"] not in HELD:
            raise LeaseFenced(f"lease is {lease['state']}")
        states = tx.kv_get(job_id, "fleet_task_state", {})
        states[task_id] = state
        tx.kv_set(job_id, "fleet_task_state", states)
        _event(tx, store, job_id, f"task {state}", lane_id, attempt_no, {"epoch": epoch, "task": task_id})


def force_task_state(store: Store, job_id: str, task_id: str, state: str) -> None:
    """Controller-originated state change not tied to a live attempt (e.g. recovery exhausted)."""
    with store.immediate() as tx:
        states = tx.kv_get(job_id, "fleet_task_state", {})
        states[task_id] = state
        tx.kv_set(job_id, "fleet_task_state", states)


def task_states(store: Store, job_id: str) -> dict:
    return store.kv_get(job_id, "fleet_task_state", {})
