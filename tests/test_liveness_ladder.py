"""Liveness remedy ladder and the single ask (spec 2026-10-08 §5.1 "Remedy ladder", "The ask", §7)."""
import time

from steward import approvals, bg_verify, blockers, cli, launch, liveness
from steward.models import JobState, Judgment
from steward.store import Store

from herdr_fake import FakeHerdr

S = {"enabled": True, "every_s": 60.0, "orphan_s": 1800.0, "idle_s": 1800.0, "tries_per_day": 2}


def _job(store, state, goal="goal"):
    j = store.create_job(goal, "/tmp/r")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state.value, j.job_id))
    return store.get_job(j.job_id)


def _run(store, job, now, **fns):
    return liveness.remedy_default(store, job, now=now, settings=S, orphan_since=now - 1800, **fns)


# -- the brief's tests ------------------------------------------------------------------------------------------------

def test_stopping_dead_runner_is_finished(steward_home):
    store = Store(); j = _job(store, JobState.STOPPING); now = time.time()
    out = _run(store, j, now, runner_alive=lambda _: False, pane_gone=lambda _: True)
    assert out["rung"] == "finish_stop" and store.get_job(j.job_id).state == JobState.STOPPED
    assert any(e.what == "stop finished by sweep" for e in store.events(j.job_id))


def test_stopping_with_live_runner_is_not_finished(steward_home):
    store = Store(); j = _job(store, JobState.STOPPING); now = time.time()
    assert _run(store, j, now, runner_alive=lambda _: True, pane_gone=lambda _: True) == {}
    assert store.get_job(j.job_id).state == JobState.STOPPING


def test_running_dead_runner_heals(steward_home):
    store = Store(); j = _job(store, JobState.WORKER_FINISHED); now = time.time(); healed = []
    out = _run(store, j, now, runner_alive=lambda _: False, heal_fn=lambda jid: healed.append(jid) or {"accepted": True})
    assert out["rung"] == "heal_runner" and healed == [j.job_id]


def test_idle_worker_gets_two_nudges_then_completion_review_then_ask(steward_home):
    store = Store(); j = _job(store, JobState.RUNNING); now = time.time(); nudged, reviewed = [], []
    liveness.record_runner(store, j.job_id, turn_open=False, progress_ts=now - 7200, pid=1)
    # u2: the worker did answer this dispatch (its turn ended) — a worker that never did is a ghost, asked at once
    store.append_event(j.job_id, what="worker reported done (typed frame)", evidence={}, rule="fabric_done_frame",
                       judgment=Judgment.code)
    fns = dict(runner_alive=lambda _: True, nudge_fn=lambda jid, text: nudged.append(text) or {"accepted": True},
               completion_review_fn=lambda jid: reviewed.append(jid) or {"status": "started"})
    assert _run(store, j, now, **fns)["rung"] == "nudge"
    assert _run(store, j, now + 1, **fns) == {}  # u2: a sent nudge waits orphan_s for the worker's answer
    assert _run(store, j, now + 1801, **fns)["rung"] == "nudge"
    assert _run(store, j, now + 3602, **fns)["rung"] == "completion_review"
    out = _run(store, j, now + 5403, **fns)
    assert out["rung"] == "ask" and len(nudged) == 2 and reviewed == [j.job_id]
    b = [b for b in blockers.open_blockers(store, j.job_id) if b.kind == "liveness_stuck"]
    assert len(b) == 1 and b[0].disposition == "confirmed_human"
    assert _run(store, j, now + 5404, **fns) == {}     # the ask is the actor now; never re-asked


def test_rung_tries_recorded_before_acting(steward_home):
    store = Store(); j = _job(store, JobState.WORKER_FINISHED); now = time.time()
    def boom(jid):
        raise RuntimeError("relay down")
    out = _run(store, j, now, runner_alive=lambda _: False, heal_fn=boom)
    assert out["result"].startswith("error") and store.kv_get(j.job_id, liveness.TRIES_KEY)["heal_runner"][0] >= now - 1


