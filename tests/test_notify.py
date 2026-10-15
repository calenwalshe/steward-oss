import pytest

from ruling_support import StubLead
from steward import approvals, heartbeat, notify
from steward.contract import Contract, store_contract
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.models import JobState, Judgment
from steward.runner import Runner
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store
from steward.worker import FakeWorker


# -- PhoneEvent / deep_link ---------------------------------------------------


def test_phone_event_rejects_unknown_kind():
    with pytest.raises(ValueError):
        notify.PhoneEvent(
            kind="SOMETHING_ELSE", job_id="j", title="t", body="b", deep_link="", revision=0
        )


def test_phone_event_rejects_oversized_title_and_body():
    with pytest.raises(ValueError):
        notify.PhoneEvent(
            kind="RUNNING", job_id="j", title="x" * 41, body="b", deep_link="", revision=0
        )
    with pytest.raises(ValueError):
        notify.PhoneEvent(
            kind="RUNNING", job_id="j", title="t", body="x" * 141, deep_link="", revision=0
        )


def test_deep_link_includes_all_present_params_url_encoded():
    link = notify.deep_link("ws one", "tab one", "pane one")
    assert link == "moshi://herdr?workspace=ws%20one&tab=tab%20one&pane=pane%20one"


def test_deep_link_omits_missing_params():
    assert notify.deep_link("ws", None, "pane") == "moshi://herdr?workspace=ws&pane=pane"


def test_deep_link_is_none_when_workspace_unknown():
    assert notify.deep_link(None) is None
    assert notify.deep_link(None, "tab", "pane") is None


# -- notifiers -----------------------------------------------------------


def _event(kind="RUNNING", revision=1, job_id="job-1"):
    return notify.PhoneEvent(
        kind=kind, job_id=job_id, title="t", body="b", deep_link="moshi://herdr", revision=revision
    )


def test_null_notifier_always_succeeds():
    assert notify.NullNotifier().send(_event()) is True


def test_telegram_notifier_sends_via_injected_runner_and_reports_exit_code():
    fake = FakeRunner(lambda argv, cwd: CommandResult(
        argv=argv, cwd=cwd, exit_code=0, stdout="", stderr="", duration_s=0.0
    ))
    notifier = notify.TelegramNotifier(fake)

    ok = notifier.send(_event())

    assert ok is True
    assert fake.calls[0]["argv"][0] == "tg"
    assert fake.calls[0]["argv"][1] == "send"
    assert "t\nb\nmoshi://herdr" == fake.calls[0]["argv"][2]


def test_telegram_notifier_omits_link_line_when_deep_link_is_none():
    fake = FakeRunner(lambda argv, cwd: CommandResult(
        argv=argv, cwd=cwd, exit_code=0, stdout="", stderr="", duration_s=0.0
    ))
    notifier = notify.TelegramNotifier(fake)
    event = notify.PhoneEvent(
        kind="RUNNING", job_id="j", title="t", body="b", deep_link=None, revision=0
    )

    ok = notifier.send(event)

    assert ok is True
    assert fake.calls[0]["argv"][2] == "t\nb"


def test_telegram_notifier_reports_failure_on_nonzero_exit():
    fake = FakeRunner(lambda argv, cwd: CommandResult(
        argv=argv, cwd=cwd, exit_code=1, stdout="", stderr="boom", duration_s=0.0
    ))
    notifier = notify.TelegramNotifier(fake)

    assert notifier.send(_event()) is False


def test_moshi_notifier_construction_is_harmless_but_send_raises():
    notifier = notify.MoshiNotifier()  # must not raise
    with pytest.raises(NotImplementedError):
        notifier.send(_event())


def test_notifier_from_env_defaults_to_null(monkeypatch):
    monkeypatch.delenv("STEWARD_NOTIFIER", raising=False)
    assert isinstance(notify.notifier_from_env(FakeRunner()), notify.NullNotifier)


def test_notifier_from_env_selects_telegram(monkeypatch):
    monkeypatch.setenv("STEWARD_NOTIFIER", "telegram")
    assert isinstance(notify.notifier_from_env(FakeRunner()), notify.TelegramNotifier)


