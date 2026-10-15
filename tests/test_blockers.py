"""Plan 1 Task 4: blocker identity, episodes, coalescing, dispositions, actor rights, history (spec §4, §5, §8)."""
from __future__ import annotations

import pytest

from steward import blockers, masking
from steward.store import Store
from ownership_fixtures import CONTROLLER, JEV, LEAD, OPERATOR, OWNER, contracted


def _raise(store, jid, *, kind="finish_line_unmet", subject="finish", evidence=("event:7",), now=100.0, **kw):
    return blockers.raise_blocker(store, jid, kind=kind, subject=subject, claim="finish line not met",
                                  evidence=list(evidence), origin_actor=CONTROLLER, now=now, **kw)


def test_same_snapshot_is_one_row_new_evidence_updates_in_place(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    b1, created = _raise(store, jid)
    assert created and b1.episode == 1 and b1.disposition == "candidate"
    b2, created2 = _raise(store, jid, now=200.0)
    assert not created2 and b2.blocker_id == b1.blocker_id and b2.updated_epoch == b1.updated_epoch
    b3, created3 = _raise(store, jid, evidence=("event:9",), now=300.0)
    assert not created3 and b3.blocker_id == b1.blocker_id and b3.evidence_digest != b1.evidence_digest
    assert set(b3.evidence) == {"event:7", "event:9"}
    assert len(blockers.for_job(store, jid)) == 1


def test_resolved_issue_recurs_only_as_a_new_episode_with_new_evidence(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    b1, _ = _raise(store, jid)
    blockers.transition(store, b1.blocker_id, "resolved", actor=CONTROLLER, reason="predicate", now=150.0)
    again, created = _raise(store, jid, now=160.0)  # same snapshot: history, not a new episode
    assert not created and again.blocker_id == b1.blocker_id and not again.is_open
    b2, created = _raise(store, jid, evidence=("event:20",), now=170.0)
    assert created and b2.episode == 2 and b2.issue_key == b1.issue_key and b2.blocker_id != b1.blocker_id


def test_signal_kinds_may_recur_after_their_window(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    b1, _ = blockers.raise_blocker(store, jid, kind="jev_concern", subject="jev:risky_action", claim="risky",
                                   evidence=["signal:jev:risky_action"], origin_actor=JEV, now=0.0,
                                   recur_after_s=3600.0)
    blockers.transition(store, b1.blocker_id, "resolved", actor=CONTROLLER, reason="ttl", now=10.0)
    same, created = blockers.raise_blocker(store, jid, kind="jev_concern", subject="jev:risky_action", claim="risky",
                                           evidence=["signal:jev:risky_action"], origin_actor=JEV, now=100.0,
                                           recur_after_s=3600.0)
    assert not created
    later, created = blockers.raise_blocker(store, jid, kind="jev_concern", subject="jev:risky_action",
                                            claim="risky", evidence=["signal:jev:risky_action"], origin_actor=JEV,
                                            now=5000.0, recur_after_s=3600.0)
    assert created and later.episode == 2


def test_different_blockers_coexist_and_clearing_one_leaves_the_others(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    a, _ = _raise(store, jid, kind="verification_unverified", subject="verification_unverified")
    q, _ = _raise(store, jid, kind="owner_question", subject="q1", disposition="confirmed_human",
                  responsible="owner")
    blockers.transition(store, a.blocker_id, "resolved", actor=CONTROLLER, reason="green", now=150.0)
    assert [b.blocker_id for b in blockers.open_blockers(store, jid)] == [q.blocker_id]


def test_actor_rights(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    b, _ = _raise(store, jid)
    for who in (JEV,):
        with pytest.raises(blockers.BlockerError):
            blockers.transition(store, b.blocker_id, "resolved", actor=who, reason="x", now=110.0)
    with pytest.raises(blockers.BlockerError):  # confirming needs the exact decision and why
        blockers.transition(store, b.blocker_id, "confirmed_human", actor=LEAD, reason="x", now=110.0)
    req = blockers.validate_human_request({"decision": "Restore the Pi's account?",
                                           "why_authority_insufficient": "no grant covers the device account"})
    c = blockers.transition(store, b.blocker_id, "confirmed_human", actor=LEAD, reason="confirmed", now=120.0,
                            human_request=req)
    assert c.disposition == "confirmed_human" and c.human_request["decision"].startswith("Restore")
    with pytest.raises(blockers.BlockerError):  # an operator agent cannot close a confirmed owner decision
        blockers.transition(store, b.blocker_id, "resolved", actor=OPERATOR, reason="x", now=130.0)
    blockers.transition(store, b.blocker_id, "resolved", actor=OWNER, reason="owner decided", now=140.0)
    with pytest.raises(blockers.BlockerError):  # resolved rows are history
        blockers.transition(store, b.blocker_id, "candidate", actor=OWNER, reason="x", now=150.0)


def test_a_lead_may_withdraw_its_own_confirmation_but_not_an_owner_only_kind(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    req = blockers.validate_human_request({"decision": "d", "why_authority_insufficient": "w"})
    mine, _ = blockers.raise_blocker(store, jid, kind="lead_escalation", subject="d", claim="d", evidence=["x"],
                                     origin_actor=LEAD, now=1.0)
    blockers.transition(store, mine.blocker_id, "confirmed_human", actor=LEAD, reason="c", now=2.0, human_request=req)
    blockers.transition(store, mine.blocker_id, "resolved", actor=LEAD, reason="withdrawn: obsolete", now=3.0)
    acc, _ = blockers.raise_blocker(store, jid, kind="acceptance_decision", subject="acceptance", claim="accept?",
                                    evidence=["event:1"], origin_actor=CONTROLLER, now=4.0,
                                    disposition="confirmed_human", responsible="owner")
    with pytest.raises(blockers.BlockerError):
        blockers.transition(store, acc.blocker_id, "resolved", actor=LEAD, reason="x", now=5.0)


def test_history_is_insert_only_and_complete(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    b, _ = _raise(store, jid)
    blockers.transition(store, b.blocker_id, "reviewing", actor=CONTROLLER, reason="review started", now=110.0)
    blockers.transition(store, b.blocker_id, "resolved", actor=LEAD, reason="overruled", now=120.0,
                        evidence=["test passes at HEAD"])
    h = blockers.history(store, b.blocker_id)
    assert [(r["from_disposition"], r["to_disposition"]) for r in h] == [
        (None, "candidate"), ("candidate", "reviewing"), ("reviewing", "resolved")]
    assert h[-1]["actor"] == "tech_lead:astra-tech-lead"
    assert [e.what for e in store.events(jid)][-3:] == ["blocker_raised", "blocker_transition", "blocker_transition"]


def test_claim_and_evidence_are_masked_and_capped(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    masking.register(["hunter2-secret-value"])
    try:
        b, _ = blockers.raise_blocker(store, jid, kind="unclassified_hold", subject="x",
                                      claim="token hunter2-secret-value " + "y" * 900,
                                      evidence=[f"ref-{i}-hunter2-secret-value" for i in range(40)],
                                      origin_actor=CONTROLLER, now=1.0)
    finally:
        masking.clear()
    assert "hunter2" not in b.claim and len(b.claim) <= blockers.CLAIM_MAX
    assert len(b.evidence) <= blockers.REFS_MAX and not any("hunter2" in r for r in b.evidence)


def test_diagnosis_is_reused_only_for_unchanged_evidence_and_authority(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    b, _ = _raise(store, jid)
    b = blockers.record_diagnosis(store, b.blocker_id, {"summary": "wait for new evidence"}, now=110.0)
    assert blockers.reusable_diagnosis(b)["summary"] == "wait for new evidence"
    b2, _ = _raise(store, jid, evidence=("event:99",), now=120.0)
    assert blockers.reusable_diagnosis(b2) is None


def test_human_request_validation():
    with pytest.raises(ValueError):
        blockers.validate_human_request({"decision": "x"})
    r = blockers.validate_human_request({"decision": "a" * 900, "why_authority_insufficient": "b",
                                         "evidence": ["e"] * 20, "options": ["o"] * 10, "continuing": "c"})
    assert len(r.decision) <= blockers.REQUEST_FIELD_MAX and len(r.evidence) <= 8 and len(r.options) <= 6
