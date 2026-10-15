"""Shadow experiments require owner approval and never affect normal routing or job state."""
import json
from dataclasses import dataclass
import pytest

from ownership_fixtures import OPERATOR, OWNER, contracted
from steward import jev_experiments as jx, judgment_ledger as jl, masking
from steward.jev_observer import FakeTransport, JevObserver, TransportResponse
from steward.store import Store

CANARY = "canary-SECRET-0123456789"

@dataclass
class Ex:
    source: str; revision: str; start: int; end: int; truncated: bool; text: str

def _select(texts, *, truncated=False, revision="r1"):
    def fn(store, job_id, ref, *, max_chars):
        if ref not in texts: return None
        text = texts[ref][:max_chars]
        return Ex(ref, revision, 0, len(text), truncated or len(texts[ref]) > max_chars, text)
    return fn

@pytest.fixture
def env(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "key")
    store = Store(steward_home); return store, contracted(store)

def _measured(store, jid):
    with store.immediate() as tx:
        for i in range(jx.MEASUREMENT_MIN_RECEIPTS):
            tx.conn.execute("INSERT INTO judgment_receipts (receipt_id,job_id,lane,pack_id,model,request_digest,state_json,status,created_epoch) VALUES (?,?,'triage','p','jev-latest',?,'{}','ok',?)", (f"r{i}", jid, f"d{i}", float(i)))

def _answer(label):
    return TransportResponse(200, json.dumps({"answers": {"evidence_support": {"choice": label, "confidence": .9}}}).encode())

def test_modes_and_measurement_gate(env):
    store, jid = env
    assert all(jx.mode(store, n) == "off" for n in jx.EXPERIMENTS)
    with pytest.raises(jx.ExperimentError, match="active"): jx.set_mode(store, "attention_routing", "active", actor=OWNER, now=1)
    with pytest.raises(jx.ExperimentError, match="owner"): jx.set_mode(store, "attention_routing", "shadow", actor=OPERATOR, now=1)
    with pytest.raises(jx.ExperimentError, match="measurement"): jx.set_mode(store, "attention_routing", "shadow", actor=OWNER, now=1)
    _measured(store, jid); jx.set_mode(store, "attention_routing", "shadow", actor=OWNER, now=2)

def test_evidence_is_bounded_masked_and_incomplete_is_insufficient(env):
    store, jid = env; _measured(store, jid); jx.set_mode(store, "evidence_support", "shadow", actor=OWNER, now=1)
    texts = {f"file:a{i}": CANARY + "x" * 2500 for i in range(5)}
    masking.register([CANARY])
    try:
        body = jx.evidence_request(store, jid, "claim" * 200, list(texts), now=2, select_fn=_select(texts))
    finally: masking.clear()
    assert len(json.dumps(body).encode()) <= jx.EVIDENCE_REQUEST_MAX_BYTES
    assert len(body["state"]["excerpts"]) <= 3 and CANARY not in json.dumps(body)
    obs = JevObserver(transport=FakeTransport(_answer("contradicting")), enabled=True)
    obs._authorize = lambda *_: "test-key"
    result = jx.run_evidence_shadow(store, obs, jid, "claim", ["file:a0"], now=3, select_fn=_select({"file:a0": "x"*5000}))
    assert result["raw_label"] == "contradicting" and result["label"] == "insufficient"
    assert jl.receipts(store, job_id=jid, lane="evidence_support")

def test_only_cited_sources_are_used_and_revision_change_invalidates(env):
    store, jid = env; _measured(store, jid); jx.set_mode(store, "evidence_support", "shadow", actor=OWNER, now=1)
    body = jx.evidence_request(store, jid, "claim", ["missing"], now=2, select_fn=_select({}))
    assert body["state"]["excerpts"] == [] and body["state"]["citations"][0]["resolved"] is False
    observer = JevObserver(transport=FakeTransport(_answer("supporting")), enabled=True)
    observer._authorize = lambda *_: "test-key"
    result = jx.run_evidence_shadow(store, observer,
                                   jid, "claim", ["a"], now=3, select_fn=_select({"a": "text"}))
    assert jx.evidence_result_still_valid(store, result, select_fn=_select({"a": "text"}, revision="r1"))
    assert not jx.evidence_result_still_valid(store, result, select_fn=_select({"a": "text"}, revision="r2"))

def test_off_mode_never_calls_provider(env):
    store, jid = env; transport = FakeTransport(_answer("supporting"))
    assert jx.run_evidence_shadow(store, JevObserver(transport=transport, enabled=True), jid, "claim", ["a"],
                                  now=1, select_fn=_select({"a": "text"})) is None
    assert transport.calls == []
