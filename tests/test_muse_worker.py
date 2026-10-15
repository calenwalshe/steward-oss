"""MuseServeAdapter over a wire-level fake `muse serve` (MSP JSON-RPC). Never spawns anything.

The fake models the verified recon (PRD §6 + live traces 2026-10-04): it rejects messages without `"jsonrpc": "2.0"`,
commands without a UUIDv7 `commandId`, a session/start approvalMode other than `promptUnmatched`; it asks approvals as
the server request `approval/request` offering `allow_once`/`abort` (no "denied" choice) and rejects an interrupt that
lands before the run exists with `missing_run`."""

from __future__ import annotations

import json
import queue
import subprocess

import pytest

from steward import fleet_qualify as fq
from steward import fleet_registry as fr
from steward.fleet_store import FleetStore
from steward.muse_worker import (MUSE_APPROVAL_MODE, MuseServeAdapter, is_uuid7, muse_capability_probes, muse_env,
                                 muse_transport, uuid7)
from steward.store import Store
from steward.worker import PROBE_BRIEF, PROBE_LONG_BRIEF, StdioAppServerTransport

SESSIONS: dict = {}   # sessionId -> model (durable across processes, like muse's session log)


class FakeMuse:
    """The server half. `handle(msg)` returns the lines it writes back."""

    def __init__(self, *, default_model="muse-spark-1.3-contributor", missing_run_first=0, auto=True,
                 approval_kind="shell", stages=1, updated_as="notification", drifted=False):
        self.default_model, self.missing_run, self.auto, self.approval_kind = default_model, missing_run_first, auto, approval_kind
        self.stages, self.updated_as = stages, updated_as   # live: a compound shell command is one stage per part
        # live 2026-10-04 (Muse Code 1.4.2, drifted hosted model): every turn first asks Bash permission for
        # `ls`/`git status`, even for a no-tools brief; denied, it answers and completes the turn
        self.drifted = drifted
        self.initialized = False
        self.session = None
        self.turn = None          # active turn id
        self.turn_n = 0
        self.received, self.responses, self.decisions, self.cancelled_inputs = [], [], [], []
        self.req_n = 100

    def _err(self, mid, code, message, **data):
        return [{"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message, "data": data}}]

    def _ok(self, mid, result):
        return [{"jsonrpc": "2.0", "id": mid, "result": result}]

    def _note(self, method, params):
        return {"jsonrpc": "2.0", "method": method, "params": params}

    def handle(self, msg):
        self.received.append(msg)
        mid, method, p = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if msg.get("jsonrpc") != "2.0":
            return self._err(mid, -32600, "invalid request: missing jsonrpc") if mid is not None else []
        if method is None:  # a response to one of our server requests
            self.responses.append(msg)
            return []
        if mid is None:
            return []  # notifications (initialized)
        if method == "initialize":
            self.initialized = True
            return self._ok(mid, {"serverInfo": {"name": "muse", "version": "1.4.2"}})
        if not self.initialized:
            return self._err(mid, -32600, "not initialized", kind="notInitialized")
        if not is_uuid7(p.get("commandId")):
            return self._err(mid, -32602, "commandId must be a UUIDv7")
        if method == "session/start":
            if p.get("approvalMode") != MUSE_APPROVAL_MODE:
                return self._err(mid, -32030, "rejected", kind="commandRejected", reason="approval_mode_ceiling")
            sid = uuid7()
            model = p.get("modelId") or self.default_model
            SESSIONS[sid] = model
            self.session = sid
            return self._ok(mid, {"session": {"sessionId": sid, "modelId": model, "activeTurnId": None}})
        if method == "session/resume":
            if p.get("sessionId") not in SESSIONS:
                return self._err(mid, -32020, "session not found")
            self.session = p["sessionId"]
            return self._ok(mid, {"session": {"sessionId": p["sessionId"], "modelId": SESSIONS[p["sessionId"]],
                                              "activeTurnId": None}, "history": {}, "pendingRequests": []})
        if method == "turn/start":
            self.turn_n += 1
            tid = uuid7()
            self.turn = tid
            out = self._ok(mid, {"commandId": p["commandId"], "turnId": tid, "status": "accepted",
                                 "disposition": "started", "startedNewTurn": True})
            text = p["input"][0]["text"]
            if self.auto and text != PROBE_LONG_BRIEF:
                out.append(self._note("turn/started", {"turnId": tid, "sessionId": self.session}))
                if self.drifted and "approval" not in text:
                    self.req_n += 1
                    out.append({"jsonrpc": "2.0", "id": self.req_n, "method": "approval/request", "params": {
                        "approvalId": f"drift{self.req_n}", "currentRequirementId": {"approvalId": f"drift{self.req_n}",
                                                                                     "sourceIndex": 0},
                        "availableChoices": [
                            {"choiceId": "allow_once", "decision": "approved", "scope": "once", "label": "Allow once"},
                            {"choiceId": "abort", "decision": "abort", "scope": "once", "label": "Reject"}],
                        "subject": {"kind": "shell", "command": "ls; git status"}, "sessionId": self.session,
                        "turnId": tid, "toolName": "bash"}})
                elif "approval" in text:
                    self.req_n += 1
                    subject = ({"kind": "shell", "command": "echo steward-probe" if self.stages == 1
                                else "pwd; ls -la"} if self.approval_kind == "shell" else {"kind": "mystery"})
                    out.append({"jsonrpc": "2.0", "id": self.req_n, "method": "approval/request", "params": {
                        "approvalId": "ap1", "currentRequirementId": {"approvalId": "ap1", "sourceIndex": 0},
                        "availableChoices": [
                            {"choiceId": "allow_once", "decision": "approved", "scope": "once", "label": "Allow once"},
                            {"choiceId": "allow_local_prefix", "decision": "approvedPolicyAmendment",
                             "scope": "localPersistent", "label": "Always"},
                            {"choiceId": "abort", "decision": "abort", "scope": "once", "label": "Reject",
                             "acceptsFeedback": True}],
                        "subject": subject, "sessionId": self.session, "turnId": tid, "toolName": "bash"}})
                else:
                    out.append(self._note("session/tokenUsage", {"cumulative": {"totalTokens": 34303,
                                                                                "promptTokens": 34199,
                                                                                "outputTokens": 104}}))
                    out.append(self._note("turn/completed", {"turnId": tid, "terminal": "completed"}))
                    self.turn = None
            return out
        if method == "turn/steer":
            if p.get("expectedTurnId") != self.turn:
                return self._err(mid, -32030, "rejected", kind="commandRejected", reason="turn_mismatch")
            return self._ok(mid, {"commandId": p["commandId"], "status": "accepted", "turnId": self.turn})
        if method == "turn/interrupt":
            if self.missing_run > 0:
                self.missing_run -= 1
                return self._err(mid, -32030, "turn/interrupt rejected: missing_run", kind="commandRejected",
                                 reason="missing_run")
            tid, self.turn = self.turn, None
            return [*self._ok(mid, {"commandId": p["commandId"], "status": "accepted", "turnId": tid}),
                    self._note("turn/completed", {"turnId": tid, "terminal": "cancelled"})]
        if method == "approval/decide":
            self.decisions.append(p)
            idx = (p.get("requirementId") or {}).get("sourceIndex", 0)
            if p["choiceId"] == "allow_once" and idx + 1 < self.stages:   # more stages remain (terminal: false)
                nxt = {"approvalId": p["approvalId"], "sourceIndex": idx + 1}
                params = {"approvalId": p["approvalId"], "currentRequirementId": nxt,
                          "availableChoices": [
                              {"choiceId": "allow_once", "decision": "approved", "scope": "once", "label": "Allow"},
                              {"choiceId": "abort", "decision": "abort", "scope": "once", "label": "Reject"}],
                          "subject": {"kind": "shell", "command": "pwd; ls -la"}, "sessionId": self.session,
                          "change": {"kind": "stageResolved"}}
                upd = ({"jsonrpc": "2.0", "method": "approval/updated", "params": params}
                       if self.updated_as == "notification" else
                       {"jsonrpc": "2.0", "id": 900 + idx, "method": "approval/request", "params": params})
                return [*self._ok(mid, {"approvalId": p["approvalId"], "status": "accepted", "terminal": False}), upd]
            if self.turn and (p["choiceId"] == "allow_once" or str(p["approvalId"]).startswith("drift")):
                tid, self.turn = self.turn, None
                if p["choiceId"] == "abort":   # denied: the model answers without tools and the turn completes
                    return [*self._ok(mid, {"approvalId": p["approvalId"], "status": "accepted", "terminal": True}),
                            self._note("session/tokenUsage", {"cumulative": {"totalTokens": 34303}}),
                            self._note("turn/completed", {"turnId": tid, "terminal": "completed"})]
                return [*self._ok(mid, {"approvalId": p["approvalId"], "status": "accepted", "terminal": True}),
                        self._note("turn/completed", {"turnId": tid, "terminal": "completed"})]
            return self._ok(mid, {"approvalId": p["approvalId"], "status": "accepted", "terminal": True})
        if method == "userInput/cancel":
            self.cancelled_inputs.append(p)
            return self._ok(mid, {"status": "accepted"})
        return self._err(mid, -32601, "method not found")


class _In:
    def __init__(self, proc):
        self.proc = proc

    def write(self, line):
        for raw in line.splitlines():
            for out in self.proc.server.handle(json.loads(raw)):
                self.proc.out.put(json.dumps(out) + "\n")

    def flush(self):
        pass


class FakeProc:
    PID = 6000

    def __init__(self, server):
        FakeProc.PID += 1
        self.pid, self.server, self.returncode = FakeProc.PID, server, None
        self.out: "queue.Queue[str | None]" = queue.Queue()
        self.stdin = _In(self)
        self.stdout = iter(self.out.get, None)

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9
        self.out.put(None)


def wire(server=None, **kw):
    """A real StdioAppServerTransport (jsonrpc envelope) whose process is the fake."""
    server = server or FakeMuse(**kw)
    procs, seen = {}, {}

    def popen(argv, **pkw):
        seen.update(argv=argv, **pkw)
        proc = FakeProc(server)
        procs[proc.pid] = proc
        return proc

    def killpg(pid, sig):
        procs[pid].kill()

    t = StdioAppServerTransport(["muse", "serve"], jsonrpc=True, env=muse_env(), popen=popen, killpg=killpg,
                                grace_s=0.5)
    return t, server, seen


def adapter(**kw):
    model = kw.pop("model", None)
    t, srv, seen = wire(**kw)
    return MuseServeAdapter(t, model=model), srv, seen


def pump(a, n=40, pred=lambda e: True):
    import time
    out = []
    for _ in range(n):
        out += [e for e in a.observe().events if pred(e)]
        if out:
            return out
        time.sleep(0.01)
    return out


# ------------------------------------------------------------------------------------------------------- wire format

def test_uuid7_is_a_real_version_7_uuid():
    ids = {uuid7() for _ in range(50)}
    assert len(ids) == 50 and all(is_uuid7(i) for i in ids)
    assert not is_uuid7("01a1047b-aca3-4180-8239-d1cf3f8f036c") and not is_uuid7("nope")


def test_every_message_carries_the_jsonrpc_envelope_and_commands_carry_uuid7(tmp_path):
    a, srv, _ = adapter()
    a.start("probe", tmp_path)
    sent = [m for m in srv.received]
    assert sent and all(m.get("jsonrpc") == "2.0" for m in sent)
    cmds = [m for m in sent if m.get("method") not in ("initialize", "initialized", None)]
    assert cmds and all(is_uuid7(m["params"]["commandId"]) for m in cmds)
    a.stop()


def test_the_fake_rejects_what_real_muse_rejects():
    srv = FakeMuse()
    assert "error" in srv.handle({"id": 1, "method": "initialize", "params": {}})[0]          # no jsonrpc
    srv.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})
    bad_id = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "session/start",
                         "params": {"commandId": "1", "approvalMode": MUSE_APPROVAL_MODE}})
    assert "error" in bad_id[0]
    on_req = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "session/start",
                         "params": {"commandId": uuid7(), "approvalMode": "onRequest"}})
    assert on_req[0]["error"]["data"]["reason"] == "approval_mode_ceiling"


