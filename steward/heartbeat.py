"""Heartbeat signals and deterministic classifier (SP-12).

Quiet is not stalled: `collect` gathers only observable evidence (worker
lifecycle, process liveness, git HEAD, diff fingerprint, repeated-error
fingerprinting, permission-prompt / known-running-process detection) and
`classify` turns that evidence into one of a small set of classes with a pure
function — no model call happens here, ever (SP-12, SP-21 caps model calls to
one diagnosis per suspect stall, handled in `steward.recovery`).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from steward.diagnosis import redact
from steward.runner_exec import Runner
from steward.worker import WorkerAdapter

_PERMISSION_PROMPT_RE = re.compile(
    r"(?i)\ballow\b|\bapprove\b|\[y/n\]|do you want to proceed"
)

# A permission-prompt card looks like (Claude; Codex's box differs cosmetically
# but shares the same header/footer shape):
#
#  Bash command
#    ls tests && python -m pytest -q 2>&1 | tail -40
#    List tests and run pytest baseline
#  This command requires approval
#  Do you want to proceed?
#  > 1. Yes
#
# `extract_permission_request` pulls just the command/description lines
# between the tool header and the footer, for the approval question a human
# actually reads (SP-19) — never the whole pane tail.
_PERMISSION_HEADER_RE = re.compile(r"(?i)^(?:bash command|\S.*\bcommand)$")
_PERMISSION_FOOTER_RE = re.compile(r"(?i)^(?:this command requires approval|do you want to proceed\??)$")
_LEADING_BOX_RE = re.compile(r"^[\s│┃|╭╮╰╯─━┌┐└┘❯›]+")
_TRAILING_BOX_RE = re.compile(r"[\s│┃|╭╮╰╯─━┌┐└┘❯›]+$")
_PERMISSION_REQUEST_FALLBACK = "worker requests permission (see pane)"
_PERMISSION_REQUEST_CAP = 300


def _strip_box_chars(line: str) -> str:
    """Strip box-drawing/pointer chars and whitespace from a line's edges only
    — never from the middle, so a command containing e.g. `2>&1` survives."""
    return _TRAILING_BOX_RE.sub("", _LEADING_BOX_RE.sub("", line))


# Codex cards. MCP-app tool:
#
#  Allow Stew to run tool "stew.get_recent_events"?
#  {"job_id": "6369d958", "limit": 20}
#  1. Allow
#  2. Allow for this session
#  3. Always allow
#  4. Cancel
#  enter to submit | esc to cancel
#
# and shell command approval:
#
#  Would you like to run the following command?
#  Reason: needs network
#  $ git fetch origin
#  1. Yes, proceed (y)
#  2. Yes, and don't ask again for commands that start with `git fetch` (p)
#  3. No, and tell Codex what to do differently (esc)
_CODEX_MCP_HEADER_RE = re.compile(r'(?i)^allow\s+.+?\s+to\s+run\s+tool\s+["\u201c`\']?([^"\u201d`\'?\s]+)["\u201d`\']?\s*\?$')
_CODEX_SHELL_HEADER_RE = re.compile(r"(?i)^would you like to run the following command\??$")
_CODEX_OPTION_RE = re.compile(r"^(?:\d+\.\s|enter to submit\b)", re.I)
_CODEX_SESSION_OPTION_RE = re.compile(r"(?i)^\d+\.\s*allow for this session\b")
_PANE_TAIL_LINES = 15
_PANE_TAIL_CAP = 1200


def _codex_card(lines: list[str]) -> tuple[int, str, str, list[str]] | None:
    """The LAST Codex card in `lines`: `(header_idx, kind, tool, body)` (`kind` is `mcp` or `shell`)."""
    for i in range(len(lines) - 1, -1, -1):
        m = _CODEX_MCP_HEADER_RE.match(lines[i])
        kind = "mcp" if m else ("shell" if _CODEX_SHELL_HEADER_RE.match(lines[i]) else "")
        if not kind:
            continue
        body: list[str] = []
        for line in lines[i + 1 :]:
            if _CODEX_OPTION_RE.match(line):
                break
            if line:
                body.append(line)
        return i, kind, (m.group(1) if m else ""), body
    return None


def codex_mcp_card(output_tail: str) -> tuple[str, bool] | None:
    """`(tool, offers_allow_for_session)` for the live Codex MCP-app card at the bottom of the pane, else None."""
    lines = [_strip_box_chars(line) for line in output_tail.splitlines()]
    card = _codex_card(lines)
    if card is None or card[1] != "mcp":
        return None
    session = any(_CODEX_SESSION_OPTION_RE.match(line) for line in lines[card[0] + 1 :])
    return card[2], session


def _pane_tail(lines: list[str]) -> str:
    tail = [line for line in lines if line][-_PANE_TAIL_LINES:]
    if not tail:
        return _PERMISSION_REQUEST_FALLBACK
    return (_PERMISSION_REQUEST_FALLBACK + "; unparsed pane tail:\n" + redact("\n".join(tail)))[:_PANE_TAIL_CAP]


def extract_permission_request(output_tail: str) -> str:
    """Pull the requested command/description out of a permission-prompt
    card in the worker's output tail, redacted and capped (SP-19, SP-24).

    Understands Claude's `Bash command ... Do you want to proceed?` card and
    Codex's MCP-app (`Allow <App> to run tool "x"?`) and shell-command cards.
    When nothing is recognisable it returns the generic message plus the
    redacted, capped last ~15 pane lines, so a resolver or tech lead can still
    judge the prompt instead of seeing nothing.
    """
    lines = [_strip_box_chars(line) for line in output_tail.splitlines()]
    codex = _codex_card(lines)

    # Use the LAST card: earlier (answered) prompts remain in scrollback.
    footer_idx = next(
        (i for i in range(len(lines) - 1, -1, -1) if _PERMISSION_FOOTER_RE.match(lines[i])), None
    )
    if codex is not None and (footer_idx is None or codex[0] > footer_idx):
        _, kind, tool, body = codex
        if kind == "mcp":
            args = " ".join(body)
            return redact(f"{tool} {args}".strip())[:_PERMISSION_REQUEST_CAP]
        body = [line[2:] if line.startswith("$ ") else line for line in body]
        if body:
            return redact("\n".join(body))[:_PERMISSION_REQUEST_CAP]
        return _pane_tail(lines)
    if footer_idx is None:
        return _pane_tail(lines)
    # "This command requires approval" and "Do you want to proceed?" are both
    # footers; step to the first of a consecutive footer run.
    while footer_idx > 0 and _PERMISSION_FOOTER_RE.match(lines[footer_idx - 1]):
        footer_idx -= 1
    header_idx = next(
        (i for i in range(footer_idx - 1, -1, -1) if _PERMISSION_HEADER_RE.match(lines[i])), None
    )
    if header_idx is None:
        return _pane_tail(lines)

    body = [line for line in lines[header_idx + 1 : footer_idx] if line]
    if not body:
        return _pane_tail(lines)

    return redact("\n".join(body))[:_PERMISSION_REQUEST_CAP]

# (name, pattern) — a known long-running check tool actively producing output,
# checked only when there is no permission prompt in the same tail.
_RUNNING_PROCESS_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("pytest", re.compile(r"test session starts|collecting \d+ item")),
    ("npm", re.compile(r"\bnpm (?:run|test)\b")),
    ("make", re.compile(r"^make(?:\[\d+\])?:\s", re.MULTILINE)),
]

_ERROR_LINE_RE = re.compile(r"^.*(Traceback|FAILED|Error:).*$", re.MULTILINE)
_DIGITS_RE = re.compile(r"\d+")


class Class(str, Enum):
    progressing = "progressing"
    waiting = "waiting"
    quiet_known_process = "quiet_known_process"
    suspect_stall = "suspect_stall"
    dead = "dead"
    idle = "idle"


@dataclass(frozen=True)
class HeartbeatPolicy:
    long_process_s: float = 1800
    stall_s: float = 600


@dataclass(frozen=True)
class Signals:
    lifecycle: str
    pid_alive: bool | None
    head_sha: str
    diff_fingerprint: str
    last_check_exit: int | None
    error_fingerprint: str
    same_error_count: int
    permission_prompt: bool
    running_process: str | None
    since_last_change_s: float
    output_tail: str = ""
    # Typed-frame path only (WRK-03): `True` while a fabric worker's turn is
    # open (an activity/brief/steer has been sent and no `done`/`error` frame
    # has arrived yet) — the typed equivalent of `running_process`, since a
    # `FabricWorker` never has pane text to pattern-match. Defaults `False`
    # for every adapter that predates the fabric, so it never changes
    # `classify()`'s result for a `Signals` built directly (`_sig()` in
    # tests) or for `HerdrWorker`/`FakeWorker` observations.
    turn_open: bool = False
    # Frames observed this tick (empty for every non-fabric adapter — see
    # `steward.worker.WorkerObservation.events`), forwarded so
    # `steward.runner` can consume them without a second `worker.observe()`
    # call.
    events: tuple = ()


def _fingerprint_error(output_tail: str) -> str:
    lines = [m.group(0) for m in _ERROR_LINE_RE.finditer(output_tail)]
    if not lines:
        return ""
    normalized = _DIGITS_RE.sub("", lines[-1].strip())
    return hashlib.sha1(normalized.encode()).hexdigest()


def _detect_permission_prompt(observation_lifecycle: str, output_tail: str) -> bool:
    # Herdr's own lifecycle is authoritative when known: a prompt already
    # answered stays in scrollback, so text matching over the whole tail keeps
    # "seeing" it (live run 2026-09-26: phantom approval + false break-glass).
    if observation_lifecycle != "unknown":
        return observation_lifecycle == "blocked"
    # Unknown lifecycle: only the live bottom of the screen counts.
    return bool(_PERMISSION_PROMPT_RE.search("\n".join(output_tail.splitlines()[-15:])))

def _detect_running_process(output_tail: str, permission_prompt: bool) -> str | None:
    if permission_prompt:
        return None
    for name, pattern in _RUNNING_PROCESS_PATTERNS:
        if pattern.search(output_tail):
            return name
    return None


def collect(
    worker: WorkerAdapter,
    worktree: Path,
    runner: Runner,
    prev: Signals | None,
    now: float,
    *,
    last_change_at: float | None = None,
    last_check_exit: int | None = None,
) -> tuple[Signals, float]:
    """Collect one heartbeat's worth of deterministic evidence.

    Returns `(signals, new_last_change_at)` — callers persist
    `new_last_change_at` and feed it back in as `last_change_at` on the next
    call so `since_last_change_s` is computed from a real elapsed clock, not
    re-derived every tick.
    """
    observation = worker.observe()

    head_result = runner.run(["git", "rev-parse", "HEAD"], cwd=worktree)
    head_sha = (
        head_result.stdout.strip()
        if head_result.exit_code == 0
        else (prev.head_sha if prev else "")
    )

    diff_result = runner.run(["git", "diff"], cwd=worktree)
    status_result = runner.run(["git", "status", "--porcelain"], cwd=worktree)
    untracked = sorted(
        line[3:].strip()
        for line in status_result.stdout.splitlines()
        if line.startswith("??")
    )
    diff_blob = (diff_result.stdout if diff_result.exit_code == 0 else "")
    diff_blob += "\n" + "\n".join(untracked)
    diff_fingerprint = hashlib.sha1(diff_blob.encode()).hexdigest()

    permission_prompt = _detect_permission_prompt(observation.lifecycle, observation.output_tail)
    running_process = _detect_running_process(observation.output_tail, permission_prompt)
    error_fingerprint = _fingerprint_error(observation.output_tail)
    turn_open = observation.lifecycle == "working"

    prev_error_fp = prev.error_fingerprint if prev else ""
    prev_same_error_count = prev.same_error_count if prev else 0
    if error_fingerprint and error_fingerprint == prev_error_fp:
        same_error_count = prev_same_error_count + 1
    elif error_fingerprint:
        same_error_count = 1
    else:
        same_error_count = 0

    new_error = bool(error_fingerprint) and error_fingerprint != prev_error_fp
    changed = (
        prev is None
        or head_sha != prev.head_sha
        or diff_fingerprint != prev.diff_fingerprint
        or new_error
    )

    if changed:
        new_last_change_at = now
    elif last_change_at is not None:
        new_last_change_at = last_change_at
    else:
        new_last_change_at = now
    since_last_change_s = max(0.0, now - new_last_change_at)

    if last_check_exit is None and prev is not None:
        last_check_exit = prev.last_check_exit

    sig = Signals(
        lifecycle=observation.lifecycle,
        pid_alive=observation.pid_alive,
        head_sha=head_sha,
        diff_fingerprint=diff_fingerprint,
        last_check_exit=last_check_exit,
        error_fingerprint=error_fingerprint,
        same_error_count=same_error_count,
        permission_prompt=permission_prompt,
        running_process=running_process,
        since_last_change_s=since_last_change_s,
        output_tail=observation.output_tail,
        turn_open=turn_open,
        events=tuple(observation.events),
    )
    return sig, new_last_change_at


def classify(sig: Signals, policy: HeartbeatPolicy | None = None) -> Class:
    """Pure classifier — table-driven, no I/O, no model call (SP-12)."""
    policy = policy or HeartbeatPolicy()

    if sig.lifecycle == "exited" or sig.pid_alive is False:
        return Class.dead
    if sig.permission_prompt or sig.lifecycle == "blocked":
        return Class.waiting
    if sig.since_last_change_s == 0.0:
        return Class.progressing
    if (sig.running_process is not None or sig.turn_open) and sig.since_last_change_s < policy.long_process_s:
        return Class.quiet_known_process
    if sig.same_error_count >= 3 or sig.since_last_change_s >= policy.stall_s:
        return Class.suspect_stall
    return Class.idle
