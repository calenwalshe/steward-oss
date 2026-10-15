# tests/test_pause_ruling.py
"""Spec 2026-10-13 §4.4/§7: one ruling per pause — continue / send_back / done / ask_owner — applied by code: the code
check beats a `done`, the third send-back of a unit asks the owner, a late ruling can send a provisional unit back, and a
folded newer pause is reviewed after the outstanding ruling."""
from __future__ import annotations

from pause_support import NEW, Unit, pause_facts, set_pause, steers, verdicts
from test_runner_fabric import _contracted_job

from steward import pause_checkpoint
from steward import pause_review as pr
from steward.models import JobState, Judgment
from steward.store import Store

FACT = {"seq": 9, "sha": NEW, "yield_lane": "CANDIDATE", "failed": [], "unit_id": "u1", "done_claim": False,
        "next_unit": "u2", "pause_kind": "pause", "pause_seq": 1}


def _setup(steward_home, *, units=("u1", "u2", "u3"), fact=None, **cp):
    store = Store(steward_home)
    job = _contracted_job(store)
    set_pause(store, job.job_id, "unit", [Unit(u, f"title {u}", f"pytest -q tests/test_{u}.py") for u in units])
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    fact = dict(fact or FACT)
    base = {"awaiting_review": True, "review_requested_at": 100.0, "review_in_flight": True, "pause_seq": 1,
            "last_pause_fact": fact, "unit_id": "u1"}
    pause_checkpoint.update(store, job.job_id, lambda c: c.update({**base, **cp}))
    return store, job.job_id, fact


def _apply(store, jid, fact, outcome, *, head=NEW, now=160.0, escalate=None, notes=None, **params):
    p = {"outcome": outcome, "reason": params.pop("reason", "checked"), **params}
    return pr.apply_ruling(store, jid, p, fact=fact, lead="astra-tech-lead", head=head, now=now, escalate=escalate,
                           notify_fn=(lambda *a: notes.append(a)) if notes is not None else None)


def test_continue_steers_the_next_unit_and_marks_this_one_met(steward_home):
    store, jid, fact = _setup(steward_home)
    out = _apply(store, jid, fact, "continue", next_unit={"id": "u2", "title": "parser", "finish_line": "pytest -q"})
    assert out["outcome"] == "continue" and out["overridden"] is None
    (s,) = steers(store, jid, "tech_lead")
    assert s["text"] == "Tech lead: unit u1 accepted. Go: unit u2 - parser (finish line: pytest -q)."
    cp = pause_checkpoint.load(store, jid)
    assert (cp["verdict"], cp["unit_id"], cp["met_units"], cp["last_reviewed_sha"]) == ("continue", "u2", ["u1"], NEW)
    assert not cp["awaiting_review"] and not cp["review_in_flight"] and cp["n"] == 1 and len(cp["history"]) == 1
    assert cp["open_instruction"] == s["text"]
    (v,) = verdicts(store, jid)
    assert v["outcome"] == "continue" and v["wait_s"] == 60.0 and v["provisional"] is False


def test_continue_without_a_named_next_unit_uses_the_first_unmet_contract_unit(steward_home):
    store, jid, fact = _setup(steward_home)
    _apply(store, jid, fact, "continue")
    assert "Go: unit u2 - title u2 (finish line: pytest -q tests/test_u2.py)." in steers(store, jid)[0]["text"]


def test_send_back_says_what_is_unmet_and_keeps_the_unit(steward_home):
    store, jid, fact = _setup(steward_home)
    _apply(store, jid, fact, "send_back", text="the parser test is missing")
    (s,) = steers(store, jid, "tech_lead")
    assert s["text"] == "Tech lead: unit u1 not met: the parser test is missing. Fix it, commit, and pause again."
    assert pause_checkpoint.load(store, jid)["unit_id"] == "u1"


def test_done_when_every_unit_is_met_hands_the_sha_to_closeout(steward_home):
    store, jid, fact = _setup(steward_home, units=("u1",))
    out = _apply(store, jid, fact, "done")
    cp = pause_checkpoint.load(store, jid)
    assert out["outcome"] == "done" and cp["done_sha"] == NEW and steers(store, jid) == []
    assert cp["open_instruction"] is None and cp["met_units"] == ["u1"]


