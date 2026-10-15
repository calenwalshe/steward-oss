"""Where Steward finds a secret by name (Phase 19, G11).

A name is looked up in this order, first non-empty value wins:

1. the process environment;
2. `$STEWARD_API_KEYS_FILE` (an explicit override file);
3. `<config dir>/keys.env` (Steward's own file, 0600, created empty on request by `ensure_keys_env`);
4. `$STEWARD_LEGACY_KEYS_FILE`, else `~/.api-keys` (the older single-file convention keeps working, last).

Files hold `NAME=value` lines (`export NAME=value`, quotes and `# comments` are understood). Values are returned
only by `lookup` / `load`, to the caller that asked for exactly that name; `status`, `key_names` and `chain` return
labels and names, never a value. Callers that hand a value on (granted worker keys, the Jev key) register it with
`steward.masking` themselves, as before. Workers cannot read these files (`steward.permissions`).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from steward.store import resolve_config_dir

KEY_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
KEYS_ENV_NAME = "keys.env"
_HEADER = "# Steward API keys: one NAME=value per line. Read by Steward only; never share or commit this file.\n"


# -- the files ------------------------------------------------------------------------------------------------------


def keys_env_path() -> Path:
    """`<config dir>/keys.env` (config dir as `store.resolve_config_dir`)."""
    return Path(resolve_config_dir()) / KEYS_ENV_NAME


def legacy_path() -> Path:
    """`$STEWARD_LEGACY_KEYS_FILE`, else `~/.api-keys`: the last source in the chain."""
    return Path(os.environ.get("STEWARD_LEGACY_KEYS_FILE") or Path.home() / ".api-keys")


def api_keys_file_path() -> Path | None:
    """`$STEWARD_API_KEYS_FILE` when set, else None."""
    raw = os.environ.get("STEWARD_API_KEYS_FILE")
    return Path(raw) if raw else None


def chain() -> list[tuple[str, Path | None]]:
    """The lookup order as `(label, file)`; the environment is `("env", None)`. Paths only, no values."""
    out: list[tuple[str, Path | None]] = [("env", None)]
    override = api_keys_file_path()
    if override is not None:
        out.append(("STEWARD_API_KEYS_FILE", override))
    out.append(("keys.env", keys_env_path()))
    out.append(("legacy file", legacy_path()))
    return out


def parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[7:].lstrip()
    name, sep, value = line.partition("=")
    name = name.strip()
    if not sep or not KEY_NAME_RE.match(name):
        return None
    value = value.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        value = value[1:end] if end > 0 else value[1:]
    else:
        value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    return name, value


def read_file(path: Path) -> dict[str, str]:
    """`{name: value}` for every well-formed line of one file ({} when unreadable). The first line of a name wins."""
    try:
        text = Path(path).read_text()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for parsed in map(parse_line, text.splitlines()):
        if parsed and parsed[0] not in out:
            out[parsed[0]] = parsed[1]
    return out


# -- lookup ---------------------------------------------------------------------------------------------------------


def _source_of(name: str) -> tuple[str, str] | None:
    """`(label, value)` of the first source holding a non-empty value for `name`."""
    for label, path in chain():
        if path is None:
            value = os.environ.get(name, "")
        else:
            value = read_file(path).get(name, "")
        if value and value.strip():
            return label, value
    return None


def lookup(name: str) -> str | None:
    """The value of `name` from the first source that has it, or None. The only call that returns a secret."""
    if not KEY_NAME_RE.match(str(name or "")):
        return None
    hit = _source_of(name)
    return hit[1] if hit else None


def load(names) -> dict[str, str]:
    """`{name: value}` for exactly the requested names (missing ones are omitted)."""
    out: dict[str, str] = {}
    for name in dict.fromkeys(names):
        value = lookup(name)
        if value:
            out[name] = value
    return out


def status(name: str) -> str:
    """`set (env)`, `set (STEWARD_API_KEYS_FILE)`, `set (keys.env)`, `set (legacy file)` or `missing`. Never a value."""
    if not KEY_NAME_RE.match(str(name or "")):
        return "missing"
    hit = _source_of(name)
    return f"set ({hit[0]})" if hit else "missing"


def key_names() -> list[str]:
    """Names (never values) present in any file of the chain, de-duplicated, in first-seen order."""
    seen: dict[str, None] = {}
    for _label, path in chain():
        if path is not None:
            for name in read_file(path):
                seen.setdefault(name)
    return list(seen)


# -- keys.env -------------------------------------------------------------------------------------------------------


def ensure_keys_env() -> Path:
    """Create `<config dir>/keys.env` empty (one comment line) at 0600 when missing; a new config directory is 0700.
    An existing file's content is never changed; a looser mode is tightened to 0600. Returns the path."""
    path = keys_env_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as fh:
            fh.write(_HEADER)
    if os.stat(path).st_mode & 0o077:
        os.chmod(path, 0o600)
    return path
