import os
import time

from steward import approvals, blockers, deadline, liveness, ownership, sequencing
from steward import autopilot_bg
from steward import delivery
from steward import landing_queue as q
from steward.models import JobState, Judgment
from steward.store import Store

S = {"enabled": True, "every_s": 60.0, "orphan_s": 1800.0, "idle_s": 1800.0, "tries_per_day": 2}
ALIVE, DEAD = (lambda _: True), (lambda _: False)


def _job(store, state):
    j = store.create_job("g", "/tmp/r")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state.value, j.job_id))
    return store.get_job(j.job_id)


def _check_run(store, job_id, *, state="running", heartbeat, consumed=None, run_id="run1"):
    with store._conn:
        store._conn.execute(
            "INSERT INTO check_runs (run_id, equiv_key, tree_sha, command, env_digest, toolchain, host, state, queue_seq,"
            " requested_by, enqueued_epoch, heartbeat_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, "k", "t", "pytest", "e", "py", "h", state, 1, job_id, heartbeat, heartbeat))
        store._conn.execute(
            "INSERT INTO check_run_waiters (run_id, job_id, purpose, how, attached_epoch, consumed_epoch)"
            " VALUES (?,?,?,?,?,?)", (run_id, job_id, "verify", "new", heartbeat, consumed))


# -- the brief's representative set ---------------------------------------------------------------------------

def test_running_with_open_turn_has_actor(steward_home):
    store = Store(); j = _job(store, JobState.RUNNING); now = time.time()
    liveness.record_runner(store, j.job_id, turn_open=True, progress_ts=now - 7200, pid=1)
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=ALIVE, check_runs_live=DEAD) == "runner:turn_open"


def test_running_idle_long_is_orphan(steward_home):
    store = Store(); j = _job(store, JobState.RUNNING); now = time.time()
    liveness.record_runner(store, j.job_id, turn_open=False, progress_ts=now - 7200, pid=1)
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=ALIVE, check_runs_live=DEAD) is None


def test_running_recent_real_event_is_actor(steward_home):
    store = Store(); j = _job(store, JobState.RUNNING); now = time.time()
    liveness.record_runner(store, j.job_id, turn_open=False, progress_ts=now - 7200, pid=1)
    store.append_event(j.job_id, what="worker reported done (typed frame)", evidence={}, rule="fabric_done_frame",
                       judgment=Judgment.code)
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=ALIVE, check_runs_live=DEAD) == "worker:recent_evidence"


def test_noise_events_do_not_count(steward_home):
    store = Store(); j = _job(store, JobState.RUNNING); now = time.time()
    liveness.record_runner(store, j.job_id, turn_open=False, progress_ts=now - 7200, pid=1)
    for w in liveness.NOISE_WHATS[:3]:
        store.append_event(j.job_id, what=w, evidence={}, rule="t", judgment=Judgment.code)
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=ALIVE, check_runs_live=DEAD) is None


def test_running_dead_runner_is_orphan_even_with_turn_open(steward_home):
    store = Store(); j = _job(store, JobState.RUNNING); now = time.time()
    liveness.record_runner(store, j.job_id, turn_open=True, progress_ts=now, pid=1)
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=DEAD, check_runs_live=DEAD) is None


def test_stopping_needs_live_runner(steward_home):
    store = Store(); j = _job(store, JobState.STOPPING)
    assert liveness.actor(store, j, now=1.0, settings=S, runner_alive=ALIVE, check_runs_live=DEAD) == "runner"
    assert liveness.actor(store, j, now=1.0, settings=S, runner_alive=DEAD, check_runs_live=DEAD) is None


def test_needs_human_with_confirmed_blocker_or_pending_approval(steward_home):
    store = Store(); j = _job(store, JobState.NEEDS_HUMAN); now = time.time()
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=DEAD, check_runs_live=DEAD) is None
    approvals.open_approval(store, j.job_id, "accept?", kind="acceptance")
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=DEAD, check_runs_live=DEAD) == "human:approval"


