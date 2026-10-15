"""Tests for steward/inner.py (TRJ-01).

`InnerVerdict` is a pure, typed mapping from (heartbeat class, recovery
action) to a small enum-shaped verdict, recorded only on change.
"""

from __future__ import annotations

from steward.heartbeat import Class
from steward.inner import InnerVerdict, inner_verdict, record_if_changed
from steward.models import Judgment
from steward.recovery import Action
from steward.store import Store


def _action(kind: str, *, judgment=Judgment.code, rule="r", detail="d") -> Action:
    return Action(kind=kind, detail=detail, judgment=judgment, rule=rule)


# -- the full mapping table -------------------------------------------------


def test_progressing_none_maps_to_executing_continue():
    verdict = inner_verdict(Class.progressing, _action("none"), diagnosed_this_tick=False)
    assert verdict.state == "executing"
    assert verdict.action == "continue"


def test_waiting_none_maps_to_waiting_continue():
    verdict = inner_verdict(Class.waiting, _action("none"), diagnosed_this_tick=False)
    assert verdict.state == "waiting"
    assert verdict.action == "continue"


def test_quiet_known_process_none_maps_to_quiet_continue():
    verdict = inner_verdict(Class.quiet_known_process, _action("none"), diagnosed_this_tick=False)
    assert verdict.state == "quiet"
    assert verdict.action == "continue"


def test_idle_none_maps_to_idle_continue():
    verdict = inner_verdict(Class.idle, _action("none"), diagnosed_this_tick=False)
    assert verdict.state == "idle"
    assert verdict.action == "continue"


def test_dead_replace_maps_to_dead_retry():
    verdict = inner_verdict(Class.dead, _action("replace"), diagnosed_this_tick=False)
    assert verdict.state == "dead"
    assert verdict.action == "retry"


def test_dead_needs_human_maps_to_dead_escalate():
    verdict = inner_verdict(Class.dead, _action("needs_human"), diagnosed_this_tick=False)
    assert verdict.state == "dead"
    assert verdict.action == "escalate"


def test_suspect_stall_none_maps_to_stalled_continue_when_not_diagnosed_this_tick():
    """Already-diagnosed-this-episode ticks return kind='none' and are not a
    fresh diagnosis, so they fall through to the ordinary kind mapping."""
    verdict = inner_verdict(Class.suspect_stall, _action("none"), diagnosed_this_tick=False)
    assert verdict.state == "stalled"
    assert verdict.action == "continue"


def test_suspect_stall_fresh_guide_maps_to_focused_diagnosis():
    verdict = inner_verdict(Class.suspect_stall, _action("guide"), diagnosed_this_tick=True)
    assert verdict.state == "stalled"
    assert verdict.action == "focused_diagnosis"


def test_suspect_stall_fresh_replace_maps_to_focused_diagnosis():
    verdict = inner_verdict(Class.suspect_stall, _action("replace"), diagnosed_this_tick=True)
    assert verdict.state == "stalled"
    assert verdict.action == "focused_diagnosis"


def test_suspect_stall_fresh_needs_human_still_maps_to_escalate():
    """A fresh diagnosis that concludes needs_human keeps the more severe,
    more informative `escalate` label rather than being masked by
    focused_diagnosis."""
    verdict = inner_verdict(Class.suspect_stall, _action("needs_human"), diagnosed_this_tick=True)
    assert verdict.state == "stalled"
    assert verdict.action == "escalate"


def test_suspect_stall_guide_not_diagnosed_this_tick_maps_to_repair():
    """Not part of the current tick's fresh diagnosis (e.g. a replayed
    action) falls back to the ordinary kind mapping."""
    verdict = inner_verdict(Class.suspect_stall, _action("guide"), diagnosed_this_tick=False)
    assert verdict.state == "stalled"
    assert verdict.action == "repair"


def test_reason_and_judgment_come_from_the_action():
    action = _action("guide", judgment=Judgment.model_informed_code_authorized, rule="diagnosis_verdict_retry_with_guidance")
    verdict = inner_verdict(Class.suspect_stall, action, diagnosed_this_tick=False)
    assert verdict.reason == "diagnosis_verdict_retry_with_guidance"
    assert verdict.judgment == Judgment.model_informed_code_authorized


def test_verdict_is_frozen_dataclass():
    verdict = InnerVerdict(state="executing", action="continue", reason="r", judgment=Judgment.code)
    assert verdict.state == "executing"


# -- record_if_changed -------------------------------------------------------


def test_record_if_changed_writes_event_on_first_call(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    verdict = inner_verdict(Class.progressing, _action("none"), diagnosed_this_tick=False)

    changed = record_if_changed(store, job.job_id, verdict)

    assert changed is True
    events = [e for e in store.events(job.job_id) if e.what == "inner_verdict"]
    assert len(events) == 1
    assert events[0].rule == "inner_loop"
    assert events[0].judgment == Judgment.code
    assert events[0].evidence["state"] == "executing"
    assert events[0].evidence["action"] == "continue"


def test_record_if_changed_no_event_on_repeat(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    verdict = inner_verdict(Class.progressing, _action("none"), diagnosed_this_tick=False)

    record_if_changed(store, job.job_id, verdict)
    changed = record_if_changed(store, job.job_id, verdict)

    assert changed is False
    events = [e for e in store.events(job.job_id) if e.what == "inner_verdict"]
    assert len(events) == 1


def test_record_if_changed_writes_event_on_change(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    first = inner_verdict(Class.progressing, _action("none"), diagnosed_this_tick=False)
    second = inner_verdict(Class.suspect_stall, _action("guide"), diagnosed_this_tick=True)

    record_if_changed(store, job.job_id, first)
    changed = record_if_changed(store, job.job_id, second)

    assert changed is True
    events = [e for e in store.events(job.job_id) if e.what == "inner_verdict"]
    assert len(events) == 2
    assert events[1].evidence["state"] == "stalled"
    assert events[1].evidence["action"] == "focused_diagnosis"


def test_record_if_changed_reflects_action_change_alone(steward_home):
    """Same state, different action still counts as a change."""
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    guide = inner_verdict(Class.suspect_stall, _action("guide"), diagnosed_this_tick=False)
    none = inner_verdict(Class.suspect_stall, _action("none"), diagnosed_this_tick=False)

    record_if_changed(store, job.job_id, guide)
    changed = record_if_changed(store, job.job_id, none)

    assert changed is True
