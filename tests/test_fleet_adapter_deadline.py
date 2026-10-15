"""Phase 29-01 task 2 (FLEET-18/19): independently qualified runtime capabilities, honest usage, DAG deadline range.

Real Store + temp Git; the app-server and the process are fakes."""

from __future__ import annotations

import json
import signal
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from test_fleet_graph import task
from test_fleet_tech_lead import ALL_CAPS, JOB, TL, entry

from steward import deadline_plan
from steward import fleet_deadline as fd
from steward import fleet_lease as fl
from steward import fleet_registry as fr
from steward import fleet_runtime as rt
from steward import permissions
from steward.runner_exec import SubprocessRunner
from steward.worker import (AppServerError, CodexAppServerAdapter, StdioAppServerTransport,
                            codex_capability_probes, codex_lane_factory)

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


class FakeServer:
    """Scripted `codex app-server`. Records every request; never spawns anything."""

    def __init__(self, *, model="gpt-x", bad_steer=False, tree_ok=True, resume_ok=True, fail=()):
        self.model, self.bad_steer, self.tree_ok, self.resume_ok, self.fail = model, bad_steer, tree_ok, resume_ok, set(fail)
        self.requests, self.inbox, self.responses, self.notified = [], [], [], []
        self.alive_, self.turn_n = True, 0
        self.auto = True

    def request(self, method, params, timeout=30.0):
        self.requests.append((method, params))
        if method in self.fail:
            raise AppServerError(f"{method}: boom")
        if method == "thread/start":
            return {"thread": {"id": "th1"}, "model": self.model}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"] if self.resume_ok else "other"}, "model": self.model}
        if method == "turn/start":
            self.turn_n += 1
            tid = f"turn{self.turn_n}"
            text = params["input"][0]["text"]
            if self.auto:
                self.inbox.append({"method": "turn/started", "params": {"turn": {"id": tid}}})
                if "approval" in text:
                    self.inbox.append({"id": 77, "method": "item/commandExecution/requestApproval",
                                       "params": {"command": "echo steward-probe", "threadId": "th1", "turnId": tid}})
                else:
                    self.inbox.append({"method": "thread/tokenUsage/updated", "params": {
                        "tokenUsage": {"total": {"totalTokens": 1234}}}})
                    self.inbox.append({"method": "turn/completed", "params": {"turn": {"id": tid, "status": "completed"}}})
            return {"turn": {"id": tid}}
        if method == "turn/steer":
            return {"turnId": "other" if self.bad_steer else params["expectedTurnId"]}
        if method == "turn/interrupt":
            self.inbox.append({"method": "turn/completed", "params": {"turn": {"id": params["turnId"],
                                                                                 "status": "interrupted"}}})
            return {}
        return {}

    def notify(self, method, params=None):
        self.notified.append(method)

    def drain(self):
        out, self.inbox = self.inbox, []
        return out

    def respond(self, request_id, result=None, error=None):
        self.responses.append((request_id, result, error))

    def pid(self):
        return 4242

    def alive(self):
        return self.alive_

    def terminate_tree(self):
        if self.tree_ok:
            self.alive_ = False
        return self.tree_ok


def adapter(server=None, **kw):
    server = server or FakeServer()
    kw.setdefault("model", "gpt-x")
    return CodexAppServerAdapter(server, **kw), server


# ------------------------------------------------------------------------------------------------ the adapter

def test_launch_uses_fixed_safe_policy_and_records_exact_model_identity(tmp_path):
    a, srv = adapter(job_id=JOB, task="t1", lane="lane-t1", attempt=1, epoch=1)
    ref = a.start("do it", tmp_path)
    assert ref == "codex:th1" and a.process_identity() == {
        "thread_id": "th1", "native_model": "gpt-x", "requested_model": "gpt-x", "pid": 4242}
    start = dict(srv.requests)["thread/start"]
    assert start["approvalPolicy"] == "on-request" and start["sandbox"] == "workspace-write" and start["model"] == "gpt-x"
    assert srv.notified == ["initialized"] and [m for m, _ in srv.requests][:3] == ["initialize", "thread/start", "turn/start"]


def test_model_identity_mismatch_fails_the_start_and_stops_the_process(tmp_path):
    a, srv = adapter(FakeServer(model="gpt-other"))
    with pytest.raises(AppServerError, match="model_identity_mismatch"):
        a.start("x", tmp_path)
    assert srv.alive_ is False and "turn/start" not in [m for m, _ in srv.requests]


def test_steer_is_a_typed_acknowledged_turn_steer_not_a_keystroke(tmp_path):
    a, srv = adapter(job_id=JOB, task="t1", lane="lane-t1", attempt=1, epoch=1)
    a.start("go", tmp_path)
    a.observe()
    # the scripted turn already completed; start a fresh one to have an in-flight turn to steer
    srv.auto = False
    a.deliver("[rev=0] new turn")  # no active turn -> turn/start
    srv.inbox.append({"method": "turn/started", "params": {"turn": {"id": "turn2"}}})
    a.observe()
    r = a.deliver("[rev=4] focus on the importer")
    assert r.ok
    steer = [p for m, p in srv.requests if m == "turn/steer"][-1]
    assert steer["expectedTurnId"] == "turn2" and steer["threadId"] == "th1"
    assert set(steer) == {"threadId", "expectedTurnId", "input"}  # no policy/sandbox fields can ride a steer
    acks = [e for e in a.observe().events if e.type == "ack"]
    assert [(e.v, e.body["rev"], e.body["stage"], e.lane, e.epoch) for e in acks] == [(2, 4, "acknowledged", "lane-t1", 1)]


