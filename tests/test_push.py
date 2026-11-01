import json

import pytest

from steward import cli, push
from steward.api import StewardAPI
from steward.models import JobState, Judgment
from steward.runner import Runner
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store

SESSION = "37355ed2-dc4b-43b0-9456-08c53ed13bd3"
PANE = "w4Q:p1"
JID = "0123abcd-0000-0000-0000-000000000000"


def _herdr(panes, fail_send=False):
    def respond(argv, cwd):
        out, code = "", 0
        if argv[:3] == ["herdr", "pane", "list"]:
            out = json.dumps({"result": {"panes": panes}})
        elif fail_send and argv[:3] == ["herdr", "pane", "send-text"]:
            code = 1
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=code, stdout=out, stderr="boom" if code else "", duration_s=0.0)

    return FakeRunner(respond)


def _pane(session=SESSION, pane=PANE):
    return {"pane_id": pane, "agent_session": {"agent": "claude", "kind": "id", "value": session}}


def _job(store, created=True, goal="build the thing"):
    job = store.create_job(goal, "/repo")
    if created:
        store.kv_set(job.job_id, "created_by", {"session": SESSION, "herdr_pane": PANE, "cwd": "/x"})
    return job


def _whats(store, jid):
    return [e.what for e in store.events(jid)]


def _sends(runner):
    return [c["argv"] for c in runner.calls if c["argv"][2] in ("send-text", "send-keys")]


def test_delivers_when_pane_and_session_match(steward_home):
    store = Store(steward_home)
    job = _job(store)
    r = _herdr([_pane()])
    assert push.notify_creator(store, job.job_id, "stopped", r) == "delivered"
    sends = _sends(r)
    assert sends[0][:4] == ["herdr", "pane", "send-text", PANE]
    assert sends[0][4] == f"[steward] ✗ thing ({job.job_id[:8]}) — stopped · nothing"
    assert sends[1] == ["herdr", "pane", "send-keys", PANE, "Enter"]
    assert "creator_notified" in _whats(store, job.job_id)


@pytest.mark.parametrize("panes,reason", [([], "pane_gone"), ([_pane(session="other")], "different_session")])
def test_skipped_when_pane_gone_or_different_session(steward_home, panes, reason):
    store = Store(steward_home)
    job = _job(store)
    r = _herdr(panes)
    assert push.notify_creator(store, job.job_id, "stopped", r) == f"skipped:{reason}"
    assert _sends(r) == []
    ev = [e for e in store.events(job.job_id) if e.what == "creator_notify_skipped"][0]
    assert ev.evidence["reason"] == reason


def test_skipped_without_created_by_or_pane(steward_home):
    store = Store(steward_home)
    j1 = _job(store, created=False)
    j2 = _job(store)
    store.kv_set(j2.job_id, "created_by", {"session": SESSION})
    r = _herdr([_pane()])
    assert push.notify_creator(store, j1.job_id, "stopped", r) == "skipped:no_creator"
    assert push.notify_creator(store, j2.job_id, "stopped", r) == "skipped:no_creator"
    assert r.calls == []


def test_opted_out(steward_home):
    store = Store(steward_home)
    job = _job(store)
    store.kv_set(job.job_id, "notify_creator", False)
    r = _herdr([_pane()])
    assert push.notify_creator(store, job.job_id, "stopped", r) == "skipped:opted_out"
    assert r.calls == []


def test_dedupe_per_event(steward_home):
    store = Store(steward_home)
    job = _job(store)
    r = _herdr([_pane()])
    assert push.notify_creator(store, job.job_id, "stopped", r) == "delivered"
    assert push.notify_creator(store, job.job_id, "stopped", r) == "duplicate"
    assert push.notify_creator(store, job.job_id, "incorporated", r) == "delivered"  # different glyph: not coalesced
    assert len(_sends(r)) == 4


def test_failure_recorded_not_raised(steward_home):
    store = Store(steward_home)
    job = _job(store)
    assert push.notify_creator(store, job.job_id, "stopped", _herdr([_pane()], fail_send=True)) == "failed"
    assert "creator_notify_failed" in _whats(store, job.job_id)

    class Boom:
        def run(self, *a, **k):
            raise OSError("no herdr")

    assert push.notify_creator(store, job.job_id, "stopped", Boom()) == "failed"


