# tests/test_pause_review_core.py
"""Spec 2026-10-13 §4.3/§5/§9: the worker's pause report, commit-to-unit matching, and the pause clock on a fake
clock (5-min provisional continue, ~2-min soft stop, 20/35/60-min backstops)."""
from __future__ import annotations

from steward import pause_checkpoint
from steward import pause_review as pr
from steward.brief import render_brief
from steward.contract import Contract, Unit


def _c(**kw):
    return Contract(goal="g", boundaries=[], done_when=["done"], next_step="", authority=[], assumptions=[], **kw)


def _cp(**kw):
    return pause_checkpoint.merged(kw)


# -- the report --------------------------------------------------------------------------------------------------

def test_the_last_report_line_wins():
    rep = pr.parse_report("did things\nSTEWARD-PAUSE unit=u1 next=u2 old\nmore\nSTEWARD-PAUSE unit=u2 next=u3 add cli")
    assert rep == pr.Report(unit="u2", next_unit="u3", next_title="add cli", done=False)


def test_next_none_is_a_done_claim():
    rep = pr.parse_report("STEWARD-PAUSE unit=u3 next=none all units done")
    assert rep.done and rep.next_unit is None and pr.is_done_claim(rep, None)
    assert pr.is_done_claim(None, "claims_done") and not pr.is_done_claim(None, None)


def test_no_report_or_bad_ids():
    assert pr.parse_report("I am done") is None
    rep = pr.parse_report("STEWARD-PAUSE unit=has/slash next=x!y title")
    assert rep.unit is None and rep.next_unit is None and not rep.done


def test_commit_matching_is_by_whole_token():
    assert pr.commit_matches_unit("u1: parser", "u1") and pr.commit_matches_unit("[U1] parser", "u1")
    assert not pr.commit_matches_unit("u10: parser", "u1") and not pr.commit_matches_unit("fix typo", "u1")
    assert pr.commit_matches_unit("anything", None)            # no current unit: any commit is a boundary


def test_the_report_wins_over_the_commit_subject():
    rep = pr.parse_report("STEWARD-PAUSE unit=u2 next=u3 x")
    assert pr.unit_for_pause(rep, _cp(unit_id="u1"), "u1: parser") == ("u2", True)
    assert pr.unit_for_pause(None, _cp(unit_id="u1"), "u1: parser") == ("u1", True)
    assert pr.unit_for_pause(None, _cp(unit_id="u1"), "fix typo") == ("u1", False)
    assert pr.unit_for_pause(None, _cp(), "anything") == (None, False)


# -- the pause clock ------------------------------------------------------------------------------------------------

def _step(cp, now, *, turn_open=True, head="h", subject="", mode="unit", live=True):
    upd, actions = pr.timers(cp, now=now, turn_open=turn_open, head=head, subject=subject, mode=mode, review_live=live)
    cp.update(upd)
    return actions


def test_provisional_continue_at_five_minutes_once_and_never_for_done():
    cp = _cp(awaiting_review=True, pause_seq=1, last_pause_at=100.0, review_requested_at=100.0)
    assert _step(cp, 399.0, turn_open=False) == []
    assert _step(cp, 400.0, turn_open=False) == ["provisional_continue"]
    assert cp["provisional_seq"] == 1
    assert _step(cp, 500.0, turn_open=False) == []
    done = _cp(awaiting_review=True, pause_seq=1, last_pause_at=100.0, review_requested_at=100.0, done_claim=True)
    assert _step(done, 1000.0, turn_open=False) == []


def test_a_failed_review_forces_an_immediate_provisional_abandon():
    cp = _cp(awaiting_review=True, pause_seq=2, last_pause_at=100.0, review_requested_at=100.0,
             force_provisional=True, provisional_seq=1)
    assert _step(cp, 101.0, turn_open=False) == ["provisional_abandon"]
    assert cp["provisional_seq"] == 2 and not cp["force_provisional"]