def test_a_steer_the_server_did_not_accept_is_not_acknowledged(tmp_path):
    a, srv = adapter(FakeServer(bad_steer=True))
    a.start("go", tmp_path)
    srv.auto = False
    a.turn_id = "turn1"
    r = a.deliver("[rev=2] x")
    assert not r.ok and r.detail == "steer_not_acknowledged"
    assert [e for e in a.observe().events if e.type == "ack"] == []
    a2, _ = adapter(FakeServer(fail={"turn/steer"}))
    a2.start("go", tmp_path)
    a2.turn_id = "turn1"
    assert not a2.deliver("[rev=2] x").ok


def test_permission_requests_use_the_shared_policy_and_are_never_auto_answered(tmp_path):
    a, srv = adapter(job_id=JOB, task="t1", lane="lane-t1", attempt=1, epoch=1)
    a.start("needs approval please", tmp_path)
    frames = [e for e in a.observe().events if e.type == "permission_request"]
    assert srv.responses == [] and len(frames) == 1  # nothing answered by the adapter itself
    f = frames[0]
    assert (f.body["tool"], f.body["input"]) == ("Bash", {"command": "echo steward-probe"})
    wt = tmp_path / "wt"
    wt.mkdir()
    # equivalent request from a Claude-shaped worker gets exactly the same decision
    same = permissions.decide("Bash", f.body["input"], wt)
    assert same == permissions.decide("Bash", {"command": "echo steward-probe"}, wt)
    assert a.answer_prompt(False, f.body["request_id"]).detail == "denied"
    assert srv.responses == [(77, {"decision": "decline"}, None)]
    assert not a.answer_prompt(True, f.body["request_id"]).ok  # already answered


def test_outside_worktree_write_grants_get_the_same_ask_as_any_other_runtime(tmp_path):
    a, srv = adapter()
    a.start("go", tmp_path)
    srv.inbox.append({"id": 5, "method": "item/fileChange/requestApproval",
                      "params": {"grantRoot": "/etc", "reason": "write config"}})
    f = [e for e in a.observe().events if e.type == "permission_request"][0]
    assert f.body["tool"] == "Edit" and f.body["input"] == {"file_path": "/etc"}
    d = permissions.decide(f.body["tool"], f.body["input"], tmp_path)
    assert d == permissions.decide("Edit", {"file_path": "/etc"}, tmp_path) and d.allow is False


def test_unsupported_or_inexpressible_server_requests_are_refused(tmp_path):
    a, srv = adapter()
    a.start("go", tmp_path)
    srv.inbox += [{"id": 8, "method": "item/tool/call", "params": {}},
                  {"id": 9, "method": "item/permissions/requestApproval", "params": {"permissions": {"weird": 1}}},
                  {"id": 10, "method": "item/permissions/requestApproval",
                   "params": {"permissions": {"fileSystem": {"write": ["/outside"]}}}}]
    frames = [e for e in a.observe().events if e.type == "permission_request"]
    assert srv.responses[0] == (8, None, {"code": -32601, "message": "unsupported"})
    assert srv.responses[1] == (9, {"permissions": {}, "scope": "turn"}, None)  # refused, not granted
    assert [(f.body["tool"], f.body["input"]) for f in frames] == [("Edit", {"file_path": "/outside"})]


def test_typed_done_error_and_candidate_sha(tmp_path):
    heads = iter(["base", "newsha"])
    a, srv = adapter(job_id=JOB, task="t1", lane="lane-t1", attempt=1, epoch=1, head_fn=lambda p: next(heads))
    a.start("go", tmp_path)
    done = [e for e in a.observe().events if e.type == "done"][0]
    assert done.body["candidate_sha"] == "newsha" and done.body["evidence_ref"] == "codex:th1:turn1"
    a2, s2 = adapter(head_fn=lambda p: "same")
    a2.start("go", tmp_path)
    s2.inbox[:] = [{"method": "turn/completed", "params": {"turn": {"id": "turn1", "status": "failed",
                                                                     "error": "tool crashed"}}}]
    ev = a2.observe().events
    assert [(e.type, e.body["status"]) for e in ev] == [("error", "failed")] and "candidate_sha" not in ev[0].body
    a3, s3 = adapter(head_fn=lambda p: "same")
    a3.start("go", tmp_path)
    s3.inbox[:] = [{"method": "turn/completed", "params": {"turn": {"id": "turn1", "status": "completed"}}}]
    assert "candidate_sha" not in a3.observe().events[0].body  # nothing committed -> no candidate claimed


def test_stale_lane_frames_are_dropped_by_the_lease_check(tmp_path):
    a, srv = adapter(job_id=JOB, task="t1", lane="lane-t1", attempt=1, epoch=1, lease_check=lambda f: False)
    a.start("go", tmp_path)
    assert a.observe().events == () and a.stale_dropped >= 1


def test_stop_interrupts_then_requires_a_confirmed_process_tree_kill(tmp_path):
    a, srv = adapter()
    a.start("go", tmp_path)
    assert a.stop() is True and "turn/interrupt" in [m for m, _ in srv.requests] and not srv.alive()
    b, s2 = adapter(FakeServer(tree_ok=False))
    b.start("go", tmp_path)
    assert b.stop() is False and s2.alive()  # unconfirmed stays unconfirmed


