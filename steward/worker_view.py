"""Read-only view of a stuck worker for the approval resolver and the tech lead.

Principle (owner, 2026-10-05): read is never the limit, only acting is gated. Whoever has to judge a held
approval or a stalled job gets everything needed to see what the worker is doing: the live pane screen, the
worker's own transcript for BOTH runtimes (Claude session JSONL, Codex rollout), recent events and approvals,
and the worktree's git state. Everything is redacted (`diagnosis.redact` + literal key masking), capped, and
best-effort: a missing pane, transcript or repo yields an empty/"unavailable" section, never an exception.
Nothing here writes, sends keys or runs anything but `herdr pane read` and read-only `git`.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Callable

from steward import masking
from steward.chat import worktree_path
from steward.diagnosis import redact

PANE_LINES = 40
PANE_CAP = 4000
TURN_CAP = 400
GIT_CAP = 1500
CODEX_SCAN_DAYS = 4

Run = Callable[..., "subprocess.CompletedProcess"]


def _clean(text: object, cap: int) -> str:
    t = masking.mask(redact(str(text or "")))
    return t if len(t) <= cap else t[: cap - 1] + "…"


def _run(argv: list[str], cwd: str | Path | None = None, timeout: float = 8.0, run: Run = subprocess.run) -> str:
    try:
        r = run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def pane_screen(store, job_id: str, *, lines: int = PANE_LINES, run: Run = subprocess.run) -> str:
    """Redacted last `lines` lines of the worker's live pane (`herdr pane read`), or ''."""
    pane = store.kv_get(job_id, "herdr_pane")
    if not pane:
        return ""
    masking.arm_job(store, job_id)
    out = _run(["herdr", "pane", "read", str(pane), "--source", "recent-unwrapped", "--lines", str(lines)], run=run)
    tail = [ln.rstrip() for ln in out.splitlines()]
    while tail and not tail[-1]:
        tail.pop()
    text = "\n".join(tail[-lines:])
    return _clean(text, PANE_CAP) if text.strip() else ""


# -- Codex rollout ------------------------------------------------------------------------------


def codex_rollout(worktree: Path | str, sessions_dir: Path | None = None) -> Path | None:
    """Newest Codex rollout whose `session_meta.cwd` is the worktree (searches the last few days)."""
    base = sessions_dir if sessions_dir is not None else Path.home() / ".codex" / "sessions"
    want = str(Path(worktree))
    if not base.is_dir():
        return None
    files = sorted(base.glob("*/*/*/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    cutoff = time.time() - CODEX_SCAN_DAYS * 86400
    for p in files:
        if p.stat().st_mtime < cutoff:
            break
        try:
            with open(p, errors="replace") as fh:
                first = json.loads(fh.readline() or "{}")
        except (OSError, ValueError):
            continue
        if (first.get("payload") or {}).get("cwd") == want:
            return p
    return None


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return " ".join(str(c.get("text", "")) for c in (content or []) if isinstance(c, dict))


def codex_turns(path: Path, last: int = 10) -> list[dict]:
    """`[{kind, text}]` from a Codex rollout (said / prompt / tool / result / edit), newest `last`."""
    turns: list[dict] = []
    try:
        fh = open(path, errors="replace")
    except OSError:
        return []
    with fh:
        for line in fh:
            if '"item_completed"' not in line:
                continue
            try:
                it = (json.loads(line).get("payload") or {}).get("item") or {}
            except ValueError:
                continue
            kind = it.get("type")
            if kind == "AgentMessage":
                turns.append({"kind": "said", "text": _text_of(it.get("content"))})
            elif kind == "UserMessage":
                turns.append({"kind": "prompt", "text": _text_of(it.get("content"))})
            elif kind == "CommandExecution":
                cmd = it.get("command")
                cmd = cmd[-1] if isinstance(cmd, list) and cmd else cmd
                out = str(it.get("stdout") or it.get("stderr") or "").strip().splitlines()[-3:]
                turns.append({"kind": "tool", "text": f"{cmd} → [{it.get('status')}] " + " ⏎ ".join(out)})
            elif kind == "FileChange":
                turns.append({"kind": "edit", "text": "edited " + ", ".join(sorted((it.get("changes") or {}))[:6])})
    return [{"kind": t["kind"], "text": " ".join(t["text"].split())} for t in turns[-last:]]


def codex_last_said(path: Path) -> str | None:
    """The last non-empty AgentMessage text in a Codex rollout, newlines kept (unredacted; callers redact)."""
    last = None
    try:
        fh = open(path, errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            if '"AgentMessage"' not in line:
                continue
            try:
                it = (json.loads(line).get("payload") or {}).get("item") or {}
            except ValueError:
                continue
            if it.get("type") == "AgentMessage" and _text_of(it.get("content")).strip():
                last = _text_of(it.get("content")).strip()
    return last


def transcript_turns(store, job_id: str, *, last: int = 10, projects_dir: Path | None = None,
                     sessions_dir: Path | None = None) -> list[dict]:
    """The worker's recent turns for either runtime, redacted. Claude JSONL first, else the Codex rollout."""
    masking.arm_job(store, job_id)
    turns: list[dict] = []
    try:
        from steward import transcript

        turns = [{"kind": t.get("kind"), "text": t.get("text", "")}
                 for t in (transcript.worker_activity(store, job_id, last, None, projects_dir=projects_dir).get("turns") or [])]
    except Exception:  # noqa: BLE001 - best-effort evidence
        turns = []
    if not turns:
        rollout = codex_rollout(worktree_path(store, job_id), sessions_dir)
        turns = codex_turns(rollout, last) if rollout else []
    return [{"kind": t["kind"], "text": _clean(t["text"], TURN_CAP)} for t in turns[-last:]]


# -- git ----------------------------------------------------------------------------------------


def git_state(worktree: Path | str, *, run: Run = subprocess.run) -> str:
    wt = Path(worktree)
    if not wt.is_dir():
        return ""
    status = _run(["git", "status", "--short", "--branch"], cwd=wt, run=run).strip()
    stat = _run(["git", "diff", "--stat", "HEAD"], cwd=wt, run=run).strip()
    return _clean("\n".join(x for x in (status, stat) if x), GIT_CAP)


def worker_context(store, job_id: str, *, lines: int = PANE_LINES, run: Run = subprocess.run,
                   projects_dir: Path | None = None, sessions_dir: Path | None = None) -> dict:
    """`{pane, transcript, git}` — the read-only picture of the worker. Empty parts are omitted."""
    ctx = {
        "pane": pane_screen(store, job_id, lines=lines, run=run),
        "transcript": transcript_turns(store, job_id, projects_dir=projects_dir, sessions_dir=sessions_dir),
        "git": git_state(worktree_path(store, job_id), run=run),
    }
    return {k: v for k, v in ctx.items() if v}
