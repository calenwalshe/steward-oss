"""16-07 G15 acceptance gate: a deterministic replay of job 06ea6a04 from the trimmed content-free fixture.

The replay drives the real EfficiencyWatcher.step, tokens, efficiency.compute and efficiency_detect against a real
Store (see tests/efficiency_replay.py). Assertions come from 16-CONTEXT G15 and G7 verbatim.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import efficiency_replay as er  # noqa: E402
from steward import efficiency, tokens  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "efficiency" / "06ea6a04.json.gz"
_RUNS: list = []  # fresh 06ea replays, computed lazily and kept for the session


def _runs(n: int, factory) -> list:
    """At least `n` independent replays (each in its own tmp dir). Called from inside a test so the conftest
    env isolation is active."""
    while len(_RUNS) < n:
        _RUNS.append(er.run_replay(er.from_fixture(FIXTURE), factory.mktemp(f"replay{len(_RUNS)}")))
    return _RUNS


@pytest.fixture(scope="module")
def expected():
    return er.load_expected(FIXTURE)


def _unit_file(res, agent_type_part: str, unit: int):
    """(rel, info) of the fixture subagent whose stage kind is `agent_type_part` and unit number `unit`."""
    for rel, info in res.files.items():
        meta = info["meta"]
        if info["kind"] != "subagent":
            continue
        if (efficiency.stage_kind(meta["agentType"], meta["description"]) == agent_type_part
                and efficiency.unit_key(efficiency.parse_unit(meta["description"])) == efficiency.unit_key(unit)):
            return rel, info
    raise AssertionError(f"no {agent_type_part} agent for unit {unit}")


def _first_event(res, what: str, kind: str):
    return next(ev for w, ev in res.events if w == what and ev.get("kind") == kind)


def test_gate_accounting_within_2pct(tmp_path_factory, expected):
    res = _runs(1, tmp_path_factory)[0]
    data = tokens.job_tokens(res.store, res.job_id, now=res.end, projects_dir=res.proj)
    total = data["total"]["usd"]
    assert total == pytest.approx(expected["usd_total"], rel=0.02)
    sub = tokens.worker_usd(data) - data["sources"]["worker"]["usd"]
    assert sub == pytest.approx(expected["usd_sub"], rel=0.02)
    types = {info["meta"]["agentType"] for info in res.files.values() if info["kind"] == "subagent"}
    assert {s for s in data["sources"] if s.startswith("sub:")} == {f"sub:{t}" for t in types}
    # the watcher's own agent metrics agree with the independent recompute too
    assert res.state["summary"]["usd"] == pytest.approx(expected["usd_worker"] + expected["usd_sub"], rel=0.02)
    print(f"accounting: total ${total:.2f} vs expected ${expected['usd_total']:.2f}; "
          f"sub ${sub:.2f} vs ${expected['usd_sub']:.2f}")


def test_gate_long_lived_executor_before_call_90(tmp_path_factory):
    res = _runs(1, tmp_path_factory)[0]
    rel, info = _unit_file(res, "build", 4)
    stem = Path(rel).stem
    ev = res.first_seen[f"long_lived_agent:{stem}"]
    assert ev["calls"] < 90
    seen = {mid for mid, t in info["ids"] if t <= ev["at"]}
    assert len(seen) < 90
    assert len({mid for mid, _t in info["ids"]}) > 90  # exec 4 really did go past call 90 later
    print(f"long-lived exec 4 ({stem[:14]}): raised at {ev['calls']} calls")


def test_gate_planning_on_critical_path_by_unit_2(tmp_path_factory):
    res = _runs(1, tmp_path_factory)[0]
    f = _first_event(res, "efficiency_finding", "planning_serial")
    _, plan3 = _unit_file(res, "plan", 3)
    _, build3 = _unit_file(res, "build", 3)
    assert f["at"] >= plan3["first_t"]
    assert f["at"] < build3["first_t"]
    assert "units 2,3" in f["evidence"]


def test_gate_hidden_thinking_on_planners(tmp_path_factory):
    res = _runs(1, tmp_path_factory)[0]
    planner = next(i["meta"]["agentType"] for i in res.files.values()
                   if i["kind"] == "subagent" and efficiency.stage_kind(i["meta"]["agentType"], "") == "plan")
    f = _first_event(res, "efficiency_finding", "hidden_thinking")
    assert f["stage"] == planner
    assert f["remedy"] == "effort_step_down"
    assert f["policy"] == {"effort": {planner: "high"}}  # observed xhigh
    assert "xhigh" in f["evidence"]


def test_gate_zero_findings_on_steady_job(tmp_path):
    res = er.run_replay(er.steady_job_files(), tmp_path)
    assert [w for w, _ in res.events if w == "efficiency_finding"] == []
    assert res.state["active"] == []
    assert [w for w, _ in res.events if w == "spend_rate_high"] == []
    assert res.state["summary"]["calls"] > 100  # it really was a job
    assert res.subprocess_calls == []


def test_gate_three_runs_identical(tmp_path_factory):
    runs = _runs(3, tmp_path_factory)
    digests = [r.digest() for r in runs[:3]]
    assert digests[0] == digests[1] == digests[2]
    assert len(digests[0]) > 1000
    print("digest length", len(digests[0]), "runtimes", [round(r.wall_s, 1) for r in runs[:3]])


def test_gate_no_subprocess_no_nudge_at_t0(tmp_path_factory):
    runs = _runs(1, tmp_path_factory)
    for res in runs:
        assert res.subprocess_calls == []  # no herdr, no claude: no steward-test-* session can exist
        mine = [i for i in res.instructions if i.get("origin") == "watcher:efficiency"]
        assert mine == []
        assert [w for w, _ in res.events if w == "efficiency_nudged"] == []
        assert [w for w, _ in res.events if w == "efficiency_would_nudge"], "T0 records what it would have said"


def test_gate_runtime_budget(tmp_path_factory):
    runs = _runs(1, tmp_path_factory)
    for res in runs:
        print(f"replay: {res.ticks} ticks in {res.wall_s:.1f}s")
        assert res.wall_s < 60
