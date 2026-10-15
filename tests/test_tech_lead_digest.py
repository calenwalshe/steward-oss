"""SUP-16 / PRD D-10: NEEDS_YOU only for floor hits, final decisions or tech-lead exhausted; every autonomous
tech-lead decision appears in a "TECH LEAD" digest section of `steward result` (text + --json)."""

from __future__ import annotations

import json

import pytest

from steward import approvals, cli, session
from steward import tech_lead_actions as tla
from steward import tech_lead_policy as tpol
from steward.models import JobState, Judgment
from steward.store import Store
from test_runner import _contracted_job
from test_tech_lead_actions import env, prop, run  # noqa: F401 - the shared git-worktree fixture


def test_digest_lists_every_autonomous_decision(env):  # noqa: F811
    store, jid, wt = env
    rec = approvals.open_approval(store, jid, "worker permission: Write(notes.md)", kind="permission")
    run(store, jid, wt, prop({"type": "unblock", "params": {"approval_id": rec["id"], "decision": "clear",
                                                            "text": "go"}}, signature="permission_confusion"))
    run(store, jid, wt, prop({"type": "takeover", "params": {
        "instructions": "owner -> operator", "files": ["pkg/runtime.py"],
        "summary": "removed owner name from 1 comment"}}, signature="owner_name_leak"))
    res = session.build_result(store, jid)
    acts = res["tech_lead"]["actions"]
    assert [(a["action"], a["outcome"]) for a in acts] == [("unblock", "executed"), ("takeover", "started"),
                                                           ("takeover", "executed")]
    assert all(a["model"] == "gpt-6-astra" and a["lead"] == "astra-tech-lead" for a in acts)
    assert acts[0]["signature"] == "permission_confusion" and "plan_item" in acts[0]
    text = session.render_result(res)
    assert "TECH LEAD" in text
    assert "tech lead (astra-tech-lead, gpt-6-astra) took over: removed owner name from 1 comment" in text
    assert "cleared" in text.lower()


def test_digest_counts_escalations_and_why(env):  # noqa: F811
    store, jid, wt = env
    run(store, jid, wt, prop({"type": "push", "params": {}}))
    res = session.build_result(store, jid)
    assert res["tech_lead"]["escalations"] == [{"kind": "floor_hit", "text": res["tech_lead"]["escalations"][0]["text"]}]
    assert "NEEDS_YOU" in session.render_result(res) or "escalated" in session.render_result(res)


def test_result_json_has_the_digest(env, capsys):  # noqa: F811
    store, jid, wt = env
    run(store, jid, wt, prop({"type": "send_back", "params": {"text": "fix the comment"}}))
    assert cli.main(["result", jid, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tech_lead"]["actions"][0]["action"] == "send_back"


def test_legacy_result_is_unchanged_without_tech_lead_activity(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store, "plain job")
    res = session.build_result(store, job.job_id)
    assert "tech_lead" not in res and "TECH LEAD" not in session.render_result(res)


def test_needs_you_only_for_floor_hits_final_decisions_or_exhaustion(env):  # noqa: F811
    store, jid, wt = env
    # a permission outside the policy scope stays the owner's ordinary approval: no extra tech-lead escalation
    rec = approvals.open_approval(store, jid, "worker permission: Write(notes.md)", kind="permission")
    run(store, jid, wt, prop({"type": "unblock", "params": {"approval_id": rec["id"], "decision": "clear",
                                                            "text": "go"}}), policy=tpol.Policy(permissions="none"))
    # a judgment fix below the confidence bar is simply not taken: no escalation
    run(store, jid, wt, prop({"type": "send_back", "params": {"text": "x"}}, judgment=True, confidence=0.5))
    # a takeover that cannot happen becomes a send-back: no escalation
    run(store, jid, wt, prop({"type": "takeover", "params": {"instructions": "x", "files": ["pkg/runtime.py"]}}),
        writing=True)
    assert not [e for e in store.events(jid) if e.what == "tech_lead_escalated"]
    # a floor hit does escalate
    run(store, jid, wt, prop({"type": "merge", "params": {}}))
    kinds = [e.evidence["kind"] for e in store.events(jid) if e.what == "tech_lead_escalated"]
    assert kinds == ["floor_hit"]
    assert set(kinds) <= tla.NEEDS_YOU_KINDS
