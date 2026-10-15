"""PiRpcAdapter (DeepSeek V4 Pro via deepinfra in `pi --mode rpc`) over a scripted fake pi (never spawns anything).

The fake follows pi's docs/rpc-commands.md and the live trace of 2026-10-04: id-correlated responses, `agent_start` ...
`agent_settled`, a steer to an idle session is only queued, `abort` settles the run, `get_state` names the model, and
the Steward gate extension surfaces each mutating tool call as an `extension_ui_request` confirm dialog."""

from __future__ import annotations

import json
import queue
import time
from pathlib import Path

import pytest

from steward import fleet_qualify as fq
from steward import fleet_registry as fr
from steward.fleet_store import FleetStore
from steward.pi_worker import (GATE_EXTENSION, GATE_TITLE, PI_MODEL, PiError, PiRpcAdapter, pi_argv,
                               pi_capability_probes)
from steward.store import Store
from steward.worker import PROBE_LONG_BRIEF, NdjsonProcess

SESSIONS: set = set()


class FakePi:
    PID = 8000

    def __init__(self, argv, cwd, *, model=PI_MODEL, provider="deepinfra"):
        FakePi.PID += 1
        self.pid, self.argv, self.cwd, self.returncode = FakePi.PID, argv, cwd, None
        self.model, self.provider = model, provider
        self.out: "queue.Queue[str | None]" = queue.Queue()
        self.err: "queue.Queue[str | None]" = queue.Queue()
        self.stdout, self.stderr, self.stdin = iter(self.out.get, None), iter(self.err.get, None), self
        self.received, self.running, self.queued, self.tokens = [], False, [], 0
        self.session = argv[argv.index("--session") + 1] if "--session" in argv else f"sess-{self.pid}"
        SESSIONS.add(self.session)
        self.ui_n = 0

    def emit(self, o):
        self.out.put(json.dumps(o) + "\n")

    def resp(self, msg, data=None, ok=True):
        self.emit({"id": msg.get("id"), "type": "response", "command": msg["type"], "success": ok,
                   **({"data": data} if data is not None else {})})

    def settle(self, stop="stop"):
        self.tokens += 500
        self.emit({"type": "message_end", "message": {"role": "assistant", "stopReason": stop}})
        self.emit({"type": "agent_end", "messages": []})
        self.emit({"type": "agent_settled"})
        self.running = False

    def write(self, line):
        for raw in line.splitlines():
            m = json.loads(raw)
            self.received.append(m)
            t = m.get("type")
            if t == "get_state":
                self.resp(m, {"model": {"id": self.model, "provider": self.provider}, "sessionId": self.session,
                              "isStreaming": self.running})
            elif t == "prompt":
                if self.running:
                    self.resp(m, ok=False)
                    continue
                self.resp(m)
                self.running = True
                self.emit({"type": "agent_start"})
                text = m["message"]
                if text == PROBE_LONG_BRIEF:
                    continue
                if "approval" in text:
                    self.ui_n += 1
                    self.emit({"type": "extension_ui_request", "id": f"ui{self.ui_n}", "method": "confirm",
                               "title": GATE_TITLE,
                               "message": json.dumps({"tool": "bash", "input": {"command": "echo steward-probe"}})})
                    continue
                self.settle()
            elif t == "steer":
                self.queued.append(m["message"])
                self.resp(m)
            elif t == "abort":
                if self.running:
                    self.settle("aborted")
                self.resp(m)
            elif t == "extension_ui_response":
                self.settle()
            elif t == "get_session_stats":
                self.resp(m, {"sessionId": self.session, "tokens": {"total": self.tokens}, "cost": 0.0123})

    def flush(self):
        pass

    def poll(self):
        return self.returncode

    def kill(self):
        if self.returncode is None:
            self.returncode = -15
            self.out.put(None)
            self.err.put(None)


