"""Phase 28-02 (FLEET-14): the source checkout changes only on an explicit incorporate of the accepted current SHA;
release stays a separate human action (scratch git repos + real Store only)."""

from __future__ import annotations

import pytest
from test_fleet_closeout import Ready
from test_fleet_integration import git
from test_fleet_lanes import JOB

from steward import cli, closeout, incorporate
from steward import fleet_closeout as fc
from steward.models import JobState


@pytest.fixture
def acc(steward_home, tmp_path, monkeypatch):
    """Fleet job with a verified combined candidate, human-ACCEPTED; plus a dirty/staged/untracked source repo."""
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "auto")
    r = Ready(tmp_path, steward_home)
    assert r.fco.run(JOB)["status"] == "candidate"
    r.e.repo_snapshot = None
    return r


def snap(repo):
    return {"head": git(repo, "rev-parse", "HEAD"), "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
            "status": git(repo, "status", "--porcelain", "-uall"), "branch_sha": git(repo, "rev-parse",
                                                                                      f"steward/{JOB}")}


def make_dirty(repo):
    (repo / "untracked.txt").write_text("u")
    (repo / "pyproject.toml").write_text("# local edit\n")  # dirty tracked file the merge never touches


def events(store):
    return [e.what for e in store.events(JOB)]


def test_not_accepted_fleet_job_cannot_change_source(acc):
    e = acc.e
    make_dirty(e.repo)
    before = snap(e.repo)
    assert acc.state() == JobState.NEEDS_HUMAN
    r = incorporate.incorporate(e.store, JOB)
    assert r["accepted"] is False and "needs ACCEPTED" in r["detail"]
    assert snap(e.repo) == before and "incorporated" not in events(e.store)


def test_stale_candidate_or_missing_candidate_blocks_without_mutation(acc):
    e = acc.e
    closeout.accept(e.store, JOB)
    p = e.writer.integration_path(JOB)
    (p / "late.txt").write_text("x")
    git(p, "add", "-A")
    git(p, "-c", "user.name=W", "-c", "user.email=w@x", "commit", "-q", "-m", "after acceptance")
    make_dirty(e.repo)
    before = snap(e.repo)
    r = incorporate.incorporate(e.store, JOB)
    assert r["accepted"] is False and "stale fleet candidate" in r["detail"]
    assert snap(e.repo) == before
    # ledger/branch both moved together is still not the accepted candidate
    pf = incorporate.fleet_guard(e.store, JOB)
    assert pf and "differs" in pf
    assert snap(e.repo) == before and "incorporated" not in events(e.store)


def test_no_candidate_blocks(steward_home, tmp_path):
    r = Ready(tmp_path, steward_home)  # WORKER_FINISHED, never verified: force the state, no candidate
    r.e.store.set_state(JOB, JobState.NEEDS_HUMAN, what="h", evidence={}, rule="t",
                        judgment=__import__("steward.models", fromlist=["Judgment"]).Judgment.code)
    closeout.accept(r.e.store, JOB)
    before = snap(r.e.repo)
    out = incorporate.incorporate(r.e.store, JOB)
    assert out["accepted"] is False and "no verified combined candidate" in out["detail"]
    assert snap(r.e.repo) == before


def test_released_fleet_job_is_not_incorporable(acc):
    e = acc.e
    closeout.accept(e.store, JOB)
    closeout.release(e.store, JOB)
    before = snap(e.repo)
    assert incorporate.incorporate(e.store, JOB)["accepted"] is False
    assert snap(e.repo) == before


def test_dry_run_and_preflight_preserve_source(acc, capsys):
    e = acc.e
    closeout.accept(e.store, JOB)
    make_dirty(e.repo)
    (e.repo / "staged_not.txt").write_text("s")
    before = snap(e.repo)
    pf = incorporate.preflight(e.store, JOB)
    assert not pf.block and incorporate.fleet_guard(e.store, JOB) is None
    assert cli.main(["incorporate", JOB, "--dry-run"]) == 0
    assert "nothing was changed" in capsys.readouterr().out
    assert snap(e.repo) == before and "incorporated" not in events(e.store)


def test_explicit_incorporation_merges_accepted_sha_and_leaves_job_accepted(acc):
    e = acc.e
    cand = fc.final_candidate(e.store, JOB)
    closeout.accept(e.store, JOB)
    make_dirty(e.repo)
    before = snap(e.repo)
    assert before["head"] != cand["final_sha"]
    r = incorporate.incorporate(e.store, JOB)
    assert r["accepted"] is True and r["merge_commit"]
    after = snap(e.repo)
    assert after["head"] == r["merge_commit"] and after["status"] == before["status"]  # dirty files untouched
    assert git(e.repo, "merge-base", "--is-ancestor", cand["final_sha"], "HEAD") == ""
    assert (e.repo / "src/a/a.py").exists() and (e.repo / "src/b/b.py").exists()
    ev = next(x for x in e.store.events(JOB) if x.what == "incorporated")
    assert ev.judgment.value == "human" and ev.rule == "operator_incorporate"
    assert e.store.get_job(JOB).state == JobState.ACCEPTED  # not released


def test_incorporate_release_rejected_for_fleet_before_merge_then_release_is_separate(acc):
    e = acc.e
    closeout.accept(e.store, JOB)
    before = snap(e.repo)
    handlers = cli._build_control_handlers(e.store)
    rej = handlers["incorporate"]({"op": "incorporate", "args": {"job_id": JOB, "release": True}})
    assert rej["accepted"] is False and rej["error_kind"] == "fleet_release_separate"
    assert snap(e.repo) == before and e.store.get_job(JOB).state == JobState.ACCEPTED
    ok = handlers["incorporate"]({"op": "incorporate", "args": {"job_id": JOB, "release": False}})
    assert ok["accepted"] and e.store.get_job(JOB).state == JobState.ACCEPTED
    rel = handlers["release"]({"op": "release", "args": {"job_id": JOB}})
    assert rel["accepted"] and e.store.get_job(JOB).state == JobState.RELEASED


def test_legacy_job_incorporate_release_unchanged(steward_home, tmp_path):
    from steward.store import Store
    store = Store(steward_home)
    store.create_job("g", str(tmp_path), job_id="legacy1")
    assert incorporate.fleet_guard(store, "legacy1") is None
