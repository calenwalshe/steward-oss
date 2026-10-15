"""u4 (stall fixes 2026-10-14): a lead escalation gets a real delivery receipt and one retry. Evidence: job af319063
`tech_lead_escalated` (lead_escalation) at 01:36:42 was never seen by the owner, yet its `owner_notice` intent says
`{"notified": true}`. The runner-wired lead notified through `Runner._notify`, which keys the phone dedupe on the job
revision (already used by an earlier NEEDS_YOU) and discards `notify.emit`'s False; `notify_owner` then recorded a
receipt for a notice that never left. Every transport here is a recording fake: no message is sent."""
from __future__ import annotations

import time

from steward import blockers, health, liveness, notify, ownership
from steward.blockers import HumanRequest
from steward.models import JobState, Judgment
from steward.runner import Runner, wire_job_lead
from steward.store import Store
from test_runner import _contracted_job, _Factory, _fake_diagnoser, _git_runner, _Clock
from test_tech_lead_no_clog import ASTRA, FABLE


class Flaky:
    """Recording notifier: the first `fails` sends report failure."""

    def __init__(self, fails=0):
        self.fails, self.calls, self.sent = fails, 0, []

    def send(self, event) -> bool:
        self.calls += 1
        if self.calls <= self.fails:
            return False
        self.sent.append(event)
        return True


def _job(store):
    job = _contracted_job(store, "fix it")
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    return job.job_id


def _escalate(store, jid, **kw):
    req = HumanRequest(decision="unit-3 sent back 3 times: decide", why_authority_insufficient="loop guard")
    return ownership.raise_lead_escalation(store, jid, req, kind="lead_escalation", lead_id="astra", now=time.time(),
                                           **kw)


def _whats(store, jid, what):
    return [e.evidence for e in store.events(jid) if e.what == what]


def test_delivered_notice_records_a_receipt_event(steward_home):
    store = Store(steward_home); jid = _job(store); n = Flaky()
    b = _escalate(store, jid, notifier=n)
    assert len(n.sent) == 1
    assert _whats(store, jid, "owner_notice_delivered") == [
        {"blocker_id": b.blocker_id, "kind": "NEEDS_YOU", "channel": "notifier", "attempts": 1}]
    assert ownership.undelivered_notices(store) == []


def test_a_failed_send_is_retried_once(steward_home):
    store = Store(steward_home); jid = _job(store); n = Flaky(fails=1)
    b = _escalate(store, jid, notifier=n)
    assert n.calls == 2 and len(n.sent) == 1
    assert _whats(store, jid, "owner_notice_delivered")[0]["attempts"] == 2
    assert store.kv_get(jid, ownership.NOTIFIED_PREFIX + b.blocker_id)


def test_undelivered_after_the_retry_is_recorded_and_red_in_health(steward_home, monkeypatch, tmp_path):
    monkeypatch.setattr(liveness, "PROJECTS_DIR", tmp_path / "projects")
    store = Store(steward_home); jid = _job(store); n = Flaky(fails=5)
    b = _escalate(store, jid, notifier=n)
    assert n.calls == 2 and n.sent == []
    assert not store.kv_get(jid, ownership.NOTIFIED_PREFIX + b.blocker_id)  # no false receipt
    ev = _whats(store, jid, "owner_notice_undelivered")
    assert len(ev) == 1 and ev[0]["blocker_id"] == b.blocker_id and ev[0]["attempts"] == 2
    assert [u["blocker_id"] for u in ownership.undelivered_notices(store)] == [b.blocker_id]
    now = time.time()
    store.kv_set(liveness.PSEUDO, "tick_at", now)
    comp = liveness.component(store, now=now)
    assert comp.ok is False and "undelivered" in comp.detail and jid[:8] in comp.detail


def test_a_notify_fn_that_reports_false_is_not_a_receipt(steward_home):
    store = Store(steward_home); jid = _job(store)
    b = _escalate(store, jid, notify_fn=lambda *a: False)
    assert not store.kv_get(jid, ownership.NOTIFIED_PREFIX + b.blocker_id)
    assert len(_whats(store, jid, "owner_notice_undelivered")) == 1


