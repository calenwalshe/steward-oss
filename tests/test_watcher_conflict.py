"""Phase 15-03: priority, conflict quarantine, pause-lease-in-conflict, release counters, runner wiring."""

from __future__ import annotations

import time

import pytest
from test_runner import _contracted_job, _Factory, _fake_diagnoser, _git_runner
from test_watcher_gate import T0, Rig, _pause_ready

from steward import cli
from steward import watcher_authority as wa
from steward.approvals import list_approvals
from steward.models import JobState, Judgment
from steward.runner import Runner
from steward.steering import Steering


def test_priority_order_and_sources():
    assert wa.PRIORITY == ("human", "policy", "advisor", "doctor", "watcher")
    assert wa.source_of("cli") == "human" and wa.source_of("moshi") == "human"
    assert wa.source_of("steward") == "policy" and wa.source_of("trajectory") == "advisor"
    assert wa.source_of("diagnosis") == "doctor" and wa.source_of("watcher:x") == "watcher"
    order = ["cli", "steward", "trajectory", "diagnosis", "watcher:x"]
    for i, hi in enumerate(order):
        for lo in order[i + 1:]:
            assert wa.outranks(hi, lo) and not wa.outranks(lo, hi)
    assert not wa.outranks("watcher:a", "watcher:b")


def test_a_recent_human_resume_outranks_a_watcher_halt(steward_home):
    r = Rig(steward_home)
    r.clock = time.time()
    r.store.kv_set(r.j, wa.SAMPLES_KEY, [{"head": "h", "diff": "d", "alive": True, "open": False, "t": r.clock - 4000, "last_t": r.clock}])
    r.calls = [wa.ToolCall(c.id, c.tool, c.input, r.clock - 100 + i, True, True, "2") for i, c in enumerate(r.calls)]
    r.store.set_state(r.j, JobState.PAUSED, what="op", rule="operator_pause", evidence={}, judgment=Judgment.human)
    r.store.set_state(r.j, JobState.RUNNING, what="operator resume", rule="operator_resume", evidence={}, judgment=Judgment.human)
    r.apply()
    ev = r.apply(advance=30)
    assert "interrupt:yield:human" in r.sup(ev) and r.interrupts() == []
    assert wa.quarantine(r.store, r.j) is None  # yielding to a higher priority is not a conflict


def test_contrary_watcher_proposals_quarantine_all_actuation_and_escalate(steward_home):
    r = Rig(steward_home, tier=4)
    b = r.add("b", tier=4, model="sonnet", ask="should we resume?")
    _pause_ready(r)
    v = r.verdict(interrupt=None, pause={"reason": "loop"})
    r.apply(v)
    ev = r.apply(v, advance=600)
    assert ev["actions"]["pause"]["accepted"] and r.store.get_job(r.j).state == JobState.PAUSED
    r.store.kv_set(r.j, "watcher_freeze_until", 0)
    ev = r.apply(r.verdict(interrupt=None, resume=True, status="ok"), w=b, advance=30)  # B contradicts A's halt
    assert "resume:quarantined" in r.sup(ev) and "actions" not in ev
    q = wa.quarantine(r.store, r.j)
    assert q and set(q["watchers"]) == {"w", "b"}
    assert [a for a in list_approvals(r.store, r.j, status="pending") if "watcher conflict" in a["question"]]
    assert any("conflict" in n[2] for n in r.notes)
    assert r.store.get_job(r.j).state == JobState.PAUSED  # the pause lease already in force is left to lapse on its own
    # quarantine blocks every watcher actuation
    for w, kw in ((r.w, {"interrupt": {"text": "x"}}), (b, {"interrupt": {"text": "x"}}),
                  (r.w, {"interrupt": None, "status": "concern", "nudge": "n"})):
        e = r.apply(r.verdict(**kw), w=w, advance=1000)
        assert any("quarantined" in s for s in r.sup(e)), e
    assert r.interrupts() == []
    # ... but escalation to Sam still works
    e = r.apply(r.verdict(interrupt=None, status="concern", escalate="please look"), w=r.w, advance=10)
    assert "escalated" in e
    # only Sam clears it
    with pytest.raises(wa.AuthorityError):
        wa.clear_quarantine(r.store, r.j, actor="watcher:w")
    assert cli.main(["watcher", "unquarantine", r.j]) == 0
    assert wa.quarantine(r.store, r.j) is None
    assert any(e.what == "watcher_quarantine_cleared" and e.judgment.value == "human" for e in r.store.events(r.j))


def test_halt_vs_resume_proposals_in_the_same_hour_conflict_even_without_a_lease(steward_home):
    r = Rig(steward_home, tier=4)
    b = r.add("b", tier=4, model="sonnet", ask="q2")
    r.store.kv_set(r.j, wa.LEASE_KEY, {"name": "w", "since": T0, "until": T0 + 5000, "job_updated_at": ""})
    r.apply(r.verdict())  # A proposes interrupt (first observation, logged as an interrupt-grade proposal)
    ev = r.apply(r.verdict(interrupt=None, resume=True, status="ok"), w=b, advance=60)
    assert "resume:quarantined" in r.sup(ev)
    assert wa.quarantine(r.store, r.j)


def test_no_conflict_outside_the_hour(steward_home):
    r = Rig(steward_home, tier=4)
    b = r.add("b", tier=4, model="sonnet", ask="q2")
    r.apply(r.verdict())
    r.store.kv_set(r.j, wa.LEASE_KEY, {"name": "b", "since": T0, "until": T0 + 9000, "job_updated_at": ""})
    ev = r.apply(r.verdict(interrupt=None, resume=True, status="ok"), w=b, advance=3700)
    assert wa.quarantine(r.store, r.j) is None and ev["actions"]["resume"]["accepted"] in (True, False)


