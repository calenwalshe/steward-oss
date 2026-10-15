"""Issue G: one (job, purpose) never holds two live verify runs, and a per-tick sweep cancels stale queued ones."""
from __future__ import annotations

from steward import check_runs as cr
from steward.store import Store


def spec(tree, command="python3 -m pytest -q"):
    return cr.RunSpec(tree_sha=tree, command=command, env_digest="e", toolchain="tc", heavy=True, predicted_s=600.0)


def live_for(s, job, purpose="verify"):
    return sorted(r["run_id"] for r in s._conn.execute(
        "SELECT r.run_id FROM check_runs r JOIN check_run_waiters w ON w.run_id=r.run_id"
        " WHERE w.job_id=? AND w.purpose=? AND w.consumed_epoch IS NULL AND r.state IN ('queued','running')",
        (job, purpose)).fetchall())


def test_t1_then_t2_leaves_one_live_run(steward_home):
    s = Store(steward_home)
    j = s.create_job("v", "/repo").job_id
    cr.request(s, spec("T1"), job_id=j, purpose="verify", now=1.0)
    t2 = cr.request(s, spec("T2"), job_id=j, purpose="verify", now=2.0)[0]
    assert live_for(s, j) == [t2.run_id]


def test_t1_done_once_then_t2_leaves_one_live_run(steward_home):
    s = Store(steward_home)
    j = s.create_job("v", "/repo").job_id
    t1 = cr.request(s, spec("T1"), job_id=j, purpose="verify", now=1.0)[0]
    assert cr.try_start(s, t1.run_id, owner="p", pid=None, now=1.5)
    s._conn.execute("UPDATE check_runs SET state='done', finished_epoch=2.0 WHERE run_id=?", (t1.run_id,))
    s._conn.commit()
    t2 = cr.request(s, spec("T2"), job_id=j, purpose="verify", now=3.0)[0]
    assert live_for(s, j) == [t2.run_id]


def test_newer_run_already_running_leaves_one_live_run(steward_home):
    s = Store(steward_home)
    j = s.create_job("v", "/repo").job_id
    cr.request(s, spec("T1"), job_id=j, purpose="verify", now=1.0)
    t2 = cr.request(s, spec("T2"), job_id=j, purpose="verify", now=2.0)[0]
    assert cr.try_start(s, t2.run_id, owner="p", pid=None, now=2.5)
    assert cr.request(s, spec("T2"), job_id=j, purpose="verify", now=3.0)[0].run_id == t2.run_id  # attaches
    assert live_for(s, j) == [t2.run_id]


def test_running_old_run_lost_and_requeued_beside_newer_leaves_one_live_run(steward_home):
    s = Store(steward_home)
    j = s.create_job("v", "/repo").job_id
    t1 = cr.request(s, spec("T1"), job_id=j, purpose="verify", now=1.0)[0]
    assert cr.try_start(s, t1.run_id, owner="p", pid=None, now=1.5)
    t2 = cr.request(s, spec("T2"), job_id=j, purpose="verify", now=2.0)[0]  # T1 runs on: both live (by design)
    assert cr.try_start(s, t2.run_id, owner="p2", pid=None, now=2.5)
    cr.reconcile(s, now=1.5 + cr.HEARTBEAT_STALE_S + 100, alive=lambda pid: False)  # T1 dies -> requeued
    cr.sweep_superseded(s, now=1000.0)
    assert cr.get(s, t1.run_id).state == "cancelled"


def test_sweep_cancels_queued_run_whose_waiters_have_newer_live_or_finished_run(steward_home):
    s = Store(steward_home)
    ja, jb = (s.create_job("v", "/repo").job_id for _ in range(2))
    old = cr.request(s, spec("T1"), job_id=ja, purpose="verify", now=1.0)[0]
    cr.try_start(s, old.run_id, owner="o", pid=None, now=1.5)  # running when superseded: its waiter stays (§5.5)
    # force the stale shape: a newer finished run exists for the job, the old wait is still unconsumed
    new = cr.request(s, spec("T2"), job_id=ja, purpose="verify", now=2.0)[0]
    s._conn.execute("UPDATE check_runs SET state='queued' WHERE run_id=?", (old.run_id,))
    s._conn.execute("UPDATE check_run_waiters SET consumed_epoch=NULL WHERE run_id=?", (old.run_id,))
    s._conn.execute("UPDATE check_runs SET state='done', finished_epoch=3.0 WHERE run_id=?", (new.run_id,))
    s._conn.commit()
    keep = cr.request(s, spec("T9"), job_id=jb, purpose="verify", now=4.0)[0]  # unrelated, must survive
    assert cr.sweep_superseded(s, now=5.0) == [old.run_id]
    assert cr.get(s, old.run_id).state == "cancelled" and cr.get(s, keep.run_id).state == "queued"
    assert [e.evidence["run_id"] for e in s.events(ja) if e.what == "check_run_superseded"][-1] == old.run_id
    assert cr.sweep_superseded(s, now=6.0) == []


def test_sweep_keeps_a_run_a_job_without_a_newer_run_still_waits_on(steward_home):
    s = Store(steward_home)
    ja, jb = (s.create_job("v", "/repo").job_id for _ in range(2))
    shared = cr.request(s, spec("T1"), job_id=ja, purpose="verify", now=1.0)[0]
    cr.request(s, spec("T1"), job_id=jb, purpose="verify", now=2.0)
    s._conn.execute("INSERT INTO check_runs (run_id, equiv_key, tree_sha, command, env_digest, toolchain, heavy, host,"
                    " state, queue_seq, attempt, requested_by, enqueued_epoch, heartbeat_epoch)"
                    " VALUES ('cr-new','k','T2','c','e','t',1,?,'queued',99,1,?,3.0,3.0)", (shared.host, ja))
    s._conn.execute("INSERT INTO check_run_waiters (run_id, job_id, purpose, how, attached_epoch)"
                    " VALUES ('cr-new',?,'verify','enqueued',3.0)", (ja,))
    s._conn.commit()
    assert cr.sweep_superseded(s, now=5.0) == []
    assert cr.get(s, shared.run_id).state == "queued"