def test_a_folded_pause_gets_its_own_provisional_clock():
    cp = _cp(awaiting_review=True, pause_seq=2, provisional_seq=1, last_pause_at=1000.0, review_requested_at=100.0)
    assert _step(cp, 1299.0, turn_open=False) == []
    assert _step(cp, 1300.0, turn_open=False) == ["provisional_continue"]


def test_a_stale_review_is_requested_again_only_when_nothing_is_live():
    cp = _cp(awaiting_review=True, done_claim=True, pause_seq=1, last_pause_at=0.0, review_requested_at=0.0)
    assert _step(cp, pr.STALE_REVIEW_S, turn_open=False, live=True) == []
    assert _step(cp, pr.STALE_REVIEW_S + 1, turn_open=False, live=False) == ["rerequest_review"]


def test_soft_stop_once_two_minutes_after_the_units_commit_while_active():
    cp = _cp(unit_id="u1")
    assert _step(cp, 0.0, head="a", subject="u1: parser") == []   # first sight of HEAD is not a commit
    assert _step(cp, 10.0, head="b", subject="u1: parser") == []  # the unit's commit lands
    assert cp["unit_commit_sha"] == "b" and cp["unit_commit_at"] == 10.0
    assert _step(cp, 129.0, head="b") == []
    assert _step(cp, 130.0, head="b") == ["soft_stop"]
    assert _step(cp, 400.0, head="b") == []                        # once per commit


def test_no_soft_stop_in_natural_mode_when_the_turn_closed_or_for_another_units_commit():
    for mode, turn_open, subject in (("natural", True, "u1: parser"), ("unit", False, "u1: parser"),
                                     ("unit", True, "fix typo")):
        cp = _cp(unit_id="u1")
        _step(cp, 0.0, head="a", mode=mode)
        _step(cp, 10.0, head="b", subject=subject, mode=mode, turn_open=turn_open)
        assert _step(cp, 200.0, head="b", subject=subject, mode=mode, turn_open=turn_open) == [], (mode, subject)


def test_backstops_fire_once_each_at_20_35_60_active_minutes():
    cp = _cp()
    fired, t = [], 0.0
    while t <= 61 * 60:
        fired += [(a, round(cp["active_s"])) for a in _step(cp, t)]
        t += 60.0
    assert fired == [("nudge_commit", 1200), ("snapshot_review", 2100), ("liveness_handoff", 3600)]


def test_active_time_needs_an_open_turn_and_a_restart_gap_counts_at_most_two_minutes():
    cp = _cp()
    _step(cp, 0.0)
    _step(cp, 10_000.0)
    assert cp["active_s"] == pr.MAX_TICK_GAP_S
    _step(cp, 20_000.0, turn_open=False)
    assert cp["active_s"] == pr.MAX_TICK_GAP_S


def test_no_backstop_while_waiting_on_owner():
    cp = _cp(waiting_on="owner", active_s=5000.0, last_tick_at=0.0)
    assert _step(cp, 60.0) == []


# -- the brief -------------------------------------------------------------------------------------------------------

def test_brief_has_no_pause_section_when_off():
    assert "PAUSES" not in render_brief(_c(), None, None, None, fabric=True)
    assert pr.brief_lines(_c()) == []


def test_brief_unit_mode_tells_the_worker_to_stop_and_report():
    text = render_brief(_c(pause="unit", units=[Unit("u1", "parser", "pytest -q tests/test_p.py")]),
                        None, None, None, fabric=True)
    assert "PAUSES" in text and "STEWARD-PAUSE unit=<unit id> next=" in text and "Stop after each unit" in text
    assert "u1: parser (finish line: pytest -q tests/test_p.py)" in text and "5 minutes" in text


def test_brief_natural_mode_has_no_forced_stop_and_asks_for_unit_names():
    text = render_brief(_c(pause="natural"), None, None, None, fabric=True)
    assert "STEWARD-PAUSE" in text and "Stop after each unit" not in text and "name each next unit yourself" in text