def test_no_notifier_at_all_is_undelivered_not_silent(steward_home):
    store = Store(steward_home); jid = _job(store)
    b = _escalate(store, jid)
    assert [u["blocker_id"] for u in ownership.undelivered_notices(store)] == [b.blocker_id]


def test_resolved_blocker_is_no_longer_undelivered(steward_home):
    store = Store(steward_home); jid = _job(store)
    b = _escalate(store, jid)
    blockers.transition(store, b.blocker_id, "resolved", actor=ownership.CONTROLLER, reason="t", now=time.time())
    assert ownership.undelivered_notices(store) == []


def test_runner_wired_lead_escalation_reaches_the_phone_even_when_the_job_revision_was_notified(steward_home):
    """The live failure: the job revision's NEEDS_YOU had already gone out, so the runner's job-revision notice was a
    dedupe hit. The runner-wired executor must notify per blocker (its own revision) and report the truth."""
    store = Store(steward_home); jid = _job(store); n = Flaky()
    store.kv_set(jid, f"notified:NEEDS_YOU:{store.get_revision(jid)}", True)
    runner = Runner(store, _Factory(scripts=[[]]), _git_runner(), _fake_diagnoser(), clock=_Clock(), notifier=n)
    lead = wire_job_lead(runner, [ASTRA, FABLE], invoke=lambda *a: "")
    req = HumanRequest(decision="unit-3 sent back 3 times: decide", why_authority_insufficient="loop guard")
    lead.executor.escalate_text(jid, "unit-3 loop", kind="lead_escalation", request=req, lead_id="astra")
    assert len(n.sent) == 1 and n.sent[0].revision.startswith("blocker:")
    assert len(_whats(store, jid, "owner_notice_delivered")) == 1


class Raising:
    """Recording notifier whose first `raises` sends raise (a transport crash, not a refusal)."""

    def __init__(self, raises):
        self.raises, self.calls, self.sent = raises, 0, []

    def send(self, event) -> bool:
        self.calls += 1
        if self.calls <= self.raises:
            raise OSError("transport down")
        self.sent.append(event)
        return True


def test_a_raising_transport_is_retried_once_then_delivered(steward_home):
    store = Store(steward_home); jid = _job(store); n = Raising(raises=1)
    _escalate(store, jid, notifier=n)
    assert n.calls == 2 and len(n.sent) == 1
    ev = _whats(store, jid, "owner_notice_delivered")
    assert len(ev) == 1 and ev[0]["attempts"] == 2 and ev[0]["channel"] == "notifier"
    assert _whats(store, jid, "owner_notice_undelivered") == []


def test_a_transport_raising_on_both_attempts_is_undelivered(steward_home):
    store = Store(steward_home); jid = _job(store); n = Raising(raises=5)
    b = _escalate(store, jid, notifier=n)
    assert n.calls == 2 and n.sent == []
    ev = _whats(store, jid, "owner_notice_undelivered")
    assert len(ev) == 1 and ev[0]["attempts"] == 2 and ev[0]["blocker_id"] == b.blocker_id
    assert "transport down" in ev[0]["reason"]
    assert not store.kv_get(jid, ownership.NOTIFIED_PREFIX + b.blocker_id)


def test_a_raising_notify_fn_is_retried_once(steward_home):
    store = Store(steward_home); jid = _job(store); calls = []

    def fn(*a):
        calls.append(a)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return True
    _escalate(store, jid, notify_fn=fn)
    assert len(calls) == 2
    ev = _whats(store, jid, "owner_notice_delivered")
    assert len(ev) == 1 and ev[0]["attempts"] == 2 and ev[0]["channel"] == "notify_fn"


def test_missing_transport_is_one_attempt_only(steward_home):
    store = Store(steward_home); jid = _job(store)
    _escalate(store, jid)
    ev = _whats(store, jid, "owner_notice_undelivered")
    assert len(ev) == 1 and ev[0]["attempts"] == 1 and ev[0]["reason"] == "no notifier configured"
