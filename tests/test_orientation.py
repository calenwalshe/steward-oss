"""SUP-10 / PRD D-09: orientation, not plans. Every review packet carries a cached orientation brief (goal, finish
line, contract + floor summary, milestone goal, one-line phase status, current phase must_have truths; <= 1,500
tokens, clipped deterministically; rebuilt only when plan/contract hashes change). Drill-down pulls one named plan
section, bounded; full reads only on takeover."""

from __future__ import annotations

from pathlib import Path

import pytest

from steward import orientation, tech_lead_floor
from steward.contract import Contract, store_contract
from steward.store import Store

ROADMAP = """# Roadmap

## Milestone v2.5 — Multi-runtime parallel fleet

### Phase 30: Runtime adapters
**Goal:** adapters
Plans:
- [x] 30-01-PLAN.md — adapters

# Milestone v2.6 — Autonomous supervision (append to .planning/ROADMAP.md)

Source: the PRD. Four sequential phases.

### Phase 33: Verification floor

**Goal:** A finish check that cannot finish never stands in for verification.
**Plans:** 1 plan

Plans:

- [x] 33-01-PLAN.md — Unverified finish checks

### Phase 34: Layered detection

**Goal:** Trouble is noticed by Jev and by code facts.

Plans:

- [ ] 34-01-PLAN.md — Jev general flags, code-fact floor

### Phase 35: Tech lead for every job

**Goal:** The roster-ordered tech lead diagnoses, unblocks, sends back and takes over.

Plans:

- [ ] 35-01-PLAN.md — Orientation brief
"""

PLAN34 = """---
phase: 34-layered-detection
plan: "01"
must_haves:
  truths:
    - "Jev offers permission_confusion and thrash for every job."
    - "A deterministic code-fact floor emits Arbiter facts with Jev up or down."
  artifacts:
    - path: steward/code_facts.py
---

<objective>
Make detection layered.
</objective>

<tasks>
<task type="tracer" tdd="true">
  <name>Code-fact floor and Arbiter for every job</name>
  <behavior>code_facts reads store events per tick.</behavior>
</task>
<task type="auto" tdd="true">
  <name>Jev general flags</name>
  <behavior>CHOICES gain permission_confusion and thrash.</behavior>
</task>
</tasks>
"""


def _recontract(store, jid, contract):
    """A contract revision on an already-contracted job (the kv record the brief hashes)."""
    from dataclasses import asdict

    store.kv_set(jid, "contract", asdict(contract))


@pytest.fixture
def world(steward_home, tmp_path):
    store = Store(steward_home)
    goal = "Execute v2.6 phases 33-36. Finish line: `python3 -m pytest -q tests/test_a.py` passes."
    job = store.create_job(goal, str(tmp_path))
    store_contract(store, job.job_id, Contract(goal=goal, boundaries=["never git push"], done_when=["tests pass"],
                                               next_step="", authority=["python3 -m pytest -q"], assumptions=[]))
    wt = store.home / "worktrees" / job.job_id
    (wt / ".planning" / "phases" / "34-layered-detection").mkdir(parents=True)
    (wt / ".planning" / "phases" / "33-verification-floor").mkdir(parents=True)
    (wt / ".planning" / "ROADMAP.md").write_text(ROADMAP)
    (wt / ".planning" / "phases" / "34-layered-detection" / "34-01-PLAN.md").write_text(PLAN34)
    (wt / ".planning" / "phases" / "33-verification-floor" / "33-01-SUMMARY.md").write_text("# done\n")
    return store, job.job_id, wt


def test_brief_has_every_section(world):
    store, jid, wt = world
    b = orientation.brief(store, jid, wt)
    text = b["text"]
    assert "Execute v2.6 phases 33-36" in text                                  # goal
    assert "python3 -m pytest -q tests/test_a.py" in text                       # finish line
    assert "tests pass" in text and "never git push" in text                    # contract
    assert "Hard floor" in text and "outside_world" in text                     # floor summary
    assert "Milestone v2.6" in text and "Autonomous supervision" in text        # milestone goal
    assert "33 Verification floor: done" in text and "34 Layered detection: current" in text
    assert "35 Tech lead for every job: pending" in text                        # one line per phase
    assert "Jev offers permission_confusion and thrash" in text                 # current phase must_have truths
    assert "Code-fact floor and Arbiter" not in text                            # no whole plans
    assert b["tokens"] <= orientation.MAX_TOKENS


