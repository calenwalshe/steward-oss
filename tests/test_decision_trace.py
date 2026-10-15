"""Plan 1 Task 12: §14 decision correlation, outcomes, run flags, preservation and retention."""
from __future__ import annotations

import time

import pytest

from steward import blockers, cli, ownership
from steward import decision_trace as dt
from steward import tech_lead_policy
from steward.control_pane import ControlPane
from steward.models import JobState, Judgment
from steward.steering import Steering
from steward.store import Store
from ownership_fixtures import CONTROLLER, LEAD, OWNER, contracted, control_lead, park_tracks, proposal

DAY = 86400.0


def _reopen():
    return proposal({"type": "reopen", "params": {"text": "check the unit", "evidence": "x", "confidence": 0.9}})


def test_record_links_run_blocker_actor_versions_and_outcome(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    store.append_event(jid, what="worker attempt started", evidence={}, rule="worker_dispatch", judgment=Judgment.code)
    did = dt.record(store, job_id=jid, actor=LEAD, stage="proposal", decision="proposal:p1", now=10.0,
                    blocker_id="blk-1", evidence_refs=["event:3"], versions={"model": "gpt-6-astra"}, authority_rev=4)
    dt.outcome(store, did, observed={"worker_resumed": True}, now=20.0)
    (row,) = dt.trace(store, jid)
    assert row["run_id"] == f"{jid}:a1" and (row["actor_kind"], row["actor_id"]) == ("tech_lead", "astra-tech-lead")
    assert row["versions"] == {"model": "gpt-6-astra"} and row["authority_rev"] == 4
    assert row["outcomes"][0]["observed"] == {"worker_resumed": True}
    with pytest.raises(ValueError):
        dt.record(store, job_id=jid, actor=LEAD, stage="done", decision="x", now=1.0)


def test_proposal_attempt_and_effect_stay_distinct_through_a_real_review(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    lead, _ = control_lead(store, {"astra-tech-lead": [_reopen()]})
    t = time.time()
    ownership.reconcile_job(store, jid, now=t)
    ownership.sweep(store, now=t + 31, job_lead=lead)
    ownership.sweep(store, now=t + 32, job_lead=lead)
    rows = dt.trace(store, jid)
    prop = next(r for r in rows if r["stage"] == "proposal")
    assert prop["actor_kind"] == "tech_lead" and prop["versions"]["model"] == "gpt-6-astra"
    eff = next(r for r in rows if r["decision"] == "reopen:executed")
    assert eff["stage"] == "effect" and eff["intent_key"].startswith("reopen:") and eff["parent_id"] == prop["decision_id"]
    assert [r for r in rows if r["decision"] == "send_back" and r["actor_kind"] == "tech_lead"]
    assert sum(1 for r in rows if r["decision"].startswith("raise_blocker:")) == 3


def test_a_discarded_answer_is_traced_as_discarded(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    lead, _ = control_lead(store, {"astra-tech-lead": [_reopen()]})
    t = time.time()
    ownership.reconcile_job(store, jid, now=t)
    ownership.sweep(store, now=t + 31, job_lead=lead)
    store.set_state(jid, JobState.STOPPED, what="operator stop", evidence={}, rule="operator_stop",
                    judgment=Judgment.human)
    ownership.sweep(store, now=t + 32, job_lead=lead)
    (d,) = [r for r in dt.trace(store, jid) if r["stage"] == "discarded"]
    assert d["decision"] == "discarded:owner_stop" and d["parent_id"]
    assert not [r for r in dt.trace(store, jid) if r["stage"] == "effect" and r["decision"].startswith("reopen")]


def test_signal_use_records_a_jev_concern_overruled_by_the_lead(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    b, _ = ownership.raise_signal_concern(store, jid, source="jev", choice="risky_action", text="risky",
                                          confidence=0.9, now=1.0)
    blockers.transition(store, b.blocker_id, "resolved", actor=LEAD, reason="read-only command", now=2.0)
    sig = next(r for r in dt.trace(store, jid) if r["stage"] == "signal")
    assert sig["actor_kind"] == "jev" and sig["signal"]["choice"] == "risky_action"
    assert sig["outcomes"][-1]["observed"]["signal_use"] == "overruled"


def test_fields_are_masked_and_capped(steward_home):
    from steward import masking

    store = Store(steward_home)
    jid = contracted(store)
    masking.register(["hunter2-secret-value"])
    try:
        dt.record(store, job_id=jid, actor=CONTROLLER, stage="effect", decision="x hunter2-secret-value", now=1.0,
                  evidence_refs=["ref hunter2-secret-value"], versions={"blob": "y" * 9000})
    finally:
        masking.clear()
    raw = store._conn.execute("SELECT decision, evidence_json, versions_json FROM decision_records").fetchone()
    assert "hunter2" not in "".join(raw) and len(raw[2]) <= dt.FIELD_MAX_CHARS


def test_flag_preserves_evidence_through_retention_and_restart(steward_home):
    store = Store(steward_home)
    a, b = contracted(store), contracted(store)
    now = 100 * DAY
    for jid in (a, b):
        dt.record(store, job_id=jid, actor=CONTROLLER, stage="effect", decision="old", now=now - 40 * DAY)
    flag, created = dt.flag_run(store, a, description="it paged me about a question I had answered", actor=OWNER,
                                now=now)
    assert created and flag["state"] == "open" and dt.preserved(store, a, now=now)
    assert dt.purge(store, now=now) == 1
    store2 = Store(steward_home)  # controller restart
    assert [r["decision"] for r in dt.trace(store2, a)] == ["old"] and dt.trace(store2, b) == []
    dt.close_flag(store2, flag["flag_id"], actor=OWNER, resolution="investigated", now=now + DAY)
    assert dt.purge(store2, now=now + 2 * DAY) == 0  # preserved for 30 days after closing
    assert dt.purge(store2, now=now + 32 * DAY) == 1
    assert dt.open_flags(store2) == []


def test_a_duplicate_flag_adds_a_note_not_a_new_flag(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    f1, c1 = dt.flag_run(store, jid, description="wrong escalation", actor=OWNER, now=1.0)
    f2, c2 = dt.flag_run(store, jid, description="and it happened again", actor=OWNER, now=2.0)
    assert c1 and not c2 and f1["flag_id"] == f2["flag_id"] and len(dt.open_flags(store, jid)) == 1
    notes = store._conn.execute("SELECT note FROM run_flag_notes WHERE flag_id = ?", (f1["flag_id"],)).fetchall()
    assert [n[0] for n in notes] == ["and it happened again"]


def test_a_flag_alone_never_stops_the_run_or_changes_policy(steward_home, capsys):
    store = Store(steward_home)
    jid = contracted(store)
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    before = tech_lead_policy.load_or_defaults()[0]
    assert cli.main(["flag", jid, "the reviewer ignored the failing test"]) == 0
    assert "flagged flag-" in capsys.readouterr().out
    assert store.get_job(jid).state == JobState.RUNNING and Steering(store).all_instructions(jid) == []
    assert tech_lead_policy.load_or_defaults()[0] == before
    (f,) = dt.open_flags(store, jid)
    assert f["actor_kind"] == "owner"


def test_an_operator_agent_flag_is_not_labelled_the_owners(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    pane = ControlPane(store, cli._build_control_handlers(store), print_fn=lambda _l: None)
    pane.execute({"request_id": "r1", "op": "flag", "args": {"job_id": jid, "description": "looks stuck"},
                  "origin": "cli", "actor": "operator_agent:claude-code"})
    (f,) = dt.open_flags(store, jid)
    assert (f["actor_kind"], f["actor_id"]) == ("operator_agent", "claude-code")


def test_per_job_row_cap(steward_home, monkeypatch):
    monkeypatch.setattr(dt, "MAX_ROWS_PER_JOB", 3)
    store = Store(steward_home)
    jid = contracted(store)
    for i in range(5):
        dt.record(store, job_id=jid, actor=CONTROLLER, stage="effect", decision=f"d{i}", now=float(i))
    assert dt.purge(store, now=10.0) == 2
    assert [r["decision"] for r in dt.trace(store, jid)] == ["d2", "d3", "d4"]


def test_tracing_never_breaks_control(steward_home, monkeypatch):
    store = Store(steward_home)
    jid = park_tracks(store)
    monkeypatch.setattr(dt, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    ownership.reconcile_job(store, jid, now=100.0)
    assert len(blockers.open_blockers(store, jid)) == 3
