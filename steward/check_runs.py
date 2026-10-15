"""Shared, durable check runs (continuous job ownership §8; Plan 4).

One check command on one candidate tree in one environment runs once and leaves a durable receipt. A job whose
candidate has the byte-identical tree attaches to the pending run or reuses the receipt instead of launching a
duplicate; every consumer still attributes the outcome against its OWN baseline (`verify.verify` does that, unchanged),
so sharing never mixes attribution. Heavy runs take one of the host's heavy slots (`STEWARD_HEAVY_SLOTS`, default 3)
in FIFO order; scoped runs never queue. A job's newer verify/baseline request supersedes its own older queued one
(cancelled when no other job still waits on it), so a moved candidate never leaves a dead run holding the line. A
run whose process vanished is LOST: re-queued once, then reported lost — never a pass. These rows are evidence and
queue state, not action claims; the row's own state transition (`queued -> running`) is the start claim.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import sys
import time
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):  # run as `python3 steward/check_runs.py`: sys.path[0] is steward/, so make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steward.baseline import CheckRun
from steward.diagnosis import redact
from steward.store import Store
from steward.models import Judgment

ENV_SWITCH = "STEWARD_SHARED_CHECKS"
HEAVY_SLOTS_PER_HOST = 3  # the default; `STEWARD_HEAVY_SLOTS` overrides (a 32-core host ran one ~11 min suite at a time)
HEAVY_SLOTS_ENV = "STEWARD_HEAVY_SLOTS"
HEAVY_THRESHOLD_S = 120.0
HEARTBEAT_S = 30.0
HEARTBEAT_STALE_S = 300.0
MAX_RUN_S = 4 * 3600.0
QUEUED_CANCEL_AFTER_S = 3 * HEARTBEAT_STALE_S  # a queued run nobody has heartbeated this long has no live requester
LOST_REQUEUE_MAX = 1
REUSE_MAX_AGE_S = 24 * 3600.0
RETENTION_DAYS = 30
MAX_RECEIPTS = 2000
OUTPUT_EXCERPT_CHARS = 4000
JUNIT_MAX_BYTES = 5_000_000
POLL_S = 2.0
PROGRESS_S = 300.0
STATES = ("queued", "running", "done", "lost", "failed", "cancelled")
LIVE = ("queued", "running")
FINAL = ("done", "lost", "failed", "cancelled")
SUPERSEDING = ("verify", "baseline")  # one live run per (job, purpose): a newer request cancels the older queued one
KILL_GRACE_S = 10.0  # SIGTERM to the owning process group, then SIGKILL if it is still there
FINISHED_STATES = ("STOPPED", "FAILED", "RELEASED")  # a job in one of these wants no check run, whatever the purpose
ACCEPTED_MOOT = ("verify", "baseline", "live_check", "final")  # an ACCEPTED job is past these; delivery still uses the rest
PURPOSES = ("verify", "baseline", "merged_tree", "post_merge", "final", "live_check", "worker")
WORKER_PURPOSE = "worker"  # a run the worker itself asked for (`steward check run`, v2.7 Phase 42)
ENV_KEYS = ("PYTHONHASHSEED", "LANG", "TZ")
_PYTEST = re.compile(r"\bpytest\b")
_SELECTION = re.compile(r"::|(?:^|\s)(?:-k|-m)(?:\s|=)|\s\S+\.py\b|\stests?/\S*")


@dataclass(frozen=True)
class RunSpec:
    tree_sha: str
    command: str
    env_digest: str
    toolchain: str
    heavy: bool
    predicted_s: float | None


@dataclass(frozen=True)
class CheckRunRow:
    run_id: str
    equiv_key: str
    tree_sha: str
    command: str
    env_digest: str
    toolchain: str
    heavy: bool
    host: str
    state: str
    queue_seq: int
    attempt: int
    requested_by: str
    owner: str | None
    pid: int | None
    enqueued_epoch: float
    started_epoch: float | None
    heartbeat_epoch: float | None
    finished_epoch: float | None
    predicted_s: float | None
    exit_code: int | None
    broken: str | None
    used: str | None
    tests: dict
    output_excerpt: str
    junit_path: str | None
    error: str | None


def enabled() -> bool:
    return os.environ.get(ENV_SWITCH, "1").strip().lower() not in ("0", "off", "false", "no")


def heavy_slots() -> int:
    raw = os.environ.get(HEAVY_SLOTS_ENV, "").strip()
    try:
        return max(1, int(raw)) if raw else HEAVY_SLOTS_PER_HOST
    except ValueError:
        return HEAVY_SLOTS_PER_HOST


def host_id() -> str:
    return socket.gethostname() or "localhost"


def env_digest(python_env: str = "", execution: str = "serial") -> str:
    """`execution` ('serial' / 'xdist-N', pytest_parallel.execution_label) keeps a serial receipt from satisfying a parallel run."""
    parts = [platform.system(), platform.machine(), platform.python_version(), python_env or "", f"exec={execution}"]
    parts += [f"{k}={os.environ.get(k, '')}" for k in ENV_KEYS]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def equivalence_key(tree_sha: str, command: str, env_digest_: str, toolchain: str) -> str:
    return hashlib.sha256(json.dumps([tree_sha, command, env_digest_, toolchain]).encode()).hexdigest()[:24]


def is_heavy(command: str, predicted_s: float | None) -> bool:
    """Heavy = predicted over the inline budget, or (unpredicted) a pytest run with no test selection."""
    if predicted_s is not None:
        return predicted_s > HEAVY_THRESHOLD_S
    match = _PYTEST.search(command)
    if not match:
        return False
    # `python -m pytest` contains a Python launcher `-m`; only selection flags after the pytest
    # command itself make this an explicitly scoped run.
    return not _SELECTION.search(command[match.end():])


def spec_for(runner, cwd, command: str, python_env: str = "", *, predicted_s: float | None = None,
             toolchain_fn=None) -> RunSpec | None:
    """The run identity of `command` in the clean checkout `cwd`, or None (unreadable or dirty tree: never shared)."""
    tree = runner.run(["git", "rev-parse", "HEAD^{tree}"], cwd=Path(cwd))
    if tree.exit_code != 0 or not tree.stdout.strip():
        return None
    status = runner.run(["git", "status", "--porcelain"], cwd=Path(cwd))
    if status.exit_code != 0 or status.stdout.strip():
        return None
    if toolchain_fn is None:
        from steward.verify_reuse import toolchain_fingerprint as toolchain_fn
    from steward.pytest_parallel import execution_label
    return RunSpec(tree.stdout.strip(), command, env_digest(python_env, execution_label([command], cwd)), toolchain_fn([command]),
                   is_heavy(command, predicted_s), predicted_s)


def _row(r) -> CheckRunRow:
    return CheckRunRow(r["run_id"], r["equiv_key"], r["tree_sha"], r["command"], r["env_digest"], r["toolchain"],
                       bool(r["heavy"]), r["host"], r["state"], int(r["queue_seq"]), int(r["attempt"]),
                       r["requested_by"], r["owner"], r["pid"], float(r["enqueued_epoch"]), r["started_epoch"],
                       r["heartbeat_epoch"], r["finished_epoch"], r["predicted_s"], r["exit_code"], r["broken"],
                       r["used"], json.loads(r["tests_json"]) if r["tests_json"] else {}, r["output_excerpt"] or "",
                       r["junit_path"], r["error"])


def get(store: Store, run_id: str) -> CheckRunRow | None:
    r = store._conn.execute("SELECT * FROM check_runs WHERE run_id = ?", (run_id,)).fetchone()
    return None if r is None else _row(r)


def waiters(store: Store, run_id: str) -> list[dict]:
    rows = store._conn.execute("SELECT * FROM check_run_waiters WHERE run_id = ? ORDER BY attached_epoch, job_id",
                               (run_id,)).fetchall()
    return [dict(r) for r in rows]


def request(store: Store, spec: RunSpec, *, job_id: str, purpose: str, now: float) -> tuple[CheckRunRow, str]:
    """Attach to a live equivalent run on this host, reuse a fresh unbroken receipt, or enqueue a new run."""
    key = equivalence_key(spec.tree_sha, spec.command, spec.env_digest, spec.toolchain)
    host = host_id()
    with store.immediate() as tx:
        c = tx.conn
        hit, how = None, "enqueued"
        candidates = c.execute("SELECT * FROM check_runs WHERE equiv_key = ? ORDER BY queue_seq DESC", (key,)).fetchall()
        for r in candidates:
            waiter = c.execute("SELECT consumed_epoch FROM check_run_waiters WHERE run_id=? AND job_id=? AND purpose=?",
                               (r["run_id"], job_id, purpose)).fetchone()
            if r["state"] == "lost" and waiter is not None and waiter["consumed_epoch"] is None:
                hit, how = r, "lost"
                break
            if r["state"] in LIVE and r["host"] == host:
                hit, how = r, "attached"
                break
            if (r["state"] == "done" and not r["broken"]
                    and now - float(r["finished_epoch"] or 0.0) <= REUSE_MAX_AGE_S):
                hit, how = r, "reused"
                break
        if hit is None:
            run_id = "cr-" + uuid.uuid4().hex[:12]
            seq = c.execute("SELECT COALESCE(MAX(queue_seq), 0) + 1 FROM check_runs").fetchone()[0]
            c.execute("INSERT INTO check_runs (run_id, equiv_key, tree_sha, command, env_digest, toolchain, heavy, host,"
                      " state, queue_seq, attempt, requested_by, enqueued_epoch, heartbeat_epoch, predicted_s)"
                      " VALUES (?,?,?,?,?,?,?,?,'queued',?,1,?,?,?,?)",
                      (run_id, key, spec.tree_sha, spec.command, spec.env_digest, spec.toolchain, int(spec.heavy), host,
                       int(seq), job_id, now, now, spec.predicted_s))
        else:
            run_id = hit["run_id"]
            if hit["state"] == "queued":  # a new waiter proves the queued run is still wanted
                c.execute("UPDATE check_runs SET heartbeat_epoch = ? WHERE run_id = ?", (now, run_id))
        reattached: list[str] = []
        # supersede BEFORE inserting this waiter: the job's waiter on its older queued run is re-pointed here (§5.5
        # "Reattach, not Lost"), so a process still waiting on the old run follows it instead of seeing it lost
        superseded = (_supersede(c, job_id, purpose, keep=run_id, now=now, reattached=reattached)
                      if purpose in SUPERSEDING else [])
        c.execute("INSERT OR IGNORE INTO check_run_waiters (run_id, job_id, purpose, how, attached_epoch)"
                  " VALUES (?,?,?,?,?)", (run_id, job_id, purpose, how, now))
    if superseded and store.get_job(job_id) is not None:
        for old in superseded:
            _job_event(store, job_id, "check_run_superseded", run_id=old, by=run_id, purpose=purpose)
            if old in reattached:
                _job_event(store, job_id, "check_run_reattached", purpose=purpose, **{"from": old, "to": run_id})
    return get(store, run_id), how


def _supersede(c, job_id: str, purpose: str, *, keep: str, now: float, reattached: list | None = None) -> list[str]:
    """Release this job's other unconsumed `purpose` waits on QUEUED runs; a run nobody else waits on is cancelled.
    A running run is left to finish (its receipt may still be reused); only queued ones hold the line for nothing.
    With a `keep` run the released wait is re-pointed to it (`how='reattached'`, listed in `reattached`) when the job
    has no `(keep, job, purpose)` row yet, else consumed; `keep=""` (a withdrawal) only consumes."""
    out = []
    olds = c.execute("SELECT w.run_id FROM check_run_waiters w JOIN check_runs r ON r.run_id = w.run_id"
                     " WHERE w.job_id=? AND w.purpose=? AND w.consumed_epoch IS NULL AND w.run_id<>?"
                     " AND r.state='queued' ORDER BY r.queue_seq DESC", (job_id, purpose, keep)).fetchall()
    for o in olds:
        rid = o["run_id"]
        held = keep and c.execute("SELECT 1 FROM check_run_waiters WHERE run_id=? AND job_id=? AND purpose=?",
                                  (keep, job_id, purpose)).fetchone()
        if keep and not held:
            c.execute("UPDATE check_run_waiters SET run_id=?, how='reattached' WHERE run_id=? AND job_id=?"
                      " AND purpose=?", (keep, rid, job_id, purpose))
            if reattached is not None:
                reattached.append(rid)
        else:
            c.execute("UPDATE check_run_waiters SET consumed_epoch=? WHERE run_id=? AND job_id=? AND purpose=?",
                      (now, rid, job_id, purpose))
        others = c.execute("SELECT COUNT(*) FROM check_run_waiters WHERE run_id=? AND consumed_epoch IS NULL",
                           (rid,)).fetchone()[0]
        if not others:
            c.execute("UPDATE check_runs SET state='cancelled', finished_epoch=?, error=? WHERE run_id=?"
                      " AND state='queued'", (now, f"superseded by {keep}" if keep else "withdrawn by the job", rid))
        out.append(rid)
    return out


def sweep_superseded(store: Store, *, now: float) -> list[str]:
    """Cancel a QUEUED run whose waiting jobs all hold a newer live or finished run for the same purpose
    (e.g. an older run that `reconcile` requeued after a newer request superseded it while it ran)."""
    out: list[str] = []
    events: list[tuple[str, str, str, str]] = []
    with store.immediate() as tx:
        c = tx.conn
        for r in c.execute("SELECT run_id, queue_seq FROM check_runs WHERE state='queued'").fetchall():
            ws = c.execute("SELECT job_id, purpose FROM check_run_waiters WHERE run_id=? AND consumed_epoch IS NULL",
                           (r["run_id"],)).fetchall()
            if not ws or any(w["purpose"] not in SUPERSEDING for w in ws):
                continue
            by = {}
            for w in ws:
                n = c.execute("SELECT x.run_id FROM check_run_waiters v JOIN check_runs x ON x.run_id = v.run_id"
                              " WHERE v.job_id=? AND v.purpose=? AND x.run_id<>? AND x.queue_seq>?"
                              " AND x.state IN ('queued','running','done') ORDER BY x.queue_seq DESC LIMIT 1",
                              (w["job_id"], w["purpose"], r["run_id"], r["queue_seq"])).fetchone()
                if n is None:
                    break
                by[w["job_id"]] = (n["run_id"], w["purpose"])
            else:
                c.execute("UPDATE check_run_waiters SET consumed_epoch=? WHERE run_id=? AND consumed_epoch IS NULL",
                          (now, r["run_id"]))
                c.execute("UPDATE check_runs SET state='cancelled', finished_epoch=?, error=? WHERE run_id=?"
                          " AND state='queued'", (now, "superseded by a newer run", r["run_id"]))
                out.append(r["run_id"])
                events += [(j, r["run_id"], n, p) for j, (n, p) in by.items()]
    for job_id, old, new, purpose in events:
        if store.get_job(job_id) is not None:
            _job_event(store, job_id, "check_run_superseded", run_id=old, by=new, purpose=purpose)
    return out


def try_start(store: Store, run_id: str, *, owner: str, pid: int | None, now: float) -> bool:
    """Claim a queued run subject to the host's live heavy slots (`heavy_slots()`) and FIFO order."""
    with store.immediate() as tx:
        c = tx.conn
        r = c.execute("SELECT * FROM check_runs WHERE run_id=?", (run_id,)).fetchone()
        if r is None or r["state"] != "queued": return False
        if r["heavy"]:
            fresh = now - HEARTBEAT_STALE_S
            running = c.execute("SELECT COUNT(*) FROM check_runs WHERE host=? AND heavy=1 AND state='running' AND"
                                " heartbeat_epoch>=?", (r["host"], fresh)).fetchone()[0]
            ahead = c.execute("SELECT COUNT(*) FROM check_runs WHERE host=? AND heavy=1 AND state='queued' AND"
                              " queue_seq<? AND heartbeat_epoch>=?", (r["host"], r["queue_seq"], fresh)).fetchone()[0]
            if running + ahead >= heavy_slots(): return False
        cur = c.execute("UPDATE check_runs SET state='running',owner=?,pid=?,started_epoch=?,heartbeat_epoch=?"
                        " WHERE run_id=? AND state='queued'", (owner, pid, now, now, run_id))
        return cur.rowcount == 1