def test_reattach_requires_the_same_thread(tmp_path):
    a, srv = adapter(FakeServer(resume_ok=True))
    assert a.attach("codex:th9") and a.thread_id == "th9"
    b, _ = adapter(FakeServer(resume_ok=False))
    assert not b.attach("codex:th9")
    c, _ = adapter(FakeServer(fail={"thread/resume"}))
    assert not c.attach("codex:th9")


def test_usage_is_reported_only_when_measured_and_cost_is_never_invented(tmp_path):
    a, srv = adapter()
    assert a.usage() is None
    a.start("go", tmp_path)
    a.observe()
    assert a.usage() == {"tokens": 1234, "cost_usd": None, "source": "codex_app_server", "confidence": "reported"}
    assert rt.honest_usage(a.usage())["cost_usd"] is None
    assert rt.honest_usage(None) == {"tokens": None, "cost_usd": None, "source": "unknown", "confidence": "none"}
    assert rt.honest_usage({"tokens": "lots", "cost_usd": -1}) == rt.honest_usage(None)
    assert rt.honest_usage({"tokens": 0})["tokens"] == 0  # a measured zero is still a measurement


def test_without_a_transport_the_adapter_still_refuses_to_exist():
    with pytest.raises(NotImplementedError):
        CodexAppServerAdapter()


# ------------------------------------------------------------------------------------------------ stdio transport

class ScriptedProc:
    """A fake child: whatever the transport writes to stdin may trigger scripted lines on stdout."""

    def __init__(self, replies=None, preset=()):
        import queue
        self.out: "queue.Queue" = queue.Queue()
        for line in preset:
            self.out.put(line)
        self.replies = replies or {}
        self.stdin = self
        self.stdout = iter(self.out.get, None)
        self.written, self.pid, self.rc = [], 999, None

    def write(self, s):
        self.written.append(s)
        msg = json.loads(s)
        if "method" in msg and "id" in msg and msg["method"] in self.replies:
            self.out.put(json.dumps({"id": msg["id"], **self.replies[msg["method"]]}) + "\n")

    def flush(self):
        pass

    def poll(self):
        return self.rc


def test_stdio_transport_routes_responses_notifications_and_server_requests():
    proc = ScriptedProc({"turn/start": {"result": {"turn": {"id": "t"}}}, "boom": {"error": {"code": 1}}},
                        preset=["not json\n", json.dumps({"method": "turn/started", "params": {"x": 1}}) + "\n",
                                json.dumps({"id": "s1", "method": "item/commandExecution/requestApproval",
                                            "params": {}}) + "\n"])
    tr = StdioAppServerTransport(["codex", "app-server"], popen=lambda *a, **k: proc)
    tr.start()
    assert tr.request("turn/start", {"a": 1}, timeout=5) == {"turn": {"id": "t"}}
    assert json.loads(proc.written[0]) == {"id": 1, "method": "turn/start", "params": {"a": 1}}
    with pytest.raises(AppServerError, match="boom"):
        tr.request("boom", {}, timeout=5)
    import time
    got = []
    for _ in range(200):
        got += tr.drain()
        if len(got) >= 2:
            break
        time.sleep(0.01)
    assert [m.get("method") for m in got] == ["turn/started", "item/commandExecution/requestApproval"]
    tr.respond("s1", {"decision": "decline"})
    assert json.loads(proc.written[-1]) == {"id": "s1", "result": {"decision": "decline"}}
    tr.respond("s2", error={"code": -1, "message": "no"})
    assert json.loads(proc.written[-1])["error"]["code"] == -1
    tr.notify("initialized")
    assert json.loads(proc.written[-1]) == {"method": "initialized"}
    assert tr.pid() == 999 and tr.alive() is True
    proc.out.put(None)


def test_stdio_transport_timeouts_and_confirmed_tree_kill():
    proc = ScriptedProc()
    tr = StdioAppServerTransport(["codex"], popen=lambda *a, **k: proc, grace_s=0.0)
    tr.start()
    with pytest.raises(AppServerError, match="timed out"):
        tr.request("thread/start", {}, timeout=0.05)
    sent = []
    tr._killpg = lambda pid, sig: (sent.append((pid, sig)), setattr(proc, "rc", -15) if sig == signal.SIGTERM else None)
    assert tr.terminate_tree() is True and sent == [(999, signal.SIGTERM)]
    stubborn = ScriptedProc()
    t2 = StdioAppServerTransport(["codex"], popen=lambda *a, **k: stubborn, grace_s=0.0)
    t2.start()
    t2._killpg = lambda pid, sig: None  # survives both signals
    assert t2.terminate_tree() is False
    assert StdioAppServerTransport(["codex"]).terminate_tree() is True  # never started: nothing to kill
    proc.out.put(None)
    stubborn.out.put(None)