def test_session_starts_in_prompt_unmatched_in_the_worktree_and_never_sees_meta_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("META_API_KEY", "sk-should-never-reach-muse")
    a, srv, seen = adapter()
    ref = a.start("probe", tmp_path)
    start = next(m for m in srv.received if m.get("method") == "session/start")["params"]
    assert start["approvalMode"] == "promptUnmatched" and start["workspaceRoot"] == str(tmp_path.resolve())
    assert "META_API_KEY" not in seen["env"] and seen["start_new_session"] is True
    assert ref == f"muse:{a.session_id}"
    ident = a.process_identity()
    assert ident["native_model"] == "muse-spark-1.3-contributor" and ident["pid"] is not None
    a.stop()


def test_muse_transport_strips_meta_api_key(monkeypatch):
    monkeypatch.setenv("META_API_KEY", "sk-x")
    t = muse_transport()
    assert t.jsonrpc is True and "META_API_KEY" not in t.env and t.argv[:2] == ["muse", "serve"]


def test_model_mismatch_fails_the_start_and_stops_the_process(tmp_path):
    a, srv, _ = adapter(default_model="muse-other")
    a.model = "muse-spark-1.3-contributor"
    srv.default_model = "muse-other"
    # the fake honours modelId; make it lie like a server that silently routes elsewhere
    orig = srv.handle

    def lying(msg):
        out = orig(msg)
        if msg.get("method") == "session/start" and out and "result" in out[0]:
            out[0]["result"]["session"]["modelId"] = "muse-other"
        return out
    srv.handle = lying
    with pytest.raises(Exception, match="model_identity_mismatch"):
        a.start("probe", tmp_path)
    assert not a.transport.alive()