class Queued(Exception):
    def __init__(self, run_id, position, eta_s):
        super().__init__(f"check run {run_id} queued #{position}")
        self.run_id, self.position, self.eta_s = run_id, position, eta_s


class Lost(Exception):
    def __init__(self, run_id, reason):
        super().__init__(f"check run {run_id} lost: {reason}")
        self.run_id, self.reason = run_id, reason


_SUPERSEDED_BY = re.compile(r"^superseded by (cr-[0-9a-f]+)$")
FOLLOW_MAX = 8  # a chain of supersedes longer than this is not followed further (never loops)


def follow_superseded(store: Store, row: CheckRunRow) -> CheckRunRow | None:
    """The run that superseded `row` (following a chain of supersedes), or None when `row` is not a run cancelled
    `superseded by <run_id>` (live, finished, lost, orphaned, withdrawn, or swept without a named successor)."""
    cur, seen = row, {row.run_id}
    for _ in range(FOLLOW_MAX):
        m = _SUPERSEDED_BY.match(cur.error or "") if cur.state == "cancelled" else None
        if m is None or m.group(1) in seen:
            break
        nxt = get(store, m.group(1))
        if nxt is None:
            break
        seen.add(nxt.run_id)
        cur = nxt
    return None if cur is row else cur


def withdraw_job(store: Store, job_id: str, *, now: float, purposes: tuple = ("verify",)) -> list[str]:
    """The job no longer wants its queued `purposes` runs (e.g. a send-back pre-empted its verification): release its
    waits on QUEUED runs; a run nobody else waits on is cancelled. Running runs finish (their receipt may be reused)."""
    out: list[str] = []
    with store.immediate() as tx:
        for purpose in purposes:
            out += _supersede(tx.conn, job_id, purpose, keep="", now=now)
    if out and store.get_job(job_id) is not None:
        _job_event(store, job_id, "check_runs_withdrawn", run_ids=out)
    return out


