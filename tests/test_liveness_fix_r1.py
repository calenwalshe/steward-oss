"""Task 10 fix round 1 (spec 2026-10-08 §5.1/§5.3): the ask survives ownership's done-state clear, an honest STOPPING
finish, code-judged fix-up stops, a PAUSED actor, release from the sweep, and the minor fixes."""
import time

import pytest

from steward import approvals, blockers, cleanup, cli, liveness, ownership, runner_exec
from steward.models import JobState, Judgment
from steward.runner_exec import CommandResult
from steward.store import Store

from herdr_fake import FakeHerdr

S = {"enabled": True, "every_s": 60.0, "orphan_s": 1800.0, "idle_s": 1800.0, "tries_per_day": 2}
ALIVE, DEAD = (lambda _: True), (lambda _: False)


def _job(store, state, repo="/tmp/r", goal="goal"):
    j = store.create_job(goal, repo)
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state.value, j.job_id))
    return store.get_job(j.job_id)


def _run(store, job, now, **fns):
    return liveness.remedy_default(store, job, now=now, settings=S, orphan_since=now - 1800, **fns)


# -- Important 1: the ask survives ownership's done-state clear -------------------------------------------------------

@pytest.mark.parametrize("state", [JobState.ACCEPTED, JobState.STOPPING])
def test_ask_survives_ownership_reconcile_in_non_terminal_done_states(steward_home, state):
    store = Store(); j = _job(store, state); now = time.time()
    bid = liveness.ask(store, j, tried=["finish_stop"], now=now, hours=1.0)
    assert bid
    ownership.reconcile_job(store, j.job_id, now=now + 1)
    ownership.reconcile_job(store, j.job_id, now=now + 2)
    assert blockers.get(store, bid).is_open
    assert liveness.actor(store, store.get_job(j.job_id), now=now + 3, settings=S, runner_alive=DEAD,
                          check_runs_live=DEAD) == "human:liveness_ask"


def test_ask_still_resolves_once_the_job_is_terminal(steward_home):
    store = Store(); j = _job(store, JobState.STOPPING); now = time.time()
    bid = liveness.ask(store, j, tried=["finish_stop"], now=now, hours=1.0)
    store.set_state(j.job_id, JobState.STOPPED, what="t", evidence={}, rule="t", judgment=Judgment.code)
    ownership.reconcile_job(store, j.job_id, now=now + 1)
    assert not blockers.get(store, bid).is_open


# -- Important 2: STOPPING with a leftover pane: close it through an explicit path, then finish (or say so) -----------

class _Herdr(FakeHerdr):
    """FakeHerdr plus `herdr pane close` (optionally refusing, to model a pane that will not close)."""

    def __init__(self, *a, stuck=(), **k):
        super().__init__(*a, **k)
        self.stuck = set(stuck)

    def run(self, argv, cwd, *, timeout=None, env=None):
        if list(argv[:3]) == ["herdr", "pane", "close"]:
            self.calls.append(list(argv))
            if argv[3] not in self.stuck:
                self.panes = [p for p in self.panes if p["pane_id"] != argv[3]]
            return CommandResult(argv=list(argv), cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)
        return super().run(argv, cwd, timeout=timeout, env=env)


def _stopping_with_panes(store, monkeypatch, *, stuck=(), worker_tab="w4Q:t2"):
    j = _job(store, JobState.STOPPING)
    store.kv_set(j.job_id, "runner_pane", "w4Q:p2")
    store.kv_set(j.job_id, "herdr_pane", "w4Q:p3")
    store.kv_set(j.job_id, "herdr_tab", "w4Q:t2")
    h = _Herdr([{"tab_id": "w4Q:t2", "label": "job"}, {"tab_id": "w4Q:t9", "label": "other"}],
               [{"pane_id": "w4Q:p2", "tab_id": "w4Q:t2", "label": f"runner {j.job_id[:8]}"},
                {"pane_id": "w4Q:p3", "tab_id": worker_tab, "label": "worker"}], stuck=stuck)
    monkeypatch.setattr(runner_exec, "SubprocessRunner", lambda: h)
    return j, h