def test_line_redacted_and_capped(steward_home):
    msg = push.line("x", push.NEEDS, "needs you: API_KEY" + "=" + "example-placeholder " + "x" * 400, "answer", job_id=JID)
    assert "example-placeholder" not in msg
    assert len(msg) <= push._MAX and "\n" not in msg


def test_hook_api_cancel_and_approve(steward_home, monkeypatch):
    from steward import autopilot

    attempts = []  # v2.7 SRD-10: an accept also attempts the merge; the merge outcome is not this test's subject
    monkeypatch.setattr(autopilot, "ensure_attempted", lambda st, jid, runner, **kw: attempts.append(jid))
    store = Store(steward_home)
    r = _herdr([_pane()])
    api = StewardAPI(store, runner_exec=r)
    stopped = _job(store)
    api.cancel(stopped.job_id)
    assert store.get_job(stopped.job_id).state == JobState.STOPPED
    assert "creator_notified" in _whats(store, stopped.job_id)

    from steward import approvals

    for decision, event in (("approve", "held"), ("deny", "✗")):
        job = _job(store)
        for st in (JobState.CONTRACTED, JobState.RUNNING, JobState.WORKER_FINISHED, JobState.CHECKS_PASSED, JobState.NEEDS_HUMAN):
            store.set_state(job.job_id, st, what="t", evidence={}, rule="t", judgment=Judgment.code)
        rec = approvals.open_approval(store, job.job_id, approvals.ACCEPTANCE_QUESTION, kind="acceptance")
        r.calls.clear()
        api.approve(job.job_id, rec["id"], decision=decision)
        sent = [c for c in r.calls if c["argv"][2] == "send-text"]
        assert bool(sent) == (decision == "deny")  # an accept is held for the merge outcome, a stop is a line
        assert all(event in c["argv"][4] for c in sent)


def test_hook_human_accept_handler_and_incorporate(steward_home, monkeypatch):
    from steward import autopilot

    attempts = []  # v2.7 SRD-10: an accept also attempts the merge; the merge outcome is not this test's subject
    monkeypatch.setattr(autopilot, "ensure_attempted", lambda st, jid, runner, **kw: attempts.append(jid))
    store = Store(steward_home)
    r = _herdr([_pane()])
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: r)
    handlers = cli._build_control_handlers(store)
    job = _job(store)
    for st in (JobState.CONTRACTED, JobState.RUNNING, JobState.WORKER_FINISHED, JobState.CHECKS_PASSED, JobState.NEEDS_HUMAN):
        store.set_state(job.job_id, st, what="t", evidence={}, rule="t", judgment=Judgment.code)
    res = handlers["accept"]({"op": "accept", "args": {"job_id": job.job_id}})
    assert res["accepted"] and attempts == [job.job_id]
    assert not any(c["argv"][2] == "send-text" for c in r.calls)  # held until merge/settle

    monkeypatch.setattr(cli.incorporate_module, "incorporate",
                        lambda s, j: {"accepted": True, "merge_commit": "abc1234", "source_branch": "main"})
    handlers["incorporate"]({"op": "incorporate", "args": {"job_id": job.job_id}})
    assert any("✓" in c["argv"][4] and "done: " in c["argv"][4] for c in r.calls if c["argv"][2] == "send-text")


def test_new_no_notify_flag_parsed():
    ns = cli._build_parser().parse_args(["new", "g", "--no-notify"])
    assert ns.no_notify is True


