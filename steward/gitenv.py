"""Optional git index locks off for read-only calls: `git status`/`diff` otherwise refresh the index under
`index.lock`, and a SIGKILLed reader leaves it behind. Mandatory locks (merge, commit, worktree add) are unaffected."""

from __future__ import annotations

import os

READ_ENV = {"GIT_OPTIONAL_LOCKS": "0"}


def apply_default() -> None:
    """Process-wide default (a value already set by the caller is kept); children inherit it."""
    for k, v in READ_ENV.items():
        os.environ.setdefault(k, v)


def merged(env: dict | None) -> dict | None:
    """`env` with the read defaults filled in; None stays None (the child inherits `os.environ`)."""
    return None if env is None else {**READ_ENV, **env}
