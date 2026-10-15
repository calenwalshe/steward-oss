"""Plan 3 Task 7: recovery attempts require observable outcomes and count only unproductive rounds."""
from __future__ import annotations

import pytest

from ownership_fixtures import CONTROLLER, LEAD, contracted
from steward import attempts, decision_trace
from steward.store import Store


def _rec(store, jid, change="replace the worker", digest_evidence=("event:1",), now=1.0, **kw):
    return attempts.record(store, jid, issue_key="recovery:worker", actor=kw.pop("actor", CONTROLLER),
                           hypothesis="the worker process died", change=change,
                           success_observation="heartbeat class returns to progressing",
                           evidence=list(digest_evidence), now=now, **kw)


def test_attempt_requires_mechanism_and_observable_success(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    with pytest.raises(attempts.AttemptError, match="observable success"):
        attempts.record(store, jid, issue_key="i", actor=LEAD, hypothesis="h", change="try again",
                        success_observation="  ", now=1.0)
    with pytest.raises(attempts.AttemptError):
        attempts.record(store, jid, issue_key="i", actor=LEAD, hypothesis="", change="c", success_observation="s",
                        now=1.0)


def test_rounds_count_unproductive_attempts_and_reset_on_progress(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    a1 = _rec(store, jid)
    assert a1.round == 1 and a1.outcome == "pending"
    attempts.resolve(store, a1.attempt_id, "failed", detail={"why": "died again"}, now=2.0)
    a2 = _rec(store, jid, now=3.0)
    assert a2.round == 2 and attempts.unproductive_rounds(store, jid, "recovery:worker") == 2
    assert attempts.progressed(store, jid, "recovery:worker", detail={"class": "progressing"}, now=4.0) == 1
    assert attempts.unproductive_rounds(store, jid, "recovery:worker") == 0
    assert _rec(store, jid, now=5.0).round == 1


def test_failed_approaches_and_unchanged_evidence_are_code_facts(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    a1 = _rec(store, jid, change="Replace the worker!")
    attempts.resolve(store, a1.attempt_id, "failed", detail={}, now=2.0)
    assert attempts.approach_key("replace   the WORKER") in attempts.failed_approaches(store, jid, "recovery:worker")
    assert attempts.unchanged_since_last_failure(store, jid, "recovery:worker", a1.evidence_digest)
    assert not attempts.unchanged_since_last_failure(store, jid, "recovery:worker", "different-digest")


def test_each_attempt_is_a_decision_record_with_its_outcome(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    a = _rec(store, jid, actor=LEAD)
    attempts.resolve(store, a.attempt_id, "progressed", detail={"evidence": "checks passed"}, now=2.0)
    trace = decision_trace.trace(store, jid)
    (rec,) = [r for r in trace if r["decision_id"] == a.decision_id]
    assert rec["stage"] == "attempted" and rec["actor_kind"] == "tech_lead"
    assert rec["outcomes"][-1]["observed"]["outcome"] == "progressed"


def test_resolve_only_from_pending(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    a = _rec(store, jid)
    attempts.resolve(store, a.attempt_id, "failed", detail={}, now=2.0)
    with pytest.raises(attempts.AttemptError, match="pending"):
        attempts.resolve(store, a.attempt_id, "progressed", detail={}, now=3.0)
