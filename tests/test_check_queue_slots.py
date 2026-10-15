"""Mechanics item 10: bounded heavy-run parallelism, and a job's newer request supersedes its own stale queued run."""
from __future__ import annotations

import pytest

from steward import bg_verify, check_runs as cr
from steward.store import Store


def spec(tree, command="python3 -m pytest -q", predicted_s=600.0):
    return cr.RunSpec(tree_sha=tree, command=command, env_digest="e", toolchain="tc", heavy=True,
                      predicted_s=predicted_s)


def test_default_is_three_heavy_slots_in_fifo_order(steward_home, monkeypatch):
    monkeypatch.delenv(cr.HEAVY_SLOTS_ENV, raising=False)
    s = Store(steward_home)
    rows = [cr.request(s, spec(t), job_id=f"J{t}", purpose="verify", now=1.0 + i)[0] for i, t in enumerate("ABCDE")]
    started = [cr.try_start(s, r.run_id, owner=f"p{i}", pid=None, now=10.0) for i, r in enumerate(rows)]
    assert started == [True, True, True, False, False]
    assert cr.queue_position(s, rows[3].run_id)[0] == 2 and cr.queue_position(s, rows[4].run_id)[0] == 3
    assert "3 heavy suites per host" in (cr.status_line(s, "JD", now=10.0) or "")


def test_fifo_holds_with_slots_free(steward_home, monkeypatch):
    monkeypatch.setenv(cr.HEAVY_SLOTS_ENV, "2")
    s = Store(steward_home)
    a = cr.request(s, spec("A"), job_id="JA", purpose="verify", now=1.0)[0]
    b = cr.request(s, spec("B"), job_id="JB", purpose="verify", now=2.0)[0]
    c = cr.request(s, spec("C"), job_id="JC", purpose="verify", now=3.0)[0]
    assert cr.try_start(s, a.run_id, owner="pa", pid=None, now=4.0)
    assert not cr.try_start(s, c.run_id, owner="pc", pid=None, now=4.0)  # B is still ahead in line
    assert cr.try_start(s, b.run_id, owner="pb", pid=None, now=4.0)
    assert not cr.try_start(s, c.run_id, owner="pc", pid=None, now=4.0)  # both slots busy


@pytest.mark.parametrize("raw,want", [("", 3), ("1", 1), ("6", 6), ("0", 1), ("x", 3)])
def test_slot_setting(monkeypatch, raw, want):
    monkeypatch.setenv(cr.HEAVY_SLOTS_ENV, raw)
    assert cr.heavy_slots() == want


def test_newer_request_cancels_the_jobs_own_stale_queued_run(steward_home):
    s = Store(steward_home)
    j = s.create_job("verify it", "/repo").job_id
    # the inline path's unshared run, then the background run at the same tree (the observed duplicate) ...
    inline = cr.request(s, spec("T1", "python3 -m pytest -q#unshared:abc"), job_id=j, purpose="verify", now=1.0)[0]
    bg = cr.request(s, spec("T1"), job_id=j, purpose="verify", now=2.0)[0]
    assert cr.get(s, inline.run_id).state == "cancelled"
    # ... then the candidate moves: the old queued run is released too
    newer = cr.request(s, spec("T2"), job_id=j, purpose="verify", now=3.0)[0]
    assert cr.get(s, bg.run_id).state == "cancelled" and cr.get(s, newer.run_id).state == "queued"
    assert [e.evidence["run_id"] for e in s.events(j) if e.what == "check_run_superseded"] == [inline.run_id, bg.run_id]


def test_supersede_keeps_a_run_another_job_still_waits_on(steward_home):
    s = Store(steward_home)
    shared = cr.request(s, spec("T1"), job_id="JA", purpose="verify", now=1.0)[0]
    cr.request(s, spec("T1"), job_id="JB", purpose="verify", now=2.0)
    cr.request(s, spec("T2"), job_id="JA", purpose="verify", now=3.0)
    assert cr.get(s, shared.run_id).state == "queued"
    assert {w["job_id"] for w in cr.waiters(s, shared.run_id) if w["consumed_epoch"] is None} == {"JB"}


def test_supersede_never_touches_a_running_run_or_another_purpose(steward_home):
    s = Store(steward_home)
    run = cr.request(s, spec("T1"), job_id="JA", purpose="verify", now=1.0)[0]
    assert cr.try_start(s, run.run_id, owner="p", pid=None, now=1.5)
    base = cr.request(s, spec("B0"), job_id="JA", purpose="baseline", now=2.0)[0]
    cr.request(s, spec("T2"), job_id="JA", purpose="verify", now=3.0)
    assert cr.get(s, run.run_id).state == "running" and cr.get(s, base.run_id).state == "queued"


def test_a_cancelled_run_is_lost_to_a_lingering_poller(steward_home, tmp_path):
    s = Store(steward_home)
    j = s.create_job("verify it", "/repo").job_id
    old = cr.request(s, spec("T1"), job_id=j, purpose="verify", now=1.0)[0]
    cr.request(s, spec("T2"), job_id=j, purpose="verify", now=2.0)
    assert cr.get(s, old.run_id).state in cr.FINAL


def test_a_superseded_background_run_cannot_overwrite_the_newer_record(steward_home):
    s = Store(steward_home)
    j = s.create_job("verify it", "/repo").job_id
    s.kv_set(j, bg_verify.KEY, {"status": "running", "sha": "new", "worktree": "/w", "started_at": 1.0})
    bg_verify.finish(s, j, status="failed", error="Lost: superseded", sha="old")
    assert bg_verify.record(s, j)["status"] == "running" and bg_verify.record(s, j)["sha"] == "new"
    bg_verify.finish(s, j, status="failed", error="boom", sha="new")
    assert bg_verify.record(s, j)["status"] == "failed"
