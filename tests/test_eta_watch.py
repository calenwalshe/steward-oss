"""The ETA inside the efficiency watcher (phase 18-05, G13, G19, G20): every recompute stores it, off with the one switch,
never steers. Real tmp git repos (tests/eta_fixtures.py), synthetic transcripts, fake clocks, a spy notify. No network."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from eta_fixtures import advance, fake_now, load_timeline, replay_events, setup_job  # noqa: E402
from efficiency_synth import agent_lines, write_subagent  # noqa: E402
from steward import efficiency_watch as ew, eta  # noqa: E402
from steward.chat import project_slug, worktree_path  # noqa: E402
from steward.runner_exec import SubprocessRunner  # noqa: E402
from steward.steering import Steering  # noqa: E402
from steward.store import Store  # noqa: E402

TL = load_timeline("a7d292d8")
T0 = TL["t0"]


class Rec:
    """A git runner that records every argv and delegates to the real one (real tmp repos only)."""

    def __init__(self):
        self.real = SubprocessRunner()
        self.calls: list = []

    def run(self, argv, cwd, **kw):
        self.calls.append(list(argv))
        return self.real.run(argv, cwd, **kw)

    def shell_line(self, line, cwd, **kw):  # pragma: no cover - never used
        raise AssertionError("no shell")


class Boom:
    def run(self, argv, cwd, **kw):
        raise RuntimeError("git exploded")

    def shell_line(self, line, cwd, **kw):  # pragma: no cover
        raise AssertionError("no shell")


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def sub(d, agent_id, typ, desc, start_min, end_min, now_min=None):
    """One helper transcript on the job's timeline: finished at end_min, or running (unfinished) until now_min."""
    t = T0 + start_min * 60
    if end_min is not None:
        lines = agent_lines(t, calls=2, gen_s=[1, (end_min - start_min) * 60 - 2], tool_s=[1], prefix=agent_id,
                            finished=True)
    else:
        lines = agent_lines(t, calls=3, gen_s=[1, 1, 1], tool_s=[1, 1], prefix=agent_id, finished=False)
    write_subagent(d, "w", agent_id, typ, desc, lines)


def a7_world(tmp_path, monkeypatch, *, at_min=64.5, goal=None):
    """The a7d292d8 job at `at_min`: planner, three finished executors, plan 16-04 running."""
    tmp_path = Path(tmp_path)
    home = tmp_path / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("STEWARD_HOME", str(home))   # `steward ...` in a test reads this world
    store = Store(home)
    clock = Clock(T0 + at_min * 60)
    fake_now(monkeypatch, clock)
    until = T0 + at_min * 60
    job, _ = setup_job(store, TL, until=until)
    replay_events(store, TL["job"], TL, until)
    proj = tmp_path / "projects"
    d = proj / project_slug(worktree_path(store, job.job_id))
    d.mkdir(parents=True)
    store.kv_set(job.job_id, "worker_session_id", "w")
    sub(d, "p1", "gsd-planner", "Plan Phase 16 efficiency watcher", 4.8, 34.4)
    sub(d, "e1", "gsd-executor", "Execute plan 16-01", 36.1, 40.8)
    sub(d, "e2", "gsd-executor", "Execute plan 16-02", 40.9, 53.8)
    sub(d, "e3", "gsd-executor", "Execute plan 16-03", 54.0, 63.8)
    sub(d, "e4", "gsd-executor", "Execute plan 16-04", 63.9, None)
    return store, job, proj, d, clock


def watcher(store, proj, clock, tmp_path, *, runner=None, spend=None, **kw):
    spend = spend or Clock(1000.0)
    kw.setdefault("tokens_fn", lambda st, jid, now=None: {"sources": {}, "last_hour": {"usd": 0.0}})
    w = ew.EfficiencyWatcher(store, clock=clock, spend_clock=spend, projects_dir=proj, config_dir=tmp_path / "cfg",
                             cpu_fn=lambda: 0.0, eta_runner=runner if runner is not None else SubprocessRunner(), **kw)
    return w, spend


def events(store, job, what):
    return [e for e in store.events(job.job_id) if e.what == what]


def state_eta(store, job):
    return (store.kv_get(job.job_id, ew.STATE_KEY) or {}).get("eta")


# -- the tracer ------------------------------------------------------------------------------------------------


