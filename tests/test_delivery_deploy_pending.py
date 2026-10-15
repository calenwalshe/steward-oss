"""Delivery latency fix #6 (2026-10-07): a delivery merge of Steward's own code marks deploy_pending before it lands
(runners wait for deploy-self instead of self-restarting ahead of the control), and the marks are a list so one job's
clear never drops another job's mark."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from steward import activation, autopilot, check_runs, delivery, deploy_pending
from steward import incorporate as inc
from steward.runner_exec import SubprocessRunner
from steward.store import Store
from test_incorporate import git, mkjob, scratch  # noqa: F401 - scratch is a fixture


@pytest.fixture
def steward_repo(scratch, monkeypatch):
    """The scratch repo plays Steward's own checkout (it has a steward/ package)."""
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    monkeypatch.setattr(autopilot, "_is_steward_repo", lambda repo, root: True)
    monkeypatch.setattr(autopilot, "_check_lines", lambda tree: [])
    (scratch / "steward").mkdir()
    (scratch / "steward" / "x.py").write_text("x=1\n")
    git(scratch, "add", "-A")
    git(scratch, "commit", "-qm", "pkg")
    return scratch


def deliver(store, jid):
    return delivery.run_delivery(store, jid, SubprocessRunner(), now=time.time())


def test_delivery_steward_merge_marks_pending(steward_home, steward_repo, tmp_path, monkeypatch):
    store = Store(steward_home)
    jid = mkjob(store, steward_repo, tmp_path, {"steward/y.py": "y=1\n"}).job_id
    seen, real = {}, inc.incorporate

    def spy(*a, **kw):
        seen["at_merge"] = deploy_pending.pending(store)
        return real(*a, **kw)

    monkeypatch.setattr(inc, "incorporate", spy)
    assert deliver(store, jid)["outcome"] == "incorporated"
    assert seen["at_merge"]["job_id"] == jid  # set BEFORE the merge reached the live checkout
    assert deploy_pending.pending(store)["job_id"] == jid  # held until deploy-self / activation


def test_non_steward_merge_does_not_mark(steward_home, steward_repo, tmp_path):
    store = Store(steward_home)
    jid = mkjob(store, steward_repo, tmp_path, {"docs.txt": "d\n"}).job_id
    assert deliver(store, jid)["outcome"] == "incorporated" and deploy_pending.pending(store) is None


def test_two_pending_merges_both_hold_runners(steward_home, steward_repo, tmp_path):
    store = Store(steward_home)
    j1 = mkjob(store, steward_repo, tmp_path, {"steward/y.py": "y=1\n"}).job_id
    j2 = mkjob(store, steward_repo, tmp_path, {"steward/z.py": "z=1\n"}).job_id
    assert deliver(store, j1)["outcome"] == "incorporated"
    assert deliver(store, j2)["outcome"] == "incorporated"
    assert {m["job_id"] for m in deploy_pending._marks(store)} == {j1, j2}
    assert deploy_pending.pending(store)["job_id"] == j1  # the oldest
    deploy_pending.clear(store, j1)  # j1's attempt ended: j2's merge still waits for its deploy
    assert deploy_pending.pending(store)["job_id"] == j2
    deploy_pending.clear(store, j2)
    assert deploy_pending.pending(store) is None


def test_activation_failure_clears_own_mark_only(steward_home, steward_repo, tmp_path, monkeypatch):
    store = Store(steward_home)
    jid = mkjob(store, steward_repo, tmp_path, {"steward/y.py": "y=1\n"}).job_id
    assert deliver(store, jid)["outcome"] == "incorporated"
    deploy_pending.mark(store, "other-job")
    monkeypatch.setattr(delivery, "next_step", lambda s, j, now=None: {
        "kind": "activate", "milestone": "activation", "revision": inc.is_incorporated(store, jid)})
    monkeypatch.setattr(activation, "activate", lambda *a, **kw: {"activated": False, "reason": "deploy failed: x"})
    assert deliver(store, jid)["activated"] is False
    assert [m["job_id"] for m in deploy_pending._marks(store)] == ["other-job"]


