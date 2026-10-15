"""Issue M: job replacement links (`steward replace OLD NEW`, `steward new --replaces OLD`).

Replacing stops OLD through the normal stop control and records `replaced_by`/`replaces` on both jobs; accept,
incorporate and the autopilot/delivery merge paths refuse a replaced job; an already-merged OLD only warns.
Temp git repos, in-process control relay (conftest), no live store, no herdr."""

from __future__ import annotations

import pytest

from steward import autopilot, cli, closeout, delivery, replace, session
from steward import incorporate as inc
from steward.machine import IllegalTransition
from steward.models import JobState
from steward.runner_exec import SubprocessRunner
from steward.store import Store
from test_incorporate import git, mkjob, scratch  # noqa: F401 - scratch is a fixture


def _state(store, job_id, state):
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job_id))


def _plain(store, name="job", state=JobState.CONTRACTED, repo="/tmp/repo"):
    job = store.create_job(f"{name} goal", repo)
    if state != JobState.CREATED:
        _state(store, job.job_id, state)
    return job


def whats(store, job_id):
    return [e.what for e in store.events(job_id)]


def _links_recorded(store, old_id, new_id):
    assert store.kv_get(old_id, replace.K_REPLACED_BY) == new_id
    assert store.kv_get(new_id, replace.K_REPLACES) == old_id
    old_ev = [e for e in store.events(old_id) if e.what == "replaced"]
    new_ev = [e for e in store.events(new_id) if e.what == "replaces"]
    assert len(old_ev) == 1 and old_ev[0].evidence["replaced_by"] == new_id
    assert len(new_ev) == 1 and new_ev[0].evidence["replaces"] == old_id


# -- (1) `steward replace OLD NEW` stops OLD and records both links ---------------------------------------------

def test_replace_running_job_stops_it_and_records_both_links(steward_home, capsys):
    store = Store(steward_home)
    old = _plain(store, "old", JobState.RUNNING)
    new = _plain(store, "new", JobState.RUNNING)

    assert cli.main(["replace", old.job_id[:8], new.job_id[:8]]) == 0

    out = capsys.readouterr().out
    assert f"{old.job_id[:8]} replaced by {new.job_id[:8]}" in out
    assert store.get_job(old.job_id).state == JobState.STOPPING  # the normal stop control: worker cancel pending
    assert store.get_job(new.job_id).state == JobState.RUNNING
    _links_recorded(store, old.job_id, new.job_id)
    assert "operator stop" in whats(store, old.job_id)


