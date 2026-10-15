"""18-01 G1: the unit id is a sortable tuple key printed as GSD prints it, and a7d292d8 shows units 7/7."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import efficiency_replay as er  # noqa: E402
from efficiency_synth import T0, agent_lines, setup_job, write_subagent, write_worker  # noqa: E402
from steward import efficiency, efficiency_watch  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "efficiency"
NOW = T0 + 100_000
_CACHE: dict = {}


def test_parse_unit_nn_mm_first():
    p = efficiency.parse_unit
    assert p("Execute plan 16-01") == (16, 1)
    assert p("Execute plan 16-07") == (16, 7)
    assert p("Execute plan 02.1-03") == (2.1, 3)
    assert p("Plan phase 16") == (16,)
    assert p("Verify phase 16") == (16,)
    assert p("Execute phase 7 plans") == (7,)
    assert p("Verify phase 2.1") == (2.1,)
    assert p("Classify plan") is None
    assert p("Execute plans 16-01 to 16-03") == (16, 1)
    assert p("Execute plan 16-1") == (16, 1)
    assert p("") is None and p(None) is None


def test_unit_key_normalises_every_form():
    k = efficiency.unit_key
    assert k(3) == (3,) and k(3.0) == (3,) and k(2.1) == (2.1,)
    assert k((16, 1)) == (16, 1) and k([16, 1]) == (16, 1)
    assert k("16-01") == (16, 1) and k("02.1-03") == (2.1, 3) and k("16") == (16,)
    assert k(None) is None
    order = [1, (1, 1), (1, 2), 2, (16,), (16, 1), (16, 10)]
    keys = [k(u) for u in order]
    assert keys == sorted(keys)
    assert k((16, 10)) > k((16, 9))  # plan 10 sorts after plan 9 (a float key breaks this)


def test_unit_label_prints_gsd_ids():
    lab = efficiency.unit_label
    assert lab((16, 1)) == "16-01" and lab((2.1, 3)) == "02.1-03"
    assert lab((16,)) == "16" and lab((2.1,)) == "2.1" and lab(3) == "3"
    assert lab([16, 1]) == "16-01" and lab(None) == ""
    assert lab("16-01") == "16-01"


def _synth_phase(steward_home, tmp_path, *, third: str | None = None):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    write_worker(d, agent_lines(T0, 1, prefix="w"))
    write_subagent(d, "w", "p", "gsd-planner", "Plan phase 16", agent_lines(T0 + 10, 3, prefix="p"))
    write_subagent(d, "w", "c", "gsd-plan-checker", "Check Phase 16 plans", agent_lines(T0 + 100, 2, prefix="c"))
    write_subagent(d, "w", "e1", "gsd-executor", "Execute plan 16-01", agent_lines(T0 + 200, 3, prefix="e1"))
    write_subagent(d, "w", "e2", "gsd-executor", "Execute plan 16-02", agent_lines(T0 + 300, 3, prefix="e2"))
    if third:
        write_subagent(d, "w", "e3", "gsd-executor", "Execute plan 16-03",
                       agent_lines(T0 + 400, 3, prefix="e3", finished=False))
    return efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj)


def test_phase_group_rolls_up_when_plans_are_units(steward_home, tmp_path):
    m = _synth_phase(steward_home, tmp_path)
    assert [u["unit_label"] for u in m["units"]] == ["16-01", "16-02"]
    assert [u["unit"] for u in m["units"]] == [(16, 1), (16, 2)]
    assert all(u["complete"] for u in m["units"])
    m = _synth_phase(steward_home, tmp_path / "three", third="16-03")
    assert [u["unit_label"] for u in m["units"]] == ["16-01", "16-02", "16-03"]
    assert [u["complete"] for u in m["units"]] == [True, True, False]


def test_phase_units_unchanged_without_plan_groups(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    write_worker(d, agent_lines(T0, 1, prefix="w"))
    write_subagent(d, "w", "b3", "gsd-executor", "Execute phase 3 plans", agent_lines(T0 + 20, 3, prefix="b3"))
    write_subagent(d, "w", "p4", "gsd-planner", "Plan phase 4", agent_lines(T0 + 100, 2, prefix="p4"))
    rows = efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj)["units"]
    assert [(u["unit"], u["unit_label"], u["complete"]) for u in rows] == [((3,), "3", True), ((4,), "4", False)]


def _replay(name: str, job_id: str, factory):
    if name not in _CACHE:
        _CACHE[name] = er.run_replay(er.from_fixture(FIXTURES / f"{name}.json.gz"), factory.mktemp(f"units{name}"),
                                     job_id=job_id)
    return _CACHE[name]


def _assert_units(res, labels):
    rep = efficiency_watch.report(res.store, res.job_id, projects_dir=res.proj, now=res.end)
    rows = rep["metrics"]["units"]
    assert [u["unit_label"] for u in rows] == labels
    assert all(u["complete"] for u in rows)
    text = efficiency_watch.render_table(rep)
    assert f"units: {len(labels)}/{len(labels)} complete" in text
    return rep


def test_a7d292d8_replay_units_7_of_7(tmp_path_factory):
    res = _replay("a7d292d8", "a7d292d8", tmp_path_factory)
    _assert_units(res, [f"16-0{n}" for n in range(1, 8)])


# -- 18-01 task 2: every comparison and label goes through unit_key / unit_label -------------------------------

def _agent(key, kind, unit, start, end, finished=True, stage=None):
    return {"key": key, "kind": kind, "stage": stage or ("gsd-planner" if kind == "plan" else "gsd-executor"),
            "unit": unit, "start": start, "end": end, "finished": finished}


def test_serial_s_uses_unit_key():
    agents = [_agent("b", "build", (16, 7), 0, 100), _agent("p17", "plan", (17,), 120, 180),
              _agent("p16", "plan", (16,), 120, 180)]
    cfg = {"plan_serial_tolerance_s": 60}
    assert efficiency_watch._serial_s(agents[1], agents, cfg) == 60
    assert efficiency_watch._serial_s(agents[2], agents, cfg) == 0.0


def test_finding_label_prints_labels():
    f = efficiency_watch.finding_label
    assert f({"stage": "gsd-executor", "unit": (16, 1)}) == "gsd-executor#16-01"
    assert f({"stage": "gsd-executor", "unit": [16, 1]}) == "gsd-executor#16-01"
    assert f({"stage": "gsd-executor", "unit": 3}) == "gsd-executor#3"


def _table_rep(unit):
    agent = {"key": "agent-a", "stage": "gsd-executor", "kind": "build", "unit": unit, "calls": 3, "ctx_mean": 1000.0,
             "ctx_peak": 2000, "usd": 1.0, "start": 0.0, "end": 60.0, "finished": True}
    metrics = {"agents": [agent], "stages": {}, "units": [], "projection": None,
               "totals": {"usd": 1.0, "sub_share": 0.0, "calls": 3, "peak_ctx": 2000, "think_share": 0.0, "tok_s": 0.0},
               "supervision": {"usd": 0.0, "share_of_worker": 0.0, "by_source": {}}, "time": {}}
    return {"mode": "shadow", "metrics": metrics, "findings": [], "state": {}, "baselines": {}, "effects": []}


def test_render_table_unit_column_prints_labels():
    row = [ln for ln in efficiency_watch.render_table(_table_rep((16, 4))).splitlines() if "agent-a" in ln][0]
    assert "16-04" in row
    row = [ln for ln in efficiency_watch.render_table(_table_rep(None)).splitlines() if "agent-a" in ln][0]
    assert "16-04" not in row and "None" not in row


class _SpyReview:
    def __init__(self):
        self.started = []

    def maybe_start(self, job_id, metrics, found, boundary, now, *, mode="shadow"):
        self.started.append(boundary)
        return "started"


def test_review_boundary_survives_json_round_trip(steward_home, tmp_path):
    store, job, _proj, _d = setup_job(steward_home, tmp_path)
    spy = _SpyReview()
    w = efficiency_watch.EfficiencyWatcher(store, projects_dir=tmp_path, review=spy)
    rows = [{"unit": (16, m), "unit_label": f"16-0{m}", "usd": 1.0, "wall_s": 1.0, "complete": True} for m in (3, 1)]
    metrics = {"units": rows}
    w._review_boundary(job.job_id, metrics, [], "shadow", NOW)
    assert spy.started == ["16-03"]
    assert store.kv_get(job.job_id, efficiency_watch.UNITS_COMPLETE_KEY) == ["16-01", "16-03"]  # labels, key order
    # the same complete units again, with the unit as a JSON list (what a stored row looks like): no new boundary
    again = {"units": [{**r, "unit": list(r["unit"])} for r in rows]}
    w._review_boundary(job.job_id, again, [], "shadow", NOW + 1)
    assert spy.started == ["16-03"]
    # old kv that held numbers: at most one extra boundary, then stable
    store.kv_set(job.job_id, efficiency_watch.UNITS_COMPLETE_KEY, [16])
    w._review_boundary(job.job_id, metrics, [], "shadow", NOW + 2)
    w._review_boundary(job.job_id, metrics, [], "shadow", NOW + 3)
    assert len(spy.started) == 2


def test_review_prompt_and_kv_use_labels(steward_home, tmp_path):
    from steward import efficiency_review as er_

    store, job, _proj, _d = setup_job(steward_home, tmp_path)
    metrics = {"totals": {}, "units": [{"unit": (16, 3), "unit_label": "16-03", "usd": 1.0, "wall_s": 2.0,
                                        "complete": True}]}
    text = er_.build_prompt(store, job.job_id, metrics, [], "16-03")
    assert "A work unit just completed: 16-03." in text and "- 16-03 $1.00" in text
    assert "A work unit just completed: 16-03." in er_.build_prompt(store, job.job_id, metrics, [], (16, 3))


def test_ed11ba3c_replay_units_7_of_7(tmp_path_factory):
    res = _replay("ed11ba3c", "ed11ba3c", tmp_path_factory)
    rep = _assert_units(res, [f"17-0{n}" for n in range(1, 8)])
    assert "17" not in [u["unit_label"] for u in rep["metrics"]["units"]]  # `Verify phase 17` rolls up: no row 17


def test_06ea6a04_units_are_the_eight_phases(tmp_path_factory):
    res = _replay("06ea6a04", "06ea6a04", tmp_path_factory)
    _assert_units(res, [str(n) for n in range(1, 9)])
