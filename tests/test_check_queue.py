"""Plan 4 Task 4: one heavy suite per host in FIFO order, visible progress, lost runs re-queued once (spec §8)."""
from __future__ import annotations

import pytest

from steward import bg_verify, check_runs as cr, ownership, stability
from steward.baseline import CheckRun
from steward.runner_exec import CommandResult
from steward.store import Store


@pytest.fixture(autouse=True)
def _one_heavy_slot(monkeypatch):
    """These are the single-slot FIFO rules; multi-slot parallelism is tests/test_check_queue_slots.py."""
    monkeypatch.setenv(cr.HEAVY_SLOTS_ENV, "1")


def spec(tree, heavy=True, predicted_s=600.0):
    return cr.RunSpec(tree_sha=tree, command="python3 -m pytest -q", env_digest="e", toolchain="tc", heavy=heavy,
                      predicted_s=predicted_s)


def job(store):
    return store.create_job("verify it", "/repo").job_id


def done(store, row, owner, now):
    return cr.complete(store, row.run_id, owner=owner, run=CheckRun(exit_code=0, tests={"t::a": "passed"}),
                       junit_src=None, now=now)


class ScriptedChecks:
    """git answers a clean tree per cwd; every check line returns `exit_code` and counts the executions."""

    def __init__(self, trees, exit_code=0):
        self.trees, self.exit_code, self.executed = trees, exit_code, []

    def run(self, argv, cwd, *, timeout=None, env=None):
        out = self.trees.get(str(cwd), "") + "\n" if argv[:2] == ["git", "rev-parse"] else ""
        return CommandResult(argv=list(argv), cwd=str(cwd), exit_code=0, stdout=out, stderr="", duration_s=0.0)

    def shell_line(self, line, cwd, *, timeout=None, env=None):
        self.executed.append((line, str(cwd)))
        return CommandResult(argv=["bash", "-lc", line], cwd=str(cwd), exit_code=self.exit_code,
                             stdout="3 passed", stderr="", duration_s=0.0)


def test_one_heavy_run_per_host_in_fifo_order(steward_home):
    s = Store(steward_home)
    a, _ = cr.request(s, spec("A"), job_id="JA", purpose="verify", now=1.0)
    b, _ = cr.request(s, spec("B"), job_id="JB", purpose="verify", now=2.0)
    c, _ = cr.request(s, spec("C"), job_id="JC", purpose="verify", now=3.0)
    assert cr.try_start(s, a.run_id, owner="pa", pid=None, now=4.0)
    assert not cr.try_start(s, b.run_id, owner="pb", pid=None, now=5.0)
    done(s, a, "pa", 6.0)
    assert not cr.try_start(s, c.run_id, owner="pc", pid=None, now=7.0)  # B is ahead
    assert cr.try_start(s, b.run_id, owner="pb", pid=None, now=8.0)


def test_scoped_runs_never_queue_behind_a_heavy_run(steward_home):
    s = Store(steward_home)
    a, _ = cr.request(s, spec("A"), job_id="JA", purpose="verify", now=1.0)
    cr.try_start(s, a.run_id, owner="pa", pid=None, now=2.0)
    small, _ = cr.request(s, spec("S", heavy=False, predicted_s=5.0), job_id="JS", purpose="verify", now=3.0)
    assert cr.try_start(s, small.run_id, owner="ps", pid=None, now=4.0)


def test_an_orphaned_queued_run_does_not_hold_the_line(steward_home):
    s = Store(steward_home)
    b, _ = cr.request(s, spec("B"), job_id="JB", purpose="verify", now=1.0)  # its waiter dies; nobody touches it
    c, _ = cr.request(s, spec("C"), job_id="JC", purpose="verify", now=1.0 + cr.HEARTBEAT_STALE_S + 10)
    assert cr.try_start(s, c.run_id, owner="pc", pid=None, now=2.0 + cr.HEARTBEAT_STALE_S + 10)