# ------------------------------------------------------------------------------------------------------- lifecycle

def test_done_frame_and_reported_usage(tmp_path):
    a, _, _ = adapter()
    a.start("probe", tmp_path)
    hit = pump(a, pred=lambda e: e.type in ("done", "error"))
    assert [e.type for e in hit] == ["done"] and hit[0].body["status"] == "completed"
    assert a.usage() == {"tokens": 34303, "cost_usd": None, "source": "muse_msp", "confidence": "reported"}
    a.stop()


def test_cost_is_reported_only_when_complete():
    a = MuseServeAdapter(object())
    a._usage = {"totalTokens": 10, "cost": {"usd": 0.5, "partial": True}}
    assert a.usage()["cost_usd"] is None
    a._usage = {"totalTokens": 10, "cost": {"usd": 0.5, "partial": False}}
    assert a.usage()["cost_usd"] == 0.5
    a._usage = None
    assert a.usage() is None


def test_steer_is_acknowledged_against_the_expected_turn(tmp_path):
    a, srv, _ = adapter(auto=False)
    a.start("long job", tmp_path)
    r = a.deliver("[rev=3] change course")
    assert r.ok
    acks = pump(a, pred=lambda e: e.type == "ack")
    assert acks and acks[0].body["rev"] == 3
    steer = next(m for m in srv.received if m.get("method") == "turn/steer")["params"]
    assert steer["expectedTurnId"] == a.turn_id
    a.stop()


