"""G7 detectors and the remedy catalog (phase 16-03)."""
from __future__ import annotations

import json

from efficiency_synth import T0, agent_lines, setup_job, write_subagent, write_worker
from steward import efficiency, efficiency_detect as ed

NOW = T0 + 100_000


def _sub(d, agent_id, stage, desc, t0, **kw):
    write_subagent(d, "w", agent_id, stage, desc, agent_lines(t0, prefix=agent_id, **kw))


def _detect(store, job, proj, cfg=None, **kw):
    cfg = cfg or ed.load_config()
    m = efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj, config=cfg)
    return ed.detect(m, cfg, now=NOW, **kw)


# -- config -------------------------------------------------------------------

def test_config_defaults_toml_and_job_overrides(steward_home, tmp_path, monkeypatch):
    store, job, _proj, _d = setup_job(steward_home, tmp_path)
    cfg = ed.load_config()
    assert cfg["long_lived_calls"] == 80 and cfg["envelope"]["effort_floor"] == "medium"
    assert cfg["mode"] == "shadow" and cfg["job_budget_usd"] is None and cfg["model_review"] is False
    assert cfg["stage_kinds"] == efficiency.DEFAULT_STAGE_KINDS

    cfgdir = tmp_path / "cfg"
    cfgdir.mkdir()
    (cfgdir / "steward.toml").write_text(
        "[efficiency]\nlong_lived_calls = 60\nthinking_share = \"high\"\njob_budget_usd = 25\n"
        "[efficiency.stage_kinds]\nbuild = [\"ship\"]\n[efficiency.envelope]\nmin_max_calls = 50\nplan_ahead = 3\n")
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(cfgdir))
    cfg = ed.load_config()
    assert cfg["long_lived_calls"] == 60
    assert cfg["thinking_share"] == 0.60  # wrong type ignored, lower layer kept
    assert cfg["job_budget_usd"] == 25
    assert cfg["stage_kinds"]["build"] == ("ship",) and cfg["stage_kinds"]["plan"] == ("plan",)
    assert cfg["envelope"]["min_max_calls"] == 50 and cfg["envelope"]["plan_ahead"] is True  # 3 is not a bool

    store.kv_set(job.job_id, "efficiency_overrides",
                 {"long_lived_calls": 50, "envelope.effort_floor": "high", "ctx_growth_ratio": "x", "envelope.nope": 1})
    cfg = ed.load_config(store, job.job_id)
    assert cfg["long_lived_calls"] == 50 and cfg["envelope"]["effort_floor"] == "high"
    assert cfg["ctx_growth_ratio"] == 1.5
    assert ed.load_config()["long_lived_calls"] == 60  # the job layer never leaks into the shared defaults
    assert ed.DEFAULTS["long_lived_calls"] == 80 and ed.DEFAULTS["envelope"]["effort_floor"] == "medium"


def test_thresholds_hash_changes_only_with_detector_thresholds():
    base = ed.load_config()
    h = ed.thresholds_hash(base)
    assert len(h) == 12 and h == ed.thresholds_hash(ed.load_config())
    for key, val in (("long_lived_calls", 60), ("min_saving_usd", 2.0), ("cache_ttl_s", 200)):
        assert ed.thresholds_hash({**base, key: val}) != h
    for key, val in (("mode", "nudge"), ("model_review", True), ("job_budget_usd", 9), ("recompute_every_s", 5)):
        assert ed.thresholds_hash({**base, key: val}) == h
    assert ed.thresholds_hash({**base, "envelope": {**base["envelope"], "effort_floor": "high"}}) == h


# -- long-lived agent ---------------------------------------------------------