def test_queued_heavy_run_shows_position_and_eventually_runs(steward_home, tmp_path):
    s = Store(steward_home)
    jo, jc = job(s), job(s)
    other, _ = cr.request(s, spec("OTHER", predicted_s=2400.0), job_id=jo, purpose="verify", now=0.0)
    cr.try_start(s, other.run_id, owner="elsewhere", pid=None, now=0.0)
    wt = tmp_path / "wt"
    wt.mkdir()
    checks = ScriptedChecks({str(wt): "MINE"})
    clock = {"t": 60.0}
    fn = cr.shared_check_fn(s, jc, purpose="verify", wait=False, predicted_s=2400.0, clock=lambda: clock["t"],
                            toolchain_fn=lambda c: "tc")
    with pytest.raises(cr.Queued) as q:
        fn(checks, "python3 -m pytest -q", wt, tmp_path / "x.xml", "")
    assert q.value.position == 2 and q.value.eta_s == pytest.approx(2340.0)
    assert "queued #2" in cr.status_line(s, jc, now=60.0)
    assert any("queued #2" in line for line in stability.explain_lines(s, jc, now=60.0, count_fn=lambda _h: 0))
    assert checks.executed == []

    def sleep(_s):  # the running suite finishes while we wait
        clock["t"] += 30.0
        if cr.get(s, other.run_id).state == "running":
            done(s, other, "elsewhere", clock["t"])

    run = cr.shared_check_fn(s, jc, purpose="verify", wait=True, predicted_s=2400.0, clock=lambda: clock["t"],
                             sleep=sleep, toolchain_fn=lambda c: "tc")(checks, "python3 -m pytest -q", wt,
                                                                       tmp_path / "x.xml", "")
    assert run.exit_code == 0 and len(checks.executed) == 1
    assert [e.what for e in s.events(jc)].count("check_run_queued") >= 1


def test_waiters_of_one_run_execute_it_once(steward_home, tmp_path):
    s = Store(steward_home)
    wa, wb = tmp_path / "a", tmp_path / "b"
    wa.mkdir()
    wb.mkdir()
    checks = ScriptedChecks({str(wa): "SAME", str(wb): "SAME"})
    tc = dict(toolchain_fn=lambda c: "tc")
    ra = cr.shared_check_fn(s, job(s), purpose="verify", **tc)(checks, "python3 -m pytest -q", wa, tmp_path / "a.xml", "")
    rb = cr.shared_check_fn(s, job(s), purpose="verify", **tc)(checks, "python3 -m pytest -q", wb, tmp_path / "b.xml", "")
    assert ra.exit_code == rb.exit_code == 0 and len(checks.executed) == 1


def test_reconcile_requeues_a_dead_run_once_then_marks_it_lost(steward_home):
    s = Store(steward_home)
    ja = job(s)
    a, _ = cr.request(s, spec("A"), job_id=ja, purpose="verify", now=1.0)
    cr.try_start(s, a.run_id, owner="pa", pid=4242, now=2.0)
    out = cr.reconcile(s, now=3.0, alive=lambda pid: False)
    assert out["requeued"] == [a.run_id] and cr.get(s, a.run_id).state == "queued" and cr.get(s, a.run_id).attempt == 2
    cr.try_start(s, a.run_id, owner="pa2", pid=4243, now=4.0)
    out = cr.reconcile(s, now=5.0, alive=lambda pid: False)
    assert out["final_lost"] == [a.run_id] and cr.get(s, a.run_id).state == "lost"
    assert [e.what for e in s.events(ja)].count("check_run_lost") == 2


def test_stale_heartbeat_is_lost_even_when_pid_is_unknown(steward_home):
    s = Store(steward_home)
    a, _ = cr.request(s, spec("A"), job_id=job(s), purpose="verify", now=1.0)
    cr.try_start(s, a.run_id, owner="pa", pid=None, now=2.0)
    assert cr.reconcile(s, now=2.0 + cr.HEARTBEAT_STALE_S - 1)["requeued"] == []
    assert cr.reconcile(s, now=2.0 + cr.HEARTBEAT_STALE_S + 1)["requeued"] == [a.run_id]


def test_a_waiter_of_a_lost_run_gets_Lost_never_a_pass(steward_home, tmp_path):
    s = Store(steward_home)
    wt = tmp_path / "wt"
    wt.mkdir()
    checks = ScriptedChecks({str(wt): "T"})
    jid = job(s)
    row, _ = cr.request(s, cr.spec_for(checks, wt, "python3 -m pytest -q", toolchain_fn=lambda c: "tc"),
                        job_id=jid, purpose="verify", now=1.0)
    with s.immediate() as tx:
        tx.conn.execute("UPDATE check_runs SET state='lost', error='lost twice' WHERE run_id=?", (row.run_id,))
    with pytest.raises(cr.Lost):
        cr.shared_check_fn(s, jid, purpose="verify", toolchain_fn=lambda c: "tc")(
            checks, "python3 -m pytest -q", wt, tmp_path / "y.xml", "")


