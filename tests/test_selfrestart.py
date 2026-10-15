"""Runner self-restart: fakes only (exec, clock, import check, git)."""
from __future__ import annotations

from types import SimpleNamespace

from steward import selfrestart, stability, watchers
from steward.runner import Runner
from steward.selfrestart import SelfRestarter
from steward.store import Store
from test_stability import _contracted_job

ARGV = ["/usr/bin/python3", "-m", "steward", "run", "JOB", "--worker", "herdr", "--pane", "p1"]


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _setup(steward_home, *, behind=2, check=None, clock=None, env=None):
    store = Store(steward_home)
    job = _contracted_job(store)
    stability.record_runner_code(store, job.job_id, head="oldhead", started_at=1.0, pid=1)
    execs, checks = [], []

    def fake_check():
        checks.append(1)
        return check

    sr = SelfRestarter(store, behind_fn=lambda j: behind, head_fn=lambda: "newhead",
                       exec_fn=lambda path, argv: execs.append((path, argv)), import_check_fn=fake_check,
                       clock=clock or Clock(), argv=ARGV, env=env if env is not None else {},
                       flush_fn=lambda: None)
    return store, job, sr, execs, checks


def _whats(store, job_id):
    return [e.what for e in store.events(job_id)]


def test_restarts_when_outdated_records_event_and_preserves_argv(steward_home):
    store, job, sr, execs, checks = _setup(steward_home)
    assert sr.maybe_restart(job.job_id) == "restarted"
    assert execs == [(ARGV[0], ARGV)]
    ev = next(e for e in store.events(job.job_id) if e.what == "runner_self_restart")
    assert ev.evidence["old_head"] == "oldhead" and ev.evidence["new_head"] == "newhead"
    assert ev.evidence["argv"] == ARGV
    assert checks == [1]


def test_event_is_recorded_before_exec(steward_home):
    store, job, sr, _, _ = _setup(steward_home)
    seen = []
    sr.exec_fn = lambda path, argv: seen.append(_whats(store, job.job_id))
    sr.maybe_restart(job.job_id)
    assert "runner_self_restart" in seen[0]


def test_current_code_does_nothing(steward_home):
    store, job, sr, execs, checks = _setup(steward_home, behind=0)
    assert sr.maybe_restart(job.job_id) == "current"
    assert not execs and not checks


def test_rate_limited_to_one_per_five_minutes(steward_home):
    clock = Clock()
    store, job, sr, execs, _ = _setup(steward_home, clock=clock)
    assert sr.maybe_restart(job.job_id) == "restarted"
    clock.t += 299
    assert sr.maybe_restart(job.job_id) == "rate_limited"
    # the stamp lives in the store, so a fresh process (new SelfRestarter) is limited too
    sr2 = SelfRestarter(store, behind_fn=lambda j: 1, exec_fn=lambda *a: execs.append(a), clock=clock,
                        import_check_fn=lambda: None, argv=ARGV, env={}, flush_fn=lambda: None)
    assert sr2.maybe_restart(job.job_id) == "rate_limited"
    clock.t += 2
    assert sr.maybe_restart(job.job_id) == "restarted"
    assert len(execs) == 2


def test_broken_new_code_keeps_old_and_records_skip(steward_home):
    clock = Clock()
    store, job, sr, execs, checks = _setup(steward_home, check="SyntaxError: bad", clock=clock)
    assert sr.maybe_restart(job.job_id) == "skipped"
    assert not execs
    ev = next(e for e in store.events(job.job_id) if e.what == "runner_self_restart_skipped")
    assert ev.evidence["error"] == "SyntaxError: bad"
    assert "runner_self_restart" not in _whats(store, job.job_id)
    # the badge still shows: nothing touched the recorded code, and the broken import isn't retried every tick
    assert stability.runner_outdated(store, job.job_id, count_fn=lambda sha: 2) == 2
    assert sr.maybe_restart(job.job_id) == "rate_limited"
    assert checks == [1]


def test_disabled_by_env(steward_home):
    for v in ("0", "false", "off"):
        store, job, sr, execs, checks = _setup(steward_home, env={"STEWARD_RUNNER_AUTORESTART": v})
        assert sr.maybe_restart(job.job_id) == "disabled"
        assert not execs and not checks
    assert selfrestart.enabled({}) and selfrestart.enabled({"STEWARD_RUNNER_AUTORESTART": "1"})


def test_git_failure_never_raises(steward_home):
    store, job, sr, execs, _ = _setup(steward_home)
    sr.behind_fn = lambda j: (_ for _ in ()).throw(RuntimeError("git"))
    assert sr.maybe_restart(job.job_id) == "current"


def test_real_import_check_passes_on_this_tree():
    assert selfrestart.import_check() is None


