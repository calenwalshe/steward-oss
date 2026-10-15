"""Process identity and exclusivity for Steward's own processes (issue W, self-heal spec §4).

Everything here works from files and `/proc` only, never the DB, so the watchdog can tell live from dead while the
store is locked:

* identity = {pid, start_ticks, ...}: `start_ticks` (field 22 of /proc/<pid>/stat) tells a recycled pid from the
  process that held it; an `execv` keeps it (a self-restart is the same process). A zombie counts as dead.
* `steward run <job>` holds `<home>/locks/runner-<job>.lock` and `steward control` holds `<home>/locks/control.lock`
  (flock, for the process's life; the kernel drops it when the process dies). A second copy finds the lock held and
  exits before touching anything: the final guard against a double, whoever launched it.
* `<home>/control.json` is control's identity plus `beat_at` (refreshed every loop pass: wedge detection);
  `<home>/runners/<job>.json` is a runner's identity; `<home>/runners/<job>.starting` marks a launch in progress.
* `find_processes` finds a lock-less (pre-change) `steward control` / `steward run` of this home by cmdline + env,
  so a rollout never mistakes a legacy process for a dead one.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable

STARTING_GRACE_S = 45.0


def _clk_tck() -> int:
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError):
        return 100


def _stat_fields(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text()
    except (OSError, ValueError):
        return None
    # comm (field 2) may contain spaces/parens: split after the last ')'
    rest = raw[raw.rfind(")") + 2:].split()
    return rest  # rest[0] = state (field 3), rest[19] = starttime (field 22)


def start_ticks(pid: int) -> int | None:
    f = _stat_fields(pid)
    try:
        return int(f[19]) if f else None
    except (IndexError, ValueError):
        return None


def proc_state(pid: int) -> str | None:
    f = _stat_fields(pid)
    return f[0] if f else None


def identity(pid: int | None = None, **extra: Any) -> dict:
    pid = os.getpid() if pid is None else int(pid)
    return {"pid": pid, "start_ticks": start_ticks(pid), "started_at": time.time(), **extra}


def identity_alive(ident: dict | None) -> bool:
    """The very process `ident` names is still running (same pid AND same start time; zombies are dead)."""
    if not isinstance(ident, dict) or not isinstance(ident.get("pid"), int):
        return False
    f = _stat_fields(ident["pid"])
    if not f or f[0] in ("Z", "X"):
        return False
    try:
        return ident.get("start_ticks") is None or int(f[19]) == int(ident["start_ticks"])
    except (IndexError, ValueError, TypeError):
        return False


# -- paths -------------------------------------------------------------------------------------------------------

def lock_path(home: Path | str, name: str) -> Path:
    return Path(home) / "locks" / f"{name}.lock"


def control_lock(home: Path | str) -> Path:
    return lock_path(home, "control")


def runner_lock(home: Path | str, job_id: str) -> Path:
    return lock_path(home, f"runner-{job_id}")


def control_file(home: Path | str) -> Path:
    return Path(home) / "control.json"


def runner_file(home: Path | str, job_id: str) -> Path:
    return Path(home) / "runners" / f"{job_id}.json"


def _starting_file(home: Path | str, job_id: str) -> Path:
    return Path(home) / "runners" / f"{job_id}.starting"


# -- locks -------------------------------------------------------------------------------------------------------

class Held:
    """An flock held on `path` until `release()` or process exit (the fd is close-on-exec: an execv drops it and the
    new image takes it again)."""

    def __init__(self, path: Path, fh):
        self.path = path
        self.fh = fh

    def release(self) -> None:
        if self.fh is None:
            return
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()
            self.fh = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


def acquire(path: Path | str) -> Held | None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return Held(path, fh)


def is_held(path: Path | str) -> bool:
    """Someone holds the lock (probed with a separate open file, so it also sees a holder in this process)."""
    path = Path(path)
    if not path.exists():
        return False
    try:
        fh = open(path, "a+")
    except OSError:
        return False
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    finally:
        fh.close()


# -- small JSON files --------------------------------------------------------------------------------------------

def write_json(path: Path | str, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")  # unique per writer thread
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def read_json(path: Path | str) -> dict | None:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def beat(home: Path | str, now: float | None = None) -> None:
    """Control's heartbeat: `beat_at` in control.json (never raises)."""
    try:
        path = control_file(home)
        data = read_json(path) or {}
        data["beat_at"] = time.time() if now is None else now
        write_json(path, data)
    except OSError:
        pass