def test_needs_human_nothing_pending_resumes_closeout_when_green(steward_home):
    store = Store(); j = _job(store, JobState.NEEDS_HUMAN); now = time.time(); resumed = []
    # the brief wrote kv "bg_verify"; the real record key is bg_verify.KEY ("verify_bg")
    store.kv_set(j.job_id, bg_verify.KEY, {"status": "done", "result": {"new_failures": [], "dirty": False}})
    out = _run(store, j, now, runner_alive=lambda _: False, resume_closeout_fn=lambda jid: resumed.append(jid) or {"state": "CHECKS_PASSED"})
    assert out["rung"] == "resume_closeout" and resumed == [j.job_id]


def test_ask_text_and_options(steward_home):
    store = Store(); j = _job(store, JobState.RUNNING, goal="a " * 80)
    bid = liveness.ask(store, j, tried=["nudge", "nudge"], now=time.time(), hours=33.4)
    b = blockers.get(store, bid)
    req = b.human_request
    assert req["options"] == ["retry", "stop"] and "stuck in RUNNING for 33h" in req["decision"]
    assert "Tried: nudge, nudge" in req["decision"] and len(req["decision"]) < 300


# -- invariants (§7) and the rest of the ladder -----------------------------------------------------------------------

def test_each_rung_capped_then_single_ask_and_tries_bounded(steward_home):
    store = Store(); j = _job(store, JobState.WORKER_FINISHED); now = time.time(); healed = []
    fns = dict(runner_alive=lambda _: False, heal_fn=lambda jid: healed.append(jid) or {"accepted": False, "detail": "no pane"})
    outs = [_run(store, j, now + i, **fns) for i in range(6)]
    assert [o.get("rung") for o in outs] == ["heal_runner", "heal_runner", "ask", None, None, None]
    assert outs[0]["result"].startswith("refused") and len(healed) == 2
    assert len([b for b in blockers.for_job(store, j.job_id) if b.kind == "liveness_stuck"]) == 1
    assert len([e for e in store.events(j.job_id) if e.what == "liveness_ask"]) == 1


def test_stopping_dead_runner_with_pane_present_closes_panes_and_is_not_stopped_while_present(steward_home):
    # fix round 1 (Important 2): the leftover panes are closed through an explicit path, never a re-sent operator stop
    store = Store(); j = _job(store, JobState.STOPPING); now = time.time(); closes = []
    fns = dict(runner_alive=lambda _: False, pane_gone=lambda _: False,
               close_panes_fn=lambda jid: closes.append(jid) or {"closed": [], "failed": ["p"]})
    out = _run(store, j, now, **fns)
    assert out["rung"] == "finish_stop" and out["result"].startswith("refused") and closes == [j.job_id]
    assert "still present" in out["result"]
    assert store.get_job(j.job_id).state == JobState.STOPPING


def test_remedy_writes_a_code_judged_event_per_try(steward_home):
    store = Store(); j = _job(store, JobState.WORKER_FINISHED); now = time.time()
    _run(store, j, now, runner_alive=lambda _: False, heal_fn=lambda jid: {"accepted": True})
    ev = [e for e in store.events(j.job_id) if e.what == "liveness_remedy"]
    assert len(ev) == 1 and ev[0].judgment == Judgment.code and ev[0].evidence == {"rung": "heal_runner", "try": 1, "result": "ok"}


def test_worker_finished_live_runner_requeues_verify_twice(steward_home):
    store = Store(); j = _job(store, JobState.WORKER_FINISHED); now = time.time(); rq = []
    fns = dict(runner_alive=lambda _: True, requeue_verify_fn=lambda jid: rq.append(jid) or {"accepted": True})
    assert [_run(store, j, now + i, **fns).get("rung") for i in range(3)] == ["requeue_verify", "requeue_verify", "ask"]


def test_needs_human_not_green_reverifies_once_then_asks(steward_home):
    store = Store(); j = _job(store, JobState.NEEDS_HUMAN); now = time.time(); rq, resumed = [], []
    fns = dict(runner_alive=lambda _: False, requeue_verify_fn=lambda jid: rq.append(jid) or {"accepted": True},
               resume_closeout_fn=lambda jid: resumed.append(jid))
    assert [_run(store, j, now + i, **fns).get("rung") for i in range(2)] == ["requeue_verify", "ask"]
    assert resumed == [] and rq == [j.job_id]