def test_lost_run_is_requeued_once_then_becomes_a_blocker(steward_home):
    """Full path through the runner (P4-1): first vanish -> re-queued (no park); second -> the existing crashed path,
    which Plan 1 turns into a reviewable verification_unverified blocker with no owner request."""
    from test_bg_verify import _bg_job

    store = Store(steward_home)
    job, runner, sr, spawned = _bg_job(store)
    runner.tick(job.job_id)

    def vanish():
        rec = bg_verify.record(store, job.job_id)
        store.kv_set(job.job_id, bg_verify.KEY, {**rec, "started_at": rec["started_at"] - bg_verify.STALE_S - 5})

    vanish()
    first = runner.tick(job.job_id)
    assert first.state == "WORKER_FINISHED" and len(spawned) == 2
    assert any(e.rule == "bg_verify_lost_requeue" for e in store.events(job.job_id))
    vanish()
    second = runner.tick(job.job_id)
    assert second.state == "WORKER_FINISHED" and len(spawned) == 3  # zero-touch §5.5: two re-queues, then the ask
    vanish()
    third = runner.tick(job.job_id)
    assert third.state == "NEEDS_HUMAN" and len(spawned) == 3
    ownership.reconcile_job(store, job.job_id, now=1e9)
    assert ownership.owner_view(store, job.job_id).status == "awaiting_technical_review"


def test_a_stale_queued_run_is_cancelled_never_lost_with_the_real_waiter_count(steward_home):
    s = Store(steward_home)
    ja, jb, jc = job(s), job(s), job(s)
    r, _ = cr.request(s, spec("Q"), job_id=ja, purpose="verify", now=1000.0)
    cr.request(s, spec("Q"), job_id=jb, purpose="verify", now=1001.0)
    cr.request(s, spec("Q"), job_id=jc, purpose="verify", now=1002.0)
    cr.consume(s, r.run_id, jc, "verify", now=1003.0)  # already consumed: not counted
    out = cr.reconcile(s, now=1000.0 + 3 * cr.HEARTBEAT_STALE_S - 1)
    assert out["orphaned"] == [] and cr.get(s, r.run_id).state == "queued"
    now = 1002.0 + 3 * cr.HEARTBEAT_STALE_S + 1
    assert cr.reconcile(s, now=now)["orphaned"] == [r.run_id]
    row = cr.get(s, r.run_id)
    assert row.state == "cancelled" and row.finished_epoch == now and row.started_epoch is None
    assert row.error == "requester stopped (no heartbeat since 1970-01-01T00:16:42+00:00; 2 waiter(s))"
    for j in (ja, jb):
        assert [e.what for e in s.events(j)].count("check_run_cancelled") == 1
    assert "check_run_cancelled" not in [e.what for e in s.events(jc)]


def test_a_queued_run_kept_fresh_by_a_live_waiter_survives(steward_home, tmp_path):
    s = Store(steward_home)
    jo, jc = job(s), job(s)
    other, _ = cr.request(s, spec("OTHER"), job_id=jo, purpose="verify", now=0.0)
    cr.try_start(s, other.run_id, owner="elsewhere", pid=None, now=0.0)
    wt = tmp_path / "wt"
    wt.mkdir()
    checks = ScriptedChecks({str(wt): "MINE"})
    clock = {"t": 10.0}
    seen = []

    def sleep(_s):  # the live waiter polls for 2 h while the other suite keeps running, then it finishes
        clock["t"] += 60.0
        cr.heartbeat(s, other.run_id, owner="elsewhere", now=clock["t"])
        mine = [r for r in s._conn.execute("SELECT run_id,state FROM check_runs WHERE state='queued'").fetchall()]
        seen.extend(r["state"] for r in mine)
        if clock["t"] > 7200.0 and cr.get(s, other.run_id).state == "running":
            done(s, other, "elsewhere", clock["t"])

    run = cr.shared_check_fn(s, jc, purpose="verify", wait=True, clock=lambda: clock["t"], sleep=sleep,
                             toolchain_fn=lambda c: "tc")(checks, "python3 -m pytest -q", wt, tmp_path / "x.xml", "")
    assert run.exit_code == 0 and seen and set(seen) == {"queued"}
    assert "check_run_cancelled" not in [e.what for e in s.events(jc)]


def test_a_started_run_that_dies_is_still_lost_not_cancelled(steward_home):
    s = Store(steward_home)
    r, _ = cr.request(s, spec("R"), job_id=job(s), purpose="verify", now=1.0)
    cr.try_start(s, r.run_id, owner="p", pid=None, now=2.0)
    cr.reconcile(s, now=2.0 + cr.HEARTBEAT_STALE_S + 1)  # lost once -> requeued
    assert cr.try_start(s, r.run_id, owner="p", pid=None, now=400.0)
    cr.reconcile(s, now=400.0 + cr.HEARTBEAT_STALE_S + 1)
    row = cr.get(s, r.run_id)
    assert row.state == "lost" and row.started_epoch is not None
    assert row.error.startswith("lost twice")