def factory(procs=None, **kw):
    procs = procs if procs is not None else []

    def make(argv, cwd):
        holder = {}

        def popen(a, **pkw):
            p = FakePi(a, cwd, **kw)
            holder["p"] = p
            procs.append(p)
            return p
        return NdjsonProcess(argv, cwd=cwd, popen=popen, killpg=lambda pid, sig: holder["p"].kill(), grace_s=0.5)
    return make, procs


def adapter(tmp_path, **kw):
    make, procs = factory(**kw)
    return PiRpcAdapter(make, session_dir=tmp_path / "sess", request_timeout_s=2), procs


def pump(a, pred, n=50):
    out = []
    for _ in range(n):
        out += [e for e in a.observe().events if pred(e)]
        if out:
            return out
        time.sleep(0.01)
    return out


def test_argv_pins_deepinfra_deepseek_v4_pro_with_the_gate_and_no_other_extensions():
    argv = pi_argv(session_dir="/s", gate="/s/.steward/steward-gate.ts")
    assert argv[:3] == ["pi", "--mode", "rpc"]
    assert argv[argv.index("--provider") + 1] == "deepinfra" and argv[argv.index("--model") + 1] == PI_MODEL
    assert "--no-extensions" in argv and argv[argv.index("-e") + 1].endswith("steward-gate.ts")
    assert PI_MODEL == "deepseek-ai/DeepSeek-V4-Pro"


def test_the_gate_blocks_every_mutating_tool_until_steward_says_yes():
    assert "ctx.ui.confirm" in GATE_EXTENSION and "block: true" in GATE_EXTENSION and "ok !== true" in GATE_EXTENSION
    assert '"read", "grep", "find", "ls"' in GATE_EXTENSION


def test_any_other_pi_model_is_refused_before_anything_runs():
    with pytest.raises(PiError, match="off_roster_model"):
        PiRpcAdapter(lambda a, c: None, model="glm-4.6")


def test_steward_never_touches_the_operator_models_json(tmp_path, monkeypatch):
    import steward.pi_worker as pw
    src = Path(pw.__file__).read_text()
    assert "models.json" not in src.replace("`~/.pi/agent/models.json`", "")


def test_start_checks_the_native_model_and_provider(tmp_path):
    a, procs = adapter(tmp_path)
    ref = a.start("probe", tmp_path)
    assert ref == f"pi:{procs[0].session}" and procs[0].cwd == str(tmp_path)
    assert a.process_identity()["native_model"] == PI_MODEL and a.process_identity()["provider"] == "deepinfra"
    gate = Path(procs[0].argv[procs[0].argv.index("-e") + 1])
    assert gate.read_text() == GATE_EXTENSION and tmp_path / "sess" in gate.parents
    a.stop()
    b, _ = adapter(tmp_path, model="some/other-model")
    with pytest.raises(PiError, match="model_identity_mismatch"):
        b.start("probe", tmp_path)
    assert not b.transport.alive()


def test_settled_run_is_a_typed_done_and_usage_comes_from_session_stats(tmp_path):
    a, _ = adapter(tmp_path)
    a.start("probe", tmp_path)
    hit = pump(a, lambda e: e.type in ("done", "error"))
    assert hit[0].type == "done" and a.turn_id is None
    assert a.usage() == {"tokens": 500, "cost_usd": 0.0123, "source": "pi_session_stats", "confidence": "reported"}
    a.stop()


def test_gate_dialog_is_relayed_and_answered_by_steward(tmp_path):
    a, procs = adapter(tmp_path)
    a.start("Run it and wait for approval", tmp_path)
    req = pump(a, lambda e: e.type == "permission_request")
    assert req[0].body == {"request_id": "ui1", "tool": "Bash", "input": {"command": "echo steward-probe"}}
    assert a.answer_prompt(False, "ui1").ok
    assert procs[0].received[-1] == {"type": "extension_ui_response", "id": "ui1", "confirmed": False}
    a.stop()


