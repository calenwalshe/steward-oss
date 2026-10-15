"""Clean-install finding 2026-10-14: a tech lead ruled a finished job with `"instructions": ""` (nothing to tell
the worker) and the proposal was rejected as malformed (`bad_field(rule_completion instructions)`), so a job with
passing checks fell to a human. Empty instructions are a valid "nothing further" answer."""
from __future__ import annotations

import json

import pytest

from steward import tech_lead_actions as tla


def _prop(**params):
    action = {"type": "rule_completion", "params": {"ruling": "finished", "sha": "abc", "evidence": [], **params}}
    return json.dumps({"proposal_id": "p1", "trigger_id": "t", "confidence": 0.95, "judgment": False,
                       "signature": "sig", "plan_item": "", "actions": [action]})


@pytest.mark.parametrize("instructions", ["", "   ", None])
def test_empty_instructions_parse(instructions):
    p = tla.parse_job_proposal(_prop(instructions=instructions))
    assert p.actions[0].type == "rule_completion"


def test_overlong_instructions_are_still_refused():
    with pytest.raises(tla.ProposalError):
        tla.parse_job_proposal(_prop(instructions="x" * 4001))
