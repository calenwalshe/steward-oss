"""Zero-touch §5.5 "Reattach, not Lost": a job's newer verify request re-points its waiter on the older queued run to
the superseding run (`check_run_reattached`), and a process still waiting on the old run follows it instead of
reporting the run lost. A supersede onto a different tree is a moved candidate: that one is still Lost (moot)."""
from __future__ import annotations

import time

import pytest

from steward import check_runs as cr
from steward.baseline import CheckRun
from steward.store import Store
from test_check_queue import ScriptedChecks


@pytest.fixture(autouse=True)
def _one_heavy_slot(monkeypatch):
    monkeypatch.setenv(cr.HEAVY_SLOTS_ENV, "1")


def _spec(tree, command="pytest -q"):
    return cr.RunSpec(tree, command, "env", "tool", True, 10.0)


def _waiters(store, job_id, purpose="verify"):
    return store._conn.execute("SELECT run_id, how, consumed_epoch FROM check_run_waiters WHERE job_id=? AND purpose=?",
                               (job_id, purpose)).fetchall()


def test_supersede_reattaches_waiter(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r"); now = time.time()
    old, how = cr.request(store, _spec("t1"), job_id=j.job_id, purpose="verify", now=now)
    new, how2 = cr.request(store, _spec("t2"), job_id=j.job_id, purpose="verify", now=now + 1)
    w = _waiters(store, j.job_id)
    assert {r["run_id"] for r in w} == {new.run_id}
    assert [r["how"] for r in w] == ["reattached"] and w[0]["consumed_epoch"] is None
    assert cr.get(store, old.run_id).state == "cancelled"
    ev = [e for e in store.events(j.job_id) if e.what == "check_run_reattached"]
    assert len(ev) == 1 and ev[0].evidence["from"] == old.run_id and ev[0].evidence["to"] == new.run_id
    assert any(e.what == "check_run_superseded" for e in store.events(j.job_id))


def test_follow_superseded_resolves_to_the_new_run(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r"); now = time.time()
    old, _ = cr.request(store, _spec("t1"), job_id=j.job_id, purpose="verify", now=now)
    new, _ = cr.request(store, _spec("t2"), job_id=j.job_id, purpose="verify", now=now + 1)
    row = cr.get(store, old.run_id)
    assert row.state == "cancelled" and row.error == f"superseded by {new.run_id}"
    assert cr.follow_superseded(store, row).run_id == new.run_id
    assert cr.follow_superseded(store, cr.get(store, new.run_id)) is None          # a live row is not followed
    lost = cr.CheckRunRow(**{**row.__dict__, "error": "owner vanished"})
    assert cr.follow_superseded(store, lost) is None                               # only a supersede is followed


def test_follow_superseded_follows_a_chain_to_the_newest_run(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r"); now = time.time()
    a, _ = cr.request(store, _spec("t1"), job_id=j.job_id, purpose="verify", now=now)
    cr.request(store, _spec("t2"), job_id=j.job_id, purpose="verify", now=now + 1)
    c, _ = cr.request(store, _spec("t3"), job_id=j.job_id, purpose="verify", now=now + 2)
    assert cr.follow_superseded(store, cr.get(store, a.run_id)).run_id == c.run_id
    assert {r["run_id"] for r in _waiters(store, j.job_id)} == {c.run_id}


def test_a_running_old_run_keeps_its_waiter(steward_home):
    """The "a running run is left to finish" rule stands: only a QUEUED old run is reattached away from."""
    store = Store(); j = store.create_job("g", "/tmp/r"); now = time.time()
    old, _ = cr.request(store, _spec("t1"), job_id=j.job_id, purpose="verify", now=now)
    assert cr.try_start(store, old.run_id, owner="o", pid=None, now=now)
    new, _ = cr.request(store, _spec("t2"), job_id=j.job_id, purpose="verify", now=now + 1)
    assert {r["run_id"] for r in _waiters(store, j.job_id)} == {old.run_id, new.run_id}
    assert cr.get(store, old.run_id).state == "running"
    assert not any(e.what == "check_run_reattached" for e in store.events(j.job_id))


def test_a_shared_old_run_stays_queued_for_the_other_job(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r"); k = store.create_job("h", "/tmp/r"); now = time.time()
    old, _ = cr.request(store, _spec("t1"), job_id=j.job_id, purpose="verify", now=now)
    cr.request(store, _spec("t1"), job_id=k.job_id, purpose="verify", now=now)
    new, _ = cr.request(store, _spec("t2"), job_id=j.job_id, purpose="verify", now=now + 1)
    assert cr.get(store, old.run_id).state == "queued"                             # k still wants it
    assert {r["run_id"] for r in _waiters(store, j.job_id)} == {new.run_id}
    assert {r["run_id"] for r in _waiters(store, k.job_id)} == {old.run_id}


def test_withdraw_still_consumes_and_never_reattaches(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r"); now = time.time()
    old, _ = cr.request(store, _spec("t1"), job_id=j.job_id, purpose="verify", now=now)
    assert cr.withdraw_job(store, j.job_id, now=now + 1) == [old.run_id]
    w = _waiters(store, j.job_id)
    assert [r["run_id"] for r in w] == [old.run_id] and w[0]["consumed_epoch"] is not None
    assert cr.get(store, old.run_id).error == "withdrawn by the job"
    assert cr.follow_superseded(store, cr.get(store, old.run_id)) is None


def _occupy_slot(store, now):
    blocker, _ = cr.request(store, _spec("BUSY"), job_id="OTHER", purpose="verify", now=now)
    assert cr.try_start(store, blocker.run_id, owner="busy", pid=None, now=now)
    return blocker


def test_a_waiter_follows_a_same_tree_supersede_and_runs_it(steward_home, tmp_path):
    """The cdb0fd37 class: the job's own unshared request on the SAME tree superseded the run a background verify
    was queued on. The waiter follows it, runs it, and returns its result: no Lost, no crash."""
    store = Store(); j = store.create_job("g", "/tmp/r").job_id; now = time.time()
    wt = tmp_path / "wt"; wt.mkdir()
    checks = ScriptedChecks({str(wt): "T"})
    blocker = _occupy_slot(store, now)
    seen = {}

    def sleep(_s):
        if "new" in seen:
            return
        old = store._conn.execute("SELECT run_id FROM check_run_waiters WHERE job_id=? AND purpose='verify'",
                                  (j,)).fetchone()["run_id"]
        new, _ = cr.request(store, _spec("T", "python3 -m pytest -q#unshared:x"), job_id=j, purpose="verify",
                            now=now + 1)
        seen.update(old=old, new=new.run_id)
        cr.complete(store, blocker.run_id, owner="busy", run=CheckRun(exit_code=0, tests={}), junit_src=None,
                    now=now + 2)

    fn = cr.shared_check_fn(store, j, purpose="verify", predicted_s=600.0, clock=lambda: now + 3, sleep=sleep,
                            toolchain_fn=lambda c: "tc", alive=lambda pid: True)
    result = fn(checks, "python3 -m pytest -q", wt, tmp_path / "x.xml", "")
    assert result.exit_code == 0 and len(checks.executed) == 1
    assert cr.get(store, seen["old"]).state == "cancelled"
    assert cr.get(store, seen["new"]).state == "done"
    whats = [e.what for e in store.events(j)]
    assert "check_run_reattached" in whats and "check_run_lost" not in whats


def test_a_supersede_onto_another_tree_is_still_lost(steward_home, tmp_path):
    """A different tree is a moved candidate: another tree's receipt is never this tree's result."""
    store = Store(); j = store.create_job("g", "/tmp/r").job_id; now = time.time()
    wt = tmp_path / "wt"; wt.mkdir()
    checks = ScriptedChecks({str(wt): "T"})
    _occupy_slot(store, now)
    calls = []

    def sleep(_s):
        if not calls:
            calls.append(cr.request(store, _spec("T2"), job_id=j, purpose="verify", now=now + 1)[0].run_id)

    fn = cr.shared_check_fn(store, j, purpose="verify", predicted_s=600.0, clock=lambda: now + 3, sleep=sleep,
                            toolchain_fn=lambda c: "tc", alive=lambda pid: True)
    with pytest.raises(cr.Lost) as exc:
        fn(checks, "python3 -m pytest -q", wt, tmp_path / "x.xml", "")
    assert f"superseded by {calls[0]}" in str(exc.value) and checks.executed == []
