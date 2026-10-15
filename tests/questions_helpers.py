"""Shared helpers for the question-queue tests (plans 17-02 to 17-07): full G2 records, seeding, and a real scan."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from steward import questions
from steward.runner_exec import SubprocessRunner
from steward.store import Store

FIX = Path(__file__).parent / "fixtures" / "questions"
CTX = ".planning/phases/16-efficiency-watcher/16-CONTEXT.md"
LOG = ".planning/phases/16-efficiency-watcher/16-DISCUSSION-LOG.md"

_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **_ENV}).stdout.strip()


def commit_file(wt, rel: str, text: str, msg: str = "add") -> str:
    p = Path(wt) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", msg)
    return git(wt, "rev-parse", "HEAD")


def fixture_text(name: str) -> str:
    return (FIX / name).read_text()


def make_record(qid: str = "gsd_context:17-CONTEXT.md:G1", options=("a", "b", "c"), recommended=("a",),
                status: str = "open", starred: bool = True, blocking: bool = False, *, file: str = "17-CONTEXT.md",
                line: int = 1, adapter: str = "gsd_context", title: str | None = None, unparsed: bool = False,
                hash: str = "h1") -> dict:
    """A full G2 record (every field plan 17-01's `_new_record` writes)."""
    anchor = qid.rsplit(":", 1)[-1]
    opts = [{"key": k, "label": f"Option {k}", "detail": f"detail {k}"} for k in options]
    keys = [k for k in recommended if k in options]
    return {
        "qid": qid, "adapter": adapter, "title": title or f"Question {anchor}", "question": f"What about {anchor}?",
        "options": opts, "recommended": keys,
        "recommended_text": " + ".join(f"Option {k}" for k in keys) if keys else "as written",
        "starred": bool(starred), "rationale": "because", "evidence": ["steward/x.py:1"],
        "blocking": bool(blocking), "blocking_reason": "the worker waits" if blocking else "",
        "source": {"file": file, "anchor": anchor, "commit": "c0ffee", "hash": hash, "line": line},
        "status": status, "answer": None,
        "flags": {"updated": False, "answered_by_worker": False, "unparsed": bool(unparsed)},
        "first_seen_at": "2026-09-30T00:00:00+00:00", "updated_at": "2026-09-30T00:00:00+00:00",
        "written": None, "history": [],
    }


def seed(store: Store, job_id: str, records: list[dict]) -> list[dict]:
    questions.save(store, job_id, records)
    return records


def make_store_job() -> tuple[Store, str]:
    store = Store()
    job = store.create_job("answer questions", "/nonexistent-repo")
    return store, job.job_id


def scan_fixture(store: Store, job_id: str, tmp_path=None, name: str = "phase16_context_before.md",
                 log: str | None = "phase16_log_before.md") -> Path:
    """A real git worktree under store.home/worktrees/<job_id> with a baseline check, the fixture committed at the
    Phase 16 CONTEXT path (and the sibling log when `log` is given), then questions.scan. Returns the worktree."""
    wt = store.home / "worktrees" / job_id
    wt.mkdir(parents=True)
    git(wt, "init", "-q")
    base = commit_file(wt, "README.md", "hello\n", "baseline")
    store.add_check(job_id, commit_sha=base, command="pytest", exit_code=0, results={}, label="baseline")
    if log:
        (wt / LOG).parent.mkdir(parents=True, exist_ok=True)
        (wt / LOG).write_text(fixture_text(log))
    commit_file(wt, CTX, fixture_text(name), "add context")
    res = questions.scan(store, job_id, SubprocessRunner())
    assert res.error is None, res.error
    return wt


def scan_files(store: Store, job_id: str, files: dict[str, str]) -> Path:
    """Like scan_fixture for arbitrary committed files ({relative path: text}), one commit, then questions.scan."""
    wt = store.home / "worktrees" / job_id
    wt.mkdir(parents=True)
    git(wt, "init", "-q")
    base = commit_file(wt, "README.md", "hello\n", "baseline")
    store.add_check(job_id, commit_sha=base, command="pytest", exit_code=0, results={}, label="baseline")
    for rel, text in files.items():
        p = wt / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "add question files")
    res = questions.scan(store, job_id, SubprocessRunner())
    assert res.error is None, res.error
    return wt


