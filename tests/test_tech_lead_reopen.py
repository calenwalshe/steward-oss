# tests/test_tech_lead_reopen.py
"""The tech lead's `reopen` (spec 2026-10-04 §5): only a stuck NEEDS_HUMAN hold, confidence >= policy, capped by
fix_rounds, through the in-pane send-back seam; every block escalates with the proposed text."""
from __future__ import annotations

import json

import pytest

from steward import hold, sendback
from steward import tech_lead_actions as tla
from steward import tech_lead_floor as floor
from steward import tech_lead_policy as tpol
from steward.models import JobState, Judgment
from steward.runner_exec import SubprocessRunner
from steward.store import Store
from test_runner import _contracted_job
from test_tech_lead_no_clog import ASTRA


def _parked(store, *, marker_rule="idle_after_final_message"):
    jid = _contracted_job(store, "fix it. Finish line: `pytest -q`").job_id
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    store.set_state(jid, JobState.WORKER_FINISHED, what="t", evidence={"marker": marker_rule.upper()},
                    rule=marker_rule, judgment=Judgment.code)
    store.set_state(jid, JobState.NEEDS_HUMAN, what="acceptance unresolved", evidence={}, rule="acceptance_unresolved",
                    judgment=Judgment.code)
    wt = store.home / "worktrees" / jid
    wt.mkdir(parents=True, exist_ok=True)
    return jid, wt


def _prop(confidence=0.9, text="restore the deleted test, then rerun verify", **extra):
    params = {"text": text, "evidence": "removed_test_function", "confidence": confidence}
    params.update(extra)
    return json.dumps({"proposal_id": "p1", "trigger_id": "t", "confidence": confidence, "judgment": False,
                       "signature": "stuck:abc", "plan_item": "", "actions": [{"type": "reopen", "params": params}]})


def _run(store, jid, wt, raw, *, policy=None, send_back=None, notes=None):
    notes = notes if notes is not None else []
    calls = []

    def sb(job_id, text, origin, request_id, judgment):
        calls.append({"job_id": job_id, "text": text, "origin": origin, "judgment": judgment})
        if send_back is not None:
            return send_back(job_id, text, origin, request_id, judgment)
        return sendback.send_back(store, job_id, text, origin=origin, request_id=request_id, judgment=judgment)

    ex = tla.ActionExecutor(store, runner_exec=SubprocessRunner(), notify_fn=lambda *a: notes.append(a),
                            send_back_fn=sb)
    prop = tla.parse_job_proposal(raw)
    pol = policy or tpol.Policy()
    ctx = tla.ActionContext(jid, ASTRA.id, ASTRA.model, prop, wt, pol, entry=ASTRA, facts=[{"kind": "parked_stuck"}])
    fctx = floor.context_for(store, jid, wt)
    out = []
    for a in prop.actions:
        decision, why, extra = tla.gate(a, ctx, fctx, store=store)
        if decision == "refused":
            out.append(ex.refuse(ctx, a, why, extra["class"]))
        elif decision == "policy_blocked":
            out.append(ex.record(ctx, a, "policy_blocked", why))
        else:
            out.append(ex.execute(ctx, a))
    return out, ex, calls, notes


def test_parse_accepts_reopen_with_its_three_params():
    p = tla.parse_job_proposal(_prop())
    assert p.actions[0].type == "reopen" and set(p.actions[0].params) == {"text", "evidence", "confidence"}
    with pytest.raises(tla.ProposalError):
        tla.parse_job_proposal(_prop(files=["x"]))


def test_stuck_hold_at_policy_confidence_is_reopened_through_the_seam(steward_home):
    store = Store(steward_home)
    jid, wt = _parked(store)
    out, ex, calls, _ = _run(store, jid, wt, _prop(confidence=0.8))
    assert out[0]["outcome"] == "executed"
    assert calls[0]["origin"] == f"{tla.ORIGIN}:{ASTRA.id}"
    assert calls[0]["judgment"] == Judgment.model_informed_code_authorized
    assert calls[0]["text"].startswith(f"Tech lead ({ASTRA.id}): ")
    assert store.get_job(jid).state == JobState.RUNNING
    ev = [e for e in store.events(jid) if e.what == "tech_lead_reopen"][-1]
    assert ev.evidence["hold_kind"] == "stuck" and ev.evidence["confidence"] == 0.8
    assert tla.reopens_executed(store, jid) == 1