def test_a_watcher_resuming_its_own_pause_is_a_reversal_not_a_conflict(steward_home):
    r = Rig(steward_home, tier=4)
    _pause_ready(r)
    v = r.verdict(interrupt=None, pause={"reason": "r"})
    r.apply(v)
    r.apply(v, advance=600)
    r.store.kv_set(r.j, "watcher_freeze_until", 0)
    ev = r.apply(r.verdict(interrupt=None, resume=True, status="ok"), advance=30)
    assert ev["actions"]["resume"]["accepted"] and wa.quarantine(r.store, r.j) is None
    assert wa.release_counters(r.store, r.j)["reversals"] == 1


def test_conflict_pause_lease_only_after_two_interrupts_or_a_reversal(steward_home):
    def conflict(r, b):
        r.store.kv_set(r.j, wa.LEASE_KEY, {"name": "w", "since": T0, "until": T0 + 5000, "job_updated_at": ""})
        r.apply(r.verdict())
        r.apply(r.verdict(interrupt=None, resume=True, status="ok"), w=b, advance=60)

    r = Rig(steward_home, tier=4)
    conflict(r, r.add("b", tier=4, model="sonnet", ask="q2"))
    assert wa.quarantine(r.store, r.j) and r.store.get_job(r.j).state == JobState.RUNNING  # no pause: too early
    assert not wa.conflict_pause_eligible(r.store, r.j, r.clock)

    r2 = Rig(steward_home / "x" if False else steward_home, tier=4)
    r2.store.kv_set(r2.j, "watcher_interrupts", [{"name": "w", "rev": 1, "ts": T0 - 100}, {"name": "b", "rev": 2, "ts": T0 - 50}])
    conflict(r2, r2.add("b", tier=4, model="sonnet", ask="q2"))
    assert r2.store.get_job(r2.j).state == JobState.PAUSED
    assert any(e.what == "watcher_conflict_pause_lease" for e in r2.store.events(r2.j))
    assert r2.store.kv_get(r2.j, wa.LEASE_KEY)["until"] - r2.store.kv_get(r2.j, wa.LEASE_KEY)["since"] == 900

    r3 = Rig(steward_home, tier=4)
    r3.store.append_event(r3.j, what="watcher_pause_reversal", evidence={}, rule="x", judgment=Judgment.code)
    r3.clock = time.time()
    assert wa.conflict_pause_eligible(r3.store, r3.j, r3.clock)


def test_release_counters_expose_the_i4_falsifier(steward_home):
    r = Rig(steward_home)
    r.store.kv_set(r.j, "watcher_runs:w", 100)
    for _ in range(2):
        wa.record_case(r.store, r.j, r.w, "false", actor="sam", kind="interrupt", now=T0)
    c = wa.release_counters(r.store, r.j)
    assert c["false_interrupts"] == 2 and c["runs"] == 100 and c["false_interrupt_rate_per_100_runs"] == 2.0
    assert c["revisit_lifetime_cap"] is True  # > 1 per 100 runs -> revisit (Sam, I4)
    r2 = Rig(steward_home)
    r2.store.kv_set(r2.j, "watcher_runs:w", 300)
    assert wa.release_counters(r2.store, r2.j)["revisit_lifetime_cap"] is False


# -- runner wiring ------------------------------------------------------------------------------


def test_runner_expires_a_watcher_pause_lease_and_records_samples(steward_home):
    from steward.store import Store

    store = Store(steward_home)
    job = _contracted_job(store)
    clock = {"t": 1000.0}
    runner = Runner(store, _Factory(scripts=[[]]), _git_runner(), _fake_diagnoser(), clock=lambda: clock["t"])
    runner.start(job.job_id)
    runner.tick(job.job_id)
    samples = store.kv_get(job.job_id, wa.SAMPLES_KEY)
    assert samples and samples[-1]["head"] == "sha-1"
    runner.tick(job.job_id)
    assert len(store.kv_get(job.job_id, wa.SAMPLES_KEY)) == len(samples)  # unchanged ticks merge
    ctx = wa.GateContext(clock["t"], None, None)
    wa._do_pause(store, job.job_id, {"name": "w"}, {"reason": "r"}, [], ctx)
    assert store.get_job(job.job_id).state == JobState.PAUSED
    clock["t"] += 100
    assert runner.tick(job.job_id).detail == "paused; no dispatch"
    clock["t"] += 900
    report = runner.tick(job.job_id)
    assert store.get_job(job.job_id).state == JobState.RUNNING and report.detail != "paused; no dispatch"


def test_runner_marks_a_delivered_steer_as_a_settle_lease(steward_home):
    from steward.store import Store

    store = Store(steward_home)
    job = _contracted_job(store)
    runner = Runner(store, _Factory(scripts=[[]]), _git_runner(), _fake_diagnoser(), clock=lambda: 5000.0)
    runner.start(job.job_id)
    Steering(store).record(job.job_id, "nudge", "please", origin="cli")
    runner.tick(job.job_id)
    assert store.kv_get(job.job_id, "watcher_settle_until") == 5000.0 + wa.SETTLE_S


def test_human_only_paths_have_no_api_surface_for_watchers():
    """The MCP/API surface exposes neither trust promotion, adjudication nor quarantine clearing."""
    import inspect

    from steward import api, mcp_server
    text = inspect.getsource(api) + inspect.getsource(mcp_server)
    for banned in ("watcher_authority.promote", "wa.promote", "record_case", "clear_quarantine", "gated_by"):
        assert banned not in text
