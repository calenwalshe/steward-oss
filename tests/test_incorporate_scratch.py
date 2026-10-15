"""Incorporation preflight refuses job branches that add worker scratch/evidence files."""

from test_incorporate import git, mkjob, scratch, snap  # noqa: F401  (scratch is a fixture)

from steward import incorporate
from steward.models import JobState
from steward.store import Store


def test_preflight_blocks_scratch_dirs(scratch, tmp_path):
    store = Store()
    job = store.create_job("Add scratch", str(scratch))
    wt = tmp_path / "wt-scratch"
    git(scratch, "worktree", "add", "-q", "-b", f"steward/{job.job_id}", str(wt), "HEAD")
    for rel in (".tmp-x/log.txt", ".evidence/run.log", "pkg/.tmp-y/z.txt"):
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text("scratch\n")
    (wt / "src.py").write_text("x = 1\n")
    git(wt, "add", "-Af")
    git(wt, "commit", "-qm", "work plus scratch")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.ACCEPTED.value, job.job_id))
    pf = incorporate.preflight(store, job.job_id)
    assert pf.block and ".tmp-x/log.txt" in pf.block and ".evidence/run.log" in pf.block and "pkg/.tmp-y/z.txt" in pf.block
    assert "src.py" not in pf.block
    before = snap(scratch)
    r = incorporate.incorporate(store, job.job_id)
    assert not r["accepted"] and r["error_kind"] == "incorporate_blocked" and ".tmp-x/log.txt" in r["files"]
    assert snap(scratch) == before


def test_preflight_allows_normal_change(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"src.py": "x = 1\n"})
    pf = incorporate.preflight(store, job.job_id)
    assert not pf.block and not pf.scratch
