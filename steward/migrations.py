"""Config migrations (Phase 19, G15): pure functions over file text, explicit, backed up and additive.

`steward.toml` carries `schema = N` (absent means 1). `run` brings a config directory from its current schema to
`target`. Each step is a pure function `text -> (new_text, changes)` registered under its target schema and file name.
Before any file is touched `<file>.bak-<old version>` is written (a number is appended rather than overwrite an
existing backup). A step may only add or rename keys: the parsed key paths before and after are compared, and a step
that would drop one raises `MigrationError` and leaves every file exactly as it was. A function that renames declares
it in a `renames` attribute (`{"jev.mode": "jev.level"}`). The SQLite store keeps its own additive ALTER TABLE path.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import tomllib
from pathlib import Path
from typing import Callable

from steward import tomledit

CURRENT_SCHEMA = 1
SCHEMA_FILE = "steward.toml"

Migration = Callable[[str], "tuple[str, list[str]]"]

# target schema -> {file name: function}. Empty at schema 1: no migration exists yet.
MIGRATIONS: dict[int, dict[str, Migration]] = {}


class MigrationError(RuntimeError):
    """A migration could not be applied safely; the files are untouched."""


def renames(mapping: dict[str, str]):
    """Decorator: declare the key paths a migration function renames (old path -> new path)."""
    def wrap(fn):
        fn.renames = dict(mapping)
        return fn
    return wrap


def config_schema(text: str) -> int:
    """The `schema` of a steward.toml text; 1 when the key is absent (files written before migrations existed)."""
    try:
        value = tomllib.loads(text).get("schema", 1)
    except ValueError as exc:
        raise MigrationError(f"steward.toml is not valid TOML: {exc}") from exc
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else 1


def _paths(node, prefix: str = "") -> set[str]:
    out: set[str] = set()
    if isinstance(node, dict):
        if not node and prefix:
            out.add(prefix)
        for key, value in node.items():
            out |= _paths(value, f"{prefix}.{key}" if prefix else str(key))
    else:
        out.add(prefix)
    return out


def _parse(text: str, what: str) -> dict:
    try:
        return tomllib.loads(text)
    except ValueError as exc:
        raise MigrationError(f"{what} is not valid TOML: {exc}") from exc


def _check_additive(name: str, old_text: str, new_text: str, declared: dict[str, str]) -> None:
    before, after = _paths(_parse(old_text, name)), _paths(_parse(new_text, name))
    for path in sorted(before - after):
        target = declared.get(path)
        if target is None or target not in after:
            raise MigrationError(f"migration would drop {path} from {name}; aborted, nothing changed")


def _backup(path: Path, from_version: str) -> Path:
    dest = path.with_name(f"{path.name}.bak-{from_version}")
    n = 2
    while dest.exists():
        dest = path.with_name(f"{path.name}.bak-{from_version}-{n}")
        n += 1
    shutil.copy2(path, dest)
    return dest


def _write_atomic(path: Path, text: str) -> None:
    mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run(config_dir: Path | str, from_version: str, target: int = CURRENT_SCHEMA,
        migrations: dict[int, dict[str, Migration]] | None = None, log: Callable[[str], object] = print) -> list[str]:
    """Migrate the config directory to `target`; returns the list of changes made (empty: a no-op)."""
    table = MIGRATIONS if migrations is None else migrations
    config = Path(config_dir)
    schema_path = config / SCHEMA_FILE
    if not schema_path.is_file():
        return []
    current = config_schema(schema_path.read_text())
    steps = [n for n in range(current + 1, target + 1)]
    if not steps:
        return []

    touched = {SCHEMA_FILE}
    for n in steps:
        touched |= set(table.get(n, {}))
    files = sorted(f for f in touched if (config / f).is_file())
    for name in files:  # the backup comes first, before anything is computed or written
        _backup(config / name, from_version)

    texts = {name: (config / name).read_text() for name in files}
    changes: list[str] = []
    for n in steps:
        for name, fn in sorted(table.get(n, {}).items()):
            if name not in texts:
                continue
            try:
                new_text, notes = fn(texts[name])
            except MigrationError:
                raise
            except Exception as exc:
                raise MigrationError(f"migration to schema {n} failed on {name}: {type(exc).__name__}") from exc
            _check_additive(name, texts[name], new_text, getattr(fn, "renames", {}))
            texts[name] = new_text
            changes += [f"{name} (schema {n}): {note}" for note in notes]
        bumped = _bump(texts[SCHEMA_FILE], n)
        _check_additive(SCHEMA_FILE, texts[SCHEMA_FILE], bumped, {})
        texts[SCHEMA_FILE] = bumped
        changes.append(f"{SCHEMA_FILE}: schema = {n}")

    for name in files:
        if texts[name] != (config / name).read_text():
            _write_atomic(config / name, texts[name])
    for line in changes:
        log(f"config migration: {line}")
    return changes


def _bump(text: str, n: int) -> str:
    try:
        return tomledit.set_key(text, "", "schema", n)
    except tomledit.TomlEditError as exc:
        raise MigrationError(f"cannot set schema = {n}: {exc}") from exc
