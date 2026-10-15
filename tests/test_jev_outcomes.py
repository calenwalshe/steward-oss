import json
import time

import pytest

from ownership_fixtures import OWNER, contracted
from steward import jev_outcomes, judgment_ledger
from steward.actor import Actor, ActorRef
from steward.jev_observer import FakeTransport, JevObserver, TransportResponse
from steward.models import Judgment
from steward.store import Store


def _record(store, jid, choice="stalled"):
    body = TransportResponse(200, json.dumps({"answers": {"triage": {"choice": choice, "confidence": .8,
                                                                           "probabilities": {choice: .8}}}}).encode())
    JevObserver(transport=FakeTransport(body), enabled=True).observe(store, jid, {}, "test")
    return judgment_ledger.receipts(store, job_id=jid)[0]


def _run_id(store, receipt):
    return store._conn.execute("SELECT run_id FROM decision_records WHERE decision_id=?",
                               (receipt["decision_id"],)).fetchone()[0]


def test_jev_outcome_report_preserves_label_sources_and_never_promotes_continuous_ownership_recovery_acceptance(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    jid = contracted(store)
    receipt = _record(store, jid)
    event = store.append_event(jid, what="worker replacement decided", evidence={"cls": "dead", "run_id": _run_id(store, receipt)},
                               rule="qualification_fixture", judgment=Judgment.code)
    now = time.time() + 1.0
    jev_outcomes.label(store, receipt["receipt_id"], "stalled", source="code_outcome",
                       actor=ActorRef(Actor.CONTROLLER, "fixture"), evidence=[f"seq:{event.seq}"], now=now)
    jev_outcomes.label(store, receipt["receipt_id"], "stalled", source="owner_correction",
                       actor=OWNER, now=now + 1)

    report = jev_outcomes.report(store, [jid])

    assert report["lanes"]["triage"]["matches"]["code_outcome"]["agreement"] == 1.0
    assert report["lanes"]["triage"]["labels"] == {"code_outcome": 1, "owner_correction": 1}
    assert report["option_order_sensitivity"].startswith("unknown")
    assert "not_authorized" in report["promotion"]
    assert "no causal claim" in report["limitations"][-1]


def test_jev_outcome_labels_enforce_owner_provenance_and_keep_model_separate_continuous_ownership_recovery_acceptance(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    jid = contracted(store)
    receipt = _record(store, jid)
    event = store.append_event(jid, what="worker replacement decided", evidence={"cls": "dead", "run_id": _run_id(store, receipt)},
                               rule="qualification_fixture", judgment=Judgment.code)
    now = time.time() + 1.0
    with pytest.raises(ValueError):
        jev_outcomes.label(store, receipt["receipt_id"], "ok", source="owner_correction",
                           actor=ActorRef(Actor.OPERATOR_AGENT, "codex"), now=10.0)
    jev_outcomes.label(store, receipt["receipt_id"], "stalled", source="independent_model",
                       actor=ActorRef(Actor.INDEPENDENT_REVIEWER, "reviewer"), uncertainty="semantic judgment",
                       now=11.0)
    result = jev_outcomes.report(store, [jid])
    assert result["lanes"]["triage"]["matches"] == {}
    assert result["lanes"]["triage"]["unlabeled"] == 0
    assert "not human ground truth" in result["limitations"][0]


def test_mult_question_receipt_requires_question_bound_outcome_label_continuous_ownership_recovery_acceptance(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    jid = contracted(store)
    receipt = _record(store, jid)
    event = store.append_event(jid, what="worker replacement decided", evidence={"cls": "dead", "run_id": _run_id(store, receipt)},
                               rule="qualification_fixture", judgment=Judgment.code)
    now = time.time() + 1.0
    response = {"answers": {"unrelated_eta": {"choice": "later", "confidence": .7},
                            "triage": {"choice": "stalled", "confidence": .8}}}
    with store._conn:
        store._conn.execute("UPDATE judgment_receipts SET response_json=? WHERE receipt_id=?",
                            (json.dumps(response), receipt["receipt_id"]))
    jev_outcomes.label(store, receipt["receipt_id"], "stalled", source="code_outcome",
                       actor=ActorRef(Actor.CONTROLLER, "fixture"), question="triage", evidence=[f"seq:{event.seq}"],
                       now=now)
    report = jev_outcomes.report(store, [jid])
    assert report["lanes"]["triage"]["matches"]["code_outcome"]["agreement"] == 1.0

    second_event = store.append_event(jid, what="worker reported done", evidence={"run_id": _run_id(store, receipt)}, rule="runner",
                                      judgment=Judgment.code)
    jev_outcomes.label(store, receipt["receipt_id"], "finished", source="code_outcome",
                       actor=ActorRef(Actor.CONTROLLER, "fixture"), evidence=[f"seq:{second_event.seq}"], now=now + 1)
    report = jev_outcomes.report(store, [jid])
    assert report["lanes"]["triage"]["unbound_question_labels"] == 1


def test_code_outcome_labels_require_trusted_actor_and_job_event_provenance(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    jid = contracted(store)
    receipt = _record(store, jid)
    with pytest.raises(ValueError, match="trusted controller"):
        jev_outcomes.label(store, receipt["receipt_id"], "stalled", source="code_outcome",
                           actor=ActorRef(Actor.WORKER, "worker"), evidence=["seq:1"], now=10.0)
    with pytest.raises(ValueError, match="event sequence"):
        jev_outcomes.label(store, receipt["receipt_id"], "stalled", source="code_outcome",
                           actor=ActorRef(Actor.CONTROLLER, "fixture"), now=10.0)
    with pytest.raises(ValueError, match="not part of this receipt"):
        jev_outcomes.label(store, receipt["receipt_id"], "stalled", source="code_outcome",
                           actor=ActorRef(Actor.CONTROLLER, "fixture"), evidence=["seq:999999"], now=10.0)
    event = store.append_event(jid, what="worker reported done", evidence={"run_id": _run_id(store, receipt)}, rule="runner",
                               judgment=Judgment.code)
    with pytest.raises(ValueError, match="does not match"):
        jev_outcomes.label(store, receipt["receipt_id"], "stalled", source="code_outcome",
                           actor=ActorRef(Actor.CONTROLLER, "fixture"), evidence=[f"seq:{event.seq}"], now=time.time()+1)

    wrong_run = store.append_event(jid, what="worker reported done", evidence={"run_id": "other-job:a9"},
                                   rule="runner", judgment=Judgment.code)
    with pytest.raises(ValueError, match="not bound"):
        jev_outcomes.label(store, receipt["receipt_id"], "finished", source="code_outcome",
                           actor=ActorRef(Actor.CONTROLLER, "fixture"), evidence=[f"seq:{wrong_run.seq}"],
                           now=time.time()+1)
