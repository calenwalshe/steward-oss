"""Quick task 20261001: the `staged` group in the status pane, its `[ ▶ play ]` button, and `steward play`.
Fakes only: no model, herdr or network."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from test_dash_polish import _running  # noqa: F401
from test_status_pane_features import _pane, _set_state

from steward import cli, dashact, dashrender
from steward.models import JobState
from steward.store import Store

ANSI = re.compile(r"\033\[[0-9;]*m")


def _staged(store, state, age=timedelta(minutes=3), goal="Staged goal"):
    job = store.create_job(goal, "/tmp/somerepo")
    _set_state(store, job, state)
    stamp = (datetime.now(timezone.utc) - age).isoformat()
    with store._conn:
        store._conn.execute("UPDATE jobs SET created_at = ? WHERE job_id = ?", (stamp, job.job_id))
    return job


def _plain(out):
    return [ANSI.sub("", ln) for ln in out]


def _play_buttons(cmap):
    return [c for c in cmap if c.startswith("btn") and " play " in c]


def test_staged_group_sits_above_running_jobs(steward_home, tmp_path):
    store = Store(steward_home)
    run = _running(store, goal="Running job goal")
    ready = _staged(store, JobState.CONTRACTED)
    out, _ = _pane(steward_home, tmp_path, 100)
    plain = _plain(out)
    head = next(i for i, ln in enumerate(plain) if ln.startswith("staged (1)"))
    row = next(i for i, ln in enumerate(plain) if "staged · ready" in ln)
    running = next(i for i, ln in enumerate(plain) if "RUNNING" in ln)
    assert head < row < running
    assert ready.job_id not in "".join(plain[running:running + 1])


def test_play_button_only_when_contracted(steward_home, tmp_path):
    store = Store(steward_home)
    baseline = _staged(store, JobState.CREATED, age=timedelta(minutes=7))
    ready = _staged(store, JobState.CONTRACTED)
    out, cmap = _pane(steward_home, tmp_path, 100)
    plain = _plain(out)
    assert any("staged · baseline running 7m" in ln and "play" not in ln for ln in plain)
    assert any("staged · ready" in ln and "[ ▶ play ]" in ln for ln in plain)
    buttons = _play_buttons(cmap)
    assert len(buttons) == 1 and buttons[0].endswith(f"play {ready.job_id} -")
    assert baseline.job_id not in "".join(buttons)
    # the click span covers exactly the button text
    _, row, c0, c1 = buttons[0].split()[:4]
    assert plain[int(row)][int(c0) - 1:int(c1)] == "[ ▶ play ]"


def test_stale_tag_after_24h_only(steward_home, tmp_path):
    store = Store(steward_home)
    _staged(store, JobState.CONTRACTED, age=timedelta(hours=30), goal="Old one")
    out, _ = _pane(steward_home, tmp_path, 100)
    assert sum("stale" in ln for ln in _plain(out)) == 1
    fresh = Store(steward_home)
    with fresh._conn:
        fresh._conn.execute("UPDATE jobs SET created_at = ?", ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),))
    out, _ = _pane(steward_home, tmp_path, 100)
    assert not any("stale" in ln for ln in _plain(out))


def test_double_tap_is_debounced(tmp_path):
    path = tmp_path / "m.expanded"
    first, argv = dashact.press(path, "play", "J1", now=1000.0)
    assert first == "run" and argv[-4:] == ["play", "J1", "--origin", "dash"]
    assert dashact.press(path, "play", "J1", now=1005.0) == ("busy", [])
    assert dashact.press(path, "play", "J2", now=1005.0)[0] == "run"  # other jobs are independent
    assert dashact.press(path, "play", "J1", now=1000.0 + dashact.PLAY_DEBOUNCE_S + 1)[0] == "run"


@pytest.fixture
def relayed(monkeypatch):
    calls = []

    def fake(store, op, args, *, request_id=None, origin="cli", **_):
        calls.append((op, args, origin))
        return {"accepted": True, "tab_id": "w1:t1", "runner_pane": "w1:p1"}

    monkeypatch.setattr(cli, "_relay", fake)
    return calls


def test_play_defaults_to_sonnet_via_relay_with_dash_origin(steward_home, relayed, capsys):
    job = _staged(Store(steward_home), JobState.CONTRACTED)
    assert cli.main(["play", job.job_id, "--workspace", "w1", "--origin", "dash"]) == 0
    (op, args, origin), = relayed
    assert op == "launch" and origin == "dash" and args["job_id"] == job.job_id
    assert args["agent_args"] == ["--model", "sonnet"] and args["worker"] == "fabric"


def test_play_uses_recorded_launch_args(steward_home, relayed):
    store = Store(steward_home)
    job = _staged(store, JobState.CONTRACTED)
    store.kv_set(job.job_id, "launch_args", {"worker": "fabric", "kind": "claude", "interval": 5.0,
                                              "agent_args": ["--model", "opus", "--research"], "keep_pane": True})
    assert cli.main(["play", job.job_id, "--workspace", "w1"]) == 0
    (_, args, origin), = relayed
    assert args["agent_args"] == ["--model", "opus", "--research"] and args["keep_pane"] is True
    assert args["interval"] == 5.0 and origin == "cli"


def test_play_on_launched_or_baseline_job_does_not_launch(steward_home, relayed, capsys):
    store = Store(steward_home)
    running = _staged(store, JobState.RUNNING)
    assert cli.main(["play", running.job_id, "--workspace", "w1"]) == 0
    assert "already RUNNING" in capsys.readouterr().out
    created = _staged(store, JobState.CREATED)
    assert cli.main(["play", created.job_id, "--workspace", "w1"]) == 2
    assert relayed == []


def test_launch_still_launches_without_play_semantics(steward_home, relayed):
    job = _staged(Store(steward_home), JobState.RUNNING)
    assert cli.main(["launch", job.job_id, "--workspace", "w1"]) == 0
    assert relayed[0][0] == "launch"
