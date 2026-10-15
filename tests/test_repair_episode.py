"""Plan 4 Task 9: a moving target reuses one repair episode; no fix-up of a fix-up; attempt limits trigger a
strategy review, never the owner (spec §7, §9; Defect A; P4-15..20)."""
from __future__ import annotations

import subprocess
import threading

from steward import autopilot, blockers, delivery, incorporate, repair_episode as rep
from steward.models import JobState
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store
from owner_surface import OwnerSurface
from test_incorporate import git, mkjob, scratch  # noqa: F401 - scratch is a fixture

OK = FakeRunner(lambda argv, cwd: CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="",
                                                duration_s=0.0))


def set_state(store, jid, state):
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, jid))


def advance_main(repo, text):
    (repo / "a.txt").write_text(text)
    git(repo, "commit", "-qam", f"main: {text.strip()}")


class Attempts:
    """start_followup fake: creates the attempt's branch at the requested start (what `steward new --follow-up` does)."""

    def __init__(self, repo, tmp_path, launch_error=None):
        self.repo, self.tmp, self.calls, self.launch_error = repo, tmp_path, [], launch_error

    def __call__(self, store, parent_id, goal):
        start = store.kv_get(parent_id, rep.START_REF_KEY) or f"steward/{parent_id}"
        child = store.create_job(goal, str(self.repo)).job_id
        wt = self.tmp / f"att-{child[:6]}"
        git(self.repo, "worktree", "add", "-q", "-b", f"steward/{child}", str(wt), start)
        set_state(store, child, JobState.RUNNING)
        self.calls.append({"parent": parent_id, "start": start, "child": child, "wt": wt})
        out = {"accepted": True, "job_id": child, "launched": True}
        return {**out, "launch_error": self.launch_error} if self.launch_error else out

    def resolve(self, store, child, text):
        """The attempt's worker merges the target into its branch and resolves a.txt (the merge itself conflicts)."""
        wt = next(c["wt"] for c in self.calls if c["child"] == child)
        subprocess.run(["git", "merge", "main", "--no-edit"], cwd=wt, capture_output=True, text=True)
        (wt / "a.txt").write_text(text)
        git(wt, "add", "-A")
        git(wt, "commit", "-qm", "resolve")
        set_state(store, child, JobState.ACCEPTED)


def conflicted_root(store, scratch, tmp_path):
    root = mkjob(store, scratch, tmp_path, {"a.txt": "root\n"}).job_id
    advance_main(scratch, "main1\n")
    return root


def merge(store, jid, attempts):
    return autopilot.try_merge(store, jid, OK, check_runner=OK, checks=[], start_followup=attempts)