def test_codex_lane_factory_builds_a_real_stdio_adapter_the_controller_can_drive(steward_home, tmp_path):
    """The factory output plugs into the real JobController: spawn -> v2 identity -> confirmed tree kill on stop."""
    procs = []

    def popen(argv, **kw):
        proc = ScriptedProc({"initialize": {"result": {}},
                             "thread/start": {"result": {"thread": {"id": "thX"}, "model": "m2"}},
                             "turn/start": {"result": {"turn": {"id": "turn1"}}},
                             "turn/interrupt": {"result": {}}})
        proc.argv, proc.kw = argv, kw
        procs.append(proc)
        return proc

    w = TL(tmp_path, steward_home, qualify=("claude-code", "codex"))
    for t in ("t1", "t2"):
        w.fs.record_assignment(JOB, rt.lane_id_for(t), roster_id="codex-gpt", actor="policy", reason="r",
                               policy_rule="p")
    factory = codex_lane_factory(["codex", "app-server"],
                                 model_for=lambda job, lane: w.fs.assignments(job, lane)[-1]["model"],
                                 transport_factory=lambda a: StdioAppServerTransport(a, popen=popen, grace_s=0.0))
    w.ctl.factory = factory
    out = w.ctl.admit(JOB)
    assert [r["status"] for r in out] == ["started", "started"], out
    assert all(p.kw["start_new_session"] for p in procs) and procs[0].argv == ["codex", "app-server"]
    lease = fl.get(w.store, JOB, "lane-t1")
    assert lease["identity"]["thread_id"] == "thX" and lease["identity"]["native_model"] == "m2"
    assert lease["identity"]["ref"] == "codex:thX"
    ad = w.ctl.adapter(JOB, "lane-t1")
    assert (ad.lane, ad.attempt, ad.epoch, ad.task) == ("lane-t1", 1, lease["epoch"], "t1")
    w.ctl.probe = lambda job, lane, ident: rt.DEAD   # the process-tree probe sees the group gone
    for adapter_ in w.ctl._adapters.values():        # signalling the group "kills" the fake child
        adapter_.transport._killpg = lambda pid, sig, procs=procs: [setattr(p, "rc", -15) for p in procs]
    res = w.ctl.stop(JOB, "s1")
    assert res["state"] == "stopped" and {r["status"] for r in res["lanes"].values()} == {"confirmed"}
    for p in procs:
        p.out.put(None)


# ------------------------------------------------------------------------------------------------ qualification

def run_probes(server_factory, tmp_path, fs, runtime="codex", **kw):
    made = []

    def make():
        srv = server_factory()
        made.append(srv)
        return CodexAppServerAdapter(srv, model="gpt-x", job_id=JOB, task="t1", lane="lane-t1", attempt=1, epoch=1)

    return rt.qualify_runtime(fs, runtime, "1", codex_capability_probes(make, tmp_path), **kw)


@pytest.fixture
def bare(steward_home, tmp_path):
    w = TL(tmp_path, steward_home, qualify=())
    return w


def test_every_capability_is_probed_and_recorded_independently(bare, tmp_path):
    res = run_probes(FakeServer, tmp_path, bare.fs, roster_capabilities=["edit"])
    assert res == {c: True for c in fr.RUNTIME_CAPABILITIES}
    obs = bare.fs.runtime_capabilities("codex")
    assert set(obs) == set(fr.RUNTIME_CAPABILITIES) and all(o["qualified"] and o["evidence"] for o in obs.values())
    assert bare.fs.qualification("codex")["qualified"] is True
    assert bare.fs.runtime_can("codex", "assign") == (True, []) and bare.fs.runtime_can("codex", "stop") == (True, [])


def test_a_capability_that_cannot_be_demonstrated_stays_unavailable_for_actions_needing_it(bare, tmp_path):
    res = run_probes(lambda: FakeServer(bad_steer=True), tmp_path, bare.fs)
    assert res["steer_ack"] is False and all(v for k, v in res.items() if k != "steer_ack")
    assert bare.fs.runtime_can("codex", "assign") == (True, [])            # dispatch needs none of steering
    assert bare.fs.runtime_can("codex", "steer") == (False, ["steer_ack"])  # steering does
    assert "boom" not in json.dumps(bare.fs.runtime_capabilities("codex"))


def test_unstoppable_runtime_cannot_be_qualified_to_stop_or_dispatch_on_stop_confirm(bare, tmp_path):
    res = run_probes(lambda: FakeServer(tree_ok=False), tmp_path, bare.fs)
    assert res["stop_confirm"] is False
    assert bare.fs.runtime_can("codex", "stop")[1] == ["stop_confirm"] and bare.fs.runtime_can("codex", "recover")[0] is False


def test_a_crashing_probe_is_a_failed_demonstration_not_an_exception(bare):
    res = rt.qualify_runtime(bare.fs, "codex", "1", {"launch": lambda: (_ for _ in ()).throw(RuntimeError("x")),
                                                    "poll": lambda: (True, "ok")})
    assert res == {"launch": False, "poll": True}
    assert "RuntimeError: x" in bare.fs.runtime_capabilities("codex")["launch"]["evidence"]


def test_unknown_capability_names_and_roster_presence_prove_nothing(bare):
    with pytest.raises(fr.RosterError):
        bare.fs.record_runtime_capability("codex", "telepathy", qualified=True, adapter_version="1", evidence="e")
    # roster entry + explicit roster qualification, but no runtime evidence: every action needing a capability fails
    bare.fs.record_qualification("codex", "1", ["edit"], qualified=True, available=True)
    assert bare.fs.runtime_can("codex", "assign")[0] is False and bare.fs.runtime_can(None, "steer")[0] is False
    assert bare.fs.runtime_can("codex", "split") == (True, [])  # pure bookkeeping needs no runtime capability


def test_capability_requirements_are_per_action_and_usage_is_never_required():
    need = fr.ACTION_REQUIREMENTS
    assert "usage" not in set().union(*need.values())
    assert need["steer"] == {"steer_ack"} and {"cancel", "stop_confirm"} == need["stop"]
    assert fr.missing_capabilities(set(), "mystery") == sorted(fr.RUNTIME_CAPABILITIES)


# ------------------------------------------------------------------------------------------------ controller gating

