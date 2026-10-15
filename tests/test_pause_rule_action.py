# tests/test_pause_rule_action.py
"""Spec 2026-10-13 §4.4: the tech lead's one pause action `rule_pause` — strict parse, gated (only for a pause review,
floor first, exempt from the judgment-confidence rule), never outbound, executed through apply_ruling."""
from __future__ import annotations

import json

import pytest

from pause_support import ASTRA, NEW, Unit, set_pause, steers
from test_runner_fabric import _contracted_job, _yield_git

from steward import pause_checkpoint
from steward import pause_review as pr
from steward import tech_lead_actions as tla
from steward import tech_lead_floor as floor
from steward import tech_lead_policy as tpol
from steward.models import JobState, Judgment
from steward.store import Store

PAUSE_FACT = {"kind": "worker_paused", "source": "pause",
              "data": {"seq": 9, "sha": NEW, "yield_lane": "CANDIDATE", "failed": [], "unit_id": "u1",
                       "done_claim": False, "next_unit": "u2", "pause_kind": "pause", "pause_seq": 1,
                       "said": "STEWARD-PAUSE unit=u1 next=u2 parser"}}


def _prop(params, *, judgment=False, confidence=0.9):
    return json.dumps({"proposal_id": "p1", "trigger_id": "t", "confidence": confidence, "judgment": judgment,
                       "signature": "pause", "actions": [{"type": "rule_pause", "params": params}]})


def _job(steward_home, units=("u1", "u2")):
    store = Store(steward_home)
    job = _contracted_job(store)
    set_pause(store, job.job_id, "unit", [Unit(u, f"title {u}") for u in units])
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    (store.home / "worktrees" / job.job_id).mkdir(parents=True)
    pause_checkpoint.update(store, job.job_id, lambda c: c.update(
        awaiting_review=True, review_requested_at=1.0, pause_seq=1, unit_id="u1",
        last_pause_fact=PAUSE_FACT["data"]))
    return store, job.job_id


def _ctx(store, jid, params, facts=(PAUSE_FACT,), **kw):
    proposal = tla.parse_job_proposal(_prop(params, **kw))
    wt = store.home / "worktrees" / jid
    return proposal, tla.ActionContext(jid, ASTRA.id, ASTRA.model, proposal, wt, tpol.Policy(), entry=ASTRA,
                                       facts=list(facts), trigger_id="t")


def test_parse_accepts_each_outcome_and_rule_pause_is_never_ordinary():
    for p in ({"outcome": "continue", "reason": "ok", "next_unit": {"id": "u2", "title": "cli", "finish_line": ""}},
              {"outcome": "send_back", "reason": "no", "text": "add the test"},
              {"outcome": "done", "reason": "all met"},
              {"outcome": "ask_owner", "reason": "value call", "decision": "Drop Python 3.10?"}):
        assert tla.parse_job_proposal(_prop(p)).actions[0].type == "rule_pause"
    assert "rule_pause" in tla.JOB_ACTION_TYPES and "rule_pause" not in tla.ORDINARY_ACTION_TYPES


@pytest.mark.parametrize("params", [
    {"outcome": "maybe", "reason": "x"},
    {"outcome": "send_back", "reason": "x"},
    {"outcome": "ask_owner", "reason": "x"},
    {"outcome": "continue", "reason": "x", "next_unit": {"id": "bad id"}},
    {"outcome": "continue", "reason": "x", "drop_units": ["ok", "not ok"]},
    {"outcome": "continue"},
])
def test_parse_refuses_malformed_rulings(params):
    with pytest.raises(tla.ProposalError):
        tla.parse_job_proposal(_prop(params))


def test_gate_allows_rule_pause_only_for_a_pause_review(steward_home):
    store, jid = _job(steward_home)
    proposal, ctx = _ctx(store, jid, {"outcome": "done", "reason": "ok"}, judgment=True, confidence=0.1)
    fctx = floor.context_for(store, jid, ctx.worktree)
    assert tla.gate(proposal.actions[0], ctx, fctx, store=store)[0] == "execute"   # judgment rule does not apply
    proposal, ctx = _ctx(store, jid, {"outcome": "done", "reason": "ok"}, facts=({"kind": "jev_flag"},))
    decision, _why, extra = tla.gate(proposal.actions[0], ctx, fctx, store=store)
    assert decision == "refused" and extra["class"] == "prohibited"


def test_outbound_worker_direction_is_caught_but_an_owner_question_is_not():
    send = tla.parse_job_proposal(_prop({"outcome": "send_back", "reason": "x", "text": "then git push the branch"}))
    ask = tla.parse_job_proposal(_prop({"outcome": "ask_owner", "reason": "x",
                                        "decision": "May the worker git push the release branch?"}))
    assert tla.find_outbound(send.actions[0]) and tla.find_outbound(ask.actions[0]) is None


def test_executor_applies_the_ruling(steward_home):
    store, jid = _job(steward_home)
    ex = tla.ActionExecutor(store, runner_exec=_yield_git(), clock=lambda: 61.0)
    proposal, ctx = _ctx(store, jid, {"outcome": "continue", "reason": "ok"})
    out = ex.execute(ctx, proposal.actions[0])
    assert out["outcome"] == "executed" and out["outcome_ruled"] == "continue"
    assert pause_checkpoint.load(store, jid)["verdict"] == "continue"
    assert "Go: unit u2" in steers(store, jid, "tech_lead")[0]["text"]


def test_executor_without_git_turns_done_into_send_back(steward_home):
    store, jid = _job(steward_home, units=("u1",))
    ex = tla.ActionExecutor(store, runner_exec=None, clock=lambda: 61.0)
    proposal, ctx = _ctx(store, jid, {"outcome": "done", "reason": "ok"})
    out = ex.execute(ctx, proposal.actions[0])
    assert out["outcome_ruled"] == "send_back" and "HEAD is unknown" in out["detail"]


def test_packet_section_carries_unit_diff_facts_instruction_and_history(steward_home):
    store, jid = _job(steward_home)
    pause_checkpoint.append_fact(store, jid, source="flow", kind="steer_nudge", text="look at the log", at=1.0)
    pause_checkpoint.update(store, jid, lambda c: c.update(open_instruction="Tech lead: go u1",
                                                            history=[{"unit_id": "u1", "outcome": "send_back"}]))
    calls = []

    def git_text(job_id, *args, cap=0):
        calls.append(args)
        return " ".join(args)

    sec = pr.packet_section(store, jid, [PAUSE_FACT], git_text=git_text)
    assert sec["unit"] == {"id": "u1", "title": "title u1", "finish_line": ""}
    assert [u["id"] for u in sec["units"]] == ["u1", "u2"] and sec["sendbacks_for_unit"] == 1
    assert sec["diff_stat"] == f"diff --stat {{base}}..{NEW}" and sec["worker_said"].startswith("STEWARD-PAUSE")
    assert sec["pending_facts"][0]["text"] == "look at the log" and sec["open_instruction"] == "Tech lead: go u1"
    assert sec["outcomes"] == list(pr.OUTCOMES)


def test_prompt_asks_for_one_rule_pause_and_copies_the_trigger():
    text = pr.prompt({"trigger_id": "trg-1", "allowed_actions": ["rule_pause"], "pause": {"kind": "pause"}})
    assert "exactly ONE action of type rule_pause" in text and "trigger_id: trg-1" in text
    assert '"allowed_actions": ["rule_pause"]' in text