def test_long_lived_fires_past_80_calls(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    _sub(d, "e1", "gsd-executor", "Execute phase 1", T0, calls=81, ctx0=100_000, ctx_step=0, gen_s=5, tool_s=1,
         finished=False)
    [f] = _detect(store, job, proj)
    assert f["id"] == "long_lived_agent:agent-e1" and f["kind"] == "long_lived_agent"
    assert f["evidence"] == "81 calls × 100k ctx"
    assert f["remedy"] == "respawn_per_unit" and f["actions"] == ("nudge", "reconfigure")
    assert f["policy"] == {"respawn": "per_unit"}
    assert f["metric"] == {"name": "ctx_sum", "value": 81 * 100_000}
    assert f["saving_usd"] == 0.0  # constant ctx: a fresh agent would not read less


def test_long_lived_saving_uses_first_calls_as_base(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    _sub(d, "e1", "gsd-executor", "Execute phase 1", T0, calls=100, ctx0=10_000, ctx_step=3_000, gen_s=5, tool_s=1,
         finished=False)
    m = efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj)
    a = m["agents"][0]
    [f] = ed.detect(m, ed.load_config(), now=NOW)
    expect = a["usd_input_side"] * (1 - a["ctx_first20_mean"] / a["ctx_mean"])
    assert f["saving_usd"] == expect > 0 and f["saving_min"] == 0.0


def test_long_lived_fires_on_mean_ctx_and_ctx_sum(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    _sub(d, "e1", "gsd-executor", "Execute phase 1", T0, calls=70, ctx0=300_000, gen_s=5, tool_s=1, finished=False)
    assert [f["kind"] for f in _detect(store, job, proj)] == ["long_lived_agent"]
    store2, job2, proj2, d2 = setup_job(steward_home, tmp_path / "b")
    _sub(d2, "e1", "gsd-executor", "Execute phase 1", T0, calls=70, ctx0=280_000, gen_s=5, tool_s=1, finished=False)
    assert _detect(store2, job2, proj2) == []  # mean 280k > 250k but Σ 19.6M is not > 20M


def test_long_lived_quiet_below_and_never_for_worker(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    _sub(d, "e1", "gsd-executor", "Execute phase 1", T0, calls=80, ctx0=100_000, gen_s=5, tool_s=1, finished=False)
    write_worker(d, agent_lines(T0, 200, ctx0=100_000, gen_s=5, tool_s=1, prefix="w", finished=False))
    assert _detect(store, job, proj) == []


def test_finding_shape(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    _sub(d, "e1", "gsd-executor", "Execute phase 3", T0, calls=81, ctx0=100_000, gen_s=5, tool_s=1, finished=False)
    [f] = _detect(store, job, proj)
    assert sorted(f) == sorted(["id", "kind", "subject", "stage", "unit", "evidence", "metric", "saving_usd",
                                "saving_min", "remedy", "remedy_text", "remedy_short", "actions", "policy",
                                "actionable", "epoch"])
    assert f["epoch"] is None and f["stage"] == "gsd-executor" and f["unit"] == (3,) and f["subject"] == "agent-e1"
    assert f["remedy_text"] == ed.REMEDIES["respawn_per_unit"]["text"]
    assert f["remedy_short"] == ed.REMEDIES["respawn_per_unit"]["short"]
    assert sorted(f["metric"]) == ["name", "value"]
    json.dumps(f)  # serializable as-is


# -- agent and stage detectors (hand-built metrics: the thresholds, not the parser, are under test) ---------------

SONNET = "claude-sonnet-5-5"


def _ag(key, stage="exec", kind="build", unit=1, start=0.0, end=100.0, calls=10, ctx_mean=50_000.0, finished=True,
        model=SONNET, **kw):
    a = {"key": key, "stage": stage, "kind": kind, "agent_type": stage, "description": "", "session": "w",
         "unit": unit, "unit_src": "description", "calls": calls, "ctx_mean": float(ctx_mean),
         "ctx_peak": int(ctx_mean), "ctx_sum": ctx_mean * calls, "ctx_first20_mean": float(ctx_mean),
         "out": 1000, "vis_chars": 0, "think_est": 0.0, "think_share": 0.0, "usd": 1.0,
         "usd_input_side": 1.0, "model": model, "effort": "high", "start": T0 + start, "end": T0 + end,
         "finished": finished, "gen_s": 10.0, "tool_s": 5.0, "tok_s": 50.0, "max_call_gap_s": 5.0,
         "open_tool": False, "cache_miss": 0.0, "cache_read": 0, "cache_create": 0}
    a.update(kw)
    return a


def _m(agents=(), **kw):
    m = {"agents": list(agents), "stages": {}, "units": [], "supervision": {"usd": 0.0, "share_of_worker": 0.0},
         "time": {"last_busy": 0.0, "open_tool": False}, "totals": {"usd": 0.0}, "projection": None,
         "artifacts": [], "big_results": {}}
    m.update(kw)
    return m


def _kinds(m, cfg=None):
    return sorted(f["kind"] for f in ed.detect(m, cfg or ed.load_config(), now=NOW))


def test_context_growth_over_1_5x_median_of_earlier_units():
    def mk(last):
        return _m([_ag("e1", unit=1, start=0, end=90, ctx_mean=190_000, calls=6, usd_input_side=2.0),
                   _ag("e2", unit=2, start=100, end=190, ctx_mean=212_000, calls=6, usd_input_side=2.0),
                   _ag("e3", unit=3, start=200, end=290, ctx_mean=last, calls=6, usd_input_side=3.0)])
    [f] = [f for f in ed.detect(mk(317_000), ed.load_config(), now=NOW) if f["kind"] == "context_growth"]
    assert f["evidence"] == "ctx/call 317k = 1.6× earlier units" and f["remedy"] == "trim_spawn_reads"
    assert f["subject"] == "e3" and f["metric"] == {"name": "ctx_mean", "value": 317_000.0}
    median = (190_000 + 212_000) / 2
    assert abs(f["saving_usd"] - 3.0 * (1 - median / 317_000)) < 1e-9
    assert "context_growth" not in _kinds(mk(300_000))  # 1.49x
    short = _m([_ag("e1", unit=1, ctx_mean=100_000, calls=6), _ag("e2", unit=2, start=200, end=290,
                                                                       ctx_mean=400_000, calls=4)])
    assert "context_growth" not in _kinds(short)  # < 5 calls is ignored


def _stage(out, share, effort, agents=1, tok_s=50.0, kind="build"):
    return {"kind": kind, "agents": agents, "calls": 10, "usd": 5.0, "out": out, "think_est": out * share,
            "think_share": share, "wall_s": 100.0, "gen_s": 10.0, "tool_s": 5.0, "tok_s": tok_s, "effort": effort,
            "cache_miss": 0.0, "ctx_sum": 1, "ctx_peak": 1}


def test_hidden_thinking_stage_share_and_output():
    ex = _ag("p1", stage="planner", kind="plan", model="claude-opus-5-5")
    m = _m([ex], stages={"planner": _stage(163_000, 0.80, "xhigh", agents=2, tok_s=40.0, kind="plan")})
    [f] = ed.detect(m, ed.load_config(), now=NOW)
    assert f["kind"] == "hidden_thinking" and f["remedy"] == "effort_step_down"
    assert f["actions"] == ("nudge", "reconfigure") and f["policy"] == {"effort": {"planner": "high"}}
    think_per = 163_000 * 0.80 / 2
    price_out = ed.tokens.price_usd("claude-opus-5-5", [0, 1_000_000, 0, 0])
    assert abs(f["saving_usd"] - 0.5 * think_per * price_out / 1e6) < 1e-9
    assert abs(f["saving_min"] - 0.5 * think_per / 40.0 / 60) < 1e-9
    assert f["metric"]["name"] == "think_share" and "80%" in f["evidence"] and "163k" in f["evidence"]
    assert _kinds(_m([ex], stages={"planner": _stage(140_000, 0.80, "xhigh", kind="plan")})) == []
    assert _kinds(_m([ex], stages={"planner": _stage(163_000, 0.58, "xhigh", kind="plan")})) == []
    assert _kinds(_m([_ag("w", stage="worker", kind="worker")],
                     stages={"worker": _stage(300_000, 0.9, "high", kind="worker")})) == []


def test_effort_step_down_respects_floor_and_allowed_values():
    def find(effort, cfg=None):
        m = _m([_ag("e1")], stages={"exec": _stage(200_000, 0.7, effort)})
        return [f for f in ed.detect(m, cfg or ed.load_config(), now=NOW) if f["kind"] == "hidden_thinking"][0]
    f = find("high")
    assert f["policy"] == {"effort": {"exec": "medium"}} and "reconfigure" in f["actions"]
    for effort in ("medium", "max", "low"):
        f = find(effort)
        assert f["actions"] == ("escalate",) and f["policy"] is None, effort
    high_floor = {**ed.load_config(), "envelope": {**ed.DEFAULTS["envelope"], "effort_floor": "high"}}
    f = find("high", high_floor)
    assert f["actions"] == ("escalate",) and f["policy"] is None


def _plan_build(p3_start, b2_end=290.0, b2_finished=True):
    return _m([
        _ag("b1", stage="exec", kind="build", unit=1, start=0, end=100),
        _ag("p2", stage="planner", kind="plan", unit=2, start=100.1, end=160),
        _ag("b2", stage="exec", kind="build", unit=2, start=170, end=b2_end, finished=b2_finished),
        _ag("p3", stage="planner", kind="plan", unit=3, start=p3_start, end=p3_start + 80),
    ])


def test_planning_on_critical_path_two_units_in_a_row():
    [f] = ed.detect(_plan_build(b2_end=290, p3_start=290.1), ed.load_config(), now=NOW)
    assert f["kind"] == "planning_serial" and f["subject"] == "planner"
    assert f["evidence"] == "plan started after build ended, units 2,3" and f["remedy"] == "plan_ahead"
    assert abs(f["saving_min"] - ((160 - 100.1) + 80) / 2 / 60) < 1e-9
    assert f["policy"] == {"plan_ahead": "on"} and f["metric"]["name"] == "serial_plan_s"


def test_planning_overlap_resets_and_tolerance():
    # planner 3 starts 6 s before build 2's last line: inside the 60 s tolerance, still serial
    assert _kinds(_plan_build(p3_start=284.0)) == ["planning_serial"]
    # 10 minutes of overlap: not serial, the run resets to one unit
    assert _kinds(_plan_build(p3_start=290.0 - 600, b2_end=290.0)) == []
    # build 2 unfinished when planner 3 starts: not serial
    assert _kinds(_plan_build(p3_start=300.0, b2_finished=False)) == []


def test_cache_misses_share_and_ttl_gap():
    def mk(calls, miss=0.35, gap=5.0, cc=350_000, cr=650_000):
        return _m([_ag("e1", calls=calls, cache_miss=miss, max_call_gap_s=gap, cache_create=cc, cache_read=cr)])
    [f] = ed.detect(mk(20), ed.load_config(), now=NOW)
    assert f["kind"] == "cache_misses" and f["remedy"] == "warm_cache" and f["actions"] == ("nudge",)
    assert f["metric"]["name"] == "cache_miss_share"
    per_m = ed.tokens.price_usd(SONNET, [0, 0, 0, 1_000_000]) - ed.tokens.price_usd(SONNET, [0, 0, 1_000_000, 0])
    assert abs(f["saving_usd"] - (350_000 - 0.10 * 1_000_000) * per_m / 1e6) < 1e-9
    assert _kinds(mk(19)) == []
    [g] = ed.detect(mk(10, miss=0.0, gap=400.0, cc=0, cr=0), ed.load_config(), now=NOW)
    assert g["metric"] == {"name": "max_call_gap_s", "value": 400.0} and g["saving_usd"] == 0.0
    assert not g["actionable"]
    assert _kinds(mk(10, miss=0.0, gap=300.0, cc=0, cr=0)) == []
    w = _m([_ag("w", stage="worker", kind="worker", unit=None, calls=10, max_call_gap_s=900.0)])
    assert _kinds(w) == []  # a worker waiting on helpers is not a cache finding


def _art(chars, readers=("r1",)):
    return {"writer": "p1", "stage": "planner", "path_key": "abc123", "chars": chars, "ts": T0,
            "whole_readers": list(readers)}


def test_artifact_bloat_write_then_whole_read():
    rd = _ag("r1", stage="exec", calls=30)
    [f] = ed.detect(_m([rd], artifacts=[_art(45_000)]), ed.load_config(), now=NOW)
    assert f["kind"] == "artifact_bloat" and f["remedy"] == "smaller_artifact" and f["actions"] == ("nudge",)
    assert f["policy"] is None and f["metric"] == {"name": "artifact_chars", "value": 45_000}
    p_cr = ed.tokens.price_usd(SONNET, [0, 0, 1_000_000, 0])
    assert abs(f["saving_usd"] - (45_000 / 4 - 2000) * 30 * p_cr / 1e6) < 1e-9
    assert _kinds(_m([rd], artifacts=[_art(39_000)])) == []
    assert _kinds(_m([rd], artifacts=[_art(45_000, readers=())])) == []  # offset/limit reads are not "whole"


def test_build_output_bloat_three_big_results_same_command():
    kinds = lambda big: _kinds(_m(big_results=big))  # noqa: E731
    [f] = ed.detect(_m(big_results={"cmd1": [25_000, 30_000, 22_000]}), ed.load_config(), now=NOW)
    assert f["kind"] == "build_output_bloat" and f["remedy"] == "quiet_output" and f["subject"] == "cmd1"
    assert f["metric"] == {"name": "big_results", "value": 3} and f["policy"] == {"output_hygiene": "on"}
    assert kinds({"cmd1": [25_000, 30_000]}) == []
    assert kinds({"a": [25_000], "b": [25_000], "c": [25_000]}) == []
    assert kinds({"cmd1": [25_000, 30_000, 20_000]}) == []  # 20,000 is not > 20,000


def test_cache_misses_end_to_end_from_transcripts(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    _sub(d, "e1", "gsd-executor", "Execute phase 1", T0, calls=20, ctx0=1000, cc=350, gen_s=5, tool_s=1)
    m = efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj)
    a = m["agents"][0]
    assert (a["cache_create"], a["cache_read"]) == (20 * 350, 20 * 640)
    [f] = [f for f in ed.detect(m, ed.load_config(), now=NOW) if f["kind"] != "idle_wait"]
    assert f["kind"] == "cache_misses" and f["saving_usd"] > 0


# -- job-level detectors, floor, determinism ----------------------------------

class _Ev:
    def __init__(self, seq, ts, what, **evidence):
        self.seq, self.ts, self.what, self.evidence = seq, ts, what, evidence


def _iso(t):
    from efficiency_synth import iso
    return iso(T0 + t)


def test_idle_wait_escalate_only_and_open_tool_not_idle():
    def go(gap, state="RUNNING", open_tool=False):
        m = _m(time={"last_busy": NOW - gap, "open_tool": open_tool})
        return ed.detect(m, ed.load_config(), now=NOW, job_state=state)
    [f] = go(601)
    assert f["kind"] == "idle_wait" and f["actions"] == ("escalate",) and f["remedy"] == "escalate_idle"
    assert f["saving_min"] == 601 / 60 and f["policy"] is None and f["metric"] == {"name": "idle_s", "value": 601}
    assert go(601, open_tool=True) == [] and go(599) == [] and go(601, state="PAUSED") == []


def test_cost_per_unit_trend():
    def units(*usd):
        return [{"unit": i + 1, "usd": u, "wall_s": 60.0, "complete": True} for i, u in enumerate(usd)]
    [f] = ed.detect(_m(units=units(8.0, 13.1, 17.8)), ed.load_config(), now=NOW)
    assert f["kind"] == "cost_per_unit_trend" and f["remedy"] == "escalate_projection"
    assert f["actions"] == ("escalate",) and f["unit"] == 3 and "1.7×" in f["evidence"]
    assert abs(f["saving_usd"] - (17.8 - 10.55)) < 1e-9
    assert ed.detect(_m(units=units(8.0, 17.8)), ed.load_config(), now=NOW) == []  # one prior unit only
    assert ed.detect(_m(units=units(8.0, 13.1, 14.0)), ed.load_config(), now=NOW) == []


def test_projection_over_budget():
    proj = {"units_total": 8, "units_complete": 2, "remaining": 6, "usd_per_unit": 10.0, "min_per_unit": 5.0,
            "projected_usd": 60.0, "projected_min": 30.0}
    m = _m(projection=proj, totals={"usd": 25.0})
    [f] = ed.detect(m, ed.load_config(), now=NOW, budget_usd=80.0)
    assert f["evidence"] == "projection $85.00 > budget $80.00" and f["subject"] == "budget"
    assert f["actions"] == ("escalate",) and abs(f["saving_usd"] - 5.0) < 1e-9
    assert ed.detect(m, ed.load_config(), now=NOW, budget_usd=85.0) == []
    assert ed.detect(m, ed.load_config(), now=NOW, budget_usd=None) == []


def _verdicts(statuses, dup_at=0.0, step=100.0, name="pace"):
    evs = [_Ev(1, _iso(dup_at), "jev_flag")]
    for i, st in enumerate(statuses):
        evs.append(_Ev(10 + i, _iso(dup_at + 50 + i * step), "watcher_verdict", name=name, status=st, cost_usd=0.5))
    return evs


def test_redundant_supervision_duplicates():
    cfg = ed.load_config()
    [f] = ed.detect(_m(), cfg, events=_verdicts(["concern"] * 3), now=NOW)
    assert f["kind"] == "redundant_supervision" and f["subject"] == "pace"
    assert f["remedy"] == "escalate_remove_watcher" and f["actions"] == ("escalate",)
    assert "Sam" in f["remedy_text"] and abs(f["saving_usd"] - 1.5) < 1e-9
    assert ed.detect(_m(), cfg, events=_verdicts(["concern", "ok", "concern"]), now=NOW) == []
    assert ed.detect(_m(), cfg, events=_verdicts(["concern"] * 2), now=NOW) == []
    # a verdict more than 600 s after the last duplicate event is not a duplicate
    assert ed.detect(_m(), cfg, events=_verdicts(["concern"] * 3, step=400.0), now=NOW) == []
    assert ed.detect(_m(), cfg, events=[_Ev(2, _iso(10), "watcher_verdict", name="pace", status="concern")] * 3,
                     now=NOW) == []  # no duplicate event at all


def test_redundant_supervision_share():
    def go(share):
        m = _m(supervision={"usd": 6.0, "share_of_worker": share})
        return ed.detect(m, ed.load_config(), now=NOW)
    [f] = go(0.26)
    assert f["subject"] == "supervision" and f["remedy"] == "escalate_remove_watcher"
    assert abs(f["saving_usd"] - 6.0 * (1 - 0.25 / 0.26)) < 1e-9
    assert go(0.25) == []


def test_saving_floor_marks_not_actionable():
    cfg = ed.load_config()

    def usd_case(saving):
        a = _ag("e1", calls=100, ctx_mean=100_000, ctx_first20_mean=50_000, usd_input_side=saving * 2, finished=False)
        [f] = ed.detect(_m([a]), cfg, now=NOW)
        return f
    assert abs(usd_case(0.80)["saving_usd"] - 0.80) < 1e-9 and usd_case(0.80)["actionable"] is False
    assert usd_case(1.00)["actionable"] is True
    mk = lambda usd, mins: ed._finding(cfg, "idle_wait", "job", "e", ("m", 1), "escalate_idle",  # noqa: E731
                                       saving_usd=usd, saving_min=mins)
    assert mk(0.80, 1.5)["actionable"] is False
    assert mk(0.80, 2.0)["actionable"] is True and mk(1.0, 0.0)["actionable"] is True


def test_supersession_one_per_stage_kind():
    e3 = _ag("e3", unit=3, start=0, end=100, calls=90, finished=True)
    e4 = _ag("e4", unit=4, start=200, end=300, calls=131, finished=False)
    found = ed.detect(_m([e3, e4]), ed.load_config(), now=NOW)
    assert [f["subject"] for f in found] == ["e4"]
    # a finished agent that is the latest of its stage is still reported
    assert [f["subject"] for f in ed.detect(_m([e3]), ed.load_config(), now=NOW)] == ["e3"]
    # two different stages each keep one
    p = _ag("p1", stage="planner", kind="plan", calls=90, finished=False)
    assert len(ed.detect(_m([e3, e4, p]), ed.load_config(), now=NOW)) == 2


def _everything():
    cfg = ed.load_config()
    agents = [
        _ag("b1", stage="exec", unit=1, start=0, end=100),
        _ag("x1", stage="helper", kind="other", unit=1, start=0, end=100, calls=100, ctx_mean=300_000, ctx_first20_mean=100_000,
            usd_input_side=9.0, cache_miss=0.4, cache_read=600_000, cache_create=400_000, finished=False),
        _ag("p2", stage="planner", kind="plan", unit=2, start=100.1, end=160),
        _ag("p3", stage="planner", kind="plan", unit=3, start=300.0, end=380),
        _ag("b2", stage="exec", unit=2, start=170, end=290),
        _ag("r1", stage="review", kind="verify", unit=1, calls=10),
    ]
    m = _m(agents, stages={"exec": _stage(200_000, 0.7, "high")},
           artifacts=[_art(50_000, ("r1",))], big_results={"cmd": [30_000] * 3},
           time={"last_busy": NOW - 900, "open_tool": False},
           units=[{"unit": i + 1, "usd": u, "wall_s": 1.0, "complete": True} for i, u in enumerate((8.0, 13.1, 17.8))],
           supervision={"usd": 9.0, "share_of_worker": 0.4})
    return cfg, m


def test_detect_is_deterministic():
    cfg, m = _everything()
    events = _verdicts(["concern"] * 3)
    a = json.dumps(ed.detect(m, cfg, events=events, now=NOW), sort_keys=True)
    b = json.dumps(ed.detect(json.loads(json.dumps(m)), cfg, events=list(events), now=NOW), sort_keys=True)
    assert a == b
    found = ed.detect(m, cfg, events=events, now=NOW)
    keys = [(-f["saving_usd"], -f["saving_min"], f["id"]) for f in found]
    assert keys == sorted(keys)


def test_every_detector_maps_to_catalog():
    cfg, m = _everything()
    found = ed.detect(m, cfg, events=_verdicts(["concern"] * 3), now=NOW)
    kinds = {f["kind"] for f in found}
    assert kinds >= {"long_lived_agent", "hidden_thinking", "planning_serial", "artifact_bloat", "build_output_bloat",
                     "cache_misses", "idle_wait", "cost_per_unit_trend", "redundant_supervision"}
    for f in found:
        assert f["remedy"] in ed.REMEDIES and f["actions"] and set(f["actions"]) <= {"nudge", "reconfigure", "escalate"}
    for rid, r in ed.REMEDIES.items():
        for text in (r["text"], r["short"]):
            assert "gsd" not in text.lower() and ".planning" not in text
    for f in found:
        assert "gsd" not in f["remedy_text"].lower()
    assert {f["kind"] for f in found if f["actions"] == ("escalate",)} >= {
        "idle_wait", "cost_per_unit_trend", "redundant_supervision"}
    assert all(f["actions"] == ("escalate",) for f in found if f["kind"] in (
        "idle_wait", "cost_per_unit_trend", "redundant_supervision"))


# -- 18-01 G1: plan-level unit ids (tuple keys) ---------------------------------------------------------

def test_planning_serial_with_plan_ids():
    builds = [_ag(f"b{m}", stage="exec", kind="build", unit=(16, m), start=100 * m, end=100 * m + 90) for m in range(1, 8)]
    p17 = _ag("p17", stage="planner", kind="plan", unit=(17,), start=800, end=860)
    b17 = [_ag(f"c{m}", stage="exec", kind="build", unit=(17, m), start=900 + 100 * m, end=990 + 100 * m) for m in (1, 2)]
    p18 = _ag("p18", stage="planner", kind="plan", unit=(18,), start=1200, end=1260)
    [f] = ed.detect(_m(builds + [p17] + b17 + [p18]), ed.load_config(), now=NOW)
    assert f["kind"] == "planning_serial" and f["evidence"] == "plan started after build ended, units 17,18"
    assert f["unit"] == (18,)
    # a planner of the same phase as the builds is not after them: a single-phase job never fires
    p16 = _ag("p16", stage="planner", kind="plan", unit=(16,), start=5, end=60)
    p16b = _ag("p16b", stage="planner", kind="plan", unit=(16,), start=1000, end=1060)
    assert _kinds(_m(builds + [p16, p16b])) == []


def test_planning_serial_mixed_int_units_still_work():
    [f] = ed.detect(_plan_build(b2_end=290, p3_start=290.1), ed.load_config(), now=NOW)
    assert f["evidence"] == "plan started after build ended, units 2,3"


def test_cost_per_unit_labels():
    rows = [{"unit": (16, m), "unit_label": f"16-0{m}", "usd": u, "wall_s": 60.0, "complete": True}
            for m, u in ((1, 8.0), (2, 13.1), (3, 17.8))]
    [f] = ed.detect(_m(units=rows), ed.load_config(), now=NOW)
    assert f["subject"] == "unit-16-03" and f["evidence"].startswith("unit 16-03 $")
    assert f["unit"] == (16, 3)
