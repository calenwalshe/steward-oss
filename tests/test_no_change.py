"""Issue L: a finished job with no change of its own is closed RELEASED ('no change'), never ACCEPTED/incorporated."""
from steward import autopilot, cli, push
from steward.closeout import accept, closeout, decide_after_ruling
from steward.contract import Contract, store_contract
from steward.models import JobState
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store
from ruling_support import answer, green
from test_incorporate import git, mkjob, scratch  # noqa: F401  (scratch is a fixture)


def _job(store, scratch, tmp_path, files, state):
    job = mkjob(store, scratch, tmp_path, files, state=JobState.CREATED)
    store_contract(store, job.job_id, Contract(goal="g", boundaries=[], done_when=["done"], next_step="",
                                               authority=[], assumptions=[]))
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job.job_id))
    return job


def _runner(log):
    def respond(argv, cwd):
        log.append(argv)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)
    return FakeRunner(respond)


def _closeout_with_ruling(store, job_id, contract):
    """v2.7: a green closeout rests in CHECKS_PASSED until one completion ruling lands; then the code guards decide."""
    assert closeout(store, job_id, green("s"), [], contract) == JobState.CHECKS_PASSED
    assert answer(store, job_id) is not None
    return decide_after_ruling(store, job_id, contract)


def _released_no_change(store, job):
    assert store.get_job(job.job_id).state == JobState.RELEASED
    ev = [e for e in store.events(job.job_id) if e.what == "released_no_change"]
    assert len(ev) == 1 and ev[0].evidence["reason"] == "no change"
    assert not any(e.what in ("auto_merged", "incorporated", "preauthorized_local_acceptance") for e in store.events(job.job_id))


def test_autopilot_closeout_releases_empty_job_and_never_incorporates(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "auto")
    store = Store(steward_home)
    job = _job(store, scratch, tmp_path, {}, JobState.WORKER_FINISHED)
    contract = Contract(goal="g", boundaries=[], done_when=["done"], next_step="", authority=[], assumptions=[])

    assert _closeout_with_ruling(store, job.job_id, contract) == JobState.RELEASED
    _released_no_change(store, job)
    log = []
    res = autopilot.try_merge(store, job.job_id, _runner(log), checks=["pytest -q"])
    assert res["outcome"] == "skipped" and log == []
    assert git(scratch, "rev-list", "--count", "HEAD") == "1"
    autopilot.ensure_attempted(store, job.job_id, _runner(log))
    assert log == []  # delivery and autopilot only act on ACCEPTED jobs; this one is RELEASED
    assert not any(e.what == "incorporated" for e in store.events(job.job_id))


def test_manual_accept_releases_empty_job(steward_home, scratch, tmp_path, capsys):
    store = Store(steward_home)
    job = _job(store, scratch, tmp_path, {}, JobState.NEEDS_HUMAN)
    assert cli.main(["accept", job.job_id]) == 0
    assert "no change" in capsys.readouterr().out
    _released_no_change(store, job)


def test_manual_accept_function_releases_empty_job(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    job = _job(store, scratch, tmp_path, {}, JobState.NEEDS_HUMAN)
    assert accept(store, job.job_id) == JobState.RELEASED
    _released_no_change(store, job)


def test_one_line_change_is_accepted_as_before(steward_home, scratch, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "auto")
    store = Store(steward_home)
    contract = Contract(goal="g", boundaries=[], done_when=["done"], next_step="", authority=[], assumptions=[])
    auto = _job(store, scratch, tmp_path, {"x.txt": "one\n"}, JobState.WORKER_FINISHED)
    assert _closeout_with_ruling(store, auto.job_id, contract) == JobState.ACCEPTED
    manual = _job(store, scratch, tmp_path, {"y.txt": "one\n"}, JobState.NEEDS_HUMAN)
    assert cli.main(["accept", manual.job_id]) == 0
    assert "ACCEPTED" in capsys.readouterr().out
    assert store.get_job(manual.job_id).state == JobState.ACCEPTED


def test_creator_notice_text(steward_home, scratch, tmp_path, monkeypatch):
    store = Store(steward_home)
    job = _job(store, scratch, tmp_path, {}, JobState.RELEASED)
    sent = []
    monkeypatch.setattr(push, "_deliver", lambda s, j, ev, r, msg: sent.append(msg) or "delivered")
    push.notify_creator(store, job.job_id, "no_change", _runner([]), facts={"reason": "already fixed upstream"})
    assert sent and f"({job.job_id[:8]}) — no change needed: already fixed upstream" in sent[0]
