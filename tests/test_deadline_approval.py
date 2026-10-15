"""v2.2 Phase 22 Task 2 (DL-10, DL-11, DL-17): no outbound call without an exact, current, attempt-specific human approval.
Fake notifiers only; every assertion counts `send` calls. No real recipient is ever contacted."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deadline_fixtures import Repo, commit_deadline, plan_json
from steward import cli, deadline, deadline_outbox, deadline_plan, deadline_status, notify
from steward.models import Judgment
from steward.runner_exec import SubprocessRunner
from steward.store import Store

NOW = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
DUE = NOW + timedelta(hours=4)
RUN = SubprocessRunner()


class FakeNotifier:
    def __init__(self, result=True, raises=False):
        self.sent, self.result, self.raises = [], result, raises

    def send(self, event):
        self.sent.append((event.kind, event.title, event.body, event.deep_link, str(event.revision)))
        if self.raises:
            raise OSError("socket closed mid-send")
        return self.result


@pytest.fixture
def env(steward_home, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_NOTIFIER", "telegram")
    monkeypatch.setattr(deadline, "utcnow", lambda: NOW + timedelta(minutes=70))
    store = Store(steward_home)
    repo = Repo(tmp_path, store)
    commit_deadline(store, repo.job_id, now=NOW)
    deadline.set_mode(store, repo.job_id, "active", judgment=Judgment.human, origin="cli", request_id="mode")
    repo.commit({deadline_plan.plan_path(repo.job_id): plan_json(repo.job_id, 1, due=DUE, now=NOW,
                                                                  m1_target=NOW + timedelta(minutes=60))},
                "plan", when=NOW + timedelta(minutes=2))
    for m in (3, 61):
        deadline_status.sweep(store, repo.job_id, now=NOW + timedelta(minutes=m), runner=RUN, worktree=repo.wt,
                              force=True)
    key = "checkin:1:M1:2026-10-02T21:00:00Z"
    assert deadline_outbox.get_draft(store, key)["state"] == "local"
    return store, repo, key


def _approve(store, key, *, version=1, attempt=1, rid="a1", origin="cli", judgment=Judgment.human):
    return deadline_outbox.approve(store, key, expect_version=version, attempt=attempt, request_id=rid, origin=origin,
                                   judgment=judgment, now=NOW + timedelta(minutes=70))


def _deliver(store, notifier, t=1000.0):
    return deadline_outbox.deliver(store, notifier, now_s=t)


def test_unapproved_drafts_never_reach_a_notifier_even_in_active_mode(env):
    store, repo, key = env
    fake = FakeNotifier()
    for t in (1000.0, 2000.0, 3000.0):
        assert _deliver(store, fake, t) == []
    assert fake.sent == [] and deadline_outbox.get_draft(store, key)["state"] == "local"


def test_show_presents_exact_bytes_and_one_approval_is_one_invocation(env):
    store, repo, key = env
    shown = deadline_outbox.show(store, key)
    assert shown["channel"] == "telegram" and "Sam" in shown["recipient"]
    assert shown["version"] == 1 and shown["next_attempt"] == 1 and len(shown["body"]) <= 140
    _approve(store, key)
    fake = FakeNotifier()
    assert _deliver(store, fake) == [{"draft_key": key, "attempt": 1, "state": "sent"}]
    assert fake.sent == [("DEADLINE", shown["title"], shown["body"], shown["link"], f"{key}#1")]
    assert _deliver(store, fake, 2000.0) == [] and len(fake.sent) == 1  # consumed: never a second call
    (att,) = deadline_outbox.attempts(store, key)
    assert att["state"] == "sent" and att["approved_by"] == "cli" and att["consumed_at"]


def test_edit_after_approval_voids_it_and_needs_a_new_exact_approval(env):
    store, repo, key = env
    _approve(store, key)
    deadline_outbox.edit(store, key, judgment=Judgment.human, origin="cli", request_id="e1", body="changed body")
    fake = FakeNotifier()
    assert _deliver(store, fake) == [] and fake.sent == []
    with pytest.raises(deadline.StaleRevision):
        _approve(store, key, version=1, attempt=2, rid="a2")
    _approve(store, key, version=2, attempt=2, rid="a3")
    _deliver(store, fake)
    assert [s[2] for s in fake.sent] == ["changed body"]


def test_channel_or_recipient_change_reversions_and_blocks_the_old_approval(env, monkeypatch):
    store, repo, key = env
    _approve(store, key)
    monkeypatch.setenv("STEWARD_NOTIFIER", "moshi")
    assert deadline_outbox.refresh_channel(store, repo.job_id) >= 1
    fake = FakeNotifier()
    assert _deliver(store, fake) == [] and fake.sent == []
    assert deadline_outbox.get_draft(store, key)["version"] == 2


def test_stale_version_wrong_attempt_duplicate_request_and_denial(env):
    store, repo, key = env
    with pytest.raises(deadline.StaleRevision):
        _approve(store, key, version=7)
    with pytest.raises(deadline.DeadlineError):
        _approve(store, key, attempt=3)
    first = _approve(store, key, rid="same")
    again = _approve(store, key, rid="same")
    assert not first["duplicate"] and again["duplicate"] and len(deadline_outbox.attempts(store, key)) == 1
    deadline_outbox.deny(store, key, request_id="d1", origin="cli", judgment=Judgment.human)
    fake = FakeNotifier()
    assert _deliver(store, fake) == [] and fake.sent == []
    with pytest.raises(deadline.DeadlineError):
        _approve(store, key, attempt=2, rid="after-deny")


@pytest.mark.parametrize("origin,judgment", [("steer-agent", Judgment.human), ("cli", Judgment.code),
                                             ("deadline", Judgment.human), ("chatgpt", Judgment.human)])
def test_only_a_human_on_a_human_surface_can_approve(env, origin, judgment):
    store, repo, key = env
    with pytest.raises(deadline.AuthorityError):
        _approve(store, key, origin=origin, judgment=judgment)
    assert deadline_outbox.attempts(store, key) == []


def test_failed_and_unknown_sends_are_never_retried_without_a_new_approval(env):
    store, repo, key = env
    _approve(store, key)
    failing = FakeNotifier(result=False)
    assert _deliver(store, failing)[0]["state"] == "failed"
    assert _deliver(store, failing, 2000.0) == [] and len(failing.sent) == 1
    _approve(store, key, attempt=2, rid="retry")
    raising = FakeNotifier(raises=True)
    assert _deliver(store, raising, 3000.0)[0]["state"] == "unknown"
    assert _deliver(store, raising, 4000.0) == [] and len(raising.sent) == 1
    states = [a["state"] for a in deadline_outbox.attempts(store, key)]
    assert states == ["failed", "unknown"]


def test_expired_lease_after_a_crash_is_unknown_not_resent(env):
    store, repo, key = env
    _approve(store, key)
    claimed = deadline_outbox._claim(store, "dead-process", 1000.0, 60.0)  # claimed, then the process died
    assert claimed and deadline_outbox.attempts(store, key)[0]["consumed_at"]
    fake = FakeNotifier()
    assert _deliver(store, fake, 1100.0) == [] and fake.sent == []
    assert deadline_outbox.attempts(store, key)[0]["state"] == "unknown"
    assert any(e.what == "deadline_notice_unknown" for e in store.events(repo.job_id))


def test_catchup_batch_needs_its_own_approval_and_never_covers_children(env):
    store, repo, key = env
    deadline_status.sweep(store, repo.job_id, now=NOW + timedelta(minutes=300), runner=RUN, worktree=repo.wt, force=True)
    (catch,) = [d for d in deadline_outbox.drafts(store, repo.job_id) if d["kind"] == "catchup"]
    _approve(store, catch["draft_key"], rid="batch")
    fake = FakeNotifier()
    _deliver(store, fake)
    assert [s[4] for s in fake.sent] == [f"{catch['draft_key']}#1"]
    children = [d for d in deadline_outbox.drafts(store, repo.job_id) if d["kind"] in ("checkin", "due")]
    assert children and all(d["state"] == "local" for d in children)


def test_null_channel_keeps_everything_local(env, monkeypatch):
    store, repo, key = env
    monkeypatch.setenv("STEWARD_NOTIFIER", "null")
    deadline_outbox.refresh_channel(store, repo.job_id)
    with pytest.raises(deadline.DeadlineError, match="stays local"):
        _approve(store, key, version=2)


def test_relayed_cli_show_and_approve_then_control_tick_sends_once(env, capsys, monkeypatch):
    store, repo, key = env
    jid = repo.job_id
    assert cli.main(["deadline", jid, "notices"]) == 0
    assert cli.main(["deadline", jid, "notice", key, "show"]) == 0
    out = capsys.readouterr().out
    assert key in out and "--expect-version 1 --attempt 1" in out
    assert cli.main(["deadline", jid, "notice", key, "approve"]) == 1  # no version/attempt: refused
    assert cli.main(["deadline", jid, "notice", key, "approve", "--expect-version", "1", "--attempt", "1"]) == 0
    fake = FakeNotifier()
    monkeypatch.setattr(deadline_outbox, "notifier_for_channel", lambda channel, runner: fake)
    deadline_outbox.deliver_all(store, RUN)
    deadline_outbox.deliver_all(store, RUN)
    assert len(fake.sent) == 1


def test_existing_six_kinds_are_intact_and_emit_never_sends_a_deadline_notice(env):
    store, repo, key = env
    assert notify._KINDS == frozenset({"RUNNING", "NEEDS_YOU", "RECOVERY", "CHECKS_FAILED", "READY", "BUDGET"})
    other = store.create_job("plain", "/tmp")
    fake = FakeNotifier()
    ev = notify.PhoneEvent(kind="DEADLINE", job_id=other.job_id, title="t", body="b", deep_link=None, revision="x")
    assert notify.emit(store, fake, ev) is False and fake.sent == []


def test_sender_uses_the_same_channel_the_approval_was_checked_against(env, monkeypatch, tmp_path):
    """Review finding: `[deadline] channel` (checked) and $STEWARD_NOTIFIER (sender) could disagree."""
    store, repo, key = env
    _approve(store, key)
    from steward.runner_exec import CommandResult, FakeRunner

    runner = FakeRunner(lambda argv, cwd: CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="",
                                                        duration_s=0.0))
    monkeypatch.setattr(notify, "notifier_from_env", lambda r: (_ for _ in ()).throw(AssertionError("env notifier")))
    assert isinstance(deadline_outbox.notifier_for_channel("telegram", runner), notify.TelegramNotifier)
    assert deadline_outbox.notifier_for_channel("null", runner) is None
    out = deadline_outbox.deliver_all(store, runner)
    assert [o["state"] for o in out] == ["sent"]
    assert [c["argv"][:2] for c in runner.calls] == [["tg", "send"]]  # exactly one call, on the approved channel


def test_a_worker_process_relays_as_origin_worker_and_cannot_approve(env, monkeypatch):
    from pathlib import Path

    store, repo, key = env
    spawn_env = cli._worker_spawn_env(store, repo.job_id, Path("/x"))
    assert spawn_env["STEWARD_FORCED_ORIGIN"] == "worker"
    monkeypatch.setenv("STEWARD_FORCED_ORIGIN", "worker")
    assert cli.main(["deadline", repo.job_id, "notice", key, "approve", "--expect-version", "1", "--attempt", "1"]) != 0
    assert cli.main(["deadline", repo.job_id, "clear"]) != 0
    assert deadline_outbox.attempts(store, key) == [] and deadline.current(store, repo.job_id) is not None
