"""Plan 1 Task 8: review scheduling (coalescing, reuse, aging, one per job, bounded concurrency) and the sweep."""
from __future__ import annotations

import time

import pytest

from steward import blockers, ownership
from steward import fleet_runtime as rt
from steward import questions_delivery as qd
from steward import tech_lead_policy as tpol
from steward.models import JobState, Judgment
from steward.store import Store
from ownership_fixtures import JEV, FakeLead, Notes, contracted, control_lead, git_ok, park_rule, park_tracks, proposal


def test_ownership_policy_table_loads_and_rejects_floor_words(tmp_path):
    p = tmp_path / "tech-lead.toml"
    p.write_text("[ownership]\nenabled = true\ncoalesce_s = 5\naging_min = 30\nmax_reviews = 2\n")
    pol = tpol.load(p)
    assert (pol.ownership_coalesce_s, pol.ownership_aging_min, pol.ownership_max_reviews) == (5, 30, 2)
    p.write_text("[ownership]\nskip_review = true\n")
    with pytest.raises(tpol.PolicyError) as e:
        tpol.load(p)
    assert e.value.reason == "floor_key"
    from pathlib import Path

    example = tpol.load(Path(__file__).resolve().parent.parent / "examples" / "tech-lead.toml")
    assert example.ownership_enabled is True and example.ownership_coalesce_s == ownership.COALESCE_S


def test_a_new_blocker_waits_for_the_coalescing_window(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    ownership.reconcile_job(store, jid, now=100.0)
    assert ownership.due_reviews(store, now=110.0) == []
    (req,) = ownership.due_reviews(store, now=131.0)
    assert req.job_id == jid and len(req.blocker_ids) == 3 and req.reason == "new"


def test_unchanged_evidence_reuses_the_diagnosis_new_evidence_reopens_review(steward_home):
    store = Store(steward_home)
    jid = park_rule(store, "finish_line_bounces_exhausted", "finish line still not met after 3 nudges: x")
    ownership.reconcile_job(store, jid, now=100.0)
    (b,) = blockers.open_blockers(store, jid)
    blockers.record_diagnosis(store, b.blocker_id, {"summary": "nothing new to try"}, now=200.0)
    assert ownership.due_reviews(store, now=10_000.0) == []
    blockers.raise_blocker(store, jid, kind=b.kind, subject=b.kind, claim=b.claim, evidence=["answer:q1"],
                           origin_actor=ownership.CONTROLLER, now=10_000.0)
    assert ownership.due_reviews(store, now=10_020.0) == []           # coalescing the new fact
    (req,) = ownership.due_reviews(store, now=10_031.0)
    assert req.reason == "evidence_changed"


def test_aging_prevents_a_low_impact_job_from_starving(steward_home):
    store = Store(steward_home)
    old = contracted(store)
    new = contracted(store)
    blockers.raise_blocker(store, old, kind="flow_finding", subject="f", claim="flow", evidence=["e1"],
                           origin_actor=ownership.CONTROLLER, now=0.0)                       # impact 1
    blockers.raise_blocker(store, new, kind="finish_line_unmet", subject="f", claim="finish", evidence=["e2"],
                           origin_actor=ownership.CONTROLLER, now=3000.0)                    # impact 3
    assert [r.job_id for r in ownership.due_reviews(store, now=3100.0)] == [new, old]
    assert [r.job_id for r in ownership.due_reviews(store, now=3700.0)] == [old, new]          # old is 61 min


def test_sweep_starts_one_review_per_job_bounded_by_max_reviews(steward_home):
    store = Store(steward_home)
    jobs = [park_tracks(store) for _ in range(5)]
    lead = FakeLead()
    pol = tpol.Policy(ownership_max_reviews=3)
    ownership.sweep(store, now=100.0, job_lead=lead, policy=pol)
    out = ownership.sweep(store, now=200.0, job_lead=lead, policy=pol)
    assert len(out["reviews"]) == 3 and len({c["job"] for c in lead.calls}) == 3
    out = ownership.sweep(store, now=201.0, job_lead=lead, policy=pol)
    assert out["reviews"] == [] and sorted(out["polled"]) == sorted({c["job"] for c in lead.calls})
    assert set(jobs) >= {c["job"] for c in lead.calls}


def test_a_running_jobs_blocker_reaches_its_runner_lead_once_per_evidence_generation(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    b, _ = blockers.raise_blocker(store, jid, kind="jev_concern", subject="jev:risky_action", claim="risky",
                                  evidence=["signal:jev:risky_action"], origin_actor=JEV, now=0.0)
    lead = FakeLead()
    ownership.sweep(store, now=100.0, job_lead=lead)
    ownership.sweep(store, now=200.0, job_lead=lead)
    facts = rt.Arbiter(store).pending_for_tech_lead(jid)
    assert [(f["source"], f["kind"], f["data"]["blocker_id"]) for f in facts] == [
        ("ownership", "blocker:jev_concern", b.blocker_id)]
    assert lead.calls == []  # the control pane does not review a job its runner owns


def test_without_a_tech_lead_review_is_unavailable_never_an_owner_request(steward_home):
    store = Store(steward_home)
    notes = Notes()
    jid = park_tracks(store)
    ownership.sweep(store, now=100.0, notifier=notes)
    out = ownership.sweep(store, now=200.0, notifier=notes)
    assert out["unavailable"] and ownership.owner_view(store, jid).status == "technical_review_unavailable"
    assert notes.events == []


def test_a_real_review_marks_blockers_reviewing_then_records_the_diagnosis(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    lead, scripted = control_lead(store, {"astra-tech-lead": [proposal(
        {"type": "send_back", "params": {"text": "check the downloads dir first"}})]})
    t0 = time.time()  # the lead's own clock is real: sweep times must not run behind its records
    ownership.sweep(store, now=t0, job_lead=lead)
    ownership.sweep(store, now=t0 + 31, job_lead=lead)                   # review starts (sync answer)
    assert {b.disposition for b in blockers.open_blockers(store, jid)} == {"reviewing"}
    ownership.sweep(store, now=t0 + 32, job_lead=lead)                   # polled: finished
    rows = blockers.open_blockers(store, jid)
    assert {b.disposition for b in rows} == {"candidate"} and all(blockers.reusable_diagnosis(b) for b in rows)
    assert rows[0].diagnosis["lead"] == "astra-tech-lead"
    ownership.sweep(store, now=t0 + 5000, job_lead=lead)
    assert len(scripted.calls) == 1                                      # unchanged evidence: not paid for again
    assert "blockers" in scripted.calls[0][1]                            # the packet carried the blockers


def test_tick_runs_the_sweep_instead_of_the_legacy_parked_pass(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    lead = FakeLead()
    qd.tick(store, git_ok(), now=100.0, job_lead=lead, policy_fn=lambda: tpol.Policy())
    out = qd.tick(store, git_ok(), now=200.0, job_lead=lead, policy_fn=lambda: tpol.Policy())
    assert out["ownership"]["reviews"][0]["job"] == jid
    assert lead.calls[0]["reason"].startswith("ownership:") and lead.calls[0]["blocker_ids"]
    assert out["parked"] == [{"job": jid, "reason": "new"}]
