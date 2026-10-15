"""Delivery of Sam's answers (Phase 17-03: G5, G6, G7, G9, G13). Real git in tmp_path, no herdr, no model."""

from __future__ import annotations

import time

from questions_helpers import (
    CTX, LOG, PHASE16_DATE, age_all_answers, answered_at, answered_phase16, events_named, git, make_record,
    make_store_job, phase16_qid, scan_fixture, seed, set_state,
)

from steward import cli, exceptions, questions, questions_delivery
from steward.models import JobState
from steward.runner_exec import CommandResult, SubprocessRunner


def _needs_human():
    store, job_id = make_store_job()
    wt = scan_fixture(store, job_id)
    answered_phase16(store, job_id)
    set_state(store, job_id, JobState.NEEDS_HUMAN)
    return store, job_id, wt


def _commits_since(wt, sha) -> int:
    return int(git(wt, "rev-list", "--count", f"{sha}..HEAD"))


class Intercept:
    """A runner over real git that lets a test rewrite the answer to chosen calls."""

    def __init__(self, hook=None):
        self.real = SubprocessRunner()
        self.calls: list[list[str]] = []
        self.hook = hook

    def run(self, argv, cwd, **kw):
        self.calls.append(list(argv))
        if self.hook is not None:
            out = self.hook(list(argv), cwd, len(self.calls))
            if out is not None:
                return out
        return self.real.run(argv, cwd, **kw)


def _fail(argv, cwd, stderr="fatal: unable to auto-detect email address (got 'x@y.(none)')"):
    return CommandResult(argv=argv, cwd=str(cwd), exit_code=128, stdout="", stderr=stderr, duration_s=0.0)


# -- Task 1: NEEDS_HUMAN answers become one commit in the worktree ---------------------------------------------------


def test_needs_human_flush_commits_the_patch_in_the_worktree(steward_home):
    store, job_id, wt = _needs_human()
    plan = questions.render_writeback(store, job_id, SubprocessRunner())
    assert len(plan.edits) == 2
    before = git(wt, "rev-parse", "HEAD")

    res = questions_delivery.flush(store, job_id, SubprocessRunner(), force=True)

    assert res["accepted"] and res["mode"] == "commit"
    assert _commits_since(wt, before) == 1
    assert git(wt, "log", "-1", "--format=%s") == "docs(16): record Sam decisions"
    sha = git(wt, "rev-parse", "HEAD")
    for edit in plan.edits:
        assert git(wt, "show", f"HEAD:{edit.path}") == edit.new_text.rstrip("\n")
    assert sorted(git(wt, "show", "--name-only", "--format=", "HEAD").splitlines()) == sorted([CTX, LOG])
    assert git(wt, "status", "--porcelain") == ""
    recs = questions.load(store, job_id)
    assert len(recs) == 15 and all(r["written"]["via"] == "commit" and r["written"]["commit"] == sha for r in recs)
    ev = events_named(store, job_id, "questions_written")
    assert len(ev) == 1 and ev[0].judgment.value == "human"
    assert ev[0].evidence["commit"] == sha and len(ev[0].evidence["qids"]) == 15
    assert set(ev[0].evidence["paths"]) == {CTX, LOG}

    again = questions_delivery.flush(store, job_id, SubprocessRunner(), force=True)
    assert again["detail"] == "nothing to write" and git(wt, "rev-parse", "HEAD") == sha
    assert len(events_named(store, job_id, "questions_written")) == 1


def test_scan_after_the_commit_keeps_answers(steward_home):
    store, job_id, _ = _needs_human()
    questions_delivery.flush(store, job_id, SubprocessRunner(), force=True)
    res = questions.scan(store, job_id, SubprocessRunner(), force=True)
    assert res.error is None
    recs = questions.load(store, job_id)
    assert len(recs) == 15 and all(r["status"] == "answered" for r in recs)
    assert not any(r["status"] == "stale" for r in recs)
    assert not events_named(store, job_id, "question_stale")
    assert not events_named(store, job_id, "question_worker_decided")
    assert all(r["written"]["via"] == "commit" for r in recs)  # the scan never overwrites a written value


def test_cli_answer_done_flushes_now(steward_home, capsys):
    store, job_id = make_store_job()
    wt = scan_fixture(store, job_id)
    set_state(store, job_id, JobState.NEEDS_HUMAN)
    before = git(wt, "rev-parse", "HEAD")
    rc = cli.main(["answer", job_id, phase16_qid(1), "--option", "a", "--done", "--origin", "dash"])
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert _commits_since(wt, before) == 1
    rec = {r["qid"]: r for r in questions.load(store, job_id)}[phase16_qid(1)]
    assert rec["written"]["via"] == "commit" and rec["answer"]["origin"] == "dash"
    assert "committed" in out.out

    assert cli.main(["answer", job_id, phase16_qid(2), "--option", "recommended", "--origin", "dash"]) == 0
    assert _commits_since(wt, before) == 1  # no --done: the batch waits
    capsys.readouterr()
    assert cli.main(["answer", job_id, "--done", "--origin", "dash"]) == 0
    assert _commits_since(wt, before) == 2
    assert {r["qid"]: r for r in questions.load(store, job_id)}[phase16_qid(2)]["written"]["via"] == "commit"


