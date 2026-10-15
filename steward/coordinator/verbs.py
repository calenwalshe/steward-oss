"""Verb builders, the daemon-side whitelist and the forced-origin runner.

Every Steward write the coordinator makes is a relayed CLI verb run as
``[sys.executable, "-m", "steward.cli", ...]`` with ``STEWARD_FORCED_ORIGIN=coordinator``.
``check_command`` is the first of two independent refusals (the second is ``coordinator_guard.guard``).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .. import proclock

ALLOWED_VERBS = ("steer", "send-back", "answer", "approve", "deny", "runner-restart", "stop", "up", "coordinator")
COORDINATOR_SUBS = ("hold", "episode")
STEER_VERBS = ("nudge", "focus", "avoid")
FORBIDDEN_FLAGS = ("--origin", "--urgent", "--then-accept", "--write-source", "--and-continue", "--done",
                   "--release", "--home", "--defer", "--model", "--goal-append")


@dataclass
class Verb:
    argv: list[str]
    label: str
    direct: bool = False


def _steer(verb: str, job: str, text: str) -> Verb:
    return Verb(["steer", job, text, "--verb", verb], verb)


def nudge(job: str, text: str) -> Verb:
    return _steer("nudge", job, text)


def focus(job: str, text: str) -> Verb:
    return _steer("focus", job, text)


def avoid(job: str, text: str) -> Verb:
    return _steer("avoid", job, text)


def send_back(job: str, text: str, workspace: str) -> Verb:
    return Verb(["send-back", job, text, "--workspace", workspace], "send-back")


def approve(job: str, aid: str) -> Verb:
    return Verb(["approve", job, aid], "approve")


def deny(job: str, aid: str) -> Verb:
    return Verb(["deny", job, aid], "deny")


def runner_restart(job: str) -> Verb:
    return Verb(["runner-restart", job], "runner-restart")


def stop(job: str) -> Verb:
    return Verb(["stop", job], "stop")


def answer_recommended(job: str, qids: list[str]) -> Verb:
    return Verb(["answer", job, "--accept-recommended", *qids], "answer")


def answer_option(job: str, qid: str, key: str, cite: str | None = None) -> Verb:
    return Verb(["answer", job, qid, "--option", key, *(["--note", cite] if cite else [])], "answer")


def answer_other(job: str, qid: str, text: str) -> Verb:
    return Verb(["answer", job, qid, "--other", text], "answer")


def up(workspace: str) -> Verb:
    return Verb(["up", "--workspace", workspace], "up", direct=True)


def hold(job: str, eid: str, trigger: str) -> Verb:
    return Verb(["coordinator", "hold", job, "--episode", eid, "--trigger", trigger], "hold")


def episode(job: str, eid: str, trigger: str, outcome: str, diagnosis: str, actions: list[str],
            rule_task: str | None = None, model_used: bool = False, ask: str | None = None) -> Verb:
    argv = ["coordinator", "episode", job, "--episode", eid, "--trigger", trigger, "--outcome", outcome,
            "--diagnosis", diagnosis]
    for a in actions:
        argv += ["--action", a]
    if rule_task:
        argv += ["--rule-task", rule_task]
    if model_used:
        argv.append("--model-used")
    if ask:
        argv += ["--ask", ask]
    return Verb(argv, "episode")


# Per-verb option allowlist (the gate). FORBIDDEN_FLAGS above is kept for documentation and tests.
# Any `--token` not listed for the verb is refused, which also closes argparse prefix abbreviations (`--urg`)
# and the `--k=v` forms. A positional message that itself begins with `--` is therefore refused.
_VALUE_OPTS = {
    "steer": ("--verb",),
    "send-back": ("--workspace",),
    "answer": ("--option", "--note", "--other"),
    "up": ("--workspace",),
    "coordinator hold": ("--episode", "--trigger"),
    "coordinator episode": ("--episode", "--trigger", "--outcome", "--diagnosis", "--action", "--rule-task", "--ask"),
}
_FLAG_OPTS = {"answer": ("--accept-recommended",), "coordinator episode": ("--model-used",)}
_REPEATABLE = ("--action",)


def _options(key: str, tokens: list[str]) -> tuple[dict[str, list[str]] | None, str | None]:
    """Parse `--k v` / `--k=v` / bare flags against the allowlist; (options, None) or (None, why)."""
    values, flags = _VALUE_OPTS.get(key, ()), _FLAG_OPTS.get(key, ())
    found: dict[str, list[str]] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if not tok.startswith("--"):
            continue
        name, eq, val = tok.partition("=")
        if name in flags:
            if eq:
                return None, f"flag {name} takes no value"
            found.setdefault(name, []).append("")
        elif name in values:
            if not eq:
                if i >= len(tokens) or tokens[i].startswith("--"):
                    return None, f"flag {name} needs a value"
                val, i = tokens[i], i + 1
            found.setdefault(name, []).append(val)
        else:
            return None, f"flag {tok} is not allowed for `{key}`"
        if len(found[name]) > 1 and name not in _REPEATABLE:
            return None, f"flag {name} may not be repeated"
    return found, None


def check_command(job_id: str | None, argv: list[str], *, workspace: str | None = None) -> str | None:
    """Why `argv` (the tokens after `steward`) is not allowed for the coordinator, else None."""
    if not argv or not all(isinstance(a, str) for a in argv):
        return "empty or malformed command"
    verb = argv[0]
    if verb not in ALLOWED_VERBS:
        return f"`{verb}` is not an allowed coordinator verb"
    if verb == "up":
        if len(argv) != 3 or argv[1] != "--workspace" or not argv[2] or argv[2].startswith("--"):
            return "up allows only --workspace W"
        if workspace is not None and argv[2] != workspace:
            return f"up --workspace must be {workspace}"
        return None
    if job_id is None:
        return f"`{verb}` needs a job id"
    if verb == "coordinator":
        if len(argv) < 3 or argv[1] not in COORDINATOR_SUBS:
            return "coordinator allows only hold and episode"
        if argv[2] != job_id:
            return f"`coordinator {argv[1]}` must name this job's full id ({job_id})"
        opts, why = _options(f"coordinator {argv[1]}", argv[3:])
        return why
    if len(argv) < 2 or argv[1] != job_id:
        return f"`{verb}` must name this job's full id ({job_id}) as its first argument"
    opts, why = _options(verb, argv[2:])
    if why:
        return why
    if verb == "steer":
        if "--verb" not in opts:
            return f"steer needs --verb ({', '.join(STEER_VERBS)})"
        if opts["--verb"][0] not in STEER_VERBS:
            return f"--verb must be one of {', '.join(STEER_VERBS)}"
    if verb == "answer" and any("cite:" not in v for v in opts.get("--other", [])):
        return "a free-text answer needs a `cite:` citation"
    return None


def workspace(home: Path, *, run=subprocess.run) -> str | None:
    """The Steward workspace: prefix of control.json's pane, else the first `herdr pane list` pane's workspace."""
    data = proclock.read_json(proclock.control_file(home)) or {}
    pane = data.get("pane")
    if isinstance(pane, str) and ":" in pane:
        return pane.split(":", 1)[0]
    try:
        res = run(["herdr", "pane", "list"], capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            return None
        panes = json.loads(res.stdout)["result"]["panes"]
        return next((p.get("workspace_id") for p in panes if p.get("workspace_id")), None) or \
            next((str(p.get("pane_id")).split(":", 1)[0] for p in panes if ":" in str(p.get("pane_id"))), None)
    except Exception:  # noqa: BLE001 - lookup is best effort
        return None


def run(verb: Verb, *, settings, workspace: str | None, run=subprocess.run, timeout: float = 180) -> dict:
    argv = list(verb.argv)
    job = None if argv[:1] == ["up"] else (argv[2] if argv[:1] == ["coordinator"] and len(argv) > 2
                                           else argv[1] if len(argv) > 1 else None)
    why = check_command(job, argv, workspace=workspace)
    full = [sys.executable, "-m", "steward.cli", *argv]
    if why:
        return {"ok": False, "rc": -1, "out": "", "err": f"refused: {why}", "argv": full}
    env = {**os.environ, "STEWARD_FORCED_ORIGIN": "coordinator", "STEWARD_HOME": str(settings.steward_home),
           "PYTHONPATH": str(settings.steward_repo)}
    try:
        res = run(full, cwd=settings.steward_repo, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": 124, "out": "", "err": f"timed out after {timeout}s", "argv": full}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "rc": -1, "out": "", "err": str(exc), "argv": full}
    return {"ok": res.returncode == 0, "rc": res.returncode, "out": res.stdout or "", "err": res.stderr or "",
            "argv": full}