def test_foreign_dialogs_are_cancelled_never_answered(tmp_path):
    a, procs = adapter(tmp_path)
    a.start(PROBE_LONG_BRIEF, tmp_path)
    procs[0].emit({"type": "extension_ui_request", "id": "x1", "method": "select", "title": "pick", "options": ["a"]})
    pump(a, lambda e: False, n=5)
    assert {"type": "extension_ui_response", "id": "x1", "cancelled": True} in procs[0].received
    a.stop()


def test_steer_into_a_running_session_and_prompt_into_an_idle_one(tmp_path):
    a, procs = adapter(tmp_path)
    a.start(PROBE_LONG_BRIEF, tmp_path)
    assert a.deliver("[rev=2] focus on tests").ok
    assert procs[0].received[-1]["type"] == "steer"
    assert pump(a, lambda e: e.type == "ack")[0].body["via"] == "steer"
    a.interrupt()
    pump(a, lambda e: e.type == "error")
    assert a.deliver("[rev=3] next").ok
    assert procs[0].received[-1]["type"] == "prompt"   # live: a steer into an idle session is only queued
    a.stop()


def test_abort_ends_the_run_with_a_typed_interrupted_result(tmp_path):
    a, _ = adapter(tmp_path)
    a.start(PROBE_LONG_BRIEF, tmp_path)
    a.interrupt()
    hit = pump(a, lambda e: e.type == "error")
    assert hit[0].body["status"] == "interrupted"
    assert a.stop() is True and not a.transport.alive()


def test_attach_reopens_the_session_on_a_fresh_process(tmp_path):
    a, _ = adapter(tmp_path)
    ref = a.start("probe", tmp_path)
    a.stop()
    b, procs = adapter(tmp_path)
    assert b.attach(ref, tmp_path) is True
    assert procs[0].argv[procs[0].argv.index("--session") + 1] == ref.removeprefix("pi:")
    b.stop()


def fake_pi_suite():
    def make_factory(model):
        make, _ = factory()
        import tempfile
        sdir = Path(tempfile.mkdtemp())
        return lambda: PiRpcAdapter(make, model=model, session_dir=sdir, request_timeout_s=2)
    return fq.Suite(runtime="pi", adapter_version="1", make_factory=make_factory, probes=pi_capability_probes)


def test_pi_qualifies_all_ten_capabilities_against_the_fake(steward_home):
    roster = fr.validate_roster({"schema_version": 1, "revision": 1, "entries": [
        {"id": "pi-deepseek-worker", "provider": "deepinfra", "runtime": "pi", "model": PI_MODEL,
         "roles": ["worker"], "capabilities": ["edit"], "strengths": [], "enabled": True, "label": "DeepSeek"}]})
    store = Store(steward_home)
    res = fq.qualify(store, "pi", roster=roster, wait_s=0.01, polls=100, suites={"pi": fake_pi_suite()})
    assert res["results"] == {c: True for c in fr.RUNTIME_CAPABILITIES}, res["evidence"]
    assert FleetStore(store).dispatchable(roster.entries[0])


def test_pi_is_a_registered_suite_whose_default_model_is_deepseek_v4_pro():
    suite = fq.SUITES["pi"]
    assert suite.probes is pi_capability_probes
    a = suite.make_factory(None)()
    assert a.model == PI_MODEL


def test_usage_is_still_known_after_the_lane_is_stopped(tmp_path):
    """Live 2026-10-04 (fleet proof d2cf45e8): the controller records lane usage AFTER `stop()`, when pi is gone;
    the pi lane's usage came out unknown. Usage must come from the event stream as the run goes."""
    a, procs = adapter(tmp_path)
    a.start("probe", tmp_path)
    pump(a, lambda e: e.type == "done")
    procs[0].emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop", "usage": {
        "totalTokens": 1200, "cost": {"total": 0.004}}}})
    pump(a, lambda e: False, n=10)
    assert a.stop() is True and not a.transport.alive()
    u = a.usage()
    assert u is not None and u["tokens"] >= 1200 and u["cost_usd"] is not None