def test_finish_stop_closes_the_leftover_panes_then_stops(steward_home, monkeypatch):
    store = Store(); j, h = _stopping_with_panes(store, monkeypatch); now = time.time()
    # the real stop handler answers a STOPPING job with a no-op, so it can never be what finishes one
    r = cli._build_control_handlers(store)["stop"]({"request_id": "s", "origin": "cli", "args": {"job_id": j.job_id}})
    assert r["detail"] == "already STOPPING; no action taken"
    out = _run(store, j, now, runner_alive=DEAD)
    assert out["rung"] == "finish_stop" and out["result"] == "ok", out
    assert store.get_job(j.job_id).state == JobState.STOPPED
    assert sorted(c[3] for c in h.cli("herdr", "pane", "close")) == ["w4Q:p2", "w4Q:p3"]
    ev = [e for e in store.events(j.job_id) if e.what == "liveness_panes_closed"]
    assert len(ev) == 1 and ev[0].judgment == Judgment.code and sorted(ev[0].evidence["closed"]) == ["w4Q:p2", "w4Q:p3"]
    fin = [e for e in store.events(j.job_id) if e.what == "stop finished by sweep"][-1]
    assert fin.judgment == Judgment.code and fin.rule == "liveness_finish_stop"
    assert not any(e.what == "operator stop" for e in store.events(j.job_id))


def test_finish_stop_reports_honestly_when_a_pane_will_not_close(steward_home, monkeypatch):
    store = Store(); j, h = _stopping_with_panes(store, monkeypatch, stuck={"w4Q:p3"}); now = time.time()
    out = _run(store, j, now, runner_alive=DEAD)
    assert out["rung"] == "finish_stop" and out["result"].startswith("refused")
    assert "still present" in out["result"] and "re-sent" not in out["result"]
    assert store.get_job(j.job_id).state == JobState.STOPPING


def test_finish_stop_never_closes_a_recorded_worker_pane_outside_the_jobs_tab(steward_home, monkeypatch):
    store = Store(); j, h = _stopping_with_panes(store, monkeypatch, worker_tab="w4Q:t9"); now = time.time()
    out = _run(store, j, now, runner_alive=DEAD)
    assert "w4Q:p3" not in [c[3] for c in h.cli("herdr", "pane", "close")]
    assert out["result"].startswith("refused") and store.get_job(j.job_id).state == JobState.STOPPING


# -- Important 3: the sweep's fix-up stops are code-judged; the owner's "stop" stays human ----------------------------

@pytest.mark.parametrize("state,after", [(JobState.RUNNING, JobState.STOPPING),
                                         (JobState.WORKER_FINISHED, JobState.STOPPED),
                                         (JobState.NEEDS_HUMAN, JobState.STOPPED)])
def test_sweep_fixup_stop_is_code_judged(steward_home, monkeypatch, state, after):
    store = Store(); child = _job(store, state); seen = {}
    store.kv_set(child.job_id, cleanup.FIXUP_STOP_KEY, {"cause": "parent released"})
    monkeypatch.setattr(liveness, "sweep", lambda st, **kw: seen.update(kw) or {})
    cli._liveness_tick(store, {k: (lambda cmd: pytest.fail("no handler")) for k in ("stop", "runner_restart", "launch")})
    from steward import train_housekeeping
    assert train_housekeeping.stop_marked_fixups(store, seen["stop_fn"]) == [child.job_id]
    assert store.get_job(child.job_id).state == after
    ev = [e for e in store.events(child.job_id) if e.rule == liveness.FIXUP_STOP_RULE]
    assert len(ev) == 1 and ev[0].judgment == Judgment.code
    assert not any(e.judgment == Judgment.human for e in store.events(child.job_id) if "stop" in e.what)


def test_code_stop_refuses_terminal_and_is_a_noop_for_stopping(steward_home):
    store = Store()
    done = _job(store, JobState.STOPPED); stopping = _job(store, JobState.STOPPING)
    assert liveness.code_stop(store, done.job_id)["accepted"] is False
    assert liveness.code_stop(store, stopping.job_id)["accepted"] is True
    assert store.get_job(stopping.job_id).state == JobState.STOPPING


def test_owner_deny_stop_stays_human(steward_home):
    store = Store(); j = _job(store, JobState.RUNNING)
    bid = liveness.ask(store, j, tried=["nudge"], now=time.time(), hours=1.0)
    aid = blockers.get(store, bid).clearing["params"]["approval_id"]
    r = cli._build_control_handlers(store)["deny"]({"op": "deny", "args": {"job_id": j.job_id, "approval_id": aid},
                                                    "origin": "cli", "request_id": "d1"})
    assert r["accepted"], r
    ev = [e for e in store.events(j.job_id) if e.what == "operator stop"]
    assert len(ev) == 1 and ev[0].judgment == Judgment.human


# -- R1: PAUSED with a live runner is the pause owner's -------------------------------------------------------------

def test_paused_with_live_runner_is_not_an_orphan(steward_home):
    store = Store(); j = _job(store, JobState.PAUSED); now = time.time()
    liveness.record_runner(store, j.job_id, turn_open=False, progress_ts=now - 7200, pid=1)
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=ALIVE, check_runs_live=DEAD) == "runner:paused"
    assert liveness.actor(store, j, now=now, settings=S, runner_alive=DEAD, check_runs_live=DEAD) is None
    out = _run(store, j, now, runner_alive=DEAD, heal_fn=lambda jid: {"accepted": True})
    assert out["rung"] == "heal_runner"