def test_dirty_or_moved_worktree_is_not_written(steward_home):
    store, job_id, wt = _needs_human()
    before = git(wt, "rev-parse", "HEAD")
    (wt / CTX).write_text((wt / CTX).read_text() + "\nuncommitted edit\n")
    res = questions_delivery.flush(store, job_id, SubprocessRunner(), force=True)
    assert not res["accepted"] and res["detail"] == f"dirty {CTX}"
    assert git(wt, "rev-parse", "HEAD") == before
    assert len(questions_delivery.pending(store, job_id)) == 15
    skips = events_named(store, job_id, "questions_delivery_skipped")
    assert [e.evidence["reason"] for e in skips] == [f"dirty {CTX}"]
    questions_delivery.flush(store, job_id, SubprocessRunner(), force=True)
    assert len(events_named(store, job_id, "questions_delivery_skipped")) == 1  # not repeated
    git(wt, "checkout", "--", CTX)

    # the state changed to RUNNING between the render and the commit
    def flip(argv, cwd, n):
        if argv[:2] == ["git", "show"]:
            set_state(store, job_id, JobState.RUNNING)
        return None

    res = questions_delivery.flush(store, job_id, Intercept(flip), force=True)
    assert not res["accepted"] and res["detail"] == "state changed"
    assert git(wt, "rev-parse", "HEAD") == before and git(wt, "status", "--porcelain") == ""

    set_state(store, job_id, JobState.NEEDS_HUMAN)
    store.kv_set(job_id, "rework_pending", {"rev": 1})
    res = questions_delivery.flush(store, job_id, SubprocessRunner(), force=True)
    assert not res["accepted"] and res["detail"] == "rework pending"
    assert git(wt, "rev-parse", "HEAD") == before


def test_git_exception_is_recorded_and_identity_fallback(steward_home, monkeypatch):
    store, job_id, wt = _needs_human()
    used = []
    real_use = exceptions.use

    def spy(store_, job, name, evidence, **kw):
        used.append((job, name, dict(evidence)))
        return real_use(store_, job, name, evidence, **kw)

    monkeypatch.setattr(questions_delivery.exceptions, "use", spy)

    def fail_first(argv, cwd, n, seen=[]):
        if "commit" in argv and not seen:
            seen.append(1)
            return _fail(argv, cwd)
        return None

    runner = Intercept(fail_first)
    res = questions_delivery.flush(store, job_id, runner, force=True)
    assert res["accepted"], res
    assert used == [(job_id, "git", {"action": "record_answers"})]
    commits = [a for a in runner.calls if "commit" in a]
    assert len(commits) == 2 and "user.name=Steward" not in commits[0]
    assert commits[1][:6] == ["git", "-c", "user.name=Steward", "-c", "user.email=steward@localhost", "commit"]
    assert git(wt, "log", "-1", "--format=%s") == "docs(16): record Sam decisions"


def test_a_commit_that_fails_twice_leaves_the_records_pending(steward_home):
    store, job_id, wt = _needs_human()
    before = git(wt, "rev-parse", "HEAD")
    runner = Intercept(lambda argv, cwd, n: _fail(argv, cwd, "fatal: boom") if "commit" in argv else None)
    res = questions_delivery.flush(store, job_id, runner, force=True)
    assert not res["accepted"] and res["detail"].startswith("git commit failed: fatal: boom")
    assert len([a for a in runner.calls if "commit" in a]) == 2
    assert len(questions_delivery.pending(store, job_id)) == 15
    assert git(wt, "rev-parse", "HEAD") == before and git(wt, "status", "--porcelain") == ""  # nothing left staged
    skips = events_named(store, job_id, "questions_delivery_skipped")
    assert len(skips) == 1 and skips[0].evidence["reason"].startswith("git commit failed:")


def test_unparsed_and_unwritable_answers_are_store_only(steward_home):
    store, job_id, wt = _needs_human()
    recs = questions.load(store, job_id)
    recs.append(make_record("gsd_context:16-CONTEXT.md:X1", file=CTX, unparsed=True, options=(), recommended=(),
                            starred=False))
    questions.save(store, job_id, recs)
    questions.apply(store, job_id, answers=[{"qid": "gsd_context:16-CONTEXT.md:X1", "other": "yes, do it"}],
                    now=PHASE16_DATE)
    res = questions_delivery.flush(store, job_id, SubprocessRunner(), force=True)
    assert res["accepted"] and res["mode"] == "commit"
    by = {r["qid"]: r for r in questions.load(store, job_id)}
    assert by["gsd_context:16-CONTEXT.md:X1"]["written"]["via"] == "store_only"
    assert by["gsd_context:16-CONTEXT.md:X1"]["written"]["why"] == "no deterministic edit"
    assert by[phase16_qid(1)]["written"]["via"] == "commit"
    assert questions_delivery.pending(store, job_id) == []


def test_only_store_only_answers_make_no_commit(steward_home):
    store, job_id = make_store_job()
    wt = scan_fixture(store, job_id)
    recs = questions.load(store, job_id)
    recs.append(make_record("gsd_context:16-CONTEXT.md:X1", file=CTX, unparsed=True, options=(), recommended=(),
                            starred=False))
    questions.save(store, job_id, recs)
    questions.apply(store, job_id, answers=[{"qid": "gsd_context:16-CONTEXT.md:X1", "defer": True}])
    set_state(store, job_id, JobState.NEEDS_HUMAN)
    before = git(wt, "rev-parse", "HEAD")
    res = questions_delivery.flush(store, job_id, SubprocessRunner(), force=True)
    assert res["accepted"] and git(wt, "rev-parse", "HEAD") == before
    assert questions.load(store, job_id)[-1]["written"]["via"] == "store_only"


def test_reconcile_never_overwrites_a_written_value(steward_home):
    store, job_id, wt = _needs_human()
    questions_delivery.flush(store, job_id, SubprocessRunner(), force=True)
    seed_written = questions.load(store, job_id)
    assert all(r["written"]["via"] == "commit" for r in seed_written)
    questions.scan(store, job_id, SubprocessRunner(), force=True)
    assert [r["written"] for r in questions.load(store, job_id)] == [r["written"] for r in seed_written]