def test_approval_request_is_receipted_relayed_and_decided_with_the_once_choice(tmp_path):
    a, srv, _ = adapter()
    a.start("Run it and wait for approval", tmp_path)
    reqs = pump(a, pred=lambda e: e.type == "permission_request")
    assert reqs[0].body == {"request_id": "ap1", "tool": "Bash", "input": {"command": "echo steward-probe"}}
    assert any(r.get("id") == 101 and r.get("result") == {} for r in srv.responses)  # the receipt, at once
    assert not srv.decisions  # the adapter never decides by itself
    assert a.answer_prompt(True, "ap1").ok
    assert srv.decisions[-1]["choiceId"] == "allow_once" and srv.decisions[-1]["requirementId"]["approvalId"] == "ap1"
    a.stop()


def test_deny_uses_the_abort_choice_when_muse_offers_no_denied_choice(tmp_path):
    """Live 2026-10-04: muse 1.4.2 offered allow_once / allow_local_prefix / abort - no 'denied' decision."""
    a, srv, _ = adapter()
    a.start("Run it and wait for approval", tmp_path)
    pump(a, pred=lambda e: e.type == "permission_request")
    r = a.answer_prompt(False, "ap1", message="not authorized for a fleet lane")
    assert r.ok and r.detail == "denied"
    assert srv.decisions[-1]["choiceId"] == "abort" and srv.decisions[-1]["feedback"]
    a.stop()


def test_an_inexpressible_approval_is_denied_never_relayed_as_consent(tmp_path):
    a, srv, _ = adapter(approval_kind="mystery")
    a.start("Run it and wait for approval", tmp_path)
    for _ in range(5):
        assert not [e for e in a.observe().events if e.type == "permission_request"]
    assert srv.decisions and srv.decisions[-1]["choiceId"] == "abort"
    a.stop()


