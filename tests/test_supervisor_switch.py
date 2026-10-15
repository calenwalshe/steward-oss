"""Per-job supervisor on/off switches (`steward supervisor JOB ROLE STATE`)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from steward.roles import role_label
from steward import cli, heartbeat
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.jev_observer import FakeTransport, JevObserver, TransportResponse
from steward.models import JobState, Judgment
from steward.recovery import decide
from steward.runner import Runner
from steward.store import Store
from steward.supervisors import next_state, supervisor_rows
from steward.worker import WorkerObservation
from test_runner import _Clock, _contracted_job, _Factory, _git_runner, _fake_diagnoser

_REPO = Path(__file__).resolve().parents[1]


def _running(store):
    job = store.create_job("g", "/repo")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.RUNNING.value, job.job_id))
    return job


def _sup(job, role, state):
    return cli.main(["supervisor", job.job_id, role, state])


def test_parse_and_relay_mapping():
    ns = cli._build_parser().parse_args(["supervisor", "abc", "sol", "toggle"])
    assert cli._op_and_args_for(ns) == ("supervisor", {"job_id": "abc", "role": "sol", "state": "toggle"})
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["supervisor", "abc", "worker", "off"])


def test_sol_and_jev_flags_and_event(steward_home, capsys):
    store = Store(steward_home)
    job = _running(store)
    assert _sup(job, "sol", "off") == 0
    assert store.kv_get(job.job_id, "supervisor_sol") == "off"
    ev = [e for e in store.events(job.job_id) if e.what == "supervisor_toggled"][-1]
    assert ev.evidence == {"role": "sol", "state": "off", "previous": "on"}
    assert ev.judgment == Judgment.human
    assert _sup(job, "jev", "on") == 0  # legacy alias for shadow
    assert store.kv_get(job.job_id, "supervisor_jev") == "shadow"
    assert _sup(job, "jev", "active") == 0 and store.kv_get(job.job_id, "supervisor_jev") == "active"
    assert _sup(job, "jev", "off") == 0 and store.kv_get(job.job_id, "supervisor_jev") == "off"
    assert _sup(job, "sol", "shadow") == 1  # invalid combo rejected
    assert store.kv_get(job.job_id, "supervisor_sol") == "off"
    assert _sup(job, "sol", "toggle") == 0
    assert store.kv_get(job.job_id, "supervisor_sol") == "on"


def test_astra_delegates_to_trajectory_mode_and_cycles(steward_home):
    store = Store(steward_home)
    job = _running(store)
    assert _sup(job, "astra", "off") == 0
    assert store.kv_get(job.job_id, "trajectory_mode") == "off"
    whats = [e.what for e in store.events(job.job_id)]
    assert "trajectory_mode_changed" in whats and "supervisor_toggled" in whats
    seen = []
    for _ in range(3):
        _sup(job, "astra", "toggle")
        seen.append(store.kv_get(job.job_id, "trajectory_mode"))
    assert seen == ["shadow", "active", "off"]
    assert _sup(job, "astra", "on") == 1


def test_next_state():
    assert [next_state("astra", m) for m in ("off", "shadow", "active")] == ["shadow", "active", "off"]
    assert next_state("sol", "on") == "off" and next_state("jev", "off") == "shadow"
    assert [next_state("jev", m) for m in ("off", "shadow", "active", "on")] == ["shadow", "active", "off", "active"]


def test_unknown_job(steward_home):
    assert cli.main(["supervisor", "nope", "sol", "off"]) == 1


def test_decide_without_diagnoser_records_sol_off(steward_home):
    store = Store(steward_home)
    job = _running(store)
    action = decide(store, job.job_id, heartbeat.Class.suspect_stall, None, context={})
    assert action.kind == "needs_human" and action.rule == "sol_off_rules_only"
    esc = [e for e in store.events(job.job_id) if e.what == "recovery escalated to human"][0]
    assert esc.evidence["sol"] == "off"
    assert not [e for e in store.events(job.job_id) if e.what == "stall diagnosis produced"]


def _stall_runner(store, job, monkeypatch, diagnoser, **kw):
    monkeypatch.setattr(heartbeat, "classify", lambda sig, policy: heartbeat.Class.suspect_stall)
    obs = WorkerObservation(lifecycle="running", output_tail="", pid_alive=True, ts="t")
    runner = Runner(store, _Factory(scripts=[[obs, obs]]), _git_runner(), diagnoser, clock=_Clock(), **kw)
    runner.start(job.job_id)
    return runner


def test_runner_skips_diagnoser_when_sol_off(steward_home, monkeypatch):
    store = Store(steward_home)
    job = _contracted_job(store)
    store.kv_set(job.job_id, "supervisor_sol", "off")
    diag = FakeDiagnoser(Diagnosis(verdict="replace_worker", guidance="", hypothesis="x"))
    runner = _stall_runner(store, job, monkeypatch, diag)
    runner.tick(job.job_id)
    assert diag.contexts == []


def test_runner_calls_diagnoser_when_sol_default_on(steward_home, monkeypatch):
    store = Store(steward_home)
    job = _contracted_job(store)
    diag = FakeDiagnoser(Diagnosis(verdict="replace_worker", guidance="", hypothesis="x"))
    runner = _stall_runner(store, job, monkeypatch, diag)
    runner.tick(job.job_id)
    assert len(diag.contexts) == 1


def _jev_runner(store, job, enabled):
    obs = WorkerObservation(lifecycle="blocked", output_tail="Allow this action? [y/n]", pid_alive=True, ts="t")
    transport = FakeTransport(TransportResponse(status_code=500, body=b""))
    runner = Runner(store, _Factory(scripts=[[obs]]), _git_runner(), _fake_diagnoser(), clock=_Clock(),
                    jev_observer=JevObserver(transport=transport, enabled=enabled))
    runner.start(job.job_id)
    return runner, transport


def _jev_codes(store, job):
    return [e.evidence["code"] for e in store.events(job.job_id) if e.what == "jev_unavailable"]


def test_jev_mode_switch_and_defaults(steward_home, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    # explicit on/shadow: observer runs and honestly reports no key
    store = Store(steward_home)
    job = _contracted_job(store)
    store.kv_set(job.job_id, "supervisor_jev", "on")
    runner, _ = _jev_runner(store, job, enabled=False)
    runner.tick(job.job_id)
    assert _jev_codes(store, job) == ["no_api_key"]
    # off: no observe call at all
    store2 = Store(steward_home)
    job2 = _contracted_job(store2)
    store2.kv_set(job2.job_id, "supervisor_jev", "off")
    runner2, _ = _jev_runner(store2, job2, enabled=True)
    runner2.tick(job2.job_id)
    assert _jev_codes(store2, job2) == []
    # unset: default is now shadow (Jev is free and always on)
    store3 = Store(steward_home)
    job3 = _contracted_job(store3)
    runner3, _ = _jev_runner(store3, job3, enabled=False)
    runner3.tick(job3.job_id)
    assert _jev_codes(store3, job3) == ["no_api_key"]


def test_supervisor_rows_switch_field(steward_home):
    store = Store(steward_home)
    job = _running(store)
    rows = {r["role"]: r for r in supervisor_rows(store, job.job_id)}
    assert rows["astra"]["switch"] == "shadow▸" and rows["astra"]["display"].startswith("[shadow▸] ")
    assert rows["sol"]["switch"] == "on" and rows["sol"]["display"].startswith("[on]")
    assert "switch" not in rows["worker"]
    store.kv_set(job.job_id, "supervisor_sol", "off")
    store.kv_set(job.job_id, "supervisor_jev", "on")
    store.kv_set(job.job_id, "jev_status", "unavailable:no_api_key")
    rows = {r["role"]: r for r in supervisor_rows(store, job.job_id)}
    assert rows["sol"]["switch"] == "off" and rows["sol"]["display"].startswith("[off]")
    assert rows["jev"]["switch"] == "shadow▸"
    assert rows["jev"]["display"] == "[shadow▸] 10s · 0 checks · 0 flags · no api key"
    store.kv_set(job.job_id, "supervisor_jev", "off")
    assert supervisor_rows(store, job.job_id)[4]["display"] == "[off]"


def test_renderer_map_toggle_columns_match_brackets(steward_home, tmp_path):
    store = Store(steward_home)
    job = _running(store)
    store.kv_set(job.job_id, "herdr_tab", "w1:t2")
    store.kv_set(job.job_id, "worker_model", "sonnet")
    map_path = tmp_path / "map"
    env = {**os.environ, "STEWARD_HOME": str(steward_home), "STEWARD_DASH_COLS": "60",
           "STEWARD_DASH_MAP": str(map_path), "PYTHONPATH": str(_REPO)}
    res = subprocess.run([sys.executable, str(_REPO / "deploy/cockpit/steward-dash-jobs.py")],
                         capture_output=True, text=True, env=env, cwd=_REPO)
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", ln).replace("{SPIN}", "x") for ln in res.stdout.splitlines()]
    assert all(len(ln) <= 60 for ln in lines), lines
    toggles = [a.split() for a in map_path.read_text().splitlines() if a.startswith("toggle ")]
    assert [t[5] for t in toggles] == ["astra", "sol", "efficiency", "jev"]
    for _, idx, c0, c1, jid, role, nxt in toggles:
        line = lines[int(idx)]
        assert line[int(c0) - 1] == "[" and line[int(c1) - 1] == "]", (line, c0, c1)
        label = role_label(role)
        assert (label if len(label) <= 8 else label[:7] + "…") in line and jid == job.job_id
    assert [t[6] for t in toggles] == ["active", "off", "active", "active"]
