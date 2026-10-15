"""Replay harness for the G16 gate (17-07): the Phase 16 discussion job, answered the way Sam answered it by hand.

`run_replay` drives the REAL code: it commits the Phase 16 artifacts as they were at 49fb32c (the committed fixtures
phase16_context_before.md and phase16_log_before.md) into a real tmp git worktree of a job, runs `questions.scan`,
answers through `questions.apply` (or the real `steward answer` verb with `via="cli"`), and flushes on a NEEDS_HUMAN
job through `questions_delivery.flush`. The only fakes are the clock, the tmp Store and repo, and a subprocess guard
that records every launch and raises on anything but `git` (so no claude, codex, herdr, tg or network is possible).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from steward import cli, questions, questions_delivery
from steward.models import JobState
from steward.runner_exec import SubprocessRunner
from steward.store import Store

HERE = Path(__file__).parent
FIX = HERE / "fixtures" / "questions"
CTX = ".planning/phases/16-efficiency-watcher/16-CONTEXT.md"
LOG = ".planning/phases/16-efficiency-watcher/16-DISCUSSION-LOG.md"
ORACLE = FIX / "phase16_expected.json"

NOW = datetime(2026, 9, 29, 23, 14)  # naive: used as is by questions.apply (renders 2026-09-29)
FLUSH_EPOCH = 1_790_000_000.0
GIT_DATE = "2026-09-29T10:00:00 +0000"
ORIGIN = "moshi"
NOTE = ("before acceptance the efficiency watcher must also run observe-only (T0) on at least one live Steward job "
        "for at least one unit boundary, with its findings shown on the status pane and no nudges sent.")
G15_OTHER = "replay + live trial"
GOAL = "Discuss Phase 16 (efficiency watcher): surface the gray areas for Sam"


def qid(n: int) -> str:
    return f"gsd_context:16-CONTEXT.md:G{n}"


def load_oracle() -> dict:
    return json.loads(ORACLE.read_text())


def _env(extra: dict | None = None) -> dict:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    env.update(extra or {})
    return env


def git(cwd, *args, env: dict | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env=_env(env)).stdout.strip()


@dataclass
class World:
    store: Store
    job_id: str
    repo: Path
    worktree: Path
    base_sha: str
    head_before: str = ""


def build_world(base_dir) -> World:
    base = Path(base_dir)
    src = base / "src"
    src.mkdir(parents=True)
    git(src, "init", "-q", "-b", "main")
    git(src, "config", "user.name", "Steward Replay")
    git(src, "config", "user.email", "replay@localhost")
    (src / "README.md").write_text("replay\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "baseline", env={"GIT_AUTHOR_DATE": GIT_DATE, "GIT_COMMITTER_DATE": GIT_DATE})
    base_sha = git(src, "rev-parse", "HEAD")

    store = Store(base / "home")
    job = store.create_job(GOAL, str(src))
    wt = store.home / "worktrees" / job.job_id
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(src, "worktree", "add", "-q", "-b", f"steward/{job.job_id}", str(wt), "HEAD")
    store.add_check(job.job_id, commit_sha=base_sha, command="pytest", exit_code=0, results={}, label="baseline")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.RUNNING.value, job.job_id))

    for rel, name in ((CTX, "phase16_context_before.md"), (LOG, "phase16_log_before.md")):
        p = wt / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((FIX / name).read_text())
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "docs(16): discuss the gray areas",
        env={"GIT_AUTHOR_DATE": GIT_DATE, "GIT_COMMITTER_DATE": GIT_DATE})
    return World(store, job.job_id, src, wt, base_sha, git(wt, "rev-parse", "HEAD"))


@contextmanager
def subprocess_guard(calls: list):
    """Record argv[0] of every subprocess launch; raise on anything but git (G15: no model, no herdr, no network)."""
    real_popen, real_run = subprocess.Popen, subprocess.run

    def check(argv):
        head = os.path.basename(str((list(argv) if not isinstance(argv, str) else argv.split())[0]))
        calls.append(head)
        if head != "git":
            raise AssertionError(f"replay launched a non-git subprocess: {argv}")

    class GuardedPopen(real_popen):
        def __init__(self, args, *a, **k):
            check(args)
            super().__init__(args, *a, **k)

    def guarded_run(args, *a, **k):
        check(args)
        return real_run(args, *a, **k)

    subprocess.Popen, subprocess.run = GuardedPopen, guarded_run
    try:
        yield
    finally:
        subprocess.Popen, subprocess.run = real_popen, real_run


@contextmanager
def _fixed_clock_and_home(home):
    """The CLI path reads $STEWARD_HOME and the engine's `datetime.now()`; pin both for the replay (the clock fake)."""
    real_dt = questions.datetime

    class FixedDT(real_dt):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.replace(tzinfo=tz)

    real_relay = cli._relay

    def in_process_relay(store, op, args, *, request_id=None, origin="cli", **_ignored):
        # the same in-process ControlPane relay the suite's conftest installs: no Herdr, no control pane process
        import uuid

        from steward.control_pane import ControlPane

        pane = ControlPane(store, cli._build_control_handlers(store), print_fn=lambda _line: None)
        return pane.execute({"request_id": request_id or str(uuid.uuid4()), "op": op, "args": args, "origin": origin})

    old_home = os.environ.get("STEWARD_HOME")
    os.environ["STEWARD_HOME"] = str(home)
    questions.datetime = FixedDT
    cli._relay = in_process_relay
    try:
        yield
    finally:
        cli._relay = real_relay
        questions.datetime = real_dt
        if old_home is None:
            os.environ.pop("STEWARD_HOME", None)
        else:
            os.environ["STEWARD_HOME"] = old_home


