"""SUP-06: the deterministic code-fact floor — model-free detectors over store events that feed the Arbiter.

Each fact is idempotent per (job, kind, evidence key) and fires whether or not Jev is available."""

from __future__ import annotations

import pytest

from steward import approvals, code_facts, stability
from steward import fleet_runtime as rt
from steward.models import Judgment
from steward.store import Store
from test_runner import _contracted_job


@pytest.fixture
def env(steward_home):
    store = Store(steward_home)
    jid = _contracted_job(store, "fix it").job_id
    return store, jid, rt.Arbiter(store)


def _ev(store, jid, what, rule="r", **evidence):
    return store.append_event(jid, what=what, evidence=evidence, rule=rule, judgment=Judgment.code)


def _kinds(arb, jid):
    return [f["kind"] for f in arb.pending_for_tech_lead(jid)]


def test_finish_check_unverified_is_a_fact_once(env):
    store, jid, arb = env
    _ev(store, jid, "finish_check_unverified", command="python3 -m pytest -q", reason="timed out")
    code_facts.step(store, jid, arb)
    code_facts.step(store, jid, arb)  # idempotent: the same evidence never re-submits
    assert _kinds(arb, jid) == ["finish_check_unverified"]


def test_finish_bounces_exhausted(env):
    store, jid, arb = env
    _ev(store, jid, "finish line still not met after 3 nudges: x", rule="finish_line_bounces_exhausted")
    code_facts.step(store, jid, arb)
    assert _kinds(arb, jid) == ["finish_bounces_exhausted"]


def test_new_failures_repeated_after_a_send_back(env):
    store, jid, arb = env
    _ev(store, jid, "verification recorded", rule="verify_at_exact_commit", new_failures=["t::a", "t::b"])
    code_facts.step(store, jid, arb)
    assert _kinds(arb, jid) == []  # once is the normal send-back
    _ev(store, jid, "verification recorded", rule="verify_at_exact_commit", new_failures=["t::a"])
    code_facts.step(store, jid, arb)
    assert _kinds(arb, jid) == []  # v2.7 SRD-08: the worker keeps getting it back; no tech-lead fact while rounds remain
    store.kv_set(jid, "verify_rounds", 3)  # the verify-round budget (2) is exhausted
    _ev(store, jid, "verification recorded", rule="verify_at_exact_commit", new_failures=["t::a"])
    code_facts.step(store, jid, arb)
    facts = arb.pending_for_tech_lead(jid)
    assert [f["kind"] for f in facts] == ["new_failures_repeated"] and facts[0]["data"]["test"] == "t::a"


def test_identical_permission_ask_three_times(env):
    store, jid, arb = env
    for _ in range(2):
        approvals.open_approval(store, jid, "worker permission: Write(/wt/notes.md)", kind="permission")
    code_facts.step(store, jid, arb)
    assert _kinds(arb, jid) == []
    approvals.open_approval(store, jid, "worker permission:  Write(/wt/notes.md) ", kind="permission")
    code_facts.step(store, jid, arb)
    code_facts.step(store, jid, arb)
    assert _kinds(arb, jid) == ["permission_ask_repeated"]


def test_idle_after_final_message_is_no_longer_a_code_fact(env):
    """v2.7 Phase 40: a stop is a `worker_stopped` yield fact from the runner, not an idle-inference fact."""
    store, jid, arb = env
    store.kv_set(jid, stability.FINISH_UNPROVEN_KEY, True)
    _ev(store, jid, "idle_after_final_message", rule="idle_after_final_message", idle_s=120)
    code_facts.step(store, jid, arb)
    assert _kinds(arb, jid) == [] and "idle_unverified" not in code_facts.KINDS


def test_review_watchdog_expired(env):
    store, jid, arb = env
    _ev(store, jid, "tech_lead_review_expired", rule="tech_lead_watchdog", review="r1")
    code_facts.step(store, jid, arb)
    assert _kinds(arb, jid) == ["review_watchdog_expired"]


def test_fires_with_jev_unavailable(env):
    store, jid, arb = env
    _ev(store, jid, "jev_unavailable", rule="jev_shadow_unavailable", code="http_error_402")
    store.kv_set(jid, "jev_status", "unavailable:http_error_402")
    _ev(store, jid, "finish_check_unverified", command="c", reason="timed out")
    code_facts.step(store, jid, arb)
    assert _kinds(arb, jid) == ["finish_check_unverified"]


def test_no_model_is_ever_called(env, monkeypatch):
    store, jid, arb = env
    from steward import jev_observer

    monkeypatch.setattr(jev_observer.JevObserver, "observe", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    _ev(store, jid, "finish_check_unverified", command="c", reason="timed out")
    assert code_facts.step(store, jid, arb) == ["finish_check_unverified"]


def test_runner_tick_runs_the_floor(steward_home):
    from test_finish_line_gate import TimeoutRunner
    from test_stability import _done_obs, _finish_job, _runner

    store = Store(steward_home)
    job = _finish_job(store)
    runner = _runner(store, TimeoutRunner(), [[_done_obs()]])
    runner.start(job.job_id)
    runner.tick(job.job_id)
    assert "finish_check_unverified" in _kinds(runner.arbiter, job.job_id)