def test_user_input_requests_are_cancelled_not_answered(tmp_path):
    a, srv, _ = adapter(auto=False)
    a.start("long job", tmp_path)
    a.transport._inbox.put({"jsonrpc": "2.0", "id": 9, "method": "userInput/request",
                            "params": {"userInputId": "ui1", "questions": []}})
    a.observe()
    assert any(r.get("id") == 9 for r in srv.responses)
    import time
    for _ in range(50):
        if srv.cancelled_inputs:
            break
        time.sleep(0.01)
    assert srv.cancelled_inputs[0]["userInputId"] == "ui1" and a.refused_user_input == 1
    a.stop()


def test_an_interrupt_rejected_missing_run_is_resent_until_the_turn_ends(tmp_path):
    """Live 2026-10-04: `turn/interrupt` before the run exists is rejected `missing_run` (commandRejected)."""
    a, srv, _ = adapter(auto=False, missing_run_first=1)
    a.start("long job", tmp_path)
    a.interrupt()
    hit = pump(a, pred=lambda e: e.type == "error")
    assert hit and hit[0].body["status"] == "cancelled"
    n = [m.get("method") for m in srv.received].count("turn/interrupt")
    assert n == 2
    a.observe()
    assert [m.get("method") for m in srv.received].count("turn/interrupt") == n
    a.stop()


def test_stop_kills_the_process_group_and_attach_resumes_on_a_fresh_process(tmp_path):
    a, _, _ = adapter()
    ref = a.start("probe", tmp_path)
    assert a.stop() is True and not a.transport.alive()
    b, srv2, _ = adapter()
    assert b.attach(ref) is True
    assert [m.get("method") for m in srv2.received][:3] == ["initialize", "initialized", "session/resume"]
    assert b.attach("muse:no-such-session") is False
    b.stop()


# ------------------------------------------------------------------------------------------------- probes + qualify

def fake_muse_suite(**server_kw):
    def make_factory(model):
        return lambda: MuseServeAdapter(wire(**server_kw)[0], model=model)
    return fq.Suite(runtime="muse", adapter_version="1", make_factory=make_factory, probes=muse_capability_probes)


def test_muse_qualifies_all_ten_capabilities_against_the_strict_fake(steward_home):
    roster = fr.validate_roster({"schema_version": 1, "revision": 1, "entries": [
        {"id": "muse-worker", "provider": "meta", "runtime": "muse", "model": "muse-spark-1.3-contributor",
         "roles": ["worker"], "capabilities": ["edit", "shell"], "strengths": [], "enabled": True,
         "label": "Muse"}]})
    store = Store(steward_home)
    res = fq.qualify(store, "muse", roster=roster, wait_s=0.01, polls=200,
                     suites={"muse": fake_muse_suite(missing_run_first=1)})
    assert res["results"] == {c: True for c in fr.RUNTIME_CAPABILITIES}, res["evidence"]
    assert FleetStore(store).dispatchable(roster.entries[0])


def test_muse_qualifies_when_the_drifted_model_asks_for_tools_on_every_turn(steward_home):
    """Live 2026-10-04: Muse answered the bare "probe" brief by asking Bash permission (`ls`, `git status`); nobody
    answered, so typed_result never saw a done/error frame. The non-permission probes now send an explicit no-tools
    brief and DENY every request they see while waiting, recording the denial; nothing is ever approved."""
    servers = []

    def make_factory(model):
        def make():
            t, srv, _ = wire(drifted=True)
            servers.append(srv)
            return MuseServeAdapter(t, model=model)
        return make

    suite = fq.Suite(runtime="muse", adapter_version="1", make_factory=make_factory, probes=muse_capability_probes)
    res = fq.qualify(Store(steward_home), "muse", wait_s=0.01, polls=200, suites={"muse": suite})
    assert res["results"]["typed_result"] is True and res["results"]["usage"] is True, res["evidence"]
    assert "denied" in res["evidence"]["typed_result"] and "ls; git status" in res["evidence"]["typed_result"]
    decisions = [d["choiceId"] for s in servers for d in s.decisions]
    assert decisions and "allow_once" not in decisions and set(decisions) == {"abort"}
    briefs = {m["params"]["input"][0]["text"] for s in servers for m in s.received if m.get("method") == "turn/start"}
    assert PROBE_BRIEF in briefs and "probe" not in briefs
    assert "Do not use any tools" in PROBE_BRIEF


