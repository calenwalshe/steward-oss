"""Replay a content-free planning timeline (tests/fixtures/eta/*.json) into a real tmp git repo (phase 18-02, G21).

`materialize` builds the repo up to a time, `advance` commits the next pending entries, `setup_job` puts the repo where a
job's worktree lives and records the baseline check, `replay_events` writes the job's attempt / done / send-back events
on the timeline's clock. File contents are generated from facts only (phase counts, progress numbers, one fixed line).
"""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from steward import chat as chat_module
from steward import store as store_module
from steward.models import Judgment

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "eta"
MARKER = "eta_fixture_index"
ROADMAP = ".planning/ROADMAP.md"
STATE = ".planning/STATE.md"


def load_timeline(job: str) -> dict:
    return json.loads((FIXTURES / f"{job}.json").read_text(encoding="utf-8"))


def _env(epoch: float) -> dict:
    stamp = f"@{int(epoch)} +0000"
    return {**os.environ, "GIT_AUTHOR_NAME": "eta", "GIT_AUTHOR_EMAIL": "eta@example.invalid",
            "GIT_COMMITTER_NAME": "eta", "GIT_COMMITTER_EMAIL": "eta@example.invalid",
            "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}


def _git(repo, *args, epoch: float = 0, check: bool = True, rc: bool = False):
    res = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                         cwd=str(repo), env=_env(epoch), capture_output=True, text=True, check=check)
    return res.returncode if rc else res.stdout.strip()


def render_roadmap(phases: dict | None) -> str:
    out = ["# Roadmap", ""]
    for ph, n in (phases or {}).items():
        out += [f"### Phase {ph}: Phase {ph}", "", f"**Plans**: {n} plans" if n is not None else "**Plans**: TBD", ""]
    return "\n".join(out)


def render_state(progress: dict | None) -> str:
    out = ["---", "progress:"]
    out += [f"  {k}: {v}" for k, v in (progress or {}).items()]
    out += ["---", "", "# State", ""]
    return "\n".join(out)


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def materialize(timeline: dict, repo_dir, *, until: float | None = None) -> str:
    """git init, an initial commit of the base files at t0 - 60 s, then every entry with at <= until. Returns the base sha."""
    repo = Path(repo_dir)
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    t0 = timeline["t0"]
    base = timeline["base"]
    for rel in base["files"]:
        _write(repo, rel, "plan file\n")
    if base.get("roadmap") is not None:
        _write(repo, ROADMAP, render_roadmap(base["roadmap"]))
    if base.get("state") is not None:
        _write(repo, STATE, render_state(base["state"]))
    _write(repo, ".work", "base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base", epoch=t0 - 60)
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / ".git" / MARKER).write_text("0", encoding="utf-8")
    advance(repo, timeline, until)
    return base_sha


def advance(repo_dir, timeline: dict, until: float | None) -> int:
    """Commit the next pending entries with at <= until. Returns how many were committed."""
    repo = Path(repo_dir)
    marker = repo / ".git" / MARKER
    index = int(marker.read_text(encoding="utf-8").strip() or 0)
    committed = 0
    commits = timeline["commits"]
    while index < len(commits) and (until is None or commits[index]["at"] <= until):
        entry = commits[index]
        touched = False
        for rel in entry["add"] + entry["modify"]:
            if rel == ROADMAP:
                text = render_roadmap(entry["roadmap"])
            elif rel == STATE:
                text = render_state(entry["state"])
            else:
                text = "plan file\n" if rel in entry["add"] else f"plan file\nrevision {index}\n"
            _write(repo, rel, text)
            touched = True
        for rel in entry["delete"]:
            if (repo / rel).exists():
                (repo / rel).unlink()
                touched = True
        if not touched:
            _write(repo, ".work", f"work {index}\n")
        _git(repo, "add", "-A")
        if _git(repo, "diff", "--cached", "--quiet", check=False, rc=True) == 0:  # a rendered file did not change
            _write(repo, ".work", f"work {index}\n")
            _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", entry["subject"], epoch=entry["at"])
        index += 1
        committed += 1
        marker.write_text(str(index), encoding="utf-8")
    return committed


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


@contextmanager
def _clock(epoch: float):
    with mock.patch.object(store_module, "now_iso", lambda: _iso(epoch)):
        yield


def fake_now(monkeypatch, getter) -> None:
    """steward.store.now_iso answers the ISO form of getter() (a timeline time) until the test ends."""
    monkeypatch.setattr(store_module, "now_iso", lambda: _iso(getter()))


def setup_job(store, timeline: dict, *, until: float | None = None):
    """A job with the timeline's id and goal, its worktree materialised where chat.worktree_path says, a baseline check."""
    with _clock(timeline["t0"]):
        job = store.create_job(timeline["goal"], "/nonexistent/eta-repo", job_id=timeline["job"])
        base = materialize(timeline, chat_module.worktree_path(store, job.job_id), until=until)
        store.add_check(job.job_id, label="baseline", commit_sha=base, command="baseline", exit_code=0, results={})
    return job, base


def _event_entries(timeline: dict) -> list[tuple[float, str, dict]]:
    rows = [(a["at"], "worker attempt started", {"attempt_no": a["no"], "rework": a["rework"]}) for a in timeline["attempts"]]
    rows += [(t, "worker reported done (typed frame)", {}) for t in timeline["done"]]
    rows += [(t, "sent back for rework; new attempt pending", {}) for t in timeline["sent_back"]]
    return sorted(rows, key=lambda r: r[0])


def replay_events(store, job_id: str, timeline: dict, until: float) -> int:
    """Append the timeline's events at or before `until` that are not recorded yet, on the timeline's clock."""
    done = int(store.kv_get(job_id, MARKER, 0) or 0)
    rows = _event_entries(timeline)
    n = 0
    while done < len(rows) and rows[done][0] <= until:
        at, what, evidence = rows[done]
        with _clock(at):
            store.append_event(job_id, what=what, evidence=evidence, rule="eta_fixture", judgment=Judgment.code)
        done += 1
        n += 1
    store.kv_set(job_id, MARKER, done)
    return n
