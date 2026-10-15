"""Autopilot merge: temp git repos, fake runner, no live store, no herdr."""

import threading
from pathlib import Path

import pytest

from steward import autopilot, session
from steward.models import JobState, Judgment
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store
from test_incorporate import git, mkjob, scratch  # noqa: F401  (scratch is a fixture)


def runner(red=False, log=None):
    def respond(argv, cwd):
        if log is not None:
            log.append(argv)
        code = 1 if (red and argv[:2] == ["bash", "-lc"]) else 0
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=code, stdout="", stderr="", duration_s=0.0)
    return FakeRunner(respond)


def whats(store, job):
    return [e.what for e in store.events(job.job_id)]


class Followups:
    def __init__(self, store):
        self.store, self.calls = store, []

    def __call__(self, store, parent_id, goal):
        child = store.create_job(goal, store.get_job(parent_id).repo)
        store.kv_set(child.job_id, "parent_job", parent_id)
        self.calls.append((parent_id, goal, child.job_id))
        return {"accepted": True, "job_id": child.job_id}


def go(store, job, r=None, **kw):
    return autopilot.try_merge(store, job.job_id, r or runner(), checks=["pytest -q"], **kw)


def test_clean_merge(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"new.txt": "x\n"})
    res = go(store, job)
    assert res["outcome"] == "merged"
    assert (scratch / "new.txt").exists()
    assert git(scratch, "rev-parse", "--short=7", "HEAD") == res["merge_commit"][:7]
    assert "auto_merged" in whats(store, job) and "incorporated" in whats(store, job)
    assert autopilot.label(store, job.job_id) == f"auto-merged {res['merge_commit'][:7]}"
    assert f"[auto-merged {res['merge_commit'][:7]}]" in session.render_rows(session.list_rows(store, all_jobs=True, me={}))
    assert git(scratch, "worktree", "list").count("steward-automerge") == 0  # temp worktree cleaned up


def test_chain_target_mismatch_escalates_and_leaves_repo_untouched(steward_home, scratch, tmp_path):
    from steward import blockers, delivery

    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"new.txt": "x\n"})
    store.kv_set(job.job_id, "chain_target_branch", "release")
    head = git(scratch, "rev-parse", "HEAD")

    result = go(store, job)

    assert result["outcome"] == "human"
    assert "chain target mismatch" in result["detail"]
    assert git(scratch, "rev-parse", "HEAD") == head
    assert "incorporated" not in whats(store, job)
    waiting = [b for b in blockers.open_blockers(store, job.job_id) if b.kind == delivery.WAITING_KIND]
    assert len(waiting) == 1
    assert "pinned to release" in waiting[0].claim


