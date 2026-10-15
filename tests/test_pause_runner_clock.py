# tests/test_pause_runner_clock.py
"""Spec 2026-10-13 §4.3/§5/§9 on a fake clock: 5-min provisional continue (never for done), the ~2-min soft stop, the
20-min backstop, a lost review asked again, done -> closeout, and the two regression shapes that must reach done."""
from __future__ import annotations

from pause_support import (BASE, NEW, Unit, events, mutable_git, pause_facts, pause_runner, rule, steers,
                           wire_pause_lead)
from ruling_support import StubLead, finish as finish_ticks, patch_green_verification
from test_runner_fabric import _frame, _obs

from steward import pause_checkpoint, pause_review
from steward.models import Judgment
from steward.steering import Steering


def _working(n):
    return [_obs(lifecycle="working") for _ in range(n)]


def test_no_ruling_in_five_minutes_continues_provisionally_once(steward_home):
    store, job, runner, _, clock = pause_runner(steward_home)
    runner.tick(job.job_id)                                   # the pause; no tech lead wired
    clock.advance(299)
    runner.tick(job.job_id)
    assert steers(store, job.job_id, "steward") == []
    clock.advance(2)
    runner.tick(job.job_id)
    (s,) = steers(store, job.job_id, "steward")
    assert s["text"] == ("No tech-lead ruling yet on unit u1. Continue provisionally with u2; a later ruling can "
                         "still send unit u1 back.")
    cp = pause_checkpoint.load(store, job.job_id)
    assert cp["provisional_units"] == ["u1"] and cp["unit_id"] == "u2" and cp["awaiting_review"]
    assert len(events(store, job.job_id, "pause_review_timeout")) == 1
    clock.advance(600)
    runner.tick(job.job_id)
    assert len(steers(store, job.job_id, "steward")) == 1


def test_a_done_claim_never_continues_provisionally(steward_home):
    store, job, runner, _, clock = pause_runner(steward_home, said="STEWARD-PAUSE unit=u3 next=none all done")
    runner.tick(job.job_id)
    clock.advance(400)
    runner.tick(job.job_id)
    assert steers(store, job.job_id, "steward") == [] and pause_facts(store, job.job_id)[0]["data"]["done_claim"]


def test_soft_stop_two_minutes_after_the_units_commit(steward_home):
    box = {"head": BASE, "subject": ""}
    store, job, runner, _, clock = pause_runner(steward_home, git=mutable_git(box), scripts=[_working(8)],
                                                units=[Unit("u1", "parser"), Unit("u2", "cli")])
    runner.tick(job.job_id)                                   # first sight of HEAD
    box.update(head=NEW, subject="u1: parser")
    clock.advance(10)
    runner.tick(job.job_id)                                   # the unit's commit
    clock.advance(100)
    runner.tick(job.job_id)
    assert steers(store, job.job_id, "steward") == []
    clock.advance(30)
    runner.tick(job.job_id)
    (s,) = steers(store, job.job_id, "steward")
    assert s["text"].startswith("Unit u1 is committed (22222222). Stop here and report") and "STEWARD-PAUSE" in s["text"]
    clock.advance(200)
    runner.tick(job.job_id)
    assert len(steers(store, job.job_id, "steward")) == 1 and len(events(store, job.job_id, "pause_soft_stop")) == 1


def test_a_commit_that_names_no_unit_gets_no_soft_stop(steward_home):
    """Review Focus 3 (clock half)."""
    box = {"head": BASE, "subject": ""}
    store, job, runner, _, clock = pause_runner(steward_home, git=mutable_git(box), scripts=[_working(6)],
                                                units=[Unit("u1", "parser"), Unit("u2", "cli")])
    runner.tick(job.job_id)
    box.update(head=NEW, subject="fix typo in README")
    clock.advance(10)
    runner.tick(job.job_id)
    clock.advance(200)
    runner.tick(job.job_id)
    assert steers(store, job.job_id, "steward") == []
    assert pause_checkpoint.load(store, job.job_id)["unit_commit_sha"] is None


def test_a_pause_with_no_report_and_an_unmatched_commit_is_about_the_current_unit(steward_home):
    """Review Focus 3 (pause half): no STEWARD-PAUSE line, a commit naming no unit -> the current unit, unmatched."""
    box = {"head": NEW, "subject": "fix typo"}
    store, job, runner, _, _ = pause_runner(steward_home, git=mutable_git(box), said="done for now",
                                            units=[Unit("u1", "parser"), Unit("u2", "cli")])
    runner.tick(job.job_id)
    (fact,) = pause_facts(store, job.job_id)
    assert fact["data"]["unit_id"] == "u1" and fact["data"]["unit_matched"] is False


def test_twenty_active_minutes_without_a_pause_nudges_once(steward_home):
    store, job, runner, _, clock = pause_runner(steward_home, scripts=[_working(14)])
    for _ in range(12):
        runner.tick(job.job_id)
        clock.advance(110)
    nudges = [s for s in steers(store, job.job_id, "steward") if s["text"].startswith("20 active minutes")]
    assert len(nudges) == 1
    assert [e.evidence["stage"] for e in events(store, job.job_id, "pause_backstop")] == [20]


