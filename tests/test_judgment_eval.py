"""Replay and reporting are pure; labels require their declared provenance."""
import json
import pytest

from ownership_fixtures import OPERATOR, OWNER, CONTROLLER, contracted
from steward import judgment_eval as je, judgment_ledger as jl
from steward import jev_outcomes
from steward.actor import Actor, ActorRef
from steward.jev_observer import FakeTransport, JevObserver, TransportResponse
from steward.models import Judgment
from steward.store import Store


def _observe(store, jid, choice, conf):
    body = {"answers": {"triage": {"choice": choice, "confidence": conf, "probabilities": {choice: conf}}}}
    JevObserver(transport=FakeTransport(TransportResponse(200, json.dumps(body).encode())), enabled=True).observe(
        store, jid, {}, "t")
    return jl.receipts(store, job_id=jid)[-1]


def test_label_provenance_and_report_counts(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home); jid = contracted(store); r = _observe(store, jid, "stalled", .9)
    je.label(store, r["receipt_id"], "ok", source="owner_correction", actor=OWNER, now=1)
    with pytest.raises(je.LabelError, match="owner"):
        je.label(store, r["receipt_id"], "ok", source="owner_correction", actor=OPERATOR, now=2)
    with pytest.raises(je.LabelError, match="independent"):
        je.label(store, r["receipt_id"], "ok", source="independent_model", actor=CONTROLLER, now=2)
    je.label(store, r["receipt_id"], "stalled", source="independent_model",
             actor=ActorRef(Actor.INDEPENDENT_REVIEWER, "fable"), now=3)
    data = je.report(store, lane="triage")["lanes"]["triage"]
    assert data["labels"]["owner_correction"] == 1 and data["ground_truth_labels"] == 1
    assert "does not establish" in je.report(store)["caveat"]


def test_gate_replay_thresholds_and_order_sensitivity_are_read_only(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home); jid = contracted(store)
    for choice, conf in (("stalled", .9), ("stalled", .9), ("ok", .9), ("stalled", .75), ("stalled", .75)):
        _observe(store, jid, choice, conf)
    count = len(store.events(jid))
    assert je.replay_gate(store, lane="triage", gate=je.LIVE_GATE)["flags"] == 1
    assert je.replay_gate(store, lane="triage", gate={**je.LIVE_GATE, "cooldown_s": 0})["flags"] == 2
    assert len(je.sweep_thresholds(store, lane="triage")) == 12
    assert len(store.events(jid)) == count
    q = {"triage": {"criteria": {"ok": "a", "stalled": "b", "looping": "c"}}}
    assert je.order_sensitivity(lambda qs, _: "ok", q, [{}]) ["changed_fraction"] == 0
    assert je.order_sensitivity(lambda qs, _: next(iter(qs["triage"]["criteria"])), q, [{}])["changed_fraction"] == 1


def test_auto_label_routes_through_receipt_bound_code_outcome_validation(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    jid = contracted(store)
    receipt = _observe(store, jid, "stalled", .9)
    run_id = store._conn.execute("SELECT run_id FROM decision_records WHERE decision_id=?",
                                 (receipt["decision_id"],)).fetchone()[0]
    event = store.append_event(jid, what="worker reported done", evidence={"run_id": run_id}, rule="runner",
                               judgment=Judgment.code)
    assert je.auto_label(store, now=receipt["created_epoch"] + 601) == 1
    rows = store._conn.execute("SELECT label,source,evidence_json FROM eval_labels WHERE receipt_id=?",
                               (receipt["receipt_id"],)).fetchall()
    assert len(rows) == 1 and rows[0]["label"] == "finished" and rows[0]["source"] == "code_outcome"
    assert f"seq:{event.seq}" in rows[0]["evidence_json"]


def test_legacy_code_label_api_rejects_unbound_event(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    jid = contracted(store)
    receipt = _observe(store, jid, "stalled", .9)
    event = store.append_event(jid, what="worker replacement decided", evidence={"cls": "dead"},
                               rule="worker_exited", judgment=Judgment.code)
    from steward.judgment_eval import LabelError
    with pytest.raises(LabelError, match="not bound"):
        je.label(store, receipt["receipt_id"], "stalled", source="code_outcome", actor=CONTROLLER,
                 evidence=[f"seq:{event.seq}"], now=receipt["created_epoch"] + 601)