def test_activation_waiting_keeps_mark(steward_home, steward_repo, tmp_path, monkeypatch):
    store = Store(steward_home)
    jid = mkjob(store, steward_repo, tmp_path, {"steward/y.py": "y=1\n"}).job_id
    assert deliver(store, jid)["outcome"] == "incorporated"
    monkeypatch.setattr(delivery, "next_step", lambda s, j, now=None: {
        "kind": "activate", "milestone": "activation", "revision": inc.is_incorporated(store, jid)})
    monkeypatch.setattr(activation, "activate", lambda *a, **kw: {"activated": False,
                                                                  "reason": "deploy already in progress"})
    deliver(store, jid)
    assert deploy_pending.pending(store)["job_id"] == jid


def test_red_post_merge_verification_clears_own_mark(steward_home, steward_repo, tmp_path, monkeypatch):
    store = Store(steward_home)
    jid = mkjob(store, steward_repo, tmp_path, {"steward/y.py": "y=1\n"}).job_id
    deploy_pending.mark(store, "other-job")
    monkeypatch.setattr(autopilot, "_check_lines", lambda tree: ["pytest -q"])
    monkeypatch.setattr(autopilot, "_test_merged_tree", lambda *a, **k: (True, "not-the-merged-tree", []))
    monkeypatch.setattr(check_runs, "shared_check_fn", lambda _s, _j, *, purpose, **kw: (
        lambda runner, line, cwd, xml, env="": SimpleNamespace(exit_code=1, broken=None, tests={})))
    res = deliver(store, jid)
    assert res["outcome"] == "incorporated" and res["receipt"]["post_merge_verification"]["green"] is False
    assert [m["job_id"] for m in deploy_pending._marks(store)] == ["other-job"]


def test_land_that_did_not_merge_clears_mark(steward_home, steward_repo, tmp_path, monkeypatch):
    store = Store(steward_home)
    jid = mkjob(store, steward_repo, tmp_path, {"steward/y.py": "y=1\n"}).job_id
    monkeypatch.setattr(inc, "incorporate", lambda *a, **kw: {"accepted": False, "error_kind": "incorporate_blocked",
                                                              "detail": "blocked: git merge failed (x)"})
    assert deliver(store, jid)["outcome"] == "blocked" and deploy_pending.pending(store) is None


def test_deploy_self_success_clears_all(steward_home):
    store = Store(steward_home)
    deploy_pending.mark(store, "a")
    deploy_pending.mark(store, "b")
    deploy_pending.clear(store)  # what deploy_self does on success
    assert deploy_pending.pending(store) is None and deploy_pending._marks(store) == []


def test_mark_refreshes_its_own_entry(steward_home):
    store = Store(steward_home)
    deploy_pending.mark(store, "a", now=time.time() - deploy_pending.MAX_AGE_S - 1)
    deploy_pending.mark(store, "b")
    deploy_pending.mark(store, "a")
    assert sorted(m["job_id"] for m in deploy_pending._marks(store)) == ["a", "b"]
    assert deploy_pending.pending(store)["job_id"] == "b"  # a's refresh is newer than b


def test_legacy_single_slot_mark_is_honoured_and_never_wipes_the_list(steward_home):
    """A worker still on the pre-list code during a deploy writes/clears the old single slot."""
    store = Store(steward_home)
    deploy_pending.mark(store, "new")
    store.kv_set(deploy_pending.KV_JOB, deploy_pending.KV_KEY, {"job_id": "old", "since": time.time() - 5})
    assert deploy_pending.pending(store)["job_id"] == "old"
    store.kv_delete(deploy_pending.KV_JOB, deploy_pending.KV_KEY)  # the old code's clear
    assert deploy_pending.pending(store)["job_id"] == "new"
    store.kv_set(deploy_pending.KV_JOB, deploy_pending.KV_KEY, {"job_id": "old", "since": time.time()})
    deploy_pending.clear(store, "old")
    assert deploy_pending.pending(store)["job_id"] == "new"
