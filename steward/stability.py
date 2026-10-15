"""Stability and accuracy checks: each one is `detection -> route`, all deterministic code.

* finish line   A "done" claim is checked against the goal's own finish check command
                (`check_finish.py`, "FINISH LINE: ... exits 0") run in the worktree, bounded, read-only. Fail ->
                `finish_line_not_met` + ONE nudge, job keeps RUNNING; after `MAX_FINISH_BOUNCES` -> NEEDS_HUMAN.
                A check that times out or cannot run is `finish_check_unverified` (v2.6, SUP-01): no bounce,
                and the real verification still runs for the candidate.
                No runnable check -> the prose finish line is recorded as unproven (shadow only).
* runner code   The runner records when it started and the repo HEAD it imported; the pane warns when steward/
                has newer commits than that.
* spend shape   A watcher costing > 2x the worker (min $0.25) gets its cadence halved (max 2, reversible);
                a job burning > $5/h warns and asks for you once.

Nothing here steers the worker beyond the one nudge text, and nothing here writes outside the store.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from steward import approvals as approvals_module
from steward.diagnosis import redact
from steward.models import Judgment
from steward.store import Store

REPO_ROOT = Path(__file__).resolve().parent.parent
# The live checkout. A process restored from a pinned deployed tree (`steward.deployed`, issue W) runs from REPO_ROOT =
# that tree, but compares its code with, reports, and self-restarts onto the live checkout.
LIVE_ROOT = Path(os.environ.get("STEWARD_LIVE_ROOT") or REPO_ROOT)

MAX_FINISH_BOUNCES = 3
FINISH_TIMEOUT_S = 120
FINISH_BOUNCES_KEY = "finish_bounces"
FINISH_UNPROVEN_KEY = "finish_line_unproven"
RUNNER_CODE_KEY = "runner_code"

OVERSPEND_RATIO = 2.0
OVERSPEND_MIN_USD = 0.25
RATE_LIMIT_USD_PER_H = 5.0
SPEND_CHECK_EVERY_S = 60.0


# -- finish line ----------------------------------------------------------------------------------------

_FINISH_RE = re.compile(r"finish[ -]?line\s*:?", re.I)
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_SCRIPT_RE = re.compile(r"(?<![\w./-])((?:\./|[\w.-]+/)*[\w.-]*(?:check|finish|verify)[\w.-]*\.(?:py|sh))\b", re.I)
_RUNNERS = ("python", "python3", "pytest", "bash", "sh", "make", "node", "npm", "uv", "cargo", "go")
_UNSAFE = re.compile(r"[;|&<>$`\\\n]|\brm\b|\bsudo\b")
_FAIL_LINE = re.compile(r"(fail|error|assert|not met|missing|✗|✘|traceback|exception|expected)", re.I)


def _finish_text(goal: str) -> str:
    m = _FINISH_RE.search(goal or "")
    return goal[m.end():] if m else (goal or "")


def has_prose_finish_line(goal: str) -> bool:
    return bool(_FINISH_RE.search(goal or ""))


def _command_ok(cmd: str, worktree: Path) -> bool:
    cmd = cmd.strip()
    if not cmd or _UNSAFE.search(cmd):
        return False
    try:
        words = shlex.split(cmd)
    except ValueError:
        return False
    if not words:
        return False
    first = words[0]
    if first.startswith("./"):
        return (worktree / first).is_file()
    return os.path.basename(first).split("-")[0] in _RUNNERS or first in _RUNNERS


def finish_check_command(goal: str, worktree: Path | str) -> str | None:
    """The finish check command a goal names, or None. Two shapes: a backticked command
    (`python3 check_finish.py`, `pytest -q tests/x.py`) or a script name containing check/finish/verify
    that exists in the worktree. Anything with shell metacharacters is never run."""
    worktree = Path(worktree)
    text = _finish_text(goal)
    for m in _BACKTICK_RE.finditer(text):
        cand = m.group(1).strip()
        if _command_ok(cand, worktree):
            words = shlex.split(cand)
            files = [w for w in words[1:] if w.endswith((".py", ".sh"))]
            if (len(words) > 1 or words[0].startswith("./")) and all((worktree / f).is_file() for f in files):
                return cand
        m2 = _SCRIPT_RE.search(cand)
        if m2 and " " not in cand and (worktree / m2.group(1)).is_file():
            return _script_command(m2.group(1))
    for m in _SCRIPT_RE.finditer(text):
        if (worktree / m.group(1)).is_file():
            return _script_command(m.group(1))
    return None


def _script_command(script: str) -> str:
    if script.endswith(".sh"):
        return f"bash {shlex.quote(script)}"
    return f"python3 {shlex.quote(script)}"


@dataclass(frozen=True)
class FinishResult:
    runnable: bool
    ok: bool
    exit_code: int | None
    tail: str
    first_failing_line: str
    # SUP-01 (v2.6): the check could not finish (timed out) or could not run at all. That proves nothing either
    # way: it is "unverified", never "not met", and the real verification (verify_worker + closeout) decides.
    unverified: bool = False
    reason: str = ""


def first_failing_line(output: str) -> str:
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if not lines:
        return "the finish check failed with no output"
    hit = next((ln for ln in lines if _FAIL_LINE.search(ln)), lines[-1])
    return redact(hit)[:160]


def run_finish_check(runner_exec, worktree: Path | str, command: str) -> FinishResult:
    """Run the finish check in the worktree (bounded to FINISH_TIMEOUT_S). `runnable=False` when the command
    itself cannot run (not found / not executable): that is a broken check, not an unmet finish line."""
    try:
        res = runner_exec.shell_line(command, cwd=worktree, timeout=FINISH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        why = f"finish check timed out after {FINISH_TIMEOUT_S}s"
        return FinishResult(True, False, None, f"timed out after {FINISH_TIMEOUT_S}s", why, unverified=True, reason=why)
    except OSError as exc:
        return FinishResult(False, False, None, str(exc)[:300], str(exc)[:160], unverified=True,
                            reason=f"finish check cannot run: {str(exc)[:160]}")
    out = redact(((res.stdout or "") + "\n" + (res.stderr or "")).strip())
    tail = "\n".join(out.splitlines()[-12:])[-1200:]
    if res.exit_code in (126, 127):
        return FinishResult(False, False, res.exit_code, tail, first_failing_line(out), unverified=True,
                            reason=f"finish check cannot run (exit {res.exit_code}): {first_failing_line(out)}")
    return FinishResult(True, res.exit_code == 0, res.exit_code, tail, first_failing_line(out))


_DONTS_RE = re.compile(r"\bdon[\u2019']?ts?\b\s*:?", re.I)
_OPT_WITH_VALUE = {"-k", "-m", "-p", "-c", "-n", "-o", "-W", "--maxfail", "--rootdir", "--deselect", "--ignore",
                   "--junitxml", "--timeout", "--durations", "-x"}
_SUITE_RUNNERS = ("npm test", "npm run test", "make test", "make check", "cargo test", "go test ./...", "tox", "nox")


def is_full_suite(command: str) -> bool:
    """A bare suite run: pytest with no test path / selector, or a project-wide test target."""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if any(" ".join(words).startswith(r) for r in _SUITE_RUNNERS):
        return True
    idx = next((i for i, w in enumerate(words) if os.path.basename(w) == "pytest"), None)
    if idx is None:
        return False
    rest, positional, skip = words[idx + 1:], [], False
    for w in rest:
        if skip:
            skip = False
            continue
        if w.startswith(("-k", "-m")) and not w.startswith("--"):
            return False  # attached selector forms: -k=expr, -kexpr, -m=expr
        if w in _OPT_WITH_VALUE and w != "-x":
            if w in ("-k", "-m"):
                return False  # a selector narrows the run
            skip = True
            continue
        if not w.startswith("-"):
            positional.append(w)
    return all(p.rstrip("/") == "tests" for p in positional)  # the project's whole tests dir is the full suite


def _picked_from_donts(goal: str, command: str) -> bool:
    text = _finish_text(goal)
    script = command.split()[-1]
    at = text.find(command) if command in text else text.find(script)
    if at < 0:
        return False
    m = _DONTS_RE.search(text[:at])
    return m is not None


def finish_check_warnings(goal: str, repo: Path | str, *, predicted_s: float | None = None) -> list[str]:
    """SUP-03 (v2.6): what `steward new` warns about the finish check it will run at every done claim (a receipt
    line, never a refusal): a bare full-suite run, a check predicted to exceed FINISH_TIMEOUT_S, or a command
    picked from the Don'ts text. Job 14305aeb's finish line named the ~30-minute suite."""
    command = finish_check_command(goal, repo)
    if command is None:
        return []
    out: list[str] = []
    if is_full_suite(command):
        out.append(f"`{command}` is the full suite; the {FINISH_TIMEOUT_S}s finish check will time out and stay "
                   "unverified (name targeted tests in the finish line)")
    if predicted_s is not None and predicted_s > FINISH_TIMEOUT_S:
        out.append(f"`{command}` is predicted to take ~{int(predicted_s)}s, beyond the {FINISH_TIMEOUT_S}s "
                   "finish-check budget")
    if _picked_from_donts(goal, command):
        out.append(f"`{command}` was picked from the Don'ts text, not the finish line itself")
    return out


def predicted_check_s(store: Store, repo: Path | str) -> float | None:
    """The longest measured baseline/verification of the most recent job on the same repo, if any."""
    want = str(Path(repo).resolve()) if repo else ""
    for job in sorted(store.list_jobs(), key=lambda j: j.created_at, reverse=True)[:50]:
        try:
            same = str(Path(job.repo).resolve()) == want
        except OSError:
            same = False
        if not same:
            continue
        vals = [store.kv_get(job.job_id, k) for k in ("baseline_duration_s", "verify_duration_s")]
        vals = [float(v) for v in vals if isinstance(v, (int, float))]
        if vals:
            return max(vals)
    return None


def finish_bounces(store: Store, job_id: str) -> int:
    return int(store.kv_get(job_id, FINISH_BOUNCES_KEY, default=0) or 0)


def nudge_text(reason: str) -> str:
    return f"Finish line not met: {reason}. Keep going until it passes."


def prose_finish_line(goal: str) -> str:
    m = _FINISH_RE.search(goal or "")
    return " ".join(goal[m.end():].split())[:160] if m else ""


# -- idle after a final message -------------------------------------------------------------------------


def _epoch(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# -- runner code ----------------------------------------------------------------------------------------


def repo_head(root: Path | str = REPO_ROOT) -> str:
    try:
        p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


PROCESS_STARTED_AT = time.time()
PROCESS_HEAD = repo_head()  # the code this process imported: HEAD when it started


def record_runner_code(store: Store, job_id: str, *, head: str | None = None, started_at: float | None = None,
                       pid: int | None = None) -> dict:
    rec = {"started_at": PROCESS_STARTED_AT if started_at is None else started_at,
           "head": PROCESS_HEAD if head is None else head, "pid": os.getpid() if pid is None else pid}
    store.kv_set(job_id, RUNNER_CODE_KEY, rec)
    return rec


def commits_behind(loaded_head: str | None, root: Path | str = REPO_ROOT, *, head_fn=None, count_fn=None) -> int:
    """Commits touching steward/ between the sha a runner loaded and the repo's current HEAD (0 = current/unknown)."""
    if not loaded_head:
        return 0
    if count_fn is not None:
        return int(count_fn(loaded_head))
    try:
        p = subprocess.run(["git", "rev-list", "--count", f"{loaded_head}..HEAD", "--", "steward/"],
                           cwd=str(root), capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return 0
    try:
        return int(p.stdout.strip()) if p.returncode == 0 else 0
    except ValueError:
        return 0


def runner_outdated(store: Store, job_id: str, root: Path | str = LIVE_ROOT, *, count_fn=None) -> int:
    """N > 0 when the job's runner loaded steward/ code that has since gained N newer commits."""
    rec = store.kv_get(job_id, RUNNER_CODE_KEY)
    if not isinstance(rec, dict):
        return 0
    return commits_behind(rec.get("head"), root, count_fn=count_fn)


# -- spend shape ----------------------------------------------------------------------------------------


def spend_findings(tokens: dict) -> dict:
    """Pure: `{"overspenders": [(watcher, usd, worker_usd, ratio)], "rate_usd_h": float}` from `tokens.job_tokens`.
    Owned by the efficiency watcher now (steward.efficiency_watch); kept here as a wrapper."""
    from steward import efficiency_watch

    return efficiency_watch.spend_shape_findings(tokens)


def spend_warnings(tokens: dict | None) -> list[str]:
    """Pane lines: `⚠ <watcher> costs 2.3× worker`, `⚠ spend $6.20/h` (wrapper, see efficiency_watch)."""
    from steward import efficiency_watch

    return efficiency_watch.spend_shape_warnings(tokens)


def apply_spend_shape(store: Store, job_id: str, tokens: dict, notify_fn=None) -> list[str]:
    """Record + route the spend shape (wrapper, see efficiency_watch.apply_spend_shape)."""
    from steward import efficiency_watch

    return efficiency_watch.apply_spend_shape(store, job_id, tokens, notify_fn)


# -- what explain-state / the feed say ------------------------------------------------------------------


def explain_lines(store: Store, job_id: str, *, now: float | None = None, count_fn=None) -> list[str]:
    now = time.time() if now is None else now
    lines: list[str] = []
    n = finish_bounces(store, job_id)
    events = store.events(job_id)
    last = next((e for e in reversed(events) if e.what == "finish_line_not_met"), None)
    if n or last:
        why = f"; last: {last.evidence.get('first_failing_line')}" if last else ""
        lines.append(f"Finish line: not met {n}/{MAX_FINISH_BOUNCES} bounces{why}")
    elif store.kv_get(job_id, FINISH_UNPROVEN_KEY):
        lines.append("Finish line: prose only, no runnable check (shadow-scored only)")
    stop = next((e for e in reversed(events) if e.what == "yield_classified"), None)
    if stop and (stop.evidence or {}).get("lane") != "CANDIDATE":
        ev = stop.evidence or {}
        lines.append(f"Last turn end: {ev.get('lane')} ({', '.join(ev.get('failed_preconditions') or []) or '-'}); "
                     "not a completion")
    from steward import closeout

    waiting = closeout.ruling_pending(store, job_id)
    if waiting:  # v2.7 Phase 41: green verification, one completion ruling outstanding
        sha = str((store.kv_get(job_id, "completion_ruling") or {}).get("sha") or "")[:12]
        lines.append(f"Completion: awaiting completion ruling at {sha} ({waiting})")
    rec = store.kv_get(job_id, RUNNER_CODE_KEY)
    if isinstance(rec, dict) and rec.get("head"):
        behind = commits_behind(rec["head"], count_fn=count_fn)
        state = f"OUTDATED ({behind} commits behind; `steward runner-restart {job_id[:8]}`)" if behind else "current"
        lines.append(f"Runner code: {rec['head'][:7]} loaded {int(now - float(rec.get('started_at') or now))}s ago, {state}")
    broken = next((e for e in reversed(events) if e.what == "check_broken"), None)
    if broken:
        lines.append(f"Tests: unverified, check broken: {broken.evidence.get('reason')} "
                     f"(`{broken.evidence.get('command')}` exit {broken.evidence.get('exit')})")
    halved = [e for e in events if e.what == "watcher_cadence_halved"]
    if halved:
        h = halved[-1].evidence
        lines.append(f"Spend: watcher {h.get('name')} cadence halved {h.get('halvings')}/{h.get('max')} "
                     f"(every {h.get('every_s')}s)")
    if any(e.what == "spend_rate_high" for e in events):
        lines.append("Spend: job exceeded the hourly rate limit once (you were asked)")
    from steward import check_runs
    queued = check_runs.status_line(store, job_id, now=now)
    if queued:
        lines.append(f"Checks: {queued}")
    return lines
