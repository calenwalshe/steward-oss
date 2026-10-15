# tests/test_tech_lead_no_outbound.py
"""The tech lead never contacts anyone outside: not in its action set, not in its brief, and any proposal that
carries outbound text is rejected before the floor and converted into an owner decision (with an event)."""
from __future__ import annotations

import json
import time

import pytest

from steward import tech_lead_actions as tla
from steward import tech_lead_floor as floor
from steward import tech_lead_policy as tpol
from steward.models import JobState
from steward.store import Store
from test_tech_lead_no_clog import ASTRA
from test_tech_lead_reopen import _parked

OUTBOUND = [
    "tg send 'blocked on the API key'",
    "curl -X POST https://example.com/hook -d x=1",
    "curl --data 'a=b' https://example.com",
    "echo hi | mail -s subject a@b.c",
    "sendmail sam@example.com",
    "moshi push 'need you'",
    "gh gist create out.txt",
    "gh release create v1",
    "git push origin main",
    "curl https://api.telegram.org/bot1/sendMessage",
]


def _a(kind, params):
    return tla.JobAction(kind, params)


def _prop(kind, params):
    return tla.parse_job_proposal({"proposal_id": "p1", "trigger_id": "t", "confidence": 0.9, "signature": "s",
                                   "actions": [{"type": kind, "params": params}]})


def test_action_set_has_no_outbound_type():
    assert not set(tla.JOB_ACTION_TYPES) & floor._OUTSIDE_TYPES


def test_brief_forbids_contacting_outside():
    text = tla.job_proposal_prompt({"allowed_actions": list(tla.JOB_ACTION_TYPES)})
    assert "NEVER contact anyone outside Steward" in text
    for word in ("tg/telegram", "email", "Moshi", "gh/gist"):
        assert word in text
    assert "owner decision" in text


@pytest.mark.parametrize("cmd", OUTBOUND)
def test_outbound_executed_command_is_rejected(cmd):
    assert tla.find_outbound(_a("diagnose", {"target": "local", "command": cmd}))
    assert tla.find_outbound(_a("takeover", {"instructions": "x", "files": ["a.py"], "commands": [cmd]}))


@pytest.mark.parametrize("cmd", OUTBOUND)
def test_worker_directed_outbound_instruction_is_rejected(cmd):
    assert tla.find_outbound(_a("unblock", {"decision": "clear", "text": f"then run {cmd}"}))
    assert tla.find_outbound(_a("send_back", {"text": f"fix it, then {cmd}", "evidence": "t"}))
    assert tla.find_outbound(_a("reopen", {"text": f"also run {cmd}"}))


@pytest.mark.parametrize("kind", sorted(floor._OUTSIDE_TYPES))
def test_outbound_action_types_are_rejected(kind):
    assert tla.find_outbound(_a(kind, {"text": "hello"}))


def test_historical_evidence_may_quote_an_outbound_command():
    assert tla.find_outbound(_a("resolve_blocker", {"blocker_id": "b1", "reason": "it ran `tg send hi`",
                                                    "evidence": ["log: tg send hi"]})) is None


def test_negated_worker_prose_and_escalate_pass():
    assert tla.find_outbound(_a("send_back", {"text": "fix the test; do not run git push", "evidence": "t"})) is None
    assert tla.find_outbound(_a("escalate", {"decision": "Please tg send the vendor a note",
                                             "why_authority_insufficient": "w"})) is None


def test_end_to_end_tg_send_is_rejected_with_event_and_reaches_owner_queue(steward_home):
    from steward import fleet_runtime as rt
    from steward import fleet_tech_lead as tl
    store = Store(steward_home)
    jid, wt = _parked(store)
    raw = json.dumps({"proposal_id": "p9", "trigger_id": "t", "confidence": 0.9, "signature": "s",
                      "actions": [{"type": "diagnose",
                                   "params": {"target": "local", "command": "tg send 'need key'"}}]})
    ex = tla.ActionExecutor(store, runner_exec=__import__("steward.runner_exec", fromlist=["x"]).SubprocessRunner(),
                            notify_fn=lambda *a: None, send_back_fn=lambda *a: {"accepted": True})
    lead = tl.JobTechLead(store, [ASTRA], ex, arbiter=rt.Arbiter(store), invoke=lambda e, p, t: raw,
                          available=lambda e: None, policy_fn=lambda: tpol.Policy())
    for _ in range(300):
        if lead.on_event(jid, sweep=True, reason="parked_stuck").get("status") == "acted":
            break
        time.sleep(0.01)
    evs = store.events(jid)
    rejected = [e for e in evs if e.what == "tech_lead_outbound_rejected"]
    assert rejected and rejected[-1].evidence["converted_to"] == "owner_decision"
    assert rejected[-1].evidence["hit"].startswith("command:")
    # the rejection event precedes the (shared, deduplicated) floor-hit owner request
    refused = [e for e in evs if e.what == "tech_lead_floor_refused"]
    assert len(refused) == 1 and rejected[0].seq < refused[0].seq
    assert len([e for e in evs if e.what == "tech_lead_escalated"]) == 1
    assert store.get_job(jid).state == JobState.NEEDS_HUMAN