def test_batch_gate_and_failure_spacing(steward_home):
    store, job_id, wt = _needs_human()
    age_all_answers(store, job_id, 10)
    waiting = questions_delivery.flush(store, job_id, SubprocessRunner(), now=time.time())
    assert waiting["detail"] == "waiting" and 45 < waiting["waiting_s"] <= 50
    assert len(questions_delivery.pending(store, job_id)) == 15
    assert questions_delivery.flush(store, job_id, SubprocessRunner(), now=time.time() + 61)["accepted"]
    assert questions_delivery.pending(store, job_id) == []


# -- Task 2: delivery by state -----------------------------------------------------------------------------------------

import json  # noqa: E402

import pytest  # noqa: E402
from questions_helpers import linked_job  # noqa: E402

from steward import incorporate  # noqa: E402
from steward.question_adapters.base import make_edit  # noqa: E402
from steward.runner_exec import FakeRunner  # noqa: E402
from steward.steering import Steering  # noqa: E402
from steward.store import Store  # noqa: E402

_TAB = json.dumps({"result": {"tab": {"tab_id": "w3T:t9"}, "root_pane": {"pane_id": "w3T:p9"}}})


def _answer_three(store, job_id):
    questions.apply(store, job_id, answers=[{"qid": phase16_qid(n), "option": "recommended"} for n in (1, 2, 3)],
                    now=PHASE16_DATE)


def test_running_gets_a_steering_revision(steward_home):
    for state in (JobState.RUNNING, JobState.PAUSED):
        store, job_id = make_store_job()
        wt = scan_fixture(store, job_id)
        _answer_three(store, job_id)
        set_state(store, job_id, state)
        before = git(wt, "rev-parse", "HEAD")
        res = questions_delivery.flush(store, job_id, SubprocessRunner(), force=True)
        assert res["accepted"] and res["mode"] == "steer"
        ins = Steering(store).all_instructions(job_id)
        assert len(ins) == 1 and ins[0]["verb"] == "focus" and ins[0]["origin"] == "questions"
        assert not ins[0].get("urgent")
        text = ins[0]["text"]
        assert text.startswith("SAM ANSWERED 3 QUESTION(S) (recorded in Steward).")
        assert all(f"G{n}: recommended" in text for n in (1, 2, 3))
        assert "@@" in text and "DECIDED (Sam, 2026-09-29): recommended" in text
        assert "docs(16): record Sam decisions" in text
        recs = questions.load(store, job_id)
        assert [r["written"]["via"] for r in recs if r.get("written")] == ["steering"] * 3
        assert all(r["written"]["rev"] == 1 for r in recs if r.get("written"))
        ev = events_named(store, job_id, "questions_delivered")
        assert len(ev) == 1 and ev[0].judgment.value == "human"
        assert ev[0].evidence["mode"] == "steer" and ev[0].evidence["steering_rev"] == 1
        assert len(ev[0].evidence["qids"]) == 3
        assert git(wt, "rev-parse", "HEAD") == before and git(wt, "status", "--porcelain") == ""
        again = questions_delivery.flush(store, job_id, SubprocessRunner(), force=True)
        assert again["detail"] == "nothing to write" and len(Steering(store).all_instructions(job_id)) == 1


def _seeded(statuses=("open",)):
    store, job_id = make_store_job()
    recs = [make_record("gsd_context:16-CONTEXT.md:G4", recommended=("a",)),
            make_record("gsd_context:16-CONTEXT.md:G3", blocking=True, recommended=("a",)),
            make_record("gsd_context:16-CONTEXT.md:G7", recommended=("a",)),
            make_record("gsd_context:16-CONTEXT.md:G9", recommended=("a",)),
            make_record("gsd_context:16-CONTEXT.md:G2", recommended=("a",)),
            make_record("gsd_context:16-CONTEXT.md:G8", recommended=("a",)),
            make_record("gsd_context:16-CONTEXT.md:G5", recommended=(), starred=False)]
    seed(store, job_id, recs)
    return store, job_id


def test_steer_text_marks_changes(steward_home):
    store, job_id = _seeded()
    q = "gsd_context:16-CONTEXT.md:"
    questions.apply(store, job_id, answers=[
        {"qid": q + "G4", "option": "b", "note": "because the pane is small"},
        {"qid": q + "G3", "option": "b"},
        {"qid": q + "G7", "defer": True},
        {"qid": q + "G9", "other": "a third way", "note": "see the log"},
        {"qid": q + "G2", "option": "recommended"},
        {"qid": q + "G8", "option": "a"},
        {"qid": q + "G5", "option": "c"},
    ])
    recs = questions.ordered(questions.load(store, job_id))
    text = questions_delivery.steer_text(questions.WritebackPlan(), recs)
    lines = text.splitlines()
    assert lines[0] == "SAM ANSWERED 7 QUESTION(S) (recorded in Steward)."
    by = {ln.split(":")[0]: ln for ln in lines if ln[:1] == "G"}
    assert by["G4"].startswith("G4: CHANGE - you were proceeding on the recommended default (a) Option a;")
    assert "Sam chose (b) Option b. Switch to it." in by["G4"]
    assert "  note: because the pane is small" in lines
    assert by["G3"] == "G3: UNBLOCKED - (b) Option b"
    assert by["G7"] == "G7: DEFERRED - keep proceeding on the recommended default"
    assert by["G9"].startswith('G9: CHANGE') and 'Sam chose "a third way"' in by["G9"]
    assert "  note: see the log" in lines
    assert by["G2"] == "G2: recommended" and by["G8"] == "G8: recommended"
    assert by["G5"] == "G5: (c) Option c"  # no star: nothing was assumed, so no CHANGE


