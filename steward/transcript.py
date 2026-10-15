"""Readable view of what a job's worker said, did and saw.

The events DB records that things happened; the worker's Claude session
transcript (`~/.claude/projects/<slug>/<session>.jsonl`) records the
conversation. `worker_activity` reads ONLY the live worker's own session file
(forks of it, watcher and doctor transcripts are never mixed in), turns it
into a short list of `{i, ts, kind, text}` turns, and redacts every text
field. Read-only; per-file parse results are cached by (mtime, size).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, TextIO

from steward import masking
from steward.chat import project_slug, worktree_path
from steward.diagnosis import redact
from steward.machine import is_terminal
from steward.tokens import _ASTRA_MARKER, _SOL_MARKER, _WATCHER_MARKER

SAID_CAP = 600
RESULT_LINES = 8
RESULT_CAP = 600
TOOL_CAP = 300
PROMPT_CAP = 600
TOTAL_CAP = 8000
_STEER_RE = re.compile(r"^\s*\[rev=(\d+)\]")
_COLORS = {"said": "36", "tool": "33", "result": "2", "error": "31", "steer": "1;32", "prompt": "35", "thinking": "2"}
_LABEL = {"said": "said", "tool": "tool", "result": "→", "error": "→ ERR", "thinking": "think", "prompt": "PROMPT"}
_CACHE: dict[str, tuple[int, int, dict]] = {}


def _trim(text: str, cap: int) -> str:
    text = text.strip()
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def _tool_summary(name: str, inp: object) -> str:
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        return f"Bash: {inp.get('command', '')}"
    if name in ("Edit", "MultiEdit", "Read", "NotebookEdit"):
        return f"{name}: {inp.get('file_path') or inp.get('notebook_path') or ''}"
    if name == "Write":
        n = len(str(inp.get("content", "")).splitlines())
        return f"Write: {inp.get('file_path', '')} ({n} lines)"
    try:
        short = json.dumps(inp, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        short = str(inp)
    return f"{name}: {short}"


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    text = redact(str(content or "")).strip("\n")
    lines = text.splitlines()
    out = "\n".join(lines[:RESULT_LINES])
    if len(lines) > RESULT_LINES:
        out += f"\n… (+{len(lines) - RESULT_LINES} lines)"
    return _trim(out, RESULT_CAP)


def _parse(path: Path) -> dict:
    turns: list[dict] = []
    model = None
    seen: set = set()
    pending: dict[str, int] = {}  # tool_use id -> turn index awaiting a result
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            typ, ts = obj.get("type"), obj.get("timestamp", "")
            msg = obj.get("message") or {}
            content = msg.get("content")
            if typ == "user":
                if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    for b in content:
                        if not isinstance(b, dict) or b.get("type") != "tool_result":
                            continue
                        pending.pop(b.get("tool_use_id"), None)
                        err = bool(b.get("is_error"))
                        turns.append({"ts": ts, "kind": "error" if err else "result", "text": _result_text(b)})
                    continue
                if isinstance(content, list):
                    content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
                if not isinstance(content, str) or not content.strip() or obj.get("isMeta"):
                    continue
                kind = "steer" if _STEER_RE.match(content) else "prompt"
                turns.append({"ts": ts, "kind": kind, "text": _trim(redact(content), PROMPT_CAP)})
            elif typ == "assistant":
                model = msg.get("model") or model
                mid = msg.get("id") or ""
                for b in content if isinstance(content, list) else []:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text" and (b.get("text") or "").strip():
                        key = (mid, "t", b["text"])
                        if key in seen:
                            continue
                        seen.add(key)
                        turns.append({"ts": ts, "kind": "said", "text": _trim(redact(b["text"]), SAID_CAP)})
                    elif bt == "thinking" and (b.get("thinking") or "").strip():
                        turns.append({"ts": ts, "kind": "thinking", "text": _trim(redact(b["thinking"]), SAID_CAP)})
                    elif bt == "tool_use":
                        key = b.get("id") or (mid, "u", json.dumps(b.get("input"), sort_keys=True, default=str))
                        if key in seen:
                            continue
                        seen.add(key)
                        pending[b.get("id") or ""] = len(turns)
                        turns.append({"ts": ts, "kind": "tool",
                                      "text": _trim(redact(_tool_summary(b.get("name", "?"), b.get("input"))), TOOL_CAP)})
    for i, t in enumerate(turns):
        t["i"] = i
    return {"turns": turns, "model": model, "waiting": bool(pending)}


def _parsed(path: Path) -> dict:
    st = path.stat()
    hit = _CACHE.get(str(path))
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    data = _parse(path)
    _CACHE[str(path)] = (st.st_mtime_ns, st.st_size, data)
    return data


def _first_user_text(path: Path) -> str:
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                if '"user"' not in line:
                    continue
                obj = json.loads(line)
                if obj.get("type") == "user":
                    c = (obj.get("message") or {}).get("content")
                    return c if isinstance(c, str) else ""
    except (OSError, ValueError):
        pass
    return ""


def worker_file(store, job_id: str, projects_dir: Path | None = None) -> Path | None:
    """The live worker's own session file: recorded `worker_session_id`, else the
    newest file in the worktree's project dir that is not a watcher/doctor/advisor run."""
    base = projects_dir if projects_dir is not None else Path.home() / ".claude" / "projects"
    d = base / project_slug(worktree_path(store, job_id))
    sid = store.kv_get(job_id, "worker_session_id")
    if sid:
        p = d / f"{sid}.jsonl"
        return p if p.is_file() else None
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if d.is_dir() else []
    for p in files:
        first = _first_user_text(p)
        if _SOL_MARKER in first or first.startswith((_ASTRA_MARKER, _WATCHER_MARKER)):
            continue
        return p
    return None