_TIME_KEYS = {"at", "commit", "sha", "paths", "first_seen_at", "updated_at"}


def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in _TIME_KEYS}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class ReplayResult:
    cards: list
    records_after: list
    events: list
    context_text: str
    log_text: str
    commit_message: str
    commits_added: int
    files_committed: list
    argv0: list
    scan_events_final: list = field(default_factory=list)
    wall_s: float = 0.0
    answer_results: list = field(default_factory=list)
    judgments: list = field(default_factory=list)

    def digest(self) -> str:
        cards = [_strip(c) for c in self.cards]
        answers = {r["qid"]: _strip(r.get("answer")) for r in self.records_after}
        written = {r["qid"]: {"via": (r.get("written") or {}).get("via")} for r in self.records_after}
        events = [[what, _strip(ev)] for what, ev in self.events]
        payload = {"cards": cards, "answers": answers, "written": written, "events": events,
                   "context_sha": _sha(self.context_text), "log_sha": _sha(self.log_text),
                   "commit_message": self.commit_message, "commits_added": self.commits_added,
                   "files_committed": sorted(self.files_committed)}
        return json.dumps(payload, sort_keys=True)


def _answer_engine(w: World, runner) -> list:
    out = []
    out.append(questions.apply(w.store, w.job_id, accept_recommended=True, qids=[qid(n) for n in range(1, 15)],
                               origin=ORIGIN, request_id="replay-1", now=NOW, runner=runner))
    out.append(questions.apply(w.store, w.job_id,
                               answers=[{"qid": qid(15), "other": G15_OTHER, "note": NOTE}],
                               origin=ORIGIN, request_id="replay-2", now=NOW, runner=runner))
    return out


def _answer_cli(w: World) -> list:
    rc1 = cli.main(["answer", w.job_id, "--accept-recommended", *[qid(n) for n in range(1, 15)],
                    "--origin", ORIGIN, "--request-id", "replay-1"])
    rc2 = cli.main(["answer", w.job_id, qid(15), "--other", G15_OTHER, "--note", NOTE,
                    "--origin", ORIGIN, "--request-id", "replay-2"])
    assert (rc1, rc2) == (0, 0), (rc1, rc2)
    return [{"rc": rc1}, {"rc": rc2}]


def _set_state(store: Store, job_id: str, state: JobState) -> None:
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job_id))


def _question_events(store: Store, job_id: str) -> list:
    return [(e.what, dict(e.evidence)) for e in store.events(job_id)
            if e.what.startswith("question_") or e.what.startswith("questions_")]


def run_replay(base_dir, *, via: str = "engine") -> ReplayResult:
    t0 = time.monotonic()
    calls: list = []
    with subprocess_guard(calls):
        w = build_world(base_dir)
        with _fixed_clock_and_home(w.store.home):
            runner = SubprocessRunner()
            res = questions.scan(w.store, w.job_id, runner)
            assert res.error is None, res.error
            cards = copy.deepcopy(questions.load(w.store, w.job_id))
            results = _answer_engine(w, runner) if via == "engine" else _answer_cli(w)
            _set_state(w.store, w.job_id, JobState.NEEDS_HUMAN)
            head_before = git(w.worktree, "rev-parse", "HEAD")
            if via == "engine":
                flushed = questions_delivery.flush(w.store, w.job_id, runner, force=True, now=FLUSH_EPOCH,
                                                   origin=ORIGIN)
                assert flushed["accepted"], flushed
            else:
                assert cli.main(["answer", w.job_id, "--done", "--origin", ORIGIN]) == 0
            n_events = len(w.store.events(w.job_id))
            questions.scan(w.store, w.job_id, runner, force=True)
            final_scan = [e.what for e in w.store.events(w.job_id)[n_events:]]
            commits = int(git(w.worktree, "rev-list", "--count", f"{head_before}..HEAD"))
            message = git(w.worktree, "log", "-1", "--format=%B")
            files = [ln for ln in git(w.worktree, "show", "--name-only", "--format=", "HEAD").splitlines() if ln]
            context = subprocess.run(["git", "show", f"HEAD:{CTX}"], cwd=w.worktree, check=True,
                                     capture_output=True, text=True).stdout
            log = subprocess.run(["git", "show", f"HEAD:{LOG}"], cwd=w.worktree, check=True,
                                 capture_output=True, text=True).stdout
            after = questions.load(w.store, w.job_id)
            events = _question_events(w.store, w.job_id)
            judgments = [(e.what, e.judgment.value) for e in w.store.events(w.job_id)
                         if e.what.startswith("question_") or e.what.startswith("questions_")]
    return ReplayResult(cards=cards, records_after=after, events=events, context_text=context, log_text=log,
                        commit_message=message, commits_added=commits, files_committed=files, argv0=list(calls),
                        scan_events_final=final_scan, wall_s=time.monotonic() - t0, answer_results=results,
                        judgments=judgments)