def test_steer_text_names_the_commit_message_and_applies_exactly(steward_home):
    store, job_id = make_store_job()
    scan_fixture(store, job_id)
    _answer_three(store, job_id)
    plan = questions.render_writeback(store, job_id, SubprocessRunner())
    recs = [r for r in questions.load(store, job_id) if r.get("answer")]
    text = questions_delivery.steer_text(plan, recs)
    assert ("Apply these edits to the source files exactly, commit them as docs(16): record Sam decisions, "
            "and continue.") in text
    assert plan.edits[0].diff.rstrip("\n") in text


def test_steer_text_cap_uses_a_compact_edit_list(steward_home):
    old = "".join(f"line {i}\n" for i in range(400))
    new = "".join(f"changed {i}\n" for i in range(400))
    plan = questions.WritebackPlan(edits=[make_edit("x/16-CONTEXT.md", old, new)], qids=["q"],
                                   commit_message="docs(16): record Sam decisions")
    assert len(plan.edits[0].diff) > questions_delivery.STEER_CAP
    rec = make_record()
    rec["answer"] = {"option": "recommended", "by": "sam"}
    text = questions_delivery.steer_text(plan, [rec])
    assert "@@" not in text and "compact list" in text
    changed = [ln for ln in text.splitlines() if ln[:1] in ("+", "-") and not ln.startswith("---")]
    assert len(changed) == questions_delivery.COMPACT_MAX_LINES
    assert changed[0] == "-line 0" and changed[1] == "-line 1"
    assert "more changed lines not shown" in text
    small = questions.WritebackPlan(edits=[make_edit("x.md", "a\n", "b\n")], commit_message="m")
    assert "compact" not in questions_delivery.steer_text(small, [rec])


def test_and_continue_is_a_send_back(steward_home):
    store, job_id, wt = _needs_human()
    calls = []

    def fake(job, text, origin, rid):
        calls.append((job, text, origin, rid))
        return {"accepted": True, "steering_rev": 4}

    before = git(wt, "rev-parse", "HEAD")
    res = questions_delivery.flush(store, job_id, SubprocessRunner(), force=True, and_continue=True, origin="moshi",
                                   send_back_fn=fake)
    assert res["accepted"] and res["mode"] == "send_back"
    assert len(calls) == 1 and calls[0][0] == job_id and calls[0][2] == "moshi"
    assert calls[0][1].startswith("SAM ANSWERED 15 QUESTION(S)") and "@@" not in calls[0][1]  # over the cap: compact
    assert git(wt, "rev-parse", "HEAD") == before
    assert all(r["written"] == {**r["written"], "via": "send_back", "rev": 4} for r in questions.load(store, job_id))
    ev = events_named(store, job_id, "questions_delivered")
    assert ev[0].evidence["mode"] == "send_back" and ev[0].evidence["steering_rev"] == 4


def test_a_failing_send_back_keeps_the_answers_pending(steward_home):
    store, job_id, _ = _needs_human()
    res = questions_delivery.flush(store, job_id, SubprocessRunner(), force=True, and_continue=True,
                                   send_back_fn=lambda *a: {"accepted": False, "detail": "no Herdr workspace known"})
    assert not res["accepted"] and "no Herdr workspace" in res["detail"]
    assert len(questions_delivery.pending(store, job_id)) == 15
    assert all(r["answer"] for r in questions.load(store, job_id))


class _Split:
    """git is real; everything else (herdr) is the fake launch seam of tests/test_send_back.py."""

    def __init__(self):
        self.real = SubprocessRunner()
        self.fake = FakeRunner(lambda a, c: CommandResult(argv=a, cwd=str(c), exit_code=0,
                               stdout=_TAB if a[:3] == ["herdr", "tab", "create"] else "", stderr="", duration_s=0.0))

    def run(self, argv, cwd, **kw):
        return (self.real if argv[0] == "git" else self.fake).run(argv, cwd, **kw)


def test_cli_and_continue_sends_the_job_back(steward_home, monkeypatch, capsys):
    store, job_id = make_store_job()
    wt = scan_fixture(store, job_id)
    set_state(store, job_id, JobState.NEEDS_HUMAN)
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w3T")
    monkeypatch.setattr(cli, "SubprocessRunner", _Split)
    before = git(wt, "rev-parse", "HEAD")
    rc = cli.main(["answer", job_id, phase16_qid(1), "--option", "a", "--and-continue", "--origin", "moshi"])
    assert rc == 0, capsys.readouterr().err
    assert store.get_job(job_id).state == JobState.RUNNING
    marker = store.kv_get(job_id, "rework_pending")
    assert marker and marker["by"] == "moshi" and marker["text"].startswith("SAM ANSWERED 1 QUESTION(S)")
    assert git(wt, "rev-parse", "HEAD") == before
    rec = {r["qid"]: r for r in questions.load(store, job_id)}[phase16_qid(1)]
    assert rec["written"]["via"] == "send_back" and rec["written"]["rev"] == marker["rev"]


def test_cli_and_continue_without_a_workspace_keeps_the_answers(steward_home, monkeypatch, capsys):
    store, job_id = make_store_job()
    scan_fixture(store, job_id)
    set_state(store, job_id, JobState.NEEDS_HUMAN)
    monkeypatch.delenv("HERDR_WORKSPACE_ID", raising=False)
    rc = cli.main(["answer", job_id, phase16_qid(1), "--option", "a", "--and-continue"])
    err = capsys.readouterr().err
    assert rc == 2 and "no Herdr workspace" in err and "answers are saved" in err
    assert store.get_job(job_id).state == JobState.NEEDS_HUMAN
    assert {r["qid"]: r for r in questions.load(store, job_id)}[phase16_qid(1)]["status"] == "answered"
    assert len(questions_delivery.pending(store, job_id)) == 1


