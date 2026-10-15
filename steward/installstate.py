"""`install.json`: the one install manifest (Phase 19, G5/G14/G16/G17).

Lives at `$STEWARD_HOME/install.json`. It records what the installer did (files written, tabs, components, plugin
links) so uninstall undoes only that, and which version the intro was last shown for. Written atomically at 0600.
A corrupt file is an error the caller reports; nothing here silently rewrites it.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import platform
import re
import tempfile
from pathlib import Path

from steward.store import resolve_home

SCHEMA = 1
FILE_NAME = "install.json"


class InstallStateError(RuntimeError):
    """install.json exists but cannot be read as a manifest."""


def path(home: Path | str | None = None) -> Path:
    return Path(resolve_home(home)) / FILE_NAME


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def new_state(version: str, install_root: str | Path, python: str | None = None, now: str | None = None) -> dict:
    stamp = now or _now()
    return {
        "schema": SCHEMA,
        "version": str(version),
        "install_root": str(install_root),
        "python": python or platform.python_version(),
        "installed_at": stamp,
        "updated_at": stamp,
        "intro_seen": None,
        "config_schema": 1,
        "template_hashes": {},
        "files": [],
        "tabs": [],
        "components": {},
        "plugin_links": [],
    }


def load(home: Path | str | None = None) -> dict | None:
    """The manifest, or None when there is none. Unreadable JSON raises InstallStateError (never a reset)."""
    p = path(home)
    try:
        text = p.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InstallStateError(f"cannot read {p}: {type(exc).__name__}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise InstallStateError(f"{p} is not valid JSON; fix or remove it") from exc
    if not isinstance(data, dict):
        raise InstallStateError(f"{p} is not a manifest object; fix or remove it")
    return data


def save(state: dict, home: Path | str | None = None, now: str | None = None) -> Path:
    """Atomic write (temp file in the same directory, then os.replace) at 0600; parent directory 0700 if created."""
    p = path(home)
    parent = p.parent
    if not parent.exists():
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(parent, 0o700)
    state["updated_at"] = now or _now()
    fd, tmp = tempfile.mkstemp(prefix=".install.json.", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def record_file(state: dict, file_path: str | Path, kind: str = "file") -> None:
    """Record a file the installer wrote, de-duplicated by path (a later record replaces the kind)."""
    entry = {"path": str(file_path), "kind": kind}
    files = [f for f in state.setdefault("files", []) if f.get("path") != entry["path"]]
    files.append(entry)
    state["files"] = files


def record_component(state: dict, name: str, **info) -> None:
    """Merge `info` into the component's record (enabled units, ports, pane titles...)."""
    comps = state.setdefault("components", {})
    merged = dict(comps.get(name) or {})
    merged.update(info)
    comps[name] = merged


def mark_intro_seen(state: dict, version: str) -> None:
    state["intro_seen"] = str(version)


def _major(text: object) -> int:
    hit = re.match(r"\s*(\d+)", str(text or ""))
    return int(hit.group(1)) if hit else 0


def needs_intro(state: dict | None, current_version: str) -> tuple[bool, str | None]:
    """`(True, "first")` when never shown, `(True, "major")` after a major-version change, else `(False, None)`."""
    seen = (state or {}).get("intro_seen")
    if not seen:
        return True, "first"
    if _major(current_version) != _major(seen):
        return True, "major"
    return False, None
