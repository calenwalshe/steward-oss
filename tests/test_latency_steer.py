"""Latency fast path #2 (docs/diagnostics/2026-10-02-latency): a steer recorded between ticks is sent on the
next poll, its consumption is recorded from the shim's replay frame (with the shim's own time), and a done is
gated on the shim's word about what the model consumed, never on the tick-lagged receipt. Fakes only."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path

from steward import runner as runner_module
from steward.fabric import Frame
from steward.shim import Shim
from steward.steering import Steering
from steward.store import Store
from test_fabric_worker import FakeApi, FakeMailbox
from test_latency_fastpath import _SimClock, _fabric_runner
from test_runner_fabric import _Clock, _contracted_job, _fake_diagnoser, _FabricFactory, _frame, _git_runner, _obs
from steward.runner import Runner

DATA = Path(__file__).resolve().parent / "fixtures" / "latency"


def _steers_sent(api):
    seen, out = set(), []
    for _, f in api.sent:
        if f.type == "steer" and f.seq not in seen:
            seen.add(f.seq)
            out.append(f.body["rev"])
    return out


def test_steer_recorded_between_ticks_is_sent_within_one_poll(steward_home):
    store = Store(steward_home)
    job, runner, worker, api, mailbox = _fabric_runner(store)
    polls = []

    def sleep(d):
        polls.append(d)
        if len(polls) == 5:
            Steering(store).record(job.job_id, "nudge", "look at the failing test", origin="cli")

    runner._sleep_between_ticks(job.job_id, 15.0, sleep)
    assert _steers_sent(api) == [1]
    sent_poll = next(i for i, (_, f) in enumerate(api.sent) if f.type == "steer")
    assert sent_poll >= 0
    # sent on the poll right after it was recorded, not at the next tick
    rec = Steering(store).all_instructions(job.job_id)[0]
    assert rec["status"] == "delivered"


def test_acknowledged_receipt_uses_the_shim_replay_time_and_lands_between_ticks(steward_home):
    store = Store(steward_home)
    job, runner, worker, api, mailbox = _fabric_runner(store)
    Steering(store).record(job.job_id, "nudge", "x", origin="cli")
    runner.pump(job.job_id)
    consumed = 1_700_000_000.25
    mailbox.push(Frame(v=1, seq=40, job="jobA", type="ack", body={"rev": 1, "stage": "acknowledged", "at": consumed}))
    assert runner.pump(job.job_id) == 1
    rec = Steering(store).all_instructions(job.job_id)[0]
    assert rec["status"] == "acknowledged"
    assert datetime.fromisoformat(rec["acknowledged_at"]).timestamp() == consumed
    assert rec["consumed_at"] == rec["acknowledged_at"]
    ev = [e for e in store.events(job.job_id) if e.what == "instruction acknowledged (typed ack frame)"]
    assert ev and ev[-1].evidence["consumed_at"] == consumed


def _done_job(steward_home, done_body, *, steer_status_events=()):
    store = Store(steward_home)
    job = _contracted_job(store)
    done = _obs(lifecycle="idle", events=(*steer_status_events, _frame(11, "done", {"sha": "new222", **done_body})))
    factory = _FabricFactory(scripts=[[_obs(), done]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    Steering(store).record(job.job_id, "nudge", "document the fix", origin="cli")
    runner.tick(job.job_id)  # sends rev 1 (status delivered); the shim's receipt has not been recorded
    return store, job, runner


def test_done_is_not_gated_on_the_tick_lagged_receipt(steward_home):
    store, job, runner = _done_job(steward_home, {"unconsumed_revs": [], "last_rev": 1})
    assert Steering(store).all_instructions(job.job_id)[0]["status"] == "delivered"
    report = runner.tick(job.job_id)
    # the done was not deferred on the lagged receipt: closeout ran in this tick (this fixture has no discoverable checks,
    # so v2.7 parks it as "no checks ran" instead of accepting it)
    assert report.action == "closeout" and report.state == "NEEDS_HUMAN"
    rec = Steering(store).all_instructions(job.job_id)[0]
    assert rec["status"] == "acknowledged"
    assert not [e for e in store.events(job.job_id) if e.rule == "no_closeout_with_unacknowledged_steering"]


def test_done_with_a_steer_the_model_has_not_consumed_is_deferred(steward_home):
    store, job, runner = _done_job(steward_home, {"unconsumed_revs": [1], "last_rev": 1})
    assert runner.tick(job.job_id).state == "RUNNING"
    deferred = [e for e in store.events(job.job_id) if e.rule == "no_closeout_with_unacknowledged_steering"]
    assert deferred and deferred[-1].evidence["unacknowledged_revs"] == [1]


def test_done_produced_before_the_shim_received_the_steer_is_deferred(steward_home):
    store, job, runner = _done_job(steward_home, {"unconsumed_revs": [], "last_rev": 0})
    assert runner.tick(job.job_id).state == "RUNNING"


def test_done_from_an_older_shim_falls_back_to_recorded_receipts(steward_home):
    store, job, runner = _done_job(steward_home, {})
    assert runner.tick(job.job_id).state == "RUNNING"


class _Agent:
    def __init__(self):
        self.stdin = io.StringIO()
        self.stdout = self.stderr = None

    def poll(self):
        return None


def _shim(clock):
    api = FakeApi()
    shim = Shim(job="jobA", peer_pane="w9:p1", api=api, mailbox=FakeMailbox(), spawn=lambda argv: _Agent(),
                agent_argv=["x"], clock=clock, print_fn=lambda line: None)
    shim._agent = _Agent()
    return shim, api


def test_shim_stamps_the_replay_and_names_unconsumed_steers_in_done():
    clock = _SimClock(500.0)
    shim, api = _shim(clock)
    for rev in (1, 2):
        shim._handle_inbound(Frame(v=1, seq=rev, job="jobA", type="steer", body={"rev": rev, "text": "t"}))
    clock.t = 512.5
    shim._handle_agent_event({"type": "user", "message": {"content": "[rev=1] t"}})
    acks = [f.body for _, f in api.sent if f.type == "ack" and f.body.get("stage") == "acknowledged"]
    assert acks == [{"rev": 1, "stage": "acknowledged", "at": 512.5}]
    shim._handle_agent_event({"type": "result", "subtype": "success"})
    (done,) = [f.body for _, f in api.sent if f.type == "done"]
    assert done["unconsumed_revs"] == [2] and done["last_rev"] == 2


def test_replay_steer_transport_recorded_to_sent_p95_under_500ms(steward_home):
    """Replay data/steers.csv (non-contract steers: recorded -> sent was 15.1 s p50 under the tick): each steer
    is recorded at its own phase within a 15 s tick; it must reach the shim within 500 ms p95."""
    with open(DATA / "steers.csv", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["origin"] != "contract" and r["recv_to_deliver_s"]][:60]
    assert len(rows) >= 50
    assert sorted(float(r["recv_to_deliver_s"]) for r in rows)[len(rows) // 2] > 14.0
    store = Store(steward_home)
    clock = _SimClock()
    job, runner, worker, api, mailbox = _fabric_runner(store, clock=clock)
    start = clock.t
    plan = sorted((i * 15.0 + datetime.fromisoformat(r["received_at"]).timestamp() % 15.0, i)
                  for i, r in enumerate(rows))
    pending = list(plan)
    recorded_at: dict[int, float] = {}
    sent_at: dict[int, float] = {}

    def sleep(d):
        clock.t += d
        while pending and start + pending[0][0] <= clock.t:
            pending.pop(0)
            Steering(store).record(job.job_id, "nudge", "t", origin="cli")
            recorded_at[len(recorded_at) + 1] = clock.t
        for rev in _steers_sent(api):
            sent_at.setdefault(rev, clock.t)

    for _ in range(len(rows) + 1):
        runner._sleep_between_ticks(job.job_id, 15.0, sleep)
        runner.tick(job.job_id)
        for rev in _steers_sent(api):
            sent_at.setdefault(rev, clock.t)
    # recorded inside a poll's sleep at clock.t; the poll that follows sends it
    lat = sorted(sent_at[rev] - (start + off) for (off, _), rev in zip(plan, range(1, len(plan) + 1)))
    assert len(lat) == len(rows)
    assert lat[int(0.95 * (len(lat) - 1))] < 0.5
    assert runner_module.FAST_POLL_S < 0.5


def test_shim_acks_every_steer_in_a_coalesced_replay():
    """2026-10-13 lost ack (jobs 9dd959b8/201550e0): steers queued together at launch are replayed by Claude Code as
    ONE user message, "[rev=1] ...\\n[rev=2] ...". Matching only the leading prefix left rev 2 in `unconsumed_revs`
    forever, so every later done was deferred `unacked_steering [2]`."""
    clock = _SimClock(500.0)
    shim, api = _shim(clock)
    for rev in (1, 2):
        shim._handle_inbound(Frame(v=1, seq=rev, job="jobA", type="steer", body={"rev": rev, "text": "t"}))
    clock.t = 512.5
    shim._handle_agent_event({"type": "user", "message": {"content": "[rev=1] focus\n[rev=2] authority"}})
    acks = [f.body for _, f in api.sent if f.type == "ack" and f.body.get("stage") == "acknowledged"]
    assert acks == [{"rev": 1, "stage": "acknowledged", "at": 512.5}, {"rev": 2, "stage": "acknowledged", "at": 512.5}]
    shim._handle_agent_event({"type": "result", "subtype": "success"})
    (done,) = [f.body for _, f in api.sent if f.type == "done"]
    assert done["unconsumed_revs"] == [] and done["last_rev"] == 2


def test_shim_does_not_ack_a_rev_marker_quoted_inside_a_steer():
    """A later-line marker counts only for a rev the shim is still waiting on: steer text that quotes `[rev=N]` on its
    own line must not acknowledge a steer the model has not been shown."""
    clock = _SimClock(500.0)
    shim, api = _shim(clock)
    for rev in (1, 2):
        shim._handle_inbound(Frame(v=1, seq=rev, job="jobA", type="steer", body={"rev": rev, "text": "t"}))
    shim._handle_agent_event({"type": "user", "message": {"content": "[rev=1] see\n[rev=7] quoted"}})
    acked = [f.body["rev"] for _, f in api.sent if f.type == "ack" and f.body.get("stage") == "acknowledged"]
    assert acked == [1]


def test_done_heals_a_delivered_rev_the_shim_reports_consumed_alongside_an_acked_one(steward_home):
    """The runner side of the lost ack: rev 1 acked, rev 2 still `delivered` in the store (its ack frame never landed),
    but the shim's done neither lists it nor predates it, so the done must heal rev 2, not defer on it."""
    store, job, runner = _done_job(steward_home, {"unconsumed_revs": [], "last_rev": 2})
    steering = Steering(store)
    steering.record(job.job_id, "nudge", "second steer", origin="cli")
    steering.mark_acknowledged(job.job_id, 1, "acknowledged")
    steering.mark_acknowledged(job.job_id, 2, "delivered")
    report = runner.tick(job.job_id)
    assert report.action == "closeout"
    assert [i["status"] for i in steering.all_instructions(job.job_id)] == ["acknowledged", "acknowledged"]
    assert not [e for e in store.events(job.job_id) if e.rule == "no_closeout_with_unacknowledged_steering"]
