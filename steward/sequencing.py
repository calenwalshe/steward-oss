"""Job sequencing (owner, 2026-10-01: "if they are in a sequence we can order them one after the other").

A staged (CREATED/CONTRACTED) job can be queued behind another job (`after`) and/or on a shared resource `lock`.
`sweep` is the one place that launches queued jobs: the control pane runs it on every state-changing cmd and on its
periodic tick, so a launch happens when the predecessor reaches ACCEPTED (or is merged, with `after_merge`) and when a
lock frees. All state is per-job kv:

  seq_after        predecessor job id          seq_after_merge  wait for incorporation, not just ACCEPTED
  seq_lock         lock name                   seq_launch_args  launch args recorded at queue time (workspace...)
  seq_launched     claim marker: set BEFORE the launch, so a job launches exactly once (play sets it too)
  seq_note         last queue event text, so a paused/failed queue records one event, not one per sweep

The launcher is injected (`launcher(job_id, launch_args) -> {"accepted": bool, ...}`): production passes the control
pane's `_h_launch`, tests pass a fake.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any, Callable

from steward.models import JobState, Judgment

STAGED = frozenset({JobState.CREATED, JobState.CONTRACTED})
_HOLDING = frozenset({JobState.RUNNING, JobState.PAUSED, JobState.STOPPING, JobState.WORKER_FINISHED,
                      JobState.CHECKS_PASSED})
_PAUSING = frozenset({JobState.STOPPED, JobState.FAILED, JobState.NEEDS_HUMAN})
_ACCEPTED = frozenset({JobState.ACCEPTED, JobState.RELEASED})

K_AFTER, K_MERGE, K_LOCK = "seq_after", "seq_after_merge", "seq_lock"
K_ARGS, K_LAUNCHED, K_NOTE = "seq_launch_args", "seq_launched", "seq_note"
K_WAIT = "seq_wait"  # a lock-only job someone tried to start while its lock was held: it starts when the lock frees

_claim_lock = threading.Lock()


def _staged(store, job_id: str) -> bool:
    job = store.get_job(job_id)
    return job is not None and job.state in STAGED


def _name(store, job_id: str) -> str:
    from steward import tabnames

    job = store.get_job(job_id)
    return tabnames.job_name(store, job, persist=False) if job is not None else job_id[:8]


def _resolve(store, ref: str) -> str | None:
    """A full id or a unique id prefix -> the job id."""
    if store.get_job(ref) is not None:
        return ref
    hits = [j.job_id for j in store.list_jobs() if j.job_id.startswith(ref)]
    return hits[0] if len(hits) == 1 else None


# -- writes (relayed through the control pane) --------------------------------------------------------------

def set_after(store, job_id: str, pred: str, *, after_merge: bool = False, lock: str | None = None,
              launch_args: dict | None = None) -> tuple[bool, str]:
    job_id = _resolve(store, job_id) or job_id
    pred_id = _resolve(store, pred)
    if store.get_job(job_id) is None:
        return False, f"unknown job: {job_id}"
    if pred_id is None:
        return False, f"unknown predecessor: {pred}"
    if not _staged(store, job_id):
        return False, f"{job_id[:8]} is not staged (only CREATED/CONTRACTED jobs can be queued)"
    if pred_id == job_id:
        return False, "a job cannot wait for itself"
    cur, seen = pred_id, {job_id}
    while cur:  # a cycle would never launch
        if cur in seen:
            return False, f"that would make a cycle ({pred_id[:8]} already waits for {job_id[:8]})"
        seen.add(cur)
        cur = store.kv_get(cur, K_AFTER)
    store.kv_set(job_id, K_AFTER, pred_id)
    from steward import chain_branch

    inherited_target = chain_branch.target(store, pred_id)
    if inherited_target:
        chain_branch.record(store, job_id, inherited_target)
    store.kv_set(job_id, K_MERGE, bool(after_merge))
    if launch_args:
        store.kv_set(job_id, K_ARGS, dict(launch_args))
    if lock:
        store.kv_set(job_id, K_LOCK, lock)
    store.kv_delete(job_id, K_NOTE)
    return True, f"{job_id[:8]} queued after {pred_id[:8]}" + (" (merged)" if after_merge else "")


def set_lock(store, job_id: str, lock: str | None) -> tuple[bool, str]:
    job_id = _resolve(store, job_id) or job_id
    if store.get_job(job_id) is None:
        return False, f"unknown job: {job_id}"
    if lock:
        store.kv_set(job_id, K_LOCK, lock)
        return True, f"{job_id[:8]} holds lock {lock} while it runs"
    store.kv_delete(job_id, K_LOCK)
    return True, f"{job_id[:8]} lock cleared"


def clear(store, job_id: str) -> tuple[bool, str]:
    job_id = _resolve(store, job_id) or job_id
    if store.get_job(job_id) is None:
        return False, f"unknown job: {job_id}"
    for key in (K_AFTER, K_MERGE, K_ARGS, K_NOTE):
        store.kv_delete(job_id, key)
    return True, f"{job_id[:8]} taken out of its queue"


def queue(store, ids: list[str], *, after_merge: bool = False, lock: str | None = None,
          launch_args: dict | None = None) -> tuple[bool, str]:
    """`steward queue J1 J2 J3`: J2 after J1, J3 after J2. J1 is the head: it starts when played (or already runs)."""
    full = [_resolve(store, i) for i in ids]
    if None in full:
        return False, f"unknown job: {ids[full.index(None)]}"
    if len(full) < 2:
        return False, "queue needs at least two jobs"
    if len(set(full)) != len(full):
        return False, "a job can appear in a queue once"
    for prev, job in zip(full, full[1:]):
        ok, detail = set_after(store, job, prev, after_merge=after_merge, lock=lock, launch_args=launch_args)
        if not ok:
            return False, detail
    return True, " -> ".join(i[:8] for i in full)


def group_by_prefix(store, prefix: str, *, after_merge: bool = False) -> tuple[bool, str]:
    """`steward queue --group-by-prefix 'Recipe book'`: chain the jobs whose name (or goal title) starts with PREFIX,
    in natural order (Job 2 before Job 10), and name the queue PREFIX. Finished jobs and jobs already in a queue
    are left out."""
    want = prefix.strip().lower()

    def title(j) -> str:
        return (_name(store, j.job_id) if _name(store, j.job_id).lower().startswith(want) else j.goal).strip()

    def natural(j):
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", title(j))] + [j.created_at]

    found = [j for j in store.list_jobs()
             if title(j).lower().startswith(want) and j.state not in _DONE
             and j.state not in {JobState.STOPPED, JobState.FAILED} and not store.kv_get(j.job_id, K_AFTER)]
    found.sort(key=natural)
    if len(found) < 2:
        return False, f"fewer than two unfinished jobs start with {prefix!r}"
    ids = [j.job_id for j in found]
    ok, detail = queue(store, ids, after_merge=after_merge)
    if ok:
        store.kv_set(ids[0], K_QNAME, prefix.strip())
        detail = f"{prefix.strip()}: {detail}"
    return ok, detail


def move(store, job_id: str, direction: str) -> tuple[bool, str]:
    """Reorder within a queue: swap with the neighbour before ("up") or after ("down")."""
    job_id = _resolve(store, job_id) or job_id
    if direction == "down":
        succ = _successors(store, job_id)
        if not succ:
            return False, "already last in its queue"
        return move(store, succ[0], "up")
    pred = store.kv_get(job_id, K_AFTER)
    if not pred or not _staged(store, pred) or store.kv_get(pred, K_LAUNCHED):
        return False, "already first in its queue"
    grand = store.kv_get(pred, K_AFTER)
    succ = _successors(store, job_id)
    if grand:
        store.kv_set(job_id, K_AFTER, grand)
    else:
        store.kv_delete(job_id, K_AFTER)
    store.kv_set(pred, K_AFTER, job_id)
    for s in succ:
        store.kv_set(s, K_AFTER, pred)
    return True, f"{job_id[:8]} moved up (before {pred[:8]})"


def _successors(store, job_id: str) -> list[str]:
    jobs = sorted(store.list_jobs(set(STAGED)), key=lambda j: j.created_at)
    return [j.job_id for j in jobs if store.kv_get(j.job_id, K_AFTER) == job_id and not store.kv_get(j.job_id, K_LAUNCHED)]


def lock_gate(store, job_id: str) -> str | None:
    """Called before any launch. The lock is exclusive even for play: when another job holds it, the job is parked
    (it keeps waiting, now for the lock only) and the holder's id is returned; None = free to launch."""
    lock = store.kv_get(job_id, K_LOCK)
    holder = _lock_holder(store, lock, job_id) if lock else None
    if holder:
        store.kv_set(job_id, K_WAIT, True)
        store.kv_delete(job_id, K_AFTER)
    return holder


