"""Urgent steer: CLI parse + relay, recording, runner delivery, shim interrupt
(fake agent), done-suppression for the interrupted turn, watchers formatting."""

from __future__ import annotations

import json
import os
import subprocess

import pytest
from test_runner_fabric import _FabricFactory, _contracted_job, _fake_diagnoser, _frame, _git_runner, _obs, _Clock
from test_shim import FakeApi, _make_shim, _run_until, _send_frame, _sent_of_type, _write_script, repo  # noqa: F401
from test_watchers import fmt
from test_fabric_worker import FakeApi as FwApi, _make_worker, _sent_of_type as fw_sent

from steward import cli
from steward.runner import Runner
from steward.steering import Steering
from steward.store import Store


# ------------------------------------------------------------ steering / CLI


def test_urgent_recorded_with_event_and_flag(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    Steering(store).record(job.job_id, "nudge", "stop now", "r1", urgent=True)
    rec = Steering(store).all_instructions(job.job_id)[0]
    assert rec["urgent"] is True and rec["rev"] == 1
    whats = [e.what for e in store.events(job.job_id)]
    assert "urgent_steer_sent" in whats


def test_non_urgent_has_no_flag_or_event(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    Steering(store).record(job.job_id, "nudge", "calm", "r1")
    assert "urgent" not in Steering(store).all_instructions(job.job_id)[0]
    assert "urgent_steer_sent" not in [e.what for e in store.events(job.job_id)]


@pytest.mark.parametrize("origin", ["trajectory", "steward", "contract", "diagnosis"])
def test_code_paths_can_never_send_urgent(steward_home, origin):
    store = Store(steward_home)
    job = _contracted_job(store)
    with pytest.raises(ValueError, match="urgent"):
        Steering(store).record(job.job_id, "nudge", "x", "r1", origin=origin, urgent=True)
    assert Steering(store).all_instructions(job.job_id) == []


def test_trajectory_and_diagnosis_source_never_pass_urgent():
    import pathlib

    root = pathlib.Path(cli.__file__).parent
    for name in ("trajectory.py", "diagnosis.py", "contract.py"):
        assert "urgent" not in (root / name).read_text(), name


def test_cli_steer_urgent_relays_and_records(steward_home, capsys):
    assert cli.main(["new", "g", "--repo", "/repo"]) == 0
    job_id = capsys.readouterr().out.split()[0]
    assert cli.main(["steer", job_id, "stop waiting", "--urgent"]) == 0
    assert cli.main(["nudge", job_id, "again", "--urgent"]) == 0
    assert cli.main(["focus", job_id, "calm"]) == 0
    recs = Steering(Store(steward_home)).all_instructions(job_id)
    assert [bool(r.get("urgent")) for r in recs][-3:] == [True, True, False]


def test_op_and_args_carry_urgent():
    ns = cli._build_parser().parse_args(["steer", "j", "t", "--urgent"])
    assert cli._op_and_args_for(ns)[1]["urgent"] is True
    ns = cli._build_parser().parse_args(["steer", "j", "t"])
    assert "urgent" not in cli._op_and_args_for(ns)[1]


# ------------------------------------------------------- runner / fabric worker


def test_fabric_worker_urgent_adds_body_key():
    api = FwApi()
    w = _make_worker(api=api)
    w.deliver("[rev=4] go", urgent=True)
    w.deliver("[rev=5] calm")
    steers = fw_sent(api, "steer")
    assert steers[0].body == {"rev": 4, "text": "go", "urgent": True}
    assert steers[1].body == {"rev": 5, "text": "calm"}


def test_runner_delivers_urgent_flag_and_records_interrupt_event(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    intr = _obs(events=(_frame(3, "event", {"kind": "interrupted", "rev": 1}),))
    factory = _FabricFactory(scripts=[[intr]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    Steering(store).record(job.job_id, "nudge", "stop", "r1", urgent=True)
    Steering(store).record(job.job_id, "nudge", "later", "r2")
    runner.tick(job.job_id)
    w = factory.created[0]
    assert w.delivered == ["[rev=1] stop", "[rev=2] later"]
    assert w.urgent_delivered == ["[rev=1] stop"]
    ev = [e for e in store.events(job.job_id) if e.what == "worker_interrupted"]
    assert ev and ev[0].evidence["rev"] == 1


# ------------------------------------------------------------------- shim


def _spy(shim):
    """Record every JSON line the shim writes to the agent's stdin."""
    log = []
    orig = shim._write_json_line

    def spy(obj):
        log.append(obj)
        orig(obj)

    shim._write_json_line = spy
    return log


def test_urgent_steer_interrupts_then_writes_message_and_result_is_not_done(repo):
    script = _write_script(repo, [{"say": "running"}, {"hang": True}, {"say": "PINEAPPLE"}, {"result": {}}])
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        log = _spy(shim)
        shim.start()
        _send_frame(write_fd, 1, "brief", {"text": "go"})
        _run_until(shim, lambda: shim._turn_active)
        shim.step(0.5)
        _send_frame(write_fd, 2, "steer", {"rev": 9, "text": "stop waiting", "urgent": True})
        _run_until(shim, lambda: _sent_of_type(api, "done"))
        kinds = [(m["type"], m.get("request", {}).get("subtype")) for m in log]
        assert kinds[0] == ("user", None)
        assert kinds[1] == ("control_request", "interrupt")
        assert kinds[2] == ("user", None)
        assert log[2]["message"]["content"] == "[rev=9] stop waiting"
        # exactly one done: the new turn's, never the interrupted turn's
        dones = _sent_of_type(api, "done")
        assert len(dones) == 1 and dones[0].body["result_subtype"] == "success"
        interrupted = [f for f in _sent_of_type(api, "event") if f.body.get("kind") == "interrupted"]
        assert interrupted and interrupted[0].body["rev"] == 9
        acks = [{k: v for k, v in f.body.items() if k != "at"} for f in _sent_of_type(api, "ack") if "rev" in f.body]
        assert {"rev": 9, "stage": "delivered"} in acks and {"rev": 9, "stage": "acknowledged"} in acks
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_interrupted_result_alone_does_not_produce_done(repo):
    script = _write_script(repo, [{"hang": True}, {"hang": True}])
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        log = _spy(shim)
        shim.start()
        _send_frame(write_fd, 1, "brief", {"text": "go"})
        _run_until(shim, lambda: shim._turn_active)
        _send_frame(write_fd, 2, "steer", {"rev": 1, "text": "x", "urgent": True})
        _run_until(shim, lambda: not shim._interrupting and any(m["type"] == "user" for m in log[1:]))
        for _ in range(5):
            shim.step(0.1)
        assert _sent_of_type(api, "done") == []
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_urgent_steer_on_idle_agent_skips_interrupt(repo):
    script = _write_script(repo, [{"result": {}}, {"say": "ok"}, {"result": {}}])
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        log = _spy(shim)
        shim.start()
        _send_frame(write_fd, 1, "brief", {"text": "go"})
        _run_until(shim, lambda: len(_sent_of_type(api, "done")) == 1)
        _send_frame(write_fd, 2, "steer", {"rev": 2, "text": "hi", "urgent": True})
        _run_until(shim, lambda: len(_sent_of_type(api, "done")) == 2)
        assert not [m for m in log if m["type"] == "control_request"]
        assert not [f for f in _sent_of_type(api, "event") if f.body.get("kind") == "interrupted"]
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_normal_steer_never_interrupts_and_normal_result_is_done(repo):
    script = _write_script(repo, [{"hang": True}, {"result": {}}])
    api = FakeApi()
    read_fd, write_fd = os.pipe()
    try:
        shim = _make_shim(repo, script, api, read_fd)
        log = _spy(shim)
        shim.start()
        _send_frame(write_fd, 1, "brief", {"text": "go"})
        _run_until(shim, lambda: shim._turn_active)
        _send_frame(write_fd, 2, "steer", {"rev": 1, "text": "polite"})
        _run_until(shim, lambda: _sent_of_type(api, "done"))
        assert not [m for m in log if m["type"] == "control_request"]
    finally:
        os.close(write_fd)
        os.close(read_fd)


# ---------------------------------------------------------------- watchers


def test_watchers_format_urgent():
    role, text = fmt("nudge instruction recorded", {"rev": 9, "origin": "cli", "text": "stop", "urgent": True})
    assert (role, text) == ("steer", "URGENT rev 9 [cli]: stop")
    assert fmt("urgent_steer_sent", {"rev": 9}) is None
    assert fmt("worker_interrupted", {"rev": 9}) == ("steer", "worker interrupted for rev 9")
