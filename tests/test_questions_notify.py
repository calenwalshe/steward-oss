"""One NEEDS_YOU per waiting question queue (Phase 17-03, G8): counts and the goal only, deduped by the queue hash."""

from __future__ import annotations

import json

import pytest
from questions_helpers import CTX, commit_file, events_named, fixture_text, git, make_store_job, set_state

from steward import notify, phone, questions, questions_delivery
from steward.models import JobState
from steward.runner_exec import SubprocessRunner


class Recording:
    def __init__(self, ok=True, boom=False):
        self.events: list[notify.PhoneEvent] = []
        self.ok, self.boom = ok, boom

    def send(self, event):
        if self.boom:
            raise RuntimeError("telegram down")
        self.events.append(event)
        return self.ok


def test_phone_event_accepts_a_string_revision(steward_home):
    ev = notify.PhoneEvent(kind="NEEDS_YOU", job_id="j", title="t", body="b", deep_link=None, revision="questions:ab12")
    assert ev.revision == "questions:ab12"
    with pytest.raises(ValueError):
        notify.PhoneEvent(kind="NEEDS_YOU", job_id="j", title="x" * 41, body="b", deep_link=None, revision="questions:ab12")
    with pytest.raises(ValueError):
        notify.PhoneEvent(kind="NEEDS_YOU", job_id="j", title="t", body="x" * 141, deep_link=None, revision="questions:ab12")
    assert notify.PhoneEvent(kind="NEEDS_YOU", job_id="j", title="t", body="b", deep_link=None, revision=3).revision == 3
    store, job_id = make_store_job()
    rec = Recording()
    ev = notify.PhoneEvent(kind="NEEDS_YOU", job_id=job_id, title="t", body="b", deep_link=None, revision="questions:ab12")
    assert notify.emit(store, rec, ev) is True and notify.emit(store, rec, ev) is False
    assert store.kv_get(job_id, "notified:NEEDS_YOU:questions:ab12") is True and len(rec.events) == 1


def test_emit_questions_shape(steward_home):
    store, job_id = make_store_job()
    rec = Recording()
    goal = "Discuss and decide the efficiency watcher " * 6
    assert notify.emit_questions(store, rec, job_id, waiting=15, starred=14, goal=goal, qhash="ab12",
                                 link="moshi://herdr?workspace=w1") is True
    (ev,) = rec.events
    assert ev.kind == "NEEDS_YOU" and ev.title == f"Steward: questions ({job_id[:8]})" and len(ev.title) <= 40
    assert ev.body.startswith("15 questions (14 ★) · Discuss and decide") and len(ev.body) == 140
    assert ev.body.endswith("…") and "\n" not in ev.body
    assert ev.deep_link == "moshi://herdr?workspace=w1" and ev.revision == "questions:ab12"
    store2, job2 = make_store_job()
    rec2 = Recording()
    notify.emit_questions(store2, rec2, job2, waiting=1, starred=1, goal="short goal", qhash="cd34", link=None)
    assert rec2.events[0].body == "1 question (1 ★) · short goal"
    failing = Recording(ok=False)
    assert notify.emit_questions(store2, failing, job2, waiting=2, starred=0, goal="g", qhash="zz", link=None) is False


def _worktree(store, job_id):
    wt = store.home / "worktrees" / job_id
    wt.mkdir(parents=True)
    git(wt, "init", "-q")
    base = commit_file(wt, "README.md", "hello\n", "baseline")
    store.add_check(job_id, commit_sha=base, command="pytest", exit_code=0, results={}, label="baseline")
    return wt


def _items(ids):
    return json.dumps([{"id": i, "question": f"Question {i}?", "options": ["a", "b"], "recommended": "a"} for i in ids])


def _scan(store, job_id, wt, ids):
    commit_file(wt, "STEWARD-QUESTIONS.json", _items(ids), "questions")
    res = questions.scan(store, job_id, SubprocessRunner())
    assert res.error is None
    return res


def _link(job_id):
    return "moshi://herdr?workspace=w1&tab=t1"


def test_one_push_per_queue_revision(steward_home):
    store, job_id = make_store_job()
    wt = _worktree(store, job_id)
    rec = Recording()
    res = _scan(store, job_id, wt, ["q0", "q1", "q2"])
    assert questions_delivery.notify_queue(store, rec, job_id, res, link_fn=_link) is True
    assert rec.events[0].body == "3 questions (3 ★) · answer questions"
    assert rec.events[0].deep_link == _link(job_id) and rec.events[0].revision == f"questions:{res.queue_hash}"
    assert questions_delivery.notify_queue(store, rec, job_id, res, link_fn=_link) is False  # same hash: silent
    again = questions.scan(store, job_id, SubprocessRunner(), force=True)
    assert again.queue_changed is False
    assert questions_delivery.notify_queue(store, rec, job_id, again, link_fn=_link) is False
    assert len(rec.events) == 1

    res = _scan(store, job_id, wt, ["q0", "q1", "q2", "q3"])  # a question was added: a new hash sends again
    assert questions_delivery.notify_queue(store, rec, job_id, res, link_fn=_link) is True
    assert rec.events[1].body.startswith("4 questions (4 ★)")

    res = _scan(store, job_id, wt, ["q1", "q2", "q3", "q9"])  # same size, different members: once more
    assert res.summary["waiting"] == 4
    assert questions_delivery.notify_queue(store, rec, job_id, res, link_fn=_link) is True
    assert len(rec.events) == 3 and len({e.revision for e in rec.events}) == 3

    questions.apply(store, job_id, accept_recommended=True)  # the queue empties: nothing to send
    commit_file(wt, "x.txt", "x\n", "more")
    res = questions.scan(store, job_id, SubprocessRunner())
    assert res.summary["waiting"] == 0
    assert questions_delivery.notify_queue(store, rec, job_id, res, link_fn=_link) is False
    assert len(rec.events) == 3


