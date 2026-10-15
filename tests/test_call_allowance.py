"""Plan 3 Task 6: exhausted calls retain partial evidence but cannot act; continuations need evidence of progress."""
from __future__ import annotations

import json

from recovery_fixtures import claude_envelope
from test_flow_investigator import FakeInvestigator, _answer, _stored_episode

from steward import call_allowance as ca
from steward import flow_investigator as fi
from steward import spend_policy as sp
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store


def test_classify_envelopes():
    got = ca.classify_claude_envelope(claude_envelope("half an answer", cost=0.50, is_error=True,
                                                      subtype="error_max_budget_usd"), exit_code=1,
                                      allowance_usd=0.50)
    assert got.outcome == "allowance_exhausted" and got.text == "half an answer" and got.cost_usd == 0.50
    assert ca.classify_claude_envelope(claude_envelope("x", cost=0.9), exit_code=0,
                                       allowance_usd=0.5).outcome == "over_allowance"
    assert ca.classify_claude_envelope(claude_envelope("x", cost=0.1), exit_code=0,
                                       allowance_usd=0.5).outcome == "completed"
    assert ca.classify_claude_envelope("not json", exit_code=0, allowance_usd=0.5).outcome == "invalid"
    assert ca.classify_claude_envelope(claude_envelope("", is_error=True, subtype="error_during_execution"),
                                       exit_code=1, allowance_usd=0.5).outcome == "failed"


def test_over_allowance_flow_answer_is_validated_not_discarded(steward_home):
    store = Store(steward_home)
    ep = _stored_episode(store)
    runner = FakeRunner(lambda argv, cwd: CommandResult(argv, cwd, 0,
                         claude_envelope(json.dumps(_answer()), cost=3.0), "", 0.1))
    inv = fi.ClaudeFlowInvestigator(runner, model="m", budget_usd=0.50)
    rec = fi.run_investigation(store, ep, inv, owner="p1", now=1001.0)
    assert rec["state"] == "accepted" and rec["cost_usd"] == 3.0
    (row,) = store._conn.execute("SELECT usd, price_basis, source FROM spend_ledger").fetchall()
    assert tuple(row) == (3.0, "reported", "flow_investigator")
    (cp,) = ca.checkpoints(store, ep["job_id"], "flow_investigation", ep["episode_id"])
    assert cp["outcome"] == "over_allowance"


def test_over_allowance_invalid_answer_stays_rejected(steward_home):
    store = Store(steward_home)
    ep = _stored_episode(store)
    runner = FakeRunner(lambda argv, cwd: CommandResult(
        argv, cwd, 0, claude_envelope(json.dumps(_answer(recommended_action="merge")), cost=3.0), "", 0.1))
    rec = fi.run_investigation(store, ep, fi.ClaudeFlowInvestigator(runner, model="m"), owner="p1", now=1001.0)
    assert rec["state"] == "rejected" and rec["reason"] == "unknown_action"


def test_partial_proposal_never_executes_but_evidence_is_kept_continuous_ownership_recovery_acceptance(steward_home):
    store = Store(steward_home)
    ep = _stored_episode(store)
    partial = json.dumps(_answer(recommended_action="nudge", instruction="run the suite"))
    inv = FakeInvestigator(fi.RawAnswer(partial, 0.50, outcome="allowance_exhausted"))
    rec = fi.run_investigation(store, ep, inv, owner="p1", now=1001.0)
    assert rec["state"] == "interrupted" and rec["reason"] == "allowance_exhausted" and rec["result"] is None
    assert store.flow_decisions(ep["job_id"]) == []
    (cp,) = ca.checkpoints(store, ep["job_id"], "flow_investigation", ep["episode_id"])
    assert cp["outcome"] == "allowance_exhausted" and "run the suite" in cp["partial"]["text"]
    assert not [e for e in store.events(ep["job_id"]) if e.what in ("needs_you", "approval_opened")]


