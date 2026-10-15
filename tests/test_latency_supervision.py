"""Supervision acts immediately (latency #6): the shim's in-flight batch, suspect_stall -> diagnosis after a batch
check, Jev flags >= 0.8 requesting a diagnosis (rate-limited, never interrupting), watcher nudges deferred only
by an unconsumed steer, and efficiency findings coalesced into one steer per job per 5 min. Fakes only."""

from __future__ import annotations

import csv
import io
from pathlib import Path

from steward import custom_watchers as cw
from steward import efficiency_watch, heartbeat, inflight, jev_watch
from steward import watcher_authority as wa
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.fabric import Frame
from steward.runner import Runner, _JevActions
from steward.shim import Shim
from steward.steering import Steering
from steward.store import Store
from test_fabric_worker import FakeApi, FakeMailbox
from test_latency_fastpath import _fabric_runner
from test_runner_fabric import _Clock, _contracted_job, _FabricFakeWorker, _frame, _git_runner, _obs

DATA = Path(__file__).resolve().parent / "fixtures" / "latency"
WAIT = "end=$((SECONDS+585)); until [ -f islands.done ] || [ $SECONDS -gt $end ]; do sleep 20; done"


def _batch(gen, *calls):
    return _frame(70 + gen, "event", {"kind": "batch", "gen": gen, "since": 1.0, "calls": list(calls)})


def _bash(i, command):
    return {"id": f"toolu_{i}", "name": "Bash", "input": {"command": command}}


# -- the shim reports the in-flight batch ------------------------------------------------------------------


class _Agent:
    def __init__(self):
        self.stdin = io.StringIO()


def test_shim_reports_the_in_flight_batch_and_its_generation():
    api = FakeApi()
    shim = Shim(job="jobA", peer_pane="w9:p1", api=api, mailbox=FakeMailbox(), spawn=lambda a: _Agent(),
                agent_argv=["x"], print_fn=lambda line: None)
    shim._agent = _Agent()
    shim._handle_agent_event({"type": "assistant", "message": {"id": "m1", "content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": WAIT, "description": "wait"}},
        {"type": "tool_use", "id": "t2", "name": "Agent", "input": {"prompt": "secret plan"}}]}})
    shim._handle_agent_event({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t2", "content": "ok"}]}})
    shim._handle_agent_event({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})
    shim._handle_agent_event({"type": "assistant", "message": {"id": "m2", "content": [
        {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "ls"}}]}})
    batches = [f.body for _, f in api.sent if f.type == "event" and f.body.get("kind") == "batch"]
    assert [(b["gen"], [c["id"] for c in b["calls"]]) for b in batches] == [
        (1, ["t1", "t2"]), (1, ["t1"]), (1, []), (2, ["t3"])]
    assert batches[0]["calls"][0]["input"] == {"command": WAIT}  # only what classification needs
    assert batches[0]["calls"][1]["input"] == {}


def test_runner_records_the_batch_from_the_frame(steward_home):
    store = Store(steward_home)
    job, runner, worker, api, mailbox = _fabric_runner(store)
    mailbox.push(_batch(3, _bash(1, WAIT)))
    assert runner.pump(job.job_id) == 1
    assert inflight.current(store, job.job_id)["gen"] == 3
    assert inflight.legitimate(store, job.job_id) == (True, ["pure_wait"])


# -- suspect_stall -> diagnosis, after checking the batch ---------------------------------------------------


def _stall_runner(steward_home, monkeypatch, events=()):
    monkeypatch.setattr(heartbeat, "classify", lambda sig, policy: heartbeat.Class.suspect_stall)
    store = Store(steward_home)
    job = _contracted_job(store)
    worker = _FabricFakeWorker(script=[_obs(events=tuple(events))] + [_obs()] * 4)
    diagnoser = FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="check the log", hypothesis="h"))
    runner = Runner(store, lambda ref: worker, _git_runner(), diagnoser, clock=_Clock())
    runner.start(job.job_id)
    return store, job, runner, worker, diagnoser