def on_manual_launch(store, job_id: str) -> None:
    """`steward launch`/play on a job: it jumps its queue (no longer waits) and can never be auto-launched again.
    Jobs queued behind it keep following it."""
    store.kv_set(job_id, K_LAUNCHED, True)
    store.kv_delete(job_id, K_AFTER)
    store.kv_delete(job_id, K_WAIT)
    from steward.governor import K_ROOM

    store.kv_delete(job_id, K_ROOM)


# -- reads --------------------------------------------------------------------------------------------------

def is_queued(store, job_id: str) -> bool:
    from steward.governor import K_ROOM

    waiting_on_lock = store.kv_get(job_id, K_LOCK) and store.kv_get(job_id, K_WAIT)
    waiting_on_room = store.kv_get(job_id, K_ROOM) is not None
    return bool(store.kv_get(job_id, K_AFTER) or waiting_on_lock or waiting_on_room) and not store.kv_get(job_id, K_LAUNCHED)


def _lock_holder(store, lock: str, me: str) -> str | None:
    for j in store.list_jobs():
        if j.job_id == me or store.kv_get(j.job_id, K_LOCK) != lock:
            continue
        if j.state in _HOLDING or (j.state in STAGED and store.kv_get(j.job_id, K_LAUNCHED)):
            return j.job_id
    return None


