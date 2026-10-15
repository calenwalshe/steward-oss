"""Operator settings from `steward.toml` that more than one module reads (v2.2 deadline section).

`[deadline]` (A-DL-1, A-DL-2): `enabled` (new deadline jobs may be created; default true — the
default *mode* is shadow, and disabling never removes merge protection), `tz` (owner timezone,
default America/Los_Angeles), `mode` (default per-job mode: off|shadow|active, default shadow).
Environment overrides for tests and one-off runs: STEWARD_DEADLINE=0|1, STEWARD_DEADLINE_TZ,
STEWARD_DEADLINE_MODE. A missing or corrupt file means the defaults.

`[flow]` (v2.3 FLOW-04): `mode` off|shadow|active, default **off**, independent of `[jev] mode`. Flow only rides
the existing per-job Jev request, so a job whose observer is off asks no Flow question either. A per-job kv switch
(`FLOW_SWITCH_KEY`) overrides the global mode; `STEWARD_FLOW_MODE` overrides the file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
import tomllib

from steward.store import resolve_config_dir

DEADLINE_MODES = ("off", "shadow", "active")
DEFAULT_TZ = "America/Los_Angeles"


def _toml_section(name: str) -> dict:
    try:
        data = tomllib.loads((resolve_config_dir() / "steward.toml").read_text())
    except (OSError, ValueError):
        return {}
    section = data.get(name, {})
    return section if isinstance(section, dict) else {}


def deadline_settings() -> dict:
    section = _toml_section("deadline")
    enabled = section.get("enabled", True) is not False
    env = os.environ.get("STEWARD_DEADLINE", "").strip().lower()
    if env in ("0", "false", "off", "no"):
        enabled = False
    elif env in ("1", "true", "on", "yes"):
        enabled = True
    tz = os.environ.get("STEWARD_DEADLINE_TZ") or str(section.get("tz") or DEFAULT_TZ)
    mode = (os.environ.get("STEWARD_DEADLINE_MODE") or str(section.get("mode") or "shadow")).strip().lower()
    if mode not in DEADLINE_MODES:
        mode = "shadow"
    return {"enabled": enabled, "tz": tz, "mode": mode}


FLOW_MODES = ("off", "shadow", "active")
FLOW_SWITCH_KEY = "supervisor_flow"


# Phase 24 investigation budgets: explicit, bounded, never unlimited. Mirrors steward.flow_investigator's defaults
# (kept literal here so config never imports the model path).
FLOW_DEFAULTS = {"investigation_timeout_s": 180.0, "investigation_budget_usd": 0.50, "investigation_prompt_chars": 6000}
FLOW_MAX_TIMEOUT_S = 600.0
FLOW_MAX_BUDGET_USD = 2.0
FLOW_MAX_PROMPT_CHARS = 20000


def _bounded(value, default, hi):
    if type(value) not in (int, float) or value <= 0:
        return default
    return type(default)(min(value, hi))


def flow_settings() -> dict:
    section = _toml_section("flow")
    mode = (os.environ.get("STEWARD_FLOW_MODE") or str(section.get("mode") or "off")).strip().lower()
    return {
        "mode": mode if mode in FLOW_MODES else "off",
        "investigation_timeout_s": _bounded(section.get("investigation_timeout_s"),
                                            FLOW_DEFAULTS["investigation_timeout_s"], FLOW_MAX_TIMEOUT_S),
        "investigation_budget_usd": _bounded(section.get("investigation_budget_usd"),
                                             FLOW_DEFAULTS["investigation_budget_usd"], FLOW_MAX_BUDGET_USD),
        "investigation_prompt_chars": _bounded(section.get("investigation_prompt_chars"),
                                               FLOW_DEFAULTS["investigation_prompt_chars"], FLOW_MAX_PROMPT_CHARS),
    }


def flow_mode(store=None, job_id: str | None = None) -> str:
    """The effective Flow mode for a job: a valid per-job switch, else the global setting (default off)."""
    if store is not None and job_id:
        flag = store.kv_get(job_id, FLOW_SWITCH_KEY)
        if flag in FLOW_MODES:
            return flag
    return flow_settings()["mode"]


TRAIN_DEFAULTS: dict = {
    "enabled": False,
    "repo": "",
    "target": "fleet-registry",
    "every_s": 180.0,
    "max_batch": 8,
    "resolver_timeout_s": 1200.0,
    "suite_cmd": "python3 -m pytest -q -n 8 -p no:cacheprovider",
    "worktree_root": str(Path.home() / ".cache" / "steward-merge"),
    "report_hour": 7,
}
_OFF = ("0", "false", "off", "no", "")


def train_settings(env: Mapping[str, str] | None = None) -> dict:
    """`[train]` in steward.toml (landing train, 2026-10-08 spec). STEWARD_TRAIN / STEWARD_TRAIN_REPO override."""
    env = os.environ if env is None else env
    s = dict(TRAIN_DEFAULTS)
    s.update({k: v for k, v in _toml_section("train").items() if k in TRAIN_DEFAULTS})
    if "STEWARD_TRAIN" in env:
        s["enabled"] = env["STEWARD_TRAIN"].strip().lower() not in _OFF
    if env.get("STEWARD_TRAIN_REPO"):
        s["repo"] = env["STEWARD_TRAIN_REPO"]
    s["enabled"] = s["enabled"] is True
    s["every_s"] = float(s["every_s"])
    s["max_batch"] = max(1, int(s["max_batch"]))
    s["resolver_timeout_s"] = float(s["resolver_timeout_s"])
    s["report_hour"] = int(s["report_hour"])
    return s


LIVENESS_DEFAULTS: dict = {"enabled": True, "every_s": 60.0, "orphan_s": 1800.0, "idle_s": 1800.0, "tries_per_day": 2,
                           "no_worker_s": 900.0}


def liveness_settings(env: Mapping[str, str] | None = None) -> dict:
    """`[liveness]` in steward.toml (liveness sweep, 2026-10-08 spec). STEWARD_LIVENESS=0 disables."""
    env = os.environ if env is None else env
    s = dict(LIVENESS_DEFAULTS)
    s.update({k: v for k, v in _toml_section("liveness").items() if k in LIVENESS_DEFAULTS})
    if "STEWARD_LIVENESS" in env:
        s["enabled"] = env["STEWARD_LIVENESS"].strip().lower() not in _OFF
    s["enabled"] = s["enabled"] is True
    for k in ("every_s", "orphan_s", "idle_s", "no_worker_s"):
        s[k] = float(s[k])
    s["tries_per_day"] = max(1, int(s["tries_per_day"]))
    return s


def train_owns(repo, env: Mapping[str, str] | None = None) -> bool:
    s = train_settings(env)
    if not (s["enabled"] and s["repo"]):
        return False
    return Path(str(repo)).resolve() == Path(s["repo"]).resolve()