@pytest.mark.legacy_ownership_off  # Plan 4: this pins the superseded follow-up-of-a-follow-up chain
def test_conflict_starts_followup_then_escalates(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    fu, notes = Followups(store), []
    job = mkjob(store, scratch, tmp_path, {"a.txt": "job\n"})
    (scratch / "a.txt").write_text("main\n")
    git(scratch, "commit", "-qam", "main edit")
    head = git(scratch, "rev-parse", "HEAD")
    res = go(store, job, start_followup=fu, notify=lambda *a: notes.append(a))
    assert res["outcome"] == "followup" and git(scratch, "rev-parse", "HEAD") == head
    assert "auto_followup_started" in whats(store, job) and "incorporated" not in whats(store, job)
    assert "merge `main` into your branch" in fu.calls[0][1].lower() and "a.txt" in fu.calls[0][1]
    # the follow-up fails too -> second follow-up -> then NEEDS_YOU
    c1 = store.get_job(fu.calls[0][2])
    assert autopilot.followup_depth(store, c1.job_id) == 1
    store.kv_set(c1.job_id, "parent_job", job.job_id)
    with store._conn:
        store._conn.execute("UPDATE jobs SET state='ACCEPTED' WHERE job_id=?", (c1.job_id,))
    git(scratch, "worktree", "add", "-q", "-b", f"steward/{c1.job_id}", str(tmp_path / "c1"), "HEAD~0")
    git(tmp_path / "c1", "config", "user.name", "x"); git(tmp_path / "c1", "config", "user.email", "x@x")
    (tmp_path / "c1" / "a.txt").write_text("c1\n"); git(tmp_path / "c1", "commit", "-qam", "c1")
    (scratch / "a.txt").write_text("main2\n"); git(scratch, "commit", "-qam", "main again")
    assert go(store, c1, start_followup=fu)["outcome"] == "followup"
    c2 = store.get_job(fu.calls[1][2])
    assert autopilot.followup_depth(store, c2.job_id) == 2
    git(scratch, "worktree", "add", "-q", "-b", f"steward/{c2.job_id}", str(tmp_path / "c2"), "HEAD~0")
    git(tmp_path / "c2", "config", "user.name", "x"); git(tmp_path / "c2", "config", "user.email", "x@x")
    (tmp_path / "c2" / "a.txt").write_text("c2\n"); git(tmp_path / "c2", "commit", "-qam", "c2")
    (scratch / "a.txt").write_text("main3\n"); git(scratch, "commit", "-qam", "main 3")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state='ACCEPTED' WHERE job_id=?", (c2.job_id,))
    assert go(store, c2, start_followup=fu, notify=lambda *a: notes.append(a))["outcome"] == "escalated"
    assert len(fu.calls) == 2 and notes and notes[0][1] == "NEEDS_YOU"
    assert "autopilot_escalated" in whats(store, c2)


def test_red_merged_tree_does_not_merge(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    fu = Followups(store)
    job = mkjob(store, scratch, tmp_path, {"new.txt": "x\n"})
    head = git(scratch, "rev-parse", "HEAD")
    res = go(store, job, runner(red=True), start_followup=fu)
    assert res["outcome"] == "followup" and "red" in res["detail"]
    assert git(scratch, "rev-parse", "HEAD") == head and not (scratch / "new.txt").exists()
    assert len(fu.calls) == 1 and "auto_merged" not in whats(store, job)


@pytest.mark.parametrize("goal", ["Deploy the service to prod", "git push the branch", "email Bob the report",
                                  "rotate the credentials"])
def test_floor_goal_goes_to_human(steward_home, scratch, tmp_path, goal):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"new.txt": "x\n"}, goal=goal)
    head = git(scratch, "rev-parse", "HEAD")
    assert go(store, job)["outcome"] == "human"
    assert git(scratch, "rev-parse", "HEAD") == head and "autopilot_skipped" in whats(store, job)


def test_floor_action_by_job_goes_to_human(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"new.txt": "x\n"})
    store.append_event(job.job_id, what="risk_classified", evidence={"tier": "floor"}, rule="r", judgment=Judgment.code)
    assert go(store, job)["outcome"] == "human"


def test_off_when_configured(steward_home, scratch, tmp_path):
    import os
    Path(os.environ["STEWARD_CONFIG_DIR"], "steward.toml").write_text('[autopilot]\nmerge = false\n')
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"new.txt": "x\n"})
    assert go(store, job)["outcome"] == "skipped" and not (scratch / "new.txt").exists()


def test_default_follows_acceptance_mode(steward_home, monkeypatch):
    assert autopilot.enabled() is True
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "human")
    assert autopilot.enabled() is False