def test_brief_is_clipped_deterministically_to_the_token_cap(world):
    store, jid, wt = world
    long_goal = "Do the thing. " * 2000
    _recontract(store, jid, Contract(goal=long_goal, boundaries=["b" * 3000] * 5, done_when=["d" * 3000] * 5,
                                     next_step="", authority=[], assumptions=[]))
    a = orientation.build(store, jid, wt)
    b = orientation.build(store, jid, wt)
    assert a["text"] == b["text"] and a["tokens"] <= orientation.MAX_TOKENS
    assert "Hard floor" in a["text"]  # the floor summary survives clipping


def test_cached_and_rebuilt_only_on_plan_or_contract_change(world):
    store, jid, wt = world
    first = orientation.brief(store, jid, wt)
    again = orientation.brief(store, jid, wt)
    assert again["built_at"] == first["built_at"] and again.get("cached") is True
    builds = [e for e in store.events(jid) if e.what == "orientation_brief_built"]
    assert len(builds) == 1
    (wt / ".planning" / "ROADMAP.md").write_text(ROADMAP.replace("- [ ] 34-01", "- [x] 34-01"))
    third = orientation.brief(store, jid, wt)
    assert third["plan_hash"] != first["plan_hash"] and "34 Layered detection: done" in third["text"]
    _recontract(store, jid, Contract(goal="new goal", boundaries=[], done_when=["x"], next_step="",
                                     authority=[], assumptions=[]))
    fourth = orientation.brief(store, jid, wt)
    assert fourth["contract_hash"] != third["contract_hash"]
    assert len([e for e in store.events(jid) if e.what == "orientation_brief_built"]) == 3


def test_drill_down_pulls_one_named_section_bounded(world):
    store, jid, wt = world
    tasks = orientation.drill_down(wt, "34-01#tasks", max_chars=2000)
    assert "Code-fact floor and Arbiter" in tasks and "must_haves" not in tasks
    truths = orientation.drill_down(wt, "34-01#must_haves")
    assert "deterministic code-fact floor" in truths
    phase = orientation.drill_down(wt, "ROADMAP#Phase 35")
    assert "roster-ordered tech lead" in phase and "Phase 34" not in phase
    assert len(orientation.drill_down(wt, "34-01", max_chars=300)) <= 300
    assert orientation.drill_down(wt, "99-01#tasks").startswith("no such plan section")
    assert orientation.drill_down(wt, "../../etc/passwd").startswith("no such plan section")


def test_full_read_only_in_takeover(world):
    store, jid, wt = world
    (wt / "a.py").write_text("x = 1\n")
    with pytest.raises(orientation.OrientationError):
        orientation.full_read(wt, ["a.py"], mode="review")
    assert orientation.full_read(wt, ["a.py"], mode="takeover") == {"a.py": "x = 1\n"}
    with pytest.raises(orientation.OrientationError):
        orientation.full_read(wt, ["../outside.py"], mode="takeover")


def test_brief_requires_goal_and_contract(world, steward_home, tmp_path):
    store, jid, wt = world
    bare = store.create_job("", str(tmp_path))
    with pytest.raises(orientation.OrientationError):
        orientation.build(store, bare.job_id, wt)


def test_no_planning_dir_still_orients(steward_home, tmp_path):
    store = Store(steward_home)
    job = store.create_job("Fix the parser. Finish line: `pytest -q tests/test_p.py`", str(tmp_path))
    store_contract(store, job.job_id, Contract(goal=job.goal, boundaries=[], done_when=["parser fixed"],
                                               next_step="", authority=[], assumptions=[]))
    text = orientation.brief(store, job.job_id, tmp_path / "nowhere")["text"]
    assert "Fix the parser" in text and "no .planning roadmap" in text and tech_lead_floor.SUMMARY[:20] in text


def test_fleet_packet_carries_the_brief(steward_home, tmp_path):
    from test_fleet_tech_lead import JOB, TL

    from steward import fleet_tech_lead as tl

    w = TL(tmp_path, steward_home)
    store_contract(w.store, JOB, Contract(goal="goal", boundaries=[], done_when=["d"], next_step="", authority=[],
                                          assumptions=[]))
    sup = tl.TechLeadSupervisor(w.store, w.ctl, None, clock=w.clock, facts=w.ctl.arbiter)
    pkt = sup.packet(JOB, [{"kind": "cadence", "ref": "cadence:1"}])
    assert "orientation" in pkt and "goal" in pkt["orientation"] and "Hard floor" in pkt["orientation"]
