"""The ETA estimator ladder (phase 18-03, G5-G9): oracle numbers computed by hand from the plan's formulas."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from eta_fixtures import load_timeline, replay_events, setup_job  # noqa: E402
from steward import eta, eta_prior, eta_structure  # noqa: E402
from steward.efficiency import unit_label  # noqa: E402
from steward.eta_structure import Structure  # noqa: E402
from steward.runner_exec import SubprocessRunner  # noqa: E402
from steward.store import Store  # noqa: E402

T0 = 1_790_000_000.0
PRIOR = eta_prior.prior_for(eta_prior.load("/nonexistent-eta-home"), "gsd-build")   # the seed
TYPES = {"plan": "gsd-planner", "build": "gsd-executor", "verify": "gsd-verifier"}


def E(minutes: float) -> float:
    return T0 + minutes * 60.0


def agent(kind, unit, start, end=None, *, out=0, typ=None):
    return {"kind": kind, "agent_type": typ or TYPES[kind], "unit": unit, "start": E(start),
            "end": E(end) if end is not None else 0.0, "finished": end is not None, "out": out}


def labels(n, phase=16):
    return [unit_label((phase, i)) for i in range(1, n + 1)]


def single(done_at=(), total=7, *, phase=16, **kw) -> Structure:
    """A single-phase job: `total` plans, the first len(done_at) finished at those minutes."""
    lab = labels(total, phase)
    return Structure(start=T0, units_source="planning", units_total=total, units_exact=True,
                     phases={str(phase): total}, planned={u: E(34.2) for u in lab},
                     done={lab[i]: E(m) for i, m in enumerate(done_at)}, known_at=E(34.2), **kw)


def est(structure, agents, at, *, prior=PRIOR, kind="gsd-build", **kw):
    return eta.estimate({"agents": agents}, structure, prior, E(at), kind=kind, **kw)


A7_AGENTS = [agent("plan", (16,), 4.8, 34.4), agent("build", (16, 1), 36.1, 40.8)]


# -- the contract -----------------------------------------------------------------------------------


def test_schema_keys():
    d = eta.Eta().to_dict()
    assert tuple(d) == eta.SCHEMA_KEYS and len(set(eta.SCHEMA_KEYS)) == len(eta.SCHEMA_KEYS)
    assert set(d["shown"]) == set(eta.SHOWN_KEYS)
    live = est(single([40.8]), A7_AGENTS, 41, jev={"unit": "verifying"})
    back = eta.Eta.from_dict(json.loads(json.dumps(live.to_dict())))
    assert back == live and set(live.to_dict()) == set(eta.SCHEMA_KEYS)
    assert eta.Eta.from_dict({"source": "units", "bogus": 1}).source == "units"


# -- the ladder ----------------------------------------------------------------------------------------


def test_prior_oracle():
    e = est(Structure(start=T0), [], 13)
    assert e.source == "prior" and e.p50_min is None
    assert e.mid_min == pytest.approx(168.0) and e.p10_min == pytest.approx(95.6) and e.p90_min == pytest.approx(276.6)
    assert e.units_total is None and e.units_done == 0 and e.elapsed_min == pytest.approx(13)
    late = est(Structure(start=T0), [], 400)        # past the class band: never negative, never zero-width
    assert late.p10_min == 0.0 and late.mid_min == 0.0 and late.p90_min >= eta.P90_MIN_PRIOR


def test_class_rate_oracle():
    s = single()
    e = est(s, [agent("plan", (16,), 4.8, 34.4)], 35)
    assert e.source == "class-rate" and e.p50_min is None and e.units_total == 7 and e.units_done == 0
    assert e.mid_min == pytest.approx(144.9, abs=0.01)
    assert e.p10_min == pytest.approx(92.94, abs=0.01) and e.p90_min == pytest.approx(196.86, abs=0.01)
    running = est(s, [agent("plan", (16,), 4.8, 34.4), agent("build", (16, 1), 29, None)], 35)
    assert running.source == "class-rate" and running.first_build_at == E(29)
    assert running.mid_min == pytest.approx(123.9, abs=0.01)


def test_units_shrinkage_oracle():
    e = est(single([40.8]), A7_AGENTS, 41)
    assert e.source == "units" and e.units_done == 1
    assert e.cycle_mean_min == pytest.approx(13.3667, abs=1e-3)
    assert e.mid_min == pytest.approx(86.2, abs=0.01) and e.p50_min == e.mid_min      # G7: a P50 from n = 1
    assert e.p10_min == pytest.approx(51.72, abs=0.01) and e.p90_min == pytest.approx(120.68, abs=0.01)
    assert e.elapsed_min + e.mid_min == pytest.approx(127.2, abs=0.01)                  # for a 128.4-minute job
    running = est(single([40.8]), A7_AGENTS + [agent("build", (16, 2), 40.9, None)], 45)
    assert running.mid_min == pytest.approx(82.1, abs=0.01)


def test_unshrunk_mean_is_what_g7_rejects(monkeypatch):
    monkeypatch.setattr(eta, "K", 0)
    e = est(single([40.8]), A7_AGENTS, 41)
    assert e.elapsed_min + e.mid_min == pytest.approx(75.2, abs=0.01)


def test_no_structure_no_prior_is_none():
    p = eta_prior.prior_for({"rows": [{"job": "x", "kind": "other", "minutes": 9}]}, "other")
    assert p is None
    e = est(Structure(start=T0), [], 10, prior=p, kind="other")
    assert e.source == "none" and e.mid_min is None and e.p10_min is None and e.p90_min is None and e.p50_min is None
    assert e.shown["p10_end"] is None


# -- the a7d292d8 timeline on real tmp repos --------------------------------------------------------------


def _a7_metrics(m):
    out = []
    if m > 4.8:
        out.append(agent("plan", (16,), 4.8, 34.4 if m >= 34.4 else None))
    if m > 36.1:
        out.append(agent("build", (16, 1), 36.1, 40.8 if m >= 40.8 else None))
    if m > 40.9:
        out.append(agent("build", (16, 2), 40.9, None))
    return out


def test_tracer_prior_class_rate_units_on_a7d292d8(tmp_path):
    tl = load_timeline("a7d292d8")
    got = []
    for m in (20, 35, 45):
        home = tmp_path / f"h{m}"
        home.mkdir()
        store = Store(home)
        until = tl["t0"] + m * 60
        setup_job(store, tl, until=until)
        replay_events(store, tl["job"], tl, until)
        st = eta_structure.read(store, tl["job"], SubprocessRunner(), now=until)
        metrics = {"agents": [{**a, "start": a["start"] - T0 + tl["t0"], "end": a["end"] - T0 + tl["t0"] if a["end"] else 0.0}
                              for a in _a7_metrics(m)]}
        got.append(eta.estimate(metrics, st, PRIOR, until, kind="gsd-build"))
    assert [e.source for e in got] == ["prior", "class-rate", "units"]
    assert [e.p50_min is None for e in got] == [True, True, False]
    assert [e.units_done for e in got] == [0, 0, 1]
    assert [e.units_total for e in got] == [None, 7, 7]


# -- trend, cycles, critical path, within-unit ----------------------------------------------------------------


def built(cycles, *, total=7, start=36.0, extra=(), **kw):
    """A single-phase job whose units finished after the given cycle minutes, each run by one finished executor."""
    bounds, t = [], start
    for c in cycles:
        t += c
        bounds.append(t)
    agents = [agent("plan", (16,), 4.8, start - 1.6)]
    prev = start
    for i, b in enumerate(bounds):
        agents.append(agent("build", (16, i + 1), prev, b))
        prev = b
    return single(bounds, total, **kw), agents + list(extra), bounds[-1]


def test_trend_oracle():
    s, agents, last = built([10, 12, 14, 16])
    e = est(s, agents, last + 1)
    assert e.source == "units+trend" and e.units_done == 4
    assert e.cycle_mean_min == pytest.approx(14.5667, abs=1e-3)
    assert e.mid_min == pytest.approx(60.2, abs=0.01)      # 17.0667 + 18.0667 + 19.0667 + 6
    s, agents, last = built([5, 10, 20, 40])
    e = est(s, agents, last + 1)
    assert e.cycle_mean_min == pytest.approx(18.4, abs=1e-3)
    assert e.mid_min == pytest.approx(85.35, abs=0.01)     # slope clamped to +2.3
    s, agents, last = built([40, 20, 10, 5])
    e = est(s, agents, last + 1)
    assert e.mid_min == pytest.approx(37.05, abs=0.01)     # 12.65 + 10.35 + 8.05 + 6: clamped to -mean/(2n)


def test_trend_needs_four_units():
    s, agents, last = built([10, 12, 14])
    assert est(s, agents, last + 1).source == "units"


def test_on_path_planning_is_excluded():
    s = single([46.0, 79.5])
    agents = [agent("build", (16, 1), 36.0, 46.0), agent("plan", (16,), 46.3, 74.5), agent("build", (16, 2), 75.0, 79.5)]
    assert eta.cycle_times(s, agents, E(80), E(36)) == pytest.approx([10.0, 5.3], abs=1e-6)
    over = [agent("build", (16, 1), 36.0, 46.0), agent("plan", (16,), 46.3, 74.5), agent("build", (16, 2), 50.0, 70.0)]
    assert eta.cycle_times(s, over, E(80), E(36)) == pytest.approx([10.0, 25.3], abs=1e-6)   # 28.2 planning, 20 overlapped
    swallowed = [agent("build", (16, 1), 36.0, 46.0), agent("plan", (16,), 40.0, 80.0)]
    assert eta.cycle_times(s, swallowed, E(80), E(36))[1] == eta.MIN_CYCLE_MIN


def test_critical_path_adds_planning_per_unplanned_phase():
    p3 = dataclass_replace(PRIOR, plans_per_phase=3.0)
    lab = [unit_label((1, i)) for i in (1, 2, 3)] + [unit_label((2, i)) for i in (1, 2, 3)]
    s = Structure(start=T0, units_source="planning", phases={"1": 3, "2": 3, "3": None, "4": None, "5": None},
                  planned={u: E(30) for u in lab}, done={lab[0]: E(50)}, known_at=E(30))
    agents = [agent("build", (1, 1), 36.0, 50.0), agent("plan", (3,), 50.0, None)]
    now = 60.0
    off = est(s, agents, now, prior=p3, plan_ahead=False)
    on = est(s, agents, now, prior=p3, plan_ahead=True)
    assert on.tail_min == off.tail_min == 15.0    # a multi-phase build's verify + closeout tail comes from the kind's history
    assert off.mid_min - on.mid_min == pytest.approx(80.0, abs=1e-6)      # (30 - 10) for phase 3, 30 each for 4 and 5
    done = Structure(start=T0, units_source="planning", phases={"1": 3, "2": 3}, planned={u: E(30) for u in lab},
                     done={lab[0]: E(50)}, known_at=E(30))
    assert est(done, agents[:1], now, prior=p3, plan_ahead=False).mid_min == est(done, agents[:1], now, prior=p3).mid_min
    late = [agent("build", (1, 1), 36.0, 50.0), agent("plan", (3,), 20.0, None)]   # planner 40 min old: 0.2 * 30 left
    assert est(s, late, now, prior=p3, plan_ahead=False).mid_min - est(s, late, now, prior=p3).mid_min == pytest.approx(66.0)


def dataclass_replace(prior, **kw):
    import dataclasses
    return dataclasses.replace(prior, **kw)


def test_within_unit_tokens_move_run():
    s = single([40.8])
    peers = [agent("build", (16, 1), 36.1, 40.8, out=10000), agent("build", (15, 8), 37.0, 39.0, out=12000),
             agent("build", (15, 9), 38.0, 40.0, out=14000)]
    tokens = est(s, peers + [agent("build", (16, 2), 40.9, None, out=6000)], 45)
    assert tokens.mid_min == pytest.approx(79.5167, abs=0.01)   # rest = 13.3667 - 0.5 * 13.3667 = 6.683
    phase_level = est(s, peers + [agent("build", (16,), 40.9, None, out=6000)], 45)
    assert phase_level.mid_min == pytest.approx(82.1, abs=0.01)   # one-element unit key: wall time
    few = est(s, peers[:1] + [agent("build", (16, 2), 40.9, None, out=6000)], 45)
    assert few.mid_min == pytest.approx(82.1, abs=0.01)           # fewer than 2 finished peers: wall time
    far = est(s, peers + [agent("build", (16, 2), 40.9, None, out=90000)], 45)   # fraction capped at 95 %, 20 % floor
    assert far.mid_min == pytest.approx(0.2 * 13.3667 + 5 * 13.3667 + 6, abs=0.01)


def test_floors_by_n():
    def half(e):
        return e.p90_min - e.mid_min      # p10 is clamped at 0 when the band is wider than the estimate

    for cycles, floor in (([13, 13], 0.30), ([13, 13, 13], 0.30)):
        s, agents, last = built(cycles)
        e = est(s, agents, last + 1)
        assert half(e) == pytest.approx(floor * e.mid_min, abs=1e-6), cycles
    s, agents, last = built([13, 13, 13, 13])                  # n = 4: floor 0.20, but 10 % of the total job dominates
    e = est(s, agents, last + 1)
    assert half(e) == pytest.approx(eta.TOTAL_FLOOR * (e.elapsed_min + e.mid_min), abs=1e-6)
    assert half(e) > 0.20 * e.mid_min
    # the dispersion term wins when it is larger
    s, agents, last = built([5, 30, 5], total=10)
    e = est(s, agents, last + 1)
    disp = 1.28 * 11.7851 * 7 ** 0.5
    assert half(e) == pytest.approx(disp, abs=0.01) and disp > 0.30 * e.mid_min
    # the 10 % floor is dropped once the final verifier starts (all units done)
    s, agents, last = built([13] * 7)
    before = est(s, agents, last + 1)
    assert half(before) == pytest.approx(eta.TOTAL_FLOOR * (before.elapsed_min + before.mid_min), abs=1e-6)
    after = est(s, agents + [agent("verify", (16,), last + 0.5, None)], last + 1)
    assert half(after) == pytest.approx(0.20 * after.mid_min, abs=1e-6) and half(after) < half(before)
    early = est(s, [agent("verify", (16,), 20.0, 30.0)] + agents, last + 1)   # a plan-checker before the first executor
    assert half(early) == pytest.approx(half(before), abs=1e-6)


def test_multi_phase_tail_and_units():
    p3 = dataclass_replace(PRIOR, plans_per_phase=3.0)
    lab = [unit_label((1, i)) for i in (1, 2, 3)]
    s = Structure(start=T0, units_source="planning", phases={"1": 3, "2": None}, planned={u: E(30) for u in lab},
                  done={lab[0]: E(50)}, known_at=E(30))
    agents = [agent("build", (1, 1), 36.0, 50.0)]
    e = est(s, agents, 55, prior=p3)
    assert e.units_total == 6 and e.units_exact is False and e.tail_min == 15.0 and e.units_done == 1
    assert est(single([50]), agents, 55, prior=p3).tail_min == eta.TAIL_SINGLE_MIN
    # labels (G6, I-3)
    lab2 = lab + [unit_label((2, i)) for i in (1, 2, 3)]
    s2 = Structure(start=T0, units_source="planning", phases={"1": 3, "2": 3}, planned={u: E(30) for u in lab2},
                   done={u: E(50 + i) for i, u in enumerate(lab)}, known_at=E(30))
    run2 = est(s2, agents + [agent("build", (2,), 53.0, None)], 55, prior=p3)
    assert run2.unit_label == "phase 2 · plan 02-01"
    assert run2.open_units == [unit_label((2, i)) for i in (1, 2, 3)]
    plan2 = est(s, agents + [agent("plan", (2,), 52.0, None)], 55, prior=p3)
    assert plan2.unit_label == "phase 2 · planning"
    five = est(s2, agents + [agent("build", (2, 3), 53.0, None)], 55, prior=p3)
    assert five.unit_label == "phase 2 · plan 02-03"
    one = est(single([40.8]), A7_AGENTS + [agent("build", (16, 4), 41.0, None)], 45)
    assert one.unit_label == "plan 16-04" and one.open_units[0] == "16-02"
    many = est(single([], total=20), [], 35)
    assert len(many.open_units) == eta.MAX_OPEN_UNITS


# -- G9: growth, send-backs, finishing -------------------------------------------------------------------------


def test_scope_growth():
    s7, agents, last = built([10, 12, 14], total=7)
    before = est(s7, agents, last + 1)
    assert before.source == "units" and before.units_done == 3 and before.phases == {"16": 7}
    s8 = single([(t - T0) / 60 for t in s7.done.values()], total=8)
    now = last + 2
    grown = est(s8, agents, now, prev=before.to_dict())
    assert grown.source == "class-rate" and grown.p50_min is None and grown.units_total == 8
    assert grown.mid_min == pytest.approx((8 - 3) * 17.7 + 6, abs=0.01)
    assert grown.scope_grown_at == E(now) and grown.grown_units_done == 3
    half = (grown.p90_min - grown.p10_min) / 2
    assert half == pytest.approx(0.30 * grown.mid_min, abs=1e-6)
    held = est(s8, agents, now + 1, prev=grown)                      # held until the next boundary
    assert held.source == "class-rate" and held.scope_grown_at == E(now)
    s8b = single([(t - T0) / 60 for t in s7.done.values()] + [now + 2], total=8)
    after = est(s8b, agents + [agent("build", (16, 4), last, now + 2)], now + 3, prev=held)
    assert after.source == "units+trend" and after.units_done == 4 and after.scope_grown_at is None
    assert after.units_total == 8


def test_tbd_to_number_is_not_growth_but_a_new_phase_is():
    p3 = dataclass_replace(PRIOR, plans_per_phase=3.0)
    lab = [unit_label((1, i)) for i in (1, 2, 3)]
    tbd = Structure(start=T0, units_source="planning", phases={"1": 3, "2": None}, planned={u: E(30) for u in lab},
                    done={lab[0]: E(50)}, known_at=E(30))
    agents = [agent("build", (1, 1), 36.0, 50.0)]
    before = est(tbd, agents, 55, prior=p3)
    planned = Structure(start=T0, units_source="planning", phases={"1": 3, "2": 3},
                        planned={u: E(30) for u in lab + [unit_label((2, i)) for i in (1, 2, 3)]},
                        done={lab[0]: E(50)}, known_at=E(30))
    same = est(planned, agents, 56, prior=p3, prev=before)
    assert same.source == "units" and same.scope_grown_at is None
    new_phase = Structure(start=T0, units_source="planning", phases={"1": 3, "2": 3, "3": 2},
                          planned=planned.planned, done={lab[0]: E(50)}, known_at=E(30))
    grown = est(new_phase, agents, 57, prior=p3, prev=same)
    assert grown.source == "class-rate" and grown.scope_grown_at == E(57)
    # before any unit finished the plan count settling is not growth
    start = est(Structure(start=T0, phases={"1": 3}, units_total=3, units_exact=True), [], 20)
    settling = est(Structure(start=T0, phases={"1": 4}, units_total=4, units_exact=True), [], 21, prev=start)
    assert settling.scope_grown_at is None


def test_send_back_keeps_units():
    s, agents, last = built([10, 12, 14], total=7)
    first = est(s, agents, last + 1)
    s2 = Structure(**{**s.to_dict(), "attempt_no": 2, "rework": True, "attempt_start": E(last + 2)})
    second = est(s2, agents, last + 3, prev=first)
    assert second.source == "units" and second.units_done == 3 and second.attempt_no == 2
    assert second.shown["attempt_no"] == 2 and second.shown["set_at"] == E(last + 3)   # re-anchored
    assert first.shown["attempt_no"] == 0


def test_finishing_and_false_done():
    s, agents, last = built([10] * 6, total=7)
    verifier = agents + [agent("verify", (16,), last + 1, None)]
    assert est(s, verifier, last + 2).finishing is True
    assert est(s, agents, last + 2).finishing is False                         # no verifier, no Jev
    assert est(s, agents, last + 2, jev={"near_done": "yes"}).finishing is True
    assert est(s, agents, last + 2, jev={"unit": "finished"}).jev_state == "ok"
    s2, agents2, last2 = built([10, 10], total=7)
    lying = est(s2, agents2 + [agent("verify", (16,), last2 + 1, None)], last2 + 2, jev={"unit": "finished"})
    assert lying.finishing is False                                            # Jev cannot make 2/7 finishing
    # a worker-reported done with nothing behind it: estimating… (ed11ba3c's 3-minute done at 0/7)
    claimed = Structure(**{**single().to_dict(), "done_claimed_at": E(40), "attempt_start": E(35)})
    e = est(claimed, [], 41)
    assert e.done_claimed is True and e.finishing is False
    assert eta.render(e, E(41))["text"] == "estimating…"
    unknown = est(Structure(start=T0, done_claimed_at=E(10)), [], 12)
    assert unknown.done_claimed is True and eta.render(unknown, E(12))["text"] == "estimating…"
    # 6 of 7 with a claim and the verifier: genuinely finishing, not a false done
    ok = est(Structure(**{**s.to_dict(), "done_claimed_at": last + 1.5}), verifier, last + 2)
    assert ok.finishing is True and ok.done_claimed is False