def test_tracer_watcher_stores_units_eta(tmp_path, monkeypatch):
    store, job, proj, d, clock = a7_world(tmp_path, monkeypatch)
    w, _ = watcher(store, proj, clock, tmp_path, runner=SubprocessRunner())
    state = w.step(job.job_id)
    e = state_eta(store, job)
    assert state["eta"] == e
    assert e["source"] == "units" and e["units_done"] == 3 and e["units_total"] == 7
    assert e["units_source"] == "planning" and e["unit_label"] == "plan 16-04" and e["kind"] == "gsd-build"
    assert isinstance(e["p50_min"], float) and e["p50_min"] > 0 and e["jev_state"] == "off"
    [ev] = events(store, job, "eta_changed")
    assert ev.rule == "eta" and ev.judgment.value == "code" and ev.evidence["to"] == "units"
    assert not events(store, job, "eta_error")


def test_eta_only_at_recompute(tmp_path, monkeypatch):
    store, job, proj, d, clock = a7_world(tmp_path, monkeypatch)
    rec = Rec()
    w, spend = watcher(store, proj, clock, tmp_path, runner=rec)
    w.step(job.job_id)
    first = state_eta(store, job)
    calls = len(rec.calls)
    assert calls > 0
    clock.t += 30
    spend.t += 30
    assert w.step(job.job_id) is None                      # not due, no helper finished
    assert len(rec.calls) == calls and state_eta(store, job)["at"] == first["at"]
    clock.t += 31
    spend.t += 31
    assert w.step(job.job_id) is not None                  # 61 s later: recomputed
    assert state_eta(store, job)["at"] == first["at"] + 61


def test_off_means_no_eta_and_no_git(tmp_path, monkeypatch):
    store, job, proj, d, clock = a7_world(tmp_path, monkeypatch)
    store.kv_set(job.job_id, ew.SWITCH_KEY, "off")
    rec = Rec()
    w, _ = watcher(store, proj, clock, tmp_path, runner=rec)
    w.step(job.job_id)
    assert "eta" not in store.kv_get(job.job_id, ew.STATE_KEY) and rec.calls == []
    store.kv_set(job.job_id, ew.SWITCH_KEY, "shadow")
    monkeypatch.setenv("STEWARD_EFFICIENCY", "0")
    w.step(job.job_id)
    assert "eta" not in store.kv_get(job.job_id, ew.STATE_KEY) and rec.calls == []


def test_codex_is_na_without_git(tmp_path, monkeypatch):
    store, job, proj, d, clock = a7_world(tmp_path, monkeypatch)
    store.kv_set(job.job_id, "worker_kind", "codex")
    rec = Rec()
    w, _ = watcher(store, proj, clock, tmp_path, runner=rec)
    w.step(job.job_id)
    e = state_eta(store, job)
    assert e["source"] == "n/a" and e["p50_min"] is None and rec.calls == []
    assert eta.render(e, clock.t)["text"] == "eta n/a (codex transcript)"
    seen = []
    orig = store.kv_set
    monkeypatch.setattr(store, "kv_set", lambda j, k, v: (seen.append(k), orig(j, k, v))[1])
    w.step(job.job_id)
    assert ew.STATE_KEY not in seen                        # unchanged: no rewrite churn


def test_eta_error_never_breaks_the_tick(tmp_path, monkeypatch):
    store, job, proj, d, clock = a7_world(tmp_path, monkeypatch)
    w, spend = watcher(store, proj, clock, tmp_path, runner=Boom())
    state = w.step(job.job_id)
    assert state is not None and state["summary"] is not None and "findings" in state
    [ev] = events(store, job, "eta_error")
    assert "git exploded" in ev.evidence["error"] and ev.rule == "eta"
    clock.t += 61
    spend.t += 61
    assert w.step(job.job_id) is not None
    assert len(events(store, job, "eta_error")) == 1      # the same message is recorded once


