"""hold.kind: code-only classification of a NEEDS_HUMAN hold from the event log (spec §3)."""
from __future__ import annotations

from steward import hold, questions
from steward.models import JobState, Judgment
from steward.store import Store
from test_runner import _contracted_job


def _park(store, jid, *, marker_rule, marker_evidence, hold_what="acceptance unresolved", hold_rule="acceptance_unresolved"):
    if store.get_job(jid).state != JobState.RUNNING:  # a re-park follows an owner send-back, which is already RUNNING
        store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    store.set_state(jid, JobState.WORKER_FINISHED, what="worker reported done", evidence=marker_evidence,
                    rule=marker_rule, judgment=Judgment.code)
    store.set_state(jid, JobState.NEEDS_HUMAN, what=hold_what, evidence={"reasons": [hold_what]}, rule=hold_rule,
                    judgment=Judgment.code)


def test_steward_done_marker_is_an_acceptance_hold(steward_home):
    store = Store(steward_home)
    jid = _contracted_job(store).job_id
    _park(store, jid, marker_rule="steward_done_marker", marker_evidence={"marker": "STEWARD-DONE"})
    k = hold.kind(store, jid)
    assert k["kind"] == "acceptance" and k["marker"] == "STEWARD-DONE"


def test_fabric_done_frame_is_an_acceptance_hold(steward_home):
    store = Store(steward_home)
    jid = _contracted_job(store).job_id
    _park(store, jid, marker_rule="fabric_done_frame", marker_evidence={"sha": "abc", "seq": 7})
    assert hold.kind(store, jid)["kind"] == "acceptance"


def test_idle_after_final_message_is_stuck(steward_home):
    store = Store(steward_home)
    jid = _contracted_job(store).job_id
    _park(store, jid, marker_rule="idle_after_final_message", marker_evidence={"marker": "idle_after_final_message"})
    k = hold.kind(store, jid)
    assert k["kind"] == "stuck" and k["marker"] == "idle_after_final_message"


def _yield(store, jid, lane, **ev):
    store.append_event(jid, what="yield_classified", evidence={"lane": lane, "seq": 3, "sha": "abc", **ev},
                       rule="yield_classification", judgment=Judgment.code)


def test_yield_classified_candidate_is_an_acceptance_hold(steward_home):
    store = Store(steward_home)
    jid = _contracted_job(store).job_id
    _yield(store, jid, "CANDIDATE", failed_preconditions=[])
    _park(store, jid, marker_rule="t", marker_evidence={})
    assert hold.kind(store, jid)["kind"] == "acceptance" and hold.worker_claim(store.events(jid)) == "done"


def test_yield_classified_stopped_is_a_stuck_hold(steward_home):
    store = Store(steward_home)
    jid = _contracted_job(store).job_id
    _yield(store, jid, "STOPPED", failed_preconditions=["no_new_commit"])
    _park(store, jid, marker_rule="t", marker_evidence={})
    assert hold.kind(store, jid)["kind"] == "stuck" and hold.worker_claim(store.events(jid)) == "unfinished"


def test_yield_classified_asking_and_waiting_are_not_parking_reasons(steward_home):
    store = Store(steward_home)
    jid = _contracted_job(store).job_id
    _yield(store, jid, "ASKING", failed_preconditions=[])
    _park(store, jid, marker_rule="t", marker_evidence={})
    assert hold.kind(store, jid)["kind"] == "unknown"


def test_finish_line_bounces_exhausted_is_stuck_even_after_a_done_marker(steward_home):
    store = Store(steward_home)
    jid = _contracted_job(store).job_id
    _park(store, jid, marker_rule="steward_done_marker", marker_evidence={"marker": "STEWARD-DONE"},
          hold_what="finish line still not met after 3 nudges: x", hold_rule="finish_line_bounces_exhausted")
    assert hold.kind(store, jid)["kind"] == "stuck"


def test_startup_blocked_after_an_owner_send_back_is_stuck(steward_home):
    """Review focus 1: the newest parking reason wins over an older STEWARD-DONE marker."""
    store = Store(steward_home)
    jid = _contracted_job(store).job_id
    _park(store, jid, marker_rule="steward_done_marker", marker_evidence={"marker": "STEWARD-DONE"})
    store.set_state(jid, JobState.RUNNING, what="sent back for rework; new attempt pending", evidence={},
                    rule="operator_send_back", judgment=Judgment.human)
    store.set_state(jid, JobState.NEEDS_HUMAN, what="worker startup blocked; brief not delivered", evidence={},
                    rule="worker_startup", judgment=Judgment.code)
    assert hold.kind(store, jid)["kind"] == "stuck"


