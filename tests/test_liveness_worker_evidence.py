"""u2 (stall fixes 2026-10-14): a RUNNING job is alive only on WORKER evidence, never on the runner's or the
supervisors' own events. Evidence: job cce9f6e8 never had a worker, yet each `liveness_remedy` (heal_runner, nudge,
nudge, completion_review) was followed by `liveness_actor rule: runner:recent_event` — the nudge record, the runner
restart, the tech lead's review events all counted as "progress" — and `steward health` said "liveness ok" for ~8 h."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from steward import chat, liveness
from steward.models import JobState, Judgment
from steward.store import Store

S = {"enabled": True, "every_s": 60.0, "orphan_s": 1800.0, "idle_s": 1800.0, "tries_per_day": 2,
     "no_worker_s": 900.0}
ALIVE, DEAD = (lambda _: True), (lambda _: False)


def _job(store, state=JobState.RUNNING):
    j = store.create_job("g", "/tmp/r")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state.value, j.job_id))
    return store.get_job(j.job_id)


def _event(store, jid, what, rule, at):
    from steward import store as store_module
    ts = datetime.fromtimestamp(at, timezone.utc).isoformat()
    real, store_module.now_iso = store_module.now_iso, (lambda: ts)  # events are append-only: write it already dated
    try:
        store.append_event(jid, what=what, evidence={}, rule=rule, judgment=Judgment.code)
    finally:
        store_module.now_iso = real


def _actor(store, j, now):
    return liveness.actor(store, j, now=now, settings=S, runner_alive=ALIVE, check_runs_live=DEAD)


# The exact runner/supervisor events that kept cce9f6e8 "alive" (taken from its live event log).
RUNNER_EVENTS = [("runner_restarted", "liveness_runner_heal"), ("tech_lead_wired", "tech_lead_wiring"),
                 ("nudge instruction recorded", "steering_instruction"), ("orientation_brief_built",
                 "orientation_brief_cache"), ("tech_lead_review_started", "tech_lead_review"),
                 ("tech_lead_answered", "tech_lead_ordered_fallthrough"), ("tech_lead_action", "tech_lead_executed"),
                 ("tech_lead_playbook_used", "tech_lead_playbook"), ("blocker_raised", "ownership"),
                 ("worker dispatch started", "worker_dispatch"), ("instruction delivery observed",
                 "adapter_deliver_result")]


def test_runner_and_supervisor_events_are_not_proof_of_life(steward_home, monkeypatch, tmp_path):
    monkeypatch.setattr(liveness, "PROJECTS_DIR", tmp_path / "projects")
    store = Store(); j = _job(store); now = time.time()
    liveness.record_runner(store, j.job_id, turn_open=False, progress_ts=0.0, pid=1)
    for what, rule in RUNNER_EVENTS:
        _event(store, j.job_id, what, rule, now - 30)
    assert _actor(store, j, now) is None


def test_worker_frame_event_is_proof_of_life(steward_home, monkeypatch, tmp_path):
    monkeypatch.setattr(liveness, "PROJECTS_DIR", tmp_path / "projects")
    store = Store(); j = _job(store); now = time.time()
    liveness.record_runner(store, j.job_id, turn_open=False, progress_ts=0.0, pid=1)
    _event(store, j.job_id, "instruction acknowledged (typed ack frame)", "fabric_ack_frame", now - 30)
    assert _actor(store, j, now) == "worker:recent_evidence"
    # ... but not once it is older than idle_s
    assert _actor(store, j, now + S["idle_s"] + 1) is None


def test_worker_transcript_activity_is_proof_of_life(steward_home, monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    monkeypatch.setattr(liveness, "PROJECTS_DIR", projects)
    store = Store(); j = _job(store); now = time.time()
    liveness.record_runner(store, j.job_id, turn_open=False, progress_ts=0.0, pid=1)
    assert _actor(store, j, now) is None
    d = projects / chat.project_slug(chat.worktree_path(store, j.job_id))
    d.mkdir(parents=True)
    t = d / "s.jsonl"
    t.write_text("{}\n")
    os.utime(t, (now - 60, now - 60))
    assert _actor(store, j, now) == "worker:recent_evidence"


def test_open_turn_without_any_worker_evidence_since_dispatch_is_a_ghost(steward_home, monkeypatch, tmp_path):
    monkeypatch.setattr(liveness, "PROJECTS_DIR", tmp_path / "projects")
    store = Store(); j = _job(store); now = time.time()
    _event(store, j.job_id, "worker dispatch started", "worker_dispatch", now - 3600)
    liveness.record_runner(store, j.job_id, turn_open=True, progress_ts=0.0, pid=1)
    assert liveness.ghost(store, j, now=now, settings=S) is True
    assert _actor(store, j, now) is None
    # inside the dispatch grace the open turn still counts (the worker is starting up)
    assert liveness.ghost(store, j, now=now - 3600 + 60, settings=S) is False
    # once the worker has shown anything since dispatch, its open turn counts again
    _event(store, j.job_id, "worker permission requested", "worker_permission_requested_frame", now - 3000)
    assert liveness.ghost(store, j, now=now, settings=S) is False
    assert _actor(store, j, now) == "runner:turn_open"


def test_health_is_red_for_a_ghost_running_job(steward_home, monkeypatch, tmp_path):
    monkeypatch.setattr(liveness, "PROJECTS_DIR", tmp_path / "projects")
    store = Store(); j = _job(store); now = time.time()
    _event(store, j.job_id, "worker dispatch started", "worker_dispatch", now - 3600)
    store.kv_set(liveness.PSEUDO, "tick_at", now - 10)
    comp = liveness.component(store, now=now)
    assert comp.ok is False
    assert j.job_id[:8] in comp.detail and "no worker" in comp.detail


def test_ghost_job_skips_the_nudge_ladder_and_asks_once(steward_home, monkeypatch, tmp_path):
    monkeypatch.setattr(liveness, "PROJECTS_DIR", tmp_path / "projects")
    store = Store(); j = _job(store); now = time.time()
    _event(store, j.job_id, "worker dispatch started", "worker_dispatch", now - 3600)
    nudges, asks = [], []
    kw = dict(now=now, settings=S, orphan_since=now - 3600, runner_alive=ALIVE,
              nudge_fn=lambda jid, text: nudges.append(jid) or {"accepted": True},
              ask_fn=lambda store, job, **k: asks.append(job.job_id) or "blk-1")
    out = liveness.remedy_default(store, j, **kw)
    assert out["rung"] == "ask" and nudges == [] and asks == [j.job_id]
    liveness.remedy_default(store, j, **kw)
    assert asks == [j.job_id]  # exactly one notice


def test_a_sent_nudge_is_sent_not_ok_and_its_outcome_waits_for_the_worker(steward_home, monkeypatch, tmp_path):
    monkeypatch.setattr(liveness, "PROJECTS_DIR", tmp_path / "projects")
    store = Store(); j = _job(store); now = time.time()
    _event(store, j.job_id, "worker dispatch started", "worker_dispatch", now - 7200)
    _event(store, j.job_id, "worker reported done (typed frame)", "fabric_done_frame", now - 7000)  # not a ghost
    kw = dict(settings=S, runner_alive=ALIVE, nudge_fn=lambda jid, text: {"accepted": True},
              ask_fn=lambda store, job, **k: "blk-1")
    out = liveness.remedy_default(store, j, now=now, orphan_since=now - 3600, **kw)
    assert out == {"rung": "nudge", "try": 1, "result": "sent"}
    # inside the response window no second rung fires
    assert liveness.remedy_default(store, j, now=now + 60, orphan_since=now - 3600, **kw) == {}
    # the window passes with no worker evidence: the outcome is recorded as no response, then the next rung acts
    out = liveness.remedy_default(store, j, now=now + S["orphan_s"] + 1, orphan_since=now - 3600, **kw)
    assert out["rung"] == "nudge" and out["try"] == 2
    outcomes = [e.evidence for e in store.events(j.job_id) if e.what == "liveness_remedy_outcome"]
    assert outcomes == [{"rung": "nudge", "try": 1, "result": "no response"}]


def test_worker_response_after_a_nudge_is_recorded_by_the_sweep(steward_home, monkeypatch, tmp_path):
    monkeypatch.setattr(liveness, "PROJECTS_DIR", tmp_path / "projects")
    store = Store(); j = _job(store); now = time.time()
    _event(store, j.job_id, "worker dispatch started", "worker_dispatch", now - 7200)
    _event(store, j.job_id, "worker reported done (typed frame)", "fabric_done_frame", now - 7000)
    liveness.record_runner(store, j.job_id, turn_open=False, progress_ts=0.0, pid=1)
    store.kv_set(j.job_id, liveness.ORPHAN_KEY, now - 3600)
    liveness.remedy_default(store, j, now=now, settings=S, orphan_since=now - 3600, runner_alive=ALIVE,
                            nudge_fn=lambda jid, text: {"accepted": True})
    # a runner/supervisor event does not clear the orphan
    _event(store, j.job_id, "tech_lead_review_started", "tech_lead_review", now + 10)
    liveness.sweep(store, now=now + 30, settings=S, remedy=lambda *a, **k: {}, heal_lock=lambda *a, **k: {},
                   runner_alive=ALIVE, check_runs_live=DEAD)
    assert not [e for e in store.events(j.job_id) if e.what == "liveness_actor"]
    # the worker acknowledges: the orphan clears and the nudge's outcome is "worker responded"
    _event(store, j.job_id, "instruction acknowledged (typed ack frame)", "fabric_ack_frame", now + 100)
    liveness.sweep(store, now=now + 200, settings=S, remedy=lambda *a, **k: {}, heal_lock=lambda *a, **k: {},
                   runner_alive=ALIVE, check_runs_live=DEAD)
    assert [e.evidence["rule"] for e in store.events(j.job_id) if e.what == "liveness_actor"] == \
        ["worker:recent_evidence"]
    outcomes = [e.evidence for e in store.events(j.job_id) if e.what == "liveness_remedy_outcome"]
    assert outcomes == [{"rung": "nudge", "try": 1, "result": "worker responded"}]


def test_runner_restart_alone_is_not_worker_progress():
    from steward.heartbeat import Signals

    def sig(head, diff):
        return Signals(lifecycle="working", pid_alive=True, head_sha=head, diff_fingerprint=diff, last_check_exit=None,
                       error_fingerprint="", same_error_count=0, permission_prompt=False, running_process=None,
                       since_last_change_s=0.0)
    # first observation after a (re)start: no previous signals, so nothing the worker did
    assert liveness.worker_progress_ts(None, sig("a", "x"), now=100.0, previous=5.0) == 5.0
    assert liveness.worker_progress_ts(sig("a", "x"), sig("a", "x"), now=100.0, previous=5.0) == 5.0
    assert liveness.worker_progress_ts(sig("a", "x"), sig("b", "x"), now=100.0, previous=5.0) == 100.0
    assert liveness.worker_progress_ts(sig("a", "x"), sig("a", "y"), now=100.0, previous=5.0) == 100.0