def status(store, job_id: str, reading=None) -> dict | None:
    """`{state: ready|running|waiting|paused, text, ...}` for a queued staged job, else None. `ready` = go now."""
    job = store.get_job(job_id)
    if job is None or job.state not in STAGED or not is_queued(store, job_id):
        return None
    pred_id, lock = store.kv_get(job_id, K_AFTER), store.kv_get(job_id, K_LOCK)
    out: dict[str, Any] = {"after": pred_id, "lock": lock, "state": "ready", "reason": "",
                           "after_staged": bool(pred_id and _staged(store, pred_id) and not store.kv_get(pred_id, K_LAUNCHED))}
    if pred_id:
        pred = store.get_job(pred_id)
        who = _name(store, pred_id)
        out["after_name"] = who
        if pred is None:
            out.update(state="paused", reason="predecessor missing", text=f"queued · paused: {who} is gone")
            return out
        if pred.state in _ACCEPTED:
            if store.kv_get(job_id, K_MERGE):
                from steward import incorporate

                if not incorporate.is_incorporated(store, pred_id):
                    out.update(state="waiting", reason="accepted, awaiting merge",
                               text=f"queued · after {who} (accepted, waiting for merge)")
                    return out
        elif pred.state in _PAUSING:
            out.update(state="paused", reason=f"{who} {pred.state.value.lower()}",
                       text=f"queued · paused: {who} {pred.state.value.lower().replace('_', ' ')}")
            return out
        else:
            running = pred.state in _HOLDING
            out.update(state="running" if running else "waiting", reason=f"{who} {pred.state.value.lower()}",
                       text=f"queued · after {who} ({'running' if running else 'waiting'})")
            return out
    if lock:
        holder = _lock_holder(store, lock, job_id)
        if holder:
            out.update(state="waiting", reason=f"lock {lock} held by {holder[:8]}",
                       text=f"queued · waiting for lock {lock}")
    from steward.governor import K_ROOM

    if out["state"] == "ready" and store.kv_get(job_id, K_ROOM) is not None:
        if reading is None or reading.wait:
            why = reading.reason() if reading is not None else store.kv_get(job_id, K_ROOM)
            out.update(state="waiting", reason=f"no room: {why}", text=f"queued · waiting for room: {why}", room=True)
        else:
            out["room"] = True
    return out


