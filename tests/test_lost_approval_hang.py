"""Lost-approval hang (job 8a641f9b retro 2026-09-30): restart safety, restart debounce, hung-call detection,
idle watcher skip. Fakes only: no Herdr, no real shim process, no model calls."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest

from steward import approvals as approvals_module
from steward import cli, hung_call, launch, restart_guard, selfrestart
from steward import custom_watchers as cw
from steward.fabric import Frame
from steward.fabric_worker import FabricWorker
from steward.jev_watch import JevWatch, build_context
from steward.models import JobState, Judgment
from steward.runner import Runner, _JevActions
from steward.shim import Shim
from steward.store import Store, now_iso
from steward.worker import DeliveryResult, WorkerObservation
from test_fabric_worker import FakeApi, FakeMailbox
from test_jev_watch import FAKE_KEY, ScriptedTransport, _resp, _sig
from test_runner_fabric import _Clock, _contracted_job, _fake_diagnoser, _FabricFakeWorker, _frame, _git_runner, _obs


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _perms(store, job_id):
    return [a for a in approvals_module.list_approvals(store, job_id) if a["kind"] == "permission"]


def _events(store, job_id, what):
    return [e for e in store.events(job_id) if e.what == what]


class _ShimFake(_FabricFakeWorker):
    """A fabric worker whose answers are recorded; `send_ok=False` simulates an unreachable pane."""

    def __init__(self, *a, send_ok=True, **kw):
        super().__init__(*a, **kw)
        self.send_ok = send_ok
        self.queries = 0

    def answer_prompt(self, approve, request_id=None):
        if not self.send_ok:
            return DeliveryResult(ok=False, detail="send_failed")
        return super().answer_prompt(approve, request_id)

    def query_permissions(self):
        self.queries += 1


def _running_fabric(store, script):
    job = _contracted_job(store)
    worker = _ShimFake(script=script)
    runner = Runner(store, lambda ref: worker, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    return job, runner, worker


def _event(kind, seq=50, **body):
    return _frame(seq, "event", {"kind": kind, **body})


# -- (1) restart safety -------------------------------------------------------------------


def test_answer_is_persisted_in_flight_until_the_shim_acks_delivery(steward_home):
    store = Store(steward_home)
    req = _frame(3, "permission_request", {"request_id": "r1", "tool": "WebSearch", "input": {"query": "x"}})
    job, runner, worker = _running_fabric(store, [_obs(lifecycle="blocked", events=(req,)), _obs(), _obs(), _obs()])
    runner.tick(job.job_id)  # opens + auto-allows
    runner.tick(job.job_id)  # relays
    (rec,) = _perms(store, job.job_id)
    assert worker.answered_ids == ["r1"]
    assert rec["worker_request_id"] == "r1" and rec["ack_expected"] and not rec.get("delivery_acked_at")
    assert approvals_module.answer_in_flight(rec)
    worker.script.append(_obs(events=(_event("permission_answered", request_id="r1", allow=True),)))
    for _ in range(3):
        runner.tick(job.job_id)
    (rec,) = _perms(store, job.job_id)
    assert rec["delivery_acked_at"] and not approvals_module.answer_in_flight(rec)
    assert worker.answered_ids == ["r1"]  # never sent twice by the same runner


def test_runner_restart_with_in_flight_approval_resends_it(steward_home):
    store = Store(steward_home)
    req = _frame(3, "permission_request", {"request_id": "r1", "tool": "WebSearch", "input": {"query": "x"}})
    job, runner_a, worker_a = _running_fabric(store, [_obs(lifecycle="blocked", events=(req,)), _obs()])
    runner_a.tick(job.job_id)
    runner_a.tick(job.job_id)
    assert worker_a.answered_ids == ["r1"]  # sent ... and then the runner was restarted before any ack

    worker_b = _ShimFake(script=[_obs(), _obs(events=(_event("permission_answered", request_id="r1", allow=True),)),
                                 _obs(), _obs()])
    runner_b = Runner(Store(steward_home), lambda ref: worker_b, _git_runner(), _fake_diagnoser(), clock=_Clock())
    assert runner_b.resume_all([job.job_id]) == [job.job_id]
    runner_b.tick(job.job_id)
    assert worker_b.answered_ids == ["r1"]  # the unacked answer went out again from the new runner
    assert len(_events(store, job.job_id, "permission answer re-sent after restart")) == 1
    runner_b.tick(job.job_id)  # the shim's delivery ack arrives
    runner_b.tick(job.job_id)
    assert worker_b.answered_ids == ["r1"]
    (rec,) = _perms(store, job.job_id)
    assert rec["status"] == "approved" and rec["delivery_acked_at"]


def test_fabric_worker_sends_a_named_answer_even_with_empty_memory():
    """A fresh FabricWorker (after reattach) never answers `no_pending_permission` for a named request."""
    api = FakeApi()
    worker = FabricWorker(api=api, mailbox=FakeMailbox(), runner_pane="w9:p1", job="jobA",
                          spawn_pane=lambda w: "w1:p2")
    worker.pane_id = "w1:p2"
    result = worker.answer_prompt(approve=True, request_id="r1")
    assert result.ok
    assert [f.body for _, f in api.sent if f.type == "approve"] == [{"request_id": "r1"}]


def test_fabric_worker_learns_pending_requests_from_the_shim_snapshot():
    mailbox = FakeMailbox()
    worker = FabricWorker(api=FakeApi(), mailbox=mailbox, runner_pane="w9:p1", job="jobA",
                          spawn_pane=lambda w: "w1:p2")
    worker.pane_id = "w1:p2"
    mailbox.push(Frame(v=1, seq=7, job="jobA", type="event",
                       body={"kind": "pending_permissions", "pending": [{"request_id": "r1", "tool": "Bash"}],
                             "answered": []}))
    assert worker.observe().lifecycle == "blocked"  # the heartbeat now sees the worker is blocked
    mailbox.push(Frame(v=1, seq=8, job="jobA", type="event",
                       body={"kind": "permission_answered", "request_id": "r1", "allow": True}))
    assert worker.observe().lifecycle != "blocked"


def test_unacked_answer_is_never_marked_stale_from_runner_memory(steward_home):
    store = Store(steward_home)
    job, runner, worker = _running_fabric(store, [_obs()] * 5)
    rec = approvals_module.open_approval(store, job.job_id, "worker permission: Bash x", kind="permission",
                                         worker_request_id="r1")
    approvals_module.decide(store, job.job_id, rec["id"], "approve", decided_by="human")
    worker.send_ok = False  # pane briefly unreachable
    runner.tick(job.job_id)
    assert _perms(store, job.job_id)[0]["status"] == "approved"  # still in flight, not stale
    worker.send_ok = True
    runner.tick(job.job_id)
    assert worker.answered_ids == ["r1"]


def test_stale_only_on_shim_confirmation(steward_home):
    store = Store(steward_home)
    job, runner, worker = _running_fabric(store, [_obs()])
    rec = approvals_module.open_approval(store, job.job_id, "worker permission: Bash x", kind="permission",
                                         worker_request_id="r1")
    approvals_module.decide(store, job.job_id, rec["id"], "approve", decided_by="human")
    runner.tick(job.job_id)
    assert _perms(store, job.job_id)[0]["status"] == "approved"
    worker.script.append(_obs(events=(_event("permission_unknown", request_id="r1"),)))
    runner.tick(job.job_id)
    runner.tick(job.job_id)
    (rec,) = _perms(store, job.job_id)
    assert rec["status"] == "stale" and rec["stale_from"] == "approved"
    stale = _events(store, job.job_id, "approval stale")
    assert stale and stale[-1].evidence["reason"] == "shim_confirmed_no_pending"


def test_snapshot_reopens_stale_answer_opens_unknown_and_acks_answered(steward_home):
    store = Store(steward_home)
    job, runner, worker = _running_fabric(store, [_obs()] * 8)
    j = job.job_id
    # r1: approved, relayed, then wrongly marked stale by the old runner (the 8a641f9b state)
    r1 = approvals_module.open_approval(store, j, "worker permission: Bash a", kind="permission", worker_request_id="r1")
    approvals_module.decide(store, j, r1["id"], "approve", decided_by="human")
    approvals_module.mark_relayed(store, j, r1["id"], ack_expected=True, relayed_by="old")
    approvals_module.mark_stale(store, j, r1["id"], reason="no_pending_permission")
    # r2: answered and relayed; the shim delivered it but the ack was lost
    r2 = approvals_module.open_approval(store, j, "worker permission: Bash b", kind="permission", worker_request_id="r2")
    approvals_module.decide(store, j, r2["id"], "deny", decided_by="human")
    approvals_module.mark_relayed(store, j, r2["id"], ack_expected=True, relayed_by="old")
    # r3: relayed but the shim has neither pending nor answered it
    r3 = approvals_module.open_approval(store, j, "worker permission: Bash c", kind="permission", worker_request_id="r3")
    approvals_module.decide(store, j, r3["id"], "approve", decided_by="human")
    approvals_module.mark_relayed(store, j, r3["id"], ack_expected=True, relayed_by="old")
    snapshot = _event("pending_permissions", pending=[
        {"request_id": "r1", "tool": "Bash", "input": {"command": "a"}},
        {"request_id": "r9", "tool": "WebSearch", "input": {"query": "q"}}], answered=["r2"])
    worker.script = [_obs(lifecycle="blocked", events=(snapshot,)), _obs(), _obs()]
    runner.tick(j)  # reconcile
    runner.tick(j)  # relay what is now owed
    by = {a["worker_request_id"]: a for a in _perms(store, j)}
    assert by["r1"]["status"] == "approved"  # its answer came back ...
    assert "r1" in worker.answered_ids  # ... and was re-sent
    assert by["r2"]["delivery_acked_at"]
    assert by["r3"]["status"] == "stale"
    assert by["r9"]["status"] in ("approved", "pending")  # unknown pending request: re-opened as an approval
    assert "r9" in by


def _shim(stdin):
    class _Agent:
        def __init__(self):
            self.stdin = stdin
            self.stdout = None
            self.stderr = None

        def poll(self):
            return None

    api = FakeApi()
    shim = Shim(job="jobA", peer_pane="w1:p1", api=api, mailbox=None, spawn=lambda argv: _Agent(), agent_argv=[],
                print_fn=lambda line: None)
    shim._agent = _Agent()
    return shim, api


def _shim_events(api, kind):
    return [f.body for _, f in api.sent if f.type == "event" and f.body.get("kind") == kind]


def test_shim_acks_delivery_answers_duplicates_and_reports_unknown():
    stdin = io.StringIO()
    shim, api = _shim(stdin)
    shim._handle_control_request({"request_id": "r1", "request": {"subtype": "can_use_tool", "tool_name": "Bash",
                                                                   "input": {"command": "ls"}}})
    shim._handle_inbound(Frame(v=1, seq=1, job="jobA", type="cmd", body={"op": "rebind", "peer": "w9:p9"}))
    (snap,) = _shim_events(api, "pending_permissions")
    assert [p["request_id"] for p in snap["pending"]] == ["r1"] and snap["answered"] == []
    shim._handle_inbound(Frame(v=1, seq=2, job="jobA", type="approve", body={"request_id": "r1"}))
    assert '"request_id": "r1"' in stdin.getvalue()
    shim._handle_inbound(Frame(v=1, seq=3, job="jobA", type="approve", body={"request_id": "r1"}))  # re-sent
    assert stdin.getvalue().count("control_response") == 1  # written to the agent once
    answered = _shim_events(api, "permission_answered")
    assert answered == [{"kind": "permission_answered", "request_id": "r1", "allow": True},
                        {"kind": "permission_answered", "request_id": "r1", "allow": True, "duplicate": True}]
    shim._handle_inbound(Frame(v=1, seq=4, job="jobA", type="deny", body={"request_id": "r7"}))
    assert _shim_events(api, "permission_unknown") == [{"kind": "permission_unknown", "request_id": "r7"}]
    shim._handle_inbound(Frame(v=1, seq=5, job="jobA", type="cmd", body={"op": "list_permissions"}))
    assert _shim_events(api, "pending_permissions")[-1] == {"kind": "pending_permissions", "pending": [],
                                                           "answered": ["r1"]}


# -- (2) debounce ----------------------------------------------------------------------------


class _T:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_restart_debounce_ignores_a_second_restart_within_30s(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    t = _T()
    assert restart_guard.check(store, job.job_id, origin="dash", clock=t, sleep=t.sleep) is None
    restart_guard.stamp(store, job.job_id, t())
    t.t += 11  # 19:53:15 -> 19:53:26
    why = restart_guard.check(store, job.job_id, origin="cli", clock=t, sleep=t.sleep)
    assert why and "ignored" in why
    assert _events(store, job.job_id, "runner_restart_debounced")[0].evidence["origin"] == "cli"
    t.t += 20
    assert restart_guard.check(store, job.job_id, origin="cli", clock=t, sleep=t.sleep) is None


def test_restart_waits_for_an_in_flight_answer_then_refuses_after_10s(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    rec = approvals_module.open_approval(store, job.job_id, "worker permission: Bash x", kind="permission",
                                         worker_request_id="r1")
    approvals_module.decide(store, job.job_id, rec["id"], "approve", decided_by="human")
    t = _T(datetime.now(timezone.utc).timestamp())
    why = restart_guard.check(store, job.job_id, origin="dash", clock=t, sleep=t.sleep)
    assert why and "in flight" in why and _events(store, job.job_id, "runner_restart_deferred")
    # the answer lands during the wait: the restart goes ahead
    t2 = _T(datetime.now(timezone.utc).timestamp())

    def sleep_then_ack(s):
        t2.sleep(s)
        approvals_module.mark_relayed(store, job.job_id, rec["id"], ack_expected=True)
        approvals_module.mark_delivery_acked(store, job.job_id, rec["id"])

    assert restart_guard.check(store, job.job_id, origin="dash", clock=t2, sleep=sleep_then_ack) is None


def test_cli_runner_restart_is_debounced(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    job = _contracted_job(store)
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    calls = []
    monkeypatch.setattr(launch, "restart_runner", lambda *a, **k: calls.append(1) or
                        {"accepted": True, "detail": "runner restarted", "pane": "w1:p7"})
    assert cli.main(["runner-restart", job.job_id, "--origin", "dash"]) == 0
    assert cli.main(["runner-restart", job.job_id]) == 1  # 2nd press inside 30 s
    assert len(calls) == 1 and "ignored" in capsys.readouterr().err


def test_self_restart_respects_debounce_and_in_flight_answers(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    execs = []
    t = _T(datetime.now(timezone.utc).timestamp())
    sr = selfrestart.SelfRestarter(store, behind_fn=lambda j: 3, head_fn=lambda: "new", import_check_fn=lambda: None,
                                   exec_fn=lambda *a: execs.append(a), clock=t, argv=["python3", "x"],
                                   env={}, flush_fn=lambda: None, sleep_fn=t.sleep, min_interval_s=0)
    restart_guard.stamp(store, job.job_id, t() - 5)  # a button restart 5 s ago
    assert sr.maybe_restart(job.job_id) == "debounced" and execs == []
    t.t += 60
    rec = approvals_module.open_approval(store, job.job_id, "worker permission: Bash x", kind="permission",
                                         worker_request_id="r1")
    approvals_module.decide(store, job.job_id, rec["id"], "approve", decided_by="code")
    t.t = datetime.now(timezone.utc).timestamp() + 60
    store.kv_set(job.job_id, selfrestart.STAMP_KEY, None)
    assert sr.maybe_restart(job.job_id) == "answer_in_flight" and execs == []
    approvals_module.mark_relayed(store, job.job_id, rec["id"])  # legacy relay: no ack expected
    t.t += 60
    store.kv_set(job.job_id, selfrestart.STAMP_KEY, None)
    assert sr.maybe_restart(job.job_id) == "restarted" and len(execs) == 1
    assert store.kv_get(job.job_id, restart_guard.STAMP_KEY) is not None


# -- (3) hung call detection -------------------------------------------------------------------


class _Actions:
    def __init__(self):
        self.calls = []

    def rerelay(self, job_id, approval_id):
        self.calls.append(("rerelay", approval_id))
        return "rerelayed"

    def doctor(self, job_id, evidence=None):
        self.calls.append(("doctor", evidence))
        return "doc"

    def permission(self, job_id):
        self.calls.append(("permission", None))
        return "perm"

    def needs_you(self, job_id, text):
        self.calls.append(("needs_you", text))
        return "notified"


def _tool_running(started):
    return lambda s, j: {"turns": [{"kind": "said", "ts": _iso(started - 5), "text": "running it"},
                                   {"kind": "tool", "ts": _iso(started), "text": "Bash: ./long.sh"}]}


def test_tool_call_over_30_min_routes_to_doctor_then_escalates_at_60(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    now = [100_000.0]
    watch = hung_call.HungCallWatch(store, clock=lambda: now[0], activity_fn=_tool_running(now[0]), env={})
    acts = _Actions()
    now[0] += 29 * 60
    assert watch.step(job.job_id, acts) == [] and acts.calls == []
    now[0] += 2 * 60  # 31 min
    (f,) = watch.step(job.job_id, acts)
    assert f["kind"] == "hung_call" and acts.calls[0][0] == "doctor"
    assert acts.calls[0][1]["finding"] == "hung_call" and acts.calls[0][1]["age_s"] >= 1800
    assert _events(store, job.job_id, "hung_call")
    now[0] += 60  # within the repeat window: no re-route
    watch.step(job.job_id, acts)
    assert [c[0] for c in acts.calls] == ["doctor"]
    now[0] += 30 * 60  # 62 min: NEEDS_YOU once, with a one-line cause
    watch.step(job.job_id, acts)
    watch.step(job.job_id, acts, force=True)
    needs = [c[1] for c in acts.calls if c[0] == "needs_you"]
    assert len(needs) == 1 and "Bash: ./long.sh" in needs[0] and "\n" not in needs[0]
    assert _events(store, job.job_id, "hung_call_escalated")


def test_hung_call_threshold_is_configurable(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    now = [100_000.0]
    store.kv_set(job.job_id, "hung_tool_s", 60)
    watch = hung_call.HungCallWatch(store, clock=lambda: now[0], activity_fn=_tool_running(now[0]), env={})
    now[0] += 90
    assert watch.step(job.job_id, _Actions())[0]["kind"] == "hung_call"


def test_permission_stuck_with_an_answer_rerelays_instead_of_doctor(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    rec = approvals_module.open_approval(store, job.job_id, "worker permission: Bash x", kind="permission",
                                         worker_request_id="r1")
    approvals_module.decide(store, job.job_id, rec["id"], "approve", decided_by="human")
    approvals_module.mark_relayed(store, job.job_id, rec["id"], ack_expected=True)
    now = datetime.now(timezone.utc).timestamp() + 6 * 60
    watch = hung_call.HungCallWatch(store, clock=lambda: now, activity_fn=lambda s, j: {"turns": []}, env={})
    acts = _Actions()
    (f,) = watch.step(job.job_id, acts)
    assert f["kind"] == "permission_stuck" and f["state"] == "unacked"
    assert acts.calls == [("rerelay", rec["id"])]


def test_undecided_permission_stuck_routes_to_permission_and_doctor(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    approvals_module.open_approval(store, job.job_id, "worker permission: Bash x", kind="permission",
                                   worker_request_id="r1")
    now = datetime.now(timezone.utc).timestamp() + 6 * 60
    acts = _Actions()
    hung_call.HungCallWatch(store, clock=lambda: now, activity_fn=lambda s, j: {"turns": []}, env={}).step(
        job.job_id, acts)
    assert [c[0] for c in acts.calls] == ["permission", "doctor"]
    assert acts.calls[1][1]["pending_permission"]["state"] == "pending"


def test_runner_rerelay_action_resends_the_unacked_answer(steward_home):
    store = Store(steward_home)
    job, runner, worker = _running_fabric(store, [_obs()] * 3)
    rec = approvals_module.open_approval(store, job.job_id, "worker permission: Bash x", kind="permission",
                                         worker_request_id="r1")
    approvals_module.decide(store, job.job_id, rec["id"], "approve", decided_by="human")
    runner.tick(job.job_id)
    assert worker.answered_ids == ["r1"]
    assert "re-relayed" in _JevActions(runner).rerelay(job.job_id, rec["id"])
    assert worker.answered_ids == ["r1", "r1"] and worker.queries == 1


def test_jev_context_has_tool_and_permission_ages_and_shadow_hung_call_only_records(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    rec = approvals_module.open_approval(store, job.job_id, "worker permission: Bash x", kind="permission",
                                         worker_request_id="r1")
    approvals_module.decide(store, job.job_id, rec["id"], "approve", decided_by="human")
    approvals_module.mark_relayed(store, job.job_id, rec["id"], ack_expected=True)
    now = datetime.now(timezone.utc).timestamp() + 600
    ctx = build_context(store, job.job_id, _sig(), now, activity_fn=_tool_running(now - 2000))
    assert ctx["tool_s"] >= 2000 and ctx["perm_wait_s"] >= 600
    assert any(p["st"] == "unacked" for p in ctx["perms"])

    from steward.jev_observer import JevObserver

    store.kv_set(job.job_id, "supervisor_jev", "shadow")
    t = ScriptedTransport([_resp("hung_call", 0.9)], [now])
    obs = JevObserver(transport=t, enabled=True, api_key_fn=lambda: FAKE_KEY, clock=lambda: now)
    acts = _Actions()
    w = JevWatch(store, obs, acts, lambda: now, activity_fn=lambda s, j: {"turns": []})
    w.step(job.job_id, _sig(), force=True)
    w.step(job.job_id, _sig(), force=True)
    # HOTFIX 2026-10-01: shadow Jev is record-only, hung_call included (it killed a live worker on a 0 s permission)
    assert acts.calls == [] and not _events(store, job.job_id, "jev_escalated")


# -- (4) idle supervision ----------------------------------------------------------------------


class _Popen:
    started = 0

    def __init__(self, argv, **kw):
        _Popen.started += 1
        kw["stdout"].write(json.dumps({"type": "result", "result": '{"status": "ok", "note": "fine"}',
                                       "total_cost_usd": 0.01, "usage": {"input_tokens": 1}}))
        kw["stdout"].flush()
        self.pid = 1

    def poll(self):
        return 0


def test_watcher_skips_scheduled_run_when_transcript_and_diff_unchanged(steward_home):
    store = Store(steward_home)
    job = store.create_job("ship it", "/tmp/somerepo")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.RUNNING.value, job.job_id))
    cw.add_watcher(store, job.job_id, cw.make_spec("drift", "on track?", every="10m"))
    now = [1_800_000_000.0]
    fp = {"v": "a"}
    _Popen.started = 0
    w = cw.CustomWatchers(store, clock=lambda: now[0], popen=_Popen, spend_fn=lambda j: (1.0, 0.0),
                          fingerprint_fn=lambda j: fp["v"])
    w.step(job.job_id)  # first run
    w.step(job.job_id)  # collects the verdict
    assert _Popen.started == 1
    now[0] += 700
    w.step(job.job_id)  # nothing changed: skipped
    assert _Popen.started == 1 and len(_events(store, job.job_id, "watcher_skipped_idle")) == 1
    now[0] += 700
    fp["v"] = "b"  # the worker moved
    w.step(job.job_id)
    assert _Popen.started == 2
    w.step(job.job_id)
    cw.request_run(store, job.job_id, "drift")  # a forced run is never skipped
    w.step(job.job_id)
    assert _Popen.started == 3


def test_watcher_never_skips_when_fingerprint_unknown(steward_home):
    store = Store(steward_home)
    job = store.create_job("ship it", "/tmp/somerepo")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.RUNNING.value, job.job_id))
    cw.add_watcher(store, job.job_id, cw.make_spec("drift", "on track?", every="10m"))
    now = [1_800_000_000.0]
    _Popen.started = 0
    w = cw.CustomWatchers(store, clock=lambda: now[0], popen=_Popen, spend_fn=lambda j: (1.0, 0.0),
                          fingerprint_fn=lambda j: None)
    for _ in range(3):
        w.step(job.job_id)
        w.step(job.job_id)
        now[0] += 700
    assert _Popen.started == 3 and not _events(store, job.job_id, "watcher_skipped_idle")
