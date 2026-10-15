"""Non-Herdr exception registry (OPX-04, SP-34).

Every message-passing path Steward controls that does NOT travel through
Herdr (SP-31) is an *exception*: git subprocesses, the worker's own model
API calls, the phone notifier, ChatGPT MCP writes relayed into the control
pane, and the fabric itself (recorded for completeness). Each is listed in
`~/.config/steward/exceptions.toml` (override: `$STEWARD_EXCEPTIONS`, an
explicit path) with a name, direction (`in`/`out`), purpose, and whether its
use is audited (`event`/`none`). A default registry is created the first
time none exists.

`use()` is the one call every non-Herdr code path makes before acting: a
registered name records an `exception_used` event (`judgment=code`); an
unregistered name records `exception_violation` and raises
`UnregisteredPath` — the caller never proceeds past that (SP-34: "anything
unregistered is a violation, blocked where Steward controls it").
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from steward.models import Judgment
from steward.store import Store, resolve_config_dir

_DEFAULT_HEADER = '''\
# Steward exceptions registry (OPX-04, SP-34).
#
# Every non-Herdr message-passing path Steward controls must be registered
# here; an unregistered name is a violation (blocked where Steward controls
# it). `direction` is "in" (something reaches Steward this way) or "out"
# (Steward reaches out this way). `audit` is "event" (every use() records an
# exception_used event) or "none".
'''

# Each default section kept as its own verbatim block (12-CONTEXT D10:
# `sync()` appends missing ones exactly as written here, never reconstructed).
_DEFAULT_SECTIONS: dict[str, str] = {
    "git": '''\
[git]
direction = "out"
purpose = "worktree creation and commit/diff reads"
audit = "event"
''',
    "model-api": '''\
[model-api]
direction = "out"
purpose = "the worker's own LLM calls (claude -p)"
audit = "event"
''',
    "chatgpt-mcp": '''\
[chatgpt-mcp]
direction = "in"
purpose = "ChatGPT MCP writes, relayed into the control pane"
audit = "event"
''',
    "phone-notify": '''\
[phone-notify]
direction = "out"
purpose = "phone notifications (Telegram/Moshi)"
audit = "event"
''',
    "herdr-api": '''\
[herdr-api]
direction = "out"
purpose = "the fabric itself (recorded for completeness)"
audit = "event"
''',
    "research-mcp": '''\
[research-mcp]
direction = "out"
purpose = "sanctioned research MCP calls made by a worker (job capability, spend-capped)"
audit = "event"
''',
    "supervisor-model": '''\
[supervisor-model]
direction = "out"
purpose = "Steward's own bounded model calls: Sol diagnosis, Astra trajectory review, Jev"
audit = "event"
''',
    "worker-check-wait": '''\
[worker-check-wait]
direction = "in"
purpose = "a worker's `steward check wait <run>` polling the store read-only for its Steward-owned check run result"
audit = "event"
''',
}

_DEFAULT_TOML = _DEFAULT_HEADER + "\n" + "\n".join(_DEFAULT_SECTIONS.values())


class UnregisteredPath(Exception):
    """`use()` was called for a name not in the exceptions registry."""


@dataclass(frozen=True)
class ExceptionEntry:
    name: str
    direction: str
    purpose: str
    audit: str


def registry_path(path: Path | str | None = None) -> Path:
    """explicit arg > $STEWARD_EXCEPTIONS > $XDG_CONFIG_HOME/steward > ~/.config/steward, `exceptions.toml`."""
    if path is not None:
        return Path(path)
    env_path = os.environ.get("STEWARD_EXCEPTIONS")
    if env_path:
        return Path(env_path)
    return resolve_config_dir() / "exceptions.toml"


def load(path: Path | str | None = None) -> dict[str, ExceptionEntry]:
    """Load the registry, creating the default file if none exists."""
    resolved = registry_path(path)
    if not resolved.exists():
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(_DEFAULT_TOML)
    with resolved.open("rb") as fh:
        data = tomllib.load(fh)
    return {
        name: ExceptionEntry(
            name=name,
            direction=entry.get("direction", ""),
            purpose=entry.get("purpose", ""),
            audit=entry.get("audit", "event"),
        )
        for name, entry in data.items()
    }


def sync(path: Path | str | None = None) -> list[str]:
    """Append any *default* section missing from an existing registry,
    verbatim (12-CONTEXT D10) — never rewrites what's already there. Explicit,
    run by the chair at cut-over (`steward exceptions sync`); code never
    silently widens a live registry on its own. Idempotent: a second call
    with nothing missing returns `[]` and touches nothing.

    A registry that doesn't exist yet is simply created fresh by `load()`
    (which already contains every default section), so nothing is "missing".
    """
    resolved = registry_path(path)
    if not resolved.exists():
        load(path)
        return []

    with resolved.open("rb") as fh:
        existing = tomllib.load(fh)
    missing = [name for name in _DEFAULT_SECTIONS if name not in existing]
    if not missing:
        return []

    addition = "\n" + "\n".join(_DEFAULT_SECTIONS[name] for name in missing)
    with resolved.open("a") as fh:
        fh.write(addition)
    return missing


def use(
    store: Store,
    job_id: str | None,
    name: str,
    evidence: dict,
    *,
    path: Path | str | None = None,
) -> ExceptionEntry:
    """Record (and authorize) one use of a registered exception path.

    Raises `UnregisteredPath` for a name the registry doesn't list — the
    caller must not proceed past that. `job_id=None` is accepted for a use
    that has no job to attach an event to yet (e.g. before a job exists);
    no event is recorded in that case, since `events.job_id` is a foreign
    key into `jobs` and there is nothing to reference.
    """
    registry = load(path)
    entry = registry.get(name)
    if entry is None:
        if job_id is not None:
            store.append_event(
                job_id,
                what="exception_violation",
                evidence={"name": name, **evidence},
                rule="unregistered_exception_path",
                judgment=Judgment.code,
            )
        raise UnregisteredPath(f"unregistered exception path: {name!r}")

    if entry.audit == "event" and job_id is not None:
        store.append_event(
            job_id,
            what="exception_used",
            evidence={"name": name, "direction": entry.direction, **evidence},
            rule="exceptions_registry",
            judgment=Judgment.code,
        )
    return entry
