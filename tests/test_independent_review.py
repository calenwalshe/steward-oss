"""Plan 3 Task 10: independent diagnosis validates actionable mechanisms, waits, and exact owner requests."""
from __future__ import annotations

import json
import pytest

from ownership_fixtures import ASTRA, CONTROLLER, FABLE, LEAD, park_rule
from steward import attempts, blockers
from steward import independent_review as ir
from steward.store import Store


def test_select_prefers_a_different_model_and_discloses_fallback():
    assert ir.select_reviewer([ASTRA, FABLE], avoid_models={"gpt-6-astra"}, available=lambda e: None) == \
        (FABLE, "different_model")
    assert ir.select_reviewer([ASTRA], avoid_models={"gpt-6-astra"}, available=lambda e: None) == \
        (ASTRA, "same_model_fresh_context")
    assert ir.select_reviewer([ASTRA, FABLE], avoid_models=set(), available=lambda e: "down") == (None, "none")


def _v(**kw):
    base = {"verdict": "new_approach", "hypothesis": "the fixture path is wrong", "evidence_refs": ["event:3"],
            "confidence": 0.9, "mechanism": "point the test at the generated fixture",
            "change": "regenerate fixtures before the suite", "success_observation": "pytest -q tests/test_a.py exits 0",
            "instructions": "Run make fixtures, then re-run tests/test_a.py."}
    base.update(kw)
    return json.dumps(base)


def test_new_approach_needs_mechanism_and_observable_success():
    v = ir.parse_verdict(_v(), failed_approaches=set())
    assert v.verdict == "new_approach" and v.success_observation.startswith("pytest")
    for missing in ("mechanism", "change", "success_observation", "instructions"):
        with pytest.raises(ValueError, match=missing):
            ir.parse_verdict(_v(**{missing: " "}), failed_approaches=set())


def test_repeating_a_failed_approach_is_rejected():
    with pytest.raises(ValueError, match="repeats_failed_approach"):
        ir.parse_verdict(_v(change="Regenerate fixtures before the suite!"),
                         failed_approaches={attempts.approach_key("regenerate fixtures before the suite")})


def test_no_credible_action_needs_a_supported_wait():
    raw = {"verdict": "no_credible_action", "hypothesis": "vendor outage", "evidence_refs": [], "confidence": 0.7,
           "wait": {"kind": "time", "params": {"at": 5000}}}
    assert ir.parse_verdict(json.dumps(raw), failed_approaches=set()).wait == {"kind": "time", "params": {"at": 5000}}
    raw["wait"] = {"kind": "whenever", "params": {}}
    with pytest.raises(ValueError, match="wait"):
        ir.parse_verdict(json.dumps(raw), failed_approaches=set())


def test_confirm_human_needs_decision_and_why_authority_is_insufficient():
    raw = {"verdict": "confirm_human", "hypothesis": "needs the device", "evidence_refs": [], "confidence": 0.9,
           "human_request": {"decision": "Plug the Circuit Tracks into USB port 2",
                             "why_authority_insufficient": "a physical action no grant can perform"}}
    v = ir.parse_verdict(json.dumps(raw), failed_approaches=set())
    assert v.human_request.decision.startswith("Plug")
    raw["human_request"] = {"decision": "decide"}
    with pytest.raises(ValueError):
        ir.parse_verdict(json.dumps(raw), failed_approaches=set())


@pytest.mark.parametrize("bad,reason", [
    ("not json", "invalid_json"), (json.dumps([1]), "not_object"), (_v(extra=1), "unknown_fields"),
    (_v(verdict="merge_it"), "verdict"), (_v(confidence=1.5), "confidence"),
])
def test_malformed_answers_are_invocation_failures(bad, reason):
    with pytest.raises(ValueError, match=reason):
        ir.parse_verdict(bad, failed_approaches=set())


def test_packet_carries_evidence_attempts_constraints_and_claim(steward_home):
    store = Store(steward_home)
    jid = park_rule(store, "recovery_budget_exhausted", "recovery escalated to human")
    b, _ = blockers.raise_blocker(store, jid, kind="recovery_exhausted", subject="worker", claim="worker keeps dying",
                                  evidence=["event:1", "event:2"], origin_actor=CONTROLLER, now=1.0)
    blockers.record_diagnosis(store, b.blocker_id, {"by": "tech_lead", "summary": "flaky machine"}, now=2.0)
    a = attempts.record(store, jid, issue_key="recovery:worker", actor=LEAD, hypothesis="flaky", change="replace",
                        success_observation="progressing", now=3.0)
    attempts.resolve(store, a.attempt_id, "failed", detail={}, now=4.0)
    pkt = ir.packet(store, jid, b, issue_key="recovery:worker", now=5.0)
    assert pkt["blocker"]["claim"] == "worker keeps dying" and pkt["blocker"]["evidence"] == ["event:1", "event:2"]
    assert pkt["claimed_diagnosis"]["summary"] == "flaky machine"
    assert pkt["attempts"][0]["outcome"] == "failed" and pkt["failed_approaches"] == ["replace"]
    assert "explicit owner instructions bind" in pkt["constraints"]
    assert pkt["grants"] is not None or pkt["grants_note"] == "structured grants unavailable"
    text = ir.prompt(pkt, disclosure="same_model_fresh_context")
    assert "fresh context" in text and "untrusted" in text and len(text) <= ir.PROMPT_MAX_CHARS