def test_fixup_of_a_fixup_is_never_spawned_when_source_moves(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    att = Attempts(scratch, tmp_path)
    root = conflicted_root(store, scratch, tmp_path)
    assert merge(store, root, att)["outcome"] == "followup"
    x1 = att.calls[0]["child"]
    att.resolve(store, x1, "root+main1\n")
    advance_main(scratch, "main2\n")  # the target moves again in the same file while x1 was being resolved
    res = merge(store, x1, att)
    assert res["outcome"] == "followup"
    x2 = att.calls[1]["child"]
    assert att.calls[1]["parent"] == root and att.calls[1]["start"] == f"steward/{x1}"
    assert store.kv_get(x2, "autopilot_followup") == root and autopilot.followup_depth(store, x2) == 1
    assert not [j for j in store.list_jobs() if store.kv_get(j.job_id, "autopilot_followup") == x1]
    ep = rep.open_for(store, root)
    assert ep.attempts == 2 and ep.current_job == x2 and [h["event"] for h in ep.history].count("attempt") == 2


def test_target_moving_during_the_attempt_retries_in_place(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    root = conflicted_root(store, scratch, tmp_path)
    pf = incorporate.preflight(store, root)
    stale = git(scratch, "rev-parse", "HEAD")
    (scratch / "b.txt").write_text("unrelated\n")
    git(scratch, "add", "b.txt")
    git(scratch, "commit", "-qm", "unrelated")
    d = rep.decide(store, store.get_job(root), pf, detail="merge conflict", files=pf.conflicts, now=1.0,
                   pf_target_head=stale)
    assert d["action"] == "retry_in_place" and store.list_jobs() == [store.get_job(root)]


def test_unchanged_conflict_after_an_attempt_requests_strategy_review_not_a_new_job(steward_home, scratch, tmp_path,
                                                                                   monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    surf = OwnerSurface(store, monkeypatch)
    att = Attempts(scratch, tmp_path)
    root = conflicted_root(store, scratch, tmp_path)
    merge(store, root, att)
    x1 = att.calls[0]["child"]
    set_state(store, x1, JobState.ACCEPTED)  # the attempt "finished" without resolving anything
    res = merge(store, x1, att)
    assert res["outcome"] == "strategy_review" and len(att.calls) == 1
    assert rep.open_for(store, root).state == "strategy_review"
    assert any(b.kind == "repeated_failure" for b in blockers.open_blockers(store, root))  # Plan 1 kind routed by Plan 3
    assert surf.owner_requests() == []


def test_attempt_limit_requests_strategy_review_not_owner(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    surf = OwnerSurface(store, monkeypatch)
    att = Attempts(scratch, tmp_path)
    root = conflicted_root(store, scratch, tmp_path)
    merge(store, root, att)
    for n, text in ((0, "main2\n"), (1, "main3\n")):
        child = att.calls[n]["child"]
        att.resolve(store, child, f"resolved {n}\n")
        advance_main(scratch, text)
        res = merge(store, child, att)
    assert res["outcome"] == "strategy_review" and len(att.calls) == 2
    assert not any(e.what == "autopilot_escalated" for e in store.events(root))
    assert surf.owner_requests() == []


def test_concurrent_failures_start_one_attempt(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    att = Attempts(scratch, tmp_path)
    root = conflicted_root(store, scratch, tmp_path)
    pf = incorporate.preflight(store, root)
    job = store.get_job(root)
    threads = [threading.Thread(target=autopilot._failed, args=(Store(steward_home), job, pf, "merge conflict",
                                                                pf.conflicts, OK, att, None)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(att.calls) == 1


def test_a_live_attempt_coalesces_further_failures(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    att = Attempts(scratch, tmp_path)
    root = conflicted_root(store, scratch, tmp_path)
    merge(store, root, att)
    store.kv_delete(root, autopilot.FIXUP_CLAIM_KEY)
    store.kv_delete(root, autopilot.ATTEMPT_KEY)
    res = merge(store, root, att)  # x1 is still RUNNING
    assert res["outcome"] == "pending" and len(att.calls) == 1


def test_fix_merge_tap_reuses_the_episode(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    att = Attempts(scratch, tmp_path)
    root = conflicted_root(store, scratch, tmp_path)
    merge(store, root, att)
    store.kv_delete(root, autopilot.FIXUP_CLAIM_KEY)
    res = autopilot.start_fix_merge(store, root, OK, start_followup=att)
    assert not res["accepted"] and "needs you" not in res["detail"] and len(att.calls) == 1


def test_attempt_launch_failure_is_a_reviewable_blocker(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    notified = []
    att = Attempts(scratch, tmp_path, launch_error="no worker slot")
    root = conflicted_root(store, scratch, tmp_path)
    res = autopilot.try_merge(store, root, OK, check_runner=OK, checks=[], start_followup=att,
                              notify=lambda *a: notified.append(a))
    assert res["outcome"] == "blocked" and notified == []
    (b,) = [b for b in blockers.open_blockers(store, root) if b.kind == delivery.LAUNCH_KIND]
    assert b.disposition == "candidate" and "no worker slot" in b.claim


def test_resolution_marks_only_contained_attempts(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    att = Attempts(scratch, tmp_path)
    root = conflicted_root(store, scratch, tmp_path)
    merge(store, root, att)
    x1 = att.calls[0]["child"]
    att.resolve(store, x1, "root+main1\n")
    advance_main(scratch, "main2\n")
    merge(store, x1, att)
    x2 = att.calls[1]["child"]
    stray = mkjob(store, scratch, tmp_path, {"z.txt": "z\n"}).job_id  # an attempt record whose branch diverged
    ep = rep.open_for(store, root)
    rep.record_attempt(store, ep.episode_id, stray, start_ref=None, conflicts=[], digest="x", now=5.0)
    att.resolve(store, x2, "root+main2\n")
    res = merge(store, x2, att)
    assert res["outcome"] == "merged"
    assert incorporate.is_incorporated(store, root) and incorporate.is_incorporated(store, x1)
    assert not incorporate.is_incorporated(store, stray)
    assert rep.get(store, ep.episode_id).state == "resolved"