def test_suspect_stall_with_a_legitimate_wait_in_flight_is_not_diagnosed(steward_home, monkeypatch):
    store, job, runner, worker, diagnoser = _stall_runner(steward_home, monkeypatch, [_batch(1, _bash(1, WAIT))])
    for _ in range(3):
        report = runner.tick(job.job_id)
    assert report.action == "none" and diagnoser.contexts == []
    checks = [e for e in store.events(job.job_id) if e.what == "stall check: legitimate call in flight"]
    assert len(checks) == 1 and checks[0].evidence["labels"] == ["pure_wait"]  # once per batch
    assert not any(e.what == "stall diagnosis produced" for e in store.events(job.job_id))


def test_suspect_stall_with_work_in_flight_is_diagnosed_in_the_same_pass(steward_home, monkeypatch):
    store, job, runner, worker, diagnoser = _stall_runner(
        steward_home, monkeypatch, [_batch(1, _bash(1, "python3 scraper.py --all"))])
    report = runner.tick(job.job_id)
    assert report.action == "guide" and len(diagnoser.contexts) == 1
    assert Steering(store).all_instructions(job.job_id)[-1]["text"] == "check the log"


# -- Jev flags request a diagnosis ---------------------------------------------------------------------------


class _Actions:
    def __init__(self):
        self.requests = []

    def request_diagnosis(self, job_id, evidence):
        self.requests.append(evidence)
        return "ok"


def _watch(store, actions, clock):
    from steward.jev_observer import JevObserver

    return jev_watch.JevWatch(store, JevObserver(enabled=False), actions, clock)