def test_muse_is_a_registered_suite():
    suite = fq.SUITES["muse"]
    assert suite.runtime == "muse" and suite.adapter_version == MuseServeAdapter.adapter_version
    assert suite.probes is muse_capability_probes


def test_the_real_cli_is_never_spawned_by_the_suite_factory_until_used():
    # building a factory must not start a process
    make = fq.SUITES["muse"].make_factory(None)
    a = make()
    assert not a.transport.alive() and a.transport.argv[:2] == ["muse", "serve"]
    assert subprocess  # noqa: the module is imported only for typing symmetry


def test_a_request_to_a_dead_server_fails_fast_instead_of_waiting_out_the_timeout(tmp_path):
    """Found by the suite: `qualify` stops every adapter again after its probe, and a stopped adapter's interrupt
    waited the full 30 s request timeout on a process that could never answer."""
    import time
    a, _, _ = adapter()
    a.start("probe", tmp_path)
    assert a.stop() is True
    t0 = time.monotonic()
    a.stop()
    with pytest.raises(Exception):
        a.transport.request("turn/interrupt", {"commandId": uuid7()}, timeout=30)
    assert time.monotonic() - t0 < 2


@pytest.mark.parametrize("updated_as", ["notification", "request"])
def test_a_multi_stage_approval_is_decided_stage_by_stage_with_the_one_verdict(tmp_path, updated_as):
    """Live 2026-10-04 (fleet proof 3d0441cf): `pwd; ls -la; git ...` is one approval with a stage per command.
    Deciding stage 0 returned terminal:false and muse waited for the next stage forever. Steward's policy judged
    the WHOLE command, so the same verdict decides every remaining stage of that approval."""
    a, srv, _ = adapter(stages=2, updated_as=updated_as)
    a.start("Run it and wait for approval", tmp_path)
    req = pump(a, pred=lambda e: e.type == "permission_request")
    assert req[0].body["input"] == {"command": "pwd; ls -la"}
    assert a.answer_prompt(True, req[0].body["request_id"]).ok
    done = pump(a, pred=lambda e: e.type in ("done", "permission_request"))
    assert [e.type for e in done] == ["done"]   # no second relay to the owner; the turn completes
    assert [(d["choiceId"], d["requirementId"]["sourceIndex"]) for d in srv.decisions] == [("allow_once", 0),
                                                                                            ("allow_once", 1)]
    a.stop()


def test_a_later_stage_for_a_different_command_is_relayed_again(tmp_path):
    a, srv, _ = adapter(stages=2)
    a.start("Run it and wait for approval", tmp_path)
    req = pump(a, pred=lambda e: e.type == "permission_request")
    a._decided[req[0].body["request_id"]] = (True, "something else entirely")
    a._perm[req[0].body["request_id"]] = a._perm.get(req[0].body["request_id"]) or {}
    a.transport._inbox.put({"jsonrpc": "2.0", "method": "approval/updated", "params": {
        "approvalId": req[0].body["request_id"], "currentRequirementId": {"approvalId": "ap1", "sourceIndex": 1},
        "availableChoices": [{"choiceId": "allow_once", "decision": "approved", "scope": "once"}],
        "subject": {"kind": "shell", "command": "rm -rf /"}, "change": {"kind": "stageResolved"}}})
    again = pump(a, pred=lambda e: e.type == "permission_request")
    assert again and again[0].body["input"] == {"command": "rm -rf /"}
    a.stop()


def test_an_update_about_an_already_decided_stage_is_not_decided_again(tmp_path):
    a, srv, _ = adapter()
    a.start("Run it and wait for approval", tmp_path)
    pump(a, pred=lambda e: e.type == "permission_request")
    a.answer_prompt(True, "ap1")
    n = len(srv.decisions)
    a.transport._inbox.put({"jsonrpc": "2.0", "method": "approval/updated", "params": {
        "approvalId": "ap1", "currentRequirementId": {"approvalId": "ap1", "sourceIndex": 0},
        "availableChoices": [], "subject": {"kind": "shell", "command": "echo steward-probe"},
        "change": {"kind": "resolved"}}})
    pump(a, pred=lambda e: False, n=5)
    assert len(srv.decisions) == n
    a.stop()
