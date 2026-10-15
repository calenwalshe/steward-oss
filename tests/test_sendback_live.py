"""Mechanics item 12: a WORKER_FINISHED job verifying in the background can be sent back; the runner pre-empts the
verification, delivers the instructions to the live worker, withdraws its queued check run, and returns to RUNNING.
The runner never blocks a closeout tick on a still-running background baseline (the 29-minute steer delay)."""
from __future__ import annotations

import os
import time

from steward import bg_baseline, bg_verify, check_runs as cr, cli
from steward.models import JobState, Judgment
from steward.steering import Steering
from steward.store import Store
from test_bg_verify import _bg_job, HeadRunner
from test_stability import _done_obs, _finish_job, _runner


def _send(jid, text, *extra):
    return cli.main(["send-back", jid, text, "--origin", "cli", *extra])


def test_a_running_background_baseline_sends_closeout_to_the_background(steward_home):
    store = Store(steward_home)
    job = _finish_job(store)
    store.kv_set(job.job_id, bg_baseline.KEY, {"status": "running", "sha": "base", "started_at": time.time(),
                                               "pid": os.getpid()})  # alive, no measured duration yet
    assert bg_verify.predicted_s(store, job.job_id) is None
    runner = _runner(store, HeadRunner(), [[_done_obs()] * 4])
    spawned = []
    runner.bg_verify_spawn = lambda argv: spawned.append(argv)
    runner.start(job.job_id)
    t0 = time.monotonic()
    report = runner.tick(job.job_id)
    assert time.monotonic() - t0 < 5  # never parked inside bg_baseline.wait
    assert report.state == "WORKER_FINISHED" and len(spawned) == 1


def test_send_back_while_verifying_steers_the_live_worker(steward_home, capsys):
    store = Store(steward_home)
    job, runner, sr, spawned = _bg_job(store)
    assert runner.tick(job.job_id).state == "WORKER_FINISHED" and bg_verify.pending(store, job.job_id)
    spec = cr.RunSpec("sha-1", "python3 -m pytest -q", "e", "tc", True, 900.0)
    queued, _ = cr.request(store, spec, job_id=job.job_id, purpose="verify", now=time.time())
    tab_before = store.kv_get(job.job_id, "herdr_tab")

    assert _send(job.job_id, "your change breaks tests/test_x.py::t; fix it") == 0
    assert "sent back" in capsys.readouterr().out
    assert store.get_job(job.job_id).state == JobState.WORKER_FINISHED  # the runner owns the transition
    assert store.kv_get(job.job_id, "rework_pending") is None and store.kv_get(job.job_id, "herdr_tab") == tab_before

    report = runner.tick(job.job_id)
    assert report.state == "RUNNING"
    (sent,) = [i for i in Steering(store).all_instructions(job.job_id) if "breaks tests" in i["text"]]
    assert sent["status"] in ("delivered", "acknowledged")
    assert bg_verify.record(store, job.job_id)["status"] == "consumed"
    assert cr.get(store, queued.run_id).state == "cancelled" and cr.get(store, queued.run_id).error == "withdrawn by the job"
    # the pre-empted background process finishing late cannot resurrect the consumed record
    bg_verify.finish(store, job.job_id, status="failed", error="Lost: cancelled", sha="sha-1")
    assert bg_verify.record(store, job.job_id)["status"] == "consumed"


def test_inline_verification_and_goal_append_are_refused_with_the_reason(steward_home, capsys):
    store = Store(steward_home)
    job = _finish_job(store)
    for st in (JobState.RUNNING, JobState.WORKER_FINISHED):
        store.set_state(job.job_id, st, what="t", evidence={}, rule="t", judgment=Judgment.code)
    assert _send(job.job_id, "redo") == 2
    assert "verifying inline" in capsys.readouterr().err
    job2, runner, _, _ = _bg_job(store)
    runner.tick(job2.job_id)
    assert _send(job2.job_id, "redo", "--goal-append") == 2
    assert "--goal-append needs NEEDS_HUMAN" in capsys.readouterr().err
