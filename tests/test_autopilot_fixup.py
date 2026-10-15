"""Autopilot fix-up: it is LAUNCHED, its instructions are sane, its merge releases the parent, the pane tells the truth."""

import re

import pytest

from steward import autopilot, cli, incorporate
from steward.models import JobState, Judgment
from steward.store import Store
from test_autopilot import Followups, git, go, mkjob, runner, scratch, whats  # noqa: F401
from test_dash_polish import _render, world  # noqa: F401


class FakeHandlers:
    """Stands in for `cli._build_control_handlers`: records `new` / `launch`, no herdr."""

    def __init__(self, store, launch_ok=True):
        self.store, self.launch_ok, self.launched, self.created = store, launch_ok, [], []

    def __call__(self, store):
        def new(cmd):
            child = store.create_job(cmd["args"]["goal"], cmd["args"]["repo"])
            store.kv_set(child.job_id, "parent_job", cmd["args"]["follow_up"])
            self.created.append(child.job_id)
            return {"accepted": True, "job_id": child.job_id}

        def launch(cmd):
            self.launched.append(cmd["args"])
            if not self.launch_ok:
                return {"accepted": False, "detail": "no Herdr workspace"}
            self.store.kv_set(cmd["args"]["job_id"], "herdr_tab", "w1:t9")
            return {"accepted": True, "tab_id": "w1:t9"}
        return {"new": new, "launch": launch}


def conflicted(store, scratch, tmp_path, goal="Add the thing to the repo"):
    job = mkjob(store, scratch, tmp_path, {"a.txt": "job\n"}, goal=goal)
    (scratch / "a.txt").write_text("main\n")
    git(scratch, "commit", "-qam", "main edit")
    return job


def test_followup_is_launched_not_just_created(steward_home, scratch, tmp_path, monkeypatch):
    store = Store(steward_home)
    fake = FakeHandlers(store)
    monkeypatch.setattr(cli, "_build_control_handlers", fake)
    job = conflicted(store, scratch, tmp_path)
    store.kv_set(job.job_id, "launch_args", {"workspace": "w1", "worker": "fabric", "kind": "claude"})
    res = go(store, job)  # the DEFAULT start_followup
    assert res["outcome"] == "followup"
    child = res["follow_up"]
    assert [a["job_id"] for a in fake.launched] == [child]  # fails if the fix-up is only created
    assert fake.launched[0]["workspace"] == "w1" and fake.launched[0]["worker"] == "fabric"
    assert store.kv_get(child, "herdr_tab") and "auto_followup_started" in whats(store, job)
    assert autopilot.followup_status(store, job.job_id) == {"kind": "running", "child": child, "n": 1, "max": 2}


@pytest.mark.legacy_ownership_off  # Plan 4: ownership routes launch failure to a reviewable blocker
def test_failed_launch_escalates_needs_you(steward_home, scratch, tmp_path, monkeypatch):
    store = Store(steward_home)
    fake, notes = FakeHandlers(store, launch_ok=False), []
    monkeypatch.setattr(cli, "_build_control_handlers", fake)
    job = conflicted(store, scratch, tmp_path)
    res = go(store, job, notify=lambda *a: notes.append(a))
    assert res["outcome"] == "escalated" and "could not launch" in res["detail"]
    assert {"auto_followup_launch_failed", "autopilot_escalated"} <= set(whats(store, job))
    assert "auto_followup_started" not in whats(store, job)
    assert notes and notes[0][1] == "NEEDS_YOU"
    st = autopilot.followup_status(store, job.job_id)
    assert st["kind"] == "not_started" and st["child"] == fake.created[0]