def test_accepted_unmerged_commits_before_the_merge(steward_home, tmp_path):
    store, job_id, repo, wt = linked_job(tmp_path)
    answered_phase16(store, job_id)
    set_state(store, job_id, JobState.ACCEPTED)
    res = questions_delivery.flush(store, job_id, SubprocessRunner(), force=True)
    assert res["accepted"] and res["mode"] == "commit"
    assert git(wt, "log", "-1", "--format=%s") == "docs(16): record Sam decisions"
    merged = incorporate.incorporate(store, job_id)
    assert merged["accepted"] and merged["merge_commit"], merged
    text = git(repo, "show", f"HEAD:{CTX}")
    assert text.count("DECIDED (Sam, 2026-09-29)") >= 15 and "OPEN FOR SAM" not in text


@pytest.mark.parametrize("state,why,source", [
    (JobState.RELEASED, "released", True), (JobState.STOPPED, "stopped", False), (JobState.FAILED, "failed", False)])
def test_released_stopped_failed_are_store_only(steward_home, state, why, source):
    store, job_id = make_store_job()
    wt = scan_fixture(store, job_id)
    answered_phase16(store, job_id)
    set_state(store, job_id, state)
    before = git(wt, "rev-parse", "HEAD")
    res = questions_delivery.flush(store, job_id, SubprocessRunner(), force=True)
    assert res["accepted"] and res["mode"] == "store_only"
    assert git(wt, "rev-parse", "HEAD") == before and not Steering(store).all_instructions(job_id)
    assert all(r["written"]["via"] == "store_only" and r["written"]["why"] == why for r in questions.load(store, job_id))
    assert bool(store.kv_get(job_id, "questions_source_unwritten")) is source
    assert len(events_named(store, job_id, "questions_delivery_skipped")) == 1


def test_merged_is_store_only(steward_home, tmp_path):
    store, job_id, repo, wt = linked_job(tmp_path)
    set_state(store, job_id, JobState.ACCEPTED)
    assert incorporate.incorporate(store, job_id)["accepted"]
    answered_phase16(store, job_id)
    before = git(wt, "rev-parse", "HEAD")
    res = questions_delivery.flush(store, job_id, SubprocessRunner(), force=True)
    assert res["mode"] == "store_only" and git(wt, "rev-parse", "HEAD") == before
    assert all(r["written"]["why"] == "merged" for r in questions.load(store, job_id))
    assert sorted(store.kv_get(job_id, "questions_source_unwritten")) == sorted(phase16_qid(n) for n in range(1, 16))
    assert len(events_named(store, job_id, "questions_delivery_skipped")) == 1


@pytest.mark.parametrize("state", [JobState.CREATED, JobState.CONTRACTED, JobState.WORKER_FINISHED,
                                   JobState.CHECKS_PASSED, JobState.STOPPING])
def test_transitional_states_hold(steward_home, state):
    store, job_id = make_store_job()
    wt = scan_fixture(store, job_id)
    _answer_three(store, job_id)
    set_state(store, job_id, state)
    before = git(wt, "rev-parse", "HEAD")
    for _ in range(2):
        res = questions_delivery.flush(store, job_id, SubprocessRunner(), force=True)
        assert res["accepted"] and res["mode"] == "hold"
    assert git(wt, "rev-parse", "HEAD") == before and len(questions_delivery.pending(store, job_id)) == 3
    skips = events_named(store, job_id, "questions_delivery_skipped")
    assert len(skips) == 1 and skips[0].evidence["reason"] == f"state {state.value}"
    set_state(store, job_id, JobState.NEEDS_HUMAN)  # the hold lifts when the state settles
    assert questions_delivery.flush(store, job_id, SubprocessRunner(), force=True)["mode"] == "commit"


def _merged_with_unwritten(tmp_path):
    store, job_id, repo, wt = linked_job(tmp_path)
    set_state(store, job_id, JobState.ACCEPTED)
    assert incorporate.incorporate(store, job_id)["accepted"]
    answered_phase16(store, job_id)
    questions_delivery.flush(store, job_id, SubprocessRunner(), force=True)
    return store, job_id, repo, wt


def test_write_source_commits_to_the_source_branch(steward_home, tmp_path, capsys, monkeypatch):
    store, job_id, repo, wt = _merged_with_unwritten(tmp_path)
    (repo / "other.txt").write_text("staged elsewhere\n")
    git(repo, "add", "other.txt")
    used = []
    real_use = exceptions.use
    monkeypatch.setattr(questions_delivery.exceptions, "use",
                        lambda s, j, n, e, **k: (used.append((n, dict(e))), real_use(s, j, n, e, **k))[1])
    before = git(repo, "rev-parse", "HEAD")
    rc = cli.main(["answer", job_id, "--write-source", "--origin", "dash"])
    assert rc == 0, capsys.readouterr().err
    assert git(repo, "rev-list", "--count", f"{before}..HEAD") == "1"
    assert git(repo, "log", "-1", "--format=%s") == "docs(16): record Sam decisions"
    assert git(repo, "show", "--name-only", "--format=", "HEAD").splitlines().count("other.txt") == 0
    assert sorted(git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()) == sorted([CTX, LOG])
    assert git(repo, "diff", "--cached", "--name-only") == "other.txt"  # the unrelated staged file is untouched
    assert git(repo, "show", f"HEAD:{CTX}").count("DECIDED (Sam, 2026-09-29)") >= 15
    sha = git(repo, "rev-parse", "HEAD")
    recs = questions.load(store, job_id)
    assert all(r["written"] == {**r["written"], "via": "source_branch", "commit": sha, "branch": "main"} for r in recs)
    assert store.kv_get(job_id, "questions_source_unwritten") == []
    ev = events_named(store, job_id, "questions_written")
    assert ev[-1].evidence["commit"] == sha and ev[-1].evidence["branch"] == "main" and len(ev[-1].evidence["qids"]) == 15
    assert ("git", {"action": "record_answers_source", "repo": str(repo)}) in used
    assert cli.main(["answer", job_id, "--write-source", "--origin", "dash"]) == 0  # nothing left: a no-op
    assert git(repo, "rev-parse", "HEAD") == sha


