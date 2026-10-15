"""Plan 3 Task 5: tech-lead calls are priced or explicitly unknown and checked against owner hard headroom."""
from __future__ import annotations

import subprocess

import pytest
from recovery_fixtures import claude_envelope
from test_tech_lead_no_clog import ASTRA, FABLE, Clock, make_lead, send_back_proposal, wait_done

from steward import code_facts
from steward import fleet_tech_lead as tl
from steward import spend_policy as sp
from steward import tech_lead_floor
from steward.models import JobState, Judgment
from steward.store import Store
from test_runner import _contracted_job


@pytest.fixture
def env(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store, "fix it. Finish line: `pytest -q tests/test_a.py`")
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    (store.home / "worktrees" / job.job_id).mkdir(parents=True)
    return store, job.job_id


class Costed:
    def __init__(self, answers):
        self.answers = dict(answers)
        self.calls = []

    def __call__(self, entry, prompt, timeout_s):
        self.calls.append(entry.id)
        return self.answers[entry.id]


def test_lead_review_cost_is_recorded_and_reaches_the_floor(env):
    store, jid = env
    clock = Clock()
    lead, arb = make_lead(store, Costed({"astra-tech-lead": (send_back_proposal(), 0.30)}), clock)
    arb.submit(jid, code_facts.SOURCE, "finish_check_unverified", key="a")
    lead.on_event(jid)
    wait_done(lead, jid)
    lead.on_event(jid)
    rows = store._conn.execute("SELECT source, usd, price_basis, actor_id FROM spend_ledger WHERE job_id = ?",
                               (jid,)).fetchall()
    assert [tuple(r) for r in rows] == [("tech_lead", 0.30, "reported", "astra-tech-lead")]
    sp.set_limit(store, jid, "supervision", 1.0, binding="hard", provenance="owner_explicit",
                 source={"via": "spend-limit"}, now=clock())
    ctx = tech_lead_floor.context_for(store, jid, store.home / "worktrees" / jid)
    assert ctx.money_cap_usd == 1.0 and ctx.spent_usd == pytest.approx(0.30)
    assert not tech_lead_floor.check({"type": "send_back", "cost_usd": 0.80}, ctx).ok


def test_codex_lead_cost_is_unknown_not_zero(env):
    store, jid = env
    clock = Clock()
    lead, arb = make_lead(store, Costed({"astra-tech-lead": send_back_proposal()}), clock)
    arb.submit(jid, code_facts.SOURCE, "finish_check_unverified", key="a")
    lead.on_event(jid)
    wait_done(lead, jid)
    lead.on_event(jid)
    (row,) = store._conn.execute("SELECT usd, price_basis FROM spend_ledger WHERE job_id = ?", (jid,)).fetchall()
    assert tuple(row) == (None, "unknown")


def test_advisory_limits_never_refuse_a_lead_action(env):
    store, jid = env
    sp.record(store, entry_id="lead:x", job_id=jid, source="tech_lead", usd=50.0,
              price_basis="reported", now=1.0)
    ctx = tech_lead_floor.context_for(store, jid, store.home / "worktrees" / jid)
    assert ctx.money_cap_usd is None
    assert tech_lead_floor.check({"type": "send_back", "cost_usd": 5.0}, ctx).ok


def test_unbounded_lead_is_skipped_under_an_owner_cap_and_bounded_one_runs(env):
    store, jid = env
    clock = Clock()
    sp.set_limit(store, jid, "supervision", 5.0, binding="hard", provenance="owner_explicit",
                 source={"via": "spend-limit"}, now=clock())
    costed = Costed({"fable-tech-lead": (send_back_proposal(), 0.10)})
    lead, arb = make_lead(store, costed, clock)
    arb.submit(jid, code_facts.SOURCE, "finish_check_unverified", key="a")
    res = lead.on_event(jid)
    assert res["status"] == "review_started" and res["lead"] == "fable-tech-lead"
    started = [e for e in store.events(jid) if e.what == "tech_lead_review_started"][-1]
    assert started.evidence["skipped"][0]["reason"].startswith("spend_hold: unbounded_under_hard_cap")


def test_no_lead_fits_is_a_spend_hold_not_an_outage(env):
    store, jid = env
    clock = Clock()
    sp.set_limit(store, jid, "supervision", 1.0, binding="hard", provenance="owner_explicit",
                 source={"via": "spend-limit"}, now=clock())
    lead, arb = make_lead(store, Costed({}), clock)
    arb.submit(jid, code_facts.SOURCE, "finish_check_unverified", key="a")
    res = lead.on_event(jid)
    assert res["status"] == "spend_hold"
    whats = [e.what for e in store.events(jid)]
    assert "tech_lead_spend_hold" in whats and "tech_lead_exhausted" not in whats


def test_invoke_via_runtime_claude_reads_cost_and_passes_allowance(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout=claude_envelope('{"a": 1}', cost=0.12), stderr="")

    monkeypatch.setattr(tl.subprocess, "run", fake_run)
    text, cost = tl.invoke_via_runtime(FABLE, "P", 30)
    assert text == '{"a": 1}' and cost == 0.12
    argv = seen["argv"]
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--max-budget-usd") + 1] == "2"


# -- v2.7 Phase 41: the advisory supervision allowance, from a real SpendReport -----------------------------------------


def _report(job_id, worker, supervision):
    return sp.SpendReport(job_id=job_id, known_usd=worker + supervision, unknown_calls=0, by_source={},
                          worker_usd=worker, supervision_usd=supervision, review_usd=0.0, jev_calls=0, limits=[],
                          estimate=None)


@pytest.mark.parametrize("worker,supervision,allowance,over", [
    (0.0, 0.0, 3.0, False), (4.0, 2.9, 3.0, False), (4.0, 3.0, 3.0, True), (20.0, 9.9, 10.0, False),
    (20.0, 10.0, 10.0, True)])
def test_allowance_is_max_3_or_half_the_worker_spend(env, worker, supervision, allowance, over):
    from steward import supervision_allowance as sa

    store, jid = env
    sa.reset_cache()
    st = sa.standing(store, jid, now=1000.0, report_fn=lambda: _report(jid, worker, supervision))
    assert (st.allowance_usd, st.over) == (allowance, over)


def test_no_default_hard_cap_the_allowance_only_defers(env):
    from steward import supervision_allowance as sa

    store, jid = env
    assert sp.hard_limits(store, jid) == []  # nothing creates an owner cap by default
    sa.reset_cache()
    assert sa.defer_reason(store, jid, [], now=1000.0, report_fn=lambda: _report(jid, 1.0, 50.0)) == "allowance"
    assert sp.hard_limits(store, jid) == []  # and deferring never writes one