def _hms(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except (ValueError, AttributeError):
        return "--:--:--"


def _now_line(turns: list[dict], waiting: bool) -> str:
    if not turns:
        return "no transcript activity yet"
    last_tool = next((t for t in reversed(turns) if t["kind"] == "tool"), None)
    lt = turns[-1]
    if waiting and last_tool is not None:
        return f"running {last_tool['text']} (waiting on its result since {_hms(last_tool['ts'])})"
    tool = f"last tool {last_tool['text']}; " if last_tool else ""
    return f"{tool}idle since {_hms(lt['ts'])} (last turn: {lt['kind']})"


def live_line(store, job_id: str, *, projects_dir: Path | None = None) -> dict:
    """The worker's "now" as fields: `now` text, running tool + `since`, last thing said."""
    masking.arm_job(store, job_id)
    path = worker_file(store, job_id, projects_dir)
    if path is None:
        return {"now": "no worker transcript found", "running_tool": None, "since": None,
                "last_said": None, "last_activity_ts": None}
    data = _parsed(path)
    turns = data["turns"]
    last_tool = next((t for t in reversed(turns) if t["kind"] == "tool"), None)
    said = next((t for t in reversed(turns) if t["kind"] == "said"), None)
    running = bool(data["waiting"] and last_tool is not None)
    return {
        "now": _now_line(turns, data["waiting"]),
        "running_tool": last_tool["text"] if running else None,
        "since": last_tool["ts"] if running else None,
        "last_said": said["text"] if said else None,
        "last_activity_ts": turns[-1]["ts"] if turns else None,
    }


def _codex_activity(store, job_id: str, last: int, sessions_dir: Path | None) -> dict | None:
    """worker_activity's shape for a Codex rollout (redacted by worker_view), or None if there is none."""
    from steward import worker_view

    rollout = worker_view.codex_rollout(worktree_path(store, job_id), sessions_dir)
    raw = worker_view.codex_turns(rollout, last or 10) if rollout else []
    turns = [{"kind": t["kind"], "text": worker_view._clean(t["text"], worker_view.TURN_CAP)} for t in raw]
    if not turns:
        return None
    turns = [{"i": i, "ts": "", "kind": t["kind"], "text": t["text"]} for i, t in enumerate(turns)]
    return {"job_id": job_id, "session_id": None, "model": "codex", "last_activity_ts": None,
            "turn_count": len(turns), "now": "codex rollout (no timestamps)", "turns": turns,
            "next_since": len(turns), "truncated": None}


def worker_activity(store, job_id: str, last: int = 10, since=None, *, projects_dir: Path | None = None,
                    sessions_dir: Path | None = None) -> dict:
    """Turns in order. `last` counts from the end; `since` is an ISO timestamp
    (turns after it) or a turn-index cursor (turns with i >= since) for paging
    forward — feed back `next_since`."""
    masking.arm_job(store, job_id)
    path = worker_file(store, job_id, projects_dir)
    if path is None:
        codex = _codex_activity(store, job_id, last, sessions_dir)
        if codex is not None:
            return codex
        return {"job_id": job_id, "session_id": None, "model": None, "last_activity_ts": None, "turn_count": 0,
                "now": "no worker transcript found", "turns": [], "next_since": 0, "truncated": None}
    data = _parsed(path)
    turns = data["turns"]
    sel = turns
    if since not in (None, ""):
        s = str(since)
        if s.isdigit():
            sel = [t for t in turns if t["i"] >= int(s)]
        else:
            sel = [t for t in turns if t["ts"] > s]
    if last is not None and last >= 0:
        sel = sel[-last:] if last else []
    note = None
    total = 0
    keep = []
    for t in reversed(sel):
        total += len(t["text"]) + 20
        if total > TOTAL_CAP and keep:
            note = f"output capped at ~{TOTAL_CAP} chars: {len(sel) - len(keep)} older turn(s) omitted; page with since"
            break
        keep.append(t)
    keep.reverse()
    return {
        "job_id": job_id, "session_id": path.stem, "model": data["model"],
        "last_activity_ts": turns[-1]["ts"] if turns else None, "turn_count": len(turns),
        "now": _now_line(turns, data["waiting"]),
        "turns": [dict(t) for t in keep], "next_since": len(turns), "truncated": note,
    }


# -- rendering -------------------------------------------------------------------------

def _one_line(text: str, cap: int) -> str:
    s = " ⏎ ".join(x.strip() for x in text.splitlines() if x.strip())
    return s if len(s) <= cap else s[: cap - 1] + "…"


def render_turn(t: dict, *, width: int = 200, color: bool = False) -> str:
    kind = t["kind"]
    if kind == "steer":
        m = _STEER_RE.match(t["text"])
        label, text = f"STEER rev {m.group(1)}" if m else "STEER", t["text"][m.end():] if m else t["text"]
    else:
        label, text = _LABEL.get(kind, kind), t["text"]
    head = f"{_hms(t['ts'])} {label:<6} "
    body = _one_line(text, max(width - len(head) - 1, 20))
    if not color:
        return head + body
    c = _COLORS.get(kind, "0")
    return f"\x1b[2m{_hms(t['ts'])}\x1b[0m \x1b[{c}m{label:<6}\x1b[0m {body}"


def render_text(act: dict) -> str:
    lines = [f"now: {act['now']}"]
    lines += [render_turn(t) for t in act["turns"]]
    if act.get("truncated"):
        lines.append(f"[{act['truncated']}]")
    return "\n".join(lines)


def run(store, job_id: str, *, last: int = 10, follow: bool = False, as_json: bool = False, out: TextIO = sys.stdout,
        width_fn: Callable[[], int] = lambda: 120, color: bool = False, sleep: Callable[[float], None] = time.sleep,
        poll_s: float = 2.0, projects_dir: Path | None = None, sessions_dir: Path | None = None) -> int:
    def emit(turns):
        for t in turns:
            print(json.dumps(t) if as_json else render_turn(t, width=width_fn(), color=color), file=out)
        out.flush()

    act = worker_activity(store, job_id, last=last, projects_dir=projects_dir, sessions_dir=sessions_dir)
    if as_json and not follow:
        print(json.dumps(act, indent=2), file=out)
        return 0
    if not as_json:
        print(f"now: {act['now']}", file=out)
    emit(act["turns"])
    if act.get("truncated") and not as_json:
        print(f"[{act['truncated']}]", file=out)
    if not follow:
        return 0
    cursor = act["next_since"]
    try:
        while True:
            job = store.get_job(job_id)
            act = worker_activity(store, job_id, last=1000, since=cursor, projects_dir=projects_dir, sessions_dir=sessions_dir)
            emit(act["turns"])
            cursor = act["next_since"]
            if job is None or is_terminal(job.state):
                return 0
            sleep(poll_s)
    except KeyboardInterrupt:
        return 0