def _snap(repo):
    return (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain", "-uall"), git(repo, "diff"),
            git(repo, "diff", "--cached"))


def test_write_source_refuses_when_it_could_sweep_or_clobber(steward_home, tmp_path, capsys):
    store, job_id, repo, wt = _merged_with_unwritten(tmp_path)
    before = _snap(repo)
    (repo / ".git" / "MERGE_HEAD").write_text(git(repo, "rev-parse", "HEAD") + "\n")
    assert cli.main(["answer", job_id, "--write-source"]) == 2
    assert "mid-merge" in capsys.readouterr().err
    (repo / ".git" / "MERGE_HEAD").unlink()

    (repo / CTX).write_text((repo / CTX).read_text() + "\nmine\n")
    assert cli.main(["answer", job_id, "--write-source"]) == 2
    assert f"dirty {CTX}" in capsys.readouterr().err
    git(repo, "add", CTX)
    assert cli.main(["answer", job_id, "--write-source"]) == 2
    assert f"staged changes on {CTX}" in capsys.readouterr().err
    git(repo, "reset", "-q", "--", CTX)
    git(repo, "checkout", "--", CTX)
    assert _snap(repo) == before
    assert len(store.kv_get(job_id, "questions_source_unwritten")) == 15
    assert all(r["written"]["via"] == "store_only" for r in questions.load(store, job_id))


def test_human_only_flags(steward_home, tmp_path, capsys):
    store, job_id = make_store_job()
    wt = scan_fixture(store, job_id)
    set_state(store, job_id, JobState.NEEDS_HUMAN)
    handlers = cli._build_control_handlers(store)
    for flag in ("and_continue", "then_accept", "write_source"):
        out = handlers["answer"]({"request_id": f"r-{flag}", "op": "answer", "origin": "chatgpt",
                                  "args": {"job_id": job_id, "answers": [{"qid": phase16_qid(1), "option": "a"}],
                                           flag: True}})
        assert not out["accepted"] and "needs a human origin" in out["detail"]
    assert questions.load(store, job_id)[0]["status"] == "open"  # refused before anything was recorded
    for origin in ("cli", "moshi", "dash", "job-chat"):
        assert cli.main(["answer", job_id, "--write-source", "--origin", origin]) == 0
    capsys.readouterr()
    assert cli.main(["answer", job_id, phase16_qid(1), "--option", "a", "--and-continue", "--then-accept"]) == 2
    assert "cannot be combined" in capsys.readouterr().err
    assert questions.load(store, job_id)[0]["status"] == "open"


def test_then_accept_answers_commits_then_accepts(steward_home, monkeypatch, capsys):
    store, job_id = make_store_job()
    wt = scan_fixture(store, job_id)
    set_state(store, job_id, JobState.NEEDS_HUMAN)
    monkeypatch.setattr(cli, "SubprocessRunner", _Split)
    before = git(wt, "rev-parse", "HEAD")
    rc = cli.main(["answer", job_id, "--accept-recommended", "--then-accept", "--origin", "dash"])
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert store.get_job(job_id).state == JobState.ACCEPTED
    assert git(wt, "rev-parse", "HEAD") != before and "DECIDED" in git(wt, "show", f"HEAD:{CTX}")
    assert "accepted" in out.out and questions_delivery.pending(store, job_id) == []


def test_then_accept_refused_keeps_the_answers(steward_home, monkeypatch, capsys):
    store, job_id = make_store_job()
    scan_fixture(store, job_id)
    set_state(store, job_id, JobState.RUNNING)  # accept needs NEEDS_HUMAN
    monkeypatch.setattr(cli, "SubprocessRunner", _Split)
    rc = cli.main(["answer", job_id, "--accept-recommended", "--then-accept"])
    err = capsys.readouterr().err
    assert rc == 2 and "accept refused" in err and "answers saved" in err
    assert all(r["status"] == "answered" for r in questions.load(store, job_id))


# -- Task 3: batching, the control tick, resting-job rescans and confirmation ----------------------------------------------

from questions_helpers import commit_file  # noqa: E402


def _answer_one(store, job_id, n, epoch):
    questions.apply(store, job_id, answers=[{"qid": phase16_qid(n), "option": "recommended"}], now=PHASE16_DATE)
    answered_at(store, job_id, phase16_qid(n), epoch)


def test_batch_waits_60s_then_flushes_once(steward_home):
    store, job_id = make_store_job()
    wt = scan_fixture(store, job_id)
    set_state(store, job_id, JobState.NEEDS_HUMAN)
    t0 = 1_800_000_000.0
    _answer_one(store, job_id, 1, t0)
    _answer_one(store, job_id, 2, t0 + 20)
    base = git(wt, "rev-parse", "HEAD")
    runner = SubprocessRunner()

    assert questions_delivery.tick(store, runner, now=t0 + 30)["flushed"] == []
    assert _commits_since(wt, base) == 0
    out = questions_delivery.tick(store, runner, now=t0 + 81)
    assert [f["mode"] for f in out["flushed"]] == ["commit"] and _commits_since(wt, base) == 1
    first = git(wt, "rev-parse", "HEAD")
    text = git(wt, "show", f"HEAD:{CTX}")
    assert "G1" in text and text.count("DECIDED (Sam") == 2  # one commit carries both

    _answer_one(store, job_id, 3, t0 + 90)
    assert questions_delivery.tick(store, runner, now=t0 + 120)["flushed"] == []  # a new batch starts its own clock
    assert git(wt, "rev-parse", "HEAD") == first
    questions_delivery.tick(store, runner, now=t0 + 151)
    assert _commits_since(wt, first) == 1 and git(wt, "show", f"HEAD:{CTX}").count("DECIDED (Sam") == 3