def test_needs_human_with_an_open_review_blocker_never_bypasses_it(steward_home):
    store = Store(); j = _job(store, JobState.NEEDS_HUMAN); now = time.time(); called = []
    store.kv_set(j.job_id, bg_verify.KEY, {"status": "done", "result": {"new_failures": [], "dirty": False}})
    blockers.raise_blocker(store, j.job_id, kind="test_weakening", subject="t", claim="weakened", evidence=["x"],
                           origin_actor=liveness._controller(), now=now)
    fns = dict(runner_alive=lambda _: False, resume_closeout_fn=called.append, requeue_verify_fn=called.append)
    assert _run(store, j, now, **fns)["rung"] == "ask" and called == []


def test_contracted_relaunches_once_then_asks(steward_home):
    store = Store(); j = _job(store, JobState.CONTRACTED); now = time.time(); rl = []
    fns = dict(runner_alive=lambda _: False, relaunch_fn=lambda jid: rl.append(jid) or {"accepted": True})
    assert [_run(store, j, now + i, **fns).get("rung") for i in range(2)] == ["relaunch", "ask"] and rl == [j.job_id]


def test_accepted_not_incorporated_goes_to_the_ask_without_stop(steward_home):
    store = Store(); j = _job(store, JobState.ACCEPTED); now = time.time()
    out = _run(store, j, now, runner_alive=lambda _: False)
    assert out["rung"] == "ask"
    b = blockers.get(store, out["result"])
    assert b.human_request["options"] == ["retry"]


def test_ask_offers_release_only_when_incorporated(steward_home):
    store = Store(); j = _job(store, JobState.ACCEPTED); store.kv_set(j.job_id, "incorporated_commit", "abc")
    bid = liveness.ask(store, j, tried=[], now=time.time(), hours=2.0)
    req = blockers.get(store, bid).human_request
    assert req["options"] == ["retry", "release"] and "Tried: nothing applicable" in req["decision"]


def test_ask_is_an_actor_so_the_sweep_does_not_spam(steward_home):
    store = Store(); j = _job(store, JobState.RUNNING); now = time.time()
    liveness.ask(store, j, tried=["nudge"], now=now, hours=1.0)
    assert liveness.actor(store, store.get_job(j.job_id), now=now, settings=S, runner_alive=lambda _: False,
                          check_runs_live=lambda _: False) == "human:liveness_ask"


def test_ask_links_one_general_approval_marked_as_the_liveness_ask(steward_home):
    store = Store(); j = _job(store, JobState.RUNNING); now = time.time()
    bid = liveness.ask(store, j, tried=["nudge"], now=now, hours=1.0)
    pend = approvals.list_approvals(store, j.job_id, status="pending")
    assert len(pend) == 1 and pend[0]["kind"] == "general" and pend[0].get("liveness_ask") is True
    assert pend[0].get("blocker_id") == bid


def test_a_failing_remedy_is_an_event_not_a_crash_of_the_sweep(steward_home):
    store = Store(); j = _job(store, JobState.RUNNING)
    def bad(*a, **k):
        raise RuntimeError("boom")
    kw = dict(settings={**S, "every_s": 0.0}, heal_lock=lambda *a, **k: {}, runner_alive=lambda _: False,
              check_runs_live=lambda _: False, remedy=bad)
    liveness.sweep(store, now=10_000.0, **kw)
    liveness.sweep(store, now=12_000.0, **kw)
    liveness.sweep(store, now=12_100.0, **kw)
    ev = [e for e in store.events(j.job_id) if e.what == "liveness_remedy"]
    assert len(ev) == 1 and ev[0].evidence["result"].startswith("error: RuntimeError")


def test_sweep_stops_marked_fixups_through_stop_fn(steward_home):
    from steward import cleanup
    store = Store(); child = _job(store, JobState.RUNNING); stopped = []
    store.kv_set(child.job_id, cleanup.FIXUP_STOP_KEY, {"cause": "parent released"})
    liveness.sweep(store, now=1.0, settings=S, heal_lock=lambda *a, **k: {}, remedy=lambda *a, **k: {},
                   runner_alive=lambda _: True, check_runs_live=lambda _: False,
                   stop_fn=lambda jid: stopped.append(jid) or {"accepted": True})
    assert stopped == [child.job_id] and store.kv_get(child.job_id, cleanup.FIXUP_STOP_KEY) is None