def test_a_review_lost_to_a_restart_is_requested_again_once(steward_home):
    """Review Focus 4: the TL took the fact, then its process died; the checkpoint still says review_in_flight."""
    store, job, runner, _, clock = pause_runner(steward_home, said="STEWARD-PAUSE unit=u1 next=none")
    runner.tick(job.job_id)
    runner.arbiter.mark_reviewed(job.job_id)
    pause_checkpoint.update(store, job.job_id, lambda c: c.update(review_in_flight=True))
    clock.advance(pause_review.STALE_REVIEW_S + 1)
    runner.tick(job.job_id)
    facts = pause_facts(store, job.job_id)
    assert len(facts) == 2 and facts[-1]["data"]["resubmitted"] is True and not facts[-1]["reviewed"]
    assert not pause_checkpoint.load(store, job.job_id)["review_in_flight"]
    clock.advance(30)
    runner.tick(job.job_id)
    assert len(pause_facts(store, job.job_id)) == 2


def test_a_stale_done_sha_is_reviewed_again(steward_home):
    box = {"head": NEW, "subject": ""}
    store, job, runner, _, _ = pause_runner(steward_home, git=mutable_git(box), said="STEWARD-PAUSE unit=u1 next=none")
    runner.tick(job.job_id)
    pause_checkpoint.update(store, job.job_id, lambda c: c.update(done_sha="9" * 40))
    runner.tick(job.job_id)
    assert store.get_job(job.job_id).state.value == "RUNNING"
    assert len(events(store, job.job_id, "pause_done_stale")) == 1 and len(pause_facts(store, job.job_id)) == 2


def test_a_done_ruling_moves_the_job_to_closeout(steward_home, monkeypatch):
    patch_green_verification(monkeypatch, sha=NEW)
    store, job, runner, _, _ = pause_runner(steward_home, said="STEWARD-PAUSE unit=u1 next=none")
    wire_pause_lead(runner, {"astra-tech-lead": [rule("done")]})
    runner.tick(job.job_id)                                   # pause -> review started
    runner.tick(job.job_id)                                   # ruling applied: done
    assert pause_checkpoint.load(store, job.job_id)["done_sha"] == NEW
    runner.job_lead = StubLead(store)                         # the existing completion ruling, answered by a stub
    assert runner.tick(job.job_id).state == "CHECKS_PASSED"
    assert finish_ticks(runner, job.job_id).state == "ACCEPTED"
    assert len(events(store, job.job_id, "pause review ruled done")) == 1


def test_regression_2026_10_11_sendback_deadlock_reaches_done(steward_home, monkeypatch):
    """4060c0b0 shape (RC-2/F2): after a send-back the worker answers in prose at the already-ruled SHA. Today that yield
    is refused forever (`same_as_last_ruled`); with pause review it gets a fresh ruling and reaches done."""
    patch_green_verification(monkeypatch, sha=NEW)
    store, job, runner, _, _ = pause_runner(
        steward_home, said="No config change weakened verification; the hunks are packaging metadata only.")
    store.kv_set(job.job_id, "last_ruled_sha", NEW)
    store.append_event(job.job_id, what="sent_back", evidence={"steering_rev": 1, "text": "explain the hunks"},
                       rule="t", judgment=Judgment.model_informed_code_authorized)
    wire_pause_lead(runner, {"astra-tech-lead": [rule("done", reason="the answer settles the send-back")]})
    runner.tick(job.job_id)
    (fact,) = pause_facts(store, job.job_id)
    assert fact["data"]["failed"] == ["same_as_last_ruled"] and fact["data"]["done_claim"] is True
    runner.tick(job.job_id)
    runner.job_lead = StubLead(store)
    runner.tick(job.job_id)
    assert finish_ticks(runner, job.job_id).state == "ACCEPTED"


def test_regression_2026_10_13_lost_ack_reaches_done(steward_home, monkeypatch):
    """9dd959b8 / 201550e0 shape: revs 1 and 2 delivered together at launch, only rev 1 acknowledged. Today every done
    is deferred `unacked_steering [2]`; with pause review the open instruction rides in the packet and done proceeds."""
    patch_green_verification(monkeypatch, sha=NEW)
    frames = (_frame(8, "ack", {"rev": 1, "stage": "acknowledged"}),
              _frame(9, "done", {"sha": NEW, "unconsumed_revs": [2], "last_rev": 2}))
    store, job, runner, _, _ = pause_runner(steward_home, frames=frames, said="STEWARD-PAUSE unit=u1 next=none")
    steering = Steering(store)
    steering.record(job.job_id, "focus", "stay inside the parser module", "req-lost-ack-1")
    steering.record(job.job_id, "authority", "no network access", "req-lost-ack-2")
    wire_pause_lead(runner, {"astra-tech-lead": [rule("done")]})
    runner.tick(job.job_id)
    (fact,) = pause_facts(store, job.job_id)
    assert fact["data"]["unacked"] == [2]
    assert events(store, job.job_id, "worker done deferred: steering not yet acknowledged") == []
    runner.tick(job.job_id)
    runner.job_lead = StubLead(store)
    runner.tick(job.job_id)
    assert finish_ticks(runner, job.job_id).state == "ACCEPTED"
