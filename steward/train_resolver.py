# steward/train_resolver.py
"""Headless Opus conflict resolution inside the train worktree (2026-10-08 spec)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

PROMPT = """You are resolving git merge conflicts in this repository (a landing-train worktree).
The merge in progress lands Steward job {job_id} onto `{target}`.
Job goal: {goal}

Conflicted files:
{files}

Rules:
- Keep BOTH sides' intent. Never revert changes that are already on `{target}`.
- Golden fixtures (e.g. *.golden.json) are never hand-merged: take `{target}`'s version, then regenerate
  them by running the test that owns them (it rewrites the fixture when its regen env/flag is set; read the test).
- Never delete, skip or weaken a test or an assertion.
- When every conflict is resolved: `git add` the resolved files. Do NOT commit, do NOT run `git merge --abort`.
- Leave no conflict markers anywhere.
"""

ALLOWED = "Read Edit Bash(git add:*) Bash(git diff:*) Bash(git status:*) Bash(git show:*) Bash(python3 -m pytest:*)"


def resolve(worktree, job_id: str, files: list[str], *, goal: str, target: str, runner,
            timeout_s: float, model: str = "opus", tmp_dir=None) -> bool:
    prompt = PROMPT.format(job_id=job_id, target=target, goal=goal or "(no goal recorded)",
                           files="\n".join(f"- {f}" for f in files))
    argv = ["env", "-u", "ANTHROPIC_API_KEY", "claude", "-p", prompt, "--model", model,
            "--permission-mode", "acceptEdits", "--allowedTools", ALLOWED]
    kw = {}
    if tmp_dir is not None:                    # keep claude's scratch under the merge root, not the /tmp tmpfs
        Path(tmp_dir).mkdir(parents=True, exist_ok=True)
        kw["env"] = {**os.environ, "TMPDIR": str(tmp_dir)}
    try:
        res = runner.run(argv, str(worktree), timeout=timeout_s, **kw)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return res.exit_code == 0


def has_markers(worktree, files: list[str]) -> bool:
    if not files:
        return False
    for extra in ([], ["--cached"]):
        r = subprocess.run(["git", "grep", *extra, "-l", "-E", "^(<<<<<<<|>>>>>>>) ", "--", *files],
                           cwd=Path(worktree), capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return True
    return False
