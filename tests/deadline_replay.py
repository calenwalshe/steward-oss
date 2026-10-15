"""Fake-clock deadline replay (v2.2, DL-16): one job timeline through the real Store, Runner tick, closeout and the
control-pane sweep, with a real temp git repo and pytest checks. Reused by Phase 21 (shadow status) and Phase 22
(check-ins, outbox, approvals). Everything outside is injected: a fake clock, a scripted worker, fixed git dates and
a fake notifier; no network, no Herdr, no real recipient.

`digest()` hashes the normalized deadline/state/notification record (job id, temp paths and wall-clock stamps
removed), so three runs of the same timeline must produce identical digests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from steward import deadline, deadline_plan, deadline_status
from steward.baseline import run_baseline
from steward.contract import propose_contract, store_contract
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.discovery import discover
from steward.models import JobState, Judgment
from steward.runner import Runner
from steward.runner_exec import SubprocessRunner
from steward.store import Store
from steward.worker import WorkerObservation
from steward.worktree import create_worktree

T0 = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
_IDENT = {"GIT_AUTHOR_NAME": "replay", "GIT_AUTHOR_EMAIL": "replay@example.invalid", "GIT_COMMITTER_NAME": "replay",
          "GIT_COMMITTER_EMAIL": "replay@example.invalid"}
_WALL_KEYS = {"at", "ts", "first_at", "resolved_at_wall", "duration_s", "elapsed_s"}


@contextmanager
def git_time(when: datetime):
    stamp = f"{int(when.timestamp())} +0000"
    saved = {k: os.environ.get(k) for k in (*_IDENT, "GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE")}
    os.environ.update(_IDENT, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class ScriptedWorker:
    """A worker that is busy until told it is done (then reports STEWARD-DONE once per `done()`)."""

    def __init__(self):
        self.started_brief = None
        self.worktree = None
        self.worker_ref = None
        self.delivered: list[str] = []
        self._done = False
        self.stopped = False

    def start(self, brief, worktree):
        self.started_brief, self.worktree, self.worker_ref = brief, Path(worktree), "replay-worker"
        return self.worker_ref

    def deliver(self, text, *, urgent=False):
        from steward.worker import DeliveryResult

        self.delivered.append(text)
        return DeliveryResult(ok=True, detail="replay")

    def interrupt(self):
        from steward.worker import DeliveryResult

        return DeliveryResult(ok=True, detail="replay")

    def observe(self):
        tail = "STEWARD-DONE" if self._done else "working"
        self._done = False
        return WorkerObservation(lifecycle="working" if tail == "working" else "idle", output_tail=tail,
                                 pid_alive=True, ts="replay")

    def stop(self):
        self.stopped = True
        return True

    def answer_prompt(self, approve, request_id=None):
        from steward.worker import DeliveryResult

        return DeliveryResult(ok=True, detail="replay")

    def done(self):
        self._done = True


class Replay:
    def __init__(self, home: Path, tmp: Path, *, goal="Add a CSV importer. Optional: CSV export.", when="+4h",
                 start: datetime = T0, eta: bool = False, mode: str = "shadow"):
        self.t = start
        self.home = Path(home)
        self.store = Store(self.home)
        self.run = SubprocessRunner()
        self.src = Path(tmp) / "src"
        self.src.mkdir(parents=True)
        (self.src / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n")
        (self.src / "tests").mkdir()
        (self.src / "tests" / "test_base.py").write_text("def test_base():\n    assert True\n")
        (self.src / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n")
        with git_time(start - timedelta(days=1)):
            _git(self.src, "init", "-q", "-b", "main")
            _git(self.src, "add", "-A")
            _git(self.src, "commit", "-q", "-m", "init")
        # a fixed id: it names the plan path and the branch, so commit SHAs are identical across runs
        self.job_id = self.store.create_job(goal, str(self.src), job_id="d0d0d0d0-replay-0000-0000-000000000001").job_id
        self.wt = create_worktree(self.src, self.job_id, self.run, base_dir=self.home / "worktrees")
        discovery = discover(self.src)
        baseline = run_baseline(self.store, self.job_id, self.src, discovery, self.run)
        store_contract(self.store, self.job_id, propose_contract(goal, discovery, baseline))
        if not eta:  # the ETA has its own replays; here it would add peer priors from the machine's history
            from steward import efficiency_watch

            self.store.kv_set(self.job_id, efficiency_watch.SWITCH_KEY, "off")
        self.worker = ScriptedWorker()
        self.runner = Runner(self.store, lambda ref: self.worker, self.run,
                             FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="go", hypothesis="h")),
                             clock=lambda: self.t.timestamp())
        deadline.revise(self.store, self.job_id, action="create", judgment=Judgment.human, origin="cli",
                        request_id="replay-rev1", reason="initial", due=deadline.parse_due(when, now=start,
                                                                                           tz="America/Los_Angeles"),
                        core_outcome=goal)
        if mode != "shadow":
            deadline.set_mode(self.store, self.job_id, mode, judgment=Judgment.human, origin="cli",
                              request_id="replay-mode")
        self.runner.start(self.job_id)  # launched after the commitment, as `new --deadline` does
        self.due = deadline_plan._utc(deadline.current(self.store, self.job_id)["due_at_utc"])

    # -- time and work ------------------------------------------------------------------------------------
    def at(self, minutes: float) -> "Replay":
        self.t = T0 + timedelta(minutes=minutes)
        return self

    def commit(self, files: dict, msg: str) -> str:
        with git_time(self.t):
            for path, content in files.items():
                p = self.wt / path
                if content is None:
                    _git(self.wt, "rm", "-q", path)
                    continue
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)
                _git(self.wt, "add", path)
            _git(self.wt, "commit", "-q", "-m", msg)
        return _git(self.wt, "rev-parse", "HEAD").strip()

    def plan(self, *, m1: float, cand: float, m2: float | None = None, rev: int | None = None, items=None,
             extra: dict | None = None) -> str:
        rev = rev or deadline.current(self.store, self.job_id)["rev"]
        ms = [{"id": "M1", "title": "importer", "items": ["R1"], "target_at": (T0 + timedelta(minutes=m1)).isoformat(),
               "evidence": "commit", "paths": ["importer.py"]}]
        if m2 is not None:
            ms.append({"id": "M2", "title": "tests", "items": ["R1"], "target_at": (T0 + timedelta(minutes=m2)).isoformat(),
                       "evidence": "commit", "paths": ["tests/test_importer.py"], "depends_on": ["M1"]})
        ms.append({"id": "candidate", "title": "review-ready", "target_at": (T0 + timedelta(minutes=cand)).isoformat(),
                   "evidence": "verify"})
        body = {"v": 1, "commitment_rev": rev, "approach": "stdlib csv",
                "items": items if items is not None else [
                    {"id": "R1", "class": "required", "evidence": {"paths": ["importer.py"],
                                                                   "tests": ["tests/test_importer.py::test_rows"]}},
                    {"id": "O1", "class": "optional"}],
                "milestones": ms, "adaptations": [], "tradeoffs": [], **(extra or {})}
        return self.commit({deadline_plan.plan_path(self.job_id): json.dumps(body, indent=1)}, "deadline: plan")

    def implement(self, *, with_test: bool = True) -> str:
        files = {"importer.py": "def rows(text):\n    return [line.split(',') for line in text.splitlines()]\n"}
        if with_test:
            files["tests/test_importer.py"] = ("from importer import rows\n\n\ndef test_rows():\n"
                                               "    assert rows('a,b') == [['a', 'b']]\n")
        return self.commit(files, "importer")

    def cleanup(self) -> str:
        return self.commit({deadline_plan.plan_path(self.job_id): None}, "deadline: remove transient plan")

    # -- the supervised loop -------------------------------------------------------------------------------
    def tick(self):
        return self.runner.tick(self.job_id)

    def control(self, store: Store | None = None) -> int:
        """The control pane's periodic sweep (its own Store connection, like its own process)."""
        return deadline_status.sweep_all(store or Store(self.home), self.run, now=self.t)

    def finish(self):
        self.worker.done()
        return self.tick()

    # -- restart, approval and delivery (Phase 22) --------------------------------------------------------------
    def restart(self) -> "Replay":
        """A controller restart: a fresh Store connection and a fresh Runner reattached to the same worker."""
        self.store = Store(self.home)
        self.runner = Runner(self.store, lambda ref: self.worker, self.run,
                             FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="go", hypothesis="h")),
                             clock=lambda: self.t.timestamp())
        self.runner.resume_all([self.job_id])
        return self

    def approve(self, draft_key: str, rid: str) -> dict:
        from steward import deadline_outbox

        shown = deadline_outbox.show(self.store, draft_key)
        return deadline_outbox.approve(self.store, draft_key, expect_version=shown["version"],
                                       attempt=shown["next_attempt"], request_id=rid, origin="cli",
                                       judgment=Judgment.human, now=self.t)

    def deliver(self, notifier, store: Store | None = None) -> list:
        from steward import deadline_outbox

        return deadline_outbox.deliver(store or self.store, notifier, now_s=self.t.timestamp())

    def drafts(self, kind: str | None = None) -> list:
        from steward import deadline_outbox

        return [d for d in deadline_outbox.drafts(self.store, self.job_id) if kind is None or d["kind"] == kind]

    # -- facts -----------------------------------------------------------------------------------------------
    def events(self, prefix: str = "deadline_") -> list:
        return [e for e in self.store.events(self.job_id) if e.what.startswith(prefix)]

    def whats(self, what: str) -> list:
        return [e for e in self.store.events(self.job_id) if e.what == what]

    def state(self) -> JobState:
        return self.store.get_job(self.job_id).state

    def project(self) -> dict:
        return deadline_status.project(self.store, self.job_id, self.t)

    def digest(self, extra: list | None = None) -> str:
        rows = []
        for e in self.store.events(self.job_id):
            if e.what.startswith(("deadline_", "creator_notify", "notify_")) or e.rule in (
                    "acceptance_unresolved", "checks_pass_at_sha", "steward_done_marker"):
                rows.append([e.what, e.rule, e.judgment.value, _normalize(e.evidence, self)])
        blob = json.dumps({"events": rows, "extra": _normalize(extra or [], self)}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()


def _normalize(obj, replay: Replay):
    if isinstance(obj, dict):
        return {k: _normalize(v, replay) for k, v in sorted(obj.items()) if k not in _WALL_KEYS}
    if isinstance(obj, list):
        return [_normalize(v, replay) for v in obj]
    if isinstance(obj, str):
        out = obj.replace(replay.job_id, "<job>").replace(replay.job_id[:8], "<job8>")
        out = out.replace(str(replay.home), "<home>").replace(str(replay.src), "<src>")
        out = re.sub(r"approval [0-9a-f]{8}", "approval <id>", out)  # approval ids are random by design
        return re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+\+00:00", "<wall>", out)
    return obj
