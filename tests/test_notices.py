"""One-grammar pane notices: one line per outcome, coalescing, dedupe, short names."""
import json

from steward import push, tabnames
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store

SESSION, PANE = "sess-1", "w4Q:p1"
GOAL = "Use gsd-quick to add a PLAY button for staged jobs to the Steward dashboard"


def _herdr():
    def respond(argv, cwd):
        out = json.dumps({"result": {"panes": [{"pane_id": PANE, "agent_session": {"value": SESSION}}]}}) \
            if argv[:3] == ["herdr", "pane", "list"] else ""
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=out, stderr="", duration_s=0.0)
    return FakeRunner(respond)


def _job(store):
    job = store.create_job(GOAL, "/repo")
    store.kv_set(job.job_id, "created_by", {"session": SESSION, "herdr_pane": PANE, "cwd": "/x"})
    store.kv_set(job.job_id, "notice", {"facts": {"files": 3}, "held": [], "held_t": None, "trouble": None, "sent": [], "last": None})
    return job


def _lines(r):
    return [c["argv"][4] for c in r.calls if c["argv"][2] == "send-text"]


def test_happy_path_accept_merge_deploy_is_one_line(steward_home):
    store, r = Store(steward_home), _herdr()
    job = _job(store)
    n = lambda ev, **f: push.notify_creator(store, job.job_id, ev, r, facts=f, clock=lambda: 1000.0)  # noqa: E731
    assert n("accepted") == "held"
    assert n("auto_merged", files=3, tests=3339) == "held"
    assert n("auto_deployed") == "delivered"
    assert push.settle(store, job.job_id, r, outcome="merged") == "nothing"
    name = tabnames.job_name(store, store.get_job(job.job_id))
    assert _lines(r) == [f"[steward] ✓ {name} ({job.job_id[:8]}) — done: merged 3 files, 3,339 tests pass, Steward reloaded · nothing"]
    assert "gsd-quick" not in _lines(r)[0] and "steward.cli" not in _lines(r)[0] and "…" not in _lines(r)[0]


def test_non_steward_merge_settles_into_one_line(steward_home):
    store, r = Store(steward_home), _herdr()
    job = _job(store)
    push.notify_creator(store, job.job_id, "accepted", r)
    push.notify_creator(store, job.job_id, "auto_merged", r, facts={"files": 2})
    assert push.settle(store, job.job_id, r, outcome="merged") == "delivered"
    assert len(_lines(r)) == 1 and "done: merged 2 files · nothing" in _lines(r)[0]


def test_accepted_without_merge_is_done(steward_home):
    store, r = Store(steward_home), _herdr()
    job = _job(store)
    push.notify_creator(store, job.job_id, "accepted", r)
    push.settle(store, job.job_id, r, outcome="skipped")
    assert len(_lines(r)) == 1 and "done: accepted, 3 files changed · nothing" in _lines(r)[0]


def test_held_accept_settles_after_90s_if_nothing_follows(steward_home):
    store, r = Store(steward_home), _herdr()
    job = _job(store)
    push.notify_creator(store, job.job_id, "accepted", r, clock=lambda: 0.0)
    assert push.flush_due(store, job.job_id, r, clock=lambda: 89.0) == "nothing"
    assert push.flush_due(store, job.job_id, r, clock=lambda: 91.0) == "delivered"
    assert len(_lines(r)) == 1


