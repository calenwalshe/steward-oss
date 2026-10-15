"""Stability/accuracy checks: finish line, idle-after-final, runner code, broken check, spend shape.
Fakes only: no real Jev, Herdr, panes, or live store."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ruling_support import StubLead, finish, with_check
from steward import api as api_module
from steward import baseline, cli, closeout, dashact, evidence, launch, stability, watchers
from steward import custom_watchers as cw
from steward.contract import Contract, store_contract
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.discovery import DiscoveredCommand, Discovery
from steward.heartbeat import Signals
from steward.jev_watch import JevWatch
from steward.models import JobState, Judgment
from steward.runner import Runner, _JevActions
from steward.runner_exec import CommandResult, FakeRunner
from steward.steering import Steering
from steward.store import Store
from steward.verify import verify
from steward.worker import WorkerObservation
from test_jev_watch import FakeActions, _resp, _running, _watch
from test_runner import _contracted_job, _Factory

REPO = Path(__file__).resolve().parent.parent
FINISH_GOAL = "Build the widget. FINISH LINE: `python3 check_finish.py` exits 0 and the docs are updated."


def _res(argv, cwd, code=0, out="", err=""):
    return CommandResult(argv=argv, cwd=cwd, exit_code=code, stdout=out, stderr=err, duration_s=0.0)


class ScriptedRunner(FakeRunner):
    """git answers plainly; a `bash -lc <line>` is answered by `shell(line) -> (code, out)`."""

    def __init__(self, shell=None):
        self.shell = shell or (lambda line: (0, ""))
        self.lines: list[str] = []
        super().__init__(self._respond)

    def _respond(self, argv, cwd):
        if argv[:2] == ["bash", "-lc"]:
            self.lines.append(argv[2])
            code, out = self.shell(argv[2])
            return _res(argv, cwd, code, out)
        if argv[:2] == ["git", "merge-base"]:  # the job's start commit: HEAD (sha-1) is a new commit past it
            return _res(argv, cwd, 0, "base-0\n")
        if argv[:2] == ["git", "rev-parse"]:
            return _res(argv, cwd, 0, "sha-1\n")
        return _res(argv, cwd)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _finish_job(store, goal=FINISH_GOAL, *, script=True):
    job = _contracted_job(store, goal)
    wt = store.home / "worktrees" / job.job_id
    wt.mkdir(parents=True)
    if script:
        (wt / "check_finish.py").write_text("raise SystemExit(0)\n")
    return job


def _done_obs():
    return WorkerObservation(lifecycle="idle", output_tail="all done\nSTEWARD-DONE", pid_alive=True, ts="t")


def _runner(store, runner_exec, scripts, **kw):
    return Runner(store, _Factory(scripts=scripts), runner_exec, FakeDiagnoser(Diagnosis("retry_with_guidance", "g", "h")),
                  clock=kw.pop("clock", Clock()), **kw)


def _events(store, job, what):
    return [e for e in store.events(job.job_id) if e.what == what]


# -- 1. done-claim vs finish line -----------------------------------------------------------------------


def test_finish_check_command_detection(tmp_path):
    (tmp_path / "check_finish.py").write_text("")
    (tmp_path / "verify.sh").write_text("")
    f = stability.finish_check_command
    assert f("x. FINISH LINE: `python3 check_finish.py` exits 0", tmp_path) == "python3 check_finish.py"
    assert f("do it. FINISH LINE: check_finish.py exits 0", tmp_path) == "python3 check_finish.py"
    assert f("FINISH LINE: `verify.sh` passes", tmp_path) == "bash verify.sh"
    assert f("FINISH LINE: `./verify.sh`", tmp_path) == "./verify.sh"
    assert f("FINISH LINE: `./nope.sh`", tmp_path) is None
    assert f("FINISH LINE: `python3 nope.py` exits 0", tmp_path) is None  # script must exist
    # a backticked command with shell metacharacters is never run; only the bare script it names could be
    assert f("FINISH LINE: `rm -rf / && python3 check_finish.py`", tmp_path) == "python3 check_finish.py"
    assert f("FINISH LINE: `rm -rf / && python3 nope.py`", tmp_path) is None
    assert f("FINISH LINE: `python3 check_finish.py; rm x`", tmp_path) == "python3 check_finish.py"
    assert f("just do the thing, no finish line", tmp_path) is None
    assert f("FINISH LINE: docs are updated and it reads well", tmp_path) is None


def test_finish_line_fail_nudges_once_and_keeps_running(steward_home):
    store = Store(steward_home)
    job = _finish_job(store)
    sr = ScriptedRunner(lambda line: (1, "checking...\nFAIL: docs/widget.md missing\nfinish check failed") if "check_finish" in line else (0, ""))
    runner = _runner(store, sr, [[_done_obs()] * 4])
    runner.start(job.job_id)

    report = runner.tick(job.job_id)

    assert report.state == "RUNNING" and report.action == "finish_bounce"
    assert store.get_job(job.job_id).state == JobState.RUNNING  # NOT NEEDS_HUMAN
    ev = _events(store, job, "finish_line_not_met")
    assert len(ev) == 1 and ev[0].evidence["command"] == "python3 check_finish.py"
    assert ev[0].evidence["first_failing_line"] == "FAIL: docs/widget.md missing"
    assert "FAIL: docs/widget.md missing" in ev[0].evidence["output_tail"]
    nudges = [i for i in Steering(store).all_instructions(job.job_id) if i["origin"] == "steward"]
    assert len(nudges) == 1 and nudges[0]["verb"] == "focus"
    assert nudges[0]["text"] == "Finish line not met: FAIL: docs/widget.md missing. Keep going until it passes."
    assert stability.finish_bounces(store, job.job_id) == 1


def test_three_bounces_then_needs_human_with_the_reason(steward_home):
    store = Store(steward_home)
    job = _finish_job(store)
    sr = ScriptedRunner(lambda line: (1, "FAIL: still broken") if "check_finish" in line else (0, ""))
    runner = _runner(store, sr, [[_done_obs()] * 6])
    runner.start(job.job_id)

    states = [runner.tick(job.job_id).state for _ in range(3)]
    assert states == ["RUNNING"] * 3
    last = runner.tick(job.job_id)

    assert last.state == "NEEDS_HUMAN"
    assert len([i for i in Steering(store).all_instructions(job.job_id) if i["origin"] == "steward"]) == 3  # bounded
    ev = next(e for e in store.events(job.job_id) if e.rule == "finish_line_bounces_exhausted")
    assert "FAIL: still broken" in ev.what and "after 3 nudges" in ev.what
    assert len(_events(store, job, "finish_line_not_met")) == 4


def test_finish_line_pass_goes_on_to_normal_closeout(steward_home):
    store = Store(steward_home)
    job = _finish_job(store)
    with_check(store, job)
    runner = _runner(store, ScriptedRunner(), [[_done_obs()]])
    runner.job_lead = StubLead(store)
    runner.start(job.job_id)

    report = finish(runner, job.job_id)

    assert report.state == "ACCEPTED"
    assert len(_events(store, job, "finish_line_met")) == 1 and not _events(store, job, "finish_line_not_met")


def test_unrunnable_finish_check_is_not_an_unmet_finish_line(steward_home):
    store = Store(steward_home)
    job = _finish_job(store)
    with_check(store, job)
    sr = ScriptedRunner(lambda line: (127, "bash: python3: command not found") if "check_finish" in line else (0, ""))
    runner = _runner(store, sr, [[_done_obs()]])
    runner.job_lead = StubLead(store)
    runner.start(job.job_id)

    report = finish(runner, job.job_id)

    assert report.state == "ACCEPTED" and not _events(store, job, "finish_line_not_met")
    assert _events(store, job, "finish_check_unrunnable")
    assert store.kv_get(job.job_id, stability.FINISH_UNPROVEN_KEY) is True


def test_finish_check_is_bounded_to_120s(steward_home):
    store = Store(steward_home)
    job = _finish_job(store)
    sr = ScriptedRunner(lambda line: (0, ""))
    runner = _runner(store, sr, [[_done_obs()]])
    runner.start(job.job_id)
    runner.tick(job.job_id)
    call = next(c for c in sr.calls if c["argv"][:2] == ["bash", "-lc"] and "check_finish" in c["argv"][2])
    assert call["timeout"] == 120 and call["cwd"].endswith(job.job_id)


def test_prose_finish_line_sets_unproven_flag(steward_home):
    store = Store(steward_home)
    job = _finish_job(store, "Do it. FINISH LINE: the docs read well and all links work", script=False)
    runner = _runner(store, ScriptedRunner(), [[_done_obs()]])
    runner.start(job.job_id)
    runner.tick(job.job_id)  # no runnable check: closes out normally, remembers the finish line was unproven
    assert store.kv_get(job.job_id, stability.FINISH_UNPROVEN_KEY) is True


def test_finished_and_done_unproven_routes_and_actions_are_gone():
    """v2.7 Phase 40 (SRD-03): a model opinion never closes out or spends a finish bounce."""
    from steward import jev_observer, jev_watch

    assert "finished" not in jev_watch.ROUTES and "done_unproven" not in jev_watch.ROUTES
    assert "finished" not in jev_observer.CHOICES and "done_unproven" not in jev_observer.CHOICES
    assert not hasattr(_JevActions, "finished") and not hasattr(_JevActions, "done_unproven")
    assert not hasattr(Runner, "_idle_final_closeout")
    assert not hasattr(stability, "IDLE_FINAL_PENDING_KEY") and not hasattr(stability, "final_message")


# -- 2. idle after a final message ----------------------------------------------------------------------


def _iso(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _idle_setup(store, turns_fn, *, goal="just do it"):
    job = _contracted_job(store, goal)
    clock = Clock(1000.0)
    idle_obs = WorkerObservation(lifecycle="idle", output_tail="waiting for input", pid_alive=True, ts="t")
    diag = FakeDiagnoser(Diagnosis("retry_with_guidance", "poke", "stalled"))
    runner = Runner(store, _Factory(scripts=[[idle_obs] * 5]), ScriptedRunner(), diag, clock=clock,
                    activity_fn=lambda st, jid: {"turns": turns_fn(clock.t)})
    runner.start(job.job_id)
    runner.tick(job.job_id)  # baseline signals at t=1000
    clock.t += 700  # > stall_s with nothing changing: suspect_stall
    return job, runner, clock, diag


def test_idle_after_a_final_message_is_not_a_completion(steward_home):
    """Yield is not completion: a worker idle after a final `said` message is never closed out by inference."""
    store = Store(steward_home)
    job, runner, clock, diag = _idle_setup(
        store, lambda now: [{"i": 0, "ts": _iso(now - 650), "kind": "tool", "text": "Bash: pytest"},
                            {"i": 1, "ts": _iso(now - 640), "kind": "said", "text": "All done. Tests pass."}])

    report = runner.tick(job.job_id)

    assert report.action != "closeout" and report.state == "RUNNING"
    assert not _events(store, job, "idle_after_final_message")
    assert not [e for e in store.events(job.job_id) if e.rule == "idle_after_final_message"]


def test_idle_after_final_with_pending_tool_or_permission_still_goes_to_doctor(steward_home):
    store = Store(steward_home)
    job, runner, clock, diag = _idle_setup(
        store, lambda now: [{"i": 0, "ts": _iso(now - 640), "kind": "said", "text": "running it"},
                            {"i": 1, "ts": _iso(now - 630), "kind": "tool", "text": "Bash: sleep 900"}])
    report = runner.tick(job.job_id)
    assert report.action != "closeout" and len(diag.contexts) == 1
    assert not _events(store, job, "idle_after_final_message")


# -- 3. runner on old code ------------------------------------------------------------------------------


def test_runner_records_its_code_and_start_time(steward_home, monkeypatch):
    monkeypatch.setattr(stability, "PROCESS_HEAD", "abc1234def")
    monkeypatch.setattr(stability, "PROCESS_STARTED_AT", 4242.0)
    store = Store(steward_home)
    job = _contracted_job(store)
    runner = _runner(store, ScriptedRunner(), [[WorkerObservation("working", "x", True, "t")]])
    runner.start(job.job_id)
    runner.tick(job.job_id)
    rec = store.kv_get(job.job_id, "runner_code")
    assert rec == {"started_at": 4242.0, "head": "abc1234def", "pid": os.getpid()}


def test_outdated_detection_counts_only_steward_commits_after_the_loaded_sha(steward_home):
    _needs_git_history(REPO)
    store = Store(steward_home)
    job = _contracted_job(store)
    assert stability.runner_outdated(store, job.job_id) == 0  # never recorded: say nothing
    stability.record_runner_code(store, job.job_id, head="loaded", started_at=1.0, pid=1)
    assert stability.runner_outdated(store, job.job_id, count_fn=lambda sha: 3 if sha == "loaded" else 0) == 3
    assert stability.runner_outdated(store, job.job_id, count_fn=lambda sha: 0) == 0
    # the real git path: the repo's very first commit is far behind HEAD on steward/
    first = subprocess.run(["git", "rev-list", "--max-parents=0", "HEAD"], cwd=REPO, capture_output=True,
                           text=True).stdout.split()[0]
    assert stability.commits_behind(first) > 0
    assert stability.commits_behind("") == 0 and stability.commits_behind("0" * 40) == 0  # unknown sha: no false alarm


def test_restart_runner_types_into_the_runner_pane_only_and_never_touches_the_worker(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store, repo=str(steward_home))
    store.kv_set(job.job_id, "runner_pane", "w1:p7")
    panes = {"result": {"panes": [{"pane_id": "w1:p7", "label": f"runner {job.job_id[:8]}"},
                                   {"pane_id": "w1:p8", "label": "worker"}]}}

    def respond(argv, cwd):
        return _res(argv, cwd, 0, json.dumps(panes) if argv[:3] == ["herdr", "pane", "list"] else "")

    fake = FakeRunner(respond)
    naps = []
    out = launch.restart_runner(fake, store, job, launch_args={"worker": "fabric", "kind": "claude", "sock": "/s",
                                                                "interval": 15.0}, sleep=naps.append)
    assert out["accepted"] and out["pane"] == "w1:p7"
    argvs = [c["argv"] for c in fake.calls]
    assert ["herdr", "pane", "send-keys", "w1:p7", "ctrl+c"] in argvs
    run = next(a for a in argvs if a[:3] == ["herdr", "pane", "run"])
    assert run[3] == "w1:p7" and f"steward.cli run {job.job_id}" in run[4] and "--worker fabric" in run[4]
    assert not any("w1:p8" in a for a in argvs)  # the worker pane is never addressed
    assert naps  # it waited for the old process to release the pane

    # a live recorded runner pid is signalled directly (the mailbox tty swallows a typed Ctrl-C)
    store.kv_set(job.job_id, "runner_code", {"head": "x", "pid": 4242})
    alive = {4242: f"python3 -m steward.cli run {job.job_id} --worker fabric"}
    sent = []

    def fake_kill(pid, sig):
        sent.append((pid, sig))
        alive.pop(pid, None)

    fake2 = FakeRunner(respond)
    out = launch.restart_runner(fake2, store, job, launch_args={"worker": "fabric"}, sleep=lambda s: None,
                                kill=fake_kill, cmdline=alive.get)
    assert out["accepted"] and [p for p, _ in sent] == [4242]
    assert not any(a[:3] == ["herdr", "pane", "send-keys"] for a in (c["argv"] for c in fake2.calls))

    gone = FakeRunner(lambda argv, cwd: _res(argv, cwd, 0, json.dumps({"result": {"panes": []}})))
    assert not launch.restart_runner(gone, store, job, sleep=lambda s: None)["accepted"]


def test_runner_restart_is_relayed_human_and_only_for_running_jobs(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    job = _contracted_job(store)
    calls = []
    monkeypatch.setattr(launch, "restart_runner", lambda *a, **k: calls.append((a, k)) or
                        {"accepted": True, "detail": "runner restarted", "pane": "w1:p7"})
    assert cli.main(["runner-restart", job.job_id]) == 1  # CONTRACTED: refused, nothing typed
    assert calls == [] and "only a RUNNING job" in capsys.readouterr().err
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    assert cli.main(["runner-restart", job.job_id, "--origin", "dash"]) == 0
    assert len(calls) == 1
    ev = _events(store, job, "runner_restarted")
    assert len(ev) == 1 and ev[0].judgment == Judgment.human and ev[0].evidence["origin"] == "dash"
    assert dashact.argv_for("restart_runner", "J1")[-4:] == ["runner-restart", "J1", "--origin", "dash"]
    assert cli._op_and_args_for(cli._build_parser().parse_args(["runner-restart", "J1"])) == ("runner_restart", {"job_id": "J1"})


def _render(store, tmp_path, cols=88):
    map_path = tmp_path / "map"
    env = {**os.environ, "STEWARD_HOME": str(store.home), "STEWARD_DASH_COLS": str(cols), "STEWARD_DASH_MAP": str(map_path),
           "PYTHONPATH": str(REPO), "HOME": str(tmp_path), "STEWARD_HEALTH": "0"}
    res = subprocess.run([sys.executable, str(REPO / "deploy/cockpit/steward-dash-jobs.py")], capture_output=True,
                         text=True, env=env, cwd=REPO)
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", ln).replace("{SPIN}", "x") for ln in res.stdout.splitlines()]
    return lines, (map_path.read_text().splitlines() if map_path.exists() else [])


def test_pane_shows_outdated_runner_with_a_working_button(steward_home, tmp_path):
    _needs_git_history(REPO)
    store = Store(steward_home)
    job = _contracted_job(store)
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    first = subprocess.run(["git", "rev-list", "--max-parents=0", "HEAD"], cwd=REPO, capture_output=True,
                           text=True).stdout.split()[0]
    stability.record_runner_code(store, job.job_id, head=first, started_at=1.0, pid=1)

    lines, cmap = _render(store, tmp_path)

    row = next(i for i, ln in enumerate(lines) if "runner outdated" in ln)
    assert re.search(r"⚠ runner outdated \(\d+ commits behind\)", lines[row]) and "[ restart runner ]" in lines[row]
    assert all(len(ln) <= 88 for ln in lines)
    btn = next(m for m in cmap if m.startswith(f"btn {row} ") and "restart_runner" in m).split()
    c0, c1 = int(btn[2]), int(btn[3])
    assert lines[row][c0 - 1:c1] == "[ restart runner ]" and btn[5] == job.job_id

    lines, cmap = _render(store, tmp_path, cols=44)  # a phone: the button drops to its own row, still clickable
    row = next(i for i, ln in enumerate(lines) if "runner outdated" in ln)
    assert "[ restart runner ]" in lines[row + 1] and len(lines[row]) <= 44 and len(lines[row + 1]) <= 44
    assert any(m.startswith(f"btn {row + 1} ") for m in cmap)

    stability.record_runner_code(store, job.job_id, head=stability.repo_head(), started_at=1.0, pid=1)
    assert not any("runner outdated" in ln for ln in _render(store, tmp_path)[0])  # current: no warning


# -- 4. broken test check -------------------------------------------------------------------------------


def _check_runner(exit_by_line):
    def respond(argv, cwd):
        if argv[:2] == ["bash", "-lc"]:
            line = argv[2].split(" --junitxml=")[0]
            return _res(argv, cwd, exit_by_line.get(line, 0), "", "usage: pytest ...")
        return _res(argv, cwd, 0, "sha-1\n")
    return FakeRunner(respond)


def test_broken_reason_classification():
    b = baseline.broken_reason
    assert b("pytest", 4) and b("pytest -q", 2) and b("pytest", 5) and b("make test", 127) and b("npm test", 126)
    assert b("pytest", 1) is None and b("pytest", 0) is None  # failing tests are not a broken check
    assert b("make test", 2) is None and b("make test", 4) is None  # exits 2/4/5 mean "broken" only for pytest


def test_bare_pytest_usage_error_falls_back_to_python_m_pytest(steward_home, tmp_path):
    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    fake = _check_runner({"pytest -q": 4})
    run = baseline.run_check(fake, "pytest -q", tmp_path, tmp_path / "x.xml")
    assert [c["argv"][2].split(" --junitxml=")[0] for c in fake.calls] == ["pytest -q", "python3 -m pytest -q"]
    assert run.broken is None and run.exit_code == 0 and run.used == "python3 -m pytest -q"
    baseline.record_check_events(store, job.job_id, "verify", "pytest -q", run)
    fb = _events(store, job, "check_fallback")
    assert fb and fb[0].evidence["used"] == "python3 -m pytest -q" and not _events(store, job, "check_broken")


def test_check_still_broken_after_fallback_is_reported(steward_home, tmp_path):
    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    fake = _check_runner({"pytest": 4, "python3 -m pytest": 4})
    run = baseline.run_check(fake, "pytest", tmp_path, tmp_path / "x.xml")
    assert run.broken and "usage error" in run.broken and run.exit_code == 4
    baseline.record_check_events(store, job.job_id, "baseline", "pytest", run)
    ev = _events(store, job, "check_broken")
    assert ev[0].evidence["command"] == "pytest" and ev[0].evidence["exit"] == 4
    nf = baseline.run_check(_check_runner({"make test": 127}), "make test", tmp_path, tmp_path / "y.xml")
    assert nf.broken == "command not found (exit 127)"
    no_retry = _check_runner({"python3 -m pytest": 5})
    assert baseline.run_check(no_retry, "python3 -m pytest", tmp_path, tmp_path / "z.xml").broken  # only BARE pytest retries
    assert len(no_retry.calls) == 1


def _verify_broken(store, tmp_path, exit_by_line):
    job = _contracted_job(store)
    disc = Discovery(commands=[DiscoveredCommand(line="pytest", source="t", kind="check", reason="t")])
    fake = FakeRunner(lambda argv, cwd: _res(argv, cwd, exit_by_line.get(argv[2].split(" --junitxml=")[0], 0)
                                              if argv[:2] == ["bash", "-lc"] else 0,
                                              "" if argv[:2] == ["bash", "-lc"] else ("sha-1\n" if argv[1] == "rev-parse" else "")))
    return job, verify(store, job.job_id, tmp_path, disc, fake)


def test_broken_check_makes_verification_unverified_never_yes(steward_home, tmp_path, capsys):
    store = Store(steward_home)
    job, v = _verify_broken(store, tmp_path, {"pytest": 4, "python3 -m pytest": 4})
    assert v.unverified and "usage error" in v.unverified_reason and not v.new_failures

    ver = api_module.StewardAPI(store).get_verification(job.job_id)
    assert ver["verified"] is False and ver["unverified"][0]["command"] == "pytest" and "usage error" in ver["unverified_reason"]

    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    store.set_state(job.job_id, JobState.WORKER_FINISHED, what="t", evidence={}, rule="t", judgment=Judgment.code)
    assert closeout.closeout(store, job.job_id, v, [], None) == JobState.NEEDS_HUMAN  # never auto-accepted
    last = store.events(job.job_id)[-1]
    assert last.rule == "acceptance_unresolved" and "tests unverified" in last.evidence["reasons"][0]

    assert cli.main(["verification", job.job_id]) == 0
    out = capsys.readouterr().out
    assert "verified: no" in out and "⚠ tests unverified: `pytest`: pytest usage error (exit 4)" in out
    from steward import jobview

    assert "tests unverified" in jobview.reason([], "NEEDS_HUMAN", [
        {"what": e.what, "evidence": e.evidence} for e in store.events(job.job_id)])
    panel = evidence.panel(store, job.job_id, 100, cost_usd=0.0)
    assert any(ln.startswith("⚠ tests unverified: `pytest`") for ln in panel) and any("verified no" in ln for ln in panel)


def test_working_fallback_verifies_normally(steward_home, tmp_path):
    store = Store(steward_home)
    job, v = _verify_broken(store, tmp_path, {"pytest": 4})  # python3 -m pytest passes (exit 0)
    assert not v.unverified and api_module.StewardAPI(store).get_verification(job.job_id)["verified"] is True
    assert _events(store, job, "check_fallback")


# -- 5. spend shape -------------------------------------------------------------------------------------


def _tokens(worker=0.30, watcher=0.90, hour=1.0, name="drift"):
    return {"sources": {"worker": {"usd": worker}, f"watcher:{name}": {"usd": watcher}, "astra": {"usd": 0.1}},
            "last_hour": {"usd": hour}, "total": {"usd": worker + watcher}}


def _add_watcher(store, job, name="drift", every="10m"):
    cw.add_watcher(store, job.job_id, cw.make_spec(name, "keep on track", every=every))


def test_spend_findings_threshold_and_minimum():
    f = stability.spend_findings(_tokens(worker=0.30, watcher=0.90))
    assert [o[0] for o in f["overspenders"]] == ["drift"] and round(f["overspenders"][0][3], 1) == 3.0
    assert stability.spend_findings(_tokens(worker=0.30, watcher=0.60))["overspenders"] == []  # exactly 2x: not over
    assert stability.spend_findings(_tokens(worker=0.05, watcher=0.20))["overspenders"] == []  # under the $0.25 floor
    assert stability.spend_findings(_tokens(worker=0.0, watcher=0.30))["overspenders"]  # worker free: any real spend counts
    assert stability.spend_warnings(_tokens(worker=1.0, watcher=2.3)) == ["⚠ drift costs 2.3× worker"]
    assert stability.spend_warnings(_tokens(hour=6.2)) == ["⚠ drift costs 3.0× worker", "⚠ spend $6.20/h"]
    assert stability.spend_warnings({}) == []


def test_overspend_halves_cadence_records_and_caps_at_two(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    _add_watcher(store, job)
    every = lambda: cw.list_watchers(store, job.job_id)[0]["every_s"]
    assert every() == 600

    assert stability.apply_spend_shape(store, job.job_id, _tokens()) == ["drift: halved"]
    assert every() == 1200
    assert stability.apply_spend_shape(store, job.job_id, _tokens()) == []  # nothing new since: no second halving
    store.kv_set(job.job_id, "watcher_runs:drift", 1)
    assert stability.apply_spend_shape(store, job.job_id, _tokens()) == ["drift: halved"] and every() == 2400
    store.kv_set(job.job_id, "watcher_runs:drift", 2)
    assert stability.apply_spend_shape(store, job.job_id, _tokens()) == ["drift: capped"] and every() == 2400  # max 2

    ev = _events(store, job, "watcher_overspend")
    assert [e.evidence["cadence_halved"] for e in ev] == [True, True, False]
    assert ev[0].evidence["ratio"] == 3.0 and ev[0].evidence["every_s"] == 1200
    assert len(_events(store, job, "watcher_cadence_halved")) == 2

    assert cw.restore_cadence(store, job.job_id, "drift") == 600 and every() == 600  # reversible, recorded
    assert _events(store, job, "watcher_cadence_restored") and cw.halvings(store, job.job_id, "drift") == 0


@pytest.mark.legacy_ownership_off
def test_spend_rate_over_five_dollars_an_hour_asks_once(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    asked = []
    notify = lambda jid, kind, title, body: asked.append((kind, body))
    apply = lambda usd: stability.apply_spend_shape(store, job.job_id, {"sources": {}, "last_hour": {"usd": usd}}, notify)
    assert apply(4.99) == [] and asked == []
    assert apply(6.2) == ["rate"] and asked[0][0] == "NEEDS_YOU" and "$6.20/h" in asked[0][1]
    assert apply(9.0) == [] and len(asked) == 1  # once
    assert len(_events(store, job, "spend_rate_high")) == 1


def test_runner_spend_step_is_throttled_and_routes(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    _add_watcher(store, job)
    calls = []
    clock = Clock(1000.0)
    runner = _runner(store, ScriptedRunner(), [[WorkerObservation("working", "x", True, "t")] * 5], clock=Clock(),
                     spend_clock=clock,
                     spend_fn=lambda st, jid, now=None: calls.append(jid) or _tokens(hour=6.0))
    runner.start(job.job_id)
    runner.tick(job.job_id)
    runner.tick(job.job_id)  # same minute: not re-evaluated
    assert len(calls) == 1
    assert cw.list_watchers(store, job.job_id)[0]["every_s"] == 1200 and _events(store, job, "spend_rate_high")
    clock.t += 61
    runner.tick(job.job_id)
    assert len(calls) == 2


def test_spend_check_failure_is_recorded_never_fatal(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)

    def boom(*a, **k):
        raise RuntimeError("no transcripts")

    runner = _runner(store, ScriptedRunner(), [[WorkerObservation("working", "x", True, "t")]], spend_fn=boom)
    runner.start(job.job_id)
    assert runner.tick(job.job_id).state == "RUNNING"
    assert _events(store, job, "spend_check_error")


def test_pane_renders_spend_warnings_from_the_token_data(steward_home, tmp_path):
    store = Store(steward_home)
    job = _contracted_job(store)
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    home = tmp_path / "p"
    wt = store.home / "worktrees" / job.job_id
    from steward.chat import project_slug

    proj = home / ".claude" / "projects" / project_slug(wt)
    proj.mkdir(parents=True)

    def msg(mid, model, ts, out):
        return json.dumps({"type": "assistant", "timestamp": _iso(ts), "message": {
            "id": mid, "model": model, "usage": {"input_tokens": 0, "output_tokens": out}}})

    import time as _t

    now = _t.time()
    (proj / "w.jsonl").write_text(msg("w1", "claude-sonnet-5-5", now - 60, 10_000) + "\n")  # $0.10
    marker = "You are a Steward custom watcher named drift, attached"
    user = json.dumps({"type": "user", "timestamp": _iso(now - 50), "message": {"role": "user", "content": marker}})
    (proj / "wd.jsonl").write_text(user + "\n" + msg("d1", "claude-opus-5-5", now - 40, 30_000) + "\n")  # $0.60
    store.kv_set(job.job_id, "worker_session_id", "w")
    env = {**os.environ, "STEWARD_HOME": str(store.home), "STEWARD_DASH_COLS": "88", "PYTHONPATH": str(REPO),
           "HOME": str(home), "STEWARD_HEALTH": "0"}
    res = subprocess.run([sys.executable, str(REPO / "deploy/cockpit/steward-dash-jobs.py")], capture_output=True,
                         text=True, env=env, cwd=REPO)
    text = re.sub(r"\x1b\[[0-9;]*m", "", res.stdout)
    assert "⚠ drift costs 6.0× worker" in text, text


# -- 7. feed lines + explain-state ----------------------------------------------------------------------


def test_feed_lines_mention_every_new_signal(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    for what, ev in (
        ("finish_line_not_met", {"bounce": 2, "max": 3, "first_failing_line": "FAIL: x"}),
        ("finish_line_met", {"command": "python3 check_finish.py"}),
        ("yield_classified", {"lane": "STOPPED", "failed_preconditions": ["no_new_commit"]}),
        ("check_broken", {"phase": "verify", "command": "pytest", "reason": "pytest usage error (exit 4)"}),
        ("check_fallback", {"command": "pytest", "used": "python3 -m pytest"}),
        ("watcher_overspend", {"name": "drift", "ratio": 2.3, "cadence_halved": True, "every_s": 1200}),
        ("spend_rate_high", {"usd_per_hour": 6.2, "limit": 5.0}),
        ("runner_restarted", {"pane": "w1:p7"}),
    ):
        store.append_event(job.job_id, what=what, evidence=ev, rule="r", judgment=Judgment.code)
    text = "\n".join(t for _, _, t in watchers.collect(store.events(job.job_id)))
    for needle in ("FINISH LINE NOT MET (2/3): FAIL: x", "finish line met", "worker turn ended: STOPPED no_new_commit",
                   "TESTS UNVERIFIED", "re-run as", "drift costs 2.3x worker", "cadence halved", "SPEND $6.2/h",
                   "runner restarted in place"):
        assert needle in text, needle


def test_explain_state_lists_the_new_signals(steward_home, capsys):
    store = Store(steward_home)
    job = _contracted_job(store)
    stability.record_runner_code(store, job.job_id, head="deadbeefcafe", started_at=1.0, pid=1)
    store.kv_set(job.job_id, stability.FINISH_BOUNCES_KEY, 2)
    for what, ev in (
        ("finish_line_not_met", {"first_failing_line": "FAIL: x"}),
        ("yield_classified", {"lane": "STOPPED", "failed_preconditions": ["no_new_commit"]}),
        ("check_broken", {"reason": "pytest usage error (exit 4)", "command": "pytest", "exit": 4}),
        ("watcher_cadence_halved", {"name": "drift", "halvings": 1, "max": 2, "every_s": 1200}),
        ("spend_rate_high", {}),
    ):
        store.append_event(job.job_id, what=what, evidence=ev, rule="r", judgment=Judgment.code)
    assert cli.main(["explain-state", job.job_id]) == 0
    out = capsys.readouterr().out
    for needle in ("Finish line: not met 2/3 bounces; last: FAIL: x", "Last turn end: STOPPED (no_new_commit)", "Runner code: deadbee",
                   "Tests: unverified, check broken: pytest usage error", "Spend: watcher drift cadence halved 1/2",
                   "hourly rate limit"):
        assert needle in out, needle


def _needs_git_history(repo) -> None:
    """These checks measure real commits behind HEAD; a one-commit snapshot (the public export) has none."""
    import subprocess as _sp
    n = _sp.run(["git", "rev-list", "--count", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    if not n.isdigit() or int(n) < 3:
        pytest.skip("needs the repository's git history (a snapshot export has one commit)")
