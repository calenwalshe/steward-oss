"""`steward watchers`: event formatting, jev dedupe, noise, follow loop, the
feed pane in launch and `watchers --open`."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from steward import cli, launch, watchers
from steward.models import JobState, Judgment
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store


def ev(what, evidence, seq=1, ts="2026-09-28T21:19:36+00:00"):
    return SimpleNamespace(seq=seq, ts=ts, what=what, evidence=evidence)


def fmt(what, evidence, state=None):
    return watchers.format_event(ev(what, evidence), state if state is not None else {})


def test_sol_lines():
    assert fmt("heartbeat class changed", {"from": "waiting", "to": "suspect_stall"}) == ("sol", "suspect stall → diagnosing")
    assert fmt("heartbeat class changed", {"from": "suspect_stall", "to": "waiting"}) == ("sol", "stall cleared → waiting")
    role, text = fmt("stall diagnosis produced", {"verdict": "retry_with_guidance", "hypothesis": "loop is healthy"})
    assert role == "sol" and "retry_with_guidance" in text and "loop is healthy" in text
    assert fmt("retry guidance authorized by diagnosis", {"guidance": "keep going"})[0] == "sol"


def test_astra_lines():
    rid = "5f81442c-xxxx"
    assert fmt("trajectory_review_requested", {"review_id": rid, "triggers": ["a", "b"]}) == ("astra", "review 5f81 requested (a, b)")
    role, text = fmt("trajectory_advice_received", {"review_id": rid, "status": "ok", "verdict": {"trajectory": "sound", "confidence": 0.7, "observation": "tests pass"}})
    assert (role, text) == ("astra", "review 5f81 → sound (0.70): tests pass")
    assert "redirect authorized" in fmt("trajectory_redirect_authorized", {"review_id": rid, "verb": "nudge", "rev": 9})[1]
    assert "discarded (mode_off)" in fmt("trajectory_advice_discarded", {"review_id": rid, "reason": "mode_off"})[1]
    assert "escalated" in fmt("trajectory_escalated", {"review_id": rid, "rule": "r", "detail": "d"})[1]
    assert "timed out" in fmt("trajectory_review_timed_out", {"review_id": rid})[1]
    assert fmt("trajectory_mode_changed", {"mode": "active", "scope": "job"}) == ("astra", "mode → active (job)")


def test_steering_and_toggle_lines():
    role, text = fmt("nudge instruction recorded", {"rev": 7, "origin": "steward", "text": "do x"})
    assert role == "steer" and "[diagnosis]" in text and "rev 7" in text
    assert "[cli]" in fmt("focus instruction recorded", {"rev": 8, "origin": "cli", "text": "t"})[1]
    assert "[chatgpt]" in fmt("goal instruction recorded", {"rev": 9, "origin": "chatgpt", "text": "t"})[1]
    assert fmt("focus instruction recorded", {"rev": 1, "origin": "contract", "text": "t"}) is None
    assert fmt("instruction acknowledged (typed ack frame)", {"rev": 7}) == ("steer", "rev 7 acknowledged")
    assert fmt("supervisor_toggled", {"role": "jev", "state": "on", "previous": "off"}) == ("jev", "switched off → on")


def test_noise_excluded():
    for what, evd in [
        ("worker permission requested", {}), ("approval opened", {}), ("approval approved", {}),
        ("permission_auto_allowed", {}), ("permission decision relayed", {}), ("inner_verdict", {}),
        ("heartbeat class changed", {"from": "waiting", "to": "quiet_known_process"}),
        ("trajectory_hold", {}), ("instruction delivery observed", {}),
    ]:
        assert fmt(what, evd) is None, what


def test_jev_unavailable_deduped_until_state_changes():
    state = {}
    first = watchers.format_event(ev("jev_unavailable", {"code": "disabled"}), state)
    assert first == ("jev", "observer off")
    for _ in range(5):
        assert watchers.format_event(ev("jev_unavailable", {"code": "disabled"}), state) is None
    assert watchers.format_event(ev("jev_unavailable", {"code": "no_api_key"}), state) == ("jev", "observer unavailable (no_api_key)")
    assert watchers.format_event(ev("jev_shadow", {"choice": "inspect_now", "confidence": 0.8}), state) == ("jev", "observes: inspect_now (0.80)")
    assert watchers.format_event(ev("jev_shadow", {"choice": "inspect_now", "confidence": 0.8}), state) is None
    assert watchers.format_event(ev("jev_unavailable", {"code": "disabled"}), state) == ("jev", "observer off")


def test_render_line_truncates_and_colours():
    e = ev("x", {})
    plain = watchers.render_line(e, "sol", "y" * 200, width=40, color=False)
    assert len(plain) == 40 and plain.endswith("…")
    assert " doctor   yyy" in plain
    coloured = watchers.render_line(e, "astra", "hi", width=80, color=True)
    assert "\x1b[35madvisor \x1b[0m" in coloured


def _store_job(steward_home):
    store = Store(steward_home)
    job = store.create_job("watch the thing", "/repo")
    return store, job


def _add(store, job, what, evidence):
    return store.append_event(job.job_id, what=what, evidence=evidence, rule="t", judgment=Judgment.code)


def test_non_follow_prints_history(steward_home):
    store, job = _store_job(steward_home)
    _add(store, job, "trajectory_mode_changed", {"mode": "active"})
    _add(store, job, "inner_verdict", {})
    out = io.StringIO()
    assert watchers.run_feed(store, job.job_id, out=out, width_fn=lambda: 100) == 0
    lines = out.getvalue().splitlines()
    assert len(lines) == 1 and "mode → active" in lines[0]


def test_follow_prints_new_events_and_ends_on_terminal(steward_home):
    store, job = _store_job(steward_home)
    _add(store, job, "trajectory_mode_changed", {"mode": "shadow"})
    out = io.StringIO()
    ticks = []

    def fake_sleep(secs):
        ticks.append(secs)
        if len(ticks) == 1:
            _add(store, job, "heartbeat class changed", {"from": "waiting", "to": "suspect_stall"})
            _add(store, job, "permission_auto_allowed", {})
        elif len(ticks) == 2:
            _add(store, job, "trajectory_review_requested", {"review_id": "abcd1234", "triggers": ["t"]})
            _force_state(store, job, JobState.FAILED)

    assert watchers.run_feed(store, job.job_id, follow=True, out=out, width_fn=lambda: 100, sleep=fake_sleep) == 0
    text = out.getvalue()
    assert ticks == [2.0, 2.0]
    assert text.startswith(f"job {job.job_id}")
    assert "watch the thing" in text.splitlines()[0]
    assert "mode → shadow" in text  # backlog
    assert "suspect stall → diagnosing" in text and "review abcd requested" in text  # appended later
    assert "permission" not in text
    assert text.rstrip().splitlines()[-1].endswith("feed ended") and "failed" in text.rstrip().splitlines()[-1].lower()


def _force_state(store, job, state):
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job.job_id))


def test_follow_exits_cleanly_on_ctrl_c(steward_home):
    store, job = _store_job(steward_home)

    def boom(_):
        raise KeyboardInterrupt

    out = io.StringIO()
    assert watchers.run_feed(store, job.job_id, follow=True, out=out, width_fn=lambda: 80, sleep=boom) == 0


def test_follow_backlog_default_is_last_30(steward_home):
    store, job = _store_job(steward_home)
    for i in range(40):
        _add(store, job, "trajectory_mode_changed", {"mode": f"m{i}"})
    out = io.StringIO()
    watchers.run_feed(store, job.job_id, follow=True, out=out, width_fn=lambda: 100, sleep=lambda _: (_ for _ in ()).throw(KeyboardInterrupt))
    body = [l for l in out.getvalue().splitlines() if "mode →" in l]
    assert len(body) == 30 and "m39" in body[-1] and "m10" in body[0]


# ------------------------------------------------------------- launch feed pane

_TAB_OK = json.dumps({"result": {"tab": {"tab_id": "w3T:t2"}, "root_pane": {"pane_id": "w3T:p2"}}})
_SPLIT_OK = json.dumps({"result": {"pane": {"pane_id": "w3T:p5"}}})


def _res(argv, cwd, code=0, out="", err=""):
    return CommandResult(argv=argv, cwd=str(cwd), exit_code=code, stdout=out, stderr=err, duration_s=0.0)


def test_launch_opens_watchers_pane_right_of_runner(steward_home):
    store = Store(steward_home)
    job = store.create_job("fix it", "/repo")

    def respond(argv, cwd):
        if argv[:3] == ["herdr", "tab", "create"]:
            return _res(argv, cwd, out=_TAB_OK)
        if argv[:3] == ["herdr", "pane", "split"]:
            return _res(argv, cwd, out=_SPLIT_OK)
        return _res(argv, cwd)

    runner = FakeRunner(respond)
    tab, pane = launch.launch_tab(runner, job, workspace="w3T", agent_args=[], interval=15.0)
    assert (tab, pane) == ("w3T:t2", "w3T:p2")
    calls = [c["argv"] for c in runner.calls]
    split = next(c for c in calls if c[:3] == ["herdr", "pane", "split"])
    assert split[split.index("--pane") + 1] == "w3T:p2"
    assert split[split.index("--direction") + 1] == "right"
    assert "--no-focus" in split
    assert ["herdr", "pane", "rename", "w3T:p5", f"watchers {job.job_id[:8]}"] in calls
    run = next(c for c in calls if c[:4] == ["herdr", "pane", "run", "w3T:p5"])
    assert f"steward.cli watchers {job.job_id} --follow" in run[4]
    assert run[4].startswith(launch._env_exports())
    # the runner itself is still started, and comes first
    assert next(c for c in calls if c[:4] == ["herdr", "pane", "run", "w3T:p2"])


def test_launch_survives_watchers_split_failure(steward_home):
    store = Store(steward_home)
    job = store.create_job("fix it", "/repo")

    def respond(argv, cwd):
        if argv[:3] == ["herdr", "tab", "create"]:
            return _res(argv, cwd, out=_TAB_OK)
        if argv[:3] == ["herdr", "pane", "split"]:
            return _res(argv, cwd, code=1, err="no room")
        return _res(argv, cwd)

    assert launch.launch_tab(FakeRunner(respond), job, workspace="w3T", agent_args=[], interval=15.0) == ("w3T:t2", "w3T:p2")

    def explode(argv, cwd):
        if argv[:3] == ["herdr", "pane", "split"]:
            raise OSError("boom")
        return respond(argv, cwd)

    assert launch.launch_tab(FakeRunner(explode), job, workspace="w3T", agent_args=[], interval=15.0) == ("w3T:t2", "w3T:p2")


# ------------------------------------------------------------- watchers --open


def _open_setup(monkeypatch, steward_home, panes):
    store = Store(steward_home)
    job = store.create_job("fix it", "/repo")
    fake = FakeRunner(lambda argv, cwd: _res(
        argv, cwd, out=json.dumps({"result": {"panes": panes}}) if argv[:3] == ["herdr", "pane", "list"]
        else _SPLIT_OK if argv[:3] == ["herdr", "pane", "split"] else ""))
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: fake)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/herdr")
    return store, job, fake


def test_watchers_open_splits_runner_pane(steward_home, monkeypatch, capsys):
    store, job = Store(steward_home), None
    job = store.create_job("fix it", "/repo")
    panes = [{"pane_id": "w4Q:p9", "tab_id": "w4Q:t5", "label": f"runner {job.job_id[:8]}"}]
    _, _, fake = _open_setup(monkeypatch, steward_home, panes)
    assert cli.main(["watchers", job.job_id, "--open"]) == 0
    calls = [c["argv"] for c in fake.calls]
    split = next(c for c in calls if c[:3] == ["herdr", "pane", "split"])
    assert split[split.index("--pane") + 1] == "w4Q:p9" and "right" in split
    assert any(c[:3] == ["herdr", "pane", "rename"] for c in calls)
    assert any(c[:3] == ["herdr", "pane", "run"] for c in calls)


def test_watchers_open_noop_when_pane_exists(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    job = store.create_job("fix it", "/repo")
    id8 = job.job_id[:8]
    panes = [{"pane_id": "w4Q:p9", "tab_id": "t", "label": f"runner {id8}"}, {"pane_id": "w4Q:pB", "tab_id": "t", "label": f"watchers {id8}"}]
    _, _, fake = _open_setup(monkeypatch, steward_home, panes)
    assert cli.main(["watchers", job.job_id, "--open"]) == 0
    assert "already open" in capsys.readouterr().out
    assert not any(c["argv"][:3] == ["herdr", "pane", "split"] for c in fake.calls)


def test_watchers_open_dry_run_touches_nothing(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    job = store.create_job("fix it", "/repo")
    panes = [{"pane_id": "w4Q:p9", "tab_id": "w4Q:t5", "label": f"runner {job.job_id[:8]}"}]
    _, _, fake = _open_setup(monkeypatch, steward_home, panes)
    assert cli.main(["watchers", job.job_id, "--open", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "w4Q:p9" in out and f"watchers {job.job_id} --follow" in out
    assert [c["argv"][:3] for c in fake.calls] == [["herdr", "pane", "list"]]


def test_watchers_open_errors_without_runner_pane(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    job = store.create_job("fix it", "/repo")
    _open_setup(monkeypatch, steward_home, [])
    assert cli.main(["watchers", job.job_id, "--open"]) == 1
