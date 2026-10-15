"""Who decided (continuous job ownership §2, §8, §14; Plan 1).

`Judgment` (steward.models) stays the AUTHORITY label the state machine checks. This module records WHO acted, so
owner, operator-agent, tech-lead, controller, worker and model-supervisor decisions stay distinguishable. Code-only
and conservative: an origin it cannot place is `unknown`; an actor hint can only make an actor less authoritative,
never claim the owner. While a relayed command from a non-owner actor is handled, `bind` makes every event the Store
writes carry `evidence["actor"]`; owner/legacy events are unchanged.
"""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass
from enum import Enum
from typing import Iterator, Mapping


class Actor(str, Enum):
    OWNER = "owner"
    OPERATOR_AGENT = "operator_agent"
    TECH_LEAD = "tech_lead"
    INDEPENDENT_REVIEWER = "independent_reviewer"
    CONTROLLER = "controller"
    WORKER = "worker"
    MODEL_SUPERVISOR = "model_supervisor"
    JEV = "jev"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ActorRef:
    kind: Actor
    id: str = ""

    def label(self) -> str:
        return f"{self.kind.value}:{self.id}" if self.id else self.kind.value


OWNER_ORIGINS = frozenset({"cli", "moshi", "dash", "job-chat", "phone", "human"})
_OPERATOR_AGENT_ORIGINS = frozenset({"steer-agent", "unblock-agent", "coordinator", "chatgpt", "mcp"})
_CONTROLLER_ORIGINS = frozenset({"steward", "contract", "deadline", "questions", "controller", "ownership"})
_MODEL_SUPERVISOR_ORIGINS = frozenset({"trajectory", "flow", "diagnosis", "sol", "astra"})
_FORCED_NON_OWNER = frozenset({"worker", "steer-agent", "unblock-agent", "coordinator"})
_VALUES = {a.value for a in Actor}
_CURRENT: contextvars.ContextVar[str | None] = contextvars.ContextVar("steward_actor", default=None)


def _parse(raw: str | None) -> ActorRef | None:
    if not raw:
        return None
    raw = str(raw).strip()
    head, _, rest = raw.partition(":")
    if head in _VALUES and head != Actor.UNKNOWN.value:
        return ActorRef(Actor(head), rest)
    if raw in OWNER_ORIGINS:
        return ActorRef(Actor.OWNER, raw)
    if raw in _OPERATOR_AGENT_ORIGINS or head == "operator-agent":
        return ActorRef(Actor.OPERATOR_AGENT, rest or raw)
    if raw in _CONTROLLER_ORIGINS:
        return ActorRef(Actor.CONTROLLER, raw)
    if raw in _MODEL_SUPERVISOR_ORIGINS or head == "watcher":
        return ActorRef(Actor.MODEL_SUPERVISOR, raw)
    if head in ("jev", "jev_fleet"):
        return ActorRef(Actor.JEV, raw)
    if raw == "worker":
        return ActorRef(Actor.WORKER, raw)
    try:
        from steward import owner

        if owner.is_owner_actor(raw):
            return ActorRef(Actor.OWNER, raw)
    except Exception:  # noqa: BLE001 - an unreadable owner config means "not known to be the owner"
        pass
    return None


def classify(origin: str | None, hint: str | None = None) -> ActorRef:
    """The actor behind a decision. A `hint` (e.g. `operator_agent:claude-code` carried on a relayed command) wins
    over the origin unless it claims the owner or the origin is a forced non-owner origin."""
    by_origin = _parse(origin)
    by_hint = _parse(hint)
    if by_hint is not None and by_hint.kind != Actor.OWNER and str(origin or "") not in _FORCED_NON_OWNER:
        return by_hint
    return by_origin or ActorRef(Actor.UNKNOWN, str(origin or ""))


def detect_cli_actor(env: Mapping[str, str]) -> str | None:
    """The hint a CLI invocation relays. CLAUDECODE=1 is set in every Claude Code tool subprocess (verified
    2026-10-05); CODEX_SANDBOX*/CODEX_THREAD_ID mark Codex commands (documented, unverified here); STEWARD_OPERATOR
    names an operator model explicitly (audit F-5). A forced origin already identifies the caller. Undetected
    agents fall back to origin classification (a recorded limitation)."""
    if env.get("STEWARD_FORCED_ORIGIN"):
        return None
    explicit = env.get("STEWARD_ACTOR")
    if explicit:
        ref = _parse(explicit)
        return ref.label() if ref is not None and ref.kind != Actor.OWNER else None
    if env.get("STEWARD_OPERATOR"):
        return f"operator_agent:{env['STEWARD_OPERATOR']}"
    if env.get("CLAUDECODE") == "1":
        return "operator_agent:claude-code"
    if any(k.startswith("CODEX_SANDBOX") or k == "CODEX_THREAD_ID" for k in env):
        return "operator_agent:codex"
    return None


@contextlib.contextmanager
def bind(label: str | None) -> Iterator[None]:
    token = _CURRENT.set(label)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current() -> str | None:
    return _CURRENT.get()


def label_for_cmd(cmd: dict) -> str | None:
    """The label to bind while a relayed command runs: only non-owner actors are bound (owner events unchanged)."""
    ref = classify(cmd.get("origin"), cmd.get("actor"))
    return None if ref.kind == Actor.OWNER else ref.label()


def is_owner(ref: ActorRef) -> bool:
    return ref.kind == Actor.OWNER
