"""Replay harness for the G15 gate (16-07): a deterministic replay of a content-free fixture through the REAL watcher.

`run_replay` drives the real `EfficiencyWatcher.step`, `tokens`, `efficiency.compute` and `efficiency_detect` against
a real `Store`, on transcripts appended in timestamp order under a fake clock. The only fakes are the clock and
spend_clock, cpu_fn, projects_dir (a tmp dir), the notify spy and a subprocess guard that raises on any herdr or
claude argv (so no `steward-test-*` session can ever exist).

`from_fixture` expands the trimmed records of scripts/trim_efficiency_fixture.py into real-format transcript lines with
the same usage numbers, visible-char counts, tool names, command/path keys and result sizes, but placeholder content.
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from steward import efficiency_watch, tokens
from steward.chat import project_slug, worktree_path
from steward.models import JobState
from steward.steering import Steering
from steward.store import Store

from efficiency_synth import agent_lines, iso

JOB_ID = "06ea6a04"
_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
_WRITE_CAP = 60_000
_RESULT_CAP = 25_000


@dataclass
class ReplayFile:
    rel: str
    meta: dict
    lines: list  # [(t_epoch, json_line)]
    kind: str = "toplevel"  # worker | subagent | toplevel


# -- fixture -> transcript lines ----------------------------------------------------------------------------


def _tool_input(name: str, cmd_key: str, path_key: str, write_chars: int, whole: bool) -> dict:
    if name == "Bash":
        return {"command": f"cmd-{cmd_key}"}
    if name == "Write":
        return {"file_path": f"p-{path_key}", "content": "z" * min(write_chars, _WRITE_CAP)}
    if path_key:
        return {"file_path": f"p-{path_key}"} if whole or name != "Read" else {"file_path": f"p-{path_key}", "offset": 1}
    return {}


def _expand(rec: dict, first_user: list, marker: str) -> str:
    t, ts = rec["t"], iso(rec["t"])
    if rec["k"] == "a":
        blocks = [{"type": "tool_use", "id": tid, "name": name, "input": _tool_input(name, cmd, path, wchars, whole)}
                  for tid, name, cmd, path, wchars, whole in rec["tu"]]
        text = max(0, rec["v"] - tokens.visible_chars(blocks))
        content = ([{"type": "text", "text": "x" * text}] if text else []) + blocks
        if not content:
            content = [{"type": "thinking", "thinking": "", "signature": "s"}]
        line = {"type": "assistant", "timestamp": ts, "effort": rec["e"],
                "message": {"id": rec["id"], "model": rec["m"], "role": "assistant", "stop_reason": rec["s"] or None,
                            "usage": dict(zip(_USAGE_KEYS, rec["u"])), "content": content}}
        if not line["effort"]:
            del line["effort"]
        return json.dumps(line)
    if rec["k"] == "u":
        results = [{"type": "tool_result", "tool_use_id": tid, "content": "y" * min(chars, _RESULT_CAP)}
                   for tid, chars in rec["r"]]
        if first_user[0]:
            first_user[0] = False
            content = ([{"type": "text", "text": marker}] + results) if marker and results else (marker or results or "task")
        else:
            content = results or "u"
        return json.dumps({"type": "user", "timestamp": ts, "message": {"role": "user", "content": content}})
    return json.dumps({"type": rec["y"], "timestamp": ts})


def from_fixture(path) -> list[ReplayFile]:
    doc = json.loads(gzip.decompress(Path(path).read_bytes()))
    out = []
    for f in doc["files"]:
        first_user = [True]
        lines = [(r["t"], _expand(r, first_user, f["marker"])) for r in f["lines"]]
        out.append(ReplayFile(f["rel"], f["meta"], lines, f["kind"]))
    return out


def load_expected(path) -> dict:
    return json.loads(gzip.decompress(Path(path).read_bytes()))["expected"]


# -- a synthetic steady job ----------------------------------------------------------------------------------


def steady_job_files(base: float = 1_800_000_000.0, units: int = 3) -> list[ReplayFile]:
    """Three equal units. Each: a planner (12 calls, ctx 40-60k, effort medium), an executor (25 calls, ctx 60-90k)
    and a verifier (8 calls). Planner N+1 starts halfway through executor N; call gaps <= 60 s, results <= 5k chars,
    writes <= 10k chars, and the whole job costs a few dollars an hour."""
    files: list[ReplayFile] = []

    def add(rel, meta, t0, kind, **kw):
        lines = agent_lines(t0, effort=kw.pop("effort", "medium"), **kw)
        files.append(ReplayFile(rel, meta, [(_epoch_of(l), l) for l in lines], kind))

    period, exec_calls = 600.0, 25
    exec_start = [base + 200.0 + u * period for u in range(units)]
    exec_len = exec_calls * 15.0
    for u in range(units):
        n = u + 1
        p_start = base if u == 0 else exec_start[u - 1] + exec_len / 2
        add(f"w/subagents/agent-p{n}.jsonl", {"agentType": "planner", "description": f"Plan phase {n}"}, p_start,
            "subagent", calls=12, ctx0=40_000, ctx_step=20_000 // 11, out=100, vis_ratio=0.7, prefix=f"p{n}",
            writes=[(f"/plan-{n}.md", 8_000)], tool_results=[3_000] * 12)
        add(f"w/subagents/agent-e{n}.jsonl", {"agentType": "executor", "description": f"Execute phase {n} plans"},
            exec_start[u], "subagent", calls=exec_calls, ctx0=60_000, ctx_step=30_000 // (exec_calls - 1), out=120,
            vis_ratio=0.5, prefix=f"e{n}", writes=[(f"/src-{n}.py", 9_000)], reads=[(f"/plan-{n}.md", True)],
            tool_results=[4_000] * exec_calls)
        add(f"w/subagents/agent-v{n}.jsonl", {"agentType": "verifier", "description": f"Verify phase {n}"},
            exec_start[u] + exec_len + 5.0, "subagent", calls=8, ctx0=50_000, ctx_step=5_000, out=80, vis_ratio=0.5,
            prefix=f"v{n}", tool_results=[2_000] * 8)
    add("w.jsonl", {}, base, "worker", calls=34, ctx0=30_000, ctx_step=100, out=60, vis_ratio=0.5, gen_s=20.0,
        tool_s=40.0, prefix="w", effort="high")
    return sorted(files, key=lambda f: f.rel)


def _epoch_of(line: str) -> float:
    return tokens._epoch(json.loads(line)["timestamp"])


# -- the replay ----------------------------------------------------------------------------------------------


class FakeClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@dataclass
class ReplayResult:
    events: list  # [(what, evidence)] for efficiency_* and spend events, in order
    first_seen: dict  # finding id -> evidence of its first efficiency_finding event
    state: dict  # the final efficiency_state
    notifications: list
    instructions: list  # every Steering instruction ever recorded
    subprocess_calls: list
    files: dict  # rel -> {"meta", "kind", "first_t", "ids": [(message id, t)]}
    proj: Path
    store: Store
    job_id: str
    start: float
    end: float
    ticks: int
    worker_stem: str
    wall_s: float = 0.0
    cpu_max_s: float = 0.0
    extra: dict = field(default_factory=dict)

    def digest(self) -> str:
        """Everything the watcher decided, with no real time, tmp path or pid in it."""
        return json.dumps({"events": self.events, "state": self.state, "notifications": self.notifications,
                           "instructions": [{k: v for k, v in i.items() if k != "ts"} for i in self.instructions]},
                          sort_keys=True, default=str)


@contextmanager
def subprocess_guard(calls: list):
    """Record every subprocess launch; raise on any argv that mentions herdr or claude."""
    real_popen, real_run = subprocess.Popen, subprocess.run
    state = {"in_run": False}

    def check(argv):
        text = " ".join(map(str, argv)) if isinstance(argv, (list, tuple)) else str(argv)
        calls.append(text)
        if "herdr" in text or "claude" in text:
            raise AssertionError(f"replay tried to run a real herdr/claude process: {text[:120]}")

    class GuardedPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *a, **k):
            if not state["in_run"]:
                check(args)
            super().__init__(args, *a, **k)

    def guarded_run(args, *a, **k):
        check(args)
        state["in_run"] = True
        try:
            return real_run(args, *a, **k)
        finally:
            state["in_run"] = False

    subprocess.Popen, subprocess.run = GuardedPopen, guarded_run
    try:
        yield
    finally:
        subprocess.Popen, subprocess.run = real_popen, real_run


def _clean(node, base_dir: Path):
    """Strip the tmp path from any string so digests never carry it."""
    if isinstance(node, str):
        return node.replace(str(base_dir), "BASE")
    if isinstance(node, dict):
        return {k: _clean(v, base_dir) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_clean(v, base_dir) for v in node]
    return node


def run_replay(files: list[ReplayFile], base_dir, *, tick_s: float = 30.0, extra_ticks: int = 2,
               job_id: str = JOB_ID, goal: str = "replay", setup=None, before_step=None, after_step=None,
               watcher_kwargs: dict | None = None) -> ReplayResult:
    """`setup(store, job_id, clock)` (phase 18) creates the job itself and returns it; it may move `clock.t` back to
    the job's own start, which then becomes the first tick. `before_step` / `after_step` get (store, job_id, t) around
    every watcher.step; `watcher_kwargs` is merged into the EfficiencyWatcher constructor. With none of them the
    function is exactly the Phase 16 replay."""
    import time as _time

    base_dir = Path(base_dir)
    home = base_dir / "home"
    cfg_dir = base_dir / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    store = Store(home)
    order = sorted(range(len(files)), key=lambda i: files[i].rel)
    rank = {i: r for r, i in enumerate(order)}
    pending = sorted(((t, i, k, line) for i, f in enumerate(files) for k, (t, line) in enumerate(f.lines)),
                     key=lambda x: (x[0], x[1], x[2]))
    start = pending[0][0] if pending else 0.0
    end = pending[-1][0] if pending else 0.0
    clock = FakeClock(start)
    if setup is not None:
        job = setup(store, job_id, clock)
        start = min(start, clock.t)
        clock.t = start
    else:
        job = store.create_job(goal, "/tmp/repo", job_id=job_id)
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.RUNNING.value, job.job_id))
    worker = next(f for f in files if f.kind == "worker")
    worker_stem = Path(worker.rel).stem
    store.kv_set(job.job_id, "worker_session_id", worker_stem)
    proj = base_dir / "projects"
    slug_dir = proj / project_slug(worktree_path(store, job.job_id))
    slug_dir.mkdir(parents=True)
    notes: list = []
    calls: list = []
    shared: dict = {}
    watcher = efficiency_watch.EfficiencyWatcher(
        store, clock=clock, spend_clock=clock, projects_dir=proj, config_dir=cfg_dir, cpu_fn=lambda: 0.0,
        notify=lambda *a, **k: notes.append(list(a)),
        tokens_fn=lambda st, j, now=None: tokens.job_tokens(st, j, now=now, projects_dir=proj, _cache=shared),
        **(watcher_kwargs or {}))

    index = {f.rel: {"meta": f.meta, "kind": f.kind, "first_t": f.lines[0][0] if f.lines else None,
                     "ids": []} for f in files}
    for f in files:
        for t, line in f.lines:
            o = json.loads(line)
            if o.get("type") == "assistant":
                index[f.rel]["ids"].append((o["message"]["id"], t))

    created: set[int] = set()
    ptr, ticks, cpu_max = 0, 0, 0.0
    wall0 = _time.perf_counter()
    with subprocess_guard(calls):
        total = int((end - start) / tick_s) + 1 + extra_ticks
        for _ in range(total):
            clock.t += tick_s
            ticks += 1
            due: dict[int, list[str]] = {}
            while ptr < len(pending) and pending[ptr][0] <= clock.t:
                _t, i, _k, line = pending[ptr]
                due.setdefault(i, []).append(line)
                ptr += 1
            for i in sorted(due):
                f = files[i]
                path = slug_dir / f.rel
                if i not in created:
                    created.add(i)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if f.kind == "subagent":
                        path.with_suffix(".meta.json").write_text(json.dumps(f.meta))
                with open(path, "a") as fh:
                    fh.write("\n".join(due[i]) + "\n")
                mt = int(clock.t * 1e9) + rank[i]  # deterministic mtime order, ties broken by rel
                os.utime(path, ns=(mt, mt))
            if before_step is not None:
                before_step(store, job.job_id, clock.t)
            watcher.step(job.job_id)
            if after_step is not None:
                after_step(store, job.job_id, clock.t)
    wall = _time.perf_counter() - wall0

    evs = [(e.what, _clean(e.evidence, base_dir)) for e in store.events(job.job_id)
           if e.what.startswith("efficiency_") or e.what in ("spend_rate_high", "watcher_overspend", "spend_check_error")]
    first_seen: dict = {}
    for what, ev in evs:
        if what == "efficiency_finding" and isinstance(ev, dict):
            first_seen.setdefault(ev["id"], ev)
    state = store.kv_get(job.job_id, efficiency_watch.STATE_KEY) or {}
    return ReplayResult(
        events=evs, first_seen=first_seen, state=_clean(state, base_dir), notifications=_clean(notes, base_dir),
        instructions=Steering(store).all_instructions(job.job_id), subprocess_calls=calls, files=index, proj=proj,
        store=store, job_id=job.job_id, start=start, end=clock.t, ticks=ticks, worker_stem=worker_stem,
        wall_s=wall, cpu_max_s=cpu_max)
