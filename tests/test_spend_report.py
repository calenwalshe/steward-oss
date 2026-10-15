"""Plan 3 Task 3: one spend report for the job: transcript (table-priced) and ledger (reported/table/unknown) spend,
split into worker, supervision and independent review; unknown prices are counted and shown, never summed as $0."""
from __future__ import annotations

import json

import pytest

from ownership_fixtures import contracted
from steward import cli, spend_policy as sp
from steward.store import Store

TOKENS = {"sources": {"worker": {"usd": 1.00, "all": 10}, "sub:explore": {"usd": 0.25, "all": 3},
                      "sol": {"usd": 0.10, "all": 2}, "watcher:drift": {"usd": 0.05, "all": 1}},
          "total": {"usd": 1.40}}


def _job(store):
    jid = contracted(store)
    sp.record(store, entry_id="lead:1", job_id=jid, source="tech_lead", usd=0.40, price_basis="reported", now=1.0)
    sp.record(store, entry_id="ir:1", job_id=jid, source="independent_review", usd=0.20, price_basis="reported",
              now=1.0)
    for i in range(3):
        sp.record(store, entry_id=f"jev:{i}", job_id=jid, source="jev", usd=None, price_basis="unknown", now=1.0)
    return jid


def test_report_splits_worker_supervision_review(steward_home):
    store = Store(steward_home)
    jid = _job(store)
    rep = sp.report(store, jid, now=2.0, tokens_data=TOKENS)
    assert rep.worker_usd == pytest.approx(1.25)                       # worker + subagents
    assert rep.supervision_usd == pytest.approx(0.10 + 0.05 + 0.40)    # sol + watcher + tech lead (Jev unknown)
    assert rep.review_usd == pytest.approx(0.20)
    assert rep.known_usd == pytest.approx(1.25 + 0.15 + 0.40 + 0.20)
    assert rep.unknown_calls == 3 and rep.jev_calls == 3


def test_jev_calls_are_counted_unknown_never_zero(steward_home):
    store = Store(steward_home)
    jid = _job(store)
    rep = sp.report(store, jid, now=2.0, tokens_data=TOKENS)
    assert rep.by_source["jev"] == {"usd": None, "unknown_calls": 3, "calls": 3, "basis": "unknown"}
    text = rep.text()
    assert "3 call(s) price unknown" in text and "≥ $" in text and "Jev $0.00" not in text


def test_transcript_spend_is_labelled_table_priced_not_reported(steward_home):
    store = Store(steward_home)
    jid = _job(store)
    rep = sp.report(store, jid, now=2.0, tokens_data=TOKENS)
    assert rep.by_source["worker"]["basis"].startswith("table")
    assert rep.by_source["tech_lead"]["basis"] == "reported"


def test_limits_and_revised_estimate_appear_in_the_report(steward_home):
    store = Store(steward_home)
    jid = _job(store)
    sp.set_limit(store, jid, "job_total", 10.0, binding="hard", provenance="owner_explicit",
                 source={"via": "spend-limit"}, now=1.0)
    est = sp.revise_estimate(store, jid, 7.5, basis="lead: two more review rounds", now=3.0)
    assert est["revision"] == 1 and sp.estimate(store, jid)["usd"] == 7.5
    rep = sp.report(store, jid, now=4.0, tokens_data=TOKENS)
    assert "job_total cap $10.00" in rep.text() and "estimate $7.50" in rep.text()
    assert [e.what for e in store.events(jid)].count("spend_estimate_revised") == 1


def test_spend_cli_is_read_only_json(steward_home, capsys, monkeypatch):
    store = Store(steward_home)
    jid = _job(store)
    monkeypatch.setattr(sp, "_tokens_for", lambda store, job_id, now: TOKENS)
    before = len(store.events(jid))
    assert cli.main(["spend", jid, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["unknown_calls"] == 3 and out["by_source"]["jev"]["usd"] is None
    assert len(store.events(jid)) == before
