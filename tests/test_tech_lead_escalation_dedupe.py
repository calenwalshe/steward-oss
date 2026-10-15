"""Tech-lead escalations: one open approval per job (later text is appended), and stale ones close as superseded."""

from __future__ import annotations

import pytest

from steward import approvals, closeout, sendback
from steward import tech_lead_actions as tla
from steward.models import JobState, Judgment
from steward.store import Store
from test_runner import _contracted_job


@pytest.fixture
def env(steward_home, monkeypatch):
    # These assertions cover the legacy approval fallback. Ownership mode records
    # escalation as a durable blocker instead of opening a standalone approval.
    monkeypatch.setenv("STEWARD_OWNERSHIP", "0")
    store = Store(steward_home)
    job = _contracted_job(store, "fix it")
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    ex = tla.ActionExecutor(store, runner_exec=None, notify_fn=lambda *a: None)
    return store, job.job_id, ex


def _pending(store, jid):
    return [a for a in approvals.list_approvals(store, jid, status="pending") if a["kind"] == "tech_lead"]


def test_repeat_escalation_appends_to_existing_approval(env):
    store, jid, ex = env
    first = ex.escalate_text(jid, "tests fail in foo", kind="tech_lead_exhausted")
    again = ex.escalate_text(jid, "still failing; consolidate into existing", kind="tech_lead_exhausted")
    assert again["id"] == first["id"]
    assert len(approvals.list_approvals(store, jid)) == 1
    rec = approvals.get_approval(store, jid, first["id"])
    assert "still failing" in rec["question"] and rec["updates"][0]["text"].startswith("still failing")
    assert any(e.what == "tech_lead_escalation_appended" for e in store.events(jid))


def test_new_escalation_after_decision_opens_fresh(env):
    store, jid, ex = env
    first = ex.escalate_text(jid, "a", kind="tech_lead_exhausted")
    approvals.decide(store, jid, first["id"], "deny")
    assert ex.escalate_text(jid, "b", kind="tech_lead_exhausted")["id"] != first["id"]


def test_send_back_closes_pending_tech_lead_escalations(env):
    store, jid, ex = env
    rec = ex.escalate_text(jid, "stuck", kind="tech_lead_exhausted")
    closed = approvals.close_tech_lead_superseded(store, jid, "sent back")
    assert closed == [rec["id"]] and not _pending(store, jid)
    got = approvals.get_approval(store, jid, rec["id"])
    assert (got["status"], got["decided_by"], got["closed_reason"]) == ("denied", "code", "superseded: sent back")
    assert any(e.what == "tech_lead approvals superseded" and e.evidence["approval_ids"] == [rec["id"]]
               for e in store.events(jid))


def test_stop_closes_tech_lead_escalations(env):
    store, jid, ex = env
    ex.escalate_text(jid, "stuck", kind="tech_lead_exhausted")
    store.set_state(jid, JobState.STOPPING, what="t", evidence={}, rule="t", judgment=Judgment.human)
    assert _pending(store, jid)
    store.set_state(jid, JobState.STOPPED, what="t", evidence={}, rule="t", judgment=Judgment.code)
    assert not _pending(store, jid)


def test_send_back_closes_tech_lead_escalation(env):
    store, jid, ex = env
    rec = ex.escalate_text(jid, "stuck", kind="tech_lead_exhausted")
    store.set_state(jid, JobState.NEEDS_HUMAN, what="t", evidence={}, rule="t", judgment=Judgment.code)
    res = sendback.send_back(store, jid, "try again")
    assert res["accepted"] and rec["id"] in res["closed_approvals"] and not _pending(store, jid)