def test_waiting_question_wins_over_stuck(steward_home, monkeypatch):
    """Review focus 2: an unanswered question is the owner's, never reopened."""
    store = Store(steward_home)
    jid = _contracted_job(store).job_id
    _park(store, jid, marker_rule="idle_after_final_message", marker_evidence={"marker": "idle_after_final_message"})
    monkeypatch.setattr(questions, "load", lambda s, j: [{"status": "waiting"}])
    monkeypatch.setattr(questions, "summary", lambda recs: {"waiting": 1})
    assert hold.kind(store, jid)["kind"] == "question"


def test_closeout_blocked_is_floor(steward_home):
    store = Store(steward_home)
    jid = _contracted_job(store).job_id
    _park(store, jid, marker_rule="idle_after_final_message", marker_evidence={"marker": "idle_after_final_message"})
    store.kv_set(jid, "closeout_blocked", True)
    assert hold.kind(store, jid)["kind"] == "floor"


def test_not_parked_is_none_and_unknown_is_safe(steward_home):
    store = Store(steward_home)
    jid = _contracted_job(store).job_id
    assert hold.kind(store, jid) is None
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    store.set_state(jid, JobState.NEEDS_HUMAN, what="fleet repair needs the owner", evidence={}, rule="fleet",
                    judgment=Judgment.code)
    assert hold.kind(store, jid)["kind"] == "unknown"  # no marker, no known stuck reason: never reopened


def test_cache_is_keyed_by_latest_seq(steward_home):
    store = Store(steward_home)
    jid = _contracted_job(store).job_id
    _park(store, jid, marker_rule="idle_after_final_message", marker_evidence={"marker": "idle_after_final_message"})
    first = hold.kind(store, jid)
    assert store.kv_get(jid, hold.CACHE_KEY)["seq"] == first["seq"]
    store.set_state(jid, JobState.RUNNING, what="sent back", evidence={}, rule="operator_send_back",
                    judgment=Judgment.human)
    _park(store, jid, marker_rule="steward_done_marker", marker_evidence={"marker": "STEWARD-DONE"})
    assert hold.kind(store, jid)["kind"] == "acceptance"


def test_resolve_reopens_marks_correct_or_wrong(steward_home):
    from steward import tech_lead_actions as tla
    store = Store(steward_home)
    jid = _contracted_job(store).job_id
    _park(store, jid, marker_rule="idle_after_final_message", marker_evidence={"marker": "idle_after_final_message"})
    reopened_at_seq = store.events(jid)[-1].seq
    store.kv_set(jid, tla.PRECISION_KEY, [{"id": "r1", "action": "reopen", "signature": "s", "facts": [], "at": 1.0,
                                          "lead": "astra", "outcome": "pending", "seq": reopened_at_seq}])
    # parked again as stuck after the reopen -> wrong
    store.set_state(jid, JobState.RUNNING, what="sent back", evidence={}, rule="tech_lead_send_back",
                    judgment=Judgment.model_informed_code_authorized)
    _park(store, jid, marker_rule="idle_after_final_message", marker_evidence={"marker": "idle_after_final_message"})
    assert hold.resolve_reopens(store, jid) == 1
    assert store.kv_get(jid, tla.PRECISION_KEY)[0]["outcome"] == "wrong"
    # a second reopen that ends in an acceptance hold -> correct
    store.kv_set(jid, tla.PRECISION_KEY, [{"id": "r2", "action": "reopen", "signature": "s", "facts": [], "at": 2.0,
                                          "lead": "astra", "outcome": "pending", "seq": store.events(jid)[-1].seq}])
    store.set_state(jid, JobState.RUNNING, what="sent back", evidence={}, rule="tech_lead_send_back",
                    judgment=Judgment.model_informed_code_authorized)
    _park(store, jid, marker_rule="steward_done_marker", marker_evidence={"marker": "STEWARD-DONE"})
    assert hold.resolve_reopens(store, jid) == 1
    assert store.kv_get(jid, tla.PRECISION_KEY)[0]["outcome"] == "correct"


def test_an_executed_reopen_records_the_event_seq_it_will_be_scored_from(steward_home):
    """The precision entry carries the seq of its own action event, so resolve_reopens knows what came 'since'."""
    from steward import tech_lead_actions as tla
    from test_tech_lead_reopen import _parked, _run, _prop
    store = Store(steward_home)
    jid, wt = _parked(store)
    out, *_ = _run(store, jid, wt, _prop())
    assert out[0]["outcome"] == "executed"
    entry = store.kv_get(jid, tla.PRECISION_KEY)[-1]
    assert entry["action"] == "reopen" and entry["seq"] == [e for e in store.events(jid)
                                                           if e.what == "tech_lead_action"][-1].seq