def test_followup_goal_says_merge_into_your_branch_with_hints_and_background_only(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    fu = Followups(store)
    job = conflicted(store, scratch, tmp_path, goal="(1) gsd-plan-phase 18 (2) gsd-execute-phase 18")
    go(store, job, start_followup=fu)
    goal = fu.calls[0][1]
    assert "into YOUR branch" in goal and "starts at job" in goal and f"never check out or commit to steward/{job.job_id}" in goal
    assert "- a.txt:" in goal and "keep both sides" in goal
    assert "Background only, do NOT redo it" in goal and goal.index("Background only") < goal.index("gsd-execute-phase 18")
    assert "do not push" in goal and "Never weaken" in goal
    pf = incorporate.preflight(store, job.job_id)
    text = autopilot._followup_goal(job, pf, "merge conflict", ["STEWARD-QUESTIONS.json", "steward/runner.py"])
    assert ".steward/questions/<job8>/" in text and "union" in text and "PORT" in text


def _child_branch(store, scratch, tmp_path, parent):
    """A fix-up whose branch starts at the parent's head with master merged in (resolved)."""
    child = store.create_job("merge", str(scratch))
    wt = tmp_path / "child-wt"
    git(scratch, "worktree", "add", "-q", "-b", f"steward/{child.job_id}", str(wt), f"steward/{parent.job_id}")
    git(wt, "merge", "-q", "-X", "theirs", "--no-edit", "main")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state='ACCEPTED' WHERE job_id=?", (child.job_id,))
    store.kv_set(child.job_id, "autopilot_followup", parent.job_id)
    store.kv_set(child.job_id, "parent_job", parent.job_id)
    return child


def test_merged_fixup_marks_parent_incorporated_and_releases_it(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    parent = conflicted(store, scratch, tmp_path)
    child = _child_branch(store, scratch, tmp_path, parent)
    res = go(store, child)
    assert res["outcome"] == "merged"
    assert store.kv_get(parent.job_id, "incorporated_commit") == res["merge_commit"]
    assert "incorporated_via_followup" in whats(store, parent)
    assert store.get_job(parent.job_id).state == JobState.RELEASED
    assert autopilot.followup_status(store, parent.job_id) is None


def test_manual_incorporate_of_fixup_also_marks_parent(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    parent = conflicted(store, scratch, tmp_path)
    child = _child_branch(store, scratch, tmp_path, parent)
    r = incorporate.incorporate(store, child.job_id)
    assert r["accepted"] and store.kv_get(parent.job_id, "incorporated_commit") == r["merge_commit"]


def test_manual_incorporate_noop_records_the_commit(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"n.txt": "x\n"})
    git(scratch, "merge", "-q", "--no-ff", "--no-edit", f"steward/{job.job_id}")  # merged by hand, outside Steward
    assert not incorporate.is_incorporated(store, job.job_id)
    r = incorporate.incorporate(store, job.job_id)
    assert r["noop"] and incorporate.is_incorporated(store, job.job_id) == git(scratch, "rev-parse", "HEAD")


def _fix_events(store, parent, child, *, attempt=1, escalated=False):
    store.append_event(parent.job_id, what="auto_followup_started", evidence={"follow_up": child.job_id, "attempt": attempt},
                       rule="autopilot_merge", judgment=Judgment.code)
    store.kv_set(child.job_id, "autopilot_followup", parent.job_id)
    if escalated:
        store.append_event(child.job_id, what="autopilot_escalated", evidence={"reason": "red"}, rule="autopilot_merge",
                           judgment=Judgment.code)


def test_status_needs_you_after_two_failures(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    parent = mkjob(store, scratch, tmp_path, {"a.txt": "x\n"})
    child = store.create_job("fix", str(scratch))
    _fix_events(store, parent, child, escalated=True)
    assert autopilot.followup_status(store, parent.job_id)["kind"] == "needs_you"


def _row(lines, job):
    return next(ln for ln in lines if job.job_id[:8] in ln)


def test_pane_running_fixup_hides_incorporate(world, steward_home, tmp_path):
    store, _, done = world
    parent, child = done[2], done[1]
    _fix_events(store, parent, child)
    lines, cmap = _render(steward_home, tmp_path, 120)
    row = _row(lines, parent)
    assert f"merge blocked · fix-up {child.job_id[:8]} running (1/2)" in row
    assert "[ incorporate ]" not in row and "not merged" not in row and "[ release ]" in row


def test_pane_unlaunched_fixup_offers_launch_on_the_fixup(world, steward_home, tmp_path):
    store, _, done = world
    parent = done[2]
    child = store.create_job("fix", str(parent.repo))  # CONTRACTED, no tab
    _fix_events(store, parent, child)
    lines, cmap = _render(steward_home, tmp_path, 120)
    row = _row(lines, parent)
    assert "not started" in row and "[ launch ]" in row and "[ incorporate ]" not in row
    assert any(re.match(rf"btn \d+ \d+ \d+ launch {child.job_id} -", c) for c in cmap)


def test_pane_escalated_reads_needs_you_and_keeps_incorporate(world, steward_home, tmp_path):
    store, _, done = world
    parent = done[2]
    child = store.create_job("fix", str(parent.repo))
    _fix_events(store, parent, child, escalated=True)
    row = _row(_render(steward_home, tmp_path, 120)[0], parent)
    assert "merge blocked · needs you" in row and "[ incorporate ]" in row


def test_preflight_panel_never_offers_merge_when_conflict_known(world, steward_home, tmp_path, scratch):
    store, _, done = world
    parent = done[2]
    (scratch / "f2.txt").write_text("main\n")
    git(scratch, "add", "-A")
    git(scratch, "commit", "-qm", "main adds f2")
    lines, cmap = _render(steward_home, tmp_path, 120, (f"inc:{parent.job_id}",))
    assert any("merge would conflict" in ln for ln in lines)
    assert not any("merge into" in ln for ln in lines) and not any(" merge " in c for c in cmap)
    assert any("[ cancel ]" in ln for ln in lines) and not any("open fix-up" in ln for ln in lines)
    child = store.create_job("fix", str(scratch))
    store.kv_set(child.job_id, "herdr_tab", "w1:t9")
    _fix_events(store, parent, child, escalated=True)
    lines, cmap = _render(steward_home, tmp_path, 120, (f"inc:{parent.job_id}",))
    assert any("[ open fix-up ]" in ln for ln in lines) and not any("merge into" in ln for ln in lines)
    assert any(re.match(rf"btn \d+ \d+ \d+ open_fixup {child.job_id} -", c) for c in cmap)


def _start(store, parent, child, attempt=1):
    store.append_event(parent.job_id, what="auto_followup_started", evidence={"follow_up": child.job_id, "attempt": attempt},
                       rule="autopilot_merge", judgment=Judgment.code)
    store.kv_set(child.job_id, "autopilot_followup", parent.job_id)


def _set_state(store, job, state):
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state.value, job.job_id))


def test_status_skips_stopped_duplicates_and_follows_the_live_chain(steward_home, scratch, tmp_path):
    """Replays job 88ad60fb: two stopped duplicates started AFTER the live fix-up, which has its own live child."""
    store = Store(steward_home)
    parent = mkjob(store, scratch, tmp_path, {"a.txt": "x\n"})
    live, live2, dup1, dup2 = (store.create_job("fix", str(scratch)) for _ in range(4))
    _start(store, parent, live)
    _start(store, parent, dup1)
    _start(store, parent, dup2)
    _start(store, live, live2, attempt=2)
    for j in (live, live2, dup1, dup2):
        store.kv_set(j.job_id, "herdr_tab", f"tab-{j.job_id[:4]}")
    _set_state(store, dup1, JobState.STOPPED)
    _set_state(store, dup2, JobState.STOPPED)
    _set_state(store, live, JobState.RUNNING)
    _set_state(store, live2, JobState.RUNNING)
    assert autopilot.followup_status(store, parent.job_id) == {"kind": "running", "child": live2.job_id, "n": 2, "max": 2}


def test_status_stopped_duplicates_do_not_use_the_attempt_budget(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    parent = mkjob(store, scratch, tmp_path, {"a.txt": "x\n"})
    dup, live = store.create_job("fix", str(scratch)), store.create_job("fix", str(scratch))
    _start(store, parent, live)
    _start(store, parent, dup, attempt=2)  # a stopped duplicate recorded as a later attempt
    store.kv_set(live.job_id, "herdr_tab", "t1")
    _set_state(store, dup, JobState.STOPPED)
    _set_state(store, live, JobState.RUNNING)
    assert autopilot.followup_status(store, parent.job_id)["n"] == 1


def test_status_released_or_never_launched_child_is_not_followed(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    parent = mkjob(store, scratch, tmp_path, {"a.txt": "x\n"})
    live, rel = store.create_job("fix", str(scratch)), store.create_job("fix", str(scratch))
    _start(store, parent, live)
    _start(store, parent, rel)
    store.kv_set(live.job_id, "herdr_tab", "t1")
    _set_state(store, live, JobState.WORKER_FINISHED)
    _set_state(store, rel, JobState.RELEASED)
    st = autopilot.followup_status(store, parent.job_id)
    assert st["kind"] == "running" and st["child"] == live.job_id


def test_status_prefers_launched_live_child_over_newer_unlaunched_one(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    parent = mkjob(store, scratch, tmp_path, {"a.txt": "x\n"})
    run, fresh = store.create_job("fix", str(scratch)), store.create_job("fix", str(scratch))
    _start(store, parent, run)
    _start(store, parent, fresh)
    store.kv_set(run.job_id, "herdr_tab", "t1")
    _set_state(store, run, JobState.RUNNING)
    assert autopilot.followup_status(store, parent.job_id)["child"] == run.job_id


def test_status_all_children_stopped_needs_you_with_nothing_to_open(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    parent = mkjob(store, scratch, tmp_path, {"a.txt": "x\n"})
    dup = store.create_job("fix", str(scratch))
    _start(store, parent, dup)
    _set_state(store, dup, JobState.STOPPED)
    assert autopilot.followup_status(store, parent.job_id) == {"kind": "needs_you", "child": "", "n": 0, "max": 2}


def test_open_fixup_falls_back_to_explain_when_tab_is_gone(steward_home, monkeypatch, capsys):
    import subprocess

    from steward import dashact

    store = Store(steward_home)
    job = store.create_job("fix", "/tmp")
    store.kv_set(job.job_id, "herdr_tab", "w4Q:t2B")
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1 if argv[:2] == ["herdr", "tab"] else 0, "", "tab_not_found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(dashact, "Store", Store, raising=False)
    assert dashact.main(["focus-job", job.job_id]) == 0
    assert calls[0][:3] == ["herdr", "tab", "focus"] and "explain-state" in calls[1] and calls[1][-1] == job.job_id
