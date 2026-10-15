"""u5 (stall fixes 2026-10-14): the owner's decision on an ask_owner pause (the loop-guard card) clears the checkpoint
wait and delivers the instruction to the worker. Evidence: job af319063, approval d5a23421 (kind tech_lead) approved,
its lead_escalation blocker resolved, yet kv pause_checkpoint kept `verdict: ask_owner` / `waiting_on: owner` with the
open instruction undelivered; Jev logged "ruling pending (waiting on the owner's decision (ask_owner))" every ~4 min
and the worker sat idle 3.5 h until a manual steer. Delivery here is Steward's own steering record (no message sent)."""
from __future__ import annotations

from pause_support import NEW, Unit, set_pause, steers
from test_runner_fabric import _contracted_job

from steward import approvals, pause_checkpoint, recovery
from steward import pause_review as pr
from steward.models import JobState, Judgment
from steward.store import Store

FACT = {"seq": 9, "sha": NEW, "yield_lane": "CANDIDATE", "failed": [], "unit_id": "u1", "done_claim": False,
        "next_unit": "u2", "pause_kind": "pause", "pause_seq": 1}
LOOP = [{"unit_id": "u1", "outcome": "send_back"}, {"unit_id": "u1", "outcome": "send_back"}]


def _setup(steward_home):
    steward_home.mkdir(parents=True, exist_ok=True)
    store = Store(steward_home)
    job = _contracted_job(store)
    set_pause(store, job.job_id, "unit", [Unit(u, f"title {u}", f"pytest -q tests/test_{u}.py") for u in ("u1", "u2")])
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    base = {"awaiting_review": True, "review_requested_at": 100.0, "review_in_flight": True, "pause_seq": 1,
            "last_pause_fact": FACT, "unit_id": "u1", "history": list(LOOP)}
    pause_checkpoint.update(store, job.job_id, lambda c: c.update(base))
    return store, job.job_id


def _loop_guard(store, jid, now=160.0):
    """The third send-back of u1: the loop guard turns it into ask_owner and opens the owner's card."""
    def escalate(decision):
        return approvals.open_approval(store, jid, decision[:300], kind="tech_lead")
    out = pr.apply_ruling(store, jid, {"outcome": "send_back", "reason": "r", "text": "still no test for the parser"},
                          fact=FACT, lead="astra-tech-lead", head=NEW, now=now, escalate=escalate)
    assert out["outcome"] == "ask_owner"
    card = [a for a in approvals.list_approvals(store, jid, status="pending") if a["kind"] == "tech_lead"]
    assert len(card) == 1
    return card[0]["id"]


def test_approving_the_loop_guard_card_clears_the_wait_and_delivers_the_instruction(steward_home):
    store, jid = _setup(steward_home)
    aid = _loop_guard(store, jid)
    assert pr.owner_decision_step(store, jid, now=200.0) is None  # still pending: nothing changes
    assert pause_checkpoint.load(store, jid)["waiting_on"] == "owner"
    approvals.decide(store, jid, aid, "approve")
    out = pr.owner_decision_step(store, jid, now=210.0)
    assert out["decision"] == "approve"
    cp = pause_checkpoint.load(store, jid)
    assert cp["waiting_on"] is None and cp["verdict"] != "ask_owner"
    assert recovery.ruling_pending(store, jid) is None  # Jev no longer sees "waiting on the owner's decision"
    sent = steers(store, jid)
    assert len(sent) == 1 and "still no test for the parser" in sent[0]["text"] and "owner" in sent[0]["text"].lower()
    assert cp["open_instruction"] == sent[0]["text"]
    # idempotent: a second tick sends nothing more
    assert pr.owner_decision_step(store, jid, now=220.0) is None and len(steers(store, jid)) == 1


def test_after_the_owner_says_continue_the_next_send_back_is_not_another_ask(steward_home):
    store, jid = _setup(steward_home)
    aid = _loop_guard(store, jid)
    approvals.decide(store, jid, aid, "approve")
    pr.owner_decision_step(store, jid, now=210.0)
    pause_checkpoint.update(store, jid, lambda c: c.update(awaiting_review=True, review_in_flight=True, pause_seq=2,
                                                            last_pause_fact={**FACT, "pause_seq": 2}))
    out = pr.apply_ruling(store, jid, {"outcome": "send_back", "reason": "r", "text": "one more fix"},
                          fact={**FACT, "pause_seq": 2}, lead="astra-tech-lead", head=NEW, now=300.0,
                          escalate=lambda d: None)
    assert out["outcome"] == "send_back"  # the owner's go resets the loop guard for this unit


