"""A stand-in for `python -m steward.cli control ...` / `python -m steward.cli run JOB ...` in the watchdog sandbox
(tests/test_watchdog_sandbox.py). Invoked as `python stubproc.py steward.cli <control|run JOB> ...`, so its cmdline
has the real shape the watchdog's legacy-process scan looks for. It takes the REAL `steward.proclock` lock exactly
like `steward.cli._cmd_run` / `_cmd_control` (exit 3 when held, before doing anything else), writes the real identity
file, and only then logs itself as started. Control beats every 0.2 s unless `<home>/stub-wedge` names its pid."""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # the repo under test (not the cwd)

from steward import proclock  # noqa: E402


def _log(home: Path, name: str, rec: dict) -> None:
    with (home / name).open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def main() -> int:
    home = Path(os.environ["STEWARD_HOME"])
    kind = sys.argv[2]
    job = sys.argv[3] if kind == "run" else None
    pane = sys.argv[sys.argv.index("--pane") + 1] if "--pane" in sys.argv else os.environ.get("STUB_PANE")
    lock = proclock.control_lock(home) if kind == "control" else proclock.runner_lock(home, job)
    held = proclock.acquire(lock)
    rec = {"kind": kind, "job": job, "pid": os.getpid(), "cwd": os.getcwd(), "pane": pane,
           "live_root": os.environ.get("STEWARD_LIVE_ROOT")}
    if held is None:
        _log(home, "stub-refused.log", rec)
        return 3
    ident = proclock.identity(pane=pane, head="stub", beat_at=time.time())
    if kind == "control":
        proclock.write_json(proclock.control_file(home), ident)
    else:
        proclock.write_json(proclock.runner_file(home, job), ident)
    _log(home, "stub-started.log", {**rec, "start_ticks": ident["start_ticks"]})
    wedge = home / "stub-wedge"
    try:
        while True:
            if kind == "control" and not (wedge.exists() and wedge.read_text().strip() == str(os.getpid())):
                proclock.beat(home)
            time.sleep(0.2)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
