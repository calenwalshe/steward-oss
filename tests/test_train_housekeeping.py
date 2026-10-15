from steward import train_housekeeping as hk
from steward.models import JobState
from steward.store import Store
from test_incorporate import git, mkjob, scratch  # noqa: F401
MERGE_GOAL = "Merge `fleet-registry` into YOUR branch: you are on your own"


def test_stops_fixup_whose_parent_landed(scratch, tmp_path):
    store = Store()
    parent = mkjob(store, scratch, tmp_path, {"x.txt": "x"}, state=JobState.RELEASED)
    child = mkjob(store, scratch, tmp_path, {"y.txt": "y"}, state=JobState.RUNNING, goal=MERGE_GOAL)
    other = mkjob(store, scratch, tmp_path, {"z.txt": "z"}, state=JobState.RUNNING)
    store.kv_set(child.job_id, "autopilot_followup", parent.job_id)
    store.kv_set(parent.job_id, "incorporated_commit", "abc")
    stopped = []
    assert hk.stop_superseded_fixups(store, stopped.append) == [child.job_id]
    assert stopped == [child.job_id] and other.job_id not in stopped


def test_archives_branch_without_job_row(scratch, tmp_path):
    store = Store()
    keep = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    git(scratch, "branch", "steward/00000000-dead-beef-0000-000000000000", "main")
    moved = hk.archive_orphan_branches(store, scratch)
    assert moved == ["steward/00000000-dead-beef-0000-000000000000"]
    refs = git(scratch, "for-each-ref", "--format=%(refname:short)").split()
    assert "archive/steward/00000000-dead-beef-0000-000000000000" in refs
    assert f"steward/{keep.job_id}" in refs


def _linked(store, scratch, tmp_path, goal, state):
    parent = mkjob(store, scratch, tmp_path, {"x.txt": "x"}, state=JobState.RELEASED)
    child = mkjob(store, scratch, tmp_path, {"y.txt": "y"}, state=state, goal=goal)
    store.kv_set(child.job_id, "autopilot_followup", parent.job_id)
    store.kv_set(parent.job_id, "incorporated_commit", "abc")
    return child


def test_non_merge_followup_not_stopped(scratch, tmp_path):
    store = Store()
    _linked(store, scratch, tmp_path, "Fix the flaky test", JobState.RUNNING)
    stopped = []
    assert hk.stop_superseded_fixups(store, stopped.append) == [] and stopped == []


def test_needs_human_merge_fixup_not_stopped(scratch, tmp_path):
    store = Store()
    _linked(store, scratch, tmp_path, MERGE_GOAL, JobState.NEEDS_HUMAN)
    stopped = []
    assert hk.stop_superseded_fixups(store, stopped.append, include_needs_human=False) == [] and stopped == []


def test_non_uuid_steward_ref_not_archived(scratch, tmp_path):
    store = Store()
    git(scratch, "branch", "steward/not-a-uuid", "main")
    assert hk.archive_orphan_branches(store, scratch) == []
    assert "steward/not-a-uuid" in git(scratch, "for-each-ref", "--format=%(refname:short)").split()