def test_done_flushes_immediately_regardless_of_age(steward_home):
    store, job_id = make_store_job()
    wt = scan_fixture(store, job_id)
    set_state(store, job_id, JobState.NEEDS_HUMAN)
    _answer_one(store, job_id, 1, time.time())  # a fresh answer
    base = git(wt, "rev-parse", "HEAD")
    assert questions_delivery.flush(store, job_id, SubprocessRunner())["detail"] == "waiting"
    assert _commits_since(wt, base) == 0
    assert questions_delivery.flush(store, job_id, SubprocessRunner(), force=True)["accepted"]
    assert _commits_since(wt, base) == 1


def test_tick_flushes_running_jobs_with_a_steering_revision_and_resting_jobs_with_a_commit(steward_home):
    store = Store()
    jobs = []
    for state in (JobState.RUNNING, JobState.NEEDS_HUMAN):
        job = store.create_job("discuss", "/nonexistent")
        wt = scan_fixture(store, job.job_id)
        _answer_three(store, job.job_id)
        set_state(store, job.job_id, state)
        age_all_answers(store, job.job_id)
        jobs.append((job.job_id, wt))
    (running, run_wt), (resting, rest_wt) = jobs
    base = git(rest_wt, "rev-parse", "HEAD")
    out = questions_delivery.tick(store, SubprocessRunner())
    assert sorted(f["mode"] for f in out["flushed"]) == ["commit", "steer"] and out["errors"] == []
    assert len(Steering(store).all_instructions(running)) == 1 and not Steering(store).all_instructions(resting)
    assert _commits_since(rest_wt, base) == 1
    assert questions_delivery.tick(store, SubprocessRunner())["flushed"] == []  # idempotent

    quiet = store.create_job("nothing", "/nonexistent")
    set_state(store, quiet.job_id, JobState.RUNNING)
    spy = Intercept()
    out = questions_delivery.tick(store, spy)
    assert [c for c in spy.calls] == []  # RUNNING with no records and the others settled: no git call at all


def test_tick_rescans_resting_jobs_when_head_moved(steward_home):
    store, job_id = make_store_job()
    wt = scan_fixture(store, job_id)
    set_state(store, job_id, JobState.NEEDS_HUMAN)
    spy = Intercept()
    questions_delivery.tick(store, spy, now=1000.0)
    assert [c for c in spy.calls if c[:2] == ["git", "diff"]] == []  # HEAD is the scanned one: no scan
    scanned = store.kv_get(job_id, questions.SCANNED_HEAD_KEY)

    sha = commit_file(wt, "notes.txt", "more\n", "worker commit")
    questions_delivery.tick(store, spy, now=1010.0)  # inside the 30 s window
    assert store.kv_get(job_id, questions.SCANNED_HEAD_KEY) == scanned
    out = questions_delivery.tick(store, spy, now=1031.0)
    assert [s["job"] for s in out["scanned"]] == [job_id]
    assert store.kv_get(job_id, questions.SCANNED_HEAD_KEY) == sha

    n = len(spy.calls)
    questions_delivery.tick(store, spy, now=1100.0)  # moved nothing: one rev-parse, no scan
    assert [c[:2] for c in spy.calls[n:]] == [["git", "rev-parse"]]

    store2, running = make_store_job()
    wt2 = scan_fixture(store2, running)
    set_state(store2, running, JobState.RUNNING)
    commit_file(wt2, "notes.txt", "more\n", "worker commit")
    head = store2.kv_get(running, questions.SCANNED_HEAD_KEY)
    questions_delivery.tick(store2, SubprocessRunner(), now=5000.0)
    assert store2.kv_get(running, questions.SCANNED_HEAD_KEY) == head  # RUNNING jobs are left to their runner


def test_steering_delivery_is_confirmed_by_the_next_scan(steward_home):
    store, job_id = make_store_job()
    wt = scan_fixture(store, job_id)
    _answer_three(store, job_id)
    set_state(store, job_id, JobState.RUNNING)
    plan = questions.render_writeback(store, job_id, SubprocessRunner())
    assert questions_delivery.flush(store, job_id, SubprocessRunner(), force=True)["mode"] == "steer"

    commit_file(wt, "unrelated.txt", "x\n", "worker does other things")
    questions.scan(store, job_id, SubprocessRunner())
    recs = [r for r in questions.load(store, job_id) if r.get("written")]
    assert len(recs) == 3 and all("confirmed_at" not in r["written"] for r in recs)
    assert not [e for e in events_named(store, job_id, "questions_written")]

    for e in plan.edits:  # the worker applies the steered patch exactly and commits
        (wt / e.path).write_text(e.new_text)
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", plan.commit_message)
    head = git(wt, "rev-parse", "HEAD")
    questions.scan(store, job_id, SubprocessRunner())
    recs = [r for r in questions.load(store, job_id) if r.get("written")]
    assert all(r["status"] == "answered" and r["written"]["via"] == "steering" and r["written"]["rev"] == 1
               and r["written"]["confirmed_at"] and r["written"]["commit"] == head for r in recs)
    ev = events_named(store, job_id, "questions_written")
    assert len(ev) == 1 and ev[0].evidence == {"commit": head, "qids": [phase16_qid(n) for n in (1, 2, 3)],
                                                "via": "scan"}
    assert ev[0].judgment.value == "human"
    assert not events_named(store, job_id, "question_stale")
    commit_file(wt, "again.txt", "x\n", "more")
    questions.scan(store, job_id, SubprocessRunner())
    assert len(events_named(store, job_id, "questions_written")) == 1  # confirmed once


