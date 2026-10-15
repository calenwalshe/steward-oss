"""RC-4 (docs/diagnostics/2026-10-11-stuck-after-sendback): untracked scratch never blocks a done; tracked changes do.
The runner's fake git here honours `--untracked-files=no` the way git does (the older fakes ignore flags)."""
from __future__ import annotations

from test_runner import _contracted_job, _Factory, _fake_diagnoser, _Clock

from steward.runner import Runner
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store
from steward.worker import WorkerObservation


def _git(*, modified=(), untracked=()):
    def respond(argv, cwd):
        out = ""
        if argv[:2] == ["git", "merge-base"]:
            out = "base-0\n"
        elif argv[:2] == ["git", "rev-parse"]:
            out = "sha-1\n"
        elif argv[:2] == ["git", "status"]:
            out = "".join(f" M {m}\n" for m in modified)
            if "--untracked-files=no" not in argv:
                out += "".join(f"?? {u}\n" for u in untracked)
        return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=out, stderr="", duration_s=0.0)
    return FakeRunner(respond)


def _yield(steward_home, git):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _Factory(
        scripts=[[WorkerObservation(lifecycle="idle", output_tail="STEWARD-DONE", pid_alive=True, ts="t")]])
    runner = Runner(store, factory, git, _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    runner._worktree(job.job_id).mkdir(parents=True, exist_ok=True)
    runner.tick(job.job_id)
    (yc,) = [e for e in store.events(job.job_id) if e.what == "yield_classified"]
    return yc.evidence


def test_untracked_scratch_does_not_block_the_done_and_is_listed(steward_home):
    ev = _yield(steward_home, _git(untracked=("t/full-suite.log",)))
    assert ev["lane"] == "CANDIDATE" and ev["failed_preconditions"] == []
    assert ev["untracked"] == ["t/full-suite.log"]


def test_tracked_change_is_dirty_and_untracked_still_listed(steward_home):
    ev = _yield(steward_home, _git(modified=("notes.py",), untracked=("t/suite.log",)))
    assert ev["lane"] == "STOPPED" and ev["failed_preconditions"] == ["dirty_worktree"]
    assert ev["untracked"] == ["t/suite.log"]


def test_clean_tree_carries_no_untracked_key(steward_home):
    assert "untracked" not in _yield(steward_home, _git())
