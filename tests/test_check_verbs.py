"""Phase 42 (SRD-13..15): Steward-owned long processes - `steward check run|wait|await`.

The worker never runs a full suite in its own turn: the runner's permission hook submits it as a `check_runs` row
(purpose `worker`), the heavy slot runs it, and the worker waits at zero model cost or parks and is woken with the
result. No real Herdr, git server, model or network: git runs in tmp repos, the executor spawn is injected.
"""
from __future__ import annotations

import subprocess

import pytest

from steward import check_runs as cr
from steward.baseline import CheckRun
from steward.runner_exec import SubprocessRunner
from steward.store import Store


def _repo(tmp_path, *, dirty=False):
    repo = tmp_path / "wt"
    repo.mkdir(parents=True)
    for argv in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "a@b"],
                 ["git", "config", "user.name", "a"]):
        subprocess.run(argv, cwd=repo, check=True)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "a"], cwd=repo, check=True)
    if dirty:
        (repo / "b.py").write_text("y = 2\n")
    return repo


def _job(store, repo):
    return store.create_job("goal", str(repo))


# -- task 1: submitting a worker run -------------------------------------------------------------------------


def test_submit_enqueues_a_worker_run_on_the_committed_tree(steward_home, tmp_path):
    store, repo = Store(steward_home), _repo(tmp_path)
    job = _job(store, repo)
    sub = cr.submit_worker_run(store, SubprocessRunner(), repo, job.job_id, "python3 -m pytest -q", now=5.0,
                               toolchain_fn=lambda c: "tc")
    assert sub.error is None and sub.row.state == "queued" and sub.row.heavy
    assert sub.row.requested_by == job.job_id
    (w,) = cr.waiters(store, sub.row.run_id)
    assert (w["job_id"], w["purpose"]) == (job.job_id, "worker")
    rec = store.kv_get(job.job_id, f"worker_check:{sub.row.run_id}")
    assert rec["command"] == "python3 -m pytest -q" and len(rec["sha"]) == 40


def test_submit_refuses_a_dirty_tree_with_commit_first(steward_home, tmp_path):
    store, repo = Store(steward_home), _repo(tmp_path, dirty=True)
    job = _job(store, repo)
    sub = cr.submit_worker_run(store, SubprocessRunner(), repo, job.job_id, "python3 -m pytest -q", now=5.0,
                               toolchain_fn=lambda c: "tc")
    assert sub.row is None and "commit first" in sub.error
    assert store._conn.execute("SELECT COUNT(*) FROM check_runs").fetchone()[0] == 0


def test_second_submit_of_the_same_tree_attaches_to_the_queued_run(steward_home, tmp_path):
    store, repo = Store(steward_home), _repo(tmp_path)
    job = _job(store, repo)
    a = cr.submit_worker_run(store, SubprocessRunner(), repo, job.job_id, "python3 -m pytest -q", now=5.0,
                             toolchain_fn=lambda c: "tc")
    b = cr.submit_worker_run(store, SubprocessRunner(), repo, job.job_id, "python3 -m pytest -q", now=6.0,
                             toolchain_fn=lambda c: "tc")
    assert a.row.run_id == b.row.run_id and b.how == "attached"


def _finish(store, row, *, exit_code=0, tests=None, now=20.0):
    assert cr.try_start(store, row.run_id, owner="o", pid=1, now=now)
    return cr.complete(store, row.run_id, owner="o", run=CheckRun(exit_code=exit_code, tests=tests or {}, used=row.command,
                                                                  output="out"), junit_src=None, now=now + 3)


def test_result_summary_names_exit_counts_and_failures(steward_home, tmp_path):
    store, repo = Store(steward_home), _repo(tmp_path)
    job = _job(store, repo)
    row = cr.submit_worker_run(store, SubprocessRunner(), repo, job.job_id, "python3 -m pytest -q", now=5.0,
                               toolchain_fn=lambda c: "tc").row
    done = _finish(store, row, exit_code=1, tests={"t::a": "passed", "t::b": "failed", "t::c": "failed"})
    s = cr.result_summary(done)
    assert "exit 1" in s and "1 passed" in s and "2 failed" in s and "t::b" in s and done.run_id in s
    green = cr.result_summary(_finish(store, cr.get(store, cr.submit_worker_run(
        store, SubprocessRunner(), _repo(tmp_path / "x"), job.job_id, "make t", now=9.0, toolchain_fn=lambda c: "tc"
    ).row.run_id), tests={"t::a": "passed"}))
    assert "exit 0" in green and "passed" in green


