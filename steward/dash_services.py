"""The services block of the status pane (`steward dash`), data-driven (Phase 19, G10).

Every optional item shows a named state: per enabled component a green or red dot (`active` / `inactive` from
systemd, or `(services tab)` for a component running in a Herdr pane), per optional item that is off a dim dash
with the reason (`off (no key)`, `off (no codex)`, ...). The public-connector probe row exists only when a
connector is configured; its URL comes from `<config dir>/mcp.json`, never from code, and the HTTP status arrives
from the pane's shell loop (which owns the curl), so this module never touches the network.

`python -m steward.dash_services` prints the lines; `--count` prints only how many there are.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Callable

from steward import components, installstate, keys, oauth, prereq
from steward.store import resolve_config_dir

GREEN, RED = 32, 31

# Installs without an install.json (the repo install) keep the original three-unit check.
LEGACY_UNITS = ("steward-mcp", "steward-tunnel", "steward-mcp-tunnel-local")


def public_probe_url(config_dir: Path | str | None = None) -> str:
    """The URL the status pane probes: the configured public connector's protected-resource metadata
    (`<public_base_url>` from `<config dir>/mcp.json` plus the well-known path), or '' when no connector is
    configured. Never a value from code."""
    path = resolve_config_dir(config_dir) / "mcp.json"
    try:
        cfg = json.loads(path.read_text())
        base = oauth.normalize_base_url(cfg["public_base_url"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return ""
    return base + oauth.PRM_PATH


def _dot(colour: int, name: str, detail: str) -> str:
    return f"  \033[{colour}m●\033[0m {name:<26} {detail}"


def _off(name: str, detail: str) -> str:
    return f"  \033[2m–\033[0m {name:<26} {detail}"


def _systemd_active(unit: str) -> str:
    """`systemctl --user is-active <unit>` ('inactive' when systemd is missing or does not answer)."""
    from steward.runner_exec import SubprocessRunner

    try:
        res = SubprocessRunner().run(["systemctl", "--user", "is-active", unit], ".", timeout=5)
    except Exception:  # noqa: BLE001 - no systemd here: the unit is simply not active
        return "inactive"
    return res.stdout.strip() or "inactive"


def _env_pub(_url: str) -> str:
    return os.environ.get("STEWARD_DASH_PUB") or "-"


def unit_names(state: dict | None, env=None) -> list[str]:
    """The systemd units the status pane shows, in order: the user's own list, else the recorded install's, else the
    legacy trio. Components that run in a Herdr pane have no unit to probe."""
    env = os.environ if env is None else env
    explicit = env.get("STEWARD_DASH_UNITS")
    if explicit is not None:
        return explicit.split()
    if state is None:
        return list(LEGACY_UNITS)
    out: list[str] = []
    for rec in (state.get("components") or {}).values():
        if isinstance(rec, dict) and rec.get("enabled") and rec.get("mode") != "herdr":
            out += [u for u in (rec.get("units") or []) if isinstance(u, str) and u not in out]
    return out


def _unit_rows(state: dict | None, unit_active: Callable[[str], str], env) -> list[str]:
    explicit = env.get("STEWARD_DASH_UNITS")
    if explicit is not None:  # the user's own list (may be empty) wins over everything
        units = explicit.split()
        return [_dot(GREEN if (s := unit_active(u)) == "active" else RED, u, s) for u in units]
    if state is None:
        return [_dot(GREEN if (s := unit_active(u)) == "active" else RED, u, s) for u in LEGACY_UNITS]
    rows: list[str] = []
    comps = state.get("components") or {}
    ordered = [n for n in components.COMPONENT_IDS if n in comps] + [n for n in comps if n not in components.COMPONENT_IDS]
    for name in ordered:
        rec = comps.get(name)
        if not isinstance(rec, dict) or not rec.get("enabled"):
            continue
        units = [u for u in (rec.get("units") or []) if isinstance(u, str)]
        if rec.get("mode") == "herdr":
            rows.append(_dot(GREEN, name, "(services tab)"))
        elif units:
            states = [unit_active(u) for u in units]
            ok = all(s == "active" for s in states)
            rows.append(_dot(GREEN if ok else RED, name, "active" if ok else "inactive"))
        else:
            rows.append(_dot(GREEN, name, "on"))
    return rows


def lines(
    state: dict | None,
    *,
    unit_active: Callable[[str], str] | None = None,
    which: Callable[[str], str | None] | None = None,
    keys_status: Callable[[str], str] | None = None,
    probe: Callable[[str], str] | None = None,
    checks: list | None = None,
    config_dir: Path | str | None = None,
    env=None,
) -> list[str]:
    """The services block as ANSI lines (no trailing blank line). `state` is install.json (None: no install record).
    Every dependency is injectable; `checks` replaces the light prerequisite scan."""
    env = os.environ if env is None else env
    unit_active = unit_active or _systemd_active
    keys_status = keys_status or keys.status
    probe = probe or _env_pub
    cfg_dir = resolve_config_dir(config_dir)
    if checks is None:
        checks = prereq.detect(None, env=env, which=which, config_dir=cfg_dir, light=True)

    out = _unit_rows(state, unit_active, env)
    enabled = {n for n, r in ((state or {}).get("components") or {}).items() if isinstance(r, dict) and r.get("enabled")}
    by_id = {c.id: c for c in checks}
    for id_ in prereq.LIGHT_IDS:
        c = by_id.get(id_)
        if c is None or id_ in enabled:  # an enabled component already has its own row
            continue
        if id_ == "jev":
            hit = keys_status("TYPESAFE_API_KEY")
            if hit.startswith("set"):
                out.append(_dot(GREEN, c.label, hit))
            else:
                out.append(_off(c.label, "off (no key)"))
        elif id_ == "connector":
            url = public_probe_url(cfg_dir)
            if c.status == prereq.OK and url:
                code = probe(url)
                out.append(_dot(GREEN if code == "200" else RED, "public connector", code))
            else:
                out.append(_off("public connector", "off (no public connector)"))
        elif id_ == "systemd" and c.status == prereq.OK:
            continue  # a running user manager needs no row of its own: the unit rows above show each unit's state
        elif c.status == prereq.OK:
            out.append(_dot(GREEN, c.label, c.detail))
        else:
            out.append(_off(c.label, c.detail))
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        state = installstate.load()
    except installstate.InstallStateError:
        state = None
    block = lines(state)
    print(len(block) if "--count" in args else "\n".join(block))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
