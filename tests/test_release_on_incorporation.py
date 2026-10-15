import pytest
from steward import machine
from steward.models import JobState, Judgment


def test_code_release_needs_the_rule():
    with pytest.raises(machine.AuthorityError):
        machine.transition(JobState.ACCEPTED, JobState.RELEASED, judgment=Judgment.code)
    with pytest.raises(machine.AuthorityError):
        machine.transition(JobState.ACCEPTED, JobState.RELEASED, judgment=Judgment.code, rule="landing_train")
    assert machine.transition(JobState.ACCEPTED, JobState.RELEASED, judgment=Judgment.code,
                              rule="released_on_incorporation") == JobState.RELEASED


def test_rule_does_not_unlock_other_human_only_transitions():
    with pytest.raises(machine.AuthorityError):
        machine.transition(JobState.NEEDS_HUMAN, JobState.ACCEPTED, judgment=Judgment.code,
                           rule="released_on_incorporation")