def test_result_summary_for_a_lost_or_failed_run_is_never_a_pass(steward_home, tmp_path):
    store, repo = Store(steward_home), _repo(tmp_path)
    job = _job(store, repo)
    row = cr.submit_worker_run(store, SubprocessRunner(), repo, job.job_id, "python3 -m pytest -q", now=5.0,
                               toolchain_fn=lambda c: "tc").row
    assert cr.try_start(store, row.run_id, owner="o", pid=1, now=6.0)
    cr.fail(store, row.run_id, owner="o", error="temp worktree failed", now=7.0)
    s = cr.result_summary(cr.get(store, row.run_id))
    assert "failed" in s and "temp worktree failed" in s and "exit 0" not in s


def test_executor_runs_the_submitted_run_through_the_heavy_slot(steward_home, tmp_path):
    store, repo = Store(steward_home), _repo(tmp_path)
    job = _job(store, repo)
    sub = cr.submit_worker_run(store, SubprocessRunner(), repo, job.job_id, "python3 -c 'print(1)'", now=5.0,
                               toolchain_fn=lambda c: "tc")
    ran = []

    from steward.runner_exec import CommandResult

    class Runner(SubprocessRunner):
        def shell_line(self, line, cwd, *, timeout=None, env=None):
            ran.append((line, str(cwd)))
            return CommandResult(argv=["bash", "-lc", line], cwd=str(cwd), exit_code=0, stdout="ok", stderr="",
                                 duration_s=0.1)

    out = cr.execute_worker_run(store, sub.row.run_id, job.job_id, Runner(), toolchain_fn=lambda c: "tc")
    row = cr.get(store, sub.row.run_id)
    assert out == "done" and row.state == "done" and row.exit_code == 0
    assert ran and ran[0][0] == "python3 -c 'print(1)'" and str(repo) != ran[0][1]  # an isolated temp tree, not the worker's
    assert [w["consumed_epoch"] is not None for w in cr.waiters(store, row.run_id)] == [True]


def test_purpose_worker_is_a_known_purpose():
    assert "worker" in cr.PURPOSES


def test_check_run_steers_are_nudges_and_never_urgent(steward_home, tmp_path):
    from steward.steering import Steering

    store = Store(steward_home)
    job = _job(store, _repo(tmp_path))
    st = Steering(store)
    with pytest.raises(ValueError, match="urgent"):
        st.record(job.job_id, "nudge", "x", origin="check_run", urgent=True)
    with pytest.raises(ValueError, match="nudge"):
        st.record(job.job_id, "goal", "x", origin="check_run")
    assert st.record(job.job_id, "nudge", "x", origin="check_run").accepted


# -- task 4: the runner's permission hook ---------------------------------------------------------------------

from steward import approvals as approvals_module  # noqa: E402
from steward import waits  # noqa: E402
from steward.runner import Runner  # noqa: E402
from steward.steering import Steering  # noqa: E402
from test_runner_fabric import (  # noqa: E402
    _Clock, _contracted_job, _fake_diagnoser, _FabricFakeWorker, _frame, _git_runner, _obs)

SUITE_AUTH = ["run: python3 -m pytest"]


def _bash(seq, rid, command, **extra):
    return _frame(seq, "permission_request", {"request_id": rid, "tool": "Bash",
                                              "input": {"command": command, **extra}})


def _hook(steward_home, *frames, authority=SUITE_AUTH, git=None, extra_script=()):
    store = Store(steward_home)
    job = _contracted_job(store, authority=list(authority))
    worker = _FabricFakeWorker(script=[_obs(lifecycle="blocked", events=tuple(frames)), *extra_script])
    runner = Runner(store, lambda ref: worker, git or _git_runner(), _fake_diagnoser(), clock=_Clock())
    spawned = []
    runner.check_spawn = lambda run_id, job_id: spawned.append((run_id, job_id))
    runner.start(job.job_id)
    runner.tick(job.job_id)
    perms = {a["worker_request_id"]: a for a in approvals_module.list_approvals(store, job.job_id)
             if a["kind"] == "permission"}
    return store, job, worker, perms, spawned, runner


