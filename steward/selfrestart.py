"""Runner self-restart: a `steward run` (or `steward watchers --follow`) re-execs itself onto newer steward/ code.

`maybe_restart` is called only at a safe point between ticks (no tick running, no relay write in flight). When the
code this process loaded is behind master it (1) import-checks the new code in a subprocess, (2) records
`runner_self_restart` with old/new heads, then (3) `os.execv`s the original argv: same pid, same pane/tty. The new
process takes the ordinary resume path (`Runner.resume_all` -> attach to the live shim) and never touches the worker.
A broken import stays on the old code (`runner_self_restart_skipped`, the pane badge stays). While a Steward self-merge
awaits its deploy (`steward.deploy_pending`) nothing restarts: runners move with the control pane. Attempts are rate-limited
per job across restarts (the stamp lives in the store: exec wipes memory). `STEWARD_RUNNER_AUTORESTART=0` disables."""
from __future__ import annotations

import os
import subprocess
import shutil
import sys
import time
from pathlib import Path
from typing import Callable

from steward import deploy_pending, restart_guard, stability
from steward.models import Judgment
from steward.store import Store

ENV_SWITCH = "STEWARD_RUNNER_AUTORESTART"
MIN_INTERVAL_S = 300.0
STAMP_KEY = "runner_self_restart_at"
IMPORT_CHECK = "import steward.cli, steward.runner"


def enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(ENV_SWITCH, "1")).strip().lower() not in {"0", "false", "no", "off"}


def import_check(root: Path | str = stability.LIVE_ROOT, *, timeout: float = 60.0) -> str | None:
    """Import the new code in a fresh interpreter. None = clean, else the error text. Default: the live checkout, which
    is what a re-exec loads (a process restored from a pinned deployed tree re-execs there, issue W)."""
    try:
        p = subprocess.run([sys.executable, "-c", IMPORT_CHECK], cwd=str(root), capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{type(exc).__name__}: {exc}"
    if p.returncode == 0:
        return None
    return (p.stderr.strip().splitlines() or [f"exit {p.returncode}"])[-1][:300]


def original_argv() -> list[str]:
    """The interpreter command line this process was started with (`python -m steward ...` survives)."""
    orig = getattr(sys, "orig_argv", None)
    argv = list(orig) if orig else [sys.executable, *sys.argv]
    # os.execv does no PATH lookup: a process started as bare `python3 -m ...` (the status pane) must re-exec
    # through the absolute interpreter, or the re-exec raises FileNotFoundError and the pane dies.
    if argv and not os.path.isabs(argv[0]):
        argv[0] = sys.executable or shutil.which(argv[0]) or argv[0]
    return argv


class SelfRestarter:
    def __init__(self, store: Store, *, behind_fn: Callable[[str], int] | None = None,
                 head_fn: Callable[[], str] = lambda: stability.repo_head(stability.LIVE_ROOT), exec_fn: Callable | None = None,
                 import_check_fn: Callable[[], str | None] = import_check, clock: Callable[[], float] = time.time,
                 argv: list[str] | None = None, env: dict | None = None, min_interval_s: float = MIN_INTERVAL_S,
                 flush_fn: Callable[[], None] | None = None, stamp_key: str = STAMP_KEY,
                 sleep_fn: Callable[[float], None] = time.sleep):
        self.store = store
        self.behind_fn = behind_fn or (lambda job_id: stability.runner_outdated(store, job_id))
        self.head_fn = head_fn
        self.exec_fn = exec_fn or os.execv
        self.import_check_fn = import_check_fn
        self.clock = clock
        self.argv = argv
        self.env = env
        self.min_interval_s = min_interval_s
        self.flush_fn = flush_fn or _flush_std
        self.stamp_key = stamp_key
        self.sleep_fn = sleep_fn

    def maybe_restart(self, job_id: str) -> str:
        """One safe-point check. Returns why nothing happened, or "restarted" (only reachable with a fake exec)."""
        if not enabled(self.env):
            return "disabled"
        try:
            behind = self.behind_fn(job_id)
        except Exception:  # noqa: BLE001 - a git hiccup must never stop the runner
            return "current"
        if behind <= 0:
            return "current"
        if deploy_pending.pending(self.store) is not None:  # a Steward self-merge awaits its deploy (the owner's option B)
            return "awaiting_deploy"
        now = self.clock()
        last = self.store.kv_get(job_id, self.stamp_key)
        if isinstance(last, (int, float)) and 0 <= now - last < self.min_interval_s:
            return "rate_limited"
        if self.stamp_key == STAMP_KEY:  # a runner (not the watchers follower): the shared restart debounce
            refused = restart_guard.check(self.store, job_id, origin="self-restart", clock=self.clock,
                                          sleep=self.sleep_fn)
            if refused:
                return "debounced" if "ignored" in refused else "answer_in_flight"
        self.store.kv_set(job_id, self.stamp_key, now)  # stamp every attempt: a broken import is not re-checked each tick
        rec = self.store.kv_get(job_id, stability.RUNNER_CODE_KEY)
        old = (rec or {}).get("head") if isinstance(rec, dict) and self.stamp_key == STAMP_KEY else None
        old = old or stability.PROCESS_HEAD
        new = self.head_fn()
        err = self.import_check_fn()
        if err:
            self.store.append_event(job_id, what="runner_self_restart_skipped",
                                    evidence={"error": err, "old_head": old, "new_head": new, "behind": behind},
                                    rule="runner_self_restart", judgment=Judgment.code)
            return "skipped"
        argv = list(self.argv) if self.argv is not None else original_argv()
        self.store.append_event(job_id, what="runner_self_restart",
                                evidence={"old_head": old, "new_head": new, "behind": behind, "pid": os.getpid(),
                                          "argv": argv},
                                rule="runner_self_restart", judgment=Judgment.code)
        if self.stamp_key == STAMP_KEY:
            restart_guard.stamp(self.store, job_id, now)
        self.flush_fn()
        from steward import deployed

        deployed.prepare_reexec()  # a process restored from a pinned deployed tree moves back to the live checkout
        self.exec_fn(argv[0], argv)
        return "restarted"


def _flush_std() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.flush()
        except Exception:  # noqa: BLE001
            pass


def follower(store: Store) -> SelfRestarter:
    """For `steward watchers --follow`: it records no runner code, so staleness is PROCESS_HEAD vs the repo now."""
    return SelfRestarter(store, behind_fn=lambda _job: stability.commits_behind(stability.PROCESS_HEAD),
                         stamp_key="watchers_self_restart_at")