def _waiter_finished(store: Store, job_id: str, purpose: str) -> bool:
    """True when this waiter's job can no longer want the run. STOPPED/FAILED/RELEASED want nothing; an ACCEPTED job is
    past its verify/baseline, past merged_tree once incorporated, and still wants post_merge (delivery verifies the
    merge after incorporation) until it is released."""
    job = store.get_job(job_id)
    if job is None:
        return False
    state = job.state.value
    if state in FINISHED_STATES:
        return True
    if state == "ACCEPTED":
        if purpose in ACCEPTED_MOOT:
            return True
        if purpose == "post_merge":
            return False
        return store.kv_get(job_id, "incorporated_commit") is not None
    return False


def _kill_run(pid, *, grace_s: float = KILL_GRACE_S, killpg=os.killpg, kill=os.kill, getpgid=os.getpgid) -> bool:
    """SIGTERM the process group that owns a run (a bg_* process is its own session leader), SIGKILL after the grace
    period. Never touches this process or its group, or a pid that is not a group leader of its own."""
    from steward.bg_baseline import _alive
    if not isinstance(pid, int) or pid <= 1 or pid == os.getpid():
        return False
    try:
        pgid = getpgid(pid)
        if pgid == getpgid(0):
            return False
    except OSError:
        return False
    group = pgid == pid
    def send(sig):
        try:
            (killpg if group else kill)(pid, sig)
        except OSError:
            pass
    send(signal.SIGTERM)
    def escalate():
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if not _alive(pid): return
            time.sleep(min(0.2, grace_s))
        if _alive(pid): send(signal.SIGKILL)
    threading.Thread(target=escalate, name=f"check-run-kill-{pid}", daemon=True).start()
    return True


