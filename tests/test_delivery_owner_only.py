"""Clean-install finding 2026-10-14: an operator agent's approve on an "Incorporate job …?" card was recorded and
resolved the blocker, but delivery only honours the owner's decision (decided_by == "human"), so nothing merged
and nothing said why. A non-owner decision on a delivery card is now refused up front and the card stays open."""
from __future__ import annotations

from steward import approvals, cli, delivery
from steward.store import Store


def _card(store):
    job = store.create_job("g", "/repo")
    a = approvals.open_approval(store, job.job_id, f"Incorporate job {job.job_id[:8]} (abc) into main of repo?",
                                kind=delivery.APPROVAL_KIND)
    aid = a["id"] if isinstance(a, dict) else a
    approvals.annotate(store, job.job_id, aid, milestone="incorporation", revision="abc")
    return job, aid


def _decide(store, job, aid, op, actor):
    cmd = {"op": op, "args": {"job_id": job.job_id, "approval_id": aid}, "origin": "cli", "request_id": f"r-{op}"}
    if actor:
        cmd["actor"] = actor
    return cli._build_control_handlers(store)["approve" if op == "approve" else "deny"](cmd)


def test_an_agent_cannot_decide_a_delivery_card(steward_home):
    store = Store(steward_home)
    job, aid = _card(store)
    for op in ("approve", "deny"):
        r = _decide(store, job, aid, op, "operator_agent:claude-code")
        assert r["accepted"] is False and r["error_kind"] == "owner_only" and "owner" in r["detail"]
    assert approvals.get_approval(store, job.job_id, aid)["status"] == "pending"


def test_the_owner_still_decides_a_delivery_card(steward_home):
    store = Store(steward_home)
    job, aid = _card(store)
    r = _decide(store, job, aid, "approve", None)
    assert r["accepted"] is True
    assert approvals.get_approval(store, job.job_id, aid)["decided_by"] == "human"