def test_runner_flushes_its_own_job(steward_home):
    from test_questions_runner import FIX as RFIX
    from test_questions_runner import _commit, _setup

    store, job, wt, runner = _setup(steward_home)
    _commit(wt, CTX, (RFIX / "phase16_context_before.md").read_text())
    runner.tick(job.job_id)
    assert len(questions.load(store, job.job_id)) == 15
    _answer_three(store, job.job_id)
    now = runner.clock()
    for n in (1, 2, 3):
        answered_at(store, job.job_id, phase16_qid(n), now - 100)
    base = git(wt, "rev-parse", "HEAD")

    runner.tick(job.job_id)
    ins = [i for i in Steering(store).all_instructions(job.job_id) if i["origin"] == "questions"]
    assert len(ins) == 1 and ins[0]["verb"] == "focus"
    assert git(wt, "rev-parse", "HEAD") == base  # the runner steers, it never writes the worktree

    # the control tick reading the same pending answers a moment later produces no second revision
    saved = questions.load(store, job.job_id)
    for r in saved:
        if r.get("written"):
            r["written"] = None
    questions.save(store, job.job_id, saved)
    out = questions_delivery.flush(store, job.job_id, SubprocessRunner(), force=True)
    assert out["accepted"]
    assert len([i for i in Steering(store).all_instructions(job.job_id) if i["origin"] == "questions"]) == 1
    assert len(events_named(store, job.job_id, "questions_delivered")) == 1
    assert {r["written"]["rev"] for r in questions.load(store, job.job_id) if r.get("written")} == {1}


def test_runner_does_not_flush_before_60s(steward_home):
    from test_questions_runner import FIX as RFIX
    from test_questions_runner import _commit, _setup

    store, job, wt, runner = _setup(steward_home)
    _commit(wt, CTX, (RFIX / "phase16_context_before.md").read_text())
    runner.tick(job.job_id)
    _answer_three(store, job.job_id)
    for n in (1, 2, 3):
        answered_at(store, job.job_id, phase16_qid(n), runner.clock() - 5)
    runner.tick(job.job_id)
    assert not [i for i in Steering(store).all_instructions(job.job_id) if i["origin"] == "questions"]


def test_tick_never_raises(steward_home):
    store, job_id, wt = _needs_human()
    age_all_answers(store, job_id)

    class Exploding:
        def run(self, argv, cwd, **kw):
            raise RuntimeError("git binary vanished")

    out = questions_delivery.tick(store, Exploding(), now=time.time() + 100)
    assert isinstance(out, dict)
    assert out["errors"] or any(not f["accepted"] for f in out["flushed"])
    assert len(questions_delivery.pending(store, job_id)) == 15

    import shutil

    shutil.rmtree(wt)  # a missing worktree
    out = questions_delivery.tick(store, SubprocessRunner(), now=time.time() + 200)
    assert isinstance(out, dict) and len(questions_delivery.pending(store, job_id)) == 15

    other, gone = make_store_job()  # a job whose repo does not exist
    set_state(other, gone, JobState.ACCEPTED)
    assert isinstance(questions_delivery.tick(other, SubprocessRunner()), dict)


# -- hand-off flush: accept and incorporate never wait for the 60 s batch ---------------------------------------------------


def test_incorporate_flushes_answers_answered_seconds_ago(steward_home, tmp_path):
    store, job_id, repo, wt = linked_job(tmp_path)
    answered_phase16(store, job_id)  # answered just now: the batch gate would still say "waiting"
    set_state(store, job_id, JobState.ACCEPTED)
    assert questions_delivery.flush(store, job_id, SubprocessRunner())["detail"] == "waiting"
    merged = incorporate.incorporate(store, job_id)
    assert merged["accepted"] and merged["merge_commit"], merged
    text = git(repo, "show", f"HEAD:{CTX}")
    assert text.count("DECIDED (Sam, 2026-09-29)") >= 15 and "OPEN FOR SAM" not in text
    assert questions_delivery.pending(store, job_id) == []
    assert not store.kv_get(job_id, "questions_source_unwritten")


def test_accept_relay_flushes_answers_first(steward_home, monkeypatch, capsys):
    store, job_id, wt = _needs_human()
    before = git(wt, "rev-parse", "HEAD")
    monkeypatch.setattr(cli, "SubprocessRunner", _Split)
    assert cli.main(["accept", job_id]) == 0, capsys.readouterr().err
    assert store.get_job(job_id).state == JobState.ACCEPTED
    assert git(wt, "rev-parse", "HEAD") != before and "DECIDED" in git(wt, "show", f"HEAD:{CTX}")
    assert questions_delivery.pending(store, job_id) == []


def test_incorporate_records_answers_it_could_not_write_for_write_source(steward_home, tmp_path, monkeypatch):
    store, job_id, repo, wt = linked_job(tmp_path)
    answered_phase16(store, job_id)
    set_state(store, job_id, JobState.ACCEPTED)
    calls = []

    def no_flush_before_merge(s, j, runner=None):  # the pre-merge flush fails; the post-merge one runs for real
        calls.append(1)
        if len(calls) == 1:
            return {"accepted": False}
        return real(s, j, runner)

    real = questions_delivery.flush_now
    monkeypatch.setattr(questions_delivery, "flush_now", no_flush_before_merge)
    assert incorporate.incorporate(store, job_id)["accepted"]
    assert sorted(store.kv_get(job_id, "questions_source_unwritten")) == sorted(
        r["qid"] for r in questions.load(store, job_id) if r.get("answer"))
    assert questions_delivery.pending(store, job_id) == []
    wrote = questions_delivery.write_source(store, job_id, SubprocessRunner(), origin="cli")
    assert wrote["accepted"] and "nothing to write" not in wrote["detail"], wrote
