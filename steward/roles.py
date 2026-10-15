"""Model roles: which model plays Sol/Astra/Jev, and where it is configured
from (TRJ-10, 12-CONTEXT D10).

`ModelRoles` is a plain frozen dataclass; `load_roles()` is the one place
config/env are read. Every consumer downstream takes a protocol
(`steward.diagnosis.Diagnoser`, `steward.review.TrajectoryReviewer`, the Jev
observer), so every unit test injects a fake instead of ever calling a real
model.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from steward.diagnosis import ClaudeDiagnoser, Diagnoser
from steward.jev_observer import JevObserver
from steward.review import ClaudeTrajectoryReviewer, TrajectoryReviewer
from steward.runner_exec import Runner
from steward.store import resolve_config_dir


@dataclass(frozen=True)
class ModelRoles:
    jev: str = "jev-latest"
    sol: str = "sonnet"
    astra: str = "opus"
    steer: str = "claude-opus-5-5"  # the quick-steer chat agent (`steward steer-chat`)
    steer_effort: str = "high"


def load_roles(config_dir: Path | str | None = None) -> ModelRoles:
    """`~/.config/steward/models.toml` (via `resolve_config_dir`), then
    `STEWARD_MODEL_{JEV,SOL,ASTRA,STEER}` / `STEWARD_STEER_EFFORT` env overrides on top. Missing file or
    missing keys fall back to `ModelRoles()` defaults."""
    roles = ModelRoles()
    path = resolve_config_dir(config_dir) / "models.toml"
    if path.exists():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        roles = ModelRoles(
            jev=data.get("jev", roles.jev),
            sol=data.get("sol", roles.sol),
            astra=data.get("astra", roles.astra),
            steer=data.get("steer", roles.steer),
            steer_effort=data.get("steer_effort", roles.steer_effort),
        )

    return ModelRoles(
        jev=os.environ.get("STEWARD_MODEL_JEV") or roles.jev,
        sol=os.environ.get("STEWARD_MODEL_SOL") or roles.sol,
        astra=os.environ.get("STEWARD_MODEL_ASTRA") or roles.astra,
        steer=os.environ.get("STEWARD_MODEL_STEER") or roles.steer,
        steer_effort=os.environ.get("STEWARD_STEER_EFFORT") or roles.steer_effort,
    )


def build_sol(runner: Runner, roles: ModelRoles) -> Diagnoser:
    return ClaudeDiagnoser(runner, model=roles.sol)


def build_astra(runner: Runner, roles: ModelRoles, *, timeout: float = 300) -> TrajectoryReviewer:
    return ClaudeTrajectoryReviewer(runner, roles.astra, timeout=timeout)


def build_jev(roles: ModelRoles) -> JevObserver:
    return JevObserver()


# Display names for the built-in supervisors (owner decision, 2026-09-28): the internal keys
# astra/sol/jev collided with real model names (OpenAI's Astra, GPT-Sol, TypeSafe's
# jev-latest), so people see plain job titles. Stored data keeps the internal keys;
# commands accept either.
ROLE_LABELS = {"astra": "advisor", "sol": "doctor", "jev": "observer"}
ROLE_ALIASES = {label: key for key, label in ROLE_LABELS.items()}


def role_key(name: str) -> str:
    """advisor/doctor/observer (or the old names) -> the internal key."""
    return ROLE_ALIASES.get(name, name)


def role_label(key: str) -> str:
    return ROLE_LABELS.get(key, key)

