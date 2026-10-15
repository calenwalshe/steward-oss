"""Runtime CLI versions (FLEET-27, D-02): what a qualification was recorded against, and what is installed now.

A qualification is evidence about ONE CLI build. Each probe suite records the version its probes ran against; a
runtime whose installed CLI now reports anything else is `stale` - undispatchable until `steward fleet qualify` runs
again. Versions are read with `<cli> --version` (no TTY: stdin is /dev/null) and cached briefly so per-operation
capability checks do not spawn a process each time. Tests replace `current_version`; they never spawn real CLIs.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Callable

VERSION_ARGV: dict[str, tuple[str, ...]] = {
    "codex": ("codex", "--version"),
    "muse": ("muse", "--version"),
    "agy": ("agy", "--version"),
    "pi": ("pi", "--version"),
    "claude-fabric": ("claude", "--version"),
}
CACHE_TTL_S = 60.0
_cache: dict[str, tuple[float, str | None]] = {}


def read_version(runtime: str, *, run: Callable = subprocess.run, timeout: float = 15.0) -> str | None:
    """The runtime CLI's own version line, or None (unknown runtime, CLI missing, non-zero exit, no output)."""
    argv = VERSION_ARGV.get(runtime)
    if argv is None:
        return None
    env = dict(os.environ)
    env.pop("META_API_KEY", None)
    try:
        res = run(list(argv), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, env=env,
                  check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(res, "returncode", 1) != 0:
        return None
    line = next((ln.strip() for ln in (getattr(res, "stdout", "") or "").splitlines() if ln.strip()), "")
    return line[:200] or None


def current_version(runtime: str) -> str | None:
    now = time.monotonic()
    hit = _cache.get(runtime)
    if hit is not None and now - hit[0] < CACHE_TTL_S:
        return hit[1]
    v = read_version(runtime)
    _cache[runtime] = (now, v)
    return v
