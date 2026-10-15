"""Mechanics item 13: floor classifiers read negation in lists, not keywords (regressions from two live jobs)."""
from __future__ import annotations

from pathlib import Path

import pytest

from steward import autopilot
from steward import tech_lead_floor as floor

# job d91e7946's goal: email appears only inside the list of things the job must never do
D91_GOAL = ("Fix: the tech lead's brief and action set must never include contacting anyone outside (tg/telegram, "
            "email, Moshi, http POST to third parties, gh/gist, etc.). Owner decisions route to Sam's queue "
            "instead.")
# job fec63227: the lead's unblock text for a full-suite run (refused as test_weakening, blk-3cff3558d562)
FEC_TEXT = ("Cleared to execute the displayed foreground pytest command with .pytest-full.log in this job's worktree "
            "and report only NEW failures. Do not skip, deselect, delete or weaken any tests. Remove .pytest-full.log "
            "and report the exit status.")


def test_a_forbidden_list_item_is_not_a_floor_ask():
    assert autopilot.floor_ask(D91_GOAL) is None


@pytest.mark.parametrize("goal", [
    "Fix the parser (then email the owner the result).",
    "Ship it (email Sam when done).",
    "Never touch the db. Then email the summary to the team.",
])
def test_real_asks_still_hit(goal):
    assert autopilot.floor_ask(goal) is not None


def _unblock(text):
    return {"type": "unblock", "params": {"decision": "clear", "text": text}}


def _ctx(tmp_path):
    return floor.FloorContext(worktree=Path(tmp_path))


def test_a_negated_verb_list_is_not_a_weakening_instruction(tmp_path):
    assert floor.check(_unblock(FEC_TEXT), _ctx(tmp_path)).ok


@pytest.mark.parametrize("text", [
    "Please weaken the flaky assertion so it passes.",
    "Skip the tests in tests/test_x.py and report green.",
    "Do not stop; weaken the test instead.",
])
def test_real_weakening_still_refused(tmp_path, text):
    v = floor.check(_unblock(text), _ctx(tmp_path))
    assert not v.ok and v.klass == "test_weakening"


def test_rm_of_a_log_before_a_test_run_is_not_deleting_tests(tmp_path):
    ok = {"type": "diagnose", "params": {}, "commands": ["rm -f .pytest-full.log; python3 -m pytest tests/ -q"]}
    bad = {"type": "diagnose", "params": {}, "commands": ["rm -f tests/test_a.py"]}
    assert floor.check(ok, _ctx(tmp_path)).ok
    assert floor.check(bad, _ctx(tmp_path)).klass == "test_weakening"