def order(store) -> list[str]:
    """Queued staged job ids in queue order: chains grouped (by their head's age), depth within a chain."""
    jobs = sorted(store.list_jobs(set(STAGED)), key=lambda j: j.created_at)
    queued = [j.job_id for j in jobs if is_queued(store, j.job_id)]

    def key(jid: str):
        depth, cur, seen = 0, jid, set()
        while (pred := store.kv_get(cur, K_AFTER)) and pred not in seen and _staged(store, pred):
            seen.add(cur)
            cur, depth = pred, depth + 1
        return store.get_job(cur).created_at, depth, store.get_job(jid).created_at

    return sorted(queued, key=key)


K_QNAME = "seq_qname"  # on a chain's root: the group's display name
_DONE = frozenset({JobState.ACCEPTED, JobState.RELEASED, JobState.STOPPED, JobState.FAILED})


def _root(store, job_id: str) -> str:
    cur, seen = job_id, set()
    while cur not in seen:
        seen.add(cur)
        pred = store.kv_get(cur, K_AFTER)
        if not pred or store.get_job(pred) is None:
            break
        cur = pred
    return cur


def _children(store, job_id: str) -> list[str]:
    jobs = sorted(store.list_jobs(), key=lambda j: j.created_at)
    return [j.job_id for j in jobs if store.kv_get(j.job_id, K_AFTER) == job_id]


def chain_of(store, job_id: str) -> list[str]:
    """The whole queue the job belongs to, root first, in queue order (any state: running and finished members too)."""
    out, stack, seen = [], [_root(store, job_id)], set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        out.append(cur)
        stack.extend(reversed(_children(store, cur)))
    return out


def groups(store) -> list[list[str]]:
    """Every queue of 2+ jobs with a member that is not finished, ordered by the root's age."""
    roots = {_root(store, j.job_id) for j in store.list_jobs() if store.kv_get(j.job_id, K_AFTER)}
    chains = [chain_of(store, r) for r in sorted(roots, key=lambda r: store.get_job(r).created_at)]
    return [c for c in chains if len(c) > 1 and any(store.get_job(i).state not in _DONE for i in c)]


def group_name(store, chain: list[str]) -> str:
    named = store.kv_get(chain[0], K_QNAME)
    if named:
        return named
    names = [_name(store, i) for i in chain]
    prefix = os.path.commonprefix(names)
    prefix = re.sub(r"[\s\-_:·#]*(job|step|part|task)?[\s\-_:·#]*\d*$", "", prefix, flags=re.I).strip()
    return prefix if len(prefix) >= 4 else names[0]


def set_name(store, job_id: str, name: str) -> tuple[bool, str]:
    job_id = _resolve(store, job_id) or job_id
    if store.get_job(job_id) is None:
        return False, f"unknown job: {job_id}"
    store.kv_set(_root(store, job_id), K_QNAME, name.strip())
    return True, f"queue named {name.strip()!r}"


def group_summary(store, chain: list[str]) -> dict:
    jobs = [store.get_job(i) for i in chain]
    done = sum(j.state in _DONE for j in jobs)
    now = next((j for j in jobs if j.state in _HOLDING or j.state in {JobState.NEEDS_HUMAN}), None)
    if now is None:
        now = next((j for j in jobs if j.state not in _DONE), jobs[0])
    return {"root": chain[0], "name": group_name(store, chain), "members": list(chain), "size": len(chain),
            "done": done, "now": now.job_id}


def annotate(store, summaries: list[dict]) -> list[dict]:
    """Stamp `steward status --json` rows: `queue` (status of a queued staged job), `queue_pos`, and `group` (the queue
    a member belongs to: root, name, members in order, size, done, now)."""
    pos = {jid: i for i, jid in enumerate(order(store))}
    by_member = {}
    for chain in groups(store):
        info = group_summary(store, chain)
        for i in chain:
            by_member[i] = info
    for s in summaries:
        st = status(store, s["job_id"])
        if st is not None:
            s["queue"] = st
            s["queue_pos"] = pos.get(s["job_id"], 0)
            s["queue_model"] = _queued_model(store, s["job_id"])
        if store.get_job(s["job_id"]).state in STAGED:
            s["queue_head"] = bool(_children(store, s["job_id"]))
            s["queue_next"] = bool(_successors(store, s["job_id"]))
        if s["job_id"] in by_member:
            s["group"] = by_member[s["job_id"]]
    return summaries