# -- R2: the sweep releases an incorporated ACCEPTED job (§5.3 "Called from ... the liveness sweep") ------------------

def _incorporated(store, tmp_path, *, receipt=True):
    j = _job(store, JobState.ACCEPTED, repo=str(tmp_path / "gone"))
    store.kv_set(j.job_id, "incorporated_commit", "abc123")
    if receipt:
        store.append_event(j.job_id, what="incorporated", evidence={"merge_commit": "abc123"}, rule="t",
                           judgment=Judgment.code)
    return j


def _sw(store, now, **kw):
    return liveness.sweep(store, now=now, settings={**S, "every_s": 0.0}, remedy=lambda *a, **k: {},
                          heal_lock=lambda *a, **k: {}, runner_alive=DEAD, check_runs_live=DEAD, **kw)


def test_sweep_releases_an_incorporated_accepted_job(steward_home, tmp_path):
    store = Store(); j = _incorporated(store, tmp_path)
    out = _sw(store, 10_000.0)
    assert out["released"] == [j.job_id] and store.get_job(j.job_id).state == JobState.RELEASED
    ev = [e for e in store.events(j.job_id) if e.what == "released on incorporation"]
    assert len(ev) == 1 and ev[0].judgment == Judgment.code


def test_sweep_release_without_evidence_is_one_event_not_spam(steward_home, tmp_path):
    store = Store(); j = _incorporated(store, tmp_path, receipt=False)
    for t in (10_000.0, 10_100.0, 10_200.0):
        out = _sw(store, t)
        assert out["released"] == []
    assert store.get_job(j.job_id).state == JobState.ACCEPTED
    ev = [e for e in store.events(j.job_id) if e.what == "liveness_remedy" and e.evidence.get("rung") == "release"]
    assert len(ev) == 1 and ev[0].evidence["result"].startswith("refused") and ev[0].judgment == Judgment.code


def test_sweep_release_error_is_an_event_not_a_crash(steward_home, tmp_path):
    store = Store(); j = _incorporated(store, tmp_path)

    def boom(st, jid):
        raise RuntimeError("git gone")
    out = _sw(store, 10_000.0, release_fn=boom)
    assert out["released"] == [] and store.get_job(j.job_id).state == JobState.ACCEPTED
    ev = [e for e in store.events(j.job_id) if e.what == "liveness_remedy" and e.evidence.get("rung") == "release"]
    assert len(ev) == 1 and ev[0].evidence["result"].startswith("error: RuntimeError")


# -- minors -----------------------------------------------------------------------------------------------------------

def test_m2_non_owner_approve_does_not_decide_the_liveness_approval(steward_home):
    store = Store(); j = _job(store, JobState.RUNNING)
    bid = liveness.ask(store, j, tried=["nudge"], now=time.time(), hours=1.0)
    aid = blockers.get(store, bid).clearing["params"]["approval_id"]
    r = cli._build_control_handlers(store)["approve"](
        {"op": "approve", "args": {"job_id": j.job_id, "approval_id": aid}, "origin": "cli",
         "actor": "operator_agent:claude-code", "request_id": "n1"})
    assert r["accepted"] is False and "owner" in r["detail"]
    assert approvals.get_approval(store, j.job_id, aid)["status"] == "pending"
    assert blockers.get(store, bid).is_open


def test_m3_heal_refusal_names_the_heal_states(steward_home):
    store = Store(); j = _job(store, JobState.STOPPING)
    r = cli._build_control_handlers(store)["runner_restart"](
        {"args": {"job_id": j.job_id, "mode": "heal"}, "origin": "liveness", "request_id": "h1"})
    assert r["accepted"] is False and "RUNNING" in r["detail"] and "PAUSED" in r["detail"]
    assert "WORKER_FINISHED" in r["detail"] and "CHECKS_PASSED" in r["detail"]


def test_m4_nudge_text_does_not_claim_commits():
    assert "commit" not in liveness.NUDGE_TEXT.lower()


def test_m5_an_ask_that_raised_nothing_does_not_spend_the_ask_try(steward_home):
    store = Store(); j = _job(store, JobState.ACCEPTED); now = time.time(); asked = []
    fail = lambda st, job, **k: asked.append(1) or ""  # noqa: E731
    assert _run(store, j, now, runner_alive=DEAD, ask_fn=fail)["result"] == ""
    assert "ask" not in (store.kv_get(j.job_id, liveness.TRIES_KEY) or {})
    _run(store, j, now + 1, runner_alive=DEAD, ask_fn=fail)
    assert len(asked) == 2
