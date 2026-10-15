"""Versioned operator roster of eligible engineering models (Phase 26, FLEET-01..03).

The roster is a separate namespace from the flat `models.toml` role defaults
(`steward.roles`). Presence in the roster says nothing about runtime
qualification: that is an explicit, separately recorded fact (see
`steward.fleet_store`). Strengths and price/quota observations are advisory and
never grant capability. Credentials and executable commands never enter it.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

SCHEMA_VERSION = 1
ROLES = frozenset({"worker", "tech_lead"})
CAPABILITIES = frozenset({"edit", "tests", "shell", "subagents", "resume", "usage_report", "plan", "review"})
OBSERVATION_KINDS = frozenset({"price", "quota"})

# Phase 29 (FLEET-18): runtime lifecycle capabilities, each qualified independently by observed evidence
# (distinct from the roster's role/skill `CAPABILITIES`). Roster presence or an installed CLI proves none.
RUNTIME_CAPABILITIES = ("launch", "identity", "poll", "steer_ack", "permission_relay", "typed_result",
                        "cancel", "stop_confirm", "reattach", "usage")
# What each controller operation needs. `usage` is never required to dispatch: unknown usage stays null.
ACTION_REQUIREMENTS = {
    "start": frozenset({"launch", "identity", "poll", "permission_relay", "typed_result"}),
    "assign": frozenset({"launch", "identity", "poll", "permission_relay", "typed_result"}),
    "steer": frozenset({"steer_ack"}),
    "cancel": frozenset({"cancel"}),
    "stop": frozenset({"cancel", "stop_confirm"}),
    "recover": frozenset({"stop_confirm"}),
    "reattach": frozenset({"reattach"}),
    "split": frozenset(), "reorder": frozenset(), "diagnose": frozenset(), "owner_decision": frozenset(),
}


# Runtimes each role can actually run on in production. ONE table: `FleetStore.dispatchable` (the roster view,
# snapshot eligibility, launch assignment) and `fleet_services.build_fleet_services` (adapter selection) both read
# it, so "dispatchable" can never name a runtime the services would refuse (2026-10-06: muse/agy/claude-fabric
# showed dispatchable but every `new --fleet-plan` job using them died at start). Qualification evidence is
# still required on top of this: the table says an adapter exists, never that a capability holds.
PRODUCTION_RUNTIMES = {
    "worker": frozenset({"claude-code", "claude-fabric", "codex", "muse", "agy", "pi"}),
    "tech_lead": frozenset({"claude-code", "claude-fabric", "codex"}),
}
PANE_RUNTIMES = frozenset({"claude-code", "claude-fabric"})  # Claude lanes run as Herdr shim panes


def production_gap(entry) -> list[str]:
    """The entry's roles whose runtime has no production adapter (empty: every role can run)."""
    return sorted(r for r in entry.roles if entry.runtime not in PRODUCTION_RUNTIMES.get(r, frozenset()))


def missing_capabilities(qualified, action: str) -> list[str]:
    """Capabilities `action` needs that are not in `qualified` (sorted). Unknown actions need everything."""
    need = ACTION_REQUIREMENTS.get(action, frozenset(RUNTIME_CAPABILITIES))
    return sorted(set(need) - set(qualified))

_TOP_FIELDS = {"schema_version", "revision", "entries"}
_ENTRY_FIELDS = {"id", "provider", "runtime", "model", "roles", "capabilities", "strengths",
                 "enabled", "label", "observations", "priority"}
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SECRET_KEY = re.compile(r"(secret|token|passw|credential|api[_-]?key|auth|bearer|private)", re.I)
_COMMAND_KEY = re.compile(r"^(command|cmd|exec|argv|args|script|shell|run|entrypoint|prompt|launch)$", re.I)
_SECRET_VALUE = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{12,}"
                           r"|xox[abp]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY)")
_COMMAND_VALUE = re.compile(r"(\$\(|`|;\s*\w|&&|\|\||\|\s*\w|>\s*/|\brm\s+-|\bcurl\s|\bsudo\s)")


