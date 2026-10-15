"""Transport-seam Jev receipts are bounded, linked, and replayed without network access."""
import json
import pytest

from ownership_fixtures import OWNER, contracted
from steward import decision_trace, judgment_ledger as jl
from steward.jev_observer import FakeTransport, JevObserver, TransportResponse
from steward.store import Store


def _resp(question="triage", choice="ok"):
    return TransportResponse(200, json.dumps({"answers": {question: {"choice": choice, "confidence": .9,
                                                                         "probabilities": {choice: .9}}}}).encode())


def test_all_existing_lanes_record_results_and_unknown_spend(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home); jid = contracted(store)
    triage = FakeTransport(_resp(choice="stalled"))
    got = JevObserver(transport=triage, enabled=True).observe(store, jid, {}, "t")
    assert got["choice"] == "stalled"
    JevObserver(transport=FakeTransport(_resp("risk", "high")), enabled=True).ask_risk(store, jid, "rm", "rule")
    JevObserver(transport=FakeTransport(_resp("lane_triage", "ok")), enabled=True).observe_lane(store, jid, "lane-a", {})
    rows = jl.receipts(store, job_id=jid)
    assert [r["lane"] for r in rows] == ["triage", "risk", "lane_triage"]
    assert all(r["decision_id"] and r["latency_ms"] >= 0 for r in rows)
    assert rows[0]["response"]["answers"]["triage"]["choice"] == "stalled"
    assert store._conn.execute("SELECT COUNT(*) FROM spend_ledger WHERE source='jev' AND usd IS NULL").fetchone()[0] == 3


def test_failure_receipts_and_replay_miss(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home); jid = contracted(store)
    JevObserver(transport=FakeTransport(TransportResponse(402, b"{}")), enabled=True).observe(store, jid, {}, "t")
    (row,) = jl.receipts(store, job_id=jid)
    assert row["status"] == "http_error" and row["http_status"] == 402
    with pytest.raises(jl.ReplayMiss): jl.ReplayTransport([row]).post("", b"different", {}, 1)


def test_status_and_retention_preserve_flagged_run(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home); jid = contracted(store)
    JevObserver(transport=FakeTransport(_resp()), enabled=True, clock=lambda: 1).observe(store, jid, {}, "t")
    row = jl.receipts(store, job_id=jid)[0]
    assert jl.status(store)["by_lane"] == {"triage": 1}
    decision_trace.flag_run(store, jid, description="flag", actor=OWNER, now=2)
    store._conn.execute("UPDATE judgment_receipts SET created_epoch=0"); store._conn.commit()
    assert jl.purge(store, now=31 * 86400) == 0
    assert jl.receipts(store, job_id=jid)[0]["receipt_id"] == row["receipt_id"]