def test_code_check_turns_done_with_unmet_units_into_send_back(steward_home):
    store, jid, fact = _setup(steward_home)
    out = _apply(store, jid, fact, "done")
    assert out["outcome"] == "send_back" and out["claimed"] == "done"
    assert out["overridden"] == "code check: units not met: u2, u3"
    assert "code check: units not met: u2, u3" in steers(store, jid)[0]["text"]
    assert pause_checkpoint.load(store, jid)["done_sha"] is None


def test_code_check_turns_done_with_failing_checks_into_send_back(steward_home):
    store, jid, fact = _setup(steward_home, units=("u1",))
    store.append_event(jid, what="verification recorded", evidence={"commit_sha": NEW, "new_failures": ["t::a"]},
                       rule="verify_at_exact_commit", judgment=Judgment.code)
    assert _apply(store, jid, fact, "done")["overridden"] == "code check: checks fail at 22222222: t::a"


def test_code_check_turns_done_with_a_dirty_tree_or_unknown_head_into_send_back(steward_home):
    store, jid, fact = _setup(steward_home, units=("u1",), fact={**FACT, "failed": ["dirty_worktree"]})
    assert _apply(store, jid, fact, "done")["overridden"] == "code check: preconditions failed: dirty_worktree"
    (steward_home / "b").mkdir()
    store2, jid2, fact2 = _setup(steward_home / "b", units=("u1",))
    assert _apply(store2, jid2, fact2, "done", head=None)["overridden"] == "code check: the worktree HEAD is unknown"


def test_done_ignores_same_as_last_ruled_and_unacked_steering(steward_home):
    store, jid, fact = _setup(steward_home, units=("u1",),
                              fact={**FACT, "yield_lane": "STOPPED", "failed": ["same_as_last_ruled", "unacked_steering"]})
    assert _apply(store, jid, fact, "done")["outcome"] == "done"


def test_done_after_head_moved_is_reviewed_again_not_applied(steward_home):
    store, jid, fact = _setup(steward_home, units=("u1",))
    out = _apply(store, jid, fact, "done", head="9" * 40)
    assert out["outcome"] is None and verdicts(store, jid) == []
    cp = pause_checkpoint.load(store, jid)
    assert cp["awaiting_review"] and not cp["review_in_flight"] and cp["done_sha"] is None
    assert pause_facts(store, jid)[-1]["data"]["resubmitted"] is True
    assert [e.what for e in store.events(jid) if e.what == "pause_ruling_deferred"] == ["pause_ruling_deferred"]


def test_ask_owner_escalates_and_the_worker_waits(steward_home):
    store, jid, fact = _setup(steward_home)
    asked: list = []
    _apply(store, jid, fact, "ask_owner", decision="Ship without the migration?", escalate=asked.append)
    cp = pause_checkpoint.load(store, jid)
    assert asked == ["Ship without the migration?"] and steers(store, jid) == []
    assert cp["waiting_on"] == "owner" and cp["open_instruction"] == "Ship without the migration?"


def test_the_third_send_back_of_a_unit_asks_the_owner(steward_home):
    history = [{"unit_id": "u1", "outcome": "send_back"}, {"unit_id": "u1", "outcome": "send_back"}]
    store, jid, fact = _setup(steward_home, history=history)
    asked: list = []
    out = _apply(store, jid, fact, "send_back", text="still no test", escalate=asked.append)
    assert out["outcome"] == "ask_owner" and out["overridden"] == "loop guard: unit u1 sent back 3 times"
    assert len(asked) == 1 and "sent back 2 times" in asked[0] and steers(store, jid) == []


def test_late_send_back_of_a_provisional_unit_after_k_plus_2_started(steward_home):
    """Review Focus 2: u1's review was late; the worker continued to u2, paused (folded), continued to u3."""
    store, jid, fact = _setup(steward_home, provisional_units=["u1", "u2"], unit_id="u3", pause_seq=2,
                              last_pause_fact={**FACT, "unit_id": "u2", "next_unit": "u3", "pause_seq": 2})
    out = _apply(store, jid, fact, "send_back", text="u1 broke the import order")
    assert out["outcome"] == "send_back"
    sent = [s["text"] for s in steers(store, jid, "tech_lead")]
    assert sent[0] == ("Tech lead (late review of unit u1): unit u1 not met: u1 broke the import order. "
                       "Fix it, commit, and pause again.")
    cp = pause_checkpoint.load(store, jid)
    assert cp["provisional_units"] == ["u2"] and cp["unit_id"] == "u1"
    assert verdicts(store, jid)[0]["provisional"] is True
    assert pause_facts(store, jid)[-1]["data"]["pause_seq"] == 2   # the folded u2 pause is reviewed next


