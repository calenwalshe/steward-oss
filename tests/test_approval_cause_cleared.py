"""Issue B: a pending tech_lead approval closes when its blocker's cause is gone; otherwise it stays pending."""
from __future__ import annotations

import json

import pytest

from steward import approvals, blockers, ownership
from steward.models import JobState, Judgment
from steward.store import Store
from ownership_fixtures import CONTROLLER, contracted

CAUSE = {"on": True}


def _cause(store, job_id, b, facts):
    return "holds" if CAUSE["on"] else "cleared"


@pytest.fixture
def env(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_OWNERSHIP", "1")
    monkeypatch.setitem(ownership.PREDICATES, "t_cause", _cause)
    CAUSE["on"] = True
    store = Store(steward_home)
    jid = contracted(store)
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    return store, jid


def _linked(store, jid, subject, clearing):
    b, _ = blockers.raise_blocker(store, jid, kind="unclassified_hold", subject=subject, claim=subject,
                                  evidence=[f"event:{subject}"], origin_actor=CONTROLLER, now=100.0,
                                  disposition="confirmed_human", clearing=clearing)
    rec = approvals.open_approval(store, jid, f"decide {subject}", kind="tech_lead")
    approvals.annotate(store, jid, rec["id"], blocker_id=b.blocker_id)
    return b, rec["id"]


def _status(store, jid, aid):
    return approvals.get_approval(store, jid, aid)


def test_cleared_cause_resolves_blocker_and_closes_approval(env):
    store, jid = env
    b, aid = _linked(store, jid, "a", {"predicate": "t_cause"})
    CAUSE["on"] = False
    ownership._clear_cause_cleared(store, store.get_job(jid), 200.0, None)
    got = blockers.get(store, b.blocker_id)
    assert got.disposition == "resolved" and got.resolved_epoch is not None
    assert got.resolution["reason"] == "cause cleared"
    rec = _status(store, jid, aid)
    assert (rec["status"], rec["closed_reason"]) == ("denied", "superseded: cause cleared")
    evs = [e for e in store.events(jid) if e.what == "tech_lead approvals superseded"]
    assert len(evs) == 1 and evs[0].evidence["approval_ids"] == [aid]


def test_persisting_cause_keeps_blocker_and_approval(env):
    store, jid = env
    b, aid = _linked(store, jid, "a", {"predicate": "t_cause"})
    ownership._clear_cause_cleared(store, store.get_job(jid), 200.0, None)
    assert blockers.get(store, b.blocker_id).is_open
    assert _status(store, jid, aid)["status"] == "pending"


def test_other_blockers_approval_untouched(env):
    store, jid = env
    gone, gone_aid = _linked(store, jid, "gone", {"predicate": "t_cause"})
    other, other_aid = _linked(store, jid, "other", {"predicate": "approval_decided",
                                                      "params": {"approval_id": "x"}})
    CAUSE["on"] = False
    ownership._clear_cause_cleared(store, store.get_job(jid), 200.0, None)
    assert _status(store, jid, gone_aid)["status"] == "denied"
    assert blockers.get(store, other.blocker_id).is_open
    assert _status(store, jid, other_aid)["status"] == "pending"


def test_live_shape_test_weakening_cleared_through_reconcile(env):
    """Live job fec63227: test_weakening blocker whose recorded clearing is approval_decided; the job has moved on."""
    store, jid = env
    b, aid = _linked(store, jid, "tw", {"predicate": "approval_decided", "params": {"approval_id": "x"}})
    with store._conn:
        store._conn.execute("UPDATE blockers SET kind = 'test_weakening' WHERE blocker_id = ?", (b.blocker_id,))
    other, other_aid = _linked(store, jid, "own", {"predicate": "approval_decided", "params": {"approval_id": "y"}})
    with store._conn:  # an owner-only kind: only the owner's decision clears it
        store._conn.execute("UPDATE blockers SET kind = 'hard_constraint' WHERE blocker_id = ?", (other.blocker_id,))
    ownership.reconcile_job(store, jid, now=200.0)  # RUNNING, no new attempt: cause (job_resumed) still holds
    assert _status(store, jid, aid)["status"] == "pending"
    store.set_state(jid, JobState.WORKER_FINISHED, what="t", evidence={}, rule="t", judgment=Judgment.code)
    ownership.reconcile_job(store, jid, now=300.0)
    assert blockers.get(store, b.blocker_id).disposition == "resolved"
    assert _status(store, jid, aid)["closed_reason"] == "superseded: cause cleared"
    assert _status(store, jid, other_aid)["status"] == "pending"
