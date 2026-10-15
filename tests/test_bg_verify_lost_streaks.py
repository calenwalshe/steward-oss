"""Zero-touch §5.5 "after 2 re-queues the ladder's ask applies": the allowance is per loss streak. A streak that
ended in the ask (NEEDS_HUMAN `bg_verify_lost`) must not leave its count behind, or the next streak after a resume
parks on its first loss."""
from steward import bg_verify, heartbeat
from steward.models import Judgment, JobState
from steward.store import Store

from test_bg_verify import _bg_job


def _vanish(store, job_id):
    rec = bg_verify.record(store, job_id)
    store.kv_set(job_id, bg_verify.KEY, {**rec, "started_at": rec["started_at"] - bg_verify.STALE_S - 5})


def _streak(store, runner, job_id, spawned):
    """Two losses are re-queued, the third parks with the ask."""
    base = len(spawned)
    for n in (1, 2):
        _vanish(store, job_id)
        r = runner.tick(job_id)
        assert r.state == "WORKER_FINISHED" and len(spawned) == base + n, (n, r)
    _vanish(store, job_id)
    r = runner.tick(job_id)
    assert r.state == "NEEDS_HUMAN" and len(spawned) == base + 2
    assert store.get_job(job_id).state == JobState.NEEDS_HUMAN


def test_each_loss_streak_across_park_and_resume_gets_two_requeues(steward_home):
    store = Store(steward_home)
    job, runner, sr, spawned = _bg_job(store)
    runner.tick(job.job_id)
    assert len(spawned) == 1
    _streak(store, runner, job.job_id, spawned)

    # resume: the human sends it back to work, the worker finishes again, a fresh background verify starts
    store.set_state(job.job_id, JobState.RUNNING, what="resumed", evidence={}, rule="test", judgment=Judgment.human)
    store.set_state(job.job_id, JobState.WORKER_FINISHED, what="worker finished", evidence={}, rule="test", judgment=Judgment.human)
    before = len(spawned)
    r = runner._run_closeout(job.job_id, heartbeat.Class.idle)  # the WORKER_FINISHED closeout a resumed worker reaches
    assert r.state == "WORKER_FINISHED" and len(spawned) == before + 1, r

    _streak(store, runner, job.job_id, spawned)
    requeued = [e.evidence["n"] for e in store.events(job.job_id) if e.what == "verify_requeued"]
    assert requeued == [1, 2, 1, 2]