def test_late_continue_of_a_provisional_unit_sends_no_steer(steward_home):
    store, jid, fact = _setup(steward_home, provisional_units=["u1"], unit_id="u2")
    assert _apply(store, jid, fact, "continue")["outcome"] == "continue"
    cp = pause_checkpoint.load(store, jid)
    assert steers(store, jid) == [] and cp["provisional_units"] == [] and cp["unit_id"] == "u2"


def test_a_ruling_for_an_older_pause_rerequests_the_folded_newer_one(steward_home):
    """Review Focus 1: a pause arrived while u1's review was in flight; after u1's ruling the newer pause is asked."""
    newer = {**FACT, "unit_id": "u2", "next_unit": "u3", "pause_seq": 2}
    store, jid, fact = _setup(steward_home, pause_seq=2, last_pause_fact=newer)
    _apply(store, jid, fact, "continue")
    cp = pause_checkpoint.load(store, jid)
    assert cp["awaiting_review"] and cp["review_requested_at"] == 160.0
    (f,) = pause_facts(store, jid)
    assert f["data"]["pause_seq"] == 2 and f["data"]["resubmitted"] is True


def test_a_snapshot_review_cannot_rule_done(steward_home):
    store, jid, _ = _setup(steward_home, awaiting_review=False)
    snap = {"sha": NEW, "unit_id": "u1", "pause_kind": "snapshot", "pause_seq": 1, "failed": []}
    out = _apply(store, jid, snap, "done")
    assert out["outcome"] == "continue" and out["overridden"] == "a snapshot review cannot rule done"
    assert steers(store, jid)[0]["text"].startswith("Tech lead (snapshot review): commit what is coherent, or continue.")
    assert not pause_checkpoint.load(store, jid)["awaiting_review"]


def test_no_outstanding_review_applies_nothing(steward_home):
    store, jid, fact = _setup(steward_home, awaiting_review=False)
    assert _apply(store, jid, fact, "continue")["outcome"] is None and steers(store, jid) == []


def test_facts_seen_by_the_review_are_cleared_newer_ones_kept(steward_home):
    store, jid, fact = _setup(steward_home)
    for i in range(3):
        pause_checkpoint.append_fact(store, jid, source="flow", kind="steer_nudge", text=f"f{i}", at=float(i))
    pause_checkpoint.update(store, jid, lambda c: c.update(review_fact_seq=2))
    _apply(store, jid, fact, "continue")
    assert [f["text"] for f in pause_checkpoint.load(store, jid)["pending_facts"]] == ["f2"]


def test_failed_reviews_continue_provisionally_and_notify_once_after_three(steward_home):
    store, jid, _ = _setup(steward_home)
    notes: list = []
    for i in range(4):
        cp = pr.review_failed(store, jid, reason="astra: timeout", now=200.0 + i, notify_fn=lambda *a: notes.append(a))
        assert cp["force_provisional"] and cp["tl_failures"] == i + 1 and not cp["review_in_flight"]
    assert len(notes) == 1 and notes[0][1] == "PAUSE_REVIEW"
    assert len([e for e in store.events(jid) if e.what == "pause_review_failed"]) == 4


def test_a_failed_review_after_the_provisional_continue_stops_waiting(steward_home):
    store, jid, _ = _setup(steward_home, provisional_seq=1)
    cp = pr.review_failed(store, jid, reason="x", now=1.0)
    assert not cp["awaiting_review"] and not cp["force_provisional"]


def test_a_failed_done_claim_keeps_waiting(steward_home):
    store, jid, _ = _setup(steward_home, done_claim=True)
    cp = pr.review_failed(store, jid, reason="x", now=1.0)
    assert cp["awaiting_review"] and not cp["force_provisional"]


def test_spend_cap_skips_the_review_with_one_notice(steward_home):
    store, jid, _ = _setup(steward_home)
    notes: list = []
    for _ in range(2):
        cp = pr.skip_for_spend(store, jid, now=1.0, notify_fn=lambda *a: notes.append(a))
    assert cp["force_provisional"] and len(notes) == 1
    assert len([e for e in store.events(jid) if e.what == "pause_review_skipped"]) == 2