def test_import_check_reports_error(tmp_path):
    (tmp_path / "steward").mkdir()
    (tmp_path / "steward" / "__init__.py").write_text("")
    (tmp_path / "steward" / "cli.py").write_text("raise RuntimeError('boom')\n")
    (tmp_path / "steward" / "runner.py").write_text("")
    err = selfrestart.import_check(tmp_path)
    assert err and "boom" in err


def test_run_forever_restarts_only_between_ticks(steward_home):
    store, job, sr, execs, _ = _setup(steward_home)
    runner = Runner(store, lambda ref: None, None, None)
    state = {"in_tick": False, "calls": []}
    report = SimpleNamespace(job_id=job.job_id, state="running", cls=None, action=None, detail="")

    def tick(job_id):
        state["in_tick"] = True
        state["calls"].append("tick")
        state["in_tick"] = False
        return report

    def restart(job_id):
        assert not state["in_tick"]
        state["calls"].append("restart")

    runner.tick = tick
    runner._alerts = lambda j: state["calls"].append("alerts")
    runner._sleep_between_ticks = lambda j, i, s: state["calls"].append("sleep")
    runner.run_forever(job.job_id, max_ticks=2, self_restart=restart)
    assert state["calls"] == ["tick", "alerts", "restart", "sleep", "tick", "alerts"]


def test_bounded_run_never_installs_default_restarter(steward_home):
    store, job, _, _, _ = _setup(steward_home)
    runner = Runner(store, lambda ref: None, None, None)
    report = SimpleNamespace(job_id=job.job_id, state="running", cls=None, action=None, detail="")
    runner.tick = lambda j: report
    runner._alerts = lambda j: None
    runner._sleep_between_ticks = lambda *a: None
    assert runner.run_forever(job.job_id, max_ticks=3) is report  # no exec, no error


def test_watchers_follower_uses_process_head_and_own_stamp(steward_home, monkeypatch):
    store = Store(steward_home)
    job = _contracted_job(store)
    monkeypatch.setattr(stability, "commits_behind", lambda head, *a, **k: 4 if head == stability.PROCESS_HEAD else 0)
    sr = selfrestart.follower(store)
    execs = []
    sr.exec_fn, sr.import_check_fn, sr.argv, sr.flush_fn = (lambda p, a: execs.append(a), lambda: None,
                                                            ["py", "-m", "steward", "watchers", job.job_id, "--follow"],
                                                            lambda: None)
    assert sr.maybe_restart(job.job_id) == "restarted"
    assert execs == [["py", "-m", "steward", "watchers", job.job_id, "--follow"]]
    assert store.kv_get(job.job_id, "watchers_self_restart_at") is not None
    assert store.kv_get(job.job_id, selfrestart.STAMP_KEY) is None  # runner's stamp untouched


def test_run_feed_calls_self_restart_between_polls(steward_home):
    import io

    store = Store(steward_home)
    job = _contracted_job(store)
    calls = []

    def restart(jid):
        calls.append(jid)
        raise KeyboardInterrupt  # stop the follow loop after one poll

    watchers.run_feed(store, job.job_id, follow=True, out=io.StringIO(), width_fn=lambda: 100,
                      sleep=lambda s: None, self_restart=restart)
    assert calls == [job.job_id]


def test_original_argv_uses_an_absolute_interpreter(monkeypatch):
    # Regression (live 2026-09-30): the status pane started as bare `python3 -m steward.dashapp` re-exec'd with
    # os.execv('python3', ...) -> FileNotFoundError and the pane died.
    import sys
    from steward import selfrestart
    monkeypatch.setattr(sys, "orig_argv", ["python3", "-m", "steward.dashapp"], raising=False)
    argv = selfrestart.original_argv()
    assert argv[0] == sys.executable and argv[1:] == ["-m", "steward.dashapp"]


def test_runner_waits_for_the_deploy_of_a_steward_self_merge(steward_home):
    """Sam's B (job f257aa7b): runners must not move onto a self-merge ahead of the control pane; they follow once
    the deploy is done (the mark is cleared), and a forgotten mark never freezes them for good."""
    from steward import deploy_pending

    store, job, sr, execs, checks = _setup(steward_home)
    deploy_pending.mark(store, "merging-job")
    assert sr.maybe_restart(job.job_id) == "awaiting_deploy"
    assert execs == [] and checks == [] and "runner_self_restart" not in _whats(store, job.job_id)
    deploy_pending.clear(store, "another-job")  # someone else's merge ending does not release this one
    assert sr.maybe_restart(job.job_id) == "awaiting_deploy"
    deploy_pending.clear(store, "merging-job")
    assert sr.maybe_restart(job.job_id) == "restarted" and len(execs) == 1


def test_a_stale_deploy_mark_is_ignored(steward_home):
    import time

    from steward import deploy_pending

    store, job, sr, execs, _ = _setup(steward_home)
    deploy_pending.mark(store, "dead-autopilot", now=time.time() - deploy_pending.MAX_AGE_S - 1)
    assert deploy_pending.pending(store) is None
    assert sr.maybe_restart(job.job_id) == "restarted"
