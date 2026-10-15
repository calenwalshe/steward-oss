"""The Runner's question scan triggers (Phase 17, G1): tick when HEAD moves, every closeout, never a broken tick."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from steward import heartbeat, questions
from steward.contract import Contract, store_contract
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.models import JobState, Judgment
from steward.runner import Runner
from steward.runner_exec import CommandResult, FakeRunner, SubprocessRunner
from steward.store import Store
from steward.worker import FakeWorker

FIX = Path(__file__).parent / "fixtures" / "questions"
CTX = ".planning/phases/16-efficiency-watcher/16-CONTEXT.md"


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


def _commit(wt, rel, text):
    p = Path(wt) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "c")


class Recording:
    """A questions_runner that records every call and delegates to real git on the tmp worktree."""

    def __init__(self):
        self.real = SubprocessRunner()
        self.calls = []

    def run(self, argv, cwd, **kw):
        self.calls.append(list(argv))
        return self.real.run(argv, cwd, **kw)


class Exploding:
    def run(self, argv, cwd, **kw):
        raise RuntimeError("git binary vanished")


def _exec_runner(wt):
    """The worker-facing runner_exec: `git rev-parse HEAD` answers with the real head of the tmp worktree."""

    def respond(argv, cwd):
        out = _git(wt, "rev-parse", "HEAD") + "\n" if argv[:3] == ["git", "rev-parse", "HEAD"] else ""
        return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=out, stderr="", duration_s=0.0)

    return FakeRunner(respond)


def _setup(steward_home, *, worktree=True, questions_runner=None):
    store = Store(steward_home)
    job = store.create_job("discuss", "/nonexistent")
    store_contract(store, job.job_id, Contract(goal="discuss", boundaries=[], done_when=["done"], next_step="",
                                               authority=[], assumptions=[]))
    wt = store.home / "worktrees" / job.job_id
    if worktree:
        wt.mkdir(parents=True)
        _git(wt, "init", "-q")
        _commit(wt, "README.md", "x\n")
        store.add_check(job.job_id, commit_sha=_git(wt, "rev-parse", "HEAD"), command="p", exit_code=0, results={},
                        label="baseline")
    ticks = iter(range(1000, 100000))
    runner = Runner(store, lambda ref: FakeWorker(script=[]), _exec_runner(wt) if worktree else FakeRunner(),
                    FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="g", hypothesis="h")),
                    clock=lambda: float(next(ticks)), questions_runner=questions_runner)
    runner.start(job.job_id)
    return store, job, wt, runner


def test_default_questions_runner_is_separate_from_runner_exec(steward_home):
    store, job, wt, runner = _setup(steward_home)
    assert isinstance(runner.questions_runner, SubprocessRunner) and runner.questions_runner is not runner.runner_exec


def test_tick_scans_only_when_head_moves(steward_home):
    rec = Recording()
    store, job, wt, runner = _setup(steward_home, questions_runner=rec)
    _commit(wt, CTX, (FIX / "phase16_context_before.md").read_text())
    runner.tick(job.job_id)
    assert len(questions.load(store, job.job_id)) == 15
    n = len(rec.calls)
    assert n > 0
    runner.tick(job.job_id)
    assert len(rec.calls) == n  # the same head: not one git call through questions_runner
    _commit(wt, "notes.txt", "more\n")
    runner.tick(job.job_id)
    assert len(rec.calls) > n
    assert len(questions.load(store, job.job_id)) == 15


def _patch_closeout(monkeypatch, runner, store, job, final_state, order):
    import steward.runner as runner_mod

    monkeypatch.setattr(runner, "_finish_line_gate", lambda job_id, cls: None)
    monkeypatch.setattr(runner_mod, "discover_repo", lambda wt: None)
    monkeypatch.setattr(runner_mod, "verify_worker", lambda *a, **k: SimpleNamespace(
        new_failures=[], dirty=False, commit_sha="x"))
    monkeypatch.setattr(runner_mod.weakening, "detect", lambda *a, **k: [])

    def fake_closeout(store_, job_id, verification, findings, contract):
        if final_state == JobState.NEEDS_HUMAN:  # ACCEPTED needs a human judgment in the real closeout; not needed here
            store_.set_state(job_id, final_state, what="closeout", evidence={}, rule="acceptance_unresolved",
                             judgment=Judgment.code)
        return final_state

    monkeypatch.setattr(runner_mod.closeout, "closeout", fake_closeout)

    def spy(job_id, state):
        order.append(("notify", len(questions.load(store, job_id))))

    monkeypatch.setattr(runner, "_notify_closeout_result", spy)


def test_closeout_scans_with_force_before_the_notification(steward_home, monkeypatch):
    for final in (JobState.NEEDS_HUMAN, JobState.ACCEPTED):
        rec = Recording()
        store, job, wt, runner = _setup(steward_home, questions_runner=rec)
        runner.tick(job.job_id)  # nothing committed yet; the scanned head is now recorded
        # a questions file committed just before the done marker, at a head the tick loop never saw
        _commit(wt, CTX, (FIX / "phase16_context_before.md").read_text())
        order = []
        _patch_closeout(monkeypatch, runner, store, job, final, order)
        report = runner._run_closeout(job.job_id, heartbeat.Class.idle)
        assert report.action == "closeout" and report.detail == f"closeout -> {final.value}"
        assert order == [("notify", 15)]  # recorded BEFORE the notification went out
        assert len(questions.load(store, job.job_id)) == 15


def test_closeout_forces_a_rescan_at_the_same_head(steward_home, monkeypatch):
    rec = Recording()
    store, job, wt, runner = _setup(steward_home, questions_runner=rec)
    _commit(wt, "1-CONTEXT.md", "### G1 T — OPEN FOR SAM\nq\n")
    runner.tick(job.job_id)
    n = len(rec.calls)
    _patch_closeout(monkeypatch, runner, store, job, JobState.NEEDS_HUMAN, [])
    runner._run_closeout(job.job_id, heartbeat.Class.idle)
    assert len(rec.calls) > n  # force: the unchanged head is scanned again at closeout


def test_notify_reason_ignores_question_events(steward_home):
    """The closeout notification reads the last event for WHY the job stopped; the scan's events must not hide it."""
    store, job, wt, runner = _setup(steward_home)
    store.set_state(job.job_id, JobState.NEEDS_HUMAN, what="checks passed; acceptance unresolved",
                    evidence={"reasons": ["x"]}, rule="acceptance_unresolved", judgment=Judgment.code)
    store.append_event(job.job_id, what="questions_found", evidence={}, rule="questions_scan", judgment=Judgment.code)
    sent = []
    runner._notify = lambda job_id, kind, title, body: sent.append(kind)
    runner._notify_closeout_result(job.job_id, JobState.NEEDS_HUMAN)
    assert sent == ["READY"]


def test_missing_worktree_and_scan_failure_never_break_the_tick(steward_home):
    rec = Recording()
    store, job, wt, runner = _setup(steward_home, worktree=False, questions_runner=rec)
    report = runner.tick(job.job_id)
    assert report.job_id == job.job_id and rec.calls == []
    assert not [e for e in store.events(job.job_id) if e.what.startswith("question")]

    store2, job2, wt2, runner2 = _setup(steward_home, questions_runner=Exploding())
    _commit(wt2, "1-CONTEXT.md", "### G1 T — OPEN FOR SAM\nq\n")
    report2 = runner2.tick(job2.job_id)
    assert report2.job_id == job2.job_id
    errs = [e for e in store2.events(job2.job_id) if e.what == "questions_scan_error"]
    assert len(errs) == 1 and "git binary vanished" in errs[0].evidence["error"]
    runner2.tick(job2.job_id)
    assert len([e for e in store2.events(job2.job_id) if e.what == "questions_scan_error"]) == 1  # once per message
