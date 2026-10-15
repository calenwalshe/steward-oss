"""Coordinator settings, read from ``coordinator.toml`` in the Steward config dir."""
from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .. import coordinator_link, stability, store

log = logging.getLogger(__name__)


@dataclass
class Settings:
    enabled: bool = True
    poll_s: float = 15.0
    hold_s: float = 1200.0
    ask_by_s: float = 1080.0
    rerun_run_s: float = 300.0
    rerun_total_s: float = 900.0
    rerun_max_ids: int = 50
    episodes_per_job_day: int = 3
    cooldown_s: float = 1800.0
    tries_per_rung_day: int = 2
    resolved_watch_s: float = 600.0
    orphan_s: float = 3600.0
    orphan_scan_s: float = 300.0
    health_red_s: float = 120.0
    model_primary: str = "codex:gpt-5.5"
    model_fallback: str = "claude:sonnet"
    model_reopen: str = "claude:opus"
    model_timeout_s: float = 300.0
    model_calls_per_episode: int = 2
    model_actions_per_call: int = 4
    budget_daily_usd: float = 10.0
    budget_daily_codex_calls: int = 20
    budget_call_usd: float = 2.0
    digest_slug: str = "steward-coordinator"
    digest_at: str = "07:00"
    steward_home: Path = field(default_factory=store.resolve_home)
    steward_repo: Path = field(default_factory=lambda: Path(stability.LIVE_ROOT))


_SECTIONS = ("model", "budget", "digest")
_SKIP = ("steward_home", "steward_repo")


def _coerce(default, value):
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    if isinstance(default, str):
        return str(value)
    return value


def _flatten(data: dict) -> dict:
    flat: dict = {}
    for key, val in data.items():
        if key in _SECTIONS and isinstance(val, dict):
            for sub, sv in val.items():
                flat[f"{key}_{sub}"] = sv
        elif key not in _SECTIONS:
            flat[key] = val
    return flat


def load(path: Path | None = None) -> Settings:
    s = Settings()
    p = Path(path) if path is not None else store.resolve_config_dir() / "coordinator.toml"
    try:
        data = tomllib.loads(p.read_text())
    except FileNotFoundError:
        data = {}
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        log.warning("coordinator config %s unreadable (%s); using defaults", p, exc)
        data = {}
    known = {f.name: getattr(s, f.name) for f in fields(Settings) if f.name not in _SKIP}
    for key, val in _flatten(data).items():
        if key not in known:
            continue
        try:
            setattr(s, key, _coerce(known[key], val))
        except (TypeError, ValueError):
            log.warning("coordinator config: bad value for %s; keeping default", key)
    if s.hold_s != coordinator_link.HOLD_S:
        log.warning("coordinator config hold_s=%s != HOLD_S=%s; HOLD_S wins",
                    s.hold_s, coordinator_link.HOLD_S)
        s.hold_s = coordinator_link.HOLD_S
    return s