def test_denying_the_card_keeps_the_unit_required_and_leaves_the_rescope_to_the_owner(steward_home):
    store, jid = _setup(steward_home)
    aid = _loop_guard(store, jid)
    approvals.decide(store, jid, aid, "deny")
    out = pr.owner_decision_step(store, jid, now=210.0)
    assert out["decision"] == "deny"
    cp = pause_checkpoint.load(store, jid)
    assert "u1" not in cp["dropped_units"] and "u1" not in cp["met_units"]  # no silent re-scope
    assert cp["waiting_on"] == "owner" and "re-scop" in cp["open_instruction"]
    assert steers(store, jid) == []  # nothing is sent to the worker on a no
    assert pr.owner_decision_step(store, jid, now=220.0) is None  # handled once


def test_a_rejected_steer_keeps_the_resolution_pending_and_the_next_tick_retries(steward_home, monkeypatch):
    from steward.models import Receipt
    from steward.steering import Steering
    store, jid = _setup(steward_home)
    aid = _loop_guard(store, jid)
    approvals.decide(store, jid, aid, "approve")
    real = Steering.record
    monkeypatch.setattr(Steering, "record", lambda self, job_id, *a, **k: Receipt(
        request_id="r", job_id=job_id, kind="nudge", accepted=False, detail="refused", ts="t"))
    assert pr.owner_decision_step(store, jid, now=210.0) is None
    cp = pause_checkpoint.load(store, jid)
    assert cp["waiting_on"] == "owner" and cp["owner_approval_id"] == aid and cp["verdict"] == "ask_owner"
    assert [e.what for e in store.events(jid) if e.what.startswith("pause_owner_decision")] == \
        ["pause_owner_decision_retry"]
    monkeypatch.setattr(Steering, "record", real)
    assert pr.owner_decision_step(store, jid, now=220.0)["decision"] == "approve"
    assert pr.owner_decision_step(store, jid, now=230.0) is None
    assert len(steers(store, jid)) == 1
    assert [e.what for e in store.events(jid) if e.what == "pause_owner_decision"] == ["pause_owner_decision"]


def test_an_unrelated_ask_owner_card_is_not_continued_automatically(steward_home):
    store, jid = _setup(steward_home)
    pause_checkpoint.update(store, jid, lambda c: c.update(history=[]))  # no loop: the lead itself asks the owner
    def escalate(decision):
        return approvals.open_approval(store, jid, decision[:300], kind="tech_lead")
    out = pr.apply_ruling(store, jid, {"outcome": "ask_owner", "reason": "r", "decision": "Ship without the migration?"},
                          fact=FACT, lead="astra-tech-lead", head=NEW, now=160.0, escalate=escalate)
    assert out["outcome"] == "ask_owner"
    aid = [a for a in approvals.list_approvals(store, jid, status="pending") if a["kind"] == "tech_lead"][0]["id"]
    approvals.decide(store, jid, aid, "approve")
    assert pr.owner_decision_step(store, jid, now=210.0) is None
    assert pause_checkpoint.load(store, jid)["waiting_on"] == "owner" and steers(store, jid) == []


def test_unsupported_or_code_closed_card_statuses_change_nothing(steward_home):
    for status, extra in (("withdrawn", {}), ("stale", {}), ("expired", {}),
                          ("denied", {"decided_by": "code", "closed_reason": "superseded: x"})):
        store, jid = _setup(steward_home / status)
        aid = _loop_guard(store, jid)
        rows = approvals._load(store, jid)
        for r in rows:
            if r["id"] == aid:
                r.update(status=status, **extra)
        approvals._save(store, jid, rows)
        assert pr.owner_decision_step(store, jid, now=210.0) is None, status
        cp = pause_checkpoint.load(store, jid)
        assert cp["waiting_on"] == "owner" and cp["owner_approval_id"] == aid, status
        assert steers(store, jid) == [], status


def test_the_runner_tick_applies_the_owner_decision(steward_home):
    from pause_support import pause_runner
    # the worker is idle mid-wait (no new pause of its own this tick: that would start a fresh review instead)
    store, job, runner, _factory, _clock = pause_runner(steward_home, frames=(), lifecycle="working",
                                                        units=[Unit("u1", "t1", "pytest -q"),
                                                               Unit("u2", "t2", "pytest -q")])
    jid = job.job_id
    aid = approvals.open_approval(store, jid, "Unit u1 has been sent back 2 times ...", kind="tech_lead")["id"]
    pause_checkpoint.update(store, jid, lambda c: c.update(waiting_on="owner", verdict="ask_owner", unit_id="u1",
                                                            owner_approval_id=aid, owner_card_kind="loop_guard",
                                                            owner_instruction="add the parser test"))
    approvals.decide(store, jid, aid, "approve")
    runner.tick(jid)
    assert pause_checkpoint.load(store, jid)["waiting_on"] is None
    assert any("add the parser test" in s["text"] for s in steers(store, jid))