def partial(fs, runtime, caps):
    for c in caps:
        fs.record_runtime_capability(runtime, c, qualified=True, adapter_version="1", evidence="probe")


def test_controller_rechecks_the_specific_capability_on_every_operation(steward_home, tmp_path):
    w = TL(tmp_path, steward_home, qualify=("claude-code",))
    partial(w.fs, "codex", [c for c in ALL_CAPS if c != "steer_ack"])
    w.admit()
    assign = {"type": "assign", "key": "a", "target": "t1", "params": {"roster_id": "codex-gpt"}}
    assert w.apply([assign])["outcome"] == "applied"           # codex may take work ...
    lease = w.lease("t1")
    assert lease["attempt_no"] == 2
    w.clock.t += 1000
    s = w.apply([{"type": "steer", "key": "s", "target": "t1", "params": {"verb": "focus", "text": "x"}}])
    assert s["actions"][0]["reason"] == "capability_unqualified(lane-t1): steer_ack"   # ... but cannot be steered
    assert w.adapter("t1").delivered == []
    # qualification is re-read each time: demonstrate steering later and the same action now passes
    w.fs.record_runtime_capability("codex", "steer_ack", qualified=True, adapter_version="1", evidence="later probe")
    w.clock.t += 1000
    assert w.apply([{"type": "steer", "key": "s2", "target": "t1", "params": {"verb": "focus", "text": "x"}}])["outcome"] == "applied"
    # ... and a later failed observation closes the gate again immediately
    w.fs.record_runtime_capability("codex", "steer_ack", qualified=False, adapter_version="1", evidence="regressed")
    assert w.ctl.capability_check(JOB, "codex", "steer", force=True) == ["steer_ack"]
    assert any(e["what"] == "capability_denied" for e in w.fs.fleet_events(JOB))


def test_recovery_of_a_lane_whose_runtime_cannot_confirm_stop_is_blocked_before_any_spend(steward_home, tmp_path):
    w = TL(tmp_path, steward_home, qualify=("claude-code",))
    partial(w.fs, "codex", [c for c in ALL_CAPS if c != "stop_confirm"])
    w.fs.record_assignment(JOB, "lane-t1", roster_id="codex-gpt", actor="policy", reason="r", policy_rule="p")
    w.admit()
    res = w.ctl.recover_lane(JOB, "lane-t1", "rec-1", reason="stalled")
    assert res["status"] == "blocked" and res["reason"] == "capability_unqualified: stop_confirm"
    assert w.lease("t1")["state"] == "running" and not w.adapter("t1").stopped
    assert fl.recoveries(w.store, JOB)["job"] == 0   # no recovery budget was spent


def test_governed_runtime_is_not_dispatched_until_its_dispatch_capabilities_are_qualified(steward_home, tmp_path):
    w = TL(tmp_path, steward_home, qualify=("claude-code",))
    partial(w.fs, "codex", ["launch", "poll"])   # evidence exists, so the runtime is governed, but incomplete
    w.fs.record_assignment(JOB, "lane-t1", roster_id="codex-gpt", actor="policy", reason="r", policy_rule="p")
    w.fs.record_assignment(JOB, "lane-t2", roster_id="claude-sonnet", actor="policy", reason="r", policy_rule="p")
    res = {r["task"]: r for r in w.ctl.admit(JOB)}
    assert res["t1"]["status"] == "skipped" and res["t1"]["reason"].startswith("capability_unqualified: ")
    assert "identity" in res["t1"]["reason"] and res["t2"]["status"] == "started"
    assert fl.get(w.store, JOB, "lane-t1") is None   # nothing was spawned for the unqualified runtime


def test_job_level_gate_governs_runtimes_with_no_evidence_at_all(steward_home, tmp_path):
    w = TL(tmp_path, steward_home, qualify=())
    w.fs.record_assignment(JOB, "lane-t1", roster_id="claude-sonnet", actor="policy", reason="r", policy_rule="p")
    w.fs.record_assignment(JOB, "lane-t2", roster_id="claude-sonnet", actor="policy", reason="r", policy_rule="p")
    assert [r["status"] for r in w.ctl.admit(JOB)] == ["started", "started"]  # legacy: no gate, no evidence
    (tmp_path / "b").mkdir()
    (steward_home / "b").mkdir()
    w2 = TL(tmp_path / "b", steward_home / "b", qualify=())
    rt.require_capabilities(w2.store, JOB)
    w2.fs.record_assignment(JOB, "lane-t1", roster_id="claude-sonnet", actor="policy", reason="r", policy_rule="p")
    assert w2.ctl.admit(JOB)[0]["reason"].startswith("capability_unqualified")


def test_pins_and_snapshot_survive_roster_and_qualification_changes(steward_home, tmp_path):
    w = TL(tmp_path, steward_home, pins={"lane-t1": "claude-sonnet"})
    snap_before = w.fs.get_snapshot(JOB)
    w.fs.record_roster(fr.validate_roster({"schema_version": 1, "revision": 2, "entries": [
        entry("codex-gpt", "codex", "other-model")]}))
    w.fs.record_runtime_capability("claude-code", "steer_ack", qualified=False, adapter_version="2", evidence="x")
    assert w.fs.get_snapshot(JOB) == snap_before and snap_before["pins"] == {"lane-t1": "claude-sonnet"}
    r = w.apply([{"type": "assign", "key": "a", "target": "t1", "params": {"roster_id": "codex-gpt"}}])
    assert r["actions"][0]["reason"] == "pinned"