def test_runner_hooks_auto_accept_and_stop(steward_home):
    store = Store(steward_home)
    r = _herdr([_pane()])
    job = _job(store)
    rn = object.__new__(Runner)
    rn.store, rn.runner_exec = store, r
    rn._notify = lambda *a, **k: None
    rn._notify_closeout_result(job.job_id, JobState.ACCEPTED)
    assert push._state(store, job.job_id)["held"] or any("✓" in c["argv"][4] for c in r.calls if c["argv"][2] == "send-text")

    job2 = _job(store)
    for st in (JobState.CONTRACTED, JobState.RUNNING, JobState.STOPPING):
        store.set_state(job2.job_id, st, what="t", evidence={}, rule="t", judgment=Judgment.code)
    rn._workers, rn._attempts = {}, {}
    rn._handle_stopping(job2.job_id, store.get_job(job2.job_id))
    assert any("✗" in c["argv"][4] for c in r.calls if c["argv"][2] == "send-text")


def test_ambient_env_never_reaches_real_herdr(steward_home, monkeypatch):
    """Env vars are scrubbed for every test; a job created via the CLI records no creator,
    so notify_creator never shells out (a real SubprocessRunner would blow up on herdr)."""
    import os

    assert "HERDR_PANE_ID" not in os.environ and "CLAUDE_CODE_SESSION_ID" not in os.environ
    from steward.runner_exec import SubprocessRunner

    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    store.kv_set(job.job_id, "created_by", __import__("steward.session", fromlist=["x"]).creator_from_env())
    calls = []
    monkeypatch.setattr(SubprocessRunner, "run", lambda self, argv, cwd, **k: calls.append(argv))
    assert push.notify_creator(store, job.job_id, "stopped", SubprocessRunner()) == "skipped:no_creator"
    assert calls == []


def test_conftest_guard_blocks_real_pane_sends():
    from steward.runner_exec import SubprocessRunner

    with pytest.raises(AssertionError):
        SubprocessRunner().run(["herdr", "pane", "send-text", "w:p1", "x"], "/")


def test_line_and_phone_text_render_label_and_id8():
    assert push.line("fix6 sonnet", push.DONE, "done", job_id=JID) == "[steward] ✓ fix6 sonnet (0123abcd) — done · nothing"
    assert push.line(None, push.DONE, "done", job_id=JID) == "[steward] ✓ 0123abcd — done · nothing"
    assert push.phone_text("fix6", push.DONE, "done", job_id=JID)[0] == "✓ fix6 (0123abcd)"
    assert push.phone_text("", push.DONE, "done", job_id=JID)[0] == "✓ 0123abcd"


def test_long_message_keeps_id8_and_shortens_what():
    msg = push.line("fix6 sonnet", push.NEEDS, "w" * 600, "answer", job_id=JID)
    assert len(msg) <= push._MAX and "fix6 sonnet (0123abcd) —" in msg and msg.endswith("· answer")
    title, _ = push.phone_text("L" * 80, push.NEEDS, "x", job_id=JID, title_cap=40)
    assert len(title) <= 40 and title.endswith("(0123abcd)")


@pytest.mark.parametrize("event", push.EVENTS)
def test_every_event_kind_renders_label_and_id8(steward_home, event):
    store = Store(steward_home)
    job = _job(store)
    store.kv_set(job.job_id, "short_name", "fix6 sonnet")
    r = _herdr([_pane()])
    assert push.notify_creator(store, job.job_id, event, r) in ("delivered", "held")
    if event == "auto_followup":
        assert push.flush_due(store, job.job_id, r, clock=lambda: 1e12) == "delivered"
    elif event in ("accepted", "auto_merged"):
        assert push.settle(store, job.job_id, r) == "delivered"
    sent = [a[4] for a in _sends(r) if a[2] == "send-text"]
    assert sent and all(f"fix6 sonnet ({job.job_id[:8]})" in m for m in sent)


def test_same_label_jobs_are_distinguishable(steward_home):
    store = Store(steward_home)
    a, b = _job(store), _job(store)
    for j in (a, b):
        store.kv_set(j.job_id, "short_name", "fix6 sonnet")
    r = _herdr([_pane()])
    push.notify_creator(store, a.job_id, "stopped", r)
    push.notify_creator(store, b.job_id, "stopped", r)
    msgs = [x[4] for x in _sends(r) if x[2] == "send-text"]
    assert len(msgs) == 2 and msgs[0] != msgs[1]
    assert a.job_id[:8] in msgs[0] and b.job_id[:8] in msgs[1]
