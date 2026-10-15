"""Plan 1 Task 3: actor provenance (spec §2, §8, §14; audit RC-5). Authority (Judgment) is unchanged."""
from __future__ import annotations

import json

from steward import actor, approvals, cli, sendback
from steward import tech_lead_notebook as nb
from steward import tech_lead_playbook as pb
from steward.actor import Actor, ActorRef
from steward.brief import render_brief
from steward.contract import Contract
from steward.control_pane import ControlPane, send_cmd
from steward.models import JobState, Judgment
from steward.steering import Steering
from steward.store import Store
from test_runner import _contracted_job


def test_classify_origins_and_hints():
    assert actor.classify("cli") == ActorRef(Actor.OWNER, "cli")
    assert actor.classify("moshi").kind == Actor.OWNER
    assert actor.classify("chatgpt").kind == Actor.OPERATOR_AGENT
    assert actor.classify("steer-agent").kind == Actor.OPERATOR_AGENT
    assert actor.classify("tech_lead:astra-tech-lead") == ActorRef(Actor.TECH_LEAD, "astra-tech-lead")
    assert actor.classify("watcher:style").kind == Actor.MODEL_SUPERVISOR
    assert actor.classify("worker").kind == Actor.WORKER
    assert actor.classify("steward").kind == Actor.CONTROLLER
    assert actor.classify("something-new").kind == Actor.UNKNOWN
    assert actor.classify("cli", "operator_agent:claude-code") == ActorRef(Actor.OPERATOR_AGENT, "claude-code")


def test_a_hint_can_never_claim_the_owner():
    assert actor.classify("worker", "owner:sam").kind == Actor.WORKER
    assert actor.classify("steer-agent", "owner").kind == Actor.OPERATOR_AGENT


def test_detect_cli_actor_from_agent_environment():
    assert actor.detect_cli_actor({}) is None
    assert actor.detect_cli_actor({"CLAUDECODE": "1"}) == "operator_agent:claude-code"
    assert actor.detect_cli_actor({"CODEX_SANDBOX": "seatbelt"}) == "operator_agent:codex"
    assert actor.detect_cli_actor({"STEWARD_OPERATOR": "opus"}) == "operator_agent:opus"
    assert actor.detect_cli_actor({"CLAUDECODE": "1", "STEWARD_FORCED_ORIGIN": "worker"}) is None
    assert actor.detect_cli_actor({"STEWARD_ACTOR": "owner:sam"}) is None  # never self-declared owner


def test_send_cmd_carries_the_actor_hint():
    sent = []

    class Api:
        def send(self, pane, frame):
            sent.append(frame)

    send_cmd(Api(), "w1:p1", "r1", "nudge", {"job_id": "j"}, "cli", actor="operator_agent:claude-code")
    assert sent[0].body["actor"] == "operator_agent:claude-code"
    send_cmd(Api(), "w1:p1", "r2", "nudge", {"job_id": "j"}, "cli")
    assert "actor" not in sent[1].body


def test_events_written_for_an_operator_agent_command_carry_the_actor(steward_home):
    store = Store(steward_home)
    jid = _contracted_job(store).job_id
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    pane = ControlPane(store, cli._build_control_handlers(store), print_fn=lambda _l: None)
    pane.execute({"request_id": "r1", "op": "nudge", "args": {"job_id": jid, "text": "try the small fix"},
                  "origin": "cli", "actor": "operator_agent:claude-code"})
    ev = [e for e in store.events(jid) if e.what == "nudge instruction recorded"][-1]
    assert ev.evidence["actor"] == "operator_agent:claude-code"
    assert ev.judgment == Judgment.human  # authority unchanged: a relay of the owner's words
    pane.execute({"request_id": "r2", "op": "nudge", "args": {"job_id": jid, "text": "owner typed this"},
                  "origin": "cli"})
    ev2 = [e for e in store.events(jid) if e.what == "nudge instruction recorded"][-1]
    assert "actor" not in ev2.evidence  # owner/legacy events are byte-for-byte unchanged


def test_tech_lead_send_back_closes_approvals_as_the_lead(steward_home):
    store = Store(steward_home)
    jid = _contracted_job(store).job_id
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    store.set_state(jid, JobState.NEEDS_HUMAN, what="x", evidence={}, rule="acceptance_unresolved", judgment=Judgment.code)
    rec = approvals.open_approval(store, jid, approvals.ACCEPTANCE_QUESTION, kind="acceptance")
    res = sendback.send_back(store, jid, "restore the test", origin="tech_lead:astra-tech-lead",
                             judgment=Judgment.model_informed_code_authorized)
    assert res["accepted"]
    closed = approvals.get_approval(store, jid, rec["id"])
    assert closed["status"] == "denied" and closed["decided_by"] == "tech_lead:astra-tech-lead"


def test_withdraw_marks_an_approval_withdrawn(steward_home):
    store = Store(steward_home)
    jid = _contracted_job(store).job_id
    rec = approvals.open_approval(store, jid, "tech lead: decide X", kind="tech_lead")
    out = approvals.withdraw(store, jid, rec["id"], by="tech_lead:astra-tech-lead", reason="obsolete")
    assert out["status"] == "withdrawn" and out["decided_by"] == "tech_lead:astra-tech-lead"
    assert approvals.list_approvals(store, jid, status="pending") == []


def test_rework_brief_names_who_sent_it_back():
    c = Contract(goal="g", done_when=["d"])
    assert "SENT BACK BY THE TECH LEAD (astra-tech-lead)" in render_brief(
        c, [], None, None, rework="fix", rework_by="tech_lead:astra-tech-lead")
    assert "SENT BACK BY" in render_brief(c, [], None, None, rework="fix", rework_by="cli")
    assert "THE TECH LEAD" not in render_brief(c, [], None, None, rework="fix", rework_by="cli")


def _acted(store, jid):
    nb.append(store, jid, signature="new_failures_repeated:abc", action="send_back", strategy="send_back",
              outcome="pending", model="m", lead="astra-tech-lead", trigger_id="t1", kind="new_failures_repeated")
    pb.record_attempt(store, "new_failures_repeated", "send_back", "send_back: fix", job_id=jid)
    store.append_event(jid, what="tech_lead_action", evidence={"action": "send_back", "outcome": "executed"},
                       rule="tech_lead_executed", judgment=Judgment.model_informed_code_authorized)


def test_operator_agent_steer_does_not_demote_the_playbook(steward_home):
    store = Store(steward_home)
    jid = _contracted_job(store).job_id
    _acted(store, jid)
    with actor.bind("operator_agent:claude-code"):
        Steering(store).record(jid, "focus", "do it differently", origin="cli")
    nb.Memory(store).observe(jid)
    assert pb.entries(store, "new_failures_repeated")[0]["failures"] == 0
    assert [r["outcome"] for r in nb.entries(store, jid)][-1] == "redirected_by_operator"


def test_owner_steer_still_overrules(steward_home):
    store = Store(steward_home)
    jid = _contracted_job(store).job_id
    _acted(store, jid)
    Steering(store).record(jid, "focus", "do it differently", origin="cli")
    nb.Memory(store).observe(jid)
    assert pb.entries(store, "new_failures_repeated")[0]["failures"] == 1
    assert [r["outcome"] for r in nb.entries(store, jid)][-1] == "overruled"