def _cancel_dead_runs(store: Store, candidates: list[str], *, now: float, reason: str, kill_fn=None) -> dict:
    """Cancel each live run in `candidates` whose unconsumed waiters are all finished jobs (at least one waiter). A run
    another live job still waits on is left alone. Running runs are killed. Returns {cancelled, killed, kept}."""
    kill_fn = kill_fn or _kill_run
    out = {"cancelled": [], "killed": [], "kept": []}
    host = host_id()
    for rid in candidates:
        with store.immediate() as tx:
            r = tx.conn.execute("SELECT * FROM check_runs WHERE run_id=? AND state IN ('queued','running')",
                                (rid,)).fetchone()
            if r is None: continue
            ws = tx.conn.execute("SELECT job_id, purpose FROM check_run_waiters WHERE run_id=? AND consumed_epoch IS NULL",
                                 (rid,)).fetchall()
            if not ws or not all(_waiter_finished(store, w["job_id"], w["purpose"]) for w in ws):
                out["kept"].append(rid); continue
            tx.conn.execute("UPDATE check_runs SET state='cancelled', finished_epoch=?, error=? WHERE run_id=?",
                            (now, reason, rid))
            tx.conn.execute("UPDATE check_run_waiters SET consumed_epoch=? WHERE run_id=? AND consumed_epoch IS NULL",
                            (now, rid))
        killed = False
        if r["state"] == "running" and r["host"] == host:
            killed = bool(kill_fn(r["pid"]))
        out["cancelled"].append(rid)
        if killed: out["killed"].append(rid)
        for job_id in sorted({w["job_id"] for w in ws}):
            if store.get_job(job_id) is not None:
                _job_event(store, job_id, "check_run_cancelled", run_id=rid, for_job=job_id, purpose_set=sorted(
                    {w["purpose"] for w in ws if w["job_id"] == job_id}), was=r["state"], killed=killed,
                    pid=r["pid"] if killed else None, reason=reason)
    return out


def cancel_job_runs(store: Store, job_id: str, *, now: float, kill_fn=None) -> dict:
    """The job reached a finished state: withdraw its queued runs (every purpose) and kill its running ones, unless a
    live job still waits on the same run (sharing)."""
    rids = [r["run_id"] for r in store._conn.execute(
        "SELECT DISTINCT w.run_id FROM check_run_waiters w JOIN check_runs c ON c.run_id=w.run_id"
        " WHERE w.job_id=? AND w.consumed_epoch IS NULL AND c.state IN ('queued','running')", (job_id,)).fetchall()]
    return _cancel_dead_runs(store, rids, now=now, reason=f"job {job_id} finished", kill_fn=kill_fn)


