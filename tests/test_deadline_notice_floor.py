"""v2.2 Phase 20 (DL-17): from the first commitment on, inherited RUNNING/READY/creator/alert notices for a deadline job
stay local. Fake notifiers/runners only; zero `send` calls is the assertion."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from steward import alerts, deadline, followup, notify, push
from steward.models import JobState, Judgment
from steward.runner_exec import FakeRunner
from steward.store import Store

NOW = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)


class CountingNotifier:
    def __init__(self):
        self.sent = []

    def send(self, event):
        self.sent.append(event)
        return True

    def send_text(self, text):
        self.sent.append(text)
        return True


def _job(store, *, commit=True, state=JobState.RUNNING):
    job = store.create_job("Ship the importer.", "/tmp")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.RUNNING.value, job.job_id))
    if commit:
        deadline.revise(store, job.job_id, action="create", judgment=Judgment.human, origin="cli",
                        request_id=f"r-{job.job_id}", reason="initial",
                        due=deadline.parse_due("+4h", now=NOW, tz="America/Los_Angeles"))
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job.job_id))
    return job.job_id


@pytest.fixture
def store(steward_home):
    return Store(steward_home)


def _event(jid, kind="READY", rev=1):
    return notify.PhoneEvent(kind=kind, job_id=jid, title="Steward: ready", body="checks passed", deep_link=None,
                             revision=rev)


@pytest.mark.parametrize("kind", ["RUNNING", "READY", "NEEDS_YOU", "CHECKS_FAILED", "RECOVERY", "BUDGET"])
def test_inherited_phone_notices_are_local_for_a_deadline_job(store, kind):
    jid = _job(store)
    fake = CountingNotifier()
    assert notify.emit(store, fake, _event(jid, kind)) is False
    assert notify.emit(store, fake, _event(jid, kind)) is False
    assert fake.sent == []
    local = [e for e in store.events(jid) if e.what == "deadline_notice_local"]
    assert len(local) == 1 and local[0].evidence["kind"] == kind


def test_notices_stay_local_after_clear_and_mode_off_and_for_children(store):
    jid = _job(store)
    deadline.revise(store, jid, action="clear", judgment=Judgment.human, origin="cli", request_id="c", reason="x")
    deadline.set_mode(store, jid, "off", judgment=Judgment.human, origin="cli", request_id="m")
    child = store.create_job("follow-up", "/tmp")
    followup.link(store, jid, child.job_id)
    fake = CountingNotifier()
    notify.emit(store, fake, _event(jid, rev=2))
    notify.emit(store, fake, _event(child.job_id))
    notify.emit_questions(store, fake, jid, waiting=1, starred=0, goal="g", qhash="h", link=None)
    assert fake.sent == []


def test_ordinary_job_notices_still_send(store):
    jid = _job(store, commit=False)
    fake = CountingNotifier()
    assert notify.emit(store, fake, _event(jid)) is True and len(fake.sent) == 1


def test_creator_push_never_touches_a_pane_for_a_deadline_job(store):
    jid = _job(store, state=JobState.ACCEPTED)
    store.kv_set(jid, "created_by", {"herdr_pane": "w1:p1", "session": "s1"})
    runner = FakeRunner()
    for event in ("accepted", "stopped", "incorporated"):
        out = push.notify_creator(store, jid, event, runner, clock=lambda: 10_000.0)
        assert out in ("skipped:deadline_local", "held")
    push.flush_due(store, jid, runner, clock=lambda: 10_000_000.0)
    assert runner.calls == []
    assert any(e.what == "creator_notify_local" for e in store.events(jid))


def test_telegram_alerts_stay_local_for_a_deadline_job(store):
    jid = _job(store, state=JobState.NEEDS_HUMAN)
    fake = CountingNotifier()
    assert alerts.check(store, jid, FakeRunner(), notifier=fake, enabled=True, link="") == []
    assert fake.sent == []
    ordinary = _job(store, commit=False, state=JobState.STOPPED)
    alerts.check(store, ordinary, FakeRunner(), notifier=fake, enabled=True, link="")
    assert fake.sent  # the ordinary path is unchanged