def test_replace_worker_finished_job_stops_it_and_keeps_branch_and_worktree(steward_home, scratch, tmp_path, capsys):
    store = Store(steward_home)
    old = mkjob(store, scratch, tmp_path, {"old.txt": "old\n"}, state=JobState.WORKER_FINISHED)
    new = mkjob(store, scratch, tmp_path, {"new.txt": "new\n"}, state=JobState.RUNNING)
    branch = f"steward/{old.job_id}"
    worktrees_before = git(scratch, "worktree", "list")

    assert cli.main(["replace", old.job_id, new.job_id]) == 0

    assert store.get_job(old.job_id).state == JobState.STOPPED  # issue A: stop works from WORKER_FINISHED
    _links_recorded(store, old.job_id, new.job_id)
    assert git(scratch, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")  # branch kept
    assert git(scratch, "worktree", "list") == worktrees_before  # worktree kept
    assert "kept" in capsys.readouterr().out


def test_replace_refusals(steward_home, capsys):
    store = Store(steward_home)
    old = _plain(store, "old", JobState.RUNNING)
    new = _plain(store, "new", JobState.RUNNING)
    other = _plain(store, "other", JobState.RUNNING)
    dead = _plain(store, "dead", JobState.STOPPED)

    assert cli.main(["replace", old.job_id, old.job_id]) == 2
    assert "cannot replace itself" in capsys.readouterr().err
    assert cli.main(["replace", "nope", new.job_id]) == 1
    assert "unknown job to replace" in capsys.readouterr().err
    assert cli.main(["replace", old.job_id, dead.job_id]) == 2
    assert "STOPPED" in capsys.readouterr().err
    assert store.get_job(old.job_id).state == JobState.RUNNING  # nothing stopped on a refusal

    assert cli.main(["replace", old.job_id, new.job_id]) == 0
    assert cli.main(["replace", old.job_id, other.job_id]) == 2
    assert f"already replaced by {new.job_id[:8]}" in capsys.readouterr().err
    assert store.kv_get(other.job_id, replace.K_REPLACES) is None
    # re-running the same replace is a no-op, not an error
    assert cli.main(["replace", old.job_id, new.job_id]) == 0
    assert len([e for e in store.events(old.job_id) if e.what == "replaced"]) == 1


# -- (1) `steward new --replaces OLD` ----------------------------------------------------------------------------

def test_new_replaces_creates_the_job_links_both_and_stops_old(steward_home, scratch, tmp_path, capsys):
    store = Store(steward_home)
    old = mkjob(store, scratch, tmp_path, {"old.txt": "old\n"}, state=JobState.RUNNING)

    assert cli.main(["new", "redo the thing", "--no-baseline", "--replaces", old.job_id[:8]]) == 0

    out = capsys.readouterr().out
    new_id = out.splitlines()[0].split("\t")[0]
    new = store.get_job(new_id)
    assert new is not None and new.repo == str(scratch)  # --repo defaults to OLD's repo
    assert f"{old.job_id[:8]} replaced by {new_id[:8]}" in out
    assert store.get_job(old.job_id).state == JobState.STOPPING
    _links_recorded(store, old.job_id, new_id)


def test_new_replaces_unknown_job_creates_nothing(steward_home, scratch, capsys):
    store = Store(steward_home)
    assert cli.main(["new", "redo", "--repo", str(scratch), "--no-baseline", "--replaces", "nope"]) == 2
    assert "unknown job to replace" in capsys.readouterr().err
    assert store.list_jobs() == []


# -- (2) accept / incorporate / autopilot / delivery refuse a replaced job ------------------------------------

def test_accept_refuses_a_replaced_job_naming_the_replacement(steward_home, capsys):
    store = Store(steward_home)
    old = _plain(store, "old", JobState.NEEDS_HUMAN)
    new = _plain(store, "new", JobState.RUNNING)
    store.kv_set(old.job_id, replace.K_REPLACED_BY, new.job_id)  # link only: the job is still parked, not stopped

    assert cli.main(["accept", old.job_id]) == 2

    err = capsys.readouterr().err
    assert f"replaced by {new.job_id[:8]}" in err and "accept refused" in err
    assert store.get_job(old.job_id).state == JobState.NEEDS_HUMAN
    with pytest.raises(IllegalTransition, match=f"replaced by {new.job_id[:8]}"):
        closeout.accept(store, old.job_id)  # the approvals route lands here too


def test_incorporate_refuses_a_replaced_job_and_leaves_the_repo_alone(steward_home, scratch, tmp_path, capsys):
    store = Store(steward_home)
    old = mkjob(store, scratch, tmp_path, {"old.txt": "old\n"})  # ACCEPTED
    new = mkjob(store, scratch, tmp_path, {"new.txt": "new\n"}, state=JobState.RUNNING)
    store.kv_set(old.job_id, replace.K_REPLACED_BY, new.job_id)
    head = git(scratch, "rev-parse", "HEAD")

    receipt = inc.incorporate(store, old.job_id)
    assert receipt["accepted"] is False and receipt["error_kind"] == "replaced"
    assert f"replaced by {new.job_id[:8]}" in receipt["detail"]
    assert git(scratch, "rev-parse", "HEAD") == head and inc.is_incorporated(store, old.job_id) is None

    assert cli.main(["incorporate", old.job_id]) == 2
    assert f"replaced by {new.job_id[:8]}" in capsys.readouterr().err


def test_autopilot_skips_a_replaced_job(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    old = mkjob(store, scratch, tmp_path, {"old.txt": "old\n"})
    new = mkjob(store, scratch, tmp_path, {"new.txt": "new\n"}, state=JobState.RUNNING)
    store.kv_set(old.job_id, replace.K_REPLACED_BY, new.job_id)
    head = git(scratch, "rev-parse", "HEAD")

    res = autopilot.try_merge(store, old.job_id, SubprocessRunner(), checks=[])

    assert res["outcome"] == "skipped" and f"replaced by {new.job_id[:8]}" in res["detail"]
    assert git(scratch, "rev-parse", "HEAD") == head and "incorporated" not in whats(store, old.job_id)
    skipped = [e for e in store.events(old.job_id) if e.what == "autopilot_skipped"]
    assert skipped and new.job_id[:8] in skipped[-1].evidence["reason"]


def test_delivery_skips_a_replaced_job(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    monkeypatch.setattr(autopilot, "_check_lines", lambda tree: [])
    store = Store(steward_home)
    old = mkjob(store, scratch, tmp_path, {"old.txt": "old\n"})
    new = mkjob(store, scratch, tmp_path, {"new.txt": "new\n"}, state=JobState.RUNNING)
    store.kv_set(old.job_id, replace.K_REPLACED_BY, new.job_id)
    head = git(scratch, "rev-parse", "HEAD")
    spawned = []

    swept = delivery.sweep(store, now=1000.0, spawn=lambda *a: spawned.append(a) or 1)
    result = delivery.run_delivery(store, old.job_id, SubprocessRunner(), now=1000.0)

    assert swept["launched"] == [] and spawned == []
    assert result["step"] == "none"
    assert f"replaced by {new.job_id[:8]}" in delivery.next_step(store, old.job_id, now=1000.0)["reason"]
    assert git(scratch, "rev-parse", "HEAD") == head and inc.is_incorporated(store, old.job_id) is None


# -- (3) replacing an already-merged job warns and still records the link ---------------------------------------

def test_replace_already_merged_job_warns_and_records_the_link(steward_home, scratch, tmp_path, capsys):
    store = Store(steward_home)
    old = mkjob(store, scratch, tmp_path, {"old.txt": "old\n"})
    assert inc.incorporate(store, old.job_id)["accepted"]
    merged = inc.is_incorporated(store, old.job_id)
    new = mkjob(store, scratch, tmp_path, {"new.txt": "new\n"}, state=JobState.RUNNING)

    assert cli.main(["replace", old.job_id, new.job_id]) == 0

    captured = capsys.readouterr()
    assert "warning:" in captured.err and "already incorporated" in captured.err and merged[:7] in captured.err
    assert store.get_job(old.job_id).state == JobState.ACCEPTED  # nothing stopped
    _links_recorded(store, old.job_id, new.job_id)
    assert "operator stop" not in whats(store, old.job_id)


def test_new_replaces_already_merged_job_warns(steward_home, scratch, tmp_path, capsys):
    store = Store(steward_home)
    old = mkjob(store, scratch, tmp_path, {"old.txt": "old\n"})
    assert inc.incorporate(store, old.job_id)["accepted"]

    assert cli.main(["new", "redo", "--no-baseline", "--replaces", old.job_id]) == 0

    captured = capsys.readouterr()
    new_id = captured.out.splitlines()[0].split("\t")[0]
    assert "warning:" in captured.err and "already incorporated" in captured.err
    assert store.get_job(old.job_id).state == JobState.ACCEPTED
    _links_recorded(store, old.job_id, new_id)


# -- (4) `steward result` and `steward jobs` show the links -------------------------------------------------------

def test_result_and_jobs_show_replaced_by_and_replaces(steward_home, capsys):
    store = Store(steward_home)
    old = _plain(store, "old", JobState.RUNNING)
    new = _plain(store, "new", JobState.RUNNING)
    assert cli.main(["replace", old.job_id, new.job_id]) == 0
    capsys.readouterr()

    assert cli.main(["jobs", "--all"]) == 0
    jobs = capsys.readouterr().out
    assert f"[replaced by {new.job_id[:8]}]" in jobs and f"[replaces {old.job_id[:8]}]" in jobs

    assert cli.main(["result", old.job_id]) == 0
    assert f"replaced by {new.job_id[:8]}" in capsys.readouterr().out
    assert cli.main(["result", new.job_id]) == 0
    assert f"replaces {old.job_id[:8]}" in capsys.readouterr().out

    rows = {r["job_id"]: r for r in session.list_rows(store, all_jobs=True, me={})}
    assert rows[old.job_id]["replaced_by"] == new.job_id and rows[new.job_id]["replaces"] == old.job_id
    assert replace.labels(store, old.job_id) == [f"replaced by {new.job_id[:8]}"]
    assert replace.labels(store, new.job_id) == [f"replaces {old.job_id[:8]}"]
