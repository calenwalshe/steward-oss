"""Version, install root and the `steward --version` line (Phase 19, G2/G17).

One number: `steward/_version.py` (stamped by the pack build) wins, then `pyproject.toml`'s `[project] version`,
then `0.0.0+unknown`. The install root is `$STEWARD_INSTALL_ROOT` (set by the bin shim) or the directory that
contains the `steward` package. Nothing here touches the network or writes a file.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import tomllib
from pathlib import Path
from typing import Callable, Mapping

UNKNOWN = "0.0.0+unknown"
_STAMP_RE = re.compile(r"""^\s*__version__\s*=\s*['"]([^'"]+)['"]""", re.M)
_NPM_RE = re.compile(r"""^\s*NPM_PACKAGE\s*=\s*['"]([^'"]+)['"]""", re.M)
NPM_FALLBACK = "the package you installed"


def _package_dir() -> Path:
    return Path(__file__).resolve().parent


def install_root(environ: Mapping[str, str] | None = None) -> Path:
    """`$STEWARD_INSTALL_ROOT`, else the parent of the `steward` package."""
    env = os.environ if environ is None else environ
    raw = (env.get("STEWARD_INSTALL_ROOT") or "").strip()
    return Path(raw) if raw else _package_dir().parent


def is_package_install(environ: Mapping[str, str] | None = None) -> bool:
    """Package mode is opt-in by environment: only when STEWARD_INSTALL_ROOT is set."""
    env = os.environ if environ is None else environ
    return bool((env.get("STEWARD_INSTALL_ROOT") or "").strip())


def version(stamp: Path | str | None = None, pyproject: Path | str | None = None) -> str:
    """The stamped version, else pyproject's, else `0.0.0+unknown`. Paths are injectable for tests."""
    stamp_path = Path(stamp) if stamp is not None else _package_dir() / "_version.py"
    try:
        hit = _STAMP_RE.search(stamp_path.read_text())
        if hit:
            return hit.group(1)
    except OSError:
        pass
    py_path = Path(pyproject) if pyproject is not None else install_root() / "pyproject.toml"
    if pyproject is None and not py_path.exists():
        py_path = _package_dir().parent / "pyproject.toml"
    try:
        value = tomllib.loads(py_path.read_text()).get("project", {}).get("version")
        if isinstance(value, str) and value.strip():
            return value.strip()
    except (OSError, ValueError):
        pass
    return UNKNOWN


def npm_package(stamp: Path | str | None = None) -> str:
    """The npm package name stamped into `steward/_version.py` by the pack build, else a generic phrase for the
    `npm rm -g ...` hint. Paths are injectable for tests."""
    stamp_path = Path(stamp) if stamp is not None else _package_dir() / "_version.py"
    try:
        hit = _NPM_RE.search(stamp_path.read_text())
        if hit:
            return hit.group(1)
    except OSError:
        pass
    return NPM_FALLBACK


def major(value: str | None = None) -> int:
    """The major number of `value` (default: this version); 0 when it does not parse."""
    text = version() if value is None else str(value)
    hit = re.match(r"\s*(\d+)", text)
    return int(hit.group(1)) if hit else 0


def _inside(path: Path, root: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(Path(os.path.realpath(root)))
        return True
    except ValueError:
        return False


def describe(which: Callable[[str], str | None] | None = None, environ: Mapping[str, str] | None = None,
             version_text: str | None = None) -> str:
    """`steward 0.1.0 (install root: <root>, python 3.12.x)` plus `; another steward on PATH: <path>`."""
    root = install_root(environ)
    line = f"steward {version_text or version()} (install root: {root}, python {platform.python_version()})"
    found = (which or shutil.which)("steward")
    if found and not _inside(Path(found), root):
        line += f"; another steward on PATH: {found}"
    return line