def test_accepted_queued_or_incorporated(steward_home, tmp_path):
    store = Store(); j = _job(store, JobState.ACCEPTED)
    assert liveness.actor(store, j, now=1.0, settings=S, runner_alive=DEAD, check_runs_live=DEAD) is None
    q.enqueue(store, j.job_id, tmp_path)
    assert liveness.actor(store, j, now=1.0, settings=S, runner_alive=DEAD, check_runs_live=DEAD) == "queue:landing"
    store.kv_set(j.job_id, "incorporated_commit", "abc")
    assert liveness.actor(store, j, now=1.0, settings=S, runner_alive=DEAD, check_runs_live=DEAD) == "incorporated"


def test_tl_wait_with_future_wake_is_actor(steward_home):
    store = Store(); j = _job(store, JobState.RUNNING); now = time.time()
    b, _ = blockers.raise_blocker(store, j.job_id, kind="verification_unverified", subject="v", claim="c",
                                  evidence=["e"], origin_actor=ownership.CONTROLLER, now=now)
    blockers.record_diagnosis(store, b.blocker_id, {"wait": {"until": "time"}, "wake_at": now + 600}, now=now)
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=ALIVE, check_runs_live=DEAD) == "tl_wait"
    blockers.record_diagnosis(store, b.blocker_id, {"wait": {"until": "time"}, "wake_at": now - 1}, now=now)
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=ALIVE, check_runs_live=DEAD) is None


# -- one per remaining spec §5.1 row / distinction ------------------------------------------------------------

def test_paused_follows_running_rules(steward_home):
    store = Store(); j = _job(store, JobState.PAUSED); now = time.time()
    liveness.record_runner(store, j.job_id, turn_open=True, progress_ts=now - 7200, pid=1)
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=ALIVE, check_runs_live=DEAD) == "runner:turn_open"
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=DEAD, check_runs_live=DEAD) is None


def test_running_stale_turn_record_does_not_count(steward_home):
    """A turn_open written long ago is not a runner that is ticking now."""
    store = Store(); j = _job(store, JobState.RUNNING); now = time.time() + 7200
    liveness.record_runner(store, j.job_id, turn_open=True, progress_ts=now - 9000, pid=1)
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=ALIVE, check_runs_live=DEAD) is None


def test_running_recent_progress_ts_is_actor(steward_home):
    store = Store(); j = _job(store, JobState.RUNNING); now = time.time()
    liveness.record_runner(store, j.job_id, turn_open=False, progress_ts=now - 60, pid=1)
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=ALIVE, check_runs_live=DEAD) == "worker:recent_evidence"


def test_running_live_check_run_needs_live_runner(steward_home):
    store = Store(); j = _job(store, JobState.RUNNING); now = time.time()
    liveness.record_runner(store, j.job_id, turn_open=False, progress_ts=now - 7200, pid=1)
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=ALIVE, check_runs_live=ALIVE) == "runner:check_run"
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=DEAD, check_runs_live=ALIVE) is None


def test_worker_finished_live_check_run_waiter_is_actor_without_runner(steward_home):
    store = Store(); now = time.time()
    for st in (JobState.WORKER_FINISHED, JobState.CHECKS_PASSED):
        j = _job(store, st)
        _check_run(store, j.job_id, heartbeat=now - 30, run_id="r-" + j.job_id)
        # default check_runs_live (the real check_run_waiters x check_runs query)
        assert liveness.actor(store, j, now=now, settings=S, runner_alive=DEAD) == "check_run"


def test_worker_finished_stale_or_consumed_check_run_is_not_live(steward_home):
    store = Store(); now = time.time()
    j = _job(store, JobState.WORKER_FINISHED)
    _check_run(store, j.job_id, heartbeat=now - 600, run_id="stale")
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=DEAD) is None
    k = _job(store, JobState.WORKER_FINISHED)
    _check_run(store, k.job_id, heartbeat=now - 30, consumed=now - 10, run_id="consumed")
    assert liveness.actor(store, k, now=now, settings=S, runner_alive=DEAD) is None
    m = _job(store, JobState.WORKER_FINISHED)
    _check_run(store, m.job_id, heartbeat=now - 30, state="done", run_id="done")
    assert liveness.actor(store, m, now=now, settings=S, runner_alive=DEAD) is None
    n = _job(store, JobState.WORKER_FINISHED)
    _check_run(store, n.job_id, heartbeat=now - 30, state="queued", run_id="queued")
    assert liveness.actor(store, n, now=now, settings=S, runner_alive=DEAD) == "check_run"