def test_notifier_from_env_selects_moshi_stub(monkeypatch):
    monkeypatch.setenv("STEWARD_NOTIFIER", "moshi")
    assert isinstance(notify.notifier_from_env(FakeRunner()), notify.MoshiNotifier)


# -- emit: dedupe + failure handling ------------------------------------------


def test_emit_dedupes_on_job_kind_revision(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    sent = []
    notifier = _RecordingNotifier(sent)

    first = notify.emit(store, notifier, _event(job_id=job.job_id, revision=1))
    second = notify.emit(store, notifier, _event(job_id=job.job_id, revision=1))
    third = notify.emit(store, notifier, _event(job_id=job.job_id, revision=2))

    assert first is True
    assert second is False  # deduped, not re-sent
    assert third is True  # new revision, sends again
    assert len(sent) == 2


def test_emit_records_notify_failed_event_on_notifier_exception_without_raising(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    class _Boom:
        def send(self, event):
            raise RuntimeError("network is down")

    ok = notify.emit(store, _Boom(), _event(job_id=job.job_id))

    assert ok is False
    events = [e for e in store.events(job.job_id) if e.what == "notify_failed"]
    assert len(events) == 1
    assert events[0].judgment == Judgment.code
    assert store.get_job(job.job_id).state == JobState.CREATED  # never touches job state


def test_emit_records_notify_failed_event_when_notifier_returns_false(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    class _Fails:
        def send(self, event):
            return False

    ok = notify.emit(store, _Fails(), _event(job_id=job.job_id))

    assert ok is False
    events = [e for e in store.events(job.job_id) if e.what == "notify_failed"]
    assert len(events) == 1


class _RecordingNotifier:
    def __init__(self, sink):
        self.sink = sink

    def send(self, event):
        self.sink.append(event)
        return True


# -- Runner wiring ------------------------------------------------------------


def _contracted_job(store, *, done_when=("checks pass",), goal="goal"):
    job = store.create_job(goal, "/repo")
    contract = Contract(
        goal=goal, boundaries=[], done_when=list(done_when), next_step="", authority=[], assumptions=[]
    )
    store_contract(store, job.job_id, contract)
    return job


def _git_and_tg_runner(*, head="sha-1", pytest_exit=0, tg_exit=0):
    tg_calls = []

    def respond(argv, cwd):
        if argv[:2] == ["tg", "send"]:
            tg_calls.append(argv)
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=tg_exit, stdout="", stderr="", duration_s=0.0)
        if argv[:2] == ["git", "rev-parse"]:
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=head + "\n", stderr="", duration_s=0.0)
        if argv[:2] == ["git", "status"]:
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)
        if argv[:2] == ["git", "diff"]:
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)
        if argv[:2] == ["bash", "-lc"]:
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=pytest_exit, stdout="", stderr="", duration_s=0.0)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    runner = FakeRunner(respond)
    return runner, tg_calls


def _fake_diagnoser(verdict="retry_with_guidance"):
    return FakeDiagnoser(Diagnosis(verdict=verdict, guidance="try again", hypothesis="hang"))


class _Factory:
    def __init__(self, scripts=None):
        self._scripts = list(scripts) if scripts else []

    def __call__(self, existing_ref):
        script = self._scripts.pop(0) if self._scripts else []
        return FakeWorker(script=script)