# -- default fns (no real processes) ----------------------------------------------------------------------------------

def test_default_nudge_is_a_code_judged_non_urgent_liveness_steer(steward_home):
    from steward.steering import Steering
    store = Store(); j = _job(store, JobState.RUNNING)
    liveness._default_nudge(store)(j.job_id, liveness.NUDGE_TEXT)
    (ins,) = Steering(store).all_instructions(j.job_id)
    assert ins["origin"] == "liveness" and ins["verb"] == "nudge" and not ins.get("urgent")
    ev = [e for e in store.events(j.job_id) if e.what == "nudge instruction recorded"][-1]
    assert ev.judgment == Judgment.code
    try:
        Steering(store).record(j.job_id, "nudge", "x", origin="liveness", urgent=True)
        raise AssertionError("an urgent liveness steer must be refused")
    except ValueError:
        pass


def test_default_completion_review_is_an_outer_tech_lead_fact(steward_home):
    from steward.fleet_runtime import Arbiter
    store = Store(); j = _job(store, JobState.RUNNING)
    liveness._default_completion_review(store)(j.job_id)
    (f,) = Arbiter(store).pending_for_tech_lead(j.job_id)
    assert f["kind"] == "worker_stopped" and "liveness" in f["data"]["reason"]


def test_default_requeue_verify_from_needs_human_returns_to_worker_finished(steward_home):
    store = Store(); j = _job(store, JobState.NEEDS_HUMAN)
    store.kv_set(j.job_id, bg_verify.KEY, {"status": "failed", "error": "x"})
    res = liveness._default_requeue_verify(store)(j.job_id)
    assert res["accepted"] and store.get_job(j.job_id).state == JobState.WORKER_FINISHED
    assert bg_verify.record(store, j.job_id)["status"] == "consumed"
    assert bg_verify.record(store, j.job_id)["outcome"] == "requeued"
    assert any(e.what == "verify_requeued" for e in store.events(j.job_id))
    assert not any("crashed" in e.what for e in store.events(j.job_id))


def test_default_resume_closeout_rearms_the_green_verification(steward_home):
    store = Store(); j = _job(store, JobState.NEEDS_HUMAN)
    store.kv_set(j.job_id, bg_verify.KEY, {"status": "consumed", "outcome": "consumed", "sha": "s1",
                                           "result": {"commit_sha": "s1", "new_failures": [], "dirty": False}})
    res = liveness._default_resume_closeout(store)(j.job_id)
    assert res["accepted"] and store.get_job(j.job_id).state == JobState.WORKER_FINISHED
    assert bg_verify.pending(store, j.job_id) and bg_verify.record(store, j.job_id)["status"] == "done"


def test_heal_mode_restores_a_worker_finished_runner(tmp_path, monkeypatch):
    store = Store(tmp_path / "home")
    job = store.create_job("goal", str(tmp_path))
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (JobState.WORKER_FINISHED.value, job.job_id))
    store.kv_set(job.job_id, "runner_pane", "w4Q:p2")
    store.kv_set(job.job_id, "herdr_pane", "w4Q:p3")
    store.kv_set(job.job_id, "launch_args", {"worker": "fabric", "interval": 15.0})
    h = FakeHerdr([{"tab_id": "w4Q:t2", "label": "job"}],
                  [{"pane_id": "w4Q:p2", "tab_id": "w4Q:t2", "label": f"runner {job.job_id[:8]}"},
                   {"pane_id": "w4Q:p3", "tab_id": "w4Q:t2", "label": "worker"}])
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: h)
    # conftest isolates LIVE_ROOT for this module (no git HEAD there): pin the deployed root like test_runner_heal sees it
    monkeypatch.setattr(launch.deployed, "launch_root", lambda home, live: (tmp_path, None))
    handlers = cli._build_control_handlers(store)
    r = handlers["runner_restart"]({"args": {"job_id": job.job_id, "mode": "heal"}, "origin": "liveness", "request_id": "x"})
    assert r["accepted"] and not r.get("noop"), r
    ev = [e for e in store.events(job.job_id) if e.what == "runner_restarted"][-1]
    assert ev.judgment == Judgment.code and ev.rule == "liveness_runner_heal"
    # a deliberate (non-heal) restart still needs RUNNING; launch.heal_runner's own default stays RUNNING-only
    assert not handlers["runner_restart"]({"args": {"job_id": job.job_id}, "origin": "cli", "request_id": "y"})["accepted"]
    assert not launch.heal_runner(h, store, store.get_job(job.job_id))["accepted"]
    assert launch.HEAL_STATES == {JobState.RUNNING, JobState.PAUSED, JobState.WORKER_FINISHED, JobState.CHECKS_PASSED}


