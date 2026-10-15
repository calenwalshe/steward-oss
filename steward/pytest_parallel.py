"""Execution policy for full-suite pytest checks: parallel (pytest-xdist) when it is safe, serial otherwise.

`plan()` is the one place that decides how a check line runs; `baseline.run_check` calls it, so baseline, verify,
shared check runs and the autopilot merged-tree check all behave alike.

- Eligible = a bare full-suite pytest line (`pytest`, `python3 -m pytest`, `pytest tests`): no node ids, files,
  -k/-m selectors, shell operators, and no explicit `-n` / `-p no:xdist` already in the command.
- Workers come from the CLI (`-n N` appended), never PYTEST_ADDOPTS, which would leak into the nested pytest
  runs that tests spawn. Every pytest-shaped check run also carries NESTED_ENV, and a run that already sees it
  never auto-parallelizes.
- `STEWARD_PYTEST_WORKERS`: 0 = serial, N = N workers (explicit opt-in for any repo). Unset = DEFAULT_WORKERS in
  the Steward repo (or a worktree of it) only; every other repo stays serial.
- xdist is probed with the command's own interpreter; missing xdist = serial, unchanged.
- Host-wide cap (`STEWARD_PYTEST_WORKER_CAP`, default 16 workers) via file leases under
  `resolve_home()/pytest-workers`, serialized by flock. If capacity is not free within `STEWARD_PYTEST_WAIT_S`
  (default 300 s) the run goes SERIAL (it never queues unboundedly and never exceeds the cap).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from steward.store import resolve_home

WORKERS_ENV = "STEWARD_PYTEST_WORKERS"
CAP_ENV = "STEWARD_PYTEST_WORKER_CAP"
WAIT_ENV = "STEWARD_PYTEST_WAIT_S"
NESTED_ENV = "STEWARD_PYTEST_NESTED"
DEFAULT_WORKERS = 8
DEFAULT_CAP = 16
DEFAULT_WAIT_S = 300.0
LEASE_MAX_AGE_S = 6 * 3600.0
POLL_S = 2.0

_SHELL_META = re.compile(r"[;&|<>$`()\n\\]")


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return max(0, int(raw)) if raw else default
    except ValueError:
        return default


def explicit_workers() -> int | None:
    """STEWARD_PYTEST_WORKERS as an int (0 = serial), or None when unset/garbage."""
    raw = os.environ.get(WORKERS_ENV, "").strip()
    try:
        return max(0, int(raw)) if raw else None
    except ValueError:
        return None


def is_steward_tree(cwd: Path | str) -> bool:
    cwd = Path(cwd)
    try:
        return ((cwd / "steward" / "pytest_parallel.py").is_file()
                and 'name = "steward"' in (cwd / "pyproject.toml").read_text())
    except OSError:
        return False


def configured_workers(cwd: Path | str | None = None) -> int:
    """Workers wanted for a full-suite run in `cwd` (0 = serial). cwd None: the repo gate is ignored."""
    explicit = explicit_workers()
    if explicit is not None:
        return explicit
    return DEFAULT_WORKERS if (cwd is None or is_steward_tree(cwd)) else 0


def _words(line: str) -> list[str] | None:
    if _SHELL_META.search(line):
        return None
    try:
        return shlex.split(line)
    except ValueError:
        return None


def pytest_prefix(words: list[str]) -> list[str] | None:
    """The leading words that launch pytest (`pytest` / `python3 -m pytest`), else None."""
    if words and os.path.basename(words[0]) == "pytest":
        return words[:1]
    if len(words) >= 3 and os.path.basename(words[0]).startswith("python") and words[1:3] == ["-m", "pytest"]:
        return words[:3]
    return None


def _has_worker_flag(rest: list[str]) -> bool:
    for i, w in enumerate(rest):
        if w == "-n" or w.startswith(("-n", "--numprocesses", "--dist")) and not w.startswith("--no"):
            return True
        if w == "-p" and i + 1 < len(rest) and rest[i + 1].startswith("no:xdist"):
            return True
        if w.startswith("-p") and w[2:].startswith("no:xdist"):
            return True
    return False


def eligible(line: str) -> bool:
    """A full-suite pytest line with no explicit parallelism choice of its own."""
    from steward.stability import is_full_suite

    words = _words(line)
    if not words or pytest_prefix(words) is None:
        return False
    return is_full_suite(line) and not _has_worker_flag(words[len(pytest_prefix(words)):])


_probe_cache: dict[tuple, bool] = {}


def xdist_available(line: str, cwd: Path | str, env: dict[str, str] | None = None) -> bool:
    """Can the interpreter that will run `line` import xdist? Probed through bash -lc like the check itself."""
    words = _words(line)
    prefix = pytest_prefix(words or [])
    if not prefix:
        return False
    env = env if env is not None else dict(os.environ)
    key = (tuple(prefix), str(cwd), env.get("PATH", ""), env.get("VIRTUAL_ENV", ""))
    if key not in _probe_cache:
        if len(prefix) == 3:
            probe = f"{shlex.quote(prefix[0])} -c 'import xdist'"
        else:
            exe = shlex.quote(prefix[0])
            probe = (f'p=$(command -v {exe}) && py=$(head -n1 "$p" | sed "s/^#!//") && $py -c "import xdist"')
        try:
            ok = subprocess.run(["bash", "-lc", probe], cwd=str(cwd), env=env, capture_output=True,
                                timeout=60).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        _probe_cache[key] = ok
    return _probe_cache[key]


def execution_label(commands: list[str], cwd: Path | str | None = None) -> str:
    """'serial' or 'xdist-N' for the fingerprints: a serial receipt must never satisfy a parallel run."""
    n = configured_workers(cwd)
    if n > 0 and not os.environ.get(NESTED_ENV) and any(eligible(c) for c in commands):
        return f"xdist-{n}"
    return "serial"


# -- host-wide worker semaphore -------------------------------------------------------------------------------


def cap() -> int:
    return max(1, _int_env(CAP_ENV, DEFAULT_CAP))


def _lease_dir() -> Path:
    d = resolve_home() / "pytest-workers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _live_leases(d: Path, now: float) -> list[tuple[Path, int]]:
    out = []
    for f in d.glob("lease-*.json"):
        try:
            data = json.loads(f.read_text())
            pid, n, ts = int(data["pid"]), int(data["workers"]), float(data["ts"])
        except (OSError, ValueError, KeyError, TypeError):
            f.unlink(missing_ok=True)
            continue
        if not _pid_alive(pid) or now - ts > LEASE_MAX_AGE_S:
            f.unlink(missing_ok=True)
            continue
        out.append((f, n))
    return out


def try_acquire(workers: int, *, limit: int | None = None) -> Path | None:
    """One non-blocking attempt for `workers` slots; the lease path, or None when the host is at its cap."""
    limit = limit or cap()
    d = _lease_dir()
    with open(d / ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            used = sum(n for _f, n in _live_leases(d, time.time()))
            if used + workers > limit:
                return None
            lease = d / f"lease-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
            lease.write_text(json.dumps({"pid": os.getpid(), "workers": workers, "ts": time.time()}))
            return lease
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def release(lease: Path | None) -> None:
    if lease is not None:
        lease.unlink(missing_ok=True)


def acquire(workers: int, *, wait_s: float | None = None, sleep=time.sleep, clock=time.monotonic) -> Path | None:
    """Wait up to `wait_s` for capacity; None means 'run serial instead'."""
    wait_s = float(os.environ.get(WAIT_ENV, DEFAULT_WAIT_S)) if wait_s is None else wait_s
    workers = min(workers, cap())
    deadline = clock() + wait_s
    while True:
        lease = try_acquire(workers)
        if lease is not None or clock() >= deadline:
            return lease
        sleep(min(POLL_S, max(0.0, deadline - clock())))


# -- the plan ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    line: str
    env: dict
    workers: int  # 0 = serial
    reason: str


@contextlib.contextmanager
def plan(line: str, cwd: Path | str, env: dict[str, str], *, wait_s: float | None = None, sleep=time.sleep):
    """How `line` will run: the (possibly `-n N`-extended) line and env, holding host capacity for the run."""
    env = {**env}
    if not re.search(r"\bpytest\b", line):
        yield Plan(line, env, 0, "not pytest")
        return
    nested = bool(os.environ.get(NESTED_ENV))
    env[NESTED_ENV] = "1"
    if nested:
        yield Plan(line, env, 0, "nested run")
        return
    workers = configured_workers(cwd)
    if workers <= 0:
        yield Plan(line, env, 0, "serial: off or repo not opted in")
        return
    if not eligible(line):
        yield Plan(line, env, 0, "serial: not a bare full-suite pytest line")
        return
    if not xdist_available(line, cwd, env):
        yield Plan(line, env, 0, "serial: xdist not importable")
        return
    workers = min(workers, cap())
    lease = acquire(workers, wait_s=wait_s, sleep=sleep)
    if lease is None:
        yield Plan(line, env, 0, "serial: no worker capacity within the wait")
        return
    try:
        yield Plan(f"{line} -n {workers}", env, workers, f"xdist -n {workers}")
    finally:
        release(lease)