PHASE16_NOTE = ("before acceptance the efficiency watcher must also run observe-only (T0) on at least one live Steward "
                "job for at least one unit boundary, with its findings shown on the status pane and no nudges sent.")
PHASE16_DATE = datetime(2026, 9, 29, 10, 0, 0)


def set_state(store: Store, job_id: str, state) -> None:
    """Put a job in any state (tests only): the same UPDATE tests/test_decision_buttons.py uses."""
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job_id))


def phase16_qid(n: int) -> str:
    return f"gsd_context:16-CONTEXT.md:G{n}"


def answered_phase16(store: Store, job_id: str, *, origin: str = "cli") -> dict:
    """Sam's real Phase 16 answers: G1-G14 recommended, G15 `replay + live trial` with the note. `now` is fixed so
    the rendered date is 2026-09-29; the stored `answer.at` is the real clock (batching tests use `answered_at`)."""
    spec = [{"qid": phase16_qid(n), "option": "recommended"} for n in range(1, 15)]
    spec.append({"qid": phase16_qid(15), "other": "replay + live trial", "note": PHASE16_NOTE})
    return questions.apply(store, job_id, answers=spec, origin=origin, now=PHASE16_DATE)


def answered_at(store: Store, job_id: str, qid: str, epoch: float) -> None:
    """Move one answer's `answer.at` to `epoch` (batching tests)."""
    from datetime import timezone

    records = questions.load(store, job_id)
    for r in records:
        if r["qid"] == qid and r.get("answer"):
            r["answer"]["at"] = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    questions.save(store, job_id, records)


def age_all_answers(store: Store, job_id: str, seconds: float = 3600.0) -> None:
    for r in questions.load(store, job_id):
        if r.get("answer"):
            answered_at(store, job_id, r["qid"], time.time() - seconds)


def events_named(store: Store, job_id: str, what: str) -> list:
    return [e for e in store.events(job_id) if e.what == what]


def linked_job(tmp_path, *, name: str = "src", log: str | None = "phase16_log_before.md"):
    """A real source repo on `main` plus the job's linked worktree on `steward/<job_id>` (as worktree.py cuts it), with
    the Phase 16 CONTEXT committed and scanned. Returns (store, job_id, repo, worktree)."""
    store = Store()
    repo = Path(tmp_path) / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Sam")
    git(repo, "config", "user.email", "c@x")
    (repo / "README.md").write_text("hello\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    job = store.create_job("discuss phase 16", str(repo))
    wt = store.home / "worktrees" / job.job_id
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-q", "-b", f"steward/{job.job_id}", str(wt), "HEAD")
    if log:
        (wt / LOG).parent.mkdir(parents=True, exist_ok=True)
        (wt / LOG).write_text(fixture_text(log))
    commit_file(wt, CTX, fixture_text("phase16_context_before.md"), "add context")
    res = questions.scan(store, job.job_id, SubprocessRunner())
    assert res.error is None, res.error
    return store, job.job_id, repo, wt


def running_job(store: Store, goal: str = "Ship the widget", state=None) -> str:
    """A job the status pane lists with a tab and workspace (tests only); `state` defaults to RUNNING."""
    from steward.models import JobState

    job = store.create_job(goal, "/tmp/somerepo")
    set_state(store, job.job_id, state or JobState.RUNNING)
    store.kv_set(job.job_id, "herdr_workspace", "w1")
    store.kv_set(job.job_id, "herdr_tab", "w1:t2")
    store.kv_set(job.job_id, "worker_model", "claude-sonnet-5-5")
    return job.job_id


def seed_queue(store: Store, job_id: str, starred: int, unstarred: int = 0, prefix: str = "Q") -> list[dict]:
    """`starred` + `unstarred` open records (qids `<prefix>1`...), starred ones first."""
    recs = []
    for i in range(starred + unstarred):
        recs.append(make_record(f"gsd_context:{prefix}-CONTEXT.md:G{i + 1}", starred=i < starred, line=i + 1,
                                file=f"{prefix}-CONTEXT.md"))
    return seed(store, job_id, recs)