def test_confident_jev_flag_requests_a_diagnosis_once_per_60s_without_a_streak(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    clock = _Clock(step=0.0)
    actions = _Actions()
    watch = _watch(store, actions, clock)
    watch._gate(job.job_id, {"choice": "stalled", "confidence": 0.85}, "shadow", 1000.0)
    watch._gate(job.job_id, {"choice": "looping", "confidence": 0.9}, "shadow", 1030.0)  # within 60 s
    watch._gate(job.job_id, {"choice": "stalled", "confidence": 0.79}, "shadow", 1100.0)  # below 0.8
    watch._gate(job.job_id, {"choice": "hung_call", "confidence": 0.95}, "shadow", 1200.0)  # not a diagnosis choice
    watch._gate(job.job_id, {"choice": "looping", "confidence": 0.8}, "shadow", 1300.0)
    assert actions.requests == [{"choice": "stalled", "confidence": 0.85}, {"choice": "looping", "confidence": 0.8}]
    evs = [e for e in store.events(job.job_id) if e.what == "jev_diagnosis_requested"]
    assert len(evs) == 2 and evs[0].evidence["mode"] == "shadow"


def _actions_runner(steward_home, verdict, *, batch=None):
    store = Store(steward_home)
    job = _contracted_job(store)
    worker = _FabricFakeWorker(script=[_obs(events=(batch,) if batch else ())] + [_obs()] * 3)
    diagnoser = FakeDiagnoser(Diagnosis(verdict=verdict, guidance="try the other port", hypothesis="h"))
    runner = Runner(store, lambda ref: worker, _git_runner(), diagnoser, clock=_Clock())
    runner.start(job.job_id)
    monkey_classify = heartbeat.classify
    runner.tick(job.job_id)
    return store, job, runner, worker, diagnoser, monkey_classify


def test_jev_requested_diagnosis_only_ever_nudges(steward_home):
    store, job, runner, worker, diagnoser, _ = _actions_runner(steward_home, "retry_with_guidance")
    detail = _JevActions(runner).request_diagnosis(job.job_id, {"choice": "stalled", "confidence": 0.9})
    assert detail.startswith("nudged")
    rec = Steering(store).all_instructions(job.job_id)[-1]
    assert rec["origin"] == "diagnosis" and not rec.get("urgent")
    assert worker.stopped is False and worker.interrupted is False
    assert store.kv_get(job.job_id, "recoveries_used", default=0) == 0


def test_jev_requested_diagnosis_never_replaces_or_escalates(steward_home):
    for verdict in ("replace_worker", "needs_human"):
        store, job, runner, worker, diagnoser, _ = _actions_runner(steward_home, verdict)
        detail = _JevActions(runner).request_diagnosis(job.job_id, {"choice": "stalled", "confidence": 0.9})
        assert detail == f"recorded only: {verdict}"
        assert worker.stopped is False and store.get_job(job.job_id).state.value == "RUNNING"
        assert any(e.what == "diagnosis_would_act" for e in store.events(job.job_id))


def test_no_diagnosis_nudge_while_sendback_awaits_ruling(steward_home):
    from steward.models import Judgment

    store, job, runner, worker, diagnoser, _ = _actions_runner(steward_home, "retry_with_guidance")
    store.append_event(job.job_id, what="yield_classified",
                       evidence={"lane": "STOPPED", "sha": "abc", "failed_preconditions": ["same_as_last_ruled"],
                                 "last_said": "done, awaiting review"},
                       rule="yield_classification", judgment=Judgment.code)
    detail = _JevActions(runner).request_diagnosis(job.job_id, {"choice": "stalled", "confidence": 0.9})
    assert "ruling pending" in detail and diagnoser.contexts == []
    assert not [i for i in Steering(store).all_instructions(job.job_id) if i.get("origin") == "diagnosis"]


def test_jev_diagnosis_shares_the_stall_episode_cap(steward_home):
    from steward.models import Judgment

    store, job, runner, worker, diagnoser, _ = _actions_runner(steward_home, "retry_with_guidance")
    act = _JevActions(runner)
    ask = {"choice": "stalled", "confidence": 0.9}
    assert act.request_diagnosis(job.job_id, ask).startswith("nudged")
    assert "already diagnosed" in act.request_diagnosis(job.job_id, ask)  # same (sha, last_said): one diagnosis
    assert len(diagnoser.contexts) == 1
    store.append_event(job.job_id, what="yield_classified",
                       evidence={"lane": "STOPPED", "sha": "def", "failed_preconditions": ["no_new_commit"],
                                 "last_said": "stuck on the port"},
                       rule="yield_classification", judgment=Judgment.code)
    assert act.request_diagnosis(job.job_id, ask).startswith("nudged")  # a new yield is a new state
    store.kv_set(job.job_id, "stall_episode_diagnosed", True)  # the stall path already spent this episode
    store.append_event(job.job_id, what="yield_classified",
                       evidence={"lane": "STOPPED", "sha": "fed", "failed_preconditions": ["no_new_commit"]},
                       rule="yield_classification", judgment=Judgment.code)
    assert "already diagnosed" in act.request_diagnosis(job.job_id, ask)
    assert len(diagnoser.contexts) == 2


def test_jev_requested_diagnosis_checks_the_batch_first(steward_home):
    store, job, runner, worker, diagnoser, _ = _actions_runner(
        steward_home, "retry_with_guidance", batch=_batch(1, _bash(1, "python3 -m pytest -q")))
    detail = _JevActions(runner).request_diagnosis(job.job_id, {"choice": "stalled", "confidence": 0.9})
    assert "legitimate" in detail and diagnoser.contexts == []


# -- watcher verdicts --------------------------------------------------------------------------------------


def test_watcher_nudge_held_by_an_unconsumed_steer_goes_out_when_it_is_consumed(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    cw.add_watcher(store, job.job_id, cw.make_spec("coverage", "are tests added?", may=["nudge"]))
    w = cw.list_watchers(store, job.job_id)[0]
    store.kv_set(job.job_id, wa.KV_PREFIX + "coverage", {**wa.get_record(store, job.job_id, "coverage"), "tier": 2})
    steering = Steering(store)
    steering.record(job.job_id, "nudge", "first", origin="cli")
    steering.mark_delivered(job.job_id, 1, type("R", (), {"ok": True, "detail": "d"})())
    watchers = cw.CustomWatchers(store, clock=lambda: 5000.0)
    ev = watchers.apply_verdict(job.job_id, w, {"status": "concern", "note": "n", "confidence": 0.9,
                                                "nudge": "add a test for the parser"}, now=5000.0)
    assert "nudge:unacked_pending" in ev["suppressed"]
    assert watchers.flush_deferred(job.job_id, now=5001.0) == []  # still unconsumed
    steering.mark_acknowledged(job.job_id, 1, "acknowledged")
    assert watchers.flush_deferred(job.job_id, now=5002.0) == [2]
    assert steering.all_instructions(job.job_id)[-1]["origin"] == "watcher:coverage"
    assert watchers.flush_deferred(job.job_id, now=5003.0) == []  # sent once


def test_deferred_watcher_nudge_is_dropped_when_stale(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    store.kv_set(job.job_id, "custom_watchers", None)
    cw.add_watcher(store, job.job_id, cw.make_spec("coverage", "q", may=["nudge"]))
    store.kv_set(job.job_id, cw.DEFER_KEY_PREFIX + "coverage",
                 {"text": "t", "at": 0.0, "revision": store.get_revision(job.job_id)})
    watchers = cw.CustomWatchers(store, clock=lambda: 5000.0)
    assert watchers.flush_deferred(job.job_id) == []
    assert any(e.what == "watcher_deferred_nudge_dropped" and e.evidence["reason"] == "expired"
               for e in store.events(job.job_id))


# -- efficiency findings coalesce ---------------------------------------------------------------------------


def test_efficiency_findings_coalesce_into_one_steer_per_job_per_5_min(steward_home, monkeypatch):
    store = Store(steward_home)
    job = _contracted_job(store)
    watcher = efficiency_watch.EfficiencyWatcher(store)
    monkeypatch.setattr(watcher, "_nudge_gates", lambda job_id, f, ctx, now: (
        ["cooldown_job"] if store.kv_get(job_id, "efficiency_last_nudge") is not None
        and now - float(store.kv_get(job_id, "efficiency_last_nudge")) < efficiency_watch.JOB_COOLDOWN_S else []))
    monkeypatch.setattr(watcher, "_policy_line", lambda job_id: "")
    findings = [{"id": f"f{i}", "kind": "serial_agents", "actionable": True, "actions": ["nudge"],
                 "subject": f"s{i}", "evidence": "e", "saving_min": 3.0} for i in range(3)]
    monkeypatch.setattr(efficiency_watch, "nudge_text", lambda f, policy="": f"do {f['id']} faster")
    ctx = {"steering": Steering(store), "batch": [], "tier": 2}
    for f in findings:
        watcher._nudge(job.job_id, f, ctx, 1000.0)
    watcher._send_batch(job.job_id, ctx, 1000.0)
    steers = [i for i in Steering(store).all_instructions(job.job_id) if i["origin"] == efficiency_watch.ORIGIN]
    assert len(steers) == 1 and all(f"do f{i} faster" in steers[0]["text"] for i in range(3))
    nudged = [e for e in store.events(job.job_id) if e.what == "efficiency_nudged"]
    assert {e.evidence["finding"] for e in nudged} == {"f0", "f1", "f2"} and nudged[0].evidence["coalesced"] == 3
    # a new finding 2 minutes later waits for the 5-min job cooldown; after it, it goes out
    ctx = {"steering": Steering(store), "batch": [], "tier": 2}
    late = {**findings[0], "id": "f9"}
    assert watcher._nudge(job.job_id, late, ctx, 1120.0) == ["cooldown_job"]
    ctx = {"steering": Steering(store), "batch": [], "tier": 2}
    assert watcher._nudge(job.job_id, late, ctx, 1000.0 + efficiency_watch.JOB_COOLDOWN_S) == []
    assert efficiency_watch.JOB_COOLDOWN_S == 300.0


def test_measured_supervision_delays_this_targets():
    """data/detectors.csv: suspect_stall -> first action p50 ~200 s, watcher non-ok -> action ~21 min."""
    with open(DATA / "detectors.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    stall = sorted(float(r["detect_to_action_s"]) for r in rows
                   if r["detector"] == "heartbeat:suspect_stall" and r["detect_to_action_s"])
    assert len(stall) >= 50 and stall[len(stall) // 2] > 60
