"""questions.scan on real git repos in tmp_path (git on a tmp dir only; no model, no network)."""

import os
import subprocess
from pathlib import Path

from steward import questions
from steward.models import Judgment
from steward.runner_exec import CommandResult, FakeRunner, SubprocessRunner
from steward.store import Store

FIX = Path(__file__).parent / "fixtures" / "questions"
CTX = ".planning/phases/16-efficiency-watcher/16-CONTEXT.md"


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


def _commit_file(wt, rel, text, msg="add"):
    p = Path(wt) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", msg)
    return _git(wt, "rev-parse", "HEAD")


def make_job(steward_home, *, baseline=True, repo="/nonexistent-repo"):
    """A job with a real git worktree under store.home/worktrees/<job_id>, one baseline commit, a baseline check."""
    store = Store()
    job = store.create_job("discuss phase 16", repo)
    wt = store.home / "worktrees" / job.job_id
    wt.mkdir(parents=True)
    _git(wt, "init", "-q")
    base = _commit_file(wt, "README.md", "hello\n", "baseline")
    if baseline:
        store.add_check(job.job_id, commit_sha=base, command="pytest", exit_code=0, results={}, label="baseline")
    return store, job, wt, base


def _before():
    return (FIX / "phase16_context_before.md").read_text()


def _events(store, job, what):
    return [e for e in store.events(job.job_id) if e.what == what]


def test_scan_reads_committed_diff_not_dirty_tree(steward_home):
    store, job, wt, base = make_job(steward_home)
    _commit_file(wt, CTX, _before())
    (wt / CTX).write_text(_before() + "\n### G16 Uncommitted — OPEN FOR SAM\nq\n")
    res = questions.scan(store, job.job_id, SubprocessRunner())
    assert res.error is None and res.skipped is None
    assert len(questions.load(store, job.job_id)) == 15
    assert res.base == base


def test_scan_records_15_cards_summary_and_found_event(steward_home):
    store, job, wt, base = make_job(steward_home)
    head = _commit_file(wt, CTX, _before())
    res = questions.scan(store, job.job_id, SubprocessRunner())
    recs = questions.load(store, job.job_id)
    assert [r["qid"] for r in recs] == [f"gsd_context:16-CONTEXT.md:G{i}" for i in range(1, 16)]
    for r in recs:
        for k in ("qid", "adapter", "title", "question", "options", "recommended", "recommended_text", "starred",
                  "rationale", "evidence", "blocking", "source", "status", "answer", "flags", "written", "history",
                  "first_seen_at", "updated_at"):
            assert k in r, k
        assert r["source"]["file"] == CTX and r["source"]["commit"] == head
        assert r["source"]["anchor"] == r["qid"].rsplit(":", 1)[1] and r["source"]["hash"] and r["source"]["line"]
        assert r["status"] == "open" and r["answer"] is None and r["written"] is None and r["history"] == []
        assert r["flags"] == {"updated": False, "answered_by_worker": False, "unparsed": False}
    ev = _events(store, job, "questions_found")
    assert len(ev) == 1 and ev[0].judgment == Judgment.code
    assert ev[0].evidence["adapter"] == "gsd_context" and ev[0].evidence["file"] == CTX
    assert ev[0].evidence["sha"] == head and len(ev[0].evidence["qids"]) == 15
    assert questions.summary(recs) == {"waiting": 15, "open": 15, "stale": 0, "deferred": 0, "answered": 0,
                                       "withdrawn": 0, "starred": 15, "needs_you": 0, "blocking": 0}
    assert res.summary["waiting"] == 15 and res.queue_changed and res.found == [r["qid"] for r in recs]
    assert store.kv_get(job.job_id, questions.SCANNED_HEAD_KEY) == head


def test_scan_is_head_gated_and_idempotent(steward_home):
    store, job, wt, base = make_job(steward_home)
    _commit_file(wt, CTX, _before())
    questions.scan(store, job.job_id, SubprocessRunner())
    n = len(store.events(job.job_id))
    again = questions.scan(store, job.job_id, SubprocessRunner())
    assert again.skipped == "unchanged" and len(store.events(job.job_id)) == n
    forced = questions.scan(store, job.job_id, SubprocessRunner(), force=True)
    assert forced.skipped is None and forced.found == [] and not forced.queue_changed
    assert len(store.events(job.job_id)) == n
    _commit_file(wt, "notes.txt", "unrelated\n")
    third = questions.scan(store, job.job_id, SubprocessRunner())
    assert third.skipped is None and len(store.events(job.job_id)) == n
    assert len(questions.load(store, job.job_id)) == 15


