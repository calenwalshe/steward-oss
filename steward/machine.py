"""Pure state machine for job lifecycle.

No I/O, no store access — deterministic transition rules only (SP-2, SP-17).
The store enforces persistence; this module enforces legality and authority.
"""

from __future__ import annotations

from steward.models import JobState, Judgment

TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.CREATED: frozenset(
        {JobState.CONTRACTED, JobState.STOPPED, JobState.FAILED}
    ),
    JobState.CONTRACTED: frozenset(
        {JobState.RUNNING, JobState.STOPPED, JobState.NEEDS_HUMAN}
    ),
    JobState.RUNNING: frozenset(
        {
            JobState.PAUSED,
            JobState.STOPPING,
            JobState.WORKER_FINISHED,
            JobState.NEEDS_HUMAN,
            JobState.FAILED,
        }
    ),
    JobState.PAUSED: frozenset({JobState.RUNNING, JobState.STOPPING}),
    JobState.STOPPING: frozenset({JobState.STOPPED}),
    JobState.WORKER_FINISHED: frozenset(
        {
            JobState.CHECKS_PASSED,
            JobState.RUNNING,
            JobState.NEEDS_HUMAN,
            JobState.STOPPED,
            JobState.FAILED,
        }
    ),
    JobState.CHECKS_PASSED: frozenset(
        {JobState.ACCEPTED, JobState.NEEDS_HUMAN, JobState.RUNNING, JobState.STOPPED, JobState.RELEASED}
    ),
    JobState.NEEDS_HUMAN: frozenset(
        {JobState.RUNNING, JobState.STOPPED, JobState.ACCEPTED, JobState.RELEASED}
    ),
    JobState.ACCEPTED: frozenset({JobState.RELEASED}),
}

# Transitions that require human judgment regardless of caller-supplied judgment.
_HUMAN_ONLY_TRANSITIONS: frozenset[tuple[JobState, JobState]] = frozenset(
    {
        (JobState.ACCEPTED, JobState.RELEASED),
        (JobState.NEEDS_HUMAN, JobState.ACCEPTED),
    }
)

TERMINAL_STATES: frozenset[JobState] = frozenset(
    {JobState.STOPPED, JobState.FAILED, JobState.RELEASED}
)


class IllegalTransition(Exception):
    """Raised when a transition is not present in TRANSITIONS."""


class AuthorityError(Exception):
    """Raised when a transition is legal but the judgment lacks authority for it."""


CODE_RELEASE_RULES: frozenset[str] = frozenset({"released_on_incorporation"})


def transition(cur: JobState, nxt: JobState, *, judgment: Judgment, rule: str = "") -> JobState:
    """Validate a transition, returning `nxt` on success.

    Raises IllegalTransition if `nxt` is not reachable from `cur`, and
    AuthorityError if the transition requires human judgment and none was given.
    Code judgment may release an ACCEPTED job only under a CODE_RELEASE_RULES rule.
    """
    allowed = TRANSITIONS.get(cur, frozenset())
    if nxt not in allowed:
        raise IllegalTransition(f"{cur} -> {nxt} is not a legal transition")
    if (cur, nxt) in _HUMAN_ONLY_TRANSITIONS and judgment != Judgment.human:
        if not ((cur, nxt) == (JobState.ACCEPTED, JobState.RELEASED) and judgment == Judgment.code
                and rule in CODE_RELEASE_RULES):
            raise AuthorityError(f"{cur} -> {nxt} requires human judgment, got {judgment} (rule {rule!r})")
    return nxt


def is_terminal(state: JobState) -> bool:
    return state in TERMINAL_STATES


def may_dispatch(state: JobState) -> bool:
    """True only when the controller may dispatch/launch worker activity."""
    return state == JobState.RUNNING