def _runs(store):
    return store._conn.execute("SELECT * FROM check_runs").fetchall()


def test_check_run_is_denied_with_the_run_id_and_queues_a_worker_run(steward_home):
    store, job, worker, perms, spawned, _ = _hook(
        steward_home, _bash(3, "r1", "steward check run python3 -m pytest -q tests/test_a.py"))
    (run,) = _runs(store)
    assert perms["r1"]["status"] == "denied" and perms["r1"]["decided_by"] == "code"
    assert run["state"] == "queued" and run["requested_by"] == job.job_id and run["command"].endswith("test_a.py")
    (w,) = cr.waiters(store, run["run_id"])
    assert w["purpose"] == "worker"
    (msg,) = worker.answer_messages
    assert f"submitted run {run['run_id']}" in msg
    assert f"steward check wait {run['run_id']}" in msg and f"steward check await {run['run_id']}" in msg
    assert spawned == [(run["run_id"], job.job_id)]
    assert [e.what for e in store.events(job.job_id) if e.what == "check_run_submitted"] == ["check_run_submitted"]


def test_check_run_on_a_dirty_tree_says_commit_first_and_queues_nothing(steward_home):
    store, job, worker, perms, spawned, _ = _hook(
        steward_home, _bash(3, "r1", "steward check run python3 -m pytest -q tests/test_a.py"),
        git=_git_runner(untracked=("scratch.py",)))
    assert perms["r1"]["status"] == "denied" and "commit first" in worker.answer_messages[0]
    assert _runs(store) == [] and spawned == []


def test_a_bare_full_suite_is_intercepted_like_check_run(steward_home):
    store, job, worker, perms, spawned, _ = _hook(steward_home, _bash(3, "r1", "python3 -m pytest -q"), authority=[])
    (run,) = _runs(store)
    assert perms["r1"]["status"] == "denied"
    assert run["command"] == "python3 -m pytest -q" and run["state"] == "queued"
    assert f"submitted run {run['run_id']}" in worker.answer_messages[0] and spawned


def test_a_targeted_test_run_is_left_to_the_worker(steward_home):
    store, job, worker, perms, spawned, _ = _hook(
        steward_home, _bash(3, "r1", "python3 -m pytest -q tests/test_a.py::test_x"))
    assert perms["r1"]["status"] == "approved" and _runs(store) == [] and spawned == []


def test_check_run_refuses_a_command_the_job_is_not_authorized_to_run(steward_home):
    store, job, worker, perms, spawned, _ = _hook(steward_home, _bash(3, "r1", "steward check run rm -rf build"),
                                                  authority=SUITE_AUTH)
    assert perms["r1"]["status"] == "denied" and "not an authorized check command" in worker.answer_messages[0]
    assert _runs(store) == [] and spawned == []


def test_check_run_and_full_suite_attach_to_one_queued_run(steward_home):
    store, job, worker, perms, spawned, _ = _hook(
        steward_home, _bash(3, "a", "steward check run python3 -m pytest -q"), _bash(4, "b", "python3 -m pytest -q"))
    (run,) = _runs(store)
    assert perms["a"]["status"] == perms["b"]["status"] == "denied"
    assert all(run["run_id"] in m for m in worker.answer_messages)


def test_check_await_parks_a_check_run_wait_and_denies_with_end_your_turn(steward_home):
    store, job, worker, perms, spawned, runner = _hook(
        steward_home, _bash(3, "r1", "steward check run python3 -m pytest -q"))
    (run,) = _runs(store)
    runner.worker = worker
    frames = (_bash(5, "r2", f"steward check await {run['run_id']}"),)
    worker.script.append(_obs(lifecycle="blocked", events=frames))
    runner.tick(job.job_id)
    perms = {a["worker_request_id"]: a for a in approvals_module.list_approvals(store, job.job_id)
             if a["kind"] == "permission"}
    assert perms["r2"]["status"] == "denied" and "end your turn now" in worker.answer_messages[-1]
    (wait,) = waits.waiting(store, job.job_id)
    assert wait["kind"] == "check_run" and wait["params"] == {"run_id": run["run_id"]} and wait["blocker_id"] is None