def render_show(store) -> str:
    gs = groups(store)
    lone = [j for j in order(store) if not any(j in g for g in gs)]
    if not gs and not lone:
        return "no queues"
    lines = []
    for chain in gs:
        info = group_summary(store, chain)
        lines.append(f"{info['name']} · {info['size']} jobs · {info['done']}/{info['size']} done")
        for jid in chain:
            st, job, lock = status(store, jid), store.get_job(jid), store.kv_get(jid, K_LOCK)
            what = st["text"] if st else job.state.value.lower()
            model = _queued_model(store, jid)
            lines.append(f"  {jid[:8]}  {_name(store, jid)}  {what}"
                         + (f"  [model {model}]" if model else "")
                         + (f"  [lock {lock}]" if lock else ""))
    for jid in lone:
        model = _queued_model(store, jid)
        lines.append(f"{jid[:8]}  {_name(store, jid)}  {status(store, jid)['text']}"
                     + (f"  [model {model}]" if model else ""))
    return "\n".join(lines)


# -- the launcher -------------------------------------------------------------------------------------------

def _launch_args(store, job_id: str, workspace: str | None) -> dict:
    args = dict(store.kv_get(job_id, "launch_args") or {})
    args.update(dict(store.kv_get(job_id, K_ARGS) or {}))
    if not args.get("workspace"):
        pred = store.kv_get(job_id, K_AFTER)
        args.setdefault("workspace", None)
        args["workspace"] = (dict(store.kv_get(pred, "launch_args") or {}).get("workspace") if pred else None) \
            or workspace or os.environ.get("HERDR_WORKSPACE_ID")
    return args


def _queued_model(store, job_id: str) -> str | None:
    args = dict(store.kv_get(job_id, K_ARGS) or store.kv_get(job_id, "launch_args") or {})
    agent_args = args.get("agent_args") or []
    try:
        from steward.supervisors import model_from_args

        model = model_from_args(agent_args)
    except (TypeError, ValueError):
        model = None
    return model or "sonnet"


def _note(store, job_id: str, what: str, evidence: dict, rule: str) -> None:
    if store.kv_get(job_id, K_NOTE) == what:
        return
    store.kv_set(job_id, K_NOTE, what)
    store.append_event(job_id, what=what, evidence=evidence, rule=rule, judgment=Judgment.code)


def sweep(store, launcher: Callable[[str, dict], dict], *, workspace: str | None = None, reading_fn=None) -> list[str]:
    """Launch every queued job that is ready, once. Returns the ids launched this pass. Safe to call from the event
    hook and the periodic tick at once: the `seq_launched` claim is taken (under a lock) before the launch."""
    from steward import governor

    reading = (reading_fn or (lambda: governor.read(store)))()
    room_released = False
    launched: list[str] = []
    for jid in order(store):
        st = status(store, jid, reading)
        if st is None:
            continue
        if st["state"] == "paused":
            _note(store, jid, f"queue paused: {st['reason']}", {"after": st["after"]}, "queue_paused")
            continue
        if st["state"] != "ready":
            continue
        if st.get("room") and room_released:
            continue  # one room release per tick: the next reading must include this launch's load
        with _claim_lock:
            if store.kv_get(jid, K_LAUNCHED):
                continue
            store.kv_set(jid, K_LAUNCHED, True)
        args = _launch_args(store, jid, workspace)
        try:
            result = launcher(jid, args)
        except Exception as exc:  # noqa: BLE001 - one bad launch must not stop the sweep
            result = {"accepted": False, "detail": str(exc)}
        if not result.get("accepted"):
            store.kv_delete(jid, K_LAUNCHED)  # not launched: the next sweep may try again
            _note(store, jid, f"queue launch failed: {result.get('detail')}", {"after": st["after"]}, "queue_launch_failed")
            continue
        store.kv_delete(jid, K_NOTE)
        store.append_event(
            jid, what="queue: auto-launched",
            evidence={"after": st["after"], "lock": st["lock"], "tab_id": result.get("tab_id")},
            rule="queue_launch", judgment=Judgment.code)
        if st.get("room"):
            room_released = True
            store.append_event(jid, what="governor: released", evidence={"values": reading.values},
                               rule="governor_released", judgment=Judgment.code)
        launched.append(jid)
    return launched
