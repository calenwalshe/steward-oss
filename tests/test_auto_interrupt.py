"""Code-initiated interrupt of pure waits (latency #7). Fakes only: a recording worker, an in-memory shim agent,
scripted Jev verdicts."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest

from steward import auto_interrupt as ai
from steward import inflight
from steward.fabric import Frame
from steward.shim import Shim
from steward.steering import Steering
from steward.store import Store
from test_fabric_worker import FakeApi, FakeMailbox
from test_latency_fastpath import _SimClock, _fabric_runner
from test_runner_fabric import _contracted_job

WAIT = "end=$((SECONDS+570)); until [ -f islands.done ] || [ $SECONDS -gt $end ]; do sleep 20; done"
NOW = 1_800_000_000.0


def _call(i, command=WAIT, name="Bash"):
    return {"id": f"toolu_{i}", "name": name, "input": {"command": command} if name == "Bash" else {}}


class _Worker:
    def __init__(self, store, job_id):
        self.store, self.job_id, self.sent = store, job_id, []

    def interrupt_waits(self, **kw):
        # the decision must already be persisted when the cancel goes out
        assert self.store.kv_get(self.job_id, ai.LAST_KEY)["gen"] == kw["gen"]
        assert any(e.what == "auto_interrupt_decided" for e in self.store.events(self.job_id))
        self.sent.append(kw)


def _setup(steward_home, *calls, queued_s=2.0, gen=4, urgent=False):
    store = Store(steward_home)
    job = _contracted_job(store)
    st = Steering(store)
    st.record(job.job_id, "nudge", "use the new port", origin="cli", urgent=urgent)
    instructions = st.all_instructions(job.job_id)
    instructions[-1].update(status="delivered",
                            delivered_at=datetime.fromtimestamp(NOW - queued_s, tz=timezone.utc).isoformat())
    store.kv_set(job.job_id, "instructions", instructions)
    inflight.record(store, job.job_id, {"gen": gen, "since": NOW - 60, "calls": list(calls) or [_call(1)]})
    return store, job, _Worker(store, job.job_id)


def _engine(store, verdict=("wait_poll", 0.9), clock=None):
    asked = []

    def jev(st, jid, batch):
        asked.append(batch["gen"])
        return None if verdict is None else {"choice": verdict[0], "confidence": verdict[1]}

    return ai.AutoInterrupt(store, jev, clock=clock or (lambda: NOW)), asked


def test_interrupts_a_batch_of_pure_waits_with_a_queued_decision_and_jev_agreeing(steward_home):
    store, job, worker = _setup(steward_home, _call(1), _call(2, "sleep 8"), _call(3, "tail -f gpu.log"))
    engine, asked = _engine(store)
    decision = engine.step(job.job_id, worker)
    assert worker.sent == [{"gen": 4, "call_ids": ["toolu_1", "toolu_2", "toolu_3"], "rev": 1,
                            "rule_version": ai.RULE_VERSION}]
    assert decision["revision"] == store.get_revision(job.job_id) and decision["queued_s"] == 2.0
    (ev,) = [e for e in store.events(job.job_id) if e.what == "auto_interrupt_decided"]
    for key in ("rule_version", "gen", "call_ids", "rev", "revision", "jev"):
        assert key in ev.evidence


@pytest.mark.parametrize("other", [
    _call(9, "python3 -m pytest -q"),               # test
    _call(9, "npm run build"),                      # build
    _call(9, "git commit -am wip"),                 # git write
    _call(9, "sleep 5 > state.txt"),                # file write
    _call(9, name="Agent"),                         # subagent
    _call(9, name="WebFetch"),                      # network
    _call(9, "curl -s https://example.com"),        # network via Bash
    _call(9, "frobnicate --watch"),                 # unknown
    _call(9, name="Edit"),                          # file edit
])
def test_any_protected_call_in_the_batch_vetoes(steward_home, other):
    store, job, worker = _setup(steward_home, _call(1), other)
    engine, asked = _engine(store)
    assert engine.step(job.job_id, worker) is None and worker.sent == [] and asked == []


@pytest.mark.parametrize("verdict", [("work", 0.95), ("wait_poll", 0.79), ("blocked", 0.9), None])
def test_jev_must_agree_with_confidence(steward_home, verdict):
    store, job, worker = _setup(steward_home)
    engine, _ = _engine(store, verdict)
    assert engine.step(job.job_id, worker) is None and worker.sent == []


def test_decision_must_be_queued_at_least_1s(steward_home):
    store, job, worker = _setup(steward_home, queued_s=0.5)
    engine, asked = _engine(store)
    assert engine.step(job.job_id, worker) is None and asked == []


def test_no_queued_decision_or_only_urgent_means_nothing(steward_home):
    store, job, worker = _setup(steward_home, urgent=True)  # urgent steers interrupt by their own (human) path
    engine, _ = _engine(store)
    assert engine.step(job.job_id, worker) is None


def test_at_most_once_per_job_per_30s_and_jev_asked_once_per_batch(steward_home):
    clock = _SimClock(NOW)
    store, job, worker = _setup(steward_home)
    engine, asked = _engine(store, clock=clock)
    assert engine.step(job.job_id, worker) is not None
    inflight.record(store, job.job_id, {"gen": 5, "since": NOW, "calls": [_call(7)]})
    clock.t = NOW + 10
    assert engine.step(job.job_id, worker) is None  # cooldown
    clock.t = NOW + 31
    assert engine.step(job.job_id, worker) is not None
    assert len(worker.sent) == 2 and asked == [4, 5]
    clock.t = NOW + 70
    engine.step(job.job_id, worker)
    assert asked == [4, 5]  # gen 5 answered already


def test_kill_switch(steward_home, monkeypatch):
    monkeypatch.setenv(ai.ENV, "0")
    store, job, worker = _setup(steward_home)
    engine, asked = _engine(store)
    assert engine.step(job.job_id, worker) is None and asked == []


def test_unknown_batch_means_protected(steward_home):
    store, job, worker = _setup(steward_home)
    store.kv_set(job.job_id, inflight.KEY, None)
    engine, _ = _engine(store)
    assert engine.step(job.job_id, worker) is None


# -- the shim re-checks and acts -----------------------------------------------------------------------------


class _Agent:
    def __init__(self):
        self.stdin = io.StringIO()


def _shim_with_batch(*blocks):
    api = FakeApi()
    shim = Shim(job="jobA", peer_pane="w9:p1", api=api, mailbox=FakeMailbox(), spawn=lambda a: _Agent(),
                agent_argv=["x"], print_fn=lambda line: None)
    shim._agent = _Agent()
    shim._write_user_message("brief")
    shim._handle_agent_event({"type": "assistant", "message": {"id": "m1", "content": [
        {"type": "tool_use", "id": b["id"], "name": b["name"], "input": b["input"]} for b in blocks]}})
    return shim, api


def _written(shim):
    return [json.loads(line) for line in shim._agent.stdin.getvalue().splitlines()]


def _cmd(gen, ids):
    return Frame(v=1, seq=50, job="jobA", type="cmd", body={"op": "interrupt_waits", "gen": gen, "call_ids": ids,
                                                             "rev": 3, "rule_version": ai.RULE_VERSION})


def test_shim_cancels_only_after_rechecking_and_says_the_task_is_still_running():
    shim, api = _shim_with_batch(_call(1), _call(2, "sleep 9"))
    shim._handle_inbound(_cmd(1, ["toolu_1", "toolu_2"]))
    lines = _written(shim)
    assert lines[-2]["type"] == "control_request" and lines[-2]["request"]["subtype"] == "interrupt"
    assert lines[-1]["type"] == "user" and ai.NOTE in lines[-1]["message"]["content"]
    events = [f.body for _, f in api.sent if f.type == "event" and f.body.get("kind") == "waits_interrupted"]
    assert events == [{"kind": "waits_interrupted", "gen": 1, "rev": 3, "call_ids": ["toolu_1", "toolu_2"],
                       "rule_version": ai.RULE_VERSION}]
    shim._handle_agent_event({"type": "result", "subtype": "error_during_execution"})  # the cancelled turn
    assert not [f for _, f in api.sent if f.type == "done"]  # never reported as done


@pytest.mark.parametrize("gen,ids,blocks,reason", [
    (2, ["toolu_1"], [_call(1)], "batch generation changed"),
    (1, ["toolu_1"], [_call(1), _call(2)], "in-flight calls changed"),
    (1, ["toolu_1", "toolu_2"], [_call(1), _call(2, "make test")], "a call is not a pure wait"),
])
def test_shim_refuses_when_the_batch_moved_on(gen, ids, blocks, reason):
    shim, api = _shim_with_batch(*blocks)
    before = len(_written(shim))
    shim._handle_inbound(_cmd(gen, ids))
    assert len(_written(shim)) == before  # nothing cancelled, nothing written
    refused = [f.body for _, f in api.sent if f.type == "event" and f.body.get("kind") == "interrupt_refused"]
    assert refused and refused[0]["reason"] == reason


# -- end to end through the runner's fast path ---------------------------------------------------------------


def test_runner_fast_path_interrupts_a_wait_and_logs_the_outcome(steward_home):
    import time

    store = Store(steward_home)
    clock = _SimClock(time.time())  # steer timestamps are wall time; the simulated clock starts there
    t0 = clock.t
    job, runner, worker, api, mailbox = _fabric_runner(store, clock=clock)
    runner.auto_interrupt = ai.AutoInterrupt(store, lambda st, jid, b: {"choice": "wait_poll", "confidence": 0.93},
                                             clock=clock)
    mailbox.push(Frame(v=1, seq=60, job="jobA", type="event",
                       body={"kind": "batch", "gen": 2, "since": t0 - 30, "calls": [_call(1)]}))
    runner.pump(job.job_id)
    Steering(store).record(job.job_id, "nudge", "the GPU run finished; collect results", origin="cli")
    runner.pump(job.job_id)  # sends the steer: queued from now
    cmds = lambda: [f.body for _, f in api.sent if f.type == "cmd" and f.body.get("op") == "interrupt_waits"]  # noqa: E731
    assert cmds() == []  # not yet queued 1 s
    clock.t = t0 + 1.5
    runner.pump(job.job_id)
    assert [c["gen"] for c in cmds()] == [2]
    mailbox.push(Frame(v=1, seq=61, job="jobA", type="event", body={
        "kind": "waits_interrupted", "gen": 2, "rev": 1, "call_ids": ["toolu_1"], "rule_version": ai.RULE_VERSION}))
    runner.pump(job.job_id)
    (applied,) = [e for e in store.events(job.job_id) if e.what == "auto_interrupt_applied"]
    assert applied.evidence["gen"] == 2 and applied.evidence["rule_version"] == ai.RULE_VERSION


def test_d22d6633_parallel_wait_loops_are_eligible_and_a_test_among_them_is_not():
    """The diagnostic's worst hold: three parallel 10/20/30-minute until-loops held a steer for 25 min."""
    loops = [_call(i, f"end=$((SECONDS+{585})); until [ -f islands{i}.done ] || [ $SECONDS -ge $end ]; do sleep 20; done")
             for i in range(3)]
    assert inflight.all_pure_waits({"calls": loops})
    assert not inflight.all_pure_waits({"calls": loops + [_call(9, "python3 -m pytest -q")]})
    assert not inflight.all_pure_waits({"calls": []})
