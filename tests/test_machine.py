import pytest

from steward.machine import (
    TRANSITIONS,
    AuthorityError,
    IllegalTransition,
    is_terminal,
    may_dispatch,
    transition,
)
from steward.models import JobState, Judgment

ALL_STATES = set(JobState)


def test_transitions_table_covers_every_state():
    assert set(TRANSITIONS) | {
        JobState.STOPPED,
        JobState.FAILED,
        JobState.RELEASED,
    } == ALL_STATES


@pytest.mark.parametrize("cur", sorted(TRANSITIONS, key=lambda s: s.value))
def test_allowed_transitions_succeed(cur):
    for nxt in TRANSITIONS[cur]:
        judgment = Judgment.human if _requires_human(cur, nxt) else Judgment.code
        # Should not raise.
        transition(cur, nxt, judgment=judgment)


def _requires_human(cur, nxt):
    return (cur, nxt) in {
        (JobState.ACCEPTED, JobState.RELEASED),
        (JobState.NEEDS_HUMAN, JobState.ACCEPTED),
    }


def test_exhaustive_illegal_transitions_raise():
    for cur in ALL_STATES:
        allowed = TRANSITIONS.get(cur, frozenset())
        for nxt in ALL_STATES:
            if nxt in allowed or nxt == cur:
                continue
            with pytest.raises(IllegalTransition):
                transition(cur, nxt, judgment=Judgment.code)


def test_accepted_to_released_requires_human_judgment():
    with pytest.raises(AuthorityError):
        transition(JobState.ACCEPTED, JobState.RELEASED, judgment=Judgment.code)
    # human judgment succeeds
    transition(JobState.ACCEPTED, JobState.RELEASED, judgment=Judgment.human)


def test_needs_human_to_accepted_requires_human_judgment():
    with pytest.raises(AuthorityError):
        transition(JobState.NEEDS_HUMAN, JobState.ACCEPTED, judgment=Judgment.code)
    transition(JobState.NEEDS_HUMAN, JobState.ACCEPTED, judgment=Judgment.human)


def test_terminal_states():
    for state in (JobState.STOPPED, JobState.FAILED, JobState.RELEASED):
        assert is_terminal(state)
    for state in (JobState.CREATED, JobState.RUNNING, JobState.ACCEPTED):
        assert not is_terminal(state)


def test_may_dispatch_only_running():
    assert may_dispatch(JobState.RUNNING) is True
    for state in ALL_STATES - {JobState.RUNNING}:
        assert may_dispatch(state) is False


def test_no_automatic_path_to_released():
    # RELEASED is reachable from ACCEPTED (human judgment only) and, for a job with no change of its own
    # (issue L, closed 'no change'), straight from CHECKS_PASSED / NEEDS_HUMAN instead of being accepted.
    for cur in ALL_STATES:
        allowed = TRANSITIONS.get(cur, frozenset())
        if JobState.RELEASED in allowed:
            assert cur in (JobState.ACCEPTED, JobState.CHECKS_PASSED, JobState.NEEDS_HUMAN)