def test_below_confidence_is_policy_blocked(steward_home):
    store = Store(steward_home)
    jid, wt = _parked(store)
    out, _, calls, _ = _run(store, jid, wt, _prop(confidence=0.79))
    assert out[0]["outcome"] == "policy_blocked" and "confidence" in out[0]["detail"] and not calls
    assert store.get_job(jid).state == JobState.NEEDS_HUMAN


def test_missing_or_non_numeric_confidence_is_zero(steward_home):
    """Review focus 3."""
    store = Store(steward_home)
    jid, wt = _parked(store)
    raw = json.loads(_prop())
    raw["actions"][0]["params"]["confidence"] = "high"
    out, _, calls, _ = _run(store, jid, wt, json.dumps(raw))
    assert out[0]["outcome"] == "policy_blocked" and not calls
    del raw["actions"][0]["params"]["confidence"]
    out, _, calls, _ = _run(store, jid, wt, json.dumps(raw))
    assert out[0]["outcome"] == "policy_blocked" and not calls


@pytest.mark.legacy_ownership_off  # Plan 1 G18: acceptance decisions remain owner-owned under the legacy path
def test_acceptance_hold_is_never_reopened(steward_home):
    store = Store(steward_home)
    jid, wt = _parked(store, marker_rule="steward_done_marker")
    out, _, calls, _ = _run(store, jid, wt, _prop(confidence=0.99))
    assert out[0]["outcome"] == "policy_blocked" and "hold_kind=acceptance" in out[0]["detail"] and not calls


def test_running_job_is_not_parked(steward_home):
    store = Store(steward_home)
    jid, wt = _parked(store)
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="operator_send_back", judgment=Judgment.human)
    out, _, calls, _ = _run(store, jid, wt, _prop())
    assert out[0]["outcome"] == "policy_blocked" and "not parked" in out[0]["detail"] and not calls


def test_reopen_off_in_policy_is_blocked(steward_home):
    store = Store(steward_home)
    jid, wt = _parked(store)
    out, _, calls, _ = _run(store, jid, wt, _prop(), policy=tpol.Policy(parked_reopen=False))
    assert out[0]["outcome"] == "policy_blocked" and "reopen off" in out[0]["detail"] and not calls


def test_fix_rounds_caps_reopens_per_job(steward_home):
    store = Store(steward_home)
    jid, wt = _parked(store)
    for _ in range(2):
        out, *_ = _run(store, jid, wt, _prop())
        assert out[0]["outcome"] == "executed"
        store.kv_set(jid, sendback.REWORK_KEY, None)  # the attempt launched and parked again
        store.set_state(jid, JobState.WORKER_FINISHED, what="t", evidence={"marker": "idle_after_final_message"},
                        rule="idle_after_final_message", judgment=Judgment.code)
        store.set_state(jid, JobState.NEEDS_HUMAN, what="acceptance unresolved", evidence={},
                        rule="acceptance_unresolved", judgment=Judgment.code)
    out, _, calls, _ = _run(store, jid, wt, _prop())
    assert out[0]["outcome"] == "policy_blocked" and "fix_rounds" in out[0]["detail"]


def test_rework_pending_is_blocked(steward_home):
    store = Store(steward_home)
    jid, wt = _parked(store)
    store.kv_set(jid, sendback.REWORK_KEY, {"rev": 1, "text": "x", "goal_append": False, "by": "cli"})
    out, _, calls, _ = _run(store, jid, wt, _prop())
    assert out[0]["outcome"] == "policy_blocked" and "rework pending" in out[0]["detail"] and not calls


def test_push_in_reopen_text_is_a_floor_hit(steward_home):
    store = Store(steward_home)
    jid, wt = _parked(store)
    out, _, calls, notes = _run(store, jid, wt, _prop(text="fix the test then git push origin main"))
    assert out[0]["outcome"] == "refused" and out[0]["floor_class"] == "outside_world" and not calls