class RosterError(ValueError):
    """Validation failure with a stable machine-readable `reason`."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class RosterEntry:
    id: str
    provider: str
    runtime: str
    model: str
    roles: tuple[str, ...]
    capabilities: tuple[str, ...]
    strengths: tuple[str, ...]
    enabled: bool
    label: str
    observations: tuple[tuple[tuple[str, str], ...], ...] = ()
    priority: int | None = None   # v2.5: order among same-role entries (tech-lead fallthrough); lower first

    def to_dict(self) -> dict:
        d = {
            "id": self.id, "provider": self.provider, "runtime": self.runtime, "model": self.model,
            "roles": list(self.roles), "capabilities": list(self.capabilities),
            "strengths": list(self.strengths), "enabled": self.enabled, "label": self.label,
            "observations": [dict(o) for o in self.observations],
        }
        if self.priority is not None:  # absent stays absent: older rosters keep their canonical form and hash
            d["priority"] = self.priority
        return d


@dataclass(frozen=True)
class Roster:
    revision: int
    entries: tuple[RosterEntry, ...]
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version, "revision": self.revision,
                "entries": [e.to_dict() for e in self.entries]}

    @property
    def canonical(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.canonical.encode()).hexdigest()

    def get(self, roster_id: str) -> RosterEntry | None:
        return next((e for e in self.entries if e.id == roster_id), None)


def _scan_key(key) -> None:
    if not isinstance(key, str):
        raise RosterError("unknown_field", repr(key))
    if _SECRET_KEY.search(key):
        raise RosterError("secret_field", key)
    if _COMMAND_KEY.match(key):
        raise RosterError("command_field", key)


def _scan_value(v) -> None:
    if isinstance(v, str):
        if _SECRET_VALUE.search(v):
            raise RosterError("secret_value")
        if _COMMAND_VALUE.search(v):
            raise RosterError("command_value")
    elif isinstance(v, (list, tuple)):
        for x in v:
            _scan_value(x)
    elif isinstance(v, dict):
        for k, x in v.items():
            _scan_key(k)
            _scan_value(x)


def _text(e: dict, key: str, reason: str) -> str:
    v = e.get(key)
    if not isinstance(v, str) or not v.strip() or len(v) > 200:
        raise RosterError(reason, key)
    return v.strip()


def _str_list(e: dict, key: str, reason: str, allowed=None, nonempty=False) -> tuple[str, ...]:
    v = e.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise RosterError(reason, key)
    if nonempty and not v:
        raise RosterError(reason, key)
    if allowed is not None and not set(v) <= allowed:
        raise RosterError(reason, key)
    return tuple(sorted(set(x.strip() for x in v)))


def _observations(e: dict) -> tuple:
    raw = e.get("observations", [])
    if not isinstance(raw, list):
        raise RosterError("bad_observation")
    out = []
    for o in raw:
        if not isinstance(o, dict) or set(o) - {"kind", "observed_on", "value"} or set(o) != {"kind", "observed_on", "value"}:
            raise RosterError("bad_observation")
        try:
            date.fromisoformat(str(o["observed_on"]))
        except ValueError:
            raise RosterError("bad_observation", "observed_on") from None
        if o["kind"] not in OBSERVATION_KINDS or not isinstance(o["value"], str):
            raise RosterError("bad_observation")
        out.append(tuple(sorted((k, str(v)) for k, v in o.items())))
    return tuple(sorted(out))


def validate_roster(data) -> Roster:
    """Pure schema validation. Raises RosterError(reason) on the first problem."""
    if not isinstance(data, dict):
        raise RosterError("not_a_mapping")
    for k in data:
        _scan_key(k)
    if "schema_version" not in data:
        raise RosterError("missing_schema_version")
    if data["schema_version"] != SCHEMA_VERSION or isinstance(data["schema_version"], bool):
        raise RosterError("unsupported_schema_version", str(data["schema_version"]))
    rev = data.get("revision")
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 1:
        raise RosterError("bad_revision")
    if set(data) - _TOP_FIELDS:
        raise RosterError("unknown_field", ",".join(sorted(set(data) - _TOP_FIELDS)))
    raw = data.get("entries")
    if not isinstance(raw, list):
        raise RosterError("missing_entries")
    seen: set[str] = set()
    entries = []
    for e in raw:
        if not isinstance(e, dict):
            raise RosterError("bad_entry")
        for k in e:
            _scan_key(k)
        _scan_value({k: v for k, v in e.items()})
        if set(e) - _ENTRY_FIELDS:
            raise RosterError("unknown_field", ",".join(sorted(set(e) - _ENTRY_FIELDS)))
        rid = e.get("id")
        if not isinstance(rid, str) or not _ID.match(rid):
            raise RosterError("bad_id", repr(rid))
        if rid in seen:
            raise RosterError("duplicate_id", rid)
        seen.add(rid)
        if not isinstance(e.get("enabled"), bool):
            raise RosterError("bad_enabled", rid)
        prio = e.get("priority")
        if prio is not None and (isinstance(prio, bool) or not isinstance(prio, int) or not 0 <= prio <= 1000):
            raise RosterError("bad_priority", rid)
        entries.append(RosterEntry(
            id=rid,
            provider=_text(e, "provider", "bad_provider"),
            runtime=_text(e, "runtime", "bad_runtime"),
            model=_text(e, "model", "bad_model"),
            roles=_str_list(e, "roles", "bad_roles", ROLES, nonempty=True),
            capabilities=_str_list(e, "capabilities", "bad_capabilities", CAPABILITIES),
            strengths=_str_list(e, "strengths", "bad_strengths"),
            enabled=e["enabled"],
            label=_text(e, "label", "bad_label"),
            observations=_observations(e),
            priority=prio,
        ))
    return Roster(revision=rev, entries=tuple(sorted(entries, key=lambda x: x.id)))


def load_roster(path: Path | str) -> Roster:
    """Read an operator roster TOML file (never executes anything)."""
    try:
        data = tomllib.loads(Path(path).read_text())
    except tomllib.TOMLDecodeError as exc:
        raise RosterError("malformed_toml", str(exc)) from None
    return validate_roster(data)
