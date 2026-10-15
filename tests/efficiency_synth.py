"""Synthetic Claude transcripts for the efficiency metrics (16-02); reused by 16-03..16-07.

Builders emit real-format JSONL: a first user line, then per call an assistant message (a
redacted thinking line, a text line, and a tool_use line, all one message id) followed by a
user tool_result line. Timestamps are exact: the assistant lines sit `gen_s` after the previous
line and the tool_result `tool_s` after the tool_use, so every duration metric is predictable.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from steward.chat import project_slug, worktree_path
from steward.models import JobState
from steward.store import Store

T0 = 1_800_000_000.0


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def setup_job(steward_home, tmp_path):
    """(store, job, projects_dir, slug_dir): a RUNNING job whose worker session id is "w"."""
    store = Store()
    job = store.create_job("g", "/tmp/repo")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.RUNNING.value, job.job_id))
    proj = tmp_path / "projects"
    d = proj / project_slug(worktree_path(store, job.job_id))
    d.mkdir(parents=True)
    store.kv_set(job.job_id, "worker_session_id", "w")
    return store, job, proj, d


def _line(obj: dict) -> str:
    return json.dumps(obj)


def agent_lines(t0, calls, *, ctx0=1000, ctx_step=0, out=100, vis_ratio=0.0, gen_s=10.0, tool_s=5.0,
                model="claude-sonnet-5-5", effort="high", prefix="a", tool_results=(), writes=(), reads=(),
                bash=(), finished=True, cc=100, user_text="task") -> list[str]:
    """JSON lines for one agent with `calls` assistant messages.

    ctx of call k = ctx0 + k*ctx_step, split as input 10 / cache_creation `cc` / cache_read rest.
    A text block of round(out*vis_ratio) chars is emitted per call. Call k carries a tool_use (with a
    tool_result `tool_s` later) unless it is the final call of a `finished` agent (text only, stop_reason
    end_turn). `writes` (path, chars), `reads` (path, whole) and `bash` (command) are consumed in that order
    by the first calls; the remaining calls run a default Bash command. `tool_results[k]` is the result size of
    call k's tool (default 50). `gen_s` and `tool_s` may be a number or a per-call list. An unfinished
    agent's last tool_use has no result."""
    actions = ([("Write", {"file_path": p, "content": "x" * n}) for p, n in writes]
               + [("Read", {"file_path": p} if whole else {"file_path": p, "offset": 1, "limit": 50}) for p, whole in reads]
               + [("Bash", {"command": c}) for c in bash])
    lines = [_line({"type": "user", "timestamp": iso(t0), "message": {"role": "user", "content": user_text}})]
    t = t0
    for k in range(calls):
        t += gen_s[k] if isinstance(gen_s, (list, tuple)) else gen_s
        ctx = ctx0 + k * ctx_step
        cci = min(cc, max(ctx - 10, 0))
        usage = {"input_tokens": 10, "output_tokens": out, "cache_read_input_tokens": max(ctx - 10 - cci, 0),
                 "cache_creation_input_tokens": cci}
        last = k == calls - 1
        final_text = finished and last
        mid, tid = f"{prefix}-m{k}", f"{prefix}-t{k}"
        base = {"type": "assistant", "timestamp": iso(t), "effort": effort}

        def block(b, stop):
            return _line({**base, "message": {"id": mid, "model": model, "usage": usage, "stop_reason": stop,
                                              "content": [b]}})
        stop = "end_turn" if final_text else "tool_use"
        lines.append(block({"type": "thinking", "thinking": "", "signature": "sig"}, None))
        vis = round(out * vis_ratio)
        if vis or final_text:
            lines.append(block({"type": "text", "text": "y" * vis}, None if not final_text else stop))
        if not final_text:
            name, inp = actions[k] if k < len(actions) else ("Bash", {"command": f"{prefix}-cmd-{k}"})
            lines.append(block({"type": "tool_use", "id": tid, "name": name, "input": inp}, stop))
            if finished or not last:
                t += tool_s[k] if isinstance(tool_s, (list, tuple)) else tool_s
                n = tool_results[k] if k < len(tool_results) else 50
                lines.append(_line({"type": "user", "timestamp": iso(t), "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tid, "content": "z" * n}]}}))
    return lines


def write_worker(slug_dir: Path, lines: list[str], session: str = "w") -> Path:
    p = Path(slug_dir) / f"{session}.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return p


def write_subagent(slug_dir: Path, session: str, agent_id: str, agent_type: str, description: str,
                   lines: list[str]) -> Path:
    d = Path(slug_dir) / session / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"agent-{agent_id}.jsonl"
    p.write_text("\n".join(lines) + "\n")
    p.with_suffix(".meta.json").write_text(json.dumps({"agentType": agent_type, "description": description}))
    return p