def test_scan_base_fallbacks_and_skips(steward_home, tmp_path):
    # no baseline check -> merge-base of the job branch in job.repo
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    fork = _commit_file(repo, "README.md", "hi\n")
    store = Store()
    job = store.create_job("g", str(repo))
    wt = store.home / "worktrees" / job.job_id
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", "-b", f"steward/{job.job_id}", str(wt))
    _commit_file(wt, CTX, _before())
    res = questions.scan(store, job.job_id, SubprocessRunner())
    assert res.error is None and res.base == fork and len(questions.load(store, job.job_id)) == 15

    # neither baseline nor a repo: the whole HEAD tree
    store2, job2, wt2, _ = make_job(steward_home, baseline=False)
    _commit_file(wt2, "docs/1-CONTEXT.md", "### G1 T — OPEN FOR SAM\nq\n")
    res2 = questions.scan(store2, job2.job_id, SubprocessRunner())
    assert res2.base == "" and [r["qid"] for r in questions.load(store2, job2.job_id)] == ["gsd_context:1-CONTEXT.md:G1"]

    # no worktree directory: skipped, and not one git call
    job3 = store2.create_job("g", "/x")
    fake = FakeRunner()
    res3 = questions.scan(store2, job3.job_id, fake)
    assert res3.skipped == "no worktree" and fake.calls == []

    # git failing: an error result, one event, no exception
    store4, job4, wt4, _ = make_job(steward_home)
    bad = FakeRunner(lambda argv, cwd: CommandResult(argv=argv, cwd=cwd, exit_code=128, stdout="", stderr="fatal", duration_s=0))
    res4 = questions.scan(store4, job4.job_id, bad)
    assert res4.error and "128" in res4.error
    questions.scan(store4, job4.job_id, bad)
    assert len(_events(store4, job4, "questions_scan_error")) == 1


def test_scan_only_runs_git(steward_home):
    store, job, wt, base = make_job(steward_home)
    _commit_file(wt, CTX, _before())
    real = SubprocessRunner()
    seen = []

    class Spy:
        def run(self, argv, cwd, **kw):
            seen.append(list(argv))
            return real.run(argv, cwd, **kw)

    questions.scan(store, job.job_id, Spy())
    assert seen and all(a[0] == "git" for a in seen)


def test_queue_order_helpers():
    recs = [
        {"qid": "b", "status": "open", "blocking": False, "source": {"file": "z.md", "line": 1}},
        {"qid": "a", "status": "open", "blocking": False, "source": {"file": "a.md", "line": 9}},
        {"qid": "s", "status": "stale", "blocking": False, "source": {"file": "a.md", "line": 1}},
        {"qid": "d", "status": "deferred", "blocking": False, "source": {"file": "a.md", "line": 2}},
        {"qid": "x", "status": "open", "blocking": True, "source": {"file": "z.md", "line": 50}},
    ]
    assert [r["qid"] for r in questions.ordered(recs)] == ["x", "a", "b", "s", "d"]
    assert questions.queue_hash(recs) == questions.queue_hash(list(reversed(recs)))
    assert questions.queue_hash(recs) != questions.queue_hash(recs[:2])


# -- steward_questions through the scanner ------------------------------------------------------------

import json  # noqa: E402


class Spy:
    def __init__(self):
        self.real = SubprocessRunner()
        self.calls = []

    def run(self, argv, cwd, **kw):
        self.calls.append(list(argv))
        return self.real.run(argv, cwd, **kw)


def _qjson(*items):
    return json.dumps({"questions": list(items)})


def test_json_and_fenced_questions_scan_with_one_git_grep(steward_home):
    store, job, wt, base = make_job(steward_home)
    (wt / "docs").mkdir()
    (wt / "docs/plain.md").write_text("nothing here\n")
    (wt / "docs/plan.md").write_text('```steward-questions\n[{"id": "port", "question": "Which port?"}]\n```\n')
    (wt / "STEWARD-QUESTIONS.json").write_text(_qjson({"id": "db", "question": "Which DB?", "blocking": True,
                                                       "blocking_reason": "schema", "options": ["pg", "sqlite"],
                                                       "recommended": "b"}))
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "questions")
    spy = Spy()
    res = questions.scan(store, job.job_id, spy)
    assert res.error is None
    recs = {r["qid"]: r for r in questions.load(store, job.job_id)}
    assert set(recs) == {"steward_questions:STEWARD-QUESTIONS.json:db", "steward_questions:plan.md:port"}
    db = recs["steward_questions:STEWARD-QUESTIONS.json:db"]
    assert db["blocking"] and db["blocking_reason"] == "schema" and db["starred"] and db["recommended"] == ["b"]
    assert sum(1 for c in spy.calls if c[:2] == ["git", "grep"]) == 1
    shows = [c[2] for c in spy.calls if c[:2] == ["git", "show"]]
    assert not any(s.endswith("docs/plain.md") for s in shows)
    assert len(_events(store, job, "questions_found")) == 2  # one per (adapter, file)