def test_check_await_on_a_finished_run_returns_the_result_instead_of_parking(steward_home):
    store, job, worker, perms, spawned, runner = _hook(
        steward_home, _bash(3, "r1", "steward check run python3 -m pytest -q"))
    (run,) = _runs(store)
    assert cr.try_start(store, run["run_id"], owner="o", pid=1, now=9.0)
    cr.complete(store, run["run_id"], owner="o", run=CheckRun(exit_code=0, tests={"t::a": "passed"},
                                                              used="x", output=""), junit_src=None, now=10.0)
    worker.script.append(_obs(lifecycle="blocked", events=(_bash(5, "r2", f"steward check await {run['run_id']}"),)))
    runner.tick(job.job_id)
    assert waits.waiting(store, job.job_id) == []
    assert "exit 0" in worker.answer_messages[-1] and "already finished" in worker.answer_messages[-1]


def test_check_await_on_an_unknown_run_is_denied_not_parked(steward_home):
    store, job, worker, perms, spawned, _ = _hook(steward_home, _bash(3, "r1", "steward check await cr-nope"))
    assert perms["r1"]["status"] == "denied" and "unknown run" in worker.answer_messages[0]
    assert waits.waiting(store, job.job_id) == []


def test_check_wait_is_approved_by_code_without_owner_involvement(steward_home):
    store, job, worker, perms, spawned, _ = _hook(steward_home, _bash(3, "r1", "steward check wait cr-0123456789ab"),
                                                  authority=[])
    assert perms["r1"]["status"] == "approved" and perms["r1"]["decided_by"] == "code"
    assert [e.what for e in store.events(job.job_id) if e.what == "check_wait_auto_approved"]


def test_a_chained_check_verb_is_an_ordinary_call_not_a_submission(steward_home):
    store, job, worker, perms, spawned, _ = _hook(
        steward_home, _bash(3, "r1", "cd . && steward check run python3 -m pytest -q"), authority=[])
    assert _runs(store) == [] and spawned == []


def test_wait_policy_off_disables_the_interception(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_WAIT_POLICY", "off")
    store, job, worker, perms, spawned, _ = _hook(steward_home, _bash(3, "r1", "python3 -m pytest -q"), authority=[])
    assert _runs(store) == [] and spawned == []


def test_runner_wakes_the_worker_with_exactly_one_result_steer(steward_home):
    store, job, worker, perms, spawned, runner = _hook(
        steward_home, _bash(3, "r1", "steward check run python3 -m pytest -q"))
    (run,) = _runs(store)
    waits.park(store, job.job_id, blocker_id=None, kind="check_run", params={"run_id": run["run_id"]},
               reason="r", now=1.0)
    runner.tick(job.job_id)
    assert worker.delivered == [] and len(waits.waiting(store, job.job_id)) == 1  # not finished: nothing happens
    assert cr.try_start(store, run["run_id"], owner="o", pid=1, now=9.0)
    cr.complete(store, run["run_id"], owner="o", run=CheckRun(exit_code=1, tests={"t::b": "failed"}, used="x",
                                                              output=""), junit_src=None, now=10.0)
    runner.tick(job.job_id)
    runner.tick(job.job_id)
    steers = [t for t in worker.delivered if "[steward] check run" in t]
    assert len(steers) == 1 and "exit 1" in steers[0] and "t::b" in steers[0]
    assert waits.waiting(store, job.job_id) == []


# -- task 5: the CLI verbs ------------------------------------------------------------------------------------


def _done_run(store, job_id, tmp_path, *, exit_code=0, tests=None):
    row = cr.submit_worker_run(store, SubprocessRunner(), _repo(tmp_path), job_id, "python3 -m pytest -q", now=1.0,
                               toolchain_fn=lambda c: "tc").row
    assert cr.try_start(store, row.run_id, owner="o", pid=1, now=2.0)
    cr.complete(store, row.run_id, owner="o", run=CheckRun(exit_code=exit_code, tests=tests or {"t::a": "passed"},
                                                           used="x", output=""), junit_src=None, now=5.0)
    return row


def _cli(capsys, monkeypatch, steward_home, tmp_path, *argv, registry=True):
    from steward import cli

    monkeypatch.setenv("STEWARD_HOME", str(steward_home))
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(tmp_path / "exc.toml"))
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_wait_for_result_polls_until_final_without_a_model(steward_home, tmp_path):
    store = Store(steward_home)
    job = _job(store, _repo(tmp_path))
    row = cr.submit_worker_run(store, SubprocessRunner(), _repo(tmp_path / "w"), job.job_id, "make t", now=1.0,
                               toolchain_fn=lambda c: "tc").row
    ticks = []

    def sleep(s):
        ticks.append(s)
        if len(ticks) == 3:
            assert cr.try_start(store, row.run_id, owner="o", pid=1, now=2.0)
            cr.complete(store, row.run_id, owner="o", run=CheckRun(exit_code=0, tests={"t::a": "passed"}, used="x",
                                                                   output=""), junit_src=None, now=3.0)

    now = iter(range(0, 10_000))
    code, text = cr.wait_for_result(store, row.run_id, timeout=600.0, poll=2.0, sleep=sleep,
                                    clock=lambda: float(next(now)))
    assert code == 0 and "exit 0" in text and len(ticks) == 3