def test_unblock_and_takeover_are_blocked_on_a_parked_job(steward_home):
    store = Store(steward_home)
    jid, wt = _parked(store)
    raw = json.dumps({"proposal_id": "p1", "trigger_id": "t", "confidence": 0.9, "judgment": False, "signature": "s",
                      "plan_item": "", "actions": [{"type": "unblock", "params": {"decision": "clear", "text": "go"}},
                                                   {"type": "takeover", "params": {"instructions": "edit", "files": ["a.py"], "summary": "s"}}]})
    out, *_ = _run(store, jid, wt, raw)
    assert [o["outcome"] for o in out] == ["policy_blocked", "policy_blocked"]
    assert all("parked" in o["detail"] for o in out)


def test_launch_failure_records_failed_and_does_not_count(steward_home):
    """Review focus 5."""
    store = Store(steward_home)
    jid, wt = _parked(store)

    def failing(job_id, text, origin, request_id, judgment):
        return {"accepted": False, "detail": "no Herdr workspace known for this job"}
    out, _, _, notes = _run(store, jid, wt, _prop(), send_back=failing)
    assert out[0]["outcome"] == "failed" and "no Herdr workspace" in out[0]["detail"]
    assert tla.reopens_executed(store, jid) == 0
    assert store.get_job(jid).state == JobState.NEEDS_HUMAN


@pytest.mark.legacy_ownership_off  # Plan 1 G21: blocked reopen requests strategy review under ownership
def test_blocked_reopen_escalates_with_the_proposed_text(steward_home):
    """fleet_tech_lead._finish: a blocked/refused reopen still reaches the owner as a ready instruction."""
    from steward import fleet_runtime as rt
    from steward import fleet_tech_lead as tl
    store = Store(steward_home)
    jid, wt = _parked(store, marker_rule="steward_done_marker")
    notes = []
    ex = tla.ActionExecutor(store, runner_exec=SubprocessRunner(), notify_fn=lambda *a: notes.append(a),
                            send_back_fn=lambda *a: {"accepted": True})
    lead = tl.JobTechLead(store, [ASTRA], ex, arbiter=rt.Arbiter(store), invoke=lambda e, p, t: _prop(),
                          available=lambda e: None, policy_fn=lambda: tpol.Policy())
    lead.on_event(jid, sweep=True, reason="parked_stuck")
    import time
    for _ in range(300):
        res = lead.on_event(jid, sweep=True, reason="parked_stuck")
        if res.get("status") == "acted":
            break
        time.sleep(0.01)
    esc = [e for e in store.events(jid) if e.what == "tech_lead_escalated"]
    assert esc and esc[-1].evidence["kind"] == "tech_lead_reopen_blocked"
    assert "restore the deleted test" in esc[-1].evidence["text"]
    assert store.get_job(jid).state == JobState.NEEDS_HUMAN


def test_packet_for_a_parked_job_carries_the_hold_and_restricts_actions(steward_home):
    from steward import fleet_runtime as rt
    from steward import fleet_tech_lead as tl
    store = Store(steward_home)
    jid, wt = _parked(store)
    store.append_event(jid, what="yield_classified", evidence={"lane": "STOPPED", "failed_preconditions": ["no_new_commit"], "seq": 3, "sha": "abc",
                                                      "last_said": "waiting for Sam to check the unit"},
                       rule="yield_classification", judgment=Judgment.code)
    ex = tla.ActionExecutor(store, runner_exec=SubprocessRunner())
    lead = tl.JobTechLead(store, [ASTRA], ex, arbiter=rt.Arbiter(store), policy_fn=lambda: tpol.Policy())
    pkt = lead.packet(jid, [{"kind": "parked_stuck", "source": "control_pane", "data": {}}], "trg-1")
    assert pkt["state"] == "NEEDS_HUMAN" and pkt["hold_kind"] == "stuck"
    assert pkt["last_said"] == "waiting for Sam to check the unit"
    assert pkt["allowed_actions"] == ["reopen", "resolve_blocker", "wait", "escalate"]
    assert pkt["policy"]["parked_reopen"] is True and pkt["policy"]["parked_cadence_min"] == 10
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="operator_send_back", judgment=Judgment.human)
    pkt = lead.packet(jid, [], "trg-2")
    assert "hold_kind" not in pkt and pkt["allowed_actions"] == ["unblock", "send_back", "takeover", "escalate",
                                                                   "reopen", "resolve_blocker", "wait", "diagnose"]