# -- the owner's answer (existing approval path) ----------------------------------------------------------------------

def _asked(store, state=JobState.RUNNING):
    j = _job(store, state)
    bid = liveness.ask(store, j, tried=["nudge"], now=time.time(), hours=1.0)
    aid = blockers.get(store, bid).clearing["params"]["approval_id"]
    return j, bid, aid


def test_owner_approve_means_retry_resets_tries_and_resolves(steward_home):
    store = Store(); j, bid, aid = _asked(store)
    store.kv_set(j.job_id, liveness.TRIES_KEY, {"nudge": [time.time()], "ask": [time.time()]})
    handlers = cli._build_control_handlers(store)
    r = handlers["approve"]({"op": "approve", "args": {"job_id": j.job_id, "approval_id": aid}, "origin": "cli",
                             "request_id": "r1"})
    assert r["accepted"], r
    assert store.kv_get(j.job_id, liveness.TRIES_KEY) is None
    assert not blockers.get(store, bid).is_open
    ev = [e for e in store.events(j.job_id) if e.what == "liveness_answer"][-1]
    assert ev.evidence["answer"] == "retry" and ev.judgment == Judgment.human


def test_owner_deny_means_stop(steward_home, monkeypatch):
    store = Store(); j, bid, aid = _asked(store)
    handlers = cli._build_control_handlers(store)
    r = handlers["deny"]({"op": "deny", "args": {"job_id": j.job_id, "approval_id": aid}, "origin": "cli",
                          "request_id": "r2"})
    assert r["accepted"], r
    assert store.get_job(j.job_id).state in (JobState.STOPPING, JobState.STOPPED)
    assert [e.evidence["answer"] for e in store.events(j.job_id) if e.what == "liveness_answer"] == ["stop"]


def test_apply_answer_release_uses_code_release_only_with_evidence(steward_home):
    store = Store(); j = _job(store, JobState.ACCEPTED)
    out = liveness.apply_answer(store, j.job_id, "release", stop_fn=None, now=time.time())
    assert out["accepted"] is False and store.get_job(j.job_id).state == JobState.ACCEPTED


def test_control_tick_wires_the_ladder_in_process_with_stop_fn(steward_home, monkeypatch):
    store = Store(); seen = {}
    calls = []
    handlers = {k: (lambda name: lambda cmd: calls.append((name, cmd)) or {"accepted": True})(k)
                for k in ("stop", "runner_restart", "launch")}
    monkeypatch.setattr(liveness, "sweep", lambda st, **kw: seen.update(kw) or {})
    cli._liveness_tick(store, handlers)
    assert callable(seen["stop_fn"]) and callable(seen["remedy"])
    # fix round 1 (Important 3): the sweep's stop is code-judged (liveness.code_stop), never the operator handler
    fx = _job(store, JobState.CONTRACTED)
    assert seen["stop_fn"](fx.job_id)["accepted"] and calls == []
    assert store.get_job(fx.job_id).state == JobState.STOPPED
    j = _job(store, JobState.WORKER_FINISHED)
    monkeypatch.setattr(liveness, "_default_runner_alive", lambda st: (lambda _: False))
    out = seen["remedy"](store, j, now=time.time(), settings=S, orphan_since=time.time() - 1800)
    assert out["rung"] == "heal_runner" and calls[-1][0] == "runner_restart"
    assert calls[-1][1]["args"] == {"job_id": j.job_id, "mode": "heal"}