def test_wait_for_result_times_out_with_exit_2_and_names_await(steward_home, tmp_path):
    store = Store(steward_home)
    job = _job(store, _repo(tmp_path))
    row = cr.submit_worker_run(store, SubprocessRunner(), _repo(tmp_path / "w"), job.job_id, "make t", now=1.0,
                               toolchain_fn=lambda c: "tc").row
    t = iter(range(0, 10_000, 100))
    code, text = cr.wait_for_result(store, row.run_id, timeout=250.0, poll=1.0, sleep=lambda s: None,
                                    clock=lambda: float(next(t)))
    assert code == 2 and "still queued" in text and f"steward check await {row.run_id}" in text


def test_cli_check_wait_prints_the_result_and_records_the_registered_exception(capsys, monkeypatch, steward_home,
                                                                              tmp_path):
    store = Store(steward_home)
    job = _job(store, _repo(tmp_path))
    row = _done_run(store, job.job_id, tmp_path / "w", exit_code=1, tests={"t::a": "passed", "t::b": "failed"})
    code, out, err = _cli(capsys, monkeypatch, steward_home, tmp_path, "check", "wait", row.run_id)
    assert code == 1 and "exit 1" in out and "t::b" in out
    used = [e for e in Store(steward_home).events(job.job_id) if e.what == "exception_used"]
    assert used and used[0].evidence["name"] == "worker-check-wait" and used[0].evidence["run_id"] == row.run_id


def test_cli_check_wait_green_exits_zero_and_unknown_run_exits_one(capsys, monkeypatch, steward_home, tmp_path):
    store = Store(steward_home)
    job = _job(store, _repo(tmp_path))
    row = _done_run(store, job.job_id, tmp_path / "w")
    assert _cli(capsys, monkeypatch, steward_home, tmp_path, "check", "wait", row.run_id)[0] == 0
    code, out, err = _cli(capsys, monkeypatch, steward_home, tmp_path, "check", "wait", "cr-nope")
    assert code == 1 and "unknown run" in err


def test_cli_check_wait_refuses_when_the_exception_is_not_registered(capsys, monkeypatch, steward_home, tmp_path):
    store = Store(steward_home)
    job = _job(store, _repo(tmp_path))
    row = _done_run(store, job.job_id, tmp_path / "w")
    (tmp_path / "exc.toml").write_text('[git]\ndirection = "out"\npurpose = "p"\naudit = "event"\n')
    code, out, err = _cli(capsys, monkeypatch, steward_home, tmp_path, "check", "wait", row.run_id)
    assert code == 3 and "exceptions sync" in err and "exit" not in out


@pytest.mark.parametrize("verb,extra", [("run", ["python3", "-m", "pytest", "-q"]), ("await", ["cr-1"])])
def test_cli_check_run_and_await_outside_a_worker_print_the_instruction(capsys, monkeypatch, steward_home, tmp_path,
                                                                        verb, extra):
    code, out, err = _cli(capsys, monkeypatch, steward_home, tmp_path, "check", verb, *extra)
    assert code == 2 and "Steward worker" in err and "permission hook" in err


def test_check_wait_form_passes_the_wait_policy():
    from steward import wait_policy as wp

    cmd = {"command": "steward check wait cr-0123456789ab --timeout 540", "timeout": 600000}
    assert not wp.check("Bash", cmd).deny and wp.is_pure_wait("Bash", cmd)
