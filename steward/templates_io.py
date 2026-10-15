"""Never-overwrite config templates (Phase 19, G6).

`write_templates` creates `steward.toml`, `trajectory.toml` and `roster.toml` from `steward/templates/` only when missing. An
existing file is left alone. When the TEMPLATE changed since it was recorded in install.json (the hash of the raw
template file) and the file differs from the new text, the new text is written beside it as `<name>.new`. It fills
the passed state dict (`template_hashes`, `files`) and never saves it. Directories are created 0700.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from steward import installstate

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
NAMES = ("steward.toml", "trajectory.toml", "roster.toml")

CREATED, UNCHANGED, NEW_WRITTEN, SKIPPED_USER_EDIT = "created", "unchanged", "new_written", "skipped_user_edit"


@dataclass(frozen=True)
class Outcome:
    name: str
    path: Path
    action: str  # created | unchanged | new_written | skipped_user_edit


def ensure_dirs(*dirs: Path | str) -> list[Path]:
    """Create each missing directory (and missing parents) at 0700; existing directories keep their mode."""
    made: list[Path] = []
    for d in dirs:
        missing = []
        p = Path(d)
        while not p.exists() and p != p.parent:
            missing.append(p)
            p = p.parent
        for m in reversed(missing):
            m.mkdir(mode=0o700, exist_ok=True)
            os.chmod(m, 0o700)
            made.append(m)
    return made


def _raw(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text()


def template_hash(name: str) -> str:
    return hashlib.sha256(_raw(name).encode()).hexdigest()


def _toml_string(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def render(name: str, owner_name: str, version: str) -> str:
    """The template with `{owner_name}` and `{version}` filled in (plain replace: TOML braces are left alone)."""
    return _raw(name).replace("{owner_name}", _toml_string(owner_name)).replace("{version}", str(version))


def default_owner_name() -> str:
    from steward import owner

    return owner.name()


def _write(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def write_templates(config_dir: Path | str, state: dict, owner_name: str | None = None,
                    version: str | None = None) -> list[Outcome]:
    config = Path(config_dir)
    ensure_dirs(config)
    if owner_name is None:
        owner_name = default_owner_name()
    if version is None:
        version = str(state.get("version") or "")
    hashes = state.setdefault("template_hashes", {})
    outcomes = []
    for name in NAMES:
        target = config / name
        text = render(name, owner_name, version)
        current = template_hash(name)
        if not target.exists():
            _write(target, text)
            hashes[name] = current
            installstate.record_file(state, target, "config")
            outcomes.append(Outcome(name, target, CREATED))
            continue
        recorded = hashes.get(name)
        same = target.read_text() == text
        if recorded is not None and recorded != current and not same:
            beside = config / (name + ".new")
            _write(beside, text)
            hashes[name] = current
            installstate.record_file(state, beside, "config-new")
            outcomes.append(Outcome(name, beside, NEW_WRITTEN))
            continue
        hashes[name] = current
        outcomes.append(Outcome(name, target, UNCHANGED if same else SKIPPED_USER_EDIT))
    return outcomes