def test_two_adapters_share_one_file(steward_home):
    store, job, wt, base = make_job(steward_home)
    text = ("### G1 Area — OPEN FOR SAM\n- Options: (a) x; (b) y\n- **Recommended: (a).**\n\n"
            '```steward-questions\n[{"id": "G1", "question": "Same anchor?"}]\n```\n')
    _commit_file(wt, "docs/3-CONTEXT.md", text)
    questions.scan(store, job.job_id, SubprocessRunner())
    assert sorted(r["qid"] for r in questions.load(store, job.job_id)) == [
        "gsd_context:3-CONTEXT.md:G1", "steward_questions:3-CONTEXT.md:G1"]


def test_same_basename_in_two_dirs_gets_path_qualified_qids(steward_home):
    store, job, wt, base = make_job(steward_home)
    area = "### G1 Area — OPEN FOR SAM\nq\n"
    _commit_file(wt, "one/1-CONTEXT.md", area)
    (wt / "two").mkdir()
    (wt / "two/1-CONTEXT.md").write_text(area)
    (wt / "two/other-CONTEXT.md").write_text(area)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "more")
    questions.scan(store, job.job_id, SubprocessRunner())
    qids = sorted(r["qid"] for r in questions.load(store, job.job_id))
    assert qids == ["gsd_context:one/1-CONTEXT.md:G1", "gsd_context:other-CONTEXT.md:G1",
                    "gsd_context:two/1-CONTEXT.md:G1"]


def test_lone_file_keeps_basename_and_later_twin_is_qualified(steward_home):
    store, job, wt, base = make_job(steward_home)
    area = "### G1 Area — OPEN FOR SAM\nq\n"
    _commit_file(wt, "one/1-CONTEXT.md", area)
    questions.scan(store, job.job_id, SubprocessRunner())
    _commit_file(wt, "two/1-CONTEXT.md", area)
    questions.scan(store, job.job_id, SubprocessRunner())
    assert sorted(r["qid"] for r in questions.load(store, job.job_id)) == [
        "gsd_context:1-CONTEXT.md:G1", "gsd_context:two/1-CONTEXT.md:G1"]


def test_changed_answers_file_rescans_its_questions_file(steward_home):
    store, job, wt, base = make_job(steward_home)
    (wt / "d").mkdir()
    (wt / "d/STEWARD-QUESTIONS.json").write_text(_qjson({"id": "db", "question": "Which DB?"}))
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "q")
    questions.scan(store, job.job_id, SubprocessRunner())
    assert questions.load(store, job.job_id)[0]["status"] == "open"
    seen = {}

    from steward import questions as q

    orig = q.reconcile

    def spy(existing, found_by_file, deleted, head, now):
        seen["files"] = {k: [(f.anchor, f.marker) for f in v] for k, v in found_by_file.items()}
        return orig(existing, found_by_file, deleted, head, now)

    q.reconcile = spy
    try:
        _commit_file(wt, "d/STEWARD-ANSWERS.json", json.dumps(
            {"answers": [{"id": "db", "status": "decided", "option": "recommended", "by": "sam", "date": "2026-09-30"}]}))
        questions.scan(store, job.job_id, SubprocessRunner())
    finally:
        q.reconcile = orig
    (anchor, marker), = seen["files"]["d/STEWARD-QUESTIONS.json"]
    assert anchor == "db" and marker["state"] == "decided" and marker["text"] == "recommended"


def test_blocking_orders_first_and_carries_reason(steward_home):
    store, job, wt, base = make_job(steward_home)
    (wt / "STEWARD-QUESTIONS.json").write_text(_qjson(
        {"id": "early", "question": "an early one?"},
        {"id": "late", "question": "a blocking one?", "blocking": True, "blocking_reason": "cannot pick a schema"}))
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "q")
    questions.scan(store, job.job_id, SubprocessRunner())
    ordered = questions.ordered(questions.load(store, job.job_id))
    assert [r["source"]["anchor"] for r in ordered] == ["late", "early"]
    assert ordered[0]["blocking_reason"] == "cannot pick a schema"
    assert questions.summary(ordered)["blocking"] == 1