def test_unknown_usage_is_recorded_null_and_never_proves_a_cost_cap(steward_home, tmp_path):
    w = TL(tmp_path, steward_home, limits={"max_cost_usd": 5.0, "max_tokens": 1000})
    w.admit()
    w.adapter("t1").usage = lambda: {"tokens": 400, "source": "codex_app_server"}   # tokens known, cost unknown
    w.ctl.stop(JOB, "s1")
    ev = [json.loads(e["evidence"]) for e in w.fs.fleet_events(JOB) if e["what"] == "lane_usage"]
    assert len(ev) == 2
    u = {e["usage"]["tokens"]: e["usage"] for e in ev}
    assert u[400] == {"tokens": 400, "cost_usd": None, "source": "codex_app_server", "confidence": "reported"}
    assert u[None] == {"tokens": None, "cost_usd": None, "source": "unknown", "confidence": "none"}
    summ = w.ctl.usage_summary(JOB)
    assert summ["tokens"] == 400 and summ["cost_usd"] is None and summ["complete"] is False
    assert summ["limits"]["max_cost_usd"] == {"status": "unverifiable", "enforced": False, "used": None, "cap": 5.0}
    assert summ["limits"]["max_tokens"]["status"] == "within" and summ["limits"]["max_tokens"]["enforced"] is True
    assert rt.limit_status({"max_tokens": 100}, {"tokens": 101})["max_tokens"]["status"] == "exceeded"
    assert "met" not in json.dumps(summ["limits"])


# ------------------------------------------------------------------------------------------------ deadline range

