"""Phase 40 (SRD-01/02): the yield classifier is pure over store + git + frame facts; only CANDIDATE is completion."""
from __future__ import annotations

from steward import waits, yield_lanes
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store

START, HEAD = "a" * 40, "b" * 40


def _git(*, head=HEAD, porcelain="", fail=False):
    def respond(argv, cwd):
        if fail:
            return CommandResult(argv=argv, cwd=cwd, exit_code=128, stdout="", stderr="boom", duration_s=0.0)
        if argv[:2] == ["git", "status"] and "--untracked-files=no" in argv:  # git hides `??` lines under this flag
            out = "".join(line + "\n" for line in porcelain.splitlines() if not line.startswith("??"))
        else:
            out = head + "\n" if argv[:2] == ["git", "rev-parse"] else porcelain if argv[:2] == ["git", "status"] else ""
        return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=out, stderr="", duration_s=0.0)
    return FakeRunner(respond)


def _classify(store, job, **kw):
    git = kw.pop("git", None) or _git()
    kw.setdefault("start_sha", START)
    return yield_lanes.classify(store, job.job_id, run=git.run, worktree="/wt", **kw)


def _job(steward_home):
    store = Store(steward_home)
    return store, store.create_job("g", "/repo")


def test_candidate_when_every_precondition_holds(steward_home):
    store, job = _job(steward_home)
    c = _classify(store, job)
    assert (c.lane, c.sha, c.failed_preconditions) == ("CANDIDATE", HEAD, [])


def test_untracked_only_tree_is_not_dirty(steward_home):
    """RC-4: untracked scratch (logs, temp profiles) never blocks a done; verification runs at the committed SHA."""
    store, job = _job(steward_home)
    c = _classify(store, job, git=_git(porcelain="?? t/full-suite.log\n?? t/com.google.Chrome.x/\n"))
    assert (c.lane, c.failed_preconditions) == ("CANDIDATE", [])
    assert c.untracked == ["t/full-suite.log", "t/com.google.Chrome.x/"]  # still visible as evidence


def test_tracked_modification_is_dirty(steward_home):
    store, job = _job(steward_home)
    c = _classify(store, job, git=_git(porcelain=" M a.py\n?? scratch.log\n"))
    assert c.lane == "STOPPED" and c.failed_preconditions == ["dirty_worktree"]
    assert c.untracked == ["scratch.log"]


def test_untracked_evidence_is_capped(steward_home):
    store, job = _job(steward_home)
    c = _classify(store, job, git=_git(porcelain="".join(f"?? f{i}\n" for i in range(50))))
    assert c.lane == "CANDIDATE" and len(c.untracked) == yield_lanes.UNTRACKED_CAP == 20


def test_dirty_tree_is_stopped(steward_home):
    store, job = _job(steward_home)
    c = _classify(store, job, git=_git(porcelain=" M a.py\n"))
    assert c.lane == "STOPPED" and c.failed_preconditions == ["dirty_worktree"]


def test_same_sha_as_attempt_start_is_stopped(steward_home):
    store, job = _job(steward_home)
    c = _classify(store, job, start_sha=HEAD)
    assert c.lane == "STOPPED" and c.failed_preconditions == ["no_new_commit"]


def test_unknown_start_sha_is_stopped_never_candidate(steward_home):
    store, job = _job(steward_home)
    c = _classify(store, job, start_sha=None)
    assert c.lane == "STOPPED" and "start_sha_unknown" in c.failed_preconditions


def test_same_sha_as_last_ruled_is_stopped_claims_done(steward_home):
    store, job = _job(steward_home)
    store.kv_set(job.job_id, yield_lanes.LAST_RULED_KEY, HEAD)
    c = _classify(store, job)
    assert c.lane == "STOPPED" and c.failed_preconditions == ["same_as_last_ruled"]
    assert c.label == "claims_done"


def test_unacked_steering_is_stopped(steward_home):
    store, job = _job(steward_home)
    c = _classify(store, job, unacked=[3])
    assert c.lane == "STOPPED" and c.failed_preconditions == ["unacked_steering"]


def test_registered_wait_is_waiting(steward_home):
    store, job = _job(steward_home)
    waits.park(store, job.job_id, blocker_id=None, kind="time", params={"at": 9e12}, reason="r", now=1.0)
    c = _classify(store, job)
    assert c.lane == "WAITING"


def test_open_blocking_question_is_asking(steward_home):
    store, job = _job(steward_home)
    store.kv_set(job.job_id, "questions", [{"qid": "q1", "status": "open", "blocking": True}])
    c = _classify(store, job)
    assert c.lane == "ASKING"


def test_non_blocking_question_does_not_make_asking(steward_home):
    store, job = _job(steward_home)
    store.kv_set(job.job_id, "questions", [{"qid": "q1", "status": "open", "blocking": False}])
    assert _classify(store, job).lane == "CANDIDATE"


def test_git_error_is_stopped_never_candidate(steward_home):
    store, job = _job(steward_home)
    c = _classify(store, job, git=_git(fail=True))
    assert c.lane == "STOPPED" and "git_error" in c.failed_preconditions


def test_errored_turn_is_stopped_errored(steward_home):
    store, job = _job(steward_home)
    c = _classify(store, job, errored=True)
    assert c.lane == "STOPPED" and c.failed_preconditions == ["errored"] and c.label == "errored"


def test_waiting_beats_asking_beats_candidate(steward_home):
    store, job = _job(steward_home)
    store.kv_set(job.job_id, "questions", [{"qid": "q1", "status": "open", "blocking": True}])
    waits.park(store, job.job_id, blocker_id=None, kind="time", params={"at": 9e12}, reason="r", now=1.0)
    assert _classify(store, job).lane == "WAITING"
