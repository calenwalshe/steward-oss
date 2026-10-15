"""The tech lead's tunable policy (v2.6, PRD D-03, SUP-11): `~/.config/steward/tech-lead.toml`.

The owner tunes autonomy by reviewing outcomes; Claude edits the file on request. Everything here sits ABOVE the
hard floor (`steward.tech_lead_floor`, PRD D-04): no key can open it. Unknown keys, out-of-range values and any key
that names a floor class are rejected at load, so a typo can never silently widen or narrow what the tech lead does.

    mode = "active"              # active | shadow (shadow records what it would do and does nothing)
    [cadence]                    # the adaptive outer loop (PRD D-07)
    active_min = 30              # backstop snapshot cadence while the job is active or flagged (no fact = no model call)
    quiet_min = 120              # when quiet and healthy
    [review]
    timeout_s = 300              # one review's time limit before it falls through (PRD D-08)
    token_budget = 12000         # per-review packet budget (orientation + facts + memory), in tokens
    drill_down_chars = 2000      # one pulled plan section, at most
    [fixes]
    takeover_max_lines = 20      # a takeover diff larger than this aborts and becomes a send-back (PRD D-02)
    fix_rounds = 2               # the same issue surviving this many send-backs -> takeover
    judgment_confidence = 0.8    # judgment-based fixes (Jev/advisor concerns) need at least this confidence
    send_back = true
    takeover = true
    judgment_fixes = true
    [scopes]
    permissions = "broad"        # broad | contract | none: which worker permission asks the tech lead may clear
    grants = "broad"             # broad | contract | none: which job-granted keys it may pass on
    [parked]                     # jobs resting in NEEDS_HUMAN with a `stuck` hold (spec 2026-10-04)
    cadence_min = 10             # how often the tech lead reviews a stuck parked job
    reopen = true                # may it send a stuck job back itself (acceptance holds never)
    [ownership]                  # continuous job ownership (Plan 1, 2026-10-05)
    enabled = true               # false (or STEWARD_OWNERSHIP=0) restores legacy NEEDS_HUMAN notices
    coalesce_s = 30              # quiet period after new evidence before a review starts
    aging_min = 60               # an old reviewable blocker jumps the queue
    max_reviews = 3               # reviews in flight per controller process (one per job)
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

SCOPES = ("broad", "contract", "none")
MODES = ("active", "shadow")

# (table, key) -> (field, type, validator)
_SCHEMA = {
    (None, "mode"): ("mode", str, lambda v: v in MODES),
    ("cadence", "active_min"): ("cadence_active_min", (int, float), lambda v: 1 <= v <= 24 * 60),
    ("cadence", "quiet_min"): ("cadence_quiet_min", (int, float), lambda v: 1 <= v <= 24 * 60),
    ("review", "timeout_s"): ("review_timeout_s", (int, float), lambda v: 10 <= v <= 3600),
    ("review", "token_budget"): ("token_budget", int, lambda v: 500 <= v <= 200_000),
    ("review", "drill_down_chars"): ("drill_down_chars", int, lambda v: 200 <= v <= 20_000),
    ("fixes", "takeover_max_lines"): ("takeover_max_lines", int, lambda v: 0 <= v <= 500),
    ("fixes", "fix_rounds"): ("fix_rounds", int, lambda v: 1 <= v <= 10),
    ("fixes", "judgment_confidence"): ("judgment_confidence", (int, float), lambda v: 0 < v <= 1),
    ("fixes", "send_back"): ("send_back", bool, lambda v: True),
    ("fixes", "takeover"): ("takeover", bool, lambda v: True),
    ("fixes", "judgment_fixes"): ("judgment_fixes", bool, lambda v: True),
    ("scopes", "permissions"): ("permissions", str, lambda v: v in SCOPES),
    ("scopes", "grants"): ("grants", str, lambda v: v in SCOPES),
    ("parked", "cadence_min"): ("parked_cadence_min", (int, float), lambda v: 1 <= v <= 24 * 60),
    ("parked", "reopen"): ("parked_reopen", bool, lambda v: True),
    ("ownership", "enabled"): ("ownership_enabled", bool, lambda v: True),
    ("ownership", "coalesce_s"): ("ownership_coalesce_s", (int, float), lambda v: 0 <= v <= 3600),
    ("ownership", "aging_min"): ("ownership_aging_min", (int, float), lambda v: 1 <= v <= 24 * 60),
    ("ownership", "max_reviews"): ("ownership_max_reviews", int, lambda v: 1 <= v <= 20),
}
_TABLES = {t for t, _ in _SCHEMA if t}
# Words that name a floor class. A key containing one is refused outright: the floor is not configurable.
_FLOOR_WORDS = ("push", "publish", "deploy", "contact", "notify", "accept", "merge", "incorporate", "release",
                "spend", "budget_cap", "money", "rate_cap", "secret", "key", "credential", "outside", "live",
                "store", "service", "path", "delete", "weaken", "skip", "test", "floor")


class PolicyError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        self.reason, self.detail = reason, detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class Policy:
    mode: str = "active"
    cadence_active_min: float = 30
    cadence_quiet_min: float = 120
    review_timeout_s: float = 300
    token_budget: int = 12000
    drill_down_chars: int = 2000
    takeover_max_lines: int = 20
    fix_rounds: int = 2
    judgment_confidence: float = 0.8
    send_back: bool = True
    takeover: bool = True
    judgment_fixes: bool = True
    permissions: str = "broad"
    grants: str = "broad"
    parked_cadence_min: float = 10
    parked_reopen: bool = True
    ownership_enabled: bool = True
    ownership_coalesce_s: float = 30
    ownership_aging_min: float = 60
    ownership_max_reviews: int = 3
    source: str = "defaults"

    def may_grant_permission(self, *, in_contract: bool) -> bool:
        """Whether the tech lead may clear a worker's permission ask itself (the floor is checked separately)."""
        if self.permissions == "none":
            return False
        return self.permissions == "broad" or in_contract

    def may_pass_key(self, *, granted: bool) -> bool:
        if self.grants == "none":
            return False
        return granted  # broad or contract: only keys the job was given; anything else is a floor hit anyway

    def summary(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def default_path() -> Path:
    from steward.store import resolve_config_dir

    return resolve_config_dir() / "tech-lead.toml"


def _floor_word(key: str) -> str | None:
    k = key.lower()
    return next((w for w in _FLOOR_WORDS if w in k), None)


def parse(data: dict, source: str) -> Policy:
    values: dict = {}
    for k, v in data.items():
        if isinstance(v, dict):
            if _floor_word(k) and k not in _TABLES:
                raise PolicyError("floor_key", f"[{k}]: the hard floor is not configurable")
            if k not in _TABLES:
                raise PolicyError("unknown_key", f"[{k}]")
            items = [(k, kk, vv) for kk, vv in v.items()]
        else:
            items = [(None, k, v)]
        for table, key, val in items:
            name = f"{table}.{key}" if table else key
            spec = _SCHEMA.get((table, key))
            if spec is None:
                if _floor_word(key):
                    raise PolicyError("floor_key", f"{name}: the hard floor is not configurable")
                raise PolicyError("unknown_key", name)
            field_name, typ, ok = spec
            if isinstance(val, bool) and typ is not bool:
                raise PolicyError("bad_value", f"{name} = {val!r}")
            if not isinstance(val, typ) or not ok(val):
                raise PolicyError("bad_value", f"{name} = {val!r}")
            values[field_name] = val
    return Policy(**values, source=source)


def load(path: Path | str | None = None) -> Policy:
    """The policy at `path` (default: the config dir's tech-lead.toml), or the defaults when it is absent."""
    p = Path(path) if path is not None else default_path()
    if not p.is_file():
        return Policy()
    try:
        data = tomllib.loads(p.read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise PolicyError("malformed", str(exc)[:200]) from None
    return parse(data, str(p))


def load_or_defaults(path: Path | str | None = None) -> tuple[Policy, str | None]:
    """Never raises: a bad file falls back to the defaults and says why (the caller records it)."""
    try:
        return load(path), None
    except PolicyError as exc:
        why = str(exc)
        if exc.reason == "floor_key":
            why = f"{why} (a key names a hard-floor class)"
        return Policy(), why
