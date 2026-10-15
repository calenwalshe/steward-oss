"""Issue 15: check runs of finished jobs must not hold the heavy line."""
from __future__ import annotations

import subprocess
import sys
import time

from steward import check_runs as cr
from steward.models import JobState, Judgment
from steward.store import Store


def spec(tree, command="python3 -m pytest -q"):
    return cr.RunSpec(tree_sha=tree, command=command, env_digest="e", toolchain="tc", heavy=True, predicted_s=600.0)


def job(s, goal="g"):
    j = s.create_job(goal, "/repo").job_id
    s.set_state(j, JobState.CONTRACTED, what="c", evidence={}, rule="t", judgment=Judgment.human)
    s.set_state(j, JobState.RUNNING, what="r", evidence={}, rule="t", judgment=Judgment.code)
    return j


def stop(s, j):
    s.set_state(j, JobState.STOPPING, what="x", evidence={}, rule="t", judgment=Judgment.human)
    s.set_state(j, JobState.STOPPED, what="x", evidence={}, rule="t", judgment=Judgment.human)


def sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)


def test_stopping_a_job_cancels_running_and_queued_runs_kills_process_and_frees_the_line(steward_home, monkeypatch):
    monkeypatch.setenv(cr.HEAVY_SLOTS_ENV, "1")
    monkeypatch.setattr(cr, "KILL_GRACE_S", 1.0)
    s = Store(steward_home)
    dead, live = job(s), job(s)
    proc = sleeper()
    try:
        running = cr.request(s, spec("A"), job_id=dead, purpose="baseline", now=1.0)[0]
        assert cr.try_start(s, running.run_id, owner="o", pid=proc.pid, now=2.0)
        queued = cr.request(s, spec("B"), job_id=dead, purpose="verify", now=3.0)[0]
        nxt = cr.request(s, spec("C"), job_id=live, purpose="verify", now=4.0)[0]
        assert not cr.try_start(s, nxt.run_id, owner="p", pid=None, now=5.0)  # the dead job's run holds the slot
        stop(s, dead)
        assert cr.get(s, running.run_id).state == "cancelled" and cr.get(s, queued.run_id).state == "cancelled"
        proc.wait(timeout=10)  # SIGTERM (or SIGKILL after the grace) took the process group down
        assert proc.returncode is not None
        assert any(e.what == "check_run_cancelled" and e.evidence["for_job"] == dead and e.evidence["killed"]
                   for e in s.events(dead))
        assert cr.try_start(s, nxt.run_id, owner="p", pid=None, now=6.0)  # the live job's run now starts
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_a_run_shared_with_a_live_job_survives_the_other_jobs_stop(steward_home):
    s = Store(steward_home)
    a, b = job(s), job(s)
    shared = cr.request(s, spec("T"), job_id=a, purpose="verify", now=1.0)[0]
    cr.request(s, spec("T"), job_id=b, purpose="verify", now=2.0)
    assert cr.try_start(s, shared.run_id, owner="o", pid=None, now=3.0)
    kills = []
    stop(s, a)
    assert cr.get(s, shared.run_id).state == "running"
    out = cr.cancel_job_runs(s, a, now=4.0, kill_fn=kills.append)
    assert out["cancelled"] == [] and kills == []
    assert cr.get(s, shared.run_id).state == "running"
    stop(s, b)  # the last waiter finishing releases it
    assert cr.get(s, shared.run_id).state == "cancelled"


def test_sweep_cancels_runs_of_an_already_accepted_job(steward_home):
    s = Store(steward_home)
    j = job(s)
    q = cr.request(s, spec("Q"), job_id=j, purpose="verify", now=1.0)[0]
    r = cr.request(s, spec("R"), job_id=j, purpose="baseline", now=2.0)[0]
    assert cr.try_start(s, r.run_id, owner="o", pid=None, now=3.0)
    # jobs that ended before the fix: state flipped with no cancel hook
    s._conn.execute("UPDATE jobs SET state='ACCEPTED' WHERE job_id=?", (j,)); s._conn.commit()
    killed = []
    out = cr.sweep_finished(s, now=4.0, kill_fn=lambda pid: killed.append(pid) or True)
    assert sorted(out["cancelled"]) == sorted([q.run_id, r.run_id])
    assert cr.get(s, q.run_id).state == "cancelled" and cr.get(s, r.run_id).state == "cancelled"
    assert cr.sweep_finished(s, now=5.0)["cancelled"] == []


def test_accepted_job_keeps_its_delivery_checks_until_incorporated(steward_home):
    s = Store(steward_home)
    j = job(s)
    m = cr.request(s, spec("M"), job_id=j, purpose="merged_tree", now=1.0)[0]
    s._conn.execute("UPDATE jobs SET state='ACCEPTED' WHERE job_id=?", (j,)); s._conn.commit()
    assert cr.sweep_finished(s, now=2.0)["cancelled"] == []
    s.kv_set(j, "incorporated_commit", "abc")
    assert cr.sweep_finished(s, now=3.0)["cancelled"] == [m.run_id]


def test_kill_never_targets_this_process(steward_home):
    import os
    assert cr._kill_run(os.getpid()) is False and cr._kill_run(None) is False


def test_accepted_job_keeps_its_post_merge_run_after_incorporation(steward_home):
    # 2026-10-07: 32 post_merge_verify steps failed "lost: all waiting jobs finished" — delivery verifies the merge
    # AFTER incorporated_commit is set, so the sweep must not treat post_merge as finished until the job is RELEASED.
    s = Store(steward_home)
    j = job(s)
    s._conn.execute("UPDATE jobs SET state='ACCEPTED' WHERE job_id=?", (j,)); s._conn.commit()
    s.kv_set(j, "incorporated_commit", "abc")
    p = cr.request(s, spec("P"), job_id=j, purpose="post_merge", now=1.0)[0]
    assert cr.sweep_finished(s, now=2.0)["cancelled"] == []
    s._conn.execute("UPDATE jobs SET state='RELEASED' WHERE job_id=?", (j,)); s._conn.commit()
    assert cr.sweep_finished(s, now=3.0)["cancelled"] == [p.run_id]