def test_start_emits_running_via_telegram(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_NOTIFIER", "telegram")
    store = Store(steward_home)
    job = _contracted_job(store)
    fake, tg_calls = _git_and_tg_runner()
    runner = Runner(store, _Factory(scripts=[[]]), fake, _fake_diagnoser())

    runner.start(job.job_id)

    assert len(tg_calls) == 1
    assert "↻" in tg_calls[0][2]


def test_running_notification_dedupes_across_repeated_starts_at_same_revision(steward_home, monkeypatch):
    # Not a realistic re-start path, but confirms emit()'s dedupe is really
    # wired through _notify rather than bypassed.
    monkeypatch.setenv("STEWARD_NOTIFIER", "telegram")
    store = Store(steward_home)
    job = _contracted_job(store)
    fake, tg_calls = _git_and_tg_runner()
    runner = Runner(store, _Factory(scripts=[[], []]), fake, _fake_diagnoser())

    runner._notify(job.job_id, "RUNNING", "Steward: running", "first")
    runner._notify(job.job_id, "RUNNING", "Steward: running", "second call, same revision")

    assert len(tg_calls) == 1


@pytest.mark.legacy_ownership_off  # Plan 1 G23: recovery parks receive technical review before owner notice
def test_needs_human_recovery_escalation_emits_needs_you_and_opens_approval(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_NOTIFIER", "telegram")
    store = Store(steward_home)
    job = _contracted_job(store)
    fake, tg_calls = _git_and_tg_runner()
    diagnoser = FakeDiagnoser(Diagnosis(verdict="needs_human", guidance="", hypothesis="stuck"))
    runner = Runner(store, _Factory(scripts=[[]]), fake, diagnoser)
    runner.start(job.job_id)
    worker = runner._workers[job.job_id]

    from steward.recovery import Action

    action = Action(kind="needs_human", detail="stuck: give up", judgment=Judgment.code, rule="diagnosis_verdict_needs_human")
    from steward.heartbeat import Signals

    sig = Signals(
        lifecycle="working", pid_alive=True, head_sha="sha-1", diff_fingerprint="fp",
        last_check_exit=None, error_fingerprint="", same_error_count=0,
        permission_prompt=False, running_process=None, since_last_change_s=0.0,
    )
    runner._apply_action(job.job_id, worker, action, sig)

    assert store.get_job(job.job_id).state == JobState.NEEDS_HUMAN
    needs_you_calls = [c for c in tg_calls if "◆" in c[2]]
    assert len(needs_you_calls) == 1
    pending = approvals.list_approvals(store, job.job_id, status="pending")
    assert len(pending) == 1
    assert pending[0]["question"] == "stuck: give up"


def test_replace_action_emits_recovery(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_NOTIFIER", "telegram")
    store = Store(steward_home)
    job = _contracted_job(store)
    fake, tg_calls = _git_and_tg_runner()
    runner = Runner(store, _Factory(scripts=[[], []]), fake, _fake_diagnoser())
    runner.start(job.job_id)
    worker = runner._workers[job.job_id]

    from steward.heartbeat import Signals
    from steward.recovery import Action

    action = Action(kind="replace", detail="worker process exited; replacing", judgment=Judgment.code, rule="worker_exited")
    sig = Signals(
        lifecycle="exited", pid_alive=False, head_sha="sha-1", diff_fingerprint="fp",
        last_check_exit=None, error_fingerprint="", same_error_count=0,
        permission_prompt=False, running_process=None, since_last_change_s=0.0,
    )
    runner._apply_action(job.job_id, worker, action, sig)

    recovery_calls = [c for c in tg_calls if "replaced worker" in c[2]]
    assert len(recovery_calls) == 1


@pytest.mark.legacy_ownership_off
def test_last_recovery_before_budget_exhausted_emits_budget(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_NOTIFIER", "telegram")
    store = Store(steward_home)
    job = _contracted_job(store)
    fake, tg_calls = _git_and_tg_runner()
    runner = Runner(store, _Factory(scripts=[[]]), fake, _fake_diagnoser())
    runner.start(job.job_id)
    worker = runner._workers[job.job_id]
    store.kv_set(job.job_id, "recoveries_used", 1)  # max_recoveries=2 -> this is the last one

    from steward.heartbeat import Signals
    from steward.recovery import Action

    action = Action(kind="guide", detail="try again", judgment=Judgment.code, rule="diagnosis_verdict_retry_with_guidance")
    sig = Signals(
        lifecycle="working", pid_alive=True, head_sha="sha-1", diff_fingerprint="fp",
        last_check_exit=None, error_fingerprint="", same_error_count=0,
        permission_prompt=False, running_process=None, since_last_change_s=0.0,
    )
    runner._apply_action(job.job_id, worker, action, sig)

    budget_calls = [c for c in tg_calls if "◆" in c[2]]
    assert len(budget_calls) == 1
    assert "1 recovery attempt remaining" in budget_calls[0][2]


def test_closeout_accepted_emits_ready(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_NOTIFIER", "telegram")
    store = Store(steward_home)
    job = _contracted_job(store)
    worktree = store.home / "worktrees" / job.job_id
    worktree.mkdir(parents=True)
    (worktree / "pyproject.toml").write_text("[tool.pytest.ini_options]\n# pytest\n")
    store.set_state(job.job_id, JobState.RUNNING, what="r", evidence={}, rule="r", judgment=Judgment.code)
    store.set_state(job.job_id, JobState.WORKER_FINISHED, what="w", evidence={}, rule="r", judgment=Judgment.code)
    store.add_check(job.job_id, commit_sha="base-sha", command="pytest", exit_code=0, results={}, label="baseline")
    fake, tg_calls = _git_and_tg_runner(pytest_exit=0)
    runner = Runner(store, _Factory(), fake, _fake_diagnoser())

    runner.job_lead = StubLead(store)

    runner._run_closeout(job.job_id, heartbeat.Class.progressing)
    assert store.get_job(job.job_id).state == JobState.CHECKS_PASSED  # waits for its completion ruling
    runner.job_lead.on_event(job.job_id)
    runner.tick(job.job_id)  # the ruling is in: decide, notify

    assert store.get_job(job.job_id).state == JobState.ACCEPTED
    ready_calls = [c for c in tg_calls if "✓" in c[2]]
    assert len(ready_calls) == 1


def test_closeout_new_failures_emits_checks_failed(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_NOTIFIER", "telegram")
    store = Store(steward_home)
    job = _contracted_job(store)
    worktree = store.home / "worktrees" / job.job_id
    worktree.mkdir(parents=True)
    (worktree / "pyproject.toml").write_text("[tool.pytest.ini_options]\n# pytest\n")
    store.set_state(job.job_id, JobState.RUNNING, what="r", evidence={}, rule="r", judgment=Judgment.code)
    store.set_state(job.job_id, JobState.WORKER_FINISHED, what="w", evidence={}, rule="r", judgment=Judgment.code)
    store.add_check(job.job_id, commit_sha="base-sha", command="pytest", exit_code=0, results={}, label="baseline")
    fake, tg_calls = _git_and_tg_runner(pytest_exit=1)
    runner = Runner(store, _Factory(), fake, _fake_diagnoser())

    runner._run_closeout(job.job_id, heartbeat.Class.progressing)

    assert store.get_job(job.job_id).state == JobState.RUNNING  # sent back with guidance
    failed_calls = [c for c in tg_calls if "✗" in c[2]]
    assert len(failed_calls) == 1
    assert "1 check(s) still failing" in failed_calls[0][2]


@pytest.mark.legacy_ownership_off  # Plan 1 G30: missing completion criteria is reviewed as a technical contract blocker
def test_closeout_needs_human_from_acceptance_unresolved_emits_ready_and_acceptance_approval(
    steward_home, monkeypatch
):
    monkeypatch.setenv("STEWARD_NOTIFIER", "telegram")
    store = Store(steward_home)
    job = _contracted_job(store, done_when=())  # no done_when -> acceptance unresolved
    worktree = store.home / "worktrees" / job.job_id
    worktree.mkdir(parents=True)
    store.set_state(job.job_id, JobState.RUNNING, what="r", evidence={}, rule="r", judgment=Judgment.code)
    store.set_state(job.job_id, JobState.WORKER_FINISHED, what="w", evidence={}, rule="r", judgment=Judgment.code)
    fake, tg_calls = _git_and_tg_runner()
    runner = Runner(store, _Factory(), fake, _fake_diagnoser())

    runner._run_closeout(job.job_id, heartbeat.Class.progressing)

    assert store.get_job(job.job_id).state == JobState.NEEDS_HUMAN
    ready_calls = [c for c in tg_calls if "✓" in c[2]]
    assert len(ready_calls) == 1
    pending = approvals.list_approvals(store, job.job_id, status="pending")
    assert len(pending) == 1
    assert pending[0]["question"] == approvals.ACCEPTANCE_QUESTION
