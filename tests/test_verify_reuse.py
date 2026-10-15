"""Verification reuse on merge (latency #4): temp git repos, fake check runner, no live store."""

from __future__ import annotations

import csv
from pathlib import Path

from steward import autopilot, verify_reuse
from steward.baseline import CheckResult
from steward.store import Store
from steward.verify import Verification
from test_autopilot import go, runner, whats
from test_incorporate import git, mkjob, scratch  # noqa: F401  (scratch is a fixture)

DATA = Path(__file__).resolve().parent / "fixtures" / "latency"


def _verified(store, scratch, job, *, commands=("pytest -q",), exit_code=0):
    sha = git(scratch, "rev-parse", f"steward/{job.job_id}")
    v = Verification(commit_sha=sha, results=[CheckResult(command=c, exit_code=exit_code, tests={}) for c in commands])
    return verify_reuse.record(store, job.job_id, v)


def _checks_run(log):
    return [a for a in log if a[:2] == ["bash", "-lc"]]


def test_identical_merged_tree_reuses_the_closeout_verification(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"new.txt": "x\n"})
    _verified(store, scratch, job)
    log = []
    res = go(store, job, runner(log=log))
    assert res["outcome"] == "merged"
    assert _checks_run(log) == []  # no re-test of identical content
    (ev,) = [e for e in store.events(job.job_id) if e.what == "merged_tree_verification_reused"]
    assert ev.evidence["commands"] == ["pytest -q"] and ev.evidence["tested_commit"]
    assert ev.rule == verify_reuse.RULE


def test_moved_source_branch_means_a_real_retest(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"new.txt": "x\n"})
    _verified(store, scratch, job)
    (scratch / "other.txt").write_text("main moved\n")
    git(scratch, "add", "-A")
    git(scratch, "commit", "-qm", "main moved")
    log = []
    assert go(store, job, runner(log=log))["outcome"] == "merged"
    assert len(_checks_run(log)) == 1
    assert "merged_tree_verification_needed" in whats(store, job)


def test_different_checks_red_verification_or_new_toolchain_retest(steward_home, scratch, tmp_path, monkeypatch):
    store = Store(steward_home)
    for setup in ("commands", "red", "toolchain", "off", "no_receipt"):
        job = mkjob(store, scratch, tmp_path, {f"{setup}.txt": "x\n"})
        if setup == "commands":
            _verified(store, scratch, job, commands=("pytest -q", "ruff check"))
        elif setup == "red":
            _verified(store, scratch, job, exit_code=1)
        elif setup in ("toolchain", "off"):
            _verified(store, scratch, job)
        with monkeypatch.context() as m:
            if setup == "toolchain":
                m.setattr(verify_reuse, "toolchain_fingerprint", lambda cmds: "upgraded")
            if setup == "off":
                m.setenv(verify_reuse.ENV, "0")
            log = []
            # a red re-test proves the checks really ran (and nothing merged)
            res = go(store, job, runner(red=True, log=log), start_followup=lambda *a: {"accepted": False})
        assert len(_checks_run(log)) == 1, setup
        assert res["outcome"] != "merged", setup


def test_receipt_records_green_commands_and_toolchain(steward_home):
    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    v = Verification(commit_sha="abc", results=[CheckResult(command="pytest -q", exit_code=0, tests={})])
    r = verify_reuse.record(store, job.job_id, v, fingerprint_fn=lambda c: "fp")
    assert r == {"commit_sha": "abc", "commands": ["pytest -q"], "all_green": True, "toolchain": "fp"}
    assert not verify_reuse.record(store, job.job_id, Verification(commit_sha="abc"))["all_green"]
    assert verify_reuse.reusable(store, job.job_id, merged_tree="t", job_tree="t", commands=[],
                                 fingerprint_fn=lambda c: "fp") == (False, "verification was not all green")


def test_closeout_records_the_receipt_at_checks_passed(steward_home):
    from steward import closeout
    from steward.models import JobState, Judgment

    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    store.set_state(job.job_id, JobState.CONTRACTED, what="c", evidence={}, rule="t", judgment=Judgment.code)
    store.set_state(job.job_id, JobState.RUNNING, what="r", evidence={}, rule="t", judgment=Judgment.code)
    store.set_state(job.job_id, JobState.WORKER_FINISHED, what="f", evidence={}, rule="t", judgment=Judgment.code)
    v = Verification(commit_sha="abc", results=[CheckResult(command="pytest -q", exit_code=0, tests={})])
    closeout.closeout(store, job.job_id, v, [], None)
    assert store.kv_get(job.job_id, verify_reuse.RECEIPT_KEY)["commit_sha"] == "abc"


def test_measured_eligibility_matches_the_diagnostic():
    """data/retest.csv: 27 of 35 auto-merges merged a tree identical to the job's tested tree — the cases this
    reuse covers (same content; a moved target means a re-test)."""
    with open(DATA / "retest.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    eligible = [r for r in rows if r["merged_tree_equals_job_tree"] == "True"]
    assert (len(eligible), len(rows)) == (27, 35)
    saved_h = sum(float(r["accept_to_merge_s"]) for r in eligible) / 3600
    assert saved_h > 0.5
