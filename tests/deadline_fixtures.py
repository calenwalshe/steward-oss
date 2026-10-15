"""Shared fixtures for the v2.2 deadline tests: a real temp git repo + job worktree, and commitment helpers."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from steward import deadline
from steward.models import JobState, Judgment

TZ = "America/Los_Angeles"


def git(cwd, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd)}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


def set_state(store, job_id, state: JobState) -> None:
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job_id))


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Repo:
    """A source repo with one commit and a job worktree on `steward/<job>` at `$STEWARD_HOME/worktrees/<job>`."""

    def __init__(self, tmp_path: Path, store, goal="Add a CSV importer. Optional: CSV export.", *, commit_at=None):
        self.store = store
        self.src = tmp_path / "src"
        self.src.mkdir()
        git(self.src, "init", "-q", "-b", "main")
        (self.src / "README.md").write_text("hi\n")
        git(self.src, "add", "README.md")
        git(self.src, "commit", "-q", "-m", "init")
        self.base = git(self.src, "rev-parse", "HEAD").strip()
        self.job = store.create_job(goal, str(self.src))
        self.job_id = self.job.job_id
        self.wt = store.home / "worktrees" / self.job_id
        self.wt.parent.mkdir(parents=True, exist_ok=True)
        git(self.src, "worktree", "add", "-q", "-b", f"steward/{self.job_id}", str(self.wt))
        store.add_check(self.job_id, commit_sha=self.base, command="pytest -q", exit_code=0, results={},
                        label="baseline")

    def commit(self, files: dict[str, str | None], msg: str, *, when: datetime | None = None) -> str:
        for path, content in files.items():
            p = self.wt / path
            if content is None:
                git(self.wt, "rm", "-q", path)
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            git(self.wt, "add", path)
        args = ["commit", "-q", "-m", msg]
        if when is not None:
            stamp = f"{int(when.timestamp())} +0000"
            subprocess.run(["git", *args], cwd=self.wt, check=True, capture_output=True, text=True,
                           env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                                "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin",
                                "HOME": str(self.wt), "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
        else:
            git(self.wt, *args)
        return self.head()

    def head(self) -> str:
        return git(self.wt, "rev-parse", "HEAD").strip()


def commit_deadline(store, job_id, *, now: datetime, when="+4h", rid=None, goal=None) -> dict:
    job = store.get_job(job_id)
    if job.state in (JobState.CREATED, JobState.CONTRACTED):
        set_state(store, job_id, JobState.RUNNING)
    return deadline.revise(store, job_id, action="create", judgment=Judgment.human, origin="cli",
                           request_id=rid or f"r-{job_id}", reason="initial",
                           due=deadline.parse_due(when, now=now, tz=TZ), core_outcome=goal or job.goal)


def plan_json(job_id: str, rev: int, *, due: datetime, now: datetime, m1_paths=("importer.py",), m1_target=None,
              cand_target=None, items=None, extra: dict | None = None) -> str:
    m1 = m1_target or now + (due - now) / 3
    cand = cand_target or due - timedelta(minutes=20)
    body = {
        "v": 1, "commitment_rev": rev, "approach": "stdlib csv",
        "items": items if items is not None else [
            {"id": "R1", "class": "required", "evidence": {"paths": ["importer.py"],
                                                            "tests": ["tests/test_importer.py::test_rows"]}},
            {"id": "O1", "class": "optional", "evidence": {"paths": ["exporter.py"], "tests": []}},
        ],
        "milestones": [
            {"id": "M1", "title": "parser", "items": ["R1"], "target_at": m1.isoformat(), "evidence": "commit",
             "paths": list(m1_paths)},
            {"id": "candidate", "title": "review-ready", "target_at": cand.isoformat(), "evidence": "verify"},
        ],
        "adaptations": [], "tradeoffs": [],
    }
    body.update(extra or {})
    return json.dumps(body)