def cancel_runs_for(store: Store, job_id: str, *, now: float, reason: str, kill_fn=None) -> dict:
    """Governor relief: cancel this (live) job's queued and running runs; a run another unfinished job also waits
    on is kept. Running runs on this host are killed."""
    kill_fn = kill_fn or _kill_run
    out = {"cancelled": [], "killed": [], "kept": []}
    host = host_id()
    rids = [r["run_id"] for r in store._conn.execute(
        "SELECT DISTINCT w.run_id FROM check_run_waiters w JOIN check_runs c ON c.run_id=w.run_id"
        " WHERE w.job_id=? AND w.consumed_epoch IS NULL AND c.state IN ('queued','running')", (job_id,)).fetchall()]
    for rid in rids:
        with store.immediate() as tx:
            r = tx.conn.execute("SELECT * FROM check_runs WHERE run_id=? AND state IN ('queued','running')",
                                (rid,)).fetchone()
            if r is None: continue
            others = tx.conn.execute("SELECT job_id, purpose FROM check_run_waiters WHERE run_id=? AND job_id<>?"
                                     " AND consumed_epoch IS NULL", (rid, job_id)).fetchall()
            if any(not _waiter_finished(store, w["job_id"], w["purpose"]) for w in others):
                out["kept"].append(rid); continue
            tx.conn.execute("UPDATE check_runs SET state='cancelled', finished_epoch=?, error=? WHERE run_id=?",
                            (now, reason, rid))
            tx.conn.execute("UPDATE check_run_waiters SET consumed_epoch=? WHERE run_id=? AND consumed_epoch IS NULL",
                            (now, rid))
        if r["state"] == "running" and r["host"] == host and kill_fn(r["pid"]):
            out["killed"].append(rid)
        out["cancelled"].append(rid)
        _job_event(store, job_id, "check_run_cancelled", run_id=rid, for_job=job_id, was=r["state"], reason=reason)
    return out


def sweep_finished(store: Store, *, now: float, kill_fn=None) -> dict:
    """Per-tick: cancel every queued/running run whose waiting jobs are all finished (jobs that ended before the
    terminal-transition hook existed, or whose hook failed)."""
    rids = [r["run_id"] for r in store._conn.execute(
        "SELECT run_id FROM check_runs WHERE state IN ('queued','running')").fetchall()]
    return _cancel_dead_runs(store, rids, now=now, reason="all waiting jobs finished", kill_fn=kill_fn)


def heartbeat(store, run_id, *, owner, now):
    with store.immediate() as tx:
        tx.conn.execute("UPDATE check_runs SET heartbeat_epoch=? WHERE run_id=? AND ((state='running' AND owner=?)"
                        " OR state='queued')", (now, run_id, owner))


def queue_position(store, run_id):
    row = get(store, run_id)
    if row is None or row.state not in LIVE: return 0, None
    now = float(row.heartbeat_epoch or row.enqueued_epoch)
    if row.state == "running":
        return 0, None if row.predicted_s is None else max(0, row.predicted_s - (now - float(row.started_epoch or now)))
    if not row.heavy: return 1, row.predicted_s
    fresh = now - HEARTBEAT_STALE_S
    slots = heavy_slots()
    ahead = store._conn.execute("SELECT * FROM check_runs WHERE host=? AND heavy=1 AND ((state='running') OR"
                                " (state='queued' AND queue_seq<?)) AND heartbeat_epoch>=? ORDER BY queue_seq",
                                (row.host, row.queue_seq, fresh)).fetchall()
    eta = 0.0
    for x in ahead:
        x = _row(x)
        if x.predicted_s is None: eta = None; break
        started = x.started_epoch if x.started_epoch is not None else now
        eta += max(0, x.predicted_s - (now - float(started))) if x.state == "running" else x.predicted_s
    # with `slots` parallel heavy runs the line moves `slots` at a time
    return max(1, len(ahead) + 2 - slots), (None if eta is None else eta / slots)


def _job_event(store, job_id, what, **evidence):
    store.append_event(job_id, what=what, evidence=evidence, rule="check_runs", judgment=Judgment.code)


def reconcile(store, *, now, alive=None):
    from steward.bg_baseline import _alive
    alive = alive or _alive
    out = {"requeued": [], "final_lost": [], "orphaned": []}
    host = host_id()
    rows = [_row(r) for r in store._conn.execute("SELECT * FROM check_runs WHERE state='running'").fetchall()]
    for row in rows:
        beat = float(row.heartbeat_epoch or row.started_epoch or 0)
        dead = row.host == host and row.pid is not None and not alive(row.pid)
        overdue = now - float(row.started_epoch or now) > MAX_RUN_S
        if not (dead or beat < now - HEARTBEAT_STALE_S or overdue): continue
        retry = row.attempt <= LOST_REQUEUE_MAX
        why = "process gone" if dead else ("over the run limit" if overdue else "heartbeat stale")
        with store.immediate() as tx:
            if retry:
                cur = tx.conn.execute("UPDATE check_runs SET state='queued',attempt=attempt+1,owner=NULL,pid=NULL,"
                                      "started_epoch=NULL,heartbeat_epoch=?,error=? WHERE run_id=? AND state='running'",
                                      (now, f"lost once: {why}", row.run_id))
            else:
                cur = tx.conn.execute("UPDATE check_runs SET state='lost',finished_epoch=?,error=? WHERE run_id=? "
                                      "AND state='running'", (now, f"lost twice: {why}", row.run_id))
        if cur.rowcount:
            (out["requeued"] if retry else out["final_lost"]).append(row.run_id)
            for w in waiters(store, row.run_id):
                _job_event(store, w["job_id"], "check_run_lost", run_id=row.run_id, attempt=row.attempt, why=why,
                           requeued=retry)
    stale = now - QUEUED_CANCEL_AFTER_S
    for row in [_row(r) for r in store._conn.execute("SELECT * FROM check_runs WHERE state='queued' AND"
                                                     " COALESCE(heartbeat_epoch,enqueued_epoch)<?", (stale,)).fetchall()]:
        beat = float(row.heartbeat_epoch or row.enqueued_epoch)
        since = datetime.fromtimestamp(beat, timezone.utc).isoformat(timespec="seconds")
        with store.immediate() as tx:
            waiting = [w for w in waiters(store, row.run_id) if w["consumed_epoch"] is None]
            error = f"requester stopped (no heartbeat since {since}; {len(waiting)} waiter(s))"
            cur = tx.conn.execute("UPDATE check_runs SET state='cancelled',finished_epoch=?,error=?"
                                  " WHERE run_id=? AND state='queued' AND COALESCE(heartbeat_epoch,enqueued_epoch)<?",
                                  (now, error, row.run_id, stale))
        if cur.rowcount:
            out["orphaned"].append(row.run_id)
            for w in waiting:
                _job_event(store, w["job_id"], "check_run_cancelled", run_id=row.run_id, why=error,
                           purpose=w["purpose"])
    out["superseded"] = sweep_superseded(store, now=now)
    return out


