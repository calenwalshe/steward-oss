"""SUP-14 / PRD D-06: the per-job, append-only notebook.

Signature = normalized (fact kind, failing check / ask / file) hash. A signature seen before forces a different
strategy (a proposal repeating a failed strategy is rejected); a third sighting escalates. `steward tech-lead notes`
prints it."""

from __future__ import annotations

import json
import time

import pytest

from steward import approvals, code_facts
from steward import fleet_runtime as rt
from steward import fleet_tech_lead as tl
from steward import tech_lead_actions as tla
from steward import tech_lead_notebook as nb
from steward import tech_lead_policy as tpol
from steward.models import JobState, Judgment
from steward.store import Store
from test_runner import _contracted_job
from test_tech_lead_no_clog import ASTRA, FABLE, Clock, wait_done


@pytest.fixture
def env(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store, "fix it")
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    (store.home / "worktrees" / job.job_id).mkdir(parents=True)
    return store, job.job_id


def test_signature_is_normalized_and_stable():
    a = nb.signature([{"kind": "new_failures_repeated", "data": {"test": "tests.test_x::test_a"}}])
    b = nb.signature([{"kind": "new_failures_repeated", "data": {"test": " tests.test_x::test_a "}},
                      {"kind": "thrash", "data": {}}])
    c = nb.signature([{"kind": "new_failures_repeated", "data": {"test": "tests.test_x::test_b"}}])
    assert a == b and a != c and a.startswith("new_failures_repeated:")
    ask = nb.signature([{"kind": "permission_ask_repeated", "data": {"ask": "write(notes.md)"}}])
    assert ask.startswith("permission_ask_repeated:")


def test_append_only_and_lookup(env):
    store, jid = env
    nb.append(store, jid, signature="s1", action="send_back", strategy="send_back", outcome="pending",
              model="gpt-6-astra", lead="astra-tech-lead", plan_item="33-01 task 2", evidence_refs=["fact:3"],
              trigger_id="t1")
    nb.append(store, jid, signature="s2", action="unblock", strategy="unblock", outcome="pending", model="m",
              lead="l", trigger_id="t2")
    assert [e["signature"] for e in nb.entries(store, jid)] == ["s1", "s2"]
    assert [e["action"] for e in nb.lookup(store, jid, "s1")] == ["send_back"]
    nb.set_outcome(store, jid, "s1", "failed")  # outcomes are appended as new rows, never rewritten
    rows = nb.entries(store, jid)
    assert len(rows) == 3 and rows[-1]["outcome"] == "failed" and rows[0]["outcome"] == "pending"
    assert nb.sightings(store, jid, "s1") == 1 and nb.failed_strategies(store, jid, "s1") == {"send_back"}
    with pytest.raises(AttributeError):
        nb.update  # noqa: B018 - there is no update/delete API


def _lead(store, answers, memory):
    arb = rt.Arbiter(store)
    ex = tla.ActionExecutor(store, runner_exec=None)
    queue = list(answers)
    lead = tl.JobTechLead(store, [ASTRA, FABLE], ex, arbiter=arb, invoke=lambda e, p, t: queue.pop(0),
                          available=lambda e: None, policy_fn=lambda: tpol.Policy(), memory=memory)
    return lead, arb


def sb(strategy="send_back", text="fix it"):
    return json.dumps({"proposal_id": f"p-{time.time_ns()}", "trigger_id": "t", "confidence": 0.9,
                       "strategy": strategy, "signature": "model-name-ignored",
                       "actions": [{"type": "send_back", "params": {"text": text}}]})


def esc():
    return json.dumps({"proposal_id": f"e-{time.time_ns()}", "trigger_id": "t", "confidence": 0.9,
                       "strategy": "escalate", "actions": [{"type": "escalate", "params": {"reason": "stuck"}}]})


def review(lead, arb, jid, test="tests.t::a"):
    arb.submit(jid, code_facts.SOURCE, "new_failures_repeated", key=f"k{time.time_ns()}", test=test)
    lead.on_event(jid)
    for _ in range(5):
        wait_done(lead, jid)
        res = lead.on_event(jid)
        if res["status"] != "review_started":
            return res
    return res


@pytest.mark.legacy_ownership_off  # Plan 1 G16: third sighting requests another strategy under ownership
def test_repeat_signature_rejects_a_failed_strategy_and_third_sighting_escalates(env):
    store, jid = env
    mem = nb.Memory(store)
    lead, arb = _lead(store, [sb(), sb(), sb(strategy="takeover_hint", text="different approach"), esc()], mem)
    first = review(lead, arb, jid)
    assert first["status"] == "acted"
    sig = nb.entries(store, jid)[0]["signature"]
    # the same signature comes back: the previous attempt failed; Astra repeats send_back -> rejected, Fable differs
    second = review(lead, arb, jid)
    assert second["status"] == "acted" and second["lead"] == "fable-tech-lead"
    assert nb.failed_strategies(store, jid, sig) == {"send_back"}
    rejected = [e for e in store.events(jid) if e.what == "tech_lead_strategy_rejected"]
    assert rejected and rejected[0].evidence["strategy"] == "send_back"
    third = review(lead, arb, jid)
    assert third["status"] == "escalated"
    pend = [a for a in approvals.list_approvals(store, jid, status="pending") if a["kind"] == "tech_lead"]
    assert pend and "third sighting" in pend[-1]["question"]


def test_packet_includes_prior_attempts(env):
    store, jid = env
    mem = nb.Memory(store)
    lead, arb = _lead(store, [sb()], mem)
    review(lead, arb, jid)
    facts = [{"kind": "new_failures_repeated", "data": {"test": "tests.t::a"}}]
    pkt = lead.packet(jid, facts, "trg", tpol.Policy())
    assert pkt["notebook"]["signature"] == nb.signature(facts)
    assert pkt["notebook"]["prior_attempts"][0]["strategy"] == "send_back"


def test_checks_passing_marks_pending_attempts_succeeded(env):
    store, jid = env
    mem = nb.Memory(store)
    lead, arb = _lead(store, [sb()], mem)
    review(lead, arb, jid)
    store.append_event(jid, what="checks passed at exact commit", evidence={"commit_sha": "x"},
                       rule="checks_pass_at_sha", judgment=Judgment.code)
    mem.observe(jid)
    assert nb.entries(store, jid)[-1]["outcome"] == "succeeded"


def test_cli_notes_prints_entries(env, capsys):
    from steward import cli

    store, jid = env
    nb.append(store, jid, signature="new_failures_repeated:abc", action="takeover", strategy="takeover",
              outcome="succeeded", model="claude-fable-5-1", lead="fable-tech-lead", plan_item="35-01 task 4",
              trigger_id="t1")
    assert cli.main(["tech-lead", "notes", jid]) == 0
    out = capsys.readouterr().out
    assert "new_failures_repeated:abc" in out and "takeover" in out and "claude-fable-5-1" in out
    assert cli.main(["tech-lead", "notes", jid, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["action"] == "takeover"