def mark_starting(home: Path | str, job_id: str, pane: str | None, now: float | None = None) -> None:
    write_json(_starting_file(home, job_id), {"pane": pane, "at": time.time() if now is None else now})


def starting(home: Path | str, job_id: str, now: float | None = None, grace_s: float = STARTING_GRACE_S) -> dict | None:
    """A launch of `job_id`'s runner is in progress: marked less than `grace_s` ago and not yet completed (a runner
    that took its lock after the mark has written a newer identity: that start is over, whatever happened since)."""
    rec = read_json(_starting_file(home, job_id))
    now = time.time() if now is None else now
    if not (rec and isinstance(rec.get("at"), (int, float)) and 0 <= now - rec["at"] < grace_s):
        return None
    ident = read_json(runner_file(home, job_id)) or {}
    if isinstance(ident.get("started_at"), (int, float)) and ident["started_at"] >= rec["at"]:
        return None
    return rec


# -- legacy (lock-less) processes ---------------------------------------------------------------------------------

def _same_home(env_home: str | None, home: Path) -> bool:
    from steward.store import DEFAULT_HOME

    try:
        return Path(env_home or DEFAULT_HOME).resolve() == home.resolve()
    except OSError:
        return False


def find_processes(pattern: str, home: Path | str, *, exclude: tuple[str, ...] = ()) -> list[int]:
    """Pids of live processes whose cmdline contains `pattern` (and none of `exclude`) and whose STEWARD_HOME is
    `home`. Own-user processes only (others' environ is unreadable and skipped)."""
    home = Path(home)
    out = []
    me = os.getpid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if pattern not in cmd or any(x in cmd for x in exclude):
            continue
        if proc_state(int(entry.name)) in ("Z", "X", None):
            continue
        try:
            env = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        env_home = next((e.split(b"=", 1)[1].decode(errors="replace") for e in env if e.startswith(b"STEWARD_HOME=")),
                        None)
        if _same_home(env_home, home):
            out.append(int(entry.name))
    return out


def control_processes(home: Path | str) -> list[int]:
    return find_processes("steward.cli control", home, exclude=("--ensure",))


def runner_processes(home: Path | str, job_id: str) -> list[int]:
    return find_processes(f"steward.cli run {job_id}", home)


# -- confirmed stop ----------------------------------------------------------------------------------------------

def stop_process(ident: dict | None, *, sleep: Callable[[float], Any] = time.sleep, wait_s: float = 10.0,
                 is_alive: Callable[[dict | None], bool] = identity_alive, kill: Callable[[int, int], Any] = os.kill,
                 poll_s: float = 0.2) -> bool:
    """SIGINT, then SIGTERM, then SIGKILL the process `ident` names, waiting up to `wait_s` after each. True only
    once it is confirmed gone (a recycled pid is never signalled: identity is rechecked before every signal)."""
    if isinstance(ident, dict) and ident.get("pid") == os.getpid():
        return False  # never signal ourselves
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        if not is_alive(ident):
            return True
        try:
            kill(int(ident["pid"]), sig)
        except ProcessLookupError:
            return True
        except (PermissionError, OSError):
            return False
        waited = 0.0
        while waited < wait_s:
            if not is_alive(ident):
                return True
            sleep(poll_s)
            waited += poll_s
    return not is_alive(ident)