def test_justified_larger_allowance_is_recorded_without_owner_ack_continuous_ownership_recovery_acceptance(steward_home):
    store = Store(steward_home)
    ep = _stored_episode(store)
    first = FakeInvestigator(fi.RawAnswer('{"root_cause": "partial', 0.50, outcome="allowance_exhausted"))
    first.budget_usd = 0.50
    fi.run_investigation(store, ep, first, owner="p1", now=1001.0)
    step = ca.next_step(store, ep["job_id"], call_site="flow_investigation", subject_key=ep["episode_id"],
                        max_allowance_usd=2.0)
    assert step["action"] == "continue" and step["allowance_usd"] == 1.0 and "partial evidence kept" in step["reason"]
    second = FakeInvestigator(fi.RawAnswer(json.dumps(_answer()), 0.30))
    rec = fi.continue_investigation(store, ep, second, owner="p2", now=1100.0, max_allowance_usd=2.0)
    assert rec["state"] == "accepted"
    assert "stopped at its allowance" in second.prompts[0] and "partial" in second.prompts[0]
    resumed = [e for e in store.events(ep["job_id"]) if e.what == "flow_investigation_resumed"]
    assert resumed and resumed[0].evidence["allowance_usd"] == 1.0 and resumed[0].evidence["acknowledged_by"] is None


def test_interrupted_investigation_resumes_from_checkpoint(steward_home):
    store = Store(steward_home)
    ep = _stored_episode(store)
    assert store.claim_flow_investigation(ep["episode_id"], ep["job_id"], owner="dead", now=1000.0, model="m")
    store.interrupt_flow_investigations(owner="alive")
    ca.checkpoint(store, ep["job_id"], call_site="flow_investigation", subject_key=ep["episode_id"],
                  result=ca.CallResult("interrupted", None, None, 0.5, "process_restarted"), partial=None, now=1001.0)
    out = fi.continue_investigation(store, ep, FakeInvestigator(), owner="alive", now=1002.0, max_allowance_usd=2.0)
    assert out["state"] == "accepted"
    assert [c["attempt"] for c in ca.checkpoints(store, ep["job_id"], "flow_investigation", ep["episode_id"])] == [1, 2]


def test_repeated_exhaustion_without_progress_requests_strategy_review(steward_home, monkeypatch):
    store = Store(steward_home)
    ep = _stored_episode(store)
    asked = []
    monkeypatch.setattr("steward.ownership.request_strategy_review",
                        lambda store, job_id, **kw: asked.append((job_id, kw)) or None)
    same = fi.RawAnswer('{"root_cause": "same partial', 0.5, outcome="allowance_exhausted")
    fi.run_investigation(store, ep, FakeInvestigator(same), owner="p1", now=1001.0)
    fi.continue_investigation(store, ep, FakeInvestigator(same), owner="p1", now=1002.0, max_allowance_usd=2.0)
    step = ca.next_step(store, ep["job_id"], call_site="flow_investigation", subject_key=ep["episode_id"],
                        max_allowance_usd=2.0)
    assert step["action"] == "strategy_review"
    assert fi.continue_investigation(store, ep, FakeInvestigator(same), owner="p1", now=1003.0,
                                     max_allowance_usd=2.0) is None
    assert asked and "without progress" in asked[0][1]["reason"]


def test_continuation_respects_an_owner_hard_cap(steward_home):
    store = Store(steward_home)
    ep = _stored_episode(store)
    fi.run_investigation(store, ep, FakeInvestigator(fi.RawAnswer("{", 0.5, outcome="allowance_exhausted")),
                         owner="p1", now=1001.0)
    sp.set_limit(store, ep["job_id"], "supervision", 0.9, binding="hard", provenance="owner_explicit",
                 source={"via": "spend-limit"}, now=1001.0)
    step = ca.next_step(store, ep["job_id"], call_site="flow_investigation", subject_key=ep["episode_id"],
                        max_allowance_usd=2.0)
    assert step["action"] == "wait" and "would_exceed_hard_cap" in step["reason"]
