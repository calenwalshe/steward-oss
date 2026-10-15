"""u3 (stall fixes 2026-10-14): once a signature is exhausted the tech lead stops paying for reviews of it and the
owner is asked once. Evidence: job cce9f6e8 had 16 `tech_lead_review_started` and 13 `ownership_strategy_review`
"tech lead exhausted: third sighting of no_progress_for:da39a3ee5e (tried: send_back)", one every ~20 min for hours:
after MAX_STRATEGY_REVIEWS the strategy request was a no-op, nothing reached the owner, and every sweep paid a review.
Escalation is checked locally: approvals/blockers only, the notice path is a recording fake (no message is sent)."""
from __future__ import annotations

import json
import time

from steward import approvals, blockers, code_facts
from steward import fleet_runtime as rt
from steward import fleet_tech_lead as tl
from steward import liveness
from steward import tech_lead_actions as tla
from steward import tech_lead_notebook as nb
from steward import tech_lead_policy as tpol
from steward.models import JobState, Judgment
from steward.store import Store
from test_runner import _contracted_job
from test_tech_lead_no_clog import ASTRA, FABLE, wait_done

import pytest


@pytest.fixture
def env(steward_home, monkeypatch, tmp_path):
    from steward import supervision_allowance as sa
    monkeypatch.setattr(liveness, "PROJECTS_DIR", tmp_path / "projects")
    # the live job's reviews were ~20 min apart; the hourly review cap is not what is under test here
    monkeypatch.setattr(sa, "defer_reason", lambda *a, **k: None)
    store = Store(steward_home)
    job = _contracted_job(store, "fix it")
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    (store.home / "worktrees" / job.job_id).mkdir(parents=True)
    return store, job.job_id


def _answer(i):
    return json.dumps({"proposal_id": f"p-{i}-{time.time_ns()}", "trigger_id": "t", "confidence": 0.9,
                       "strategy": f"strategy-{i}", "actions": [{"type": "send_back", "params": {"text": f"try {i}"}}]})


def _lead(store):
    arb = rt.Arbiter(store)
    notices = []
    ex = tla.ActionExecutor(store, runner_exec=None, notify_fn=lambda *a: notices.append(a))
    calls = []

    def invoke(e, prompt, timeout):
        calls.append(e.id)
        return _answer(len(calls))
    lead = tl.JobTechLead(store, [ASTRA, FABLE], ex, arbiter=arb, invoke=invoke, available=lambda e: None,
                          policy_fn=lambda: tpol.Policy(), memory=nb.Memory(store))
    return lead, arb, calls, notices


def _review(lead, arb, jid):
    arb.submit(jid, code_facts.SOURCE, "new_failures_repeated", key=f"k{time.time_ns()}", test="tests.t::a")
    res = lead.on_event(jid)
    for _ in range(5):
        if res["status"] != "review_started":
            return res
        wait_done(lead, jid)
        res = lead.on_event(jid)
    return res


def _exhaust(lead, arb, jid):
    out = []
    for _ in range(12):
        out.append(_review(lead, arb, jid))
        if out[-1].get("status") == "skipped":
            break
    return out


def _lead_escalations(store, jid):
    return [b for b in blockers.open_blockers(store, jid) if b.kind == "lead_escalation"]


def test_exhausted_signature_escalates_once_and_stops_paying_for_reviews(env):
    store, jid = env
    lead, arb, calls, notices = _lead(store)
    results = _exhaust(lead, arb, jid)
    assert results[-1] == {"status": "skipped", "reason": "signature_exhausted",
                           "signature": nb.signature([{"kind": "new_failures_repeated",
                                                       "data": {"test": "tests.t::a"}}])}
    # exactly one owner escalation, with the facts
    esc = _lead_escalations(store, jid)
    assert len(esc) == 1 and esc[0].disposition == "confirmed_human"
    assert "exhausted" in esc[0].claim and "new_failures_repeated" in esc[0].claim
    pend = [a for a in approvals.list_approvals(store, jid, status="pending") if a["kind"] == "tech_lead"]
    assert len(pend) == 1
    assert len(notices) == 1
    # more sightings of the same signature: no model call, no second escalation
    paid = len(calls)
    for _ in range(3):
        assert _review(lead, arb, jid)["status"] == "skipped"
    assert len(calls) == paid
    assert len(_lead_escalations(store, jid)) == 1 and len(notices) == 1
    skips = [e for e in store.events(jid) if e.what == "tech_lead_review_skipped_exhausted"]
    assert len(skips) == 1  # logged once, not per sweep


def test_a_change_in_the_job_reopens_the_exhausted_signature(env):
    store, jid = env
    lead, arb, calls, _notices = _lead(store)
    _exhaust(lead, arb, jid)
    paid = len(calls)
    assert _review(lead, arb, jid)["status"] == "skipped"
    # the worker does something: the signature may be reviewed again
    store.append_event(jid, what="worker reported done (typed frame)", evidence={}, rule="fabric_done_frame",
                       judgment=Judgment.code)
    assert _review(lead, arb, jid)["status"] != "skipped"
    assert len(calls) > paid