def seed_history(store, job, task_id, runtime, minutes, *, t0=NOW - timedelta(hours=5), sha="a" * 40, rid="m"):
    """An integrated task with an observed duration, written straight into the real fleet tables."""
    lane = rt.lane_id_for(task_id)
    fs = rt.FleetStore(store)
    if not store._conn.execute("SELECT 1 FROM fleet_lanes WHERE job_id=? AND lane_id=?", (job, lane)).fetchone():
        fs.record_lane(job, lane, task_id=task_id)
    store._conn.execute("INSERT INTO fleet_attempts (job_id, lane_id, attempt_no, provider, runtime, model,"
                        " adapter_version, herdr_ref, started_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (job, lane, 1, "p", runtime, rid, "1", None, t0.isoformat()))
    store._conn.execute("INSERT INTO fleet_candidates (job_id, task_id, graph_rev, lane_id, attempt_no, epoch,"
                        " candidate_sha, evidence_ref, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (job, task_id, 1, lane, 1, 1, sha, "ev", (t0 + timedelta(minutes=minutes)).isoformat()))
    store._conn.commit()
    cand = fs.latest_candidate(job, task_id)
    fs.record_integration_attempt(job, idem_key=f"k-{task_id}", cand=cand, before_sha="0" * 40, output_sha=sha,
                                  changed_paths=[], conflict_paths=[], outcome="integrated", reason="", writer_epoch=1)


def world(tmp_path, home, tasks, **kw):
    return TL(tmp_path, home, tasks=tasks, **kw)


def indep(n, **over):
    return [task(f"i{k}", owns=[f"src/p{k}/"], **over) for k in range(n)]


def history_tasks():
    """Two finished-and-integrated tasks (20 and 30 min on claude-code) plus whatever the test adds."""
    return [task("h1", owns=["src/h1/"]), task("h2", owns=["src/h2/"])]


def seeded(tmp_path, home, extra, *, runtimes=("claude-code", "claude-code"), minutes=(20, 30), **kw):
    w = world(tmp_path, home, history_tasks() + extra, **kw)
    for tid, run, mins in zip(("h1", "h2"), runtimes, minutes):
        seed_history(w.store, JOB, tid, run, mins, sha=f"{tid[1]}" * 40)
    for t in ("h1", "h2"):
        w.fs.record_assignment(JOB, rt.lane_id_for(t), roster_id="claude-sonnet", actor="policy", reason="r",
                               policy_rule="p")
    return w


def test_too_few_comparable_samples_means_estimating_with_no_numbers(steward_home, tmp_path):
    w = world(tmp_path, steward_home, indep(2))
    f = fd.forecast(w.store, JOB, now=NOW)
    assert f["status"] == "estimating" and f["range_min"] is None and f["comparable_samples"] == 0
    assert f["reasons"] and f["reasons"][0].startswith("latency_unknown")
    seed_history(w.store, JOB, "i0", "claude-code", 20)
    assert fd.forecast(w.store, JOB, now=NOW)["status"] == "estimating"   # one sample is still not a range


def test_range_covers_critical_path_capacity_queue_serial_integration_and_tail(steward_home, tmp_path):
    w = seeded(tmp_path, steward_home, [task("a", owns=["src/a/"]), task("b", owns=["src/b/"], depends_on=["a"]),
                                         task("c", owns=["src/c/"], depends_on=["b"])], max_lanes=2)
    f = fd.forecast(w.store, JOB, now=NOW, tail_min=6.0, integration_min=2.0)
    assert f["status"] == "range" and f["comparable_samples"] == 2 and f["verification_tail_min"] == 6.0
    p10, p50, p90 = (f["range_min"][k] for k in ("p10", "p50", "p90"))
    assert p10 < p50 < p90
    cp = f["critical_path_min"]["p50"]
    assert cp == pytest.approx(3 * 25.0, abs=0.01)                      # a -> b -> c, mean of 20 and 30 each
    assert p50 >= cp + 3 * 2.0 + 6.0 - 0.01                            # + one serial integration per link + tail
    assert f["remaining_tasks"] == ["a", "b", "c"] and f["integrated_tasks"] == ["h1", "h2"]
    # a second lane cannot speed up a pure chain
    assert fd.forecast(w.store, JOB, now=NOW, lanes=1, tail_min=6.0, integration_min=2.0)["range_min"]["p50"] == pytest.approx(p50)


def test_parallel_work_is_not_summed_as_sequential_nor_given_perfect_speedup(steward_home, tmp_path):
    w = seeded(tmp_path, steward_home, indep(4), max_lanes=2)
    two = fd.forecast(w.store, JOB, now=NOW, lanes=2, tail_min=6.0, integration_min=2.0)
    one = fd.forecast(w.store, JOB, now=NOW, lanes=1, tail_min=6.0, integration_min=2.0)
    four = fd.forecast(w.store, JOB, now=NOW, lanes=4, tail_min=6.0, integration_min=2.0)
    work = two["capacity"]["work_min"]["p50"]
    assert work == pytest.approx(100.0)
    assert one["range_min"]["p50"] > two["range_min"]["p50"] > four["range_min"]["p50"]
    assert two["range_min"]["p50"] < work + 6.0 + 8.0                       # clearly not sequential
    assert two["range_min"]["p50"] >= work / 2 + 6.0                        # but never perfect speedup
    assert four["range_min"]["p50"] > four["critical_path_min"]["p50"] + 6.0  # serial integration/queue still costs
    assert two["capacity"]["bound_min"]["p50"] == pytest.approx(50.0)


def test_serial_integration_dominates_when_merges_are_slow(steward_home, tmp_path):
    w = seeded(tmp_path, steward_home, indep(4), max_lanes=4)
    fast = fd.forecast(w.store, JOB, now=NOW, tail_min=0.0, integration_min=1.0)["range_min"]["p50"]
    slow = fd.forecast(w.store, JOB, now=NOW, tail_min=0.0, integration_min=40.0)["range_min"]["p50"]
    assert slow >= 4 * 40.0 and slow > fast + 100


def test_verification_tail_defaults_to_the_existing_eta_tail(steward_home, tmp_path):
    w = seeded(tmp_path, steward_home, indep(1))
    f = fd.forecast(w.store, JOB, now=NOW)
    assert f["verification_tail_min"] == deadline_plan.verification_tail_min()
    base = f["range_min"]["p50"]
    assert fd.forecast(w.store, JOB, now=NOW, tail_min=deadline_plan.verification_tail_min() + 10)["range_min"]["p50"] \
        == pytest.approx(base + 10)


def test_a_runtime_with_no_history_of_its_own_stays_unknown(steward_home, tmp_path):
    w = seeded(tmp_path, steward_home, indep(2))
    f = fd.forecast(w.store, JOB, now=NOW)
    assert f["status"] == "range"
    w.fs.record_assignment(JOB, "lane-i0", roster_id="codex-gpt", actor="tech_lead", reason="r", policy_rule="p")
    f = fd.forecast(w.store, JOB, now=NOW)
    assert f["status"] == "estimating" and f["range_min"] is None
    assert any(r.startswith("latency_unknown: codex has 0 comparable sample(s)") for r in f["reasons"])


def test_running_work_counts_only_what_is_left_and_finished_work_waits_only_for_integration(steward_home, tmp_path):
    w = seeded(tmp_path, steward_home, [task("a", owns=["src/a/"])], max_lanes=1)
    w.fs.record_assignment(JOB, "lane-a", roster_id="claude-sonnet", actor="policy", reason="r", policy_rule="p")
    rt.require_integration(w.store, JOB)
    for t in ("h1", "h2"):
        fl.force_task_state(w.store, JOB, t, "finished")   # the seeded history is finished AND integrated
    cold = fd.forecast(w.store, JOB, now=NOW, tail_min=0.0, integration_min=2.0)["range_min"]["p50"]
    assert cold == pytest.approx(25.0 + 2.0, abs=0.01)
    w.admit()
    assert w.lease("a")["state"] == "running"
    began = datetime.fromisoformat(w.store._conn.execute(
        "SELECT started_at FROM fleet_attempts WHERE lane_id='lane-a'").fetchone()[0])
    warm = fd.forecast(w.store, JOB, now=began + timedelta(minutes=20), tail_min=0.0, integration_min=2.0)["range_min"]["p50"]
    assert warm == pytest.approx(25.0 - 20.0 + 2.0, abs=0.01) and warm < cold
    over = fd.forecast(w.store, JOB, now=began + timedelta(minutes=500), tail_min=0.0, integration_min=2.0)["range_min"]["p50"]
    assert over == pytest.approx(0.2 * 25.0 + 2.0, abs=0.01)   # an overrunning task keeps a floor, never zero
    # task finished (done frame) but not integrated: only the serial integration remains
    lease = w.lease("a")
    fl.set_task_state(w.store, JOB, "lane-a", lease["epoch"], lease["attempt_no"], "a", "finished")
    done = fd.forecast(w.store, JOB, now=began, tail_min=0.0, integration_min=2.0)["range_min"]["p50"]
    assert done == pytest.approx(2.0, abs=0.01)


def test_nothing_remaining_leaves_only_the_verification_tail(steward_home, tmp_path):
    w = world(tmp_path, steward_home, history_tasks())
    seed_history(w.store, JOB, "h1", "claude-code", 20, sha="1" * 40)
    seed_history(w.store, JOB, "h2", "claude-code", 30, sha="2" * 40)
    f = fd.forecast(w.store, JOB, now=NOW, tail_min=6.0)
    assert f["range_min"] == {"p10": 6.0, "p50": 6.0, "p90": 6.0} and f["remaining_tasks"] == []


def test_held_or_blocked_graphs_do_not_pretend_to_finish(steward_home, tmp_path):
    w = seeded(tmp_path, steward_home, [task("a", owns=["src/a/"])])
    f = fd.forecast(w.store, JOB, now=NOW, held=("a",))
    assert f["status"] == "estimating" and f["reasons"] == ["integration_held: a"]


def test_implicit_graph_is_not_a_fleet_forecast(steward_home, tmp_path):
    from fixtures.repos import python_repo
    from steward.store import Store
    store = Store(steward_home)
    store.create_job("goal", str(python_repo(tmp_path, "r")), job_id="L1")
    assert fd.forecast(store, "L1")["status"] == "not_fleet"


# ------------------------------------------------------------------------------------------------ milestones / slots

def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_milestones_require_integrated_tasks_at_resolvable_shas(steward_home, tmp_path):
    w = TL(tmp_path, steward_home)
    w.admit()
    sha = w.finish("t1", {"src/a/a.py": "a\n"})
    resolve = fd.git_resolver(SubprocessRunner(), w.repo)
    mt = {"m1": ["t1"], "m2": ["t1", "t2"]}
    # lane commit exists and was admitted as a candidate, but nothing is integrated yet: NOT evidence
    facts = fd.milestone_facts(w.store, JOB, mt, resolve)
    assert facts["m1"]["met"] is False and facts["m1"]["missing"] == ["t1"] and facts["m1"]["met_sha"] is None
    w.writer.integrate(JOB)
    facts = fd.milestone_facts(w.store, JOB, mt, resolve)
    integ_sha = w.fs.integrated_tasks(JOB)["t1"]
    assert facts["m1"]["met"] and facts["m1"]["met_sha"] == integ_sha != sha and facts["m1"]["met_at"]
    assert facts["m2"]["met"] is False and facts["m2"]["missing"] == ["t2"]    # one of two tasks is not a milestone
    # an unresolvable SHA never counts, even when the ledger says integrated
    nope = fd.milestone_facts(w.store, JOB, mt, lambda s: False)
    assert nope["m1"]["met"] is False and nope["m1"]["unresolved"] == ["t1"]
    assert resolve(integ_sha) and not resolve("f" * 40) and not resolve("")
    # empty milestones are never met
    assert fd.milestone_facts(w.store, JOB, {"m3": []}, resolve)["m3"]["met"] is False


def adopt_plan(store, rev=1, target="2026-10-03T18:00:00Z"):
    plan = {"commitment_rev": 1, "source": "worker",
            "milestones": [{"id": "m1", "title": "first", "target_at_utc": target, "items": ["t1"], "evidence": "commit"}]}
    return deadline_plan.adopt(store, JOB, plan, sha=f"sha{rev}", raw="{}", source="worker", at="2026-10-03T10:00:00Z")


def test_graph_revisions_never_move_the_original_target_slots_and_the_projection_writes_nothing(steward_home, tmp_path):
    w = TL(tmp_path, steward_home, max_lanes=1)
    adopt_plan(w.store)
    before = fd.target_slots(w.store, JOB)
    assert [s["key"] for s in before] == ["1:m1:2026-10-03T18:00:00Z"]
    kv_before = w.store._conn.execute("SELECT job_id, key, value_json FROM kv ORDER BY job_id, key").fetchall()
    ev_before = w.store._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    proj = fd.projection(w.store, JOB, resolve=lambda s: True, now=NOW)
    assert proj["slots"] == before and proj["milestones"]["m1"]["met"] is False
    assert w.store._conn.execute("SELECT job_id, key, value_json FROM kv ORDER BY job_id, key").fetchall() == kv_before
    assert w.store._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == ev_before
    # revise the fleet graph (a reorder) and adopt a later plan revision with a moved target: old slot still there
    assert w.apply([{"type": "reorder", "key": "r", "params": {"order": ["t2", "t1"]}}])["outcome"] == "applied"
    assert w.fs.current_graph(JOB)[0] == 2
    adopt_plan(w.store, rev=2, target="2026-10-03T20:00:00Z")
    after = fd.target_slots(w.store, JOB)
    assert after[0] == before[0] and len(after) == 2 and {s["target_at_utc"] for s in after} == {
        "2026-10-03T18:00:00Z", "2026-10-03T20:00:00Z"}
    proj["slots"][0]["target_at_utc"] = "tampered"
    assert fd.target_slots(w.store, JOB)[0] == before[0]   # callers get copies


def test_plan_milestones_map_to_graph_tasks_by_item_id(steward_home, tmp_path):
    w = TL(tmp_path, steward_home)
    adopt_plan(w.store)
    assert fd.plan_milestone_tasks(w.store, JOB) == {"m1": ["t1"]}


def test_tech_lead_packet_carries_the_fleet_range_or_its_unknown(steward_home, tmp_path):
    from steward import fleet_tech_lead as tl
    w = seeded(tmp_path, steward_home, indep(1))
    pkts = []
    sup = tl.TechLeadSupervisor(w.store, w.ctl, lambda p: pkts.append(p), clock=w.clock)
    sup.on_event(JOB)
    assert pkts[0]["deadline"]["status"] == "range" and pkts[0]["deadline"]["range_min"]["p50"] > 0
    assert pkts[0]["usage"]["tokens"] is None
