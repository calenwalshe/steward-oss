"""Package mode for panes (Phase 19, G2/G17): an opt-in environment layer.

A new pane's shell does not inherit the caller's environment. When `STEWARD_INSTALL_ROOT` is set (by the bin shim),
every pane command Steward builds also exports PYTHONPATH and the forwarded variables, and `python3 -m steward.cli`
spawns use the interpreter that is running now. With the variable unset every string is exactly what it was before.
"""

from __future__ import annotations

import os
import shlex
import sys
from typing import Mapping

from steward import version

FORWARD = (
    "STEWARD_INSTALL_ROOT", "STEWARD_HOME", "STEWARD_EXCEPTIONS", "STEWARD_CONFIG_DIR", "STEWARD_DATA_DIR",
    "STEWARD_API_KEYS_FILE", "STEWARD_LEGACY_KEYS_FILE", "STEWARD_OWNER_NAME", "STEWARD_OWNER_PRONOUNS",
    "STEWARD_CLAUDE_DIR", "STEWARD_CLAUDE_JSON", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "HOME", "PATH",
)


def env_exports(environ: Mapping[str, str] | None = None, skip: tuple[str, ...] = ()) -> str:
    """`export NAME=value && ` for PYTHONPATH (the install root) and each set FORWARD variable; every value goes
    through shlex.quote. Empty unless STEWARD_INSTALL_ROOT is set. `skip` names variables the caller already exports."""
    env = os.environ if environ is None else environ
    if not version.is_package_install(env):
        return ""
    pairs = [("PYTHONPATH", str(version.install_root(env)))]
    pairs += [(name, env[name]) for name in FORWARD if env.get(name)]
    return "".join(f"export {name}={shlex.quote(value)} && " for name, value in pairs if name not in skip)


def cli_argv(environ: Mapping[str, str] | None = None) -> list[str]:
    """The argv prefix that runs Steward's CLI: the running interpreter in package mode, else `python3`."""
    if version.is_package_install(environ):
        return [sys.executable, "-m", "steward.cli"]
    return ["python3", "-m", "steward.cli"]


def cli_command(environ: Mapping[str, str] | None = None) -> str:
    """The shell-string form of `cli_argv()`."""
    return " ".join(shlex.quote(p) for p in cli_argv(environ))
