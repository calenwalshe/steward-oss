import pytest

from steward import incorporate as inc, machine
from steward.models import JobState, Judgment
from steward.store import Store
from test_incorporate import git, mkjob, scratch  # noqa: F401


def test_release_evidence_ok_when_merge_reachable(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"a.txt": "a"})
    r = inc.incorporate(store, job.job_id)
    assert r["accepted"]
    ev = inc.release_evidence(store, store.get_job(job.job_id))
    assert ev["ok"] and ev["merge_commit"] == r["merge_commit"] and not ev["repo_gone"]
    assert inc.release_on_incorporation(store, job.job_id) is True
    assert store.get_job(job.job_id).state == JobState.RELEASED
    e = [x for x in store.events(job.job_id) if x.what == "released on incorporation"][0]
    assert e.judgment == Judgment.code and e.rule == "released_on_incorporation"
    assert inc.release_on_incorporation(store, job.job_id) is False  # idempotent


def test_release_evidence_refuses_unreachable_commit(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"a.txt": "a"})
    inc.incorporate(store, job.job_id)
    git(scratch, "reset", "-q", "--hard", "HEAD~1")        # the merge is gone from the target
    ev = inc.release_evidence(store, store.get_job(job.job_id))
    assert not ev["ok"] and "not reachable" in ev["reason"]
    with pytest.raises(machine.AuthorityError):
        store.set_state(job.job_id, JobState.RELEASED, what="x", evidence={}, rule="released_on_incorporation",
                        judgment=Judgment.code)


def test_release_evidence_repo_gone_uses_receipt(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"a.txt": "a"})
    inc.incorporate(store, job.job_id)
    with store._conn:
        store._conn.execute("UPDATE jobs SET repo=? WHERE job_id=?", (str(tmp_path / "gone"), job.job_id))
    ev = inc.release_evidence(store, store.get_job(job.job_id))
    assert ev["ok"] and ev["repo_gone"]


def test_no_receipt_no_release(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"a.txt": "a"})
    store.kv_set(job.job_id, "incorporated_commit", "deadbeef")
    assert not inc.release_evidence(store, store.get_job(job.job_id))["ok"]


def test_commit_only_on_scratch_branch_is_not_released(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"a.txt": "a"})
    git(scratch, "checkout", "-q", "-b", "scratch/x")
    (scratch / "s.txt").write_text("s")
    git(scratch, "add", "s.txt")
    git(scratch, "commit", "-qm", "scratch")
    sha = git(scratch, "rev-parse", "HEAD")
    git(scratch, "checkout", "-q", "-")
    store.kv_set(job.job_id, "incorporated_commit", sha)
    store.append_event(job.job_id, what="incorporated_via_followup", evidence={"merge_commit": sha},
                       rule="autopilot_merge", judgment=Judgment.code)
    ev = inc.release_evidence(store, store.get_job(job.job_id))
    assert not ev["ok"] and "not reachable" in ev["reason"]
    assert inc.release_on_incorporation(store, job.job_id) is False


def test_branchless_receipt_on_target_head_releases(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"a.txt": "a"})
    sha = git(scratch, "rev-parse", "HEAD")
    store.kv_set(job.job_id, "incorporated_commit", sha)
    store.append_event(job.job_id, what="incorporated_via_followup", evidence={"merge_commit": sha},
                       rule="autopilot_merge", judgment=Judgment.code)
    assert inc.release_on_incorporation(store, job.job_id) is True


def test_train_order_receipt_then_release(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"a.txt": "a"})
    sha = git(scratch, "rev-parse", "HEAD")
    store.kv_set(job.job_id, "incorporated_commit", sha)
    assert inc.release_on_incorporation(store, job.job_id) is False       # no receipt yet
    store.append_event(job.job_id, what="landed_by_train", evidence={"merge_commit": sha, "target": "main"},
                       rule="landing_train", judgment=Judgment.code)
    assert inc.release_on_incorporation(store, job.job_id) is True