def test_eta_exception_inside_estimate_is_caught(tmp_path, monkeypatch):
    store, job, proj, d, clock = a7_world(tmp_path, monkeypatch)
    w, _ = watcher(store, proj, clock, tmp_path)
    monkeypatch.setattr(eta, "estimate", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad math")))
    state = w.step(job.job_id)
    assert state is not None and "eta" not in state
    [ev] = events(store, job, "eta_error")
    assert "ValueError: bad math" in ev.evidence["error"]


def test_eta_never_steers(tmp_path, monkeypatch):
    store, job, proj, d, clock = a7_world(tmp_path, monkeypatch)
    spy = []
    w, spend = watcher(store, proj, clock, tmp_path, notify=lambda *a: spy.append(a))
    before = len(Steering(store).all_instructions(job.job_id))
    for _ in range(10):
        w.step(job.job_id)
        clock.t += 61
        spend.t += 61
    assert len(Steering(store).all_instructions(job.job_id)) == before
    assert spy == []


def test_phase16_replay_still_has_no_subprocess(tmp_path):
    from efficiency_replay import run_replay, steady_job_files

    res = run_replay(steady_job_files(), tmp_path)
    assert res.subprocess_calls == []
    e = res.state.get("eta")
    assert e is not None and e["source"] in ("none", "prior", "peers")


# -- Jev labels, fact checks, plan-ahead, the overdue push -------------------------------------------------------


def rec(at, **answers):
    return {"at": at, "answers": {q: {"choice": c, "confidence": conf} for q, (c, conf) in answers.items()}}


def put_records(store, job, rows):
    store.kv_set(job.job_id, "eta_jev", rows)
    store.kv_set(job.job_id, "jev_status", "ok:ok")


def test_jev_labels_voted_into_eta(tmp_path, monkeypatch):
    store, job, proj, d, clock = a7_world(tmp_path, monkeypatch)
    put_records(store, job, [rec(1, unit=("16-04", 1.0), position=("late", 0.9)),
                             rec(2, unit=("16-04", 0.9), position=("late", 0.9)),
                             rec(3, unit=("16-04", 1.0), position=("late", 1.0))])
    w, _ = watcher(store, proj, clock, tmp_path)
    w.step(job.job_id)
    e = state_eta(store, job)
    assert e["jev"] == {"unit": "16-04", "position": "late"} and e["jev_state"] == "ok"
    assert not events(store, job, "eta_jev_rejected") and not events(store, job, "eta_disagree")


def test_jev_states_off_and_unavailable(tmp_path, monkeypatch):
    store, job, proj, d, clock = a7_world(tmp_path, monkeypatch)
    rows = [rec(i, unit=("16-04", 1.0)) for i in (1, 2, 3)]
    put_records(store, job, rows)
    store.kv_set(job.job_id, "supervisor_jev", "off")
    w, spend = watcher(store, proj, clock, tmp_path)
    w.step(job.job_id)
    e = state_eta(store, job)
    assert e["jev"] is None and e["jev_state"] == "off"
    store.kv_set(job.job_id, "supervisor_jev", "shadow")
    store.kv_set(job.job_id, "jev_status", "unavailable:timeout")
    clock.t += 61
    spend.t += 61
    w.step(job.job_id)
    e = state_eta(store, job)
    assert e["jev"] is None and e["jev_state"] == "unavailable"
    assert eta.no_jev_note(e) == " (no Jev)"


def test_jev_rejections_and_disagreements_are_events_and_never_change_minutes(tmp_path, monkeypatch):
    plain, job0, proj0, _d0, clock0 = a7_world(tmp_path / "a", monkeypatch)
    w0, _ = watcher(plain, proj0, clock0, tmp_path / "a")
    w0.step(job0.job_id)
    base_eta = state_eta(plain, job0)

    store, job, proj, d, clock = a7_world(tmp_path / "b", monkeypatch)
    put_records(store, job, [rec(i, unit=("16-02", 1.0), fraction=("80-100", 0.9)) for i in (1, 2, 3)])
    w, spend = watcher(store, proj, clock, tmp_path / "b")
    w.step(job.job_id)
    [rej] = events(store, job, "eta_jev_rejected")
    assert rej.evidence == {"question": "unit", "label": "16-02", "reason": "unit_done"}
    [dis] = events(store, job, "eta_disagree")
    assert dis.evidence == {"question": "fraction", "label": "80-100", "code": "3/7"}
    assert rej.rule == dis.rule == "eta" and rej.judgment.value == dis.judgment.value == "code"
    e = state_eta(store, job)
    assert "unit" not in e["jev"] and e["jev"]["fraction"] == "80-100"          # kept but the code number stands
    for _ in range(2):                                                         # the same records again: no new events
        clock.t += 61
        spend.t += 61
        w.step(job.job_id)
    assert len(events(store, job, "eta_jev_rejected")) == 1 and len(events(store, job, "eta_disagree")) == 1
    assert e["mid_min"] == pytest.approx(base_eta["mid_min"]) and e["p10_min"] == pytest.approx(base_eta["p10_min"])
    assert e["p90_min"] == pytest.approx(base_eta["p90_min"]) and e["source"] == base_eta["source"]


def test_plan_ahead_from_planning_serial(tmp_path, monkeypatch):
    store, job, proj, d, clock = a7_world(tmp_path, monkeypatch)
    w, spend = watcher(store, proj, clock, tmp_path)
    w.step(job.job_id)
    assert state_eta(store, job)["plan_ahead"] is True
    finding = {"id": "planning_serial:gsd-planner", "kind": "planning_serial", "subject": "gsd-planner",
               "actionable": False, "actions": [], "policy": None, "evidence": "serial", "stage": "gsd-planner",
               "metric": {"name": "serial_plan_s", "value": 900.0}, "saving_usd": 0.0, "saving_min": 15.0}
    monkeypatch.setattr(ew.detect_module, "detect", lambda *a, **k: [dict(finding)])
    clock.t += 61
    spend.t += 61
    w.step(job.job_id)
    assert state_eta(store, job)["plan_ahead"] is False


def _push_world(tmp_path, monkeypatch, notify_value):
    store, job, proj, d, clock = a7_world(tmp_path, monkeypatch)
    if notify_value is not None:
        store.kv_set(job.job_id, "eta_notify", notify_value)
    spy = []
    w, spend = watcher(store, proj, clock, tmp_path, notify=lambda *a: spy.append(a))
    return store, job, w, clock, spend, spy


def _step(w, job, clock, spend, dt):
    clock.t += dt
    spend.t += 61
    return w.step(job.job_id)


def test_overdue_push_opt_in(tmp_path, monkeypatch):
    store, job, w, clock, spend, spy = _push_world(tmp_path, monkeypatch, "overdue")
    w.step(job.job_id)
    end = state_eta(store, job)["shown"]["p50_end"]
    assert spy == []
    _step(w, job, clock, spend, end - clock.t + 120)                       # now past the shown end
    assert len(events(store, job, "eta_overdue")) == 1 and len(spy) == 1
    j, kind, title, body = spy[0]
    assert j == job.job_id and kind == "BUDGET" and "overdue by" in body
    _step(w, job, clock, spend, 61)
    _step(w, job, clock, spend, 61)
    assert len(spy) == 1                                                   # still overdue: nothing more
    advance(worktree_path(store, job.job_id), TL, T0 + 79 * 60)            # unit 16-04 finishes: a new estimate is shown
    _step(w, job, clock, spend, max(T0 + 80 * 60 - clock.t, 61))
    assert len(spy) == 1 and state_eta(store, job)["units_done"] == 4
    _step(w, job, clock, spend, 10 * 3600)                                 # ... and it becomes overdue again
    assert len(spy) == 2 and "overdue by" in spy[1][3]
    assert len(events(store, job, "eta_overdue")) == 2


@pytest.mark.parametrize("value", [None, "off"])
def test_no_push_unless_asked(tmp_path, monkeypatch, value):
    store, job, w, clock, spend, spy = _push_world(tmp_path, monkeypatch, value)
    w.step(job.job_id)
    end = state_eta(store, job)["shown"]["p50_end"]
    _step(w, job, clock, spend, end - clock.t + 120)
    _step(w, job, clock, spend, 10 * 3600)
    assert len(events(store, job, "eta_overdue")) >= 1 and spy == []


def test_scope_grew_and_settle_prev_handle_discovered_phases():
    from steward.eta_structure import Structure

    prev = {"units_done": 1, "phases": {"1": 3}}
    discovered = Structure(phases={"1": 3, "2": 2})
    assert eta.scope_grew(prev, discovered) is True                       # the raw G9 test sees a new phase ...
    assert eta.scope_grew(eta.settle_prev(prev, discovered), discovered) is False   # ... a discovered one is not growth
    assert eta.scope_grew(eta.settle_prev(prev, Structure(phases={"1": 5})), Structure(phases={"1": 5})) is True
    roadmap = Structure(phases={"1": 3, "2": None})                        # a TBD phase means a roadmap announces scope
    assert eta.scope_grew(eta.settle_prev(prev, roadmap), roadmap) is True
    assert eta.settle_prev(None, discovered) is None