def test_worker_finished_runner_with_recent_closeout_event(steward_home):
    """WF/CP: runner live AND a closeout event within idle_s; an open turn alone is not an actor here."""
    store = Store(); j = _job(store, JobState.WORKER_FINISHED); now = time.time()
    liveness.record_runner(store, j.job_id, turn_open=True, progress_ts=now, pid=1)
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=ALIVE, check_runs_live=DEAD) is None
    store.append_event(j.job_id, what="verification started", evidence={}, rule="closeout", judgment=Judgment.code)
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=ALIVE, check_runs_live=DEAD) == "runner:recent_event"
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=DEAD, check_runs_live=DEAD) is None
    assert liveness.actor(store, j, now=now + 3600, settings=S, runner_alive=ALIVE, check_runs_live=DEAD) is None


def test_needs_human_confirmed_human_or_waiting_external_blocker(steward_home):
    store = Store(); now = time.time()
    for disp in ("confirmed_human", "waiting_external"):
        j = _job(store, JobState.NEEDS_HUMAN)
        blockers.raise_blocker(store, j.job_id, kind="verification_unverified", subject="v", claim="c", evidence=["e"],
                               origin_actor=ownership.CONTROLLER, now=now, disposition=disp)
        assert liveness.actor(store, j, now=now, settings=S, runner_alive=DEAD, check_runs_live=DEAD) == "human:blocker"
    k = _job(store, JobState.NEEDS_HUMAN)
    blockers.raise_blocker(store, k.job_id, kind="verification_unverified", subject="v", claim="c", evidence=["e"],
                           origin_actor=ownership.CONTROLLER, now=now)  # candidate: nobody is asked
    assert liveness.actor(store, k, now=now, settings=S, runner_alive=DEAD, check_runs_live=DEAD) is None


def test_needs_human_permission_and_general_approvals_count(steward_home):
    store = Store(); now = time.time()
    for kind in ("general", "permission"):
        j = _job(store, JobState.NEEDS_HUMAN)
        approvals.open_approval(store, j.job_id, "ok?", kind=kind)
        assert liveness.actor(store, j, now=now, settings=S, runner_alive=DEAD, check_runs_live=DEAD) == "human:approval"
    k = _job(store, JobState.NEEDS_HUMAN)
    rec = approvals.open_approval(store, k.job_id, "ok?", kind="acceptance")
    approvals.withdraw(store, k.job_id, rec["id"], by="test", reason="no longer asked")
    assert liveness.actor(store, k, now=now, settings=S, runner_alive=DEAD, check_runs_live=DEAD) is None


def test_accepted_parked_entry_is_not_queued(steward_home, tmp_path):
    store = Store(); j = _job(store, JobState.ACCEPTED)
    q.enqueue(store, j.job_id, tmp_path)
    q.park(store, j.job_id, "conflict")
    assert liveness.actor(store, j, now=1.0, settings=S, runner_alive=DEAD, check_runs_live=DEAD) is None


def test_accepted_live_delivery_worker(steward_home):
    store = Store(); j = _job(store, JobState.ACCEPTED); now = time.time()
    store.kv_set(j.job_id, delivery.WORKER_KEY, {"pid": os.getpid(), "started_epoch": now, "heartbeat_epoch": now})
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=DEAD, check_runs_live=DEAD) == "delivery_worker"


def test_accepted_live_autopilot_bg_run(steward_home):
    store = Store(); j = _job(store, JobState.ACCEPTED); now = time.time()
    store.kv_set(j.job_id, autopilot_bg.KEY, {"status": "running", "pid": os.getpid(), "heartbeat_at": now,
                                              "started_at": now})
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=DEAD, check_runs_live=DEAD) == "autopilot_bg"
    store.kv_set(j.job_id, autopilot_bg.KEY, {"status": "done", "pid": os.getpid(), "heartbeat_at": now})
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=DEAD, check_runs_live=DEAD) is None