def test_a_failing_notifier_records_and_never_raises(steward_home):
    store, job_id = make_store_job()
    wt = _worktree(store, job_id)
    res = _scan(store, job_id, wt, ["q0"])
    assert questions_delivery.notify_queue(store, Recording(boom=True), job_id, res, link_fn=_link) is False
    (ev,) = events_named(store, job_id, "notify_failed")
    assert ev.evidence["kind"] == "NEEDS_YOU" and ev.evidence["revision"] == f"questions:{res.queue_hash}"
    assert questions_delivery.notify_queue(store, None, job_id, res, link_fn=_link) is False

    def bad_link(_):
        raise RuntimeError("no herdr")

    rec = Recording()
    assert questions_delivery.notify_queue(store, rec, job_id, res, link_fn=bad_link) is True
    assert rec.events[0].deep_link is None


def test_runner_step_notifies_on_queue_change(steward_home, monkeypatch):
    from test_questions_runner import FIX, _commit, _setup

    store, job, wt, runner = _setup(steward_home)
    rec = Recording()
    runner.notifier = rec
    seen = []
    monkeypatch.setattr(phone, "phone_link", lambda rx, st, jid: seen.append((rx, st, jid)) or "moshi://herdr?workspace=w9&tab=phone")
    _commit(wt, CTX, (FIX / "phase16_context_before.md").read_text())
    runner.tick(job.job_id)
    assert len(questions.load(store, job.job_id)) == 15
    assert len(rec.events) == 1
    assert rec.events[0].kind == "NEEDS_YOU" and rec.events[0].body.startswith("15 questions (15 ★)")
    assert rec.events[0].deep_link == "moshi://herdr?workspace=w9&tab=phone"
    assert seen == [(runner.runner_exec, store, job.job_id)]
    runner.tick(job.job_id)
    _commit(wt, "notes.txt", "more\n")
    runner.tick(job.job_id)  # head moved, queue unchanged
    assert len(rec.events) == 1


def test_runner_link_falls_back_to_the_job_tab(steward_home):
    from test_questions_runner import FIX, _commit, _setup

    store, job, wt, runner = _setup(steward_home)
    rec = Recording()
    runner.notifier = rec
    store.kv_set(job.job_id, "herdr_workspace", "w1")
    store.kv_set(job.job_id, "herdr_tab", "w1:t2")
    _commit(wt, CTX, (FIX / "phase16_context_before.md").read_text())
    runner.tick(job.job_id)
    assert rec.events[0].deep_link == notify.deep_link("w1", "w1:t2", None)


def test_control_tick_notifies_for_resting_jobs(steward_home):
    store, job_id = make_store_job()
    wt = _worktree(store, job_id)
    assert questions.scan(store, job_id, SubprocessRunner()).error is None  # scanned: nothing committed yet
    set_state(store, job_id, JobState.NEEDS_HUMAN)
    commit_file(wt, CTX, fixture_text("phase16_context_before.md"), "the last commit")
    rec = Recording()
    out = questions_delivery.tick(store, SubprocessRunner(), now=1000.0, notifier=rec)
    assert [s["job"] for s in out["scanned"]] == [job_id] and len(rec.events) == 1
    assert rec.events[0].body.startswith("15 questions (15 ★)")
    questions_delivery.tick(store, SubprocessRunner(), now=1100.0, notifier=rec)
    assert len(rec.events) == 1

    store2, job2 = make_store_job()
    wt2 = _worktree(store2, job2)
    questions.scan(store2, job2, SubprocessRunner())
    set_state(store2, job2, JobState.NEEDS_HUMAN)
    commit_file(wt2, CTX, fixture_text("phase16_context_before.md"), "the last commit")
    out = questions_delivery.tick(store2, SubprocessRunner(), now=1000.0)  # no notifier: nothing sent, nothing raised
    assert out["errors"] == [] and len(questions.load(store2, job2)) == 15


def test_notifier_default_is_null(steward_home, monkeypatch):
    monkeypatch.delenv("STEWARD_NOTIFIER", raising=False)
    store, job_id = make_store_job()
    notifier = notify.notifier_from_env(SubprocessRunner())
    assert isinstance(notifier, notify.NullNotifier)
    assert notify.emit_questions(store, notifier, job_id, waiting=2, starred=1, goal="g", qhash="h1", link=None) is True
    assert not [e for e in store.events(job_id) if e.what == "exception_used"]
    assert not events_named(store, job_id, "notify_failed")