def status_line(store, job_id, *, now):
    r = store._conn.execute("SELECT c.* FROM check_run_waiters w JOIN check_runs c ON c.run_id=w.run_id WHERE"
                            " w.job_id=? AND w.consumed_epoch IS NULL AND c.state IN ('queued','running')"
                            " ORDER BY w.attached_epoch DESC LIMIT 1", (job_id,)).fetchone()
    if r is None: return None
    row = _row(r)
    other = max(0, len({w["job_id"] for w in waiters(store, row.run_id)})-1)
    tail = f"; shared with {other} other job(s)" if other else ""
    if row.state == "running":
        mins = int((now-float(row.started_epoch or now))//60)
        est = f" of ~{int(row.predicted_s//60)}m" if row.predicted_s else ""
        return f"verification running {mins}m{est} ({row.command}){tail}"
    pos, eta = queue_position(store, row.run_id)
    when = f", about {int(eta//60)} min" if eta is not None else ", wait unknown"
    slots = heavy_slots()
    per = "one heavy suite" if slots == 1 else f"{slots} heavy suites"
    return f"verification queued #{pos}{when} ({per} per host){tail}"


def _execute(store, row, owner, runner, line, cwd, xml_path, python_env, clock, timeout_s=None):
    from steward.baseline import run_check
    stop = threading.Event()
    def beat():
        while not stop.wait(HEARTBEAT_S): heartbeat(store, row.run_id, owner=owner, now=clock())
    thread = threading.Thread(target=beat, name=f"check-run-{row.run_id}", daemon=True); thread.start()
    try: result = run_check(runner, line, Path(cwd), Path(xml_path), python_env, timeout_s=timeout_s)
    except Exception as exc:
        stop.set(); fail(store, row.run_id, owner=owner, error=f"{type(exc).__name__}: {exc}", now=clock()); raise
    stop.set(); complete(store, row.run_id, owner=owner, run=result, junit_src=Path(xml_path), now=clock())
    return result


def shared_check_fn(store, job_id, *, purpose, wait=True, share=True, predicted_s=None, clock=time.time,
                    sleep=time.sleep, alive=None, toolchain_fn=None, timeout_s=None):
    owner = f"{host_id()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
    def check_fn(runner, line, cwd, xml_path, python_env="", *, timeout_s=timeout_s):
        from steward.baseline import run_check
        if not enabled(): return run_check(runner, line, Path(cwd), Path(xml_path), python_env, timeout_s=timeout_s)
        spec = spec_for(runner, cwd, line, python_env, predicted_s=predicted_s, toolchain_fn=toolchain_fn)
        if spec is None: return run_check(runner, line, Path(cwd), Path(xml_path), python_env, timeout_s=timeout_s)
        if not share:
            spec = RunSpec(spec.tree_sha, spec.command + "#unshared:" + uuid.uuid4().hex, spec.env_digest,
                           spec.toolchain, spec.heavy, spec.predicted_s)
        row, how = request(store, spec, job_id=job_id, purpose=purpose, now=clock())
        if how == "lost":
            consume(store, row.run_id, job_id, purpose, now=clock())
            raise Lost(row.run_id, row.error or "lost")
        if how != "enqueued": _job_event(store, job_id, "check_run_shared", run_id=row.run_id, how=how,
                                          requested_by=row.requested_by, command=line, purpose=purpose)
        noted, next_progress = False, clock()+PROGRESS_S
        while True:
            row = get(store, row.run_id)
            if row.state == "done":
                consume(store, row.run_id, job_id, purpose, now=clock()); return as_check_run(row, Path(xml_path))
            if row.state in ("lost", "failed", "cancelled"):
                nxt = follow_superseded(store, row)
                if nxt is not None and nxt.tree_sha == spec.tree_sha:
                    # §5.5 reattach: this job's newer request on the same tree superseded the run we waited on; the
                    # waiter was re-pointed there, so wait on (or run) the superseding run instead of reporting lost
                    row = nxt; continue
                # a supersede onto a DIFFERENT tree means this candidate moved: its result is moot, and another tree's
                # receipt is never this tree's result -> Lost (bg_verify discards it as stale / re-queues it)
                consume(store, row.run_id, job_id, purpose, now=clock()); raise Lost(row.run_id, row.error or row.state)
            if row.state == "queued" and try_start(store, row.run_id, owner=owner, pid=os.getpid(), now=clock()):
                result = _execute(store, row, owner, runner, line, cwd, xml_path, python_env, clock, timeout_s)
                consume(store, row.run_id, job_id, purpose, now=clock()); return result
            if row.state == "running": reconcile(store, now=clock(), alive=alive)
            else: heartbeat(store, row.run_id, owner=None, now=clock())
            pos, eta = queue_position(store, row.run_id)
            if not noted or clock() >= next_progress:
                _job_event(store, job_id, "check_run_queued", run_id=row.run_id, position=pos, eta_s=eta,
                           command=line, purpose=purpose); noted, next_progress = True, clock()+PROGRESS_S
            if not wait: raise Queued(row.run_id, pos, eta)
            sleep(POLL_S)
    return check_fn


def complete(store: Store, run_id: str, *, owner: str, run: CheckRun, junit_src: Path | None,
             now: float) -> CheckRunRow:
    junit_path = None
    if junit_src is not None and Path(junit_src).is_file() and Path(junit_src).stat().st_size <= JUNIT_MAX_BYTES:
        d = Path(store.home) / "check_runs"
        d.mkdir(parents=True, exist_ok=True)
        junit_path = str(d / f"{run_id}.xml")
        shutil.copyfile(junit_src, junit_path)
    excerpt = redact(run.output or "")[-OUTPUT_EXCERPT_CHARS:]
    with store.immediate() as tx:
        cur = tx.conn.execute(
            "UPDATE check_runs SET state = 'done', exit_code = ?, broken = ?, used = ?, tests_json = ?,"
            " output_excerpt = ?, junit_path = ?, finished_epoch = ?, heartbeat_epoch = ?"
            " WHERE run_id = ? AND state = 'running' AND owner = ?",
            (run.exit_code, run.broken, run.used, json.dumps(run.tests or {}), excerpt, junit_path, now, now, run_id,
             owner))
        if cur.rowcount != 1:
            raise ValueError(f"check run {run_id} is not running under {owner}")
    return get(store, run_id)


def fail(store: Store, run_id: str, *, owner: str, error: str, now: float) -> CheckRunRow:
    """The check could not be executed at all (temp worktree failed, runner raised): no result, never a pass."""
    with store.immediate() as tx:
        tx.conn.execute("UPDATE check_runs SET state = 'failed', error = ?, finished_epoch = ?"
                        " WHERE run_id = ? AND state = 'running' AND owner = ?", (redact(error)[:300], now, run_id, owner))
    return get(store, run_id)


def consume(store: Store, run_id: str, job_id: str, purpose: str, *, now: float) -> None:
    with store._conn:
        store._conn.execute("UPDATE check_run_waiters SET consumed_epoch = ? WHERE run_id = ? AND job_id = ?"
                            " AND purpose = ? AND consumed_epoch IS NULL", (now, run_id, job_id, purpose))


def as_check_run(row: CheckRunRow, xml_path: Path) -> CheckRun:
    """The receipt as the CheckRun `run_check` would have returned; the junit is copied to `xml_path` so
    `verify.failure_messages` reads the same report."""
    Path(xml_path).unlink(missing_ok=True)
    if row.junit_path and Path(row.junit_path).is_file():
        shutil.copyfile(row.junit_path, xml_path)
    return CheckRun(exit_code=int(row.exit_code if row.exit_code is not None else 1), tests=dict(row.tests),
                    broken=row.broken, used=row.used or row.command, output=row.output_excerpt)


def purge(store: Store, *, now: float) -> int:
    """Delete finished receipts past retention (or beyond MAX_RECEIPTS, oldest first) that no waiter still needs."""
    cutoff = now - RETENTION_DAYS * 86400.0
    unreferenced = ("state IN ('done','lost','failed','cancelled') AND NOT EXISTS (SELECT 1 FROM check_run_waiters w"
                    " WHERE w.run_id = check_runs.run_id AND w.consumed_epoch IS NULL)")
    victims = [r["run_id"] for r in store._conn.execute(
        f"SELECT run_id FROM check_runs WHERE {unreferenced} AND COALESCE(finished_epoch, enqueued_epoch) < ?",
        (cutoff,)).fetchall()]
    total = store._conn.execute("SELECT COUNT(*) FROM check_runs").fetchone()[0] - len(victims)
    if total > MAX_RECEIPTS:
        extra = store._conn.execute(
            f"SELECT run_id FROM check_runs WHERE {unreferenced} AND COALESCE(finished_epoch, enqueued_epoch) >= ?"
            " ORDER BY finished_epoch LIMIT ?", (cutoff, total - MAX_RECEIPTS)).fetchall()
        victims += [r["run_id"] for r in extra]
    for rid in victims:
        row = get(store, rid)
        if row and row.junit_path:
            Path(row.junit_path).unlink(missing_ok=True)
        with store._conn:
            store._conn.execute("DELETE FROM check_run_waiters WHERE run_id = ?", (rid,))
            store._conn.execute("DELETE FROM check_runs WHERE run_id = ?", (rid,))
    return len(victims)


# -- worker-submitted runs (v2.7 Phase 42, SRD-13/14) --------------------------------------------------------------


@dataclass(frozen=True)
class WorkerSubmission:
    row: CheckRunRow | None
    how: str = ""
    error: str | None = None


def submit_worker_run(store: Store, runner, worktree, job_id: str, command: str, *, now: float, python_env: str = "",
                      predicted_s: float | None = None, toolchain_fn=None) -> WorkerSubmission:
    """Queue `command` as a Steward-owned run on the worker's committed tree (purpose `worker`, requested_by the job).
    A dirty or unreadable tree is refused with `commit first`: the run executes in a clean checkout of HEAD, so
    uncommitted edits would silently not be tested. Records kv `worker_check:<run_id>` {sha, worktree, command} for
    the executor."""
    spec = spec_for(runner, worktree, command, python_env, predicted_s=predicted_s, toolchain_fn=toolchain_fn)
    if spec is None:
        return WorkerSubmission(None, error="commit first: the check runs on your committed tree, and this worktree is "
                                            "dirty or unreadable (git add + git commit, then submit again)")
    head = runner.run(["git", "rev-parse", "HEAD"], cwd=Path(worktree))
    sha = head.stdout.strip() if head.exit_code == 0 else ""
    row, how = request(store, spec, job_id=job_id, purpose=WORKER_PURPOSE, now=now)
    if sha and row.state in LIVE:
        store.kv_set(job_id, f"worker_check:{row.run_id}",
                     {"sha": sha, "worktree": str(worktree), "command": command, "python_env": python_env})
    return WorkerSubmission(row, how)


def result_summary(row: CheckRunRow | None, *, failed_shown: int = 5) -> str:
    """One line a worker can act on. A run that did not finish is never reported as a pass."""
    if row is None:
        return "unknown run (no such check run)"
    tag = f"run {row.run_id} `{row.command}`"
    if row.state != "done":
        why = f": {row.error}" if row.error else ""
        return f"{tag} {row.state}{why} - there is no result; submit it again or run a targeted check yourself"
    code = int(row.exit_code if row.exit_code is not None else 1)
    counts: dict[str, int] = {}
    for status in (row.tests or {}).values():
        counts[status] = counts.get(status, 0) + 1
    bad = sorted(name for name, status in (row.tests or {}).items() if status not in ("passed", "skipped", "xfailed"))
    parts = [f"exit {code} ({'green' if code == 0 else 'RED'})"]
    if counts:
        parts.append(", ".join(f"{n} {status}" for status, n in sorted(counts.items())))
    if row.finished_epoch and row.started_epoch:
        parts.append(f"{int(row.finished_epoch - row.started_epoch)}s")
    if row.broken:
        parts.append(f"BROKEN: {row.broken}")
    text = f"{tag}: " + "; ".join(parts)
    if bad:
        text += "; failing: " + ", ".join(bad[:failed_shown]) + (f" (+{len(bad) - failed_shown} more)"
                                                                if len(bad) > failed_shown else "")
    return text


WAIT_DEFAULT_S = 540.0  # `steward check wait` stops under the 600 s Bash cap and says so


def wait_for_result(store: Store, run_id: str, *, timeout: float = WAIT_DEFAULT_S, poll: float = 2.0,
                    sleep=time.sleep, clock=time.monotonic) -> tuple[int, str]:
    """Block (no model, read-only) until the run is final: `(0, summary)` green, `(1, summary)` red / not a result,
    `(2, text)` still live at the timeout."""
    start = clock()
    while True:
        row = get(store, run_id)
        if row is None:
            return 1, f"unknown run {run_id}"
        if row.state not in LIVE:
            green = row.state == "done" and row.exit_code == 0 and not row.broken
            return (0 if green else 1), result_summary(row)
        if clock() - start >= timeout:
            return 2, (f"run {run_id} still {row.state} after {timeout:g}s - wait again with `steward check wait "
                       f"{run_id}`, or end your turn with `steward check await {run_id}` and Steward resumes you")
        sleep(poll)


def execute_worker_run(store: Store, run_id: str, job_id: str, runner, *, discover_fn=None, toolchain_fn=None,
                       clock=time.time, sleep=time.sleep) -> str:
    """The detached executor body: run the queued worker run in an isolated temp checkout of the worker's committed
    SHA, through `shared_check_fn` (attaches to the queued row, takes a heavy slot in FIFO order). The worker's own
    shell never runs the suite. Returns `done` or `failed`; a crash is recorded on the row, never a pass."""
    import tempfile

    rec = store.kv_get(job_id, f"worker_check:{run_id}") or {}
    row = get(store, run_id)
    if row is None or not rec.get("sha"):
        return "failed"
    if row.state not in LIVE:
        return "done"
    worktree = Path(rec.get("worktree") or ".")
    tmp = Path(tempfile.mkdtemp(prefix="steward-worker-check-"))
    tree = tmp / "tree"
    try:
        add = runner.run(["git", "worktree", "add", "--detach", str(tree), rec["sha"]], cwd=worktree)
        if add.exit_code != 0:
            _fail_unowned(store, run_id, f"temp worktree failed: {add.stderr or add.stdout}"[:300], clock())
            return "failed"
        check_fn = shared_check_fn(store, job_id, purpose=WORKER_PURPOSE, wait=True, share=True, clock=clock,
                                   sleep=sleep, toolchain_fn=toolchain_fn)
        try:
            check_fn(runner, rec["command"], tree, tmp / "junit.xml", rec.get("python_env") or "")
        except Lost:
            return "failed"
        return "done"
    except Exception as exc:  # noqa: BLE001 - a crashed executor is recorded on the row and is never a pass
        _fail_unowned(store, run_id, f"{type(exc).__name__}: {exc}", clock())
        return "failed"
    finally:
        runner.run(["git", "worktree", "remove", "--force", str(tree)], cwd=worktree)
        shutil.rmtree(tmp, ignore_errors=True)


def _fail_unowned(store: Store, run_id: str, error: str, now: float) -> None:
    """Mark a still-live run failed when no process owns it (the executor could not start it)."""
    with store.immediate() as tx:
        tx.conn.execute("UPDATE check_runs SET state='failed', error=?, finished_epoch=? WHERE run_id=? AND"
                        " state IN ('queued','running')", (redact(error)[:300], now, run_id))


def spawn_worker_executor(run_id: str, job_id: str, *, popen=None) -> None:
    """Start the executor detached (its own session, so the run survives the runner) - `python3 -m steward.check_runs`."""
    import subprocess
    import sys

    (popen or subprocess.Popen)([sys.executable, "-m", "steward.check_runs", "run", run_id, job_id],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)


def main(argv: list[str] | None = None) -> int:
    import sys

    from steward.runner_exec import SubprocessRunner

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3 or args[0] != "run":
        print("usage: python3 -m steward.check_runs run RUN_ID JOB_ID", file=sys.stderr)
        return 2
    return 0 if execute_worker_run(Store(), args[1], args[2], SubprocessRunner()) == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