def test_conflict_path_is_silent_then_at_most_two_lines(steward_home):
    store, r = Store(steward_home), _herdr()
    job = _job(store)
    push.notify_creator(store, job.job_id, "accepted", r, clock=lambda: 0.0)
    push.notify_creator(store, job.job_id, "auto_followup", r, clock=lambda: 1.0,
                        facts={"detail": "merge conflict", "conflict_files": ["steward/dashrender.py"], "attempt": 1, "max": 2})
    assert push.settle(store, job.job_id, r, outcome="followup") == "held"
    assert _lines(r) == []                                           # nothing before 10 min
    assert push.flush_due(store, job.job_id, r, clock=lambda: 500.0) == "nothing"
    assert push.flush_due(store, job.job_id, r, clock=lambda: 700.0) == "delivered"
    assert "↻" in _lines(r)[0] and "merging (conflict in steward/dashrender.py, fix-up 1/2) · nothing" in _lines(r)[0]
    assert push.flush_due(store, job.job_id, r, clock=lambda: 900.0) == "nothing"  # once
    push.notify_creator(store, job.job_id, "auto_merged", r, facts={"files": 4}, clock=lambda: 1000.0)
    push.settle(store, job.job_id, r, outcome="merged", clock=lambda: 1000.0)
    assert len(_lines(r)) == 2 and _lines(r)[1].split(" — ")[1].startswith("done: merged 4 files")


def test_conflict_resolved_quickly_gives_only_the_final_line(steward_home):
    store, r = Store(steward_home), _herdr()
    job = _job(store)
    push.notify_creator(store, job.job_id, "accepted", r, clock=lambda: 0.0)
    push.notify_creator(store, job.job_id, "auto_followup", r, clock=lambda: 1.0, facts={"detail": "x", "attempt": 1, "max": 2})
    push.notify_creator(store, job.job_id, "incorporated", r, clock=lambda: 120.0)
    assert len(_lines(r)) == 1 and "✓" in _lines(r)[0]


def test_needs_you_is_one_line_with_the_action(steward_home):
    store, r = Store(steward_home), _herdr()
    job = _job(store)
    push.notify_creator(store, job.job_id, "accepted", r)
    push.notify_creator(store, job.job_id, "auto_followup", r, facts={"detail": "x", "attempt": 1, "max": 2})
    assert push.notify_creator(store, job.job_id, "auto_escalated", r, facts={"reason": "merge not done after 2 fix-ups"}) == "delivered"
    assert len(_lines(r)) == 1
    assert "◆" in _lines(r)[0] and "needs you: merge not done after 2 fix-ups · tap [ fix merge ] / answer" in _lines(r)[0]
    assert push.settle(store, job.job_id, r, outcome="escalated") == "nothing"
    assert len(_lines(r)) == 1


def test_dedupe_same_outcome_never_pushes_twice(steward_home):
    store, r = Store(steward_home), _herdr()
    job = _job(store)
    f = {"reason": "boom"}
    assert push.notify_creator(store, job.job_id, "auto_escalated", r, facts=f, clock=lambda: 0.0) == "delivered"
    assert push.notify_creator(store, job.job_id, "auto_escalated", r, facts=f, clock=lambda: 1000.0) == "duplicate"
    assert push.notify_creator(store, job.job_id, "auto_escalated", r, facts={"reason": "other"}, clock=lambda: 10.0) == "duplicate"  # same glyph < 90 s
    assert len(_lines(r)) == 1


def test_stopped_is_failed_glyph_and_short_name_only(steward_home):
    store, r = Store(steward_home), _herdr()
    job = _job(store)
    push.notify_creator(store, job.job_id, "stopped", r)
    name = tabnames.job_name(store, store.get_job(job.job_id))
    assert _lines(r) == [f"[steward] ✗ {name} ({job.job_id[:8]}) — stopped · nothing"]
    assert GOAL[:30] not in _lines(r)[0]


def test_phone_text_uses_the_same_grammar(steward_home):
    store = Store(steward_home)
    job = _job(store)
    name = tabnames.job_name(store, store.get_job(job.job_id))
    title, body = push.phone_event(store, job.job_id, "NEEDS_YOU", "approval 1234abcd: delete the branch?")
    assert title == f"◆ {name} ({job.job_id[:8]})"[:40] and body.endswith("· tap to answer") and len(body) <= 140
    assert push.phone_event(store, job.job_id, "READY", "checks passed")[1].endswith("· nothing")
