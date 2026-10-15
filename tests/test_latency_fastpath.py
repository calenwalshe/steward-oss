"""Latency fast path #1 (docs/diagnostics/2026-10-02-latency): a code-policy permission answer is relayed in
the same pass it is decided, and between ticks the runner drains the shim mailbox and decides on arrival.
Fakes only: FakeApi/FakeMailbox, no Herdr, no shim process."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from steward import approvals as approvals_module
from steward import runner as runner_module
from steward.fabric import Frame
from steward.fabric_worker import FabricWorker
from steward.runner import Runner
from steward.store import Store
from test_fabric_worker import FakeApi, FakeMailbox
from test_runner_fabric import _Clock, _contracted_job, _fake_diagnoser, _FabricFakeWorker, _frame, _git_runner, _obs

DATA = Path(__file__).resolve().parent / "fixtures" / "latency"


def _perms(store, job_id):
    return [a for a in approvals_module.list_approvals(store, job_id) if a["kind"] == "permission"]


def _req(seq, rid, command="ls -la"):
    return Frame(v=1, seq=seq, job="jobA", type="permission_request",
                 body={"request_id": rid, "tool": "Bash", "input": {"command": command}})


class _SimClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _fabric_runner(store, clock=None):
    """A runner driving a real FabricWorker over an in-memory mailbox (the shim is the test)."""
    api, mailbox = FakeApi(), FakeMailbox()
    worker = FabricWorker(api=api, mailbox=mailbox, runner_pane="w9:p1", job="jobA",
                          spawn_pane=lambda w: "w1:p2", ready_timeout_s=0.01)
    worker.pane_id = "w1:p2"
    job = _contracted_job(store)
    runner = Runner(store, lambda ref: worker, _git_runner(), _fake_diagnoser(), clock=clock or _Clock())
    # dispatch without the ready handshake: attach the worker the way start() would
    from steward import lease
    from steward.models import JobState, Judgment
    store.set_state(job.job_id, JobState.RUNNING, what="worker dispatch started", evidence={}, rule="t",
                    judgment=Judgment.code)
    lease.acquire(store, job.job_id, "w1:p2")
    runner._workers[job.job_id] = worker
    runner._attempts[job.job_id] = 1
    return job, runner, worker, api, mailbox


def _answers(api, type_="approve"):
    """Answers sent, one per frame (transport retransmits of the same frame are not a second answer)."""
    seen, out = set(), []
    for _, f in api.sent:
        if f.type == type_ and f.seq not in seen:
            seen.add(f.seq)
            out.append(f.body["request_id"])
    return out


def test_code_policy_answer_is_relayed_in_the_same_tick_it_is_decided(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    worker = _FabricFakeWorker(script=[_obs(lifecycle="blocked", events=(_frame(3, "permission_request", {
        "request_id": "r1", "tool": "WebSearch", "input": {"query": "x"}}),))])
    runner = Runner(store, lambda ref: worker, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    runner.tick(job.job_id)
    assert worker.answered_ids == ["r1"]  # not one tick later
    (rec,) = _perms(store, job.job_id)
    assert rec["status"] == "approved" and rec["relayed_at"]


def test_kill_switch_restores_next_tick_relay(steward_home, monkeypatch):
    monkeypatch.setenv(runner_module.FAST_PATH_ENV, "0")
    store = Store(steward_home)
    job = _contracted_job(store)
    worker = _FabricFakeWorker(script=[_obs(lifecycle="blocked", events=(_frame(3, "permission_request", {
        "request_id": "r1", "tool": "WebSearch", "input": {"query": "x"}}),)), _obs()])
    runner = Runner(store, lambda ref: worker, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    runner.tick(job.job_id)
    assert worker.answered_ids == []
    runner.tick(job.job_id)
    assert worker.answered_ids == ["r1"]


def test_permission_arriving_between_ticks_is_answered_within_one_poll(steward_home):
    store = Store(steward_home)
    job, runner, worker, api, mailbox = _fabric_runner(store)
    slept: list[float] = []
    sent_at: dict[str, float] = {}

    def sleep(d):
        slept.append(d)
        if len(slept) == 10:  # ~2 s into the 15 s sleep the worker asks
            mailbox.push(_req(5, "r1"))
        for rid in _answers(api):
            sent_at.setdefault(rid, sum(slept))

    runner._sleep_between_ticks(job.job_id, 15.0, sleep)
    assert sent_at["r1"] - sum(slept[:10]) <= runner_module.FAST_POLL_S + 1e-9
    assert all(d <= runner_module.FAST_POLL_S for d in slept)
    # the next tick sees the frame (held for it) but neither re-opens nor re-relays it
    runner.tick(job.job_id)
    assert len(_perms(store, job.job_id)) == 1
    assert _answers(api) == ["r1"]


def test_frames_drained_between_ticks_still_reach_the_tick_in_order(steward_home):
    store = Store(steward_home)
    job, runner, worker, api, mailbox = _fabric_runner(store)
    mailbox.push(_req(5, "r1"))
    mailbox.push(Frame(v=1, seq=6, job="jobA", type="event", body={"kind": "activity", "tool": "Bash"}))
    assert runner.pump(job.job_id) == 1  # only the permission request is acted on now
    obs = worker.observe()
    assert [f.seq for f in obs.events] == [5, 6]
    assert worker.observe().events == ()


def test_human_answer_given_between_ticks_goes_out_on_the_next_poll(steward_home):
    store = Store(steward_home)
    job, runner, worker, api, mailbox = _fabric_runner(store)
    rec = approvals_module.open_approval(store, job.job_id, "worker permission: Bash rm", kind="permission",
                                         worker_request_id="r7")
    approvals_module.decide(store, job.job_id, rec["id"], "deny", decided_by="human")
    mailbox.push(Frame(v=1, seq=9, job="jobA", type="event", body={"kind": "activity", "tool": "Bash"}))
    runner._sleep_between_ticks(job.job_id, 0.4, lambda d: None)
    assert _answers(api, "deny") == ["r7"]


def _code_rows(limit):
    with open(DATA / "approvals.csv", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["kind"] == "permission" and r["decided_by"] == "code"
                and r["decided_to_relayed_s"]]
    return rows[:limit]


def test_replay_code_policy_turnaround_p95_under_500ms(steward_home):
    """Replay the measured arrival pattern (data/approvals.csv: code-policy permissions, decided -> relayed was
    15.2 s p50 under the tick) through the real runner loop with a simulated clock: each request arrives at its
    own phase within a 15 s tick; the answer must go out within 500 ms p95."""
    rows = _code_rows(120)
    assert len(rows) >= 100
    old = sorted(float(r["decided_to_relayed_s"]) for r in rows)
    assert old[len(old) // 2] > 14.0  # the tick tax this fixes

    store = Store(steward_home)
    clock = _SimClock()
    job, runner, worker, api, mailbox = _fabric_runner(store, clock=clock)
    arrivals = []
    for i, r in enumerate(rows):
        t = datetime.fromisoformat(r["created_at"]).timestamp()
        arrivals.append((i * 15.0 + (t % 15.0), f"r{i}"))
    arrivals.sort()
    start = clock.t
    pending = list(arrivals)
    sent_at: dict[str, float] = {}
    seq = 100

    def sleep(d):
        nonlocal seq
        clock.t += d
        while pending and start + pending[0][0] <= clock.t:
            _, rid = pending.pop(0)
            seq += 1
            mailbox.push(_req(seq, rid))
        for rid in _answers(api):
            sent_at.setdefault(rid, clock.t)

    for _ in range(len(rows) + 1):
        runner._sleep_between_ticks(job.job_id, 15.0, sleep)
        runner.tick(job.job_id)
        for rid in _answers(api):
            sent_at.setdefault(rid, clock.t)
    lat = sorted(sent_at[rid] - (start + off) for off, rid in arrivals)
    assert len(lat) == len(rows)
    p95 = lat[int(0.95 * (len(lat) - 1))]
    assert p95 < 0.5, p95
    assert len(set(_answers(api))) == len(_answers(api))  # every answer exactly once
