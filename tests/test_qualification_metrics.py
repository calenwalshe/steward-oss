from types import SimpleNamespace

from steward import blockers, decision_trace, qualification
from steward.actor import Actor, ActorRef
from steward.models import Judgment
from steward.store import Store


def test_metrics_report_observations_and_preserve_unlabeled_human_need_continuous_ownership_recovery_acceptance(steward_home):
    store = Store(steward_home)
    job = store.create_job("observe measurable supervision", "/repo")
    b, _ = blockers.raise_blocker(store, job.job_id, kind="worker_parked_unfinished", subject="worker",
                                 claim="worker is parked", evidence=["event:1"],
                                 origin_actor=ActorRef(Actor.WORKER, "fake"), now=100.0)
    store.append_event(job.job_id, what="independent_review_queued", evidence={"blocker_id": b.blocker_id},
                       rule="test", judgment=Judgment.code)
    store.append_event(job.job_id, what="independent_review_started", evidence={"blocker_id": b.blocker_id},
                       rule="test", judgment=Judgment.code)
    decision_trace.record(store, job_id=job.job_id, actor=ActorRef(Actor.CONTROLLER, "ownership"),
                          stage="effect", decision="owner_request:needs_you", blocker_id=b.blocker_id, now=150.0)

    observed = qualification.observe(store, [job.job_id], now=200.0)

    assert observed["jobs_observed"] == 1
    assert observed["owner_requests_observed"] == 1
    assert observed["blocked_items"][0]["blocked_seconds"] > 0
    assert len(observed["review_wait_seconds"]) == 1
    assert observed["owner_request_necessity"] == "unknown_without_independent_outcome_labels"
    assert observed["missed_genuine_owner_decisions"].startswith("unknown")
    assert observed["success_claim"] is None


def test_deterministic_human_need_and_recovery_metrics_use_explicit_outcomes_continuous_ownership_recovery_acceptance(steward_home):
    from steward import attempts, spend_policy
    from ownership_fixtures import CONTROLLER

    store = Store(steward_home)
    job = store.create_job("measure labeled ownership", "/repo")
    unneeded, _ = blockers.raise_blocker(store, job.job_id, kind="worker_parked_unfinished", subject="unneeded",
                                         claim="No owner decision is needed", evidence=["event:1"],
                                         origin_actor=ActorRef(Actor.WORKER, "fixture"), now=10.0)
    needed, _ = blockers.raise_blocker(store, job.job_id, kind="owner_question", subject="needed",
                                       claim="Owner must choose", evidence=["question:q1"],
                                       origin_actor=CONTROLLER, now=10.0)
    request_id = decision_trace.record(store, job_id=job.job_id, actor=CONTROLLER, stage="effect",
                                       decision="owner_request:worker_parked_unfinished", blocker_id=unneeded.blocker_id, now=11.0)
    first = decision_trace.record(store, job_id=job.job_id, actor=CONTROLLER, stage="proposal",
                                  decision="reviewed same evidence", blocker_id=unneeded.blocker_id,
                                  evidence_refs=["event:1"], authority_rev=0, now=12.0)
    for _ in range(2):
        store.append_event(job.job_id, what="independent_review_started",
                           evidence={"blocker_id": unneeded.blocker_id, "basis_digest": "same"},
                           rule="independent_review", judgment=Judgment.code)
    decision_trace.outcome(store, first, observed={"duplicate": True}, now=13.0)
    attempt = attempts.record(store, job.job_id, issue_key=needed.issue_key, actor=CONTROLLER,
                              hypothesis="wrong question route", change="ask owner the exact choice",
                              success_observation="the recorded decision arrives", evidence=["question:q1"],
                              blocker_id=needed.blocker_id, now=14.0)
    attempts.resolve(store, attempt.attempt_id, "progressed", detail={"effect": "owner decision recorded"}, now=15.0)
    spend_policy.revise_estimate(store, job.job_id, 1.0, basis="fixture estimate", now=10.0)
    spend_policy.record(store, entry_id="lead-call-1", job_id=job.job_id, source="tech_lead", usd=1.25,
                        price_basis="reported", bound_usd=2.0, now=15.0)
    spend_policy.record(store, entry_id="review-call-1", job_id=job.job_id, source="independent_review", usd=.5,
                        price_basis="reported", bound_usd=1.0, now=16.0)

    observed = qualification.observe(store, [job.job_id], now=20.0,
                                    expected_human_need={unneeded.blocker_id: False, needed.blocker_id: True})

    assert observed["unnecessary_owner_requests"] == [unneeded.blocker_id]
    assert observed["missed_genuine_owner_decisions"] == [needed.blocker_id]
    assert observed["repeated_unchanged_reviews"][0]["count"] == 2
    assert observed["duplicate_action_outcomes"] == 1
    assert observed["recovery_outcomes"] == {"progressed": 1}
    assert observed["spend_vs_estimate"][0]["known_usd"] == 1.75
    assert observed["supervision_spend_usd"] == 1.25
    assert observed["independent_review_spend_usd"] == 0.5
    assert observed["spend_vs_estimate"][0]["estimate_usd"] == 1.0


def test_delivery_lag_only_joins_the_verified_candidate_revision_continuous_ownership_recovery_acceptance(steward_home):
    from steward import delivery

    store = Store(steward_home)
    job = store.create_job("match verification to delivery candidate", "/repo")
    verified = store.append_event(job.job_id, what="verification recorded", evidence={"commit_sha": "abc123"},
                                  rule="verification", judgment=Judgment.code)
    verified_at = qualification._epoch(verified.ts)
    delivery.record_receipt(store, job.job_id, "incorporation", actor=ActorRef(Actor.CONTROLLER, "fixture"),
                            revision="wrong-target", evidence={}, now=verified_at + 5)
    delivery.record_receipt(store, job.job_id, "activation", actor=ActorRef(Actor.CONTROLLER, "fixture"),
                            revision="abc123", evidence={}, now=verified_at + 10)

    observed = qualification.observe(store, [job.job_id], now=verified_at + 20)

    assert observed["verified_to_delivery_seconds"] == [{"job_id": job.job_id, "commit_sha": "abc123",
                                                           "milestone": "activation", "seconds": 10.0}]


def test_qualification_cli_exposes_read_only_observations_continuous_ownership_recovery_acceptance(steward_home, capsys):
    store = Store(steward_home)
    job = store.create_job("observe only", "/repo")
    from steward.cli import _cmd_qualification

    assert _cmd_qualification(store, SimpleNamespace(jobs=[job.job_id], json=True)) == 0
    assert '"success_claim": null' in capsys.readouterr().out