def test_merges_are_serialized_per_repo(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    jobs = [mkjob(store, scratch, tmp_path, {f"f{i}.txt": "x\n"}) for i in range(3)]
    state = {"now": 0, "max": 0}
    guard = threading.Lock()

    def respond(argv, cwd):
        with guard:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        threading.Event().wait(0.15)
        with guard:
            state["now"] -= 1
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    r = FakeRunner(respond)
    out = []
    ts = [threading.Thread(target=lambda j=j: out.append(go(Store(steward_home), j, r))) for j in jobs]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert state["max"] == 1 and sorted(o["outcome"] for o in out) == ["merged"] * 3
    assert all((scratch / f"f{i}.txt").exists() for i in range(3))


def test_steward_repo_emits_restart_needed_only(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    (scratch / "steward").mkdir()
    (scratch / "steward" / "x.py").write_text("x=1\n")
    git(scratch, "add", "-A"); git(scratch, "commit", "-qm", "pkg")
    job = mkjob(store, scratch, tmp_path, {"steward/y.py": "y=1\n"})
    r = runner(log=[])
    res = go(store, job, r, steward_root=scratch)
    assert res["outcome"] == "merged" and "restart_needed" in whats(store, job)
    assert not any("systemctl" in " ".join(c["argv"]) or "restart" in " ".join(c["argv"]) for c in r.calls)
    other = mkjob(store, scratch, tmp_path, {"z.txt": "z\n"})
    go(store, other, steward_root=tmp_path / "elsewhere")
    assert "restart_needed" not in whats(store, other)


def test_dirty_source_repo_left_for_human(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"a.txt": "job\n"})
    (scratch / "a.txt").write_text("dirty\n")
    assert go(store, job, start_followup=Followups(store))["outcome"] == "human"
    assert (scratch / "a.txt").read_text() == "dirty\n"


def test_floor_ask_ignores_negated_and_path_mentions():
    # Regression: every brief says "never git push", so autopilot skipped every job as "asks for git push".
    from steward.autopilot import floor_ask
    for goal in ("Commit per GSD conventions; never git push.",
                 "no deploy/publish/push/release/messages/payments/credentials/writes outside worktree without Sam",
                 "master merges deploy automatically through deploy/cd.sh (never run it yourself, never git push)",
                 "Do not touch build.gradle.kts signing or deploy files",
                 "nothing of Sam's personal config, secrets, tokens or hostnames may enter the package"):
        assert floor_ask(goal) is None, goal
    for goal in ("Deploy to production and verify", "publish the package to npm", "then git push the branch to origin",
                 "Send a message to the team on Slack"):
        assert floor_ask(goal), goal


def test_whitespace_only_conflict_is_synced_in_code_without_a_model_fixup(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    fu = Followups(store)
    (scratch / "a.txt").write_text("hello world\n")
    git(scratch, "add", "-A"); git(scratch, "commit", "-qm", "base a")
    job = mkjob(store, scratch, tmp_path, {"a.txt": "hello big world\n"})
    (scratch / "a.txt").write_text("hello   world\n")
    git(scratch, "commit", "-qam", "main reflows a")
    assert autopilot.inc.preflight(store, job.job_id).conflicts == ["a.txt"]

    res = go(store, job, start_followup=fu)

    assert res["outcome"] == "merged" and fu.calls == []
    assert "autopilot_synced" in whats(store, job)
    assert (scratch / "a.txt").read_text().strip() in ("hello big world", "hello   big world")


def test_real_conflict_still_goes_to_a_followup_and_leaves_the_branch_alone(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    fu = Followups(store)
    job = mkjob(store, scratch, tmp_path, {"a.txt": "job\n"})
    (scratch / "a.txt").write_text("main\n")
    git(scratch, "add", "-A"); git(scratch, "commit", "-qm", "main a")
    branch_head = git(scratch, "rev-parse", f"steward/{job.job_id}")

    res = go(store, job, start_followup=fu)

    assert res["outcome"] == "followup" and len(fu.calls) == 1
    assert "autopilot_synced" not in whats(store, job)
    assert git(scratch, "rev-parse", f"steward/{job.job_id}") == branch_head
    assert git(scratch, "worktree", "list").count("MERGE") == 0


def test_deploy_is_blocked_when_the_merge_result_was_not_the_tested_tree_and_is_red(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_AUTODEPLOY", "1")
    store = Store(steward_home)
    (scratch / "steward").mkdir()
    (scratch / "steward" / "x.py").write_text("x=1\n")
    git(scratch, "add", "-A"); git(scratch, "commit", "-qm", "pkg")
    job = mkjob(store, scratch, tmp_path, {"steward/y.py": "y=1\n"})
    # the pre-merge test passes on some other tree (the source moved after it ran) ...
    monkeypatch.setattr(autopilot, "_test_merged_tree", lambda *a, **k: (True, "not-the-merged-tree", []))
    deployed = []
    # ... but the checks fail on the tree that actually got merged
    res = go(store, job, runner(red=True), steward_root=scratch,
             deploy_self=lambda s, root: deployed.append(root) or {"ok": True, "summary": "x"})
    assert res["outcome"] == "merged" and deployed == []
    assert "auto_deploy_blocked" in whats(store, job) and "restart_needed" in whats(store, job)


def test_deploy_runs_without_a_second_test_when_the_tested_tree_is_what_merged(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_AUTODEPLOY", "1")
    store = Store(steward_home)
    (scratch / "steward").mkdir()
    (scratch / "steward" / "x.py").write_text("x=1\n")
    git(scratch, "add", "-A"); git(scratch, "commit", "-qm", "pkg")
    job = mkjob(store, scratch, tmp_path, {"steward/y.py": "y=1\n"})
    log, deployed = [], []
    res = go(store, job, runner(log=log), steward_root=scratch,
             deploy_self=lambda s, root: deployed.append(root) or {"ok": True, "summary": "x"})
    assert res["outcome"] == "merged" and len(deployed) == 1
    assert sum(1 for c in log if c[:2] == ["bash", "-lc"]) == 1  # one test run: before the merge only


def test_d3477a2a_replay_denied_heredoc_floor_match_still_auto_merges(steward_home, scratch, tmp_path):
    """d3477a2a: a heredoc containing `git push` matched the floor rule, the resolver DENIED it (nothing ran),
    and the job was still forced to human acceptance. A denied floor classification must not block the merge."""
    from steward import approvals

    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"new.txt": "x\n"})
    a = approvals.open_approval(store, job.job_id, "Bash: cat <<EOF\ngit push\nEOF", kind="permission")
    store.append_event(job.job_id, what="risk_classified", evidence={"approval_id": a["id"], "tier": "floor"},
                       rule="resolver_classified", judgment=Judgment.code)
    approvals.decide(store, job.job_id, a["id"], "deny", decided_by="resolver:test")
    approvals.mark_relayed(store, job.job_id, a["id"])

    res = go(store, job)

    assert res["outcome"] == "merged" and "autopilot_skipped" not in whats(store, job)


def test_no_merge_instruction_is_inherited_through_followup_chain(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    parent = mkjob(store, scratch, tmp_path, {"new.txt": "x\n"}, goal="Do not merge or incorporate this task")
    autopilot.inherit_no_merge_policy(store, parent.job_id)
    child = store.create_job("fix the conflict", str(scratch))
    from steward import followup
    followup.link(store, parent.job_id, child.job_id)
    grandchild = store.create_job("keep fixing", str(scratch))
    followup.link(store, child.job_id, grandchild.job_id)
    assert store.kv_get(grandchild.job_id, autopilot.NO_MERGE_POLICY_KEY)["source_job"] == parent.job_id
    for job in (child, grandchild):
        with store._conn:
            store._conn.execute("UPDATE jobs SET state='ACCEPTED' WHERE job_id=?", (job.job_id,))
        assert autopilot.floor_reason(store, job.job_id) is not None
        assert autopilot.try_merge(store, job.job_id, runner()) == {
            "outcome": "human", "detail": autopilot.floor_reason(store, job.job_id)}
    assert autopilot._default_followup(store, parent.job_id, "resolve") == {
        "accepted": False, "detail": "merge is protected by job goal (ancestor " + parent.job_id[:8] + ")"}
    assert len(store.list_jobs()) == 3


def test_protected_parent_is_not_released_through_a_manually_incorporated_descendant(steward_home):
    store = Store(steward_home)
    parent = store.create_job("Do not incorporate this task", "/tmp")
    child = store.create_job("follow-up", "/tmp")
    store.kv_set(child.job_id, "autopilot_followup", parent.job_id)
    with store._conn:
        store._conn.execute("UPDATE jobs SET state='ACCEPTED' WHERE job_id=?", (parent.job_id,))
    assert __import__("steward.incorporate", fromlist=["mark_parents_incorporated"]).mark_parents_incorporated(
        store, child.job_id, "deadbeef") == []
    assert store.kv_get(parent.job_id, "incorporated_commit") is None
    assert store.get_job(parent.job_id).state == JobState.ACCEPTED


def test_exact_owner_incorporation_remains_available_for_a_protected_job(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"new.txt": "x\n"}, goal="Do not merge this task")
    autopilot.inherit_no_merge_policy(store, job.job_id)

    receipt = autopilot.inc.incorporate(store, job.job_id)

    assert receipt["accepted"] and receipt["merge_commit"]
    ev = next(e for e in store.events(job.job_id) if e.what == "incorporated")
    assert ev.rule == "operator_incorporate" and ev.judgment == Judgment.human
