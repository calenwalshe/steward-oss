"""Injectable command runner (SP-1).

Every external process — git, pytest, npm, make, whatever discovery finds —
goes through this seam. `SubprocessRunner` is the only place in the codebase
allowed to call `subprocess.run`; every caller (discovery-driven baseline
runs, worktree creation, later phases) takes a `Runner` so tests can swap in
`FakeRunner` and never touch a real process or the network.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from steward import gitenv


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float


class Runner(Protocol):
    def run(
        self,
        argv: list[str],
        cwd: str | Path,
        *,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult: ...

    def shell_line(
        self,
        line: str,
        cwd: str | Path,
        *,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult: ...


class SubprocessRunner:
    """Runs real subprocesses. Never uses `shell=True` directly."""

    def run(
        self,
        argv: list[str],
        cwd: str | Path,
        *,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        start = time.monotonic()
        # TODO(xdist): on timeout only the bash child is killed; xdist workers it spawned can outlive it and hold
        # host worker capacity until they exit. Killing the process group needs start_new_session=True, which would
        # also hide the children from check_runs' owner-group kill (KILL_GRACE_S), so it is left for its own change.
        proc = subprocess.run(
            list(argv),
            cwd=str(cwd),
            timeout=timeout,
            env=gitenv.merged(env),
            capture_output=True,
            text=True,
        )
        duration = time.monotonic() - start
        return CommandResult(
            argv=list(argv),
            cwd=str(cwd),
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_s=duration,
        )

    def shell_line(
        self,
        line: str,
        cwd: str | Path,
        *,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Run a single *discovered check* line via `bash -lc`.

        This is the only sanctioned use of a shell in Steward: `line` must
        come from discovery classification (`kind == "check"`), never from
        unvalidated user/model input (SP-7).
        """
        return self.run(["bash", "-lc", line], cwd, timeout=timeout, env=env)


class FakeRunner:
    """Scripted runner for tests. Records every call; never spawns a process."""

    def __init__(
        self,
        responses: dict[str, CommandResult]
        | Callable[[list[str], str], CommandResult]
        | None = None,
    ):
        self.responses = responses if responses is not None else {}
        self.calls: list[dict] = []

    def run(
        self,
        argv: list[str],
        cwd: str | Path,
        *,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        cwd_s = str(cwd)
        argv_l = list(argv)
        self.calls.append(
            {"argv": argv_l, "cwd": cwd_s, "timeout": timeout, "env": env}
        )
        if callable(self.responses):
            return self.responses(argv_l, cwd_s)
        key = " ".join(argv_l)
        if key in self.responses:
            return self.responses[key]
        return CommandResult(
            argv=argv_l, cwd=cwd_s, exit_code=0, stdout="", stderr="", duration_s=0.0
        )

    def shell_line(
        self,
        line: str,
        cwd: str | Path,
        *,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        return self.run(["bash", "-lc", line], cwd, timeout=timeout, env=env)