def test_created_or_contracted_staged(steward_home):
    store = Store()
    for st in (JobState.CREATED, JobState.CONTRACTED):
        j = _job(store, st)
        assert liveness.actor(store, j, now=1.0, settings=S, runner_alive=DEAD, check_runs_live=DEAD) is None
        store.kv_set(j.job_id, sequencing.K_AFTER, "pred")
        assert liveness.actor(store, j, now=1.0, settings=S, runner_alive=DEAD, check_runs_live=DEAD) == "staged"


def test_contracted_holding_deadline_slot(steward_home):
    store = Store(); j = _job(store, JobState.CONTRACTED)
    store.kv_set(j.job_id, deadline.K_COMMIT, {"current": 1, "revisions": [{"rev": 1, "due_at": "2026-10-09T00:00:00Z"}]})
    assert liveness.actor(store, j, now=1.0, settings=S, runner_alive=DEAD, check_runs_live=DEAD) == "deadline_slot"


def test_job_level_tl_wait_kv(steward_home):
    store = Store(); j = _job(store, JobState.CONTRACTED); now = time.time()
    store.kv_set(j.job_id, "tech_lead_wait", {"wait": {"until": "time"}, "wake_at": now + 600, "at": now})
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=DEAD, check_runs_live=DEAD) == "tl_wait"
    assert liveness.actor(store, j, now=now + 601, settings=S, runner_alive=DEAD, check_runs_live=DEAD) is None


def test_own_sweep_and_blocker_bookkeeping_events_are_not_progress(steward_home):
    store = Store(); j = _job(store, JobState.RUNNING); now = time.time()
    liveness.record_runner(store, j.job_id, turn_open=False, progress_ts=now - 7200, pid=1)
    store.append_event(j.job_id, what="liveness_orphan", evidence={}, rule="liveness", judgment=Judgment.code)
    store.append_event(j.job_id, what="jev_anything", evidence={}, rule="jev", judgment=Judgment.code)
    store.append_event(j.job_id, what="heartbeat_x", evidence={}, rule="t", judgment=Judgment.code)
    blockers.raise_blocker(store, j.job_id, kind="verification_unverified", subject="v", claim="c", evidence=["e"],
                           origin_actor=ownership.CONTROLLER, now=now)
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=ALIVE, check_runs_live=DEAD) is None


def test_terminal_states_are_not_orphans(steward_home):
    store = Store()
    for st in (JobState.STOPPED, JobState.FAILED, JobState.RELEASED):
        j = _job(store, st)
        assert liveness.actor(store, j, now=1.0, settings=S, runner_alive=DEAD, check_runs_live=DEAD) == "terminal"


def test_actor_is_a_pure_read(steward_home, tmp_path):
    store = Store(); now = time.time()
    jobs = [_job(store, st) for st in JobState]
    def snap():
        c = store._conn
        return (c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                [tuple(r) for r in c.execute("SELECT job_id, key, value_json FROM kv ORDER BY job_id, key")],
                [tuple(r) for r in c.execute("SELECT * FROM blockers ORDER BY blocker_id")])
    before = snap()
    for j in jobs:
        liveness.actor(store, j, now=now, settings=S, runner_alive=DEAD, check_runs_live=DEAD)
    assert snap() == before


def test_default_probes_see_no_runner_and_no_check_run(steward_home):
    store = Store(); j = _job(store, JobState.STOPPING)
    assert liveness.actor(store, j, now=time.time(), settings=S) is None


def test_trajectory_events_are_not_progress(steward_home):
    """RC-5 / F1: Steward's own supervision records (trajectory review, efficiency, the TL idle pause) are not the
    worker making progress; a trajectory review must not reset the orphan clock (case seq 129906)."""
    store = Store(); j = _job(store, JobState.RUNNING); now = time.time()
    liveness.record_runner(store, j.job_id, turn_open=False, progress_ts=now - 7200, pid=1)
    for w, rule in (("trajectory_review_requested", "trajectory"), ("trajectory_concern_recorded", "trajectory"),
                    ("efficiency_finding", "efficiency"), ("tech_lead_idle_paused", "supervision_allowance")):
        store.append_event(j.job_id, what=w, evidence={}, rule=rule, judgment=Judgment.code)
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=ALIVE, check_runs_live=DEAD) is None
