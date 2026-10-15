"""AgyStreamAdapter over a scripted fake `agy` stream-json process (never spawns anything).

The fake models what live agy 1.2.16 did (traces 2026-10-04): it refuses stdin messages without an "event" field,
emits init (immediately when resuming with --conversation, else on the first message), a DONE `user_input` step per
ingested message, a `result` per turn with cumulative usage; SIGINT ends the turn with ERROR "interrupted" and exits;
a tool needing permission is AUTO-DENIED (listed in `denied_actions`) - headless agy never asks; the model label is
written only to the --log-file."""

from __future__ import annotations

import json
import queue
import signal
import time

from steward import fleet_qualify as fq
from steward import fleet_registry as fr
from steward.agy_worker import AgyStreamAdapter, agy_argv, agy_capability_probes
from steward.fleet_store import FleetStore
from steward.store import Store
from steward.worker import PROBE_LONG_BRIEF, NdjsonProcess

LABEL = "Gemini 3.8 Flash (High)"


class FakeAgyProc:
    PID = 7000

    def __init__(self, argv, cwd, label=LABEL):
        FakeAgyProc.PID += 1
        self.pid, self.argv, self.cwd, self.returncode = FakeAgyProc.PID, argv, cwd, None
        self.out: "queue.Queue[str | None]" = queue.Queue()
        self.err: "queue.Queue[str | None]" = queue.Queue()
        self.stdout, self.stderr = iter(self.out.get, None), iter(self.err.get, None)
        self.stdin = self
        self.received, self.tokens, self.running = [], 0, False
        self.conv = argv[argv.index("--conversation") + 1] if "--conversation" in argv else f"conv-{self.pid}"
        self.init_sent = False
        if "--log-file" in argv:
            with open(argv[argv.index("--log-file") + 1], "w") as f:
                f.write(f'I1003 model_config_manager.go:327] Propagating selected model override to backend: '
                        f'label="{label}"\n')
        if "--conversation" in argv:
            self._init()

    def _emit(self, obj):
        self.out.put(json.dumps(obj) + "\n")

    def _init(self):
        self.init_sent = True
        self._emit({"event": "init", "conversation_id": self.conv, "init": {"cwd": self.cwd}})

    def write(self, line):
        for raw in line.splitlines():
            msg = json.loads(raw)
            self.received.append(msg)
            if "event" not in msg:
                self.err.put('error: stream input message is missing the "event" field\n')
                continue
            if msg["event"] != "user":
                continue
            if not self.init_sent:
                self._init()
            text = msg["message"]["content"]
            self._emit({"event": "step_update", "step_update": {"step_type": "user_input", "state": "DONE"}})
            if text == PROBE_LONG_BRIEF:
                self.running = True
                continue
            self.tokens += 1000
            result = {"conversation_id": self.conv, "status": "SUCCESS", "usage": {"total_tokens": self.tokens}}
            if "approval" in text:
                result["denied_actions"] = [{"action": "command", "display_name": "RunCommand"}]
            self._emit({"event": "result", "result": result})

    def flush(self):
        pass

    def poll(self):
        return self.returncode

    def signal(self, sig):
        if self.returncode is not None:
            raise ProcessLookupError
        if sig == signal.SIGINT:
            self._emit({"event": "result", "result": {"conversation_id": self.conv, "status": "ERROR",
                                                      "error": "interrupted", "usage": {"total_tokens": self.tokens}}})
        else:
            self._emit({"event": "result", "result": {"status": "ERROR",
                                                      "error": "stream input cancelled: context canceled"}})
        self.returncode = 1
        self.out.put(None)
        self.err.put(None)


def fake_factory(procs=None, label=LABEL):
    procs = procs if procs is not None else []

    def make(argv, cwd):
        holder = {}

        def popen(a, **kw):
            p = FakeAgyProc(a, cwd, label=label)
            holder["p"] = p
            procs.append(p)
            return p

        return NdjsonProcess(argv, cwd=cwd, popen=popen, killpg=lambda pid, sig: holder["p"].signal(sig),
                             grace_s=0.5)
    return make, procs


def adapter(tmp_path, **kw):
    make, procs = fake_factory(label=kw.pop("label", LABEL))
    return AgyStreamAdapter(make, log_dir=tmp_path / "logs", start_timeout_s=2, label_timeout_s=1, **kw), procs


def pump(a, pred, n=50):
    out = []
    for _ in range(n):
        out += [e for e in a.observe().events if pred(e)]
        if out:
            return out
        time.sleep(0.01)
    return out


def test_argv_is_stream_json_print_mode_and_never_skips_permissions():
    argv = agy_argv(model="m", conversation="c1", log_file="/tmp/x.log")
    assert argv[:6] == ["agy", "--input-format", "stream-json", "--output-format", "stream-json", "-p="]
    assert "--dangerously-skip-permissions" not in argv and "list" not in argv
    assert argv[argv.index("--conversation") + 1] == "c1" and argv[argv.index("--model") + 1] == "m"


def test_start_sends_the_brief_with_the_event_field_and_records_identity(tmp_path):
    a, procs = adapter(tmp_path)
    ref = a.start("do the task", tmp_path)
    assert procs[0].received == [{"event": "user", "message": {"role": "user", "content": "do the task"}}]
    assert procs[0].cwd == str(tmp_path)
    assert ref == f"agy:{procs[0].conv}"
    ident = a.process_identity()
    assert ident["native_model"] == LABEL and ident["conversation_id"] == procs[0].conv and ident["pid"]
    a.stop()


def test_requested_model_mismatch_fails_the_start(tmp_path):
    a, procs = adapter(tmp_path, model="Gemini 9 Ultra", label=LABEL)
    try:
        a.start("x", tmp_path)
        raise AssertionError("expected a mismatch")
    except Exception as exc:
        assert "model_identity_mismatch" in str(exc)
    assert procs[0].returncode is not None


def test_result_becomes_a_typed_done_with_cumulative_usage(tmp_path):
    a, _ = adapter(tmp_path)
    a.start("probe", tmp_path)
    hit = pump(a, lambda e: e.type in ("done", "error"))
    assert hit[0].type == "done" and hit[0].body["status"] == "completed"
    assert a.usage() == {"tokens": 1000, "cost_usd": None, "source": "agy_stream_json", "confidence": "reported"}
    a.stop()


def test_a_delivered_instruction_is_acked_when_agy_ingests_it(tmp_path):
    a, procs = adapter(tmp_path)
    a.start(PROBE_LONG_BRIEF, tmp_path)
    assert a.deliver("[rev=4] also add tests").ok
    acks = pump(a, lambda e: e.type == "ack")
    assert acks and acks[0].body == {"rev": 4, "stage": "acknowledged", "via": "agy_user_input_step"}
    a.stop()


def test_permissions_are_auto_denied_by_agy_and_surfaced_never_relayed(tmp_path):
    a, _ = adapter(tmp_path)
    a.start("Run the shell command and wait for approval.", tmp_path)
    hit = pump(a, lambda e: e.type in ("done", "error", "permission_request"))
    assert [e.type for e in hit] == ["done"]
    assert hit[0].body["denied_actions"] == [{"action": "command", "display_name": "RunCommand"}]
    assert a.answer_prompt(True).ok is False
    a.stop()


def test_interrupt_is_sigint_and_ends_the_turn_with_a_typed_interrupted_result(tmp_path):
    a, procs = adapter(tmp_path)
    a.start(PROBE_LONG_BRIEF, tmp_path)
    a.interrupt()
    hit = pump(a, lambda e: e.type == "error")
    assert hit[0].body["status"] == "interrupted" and a.turn_id is None
    assert a.stop() is True


def test_stop_shutdown_record_is_not_reported_as_a_turn_outcome(tmp_path):
    a, procs = adapter(tmp_path)
    a.start("probe", tmp_path)
    pump(a, lambda e: e.type == "done")
    assert a.stop() is True
    assert not [e for e in a.observe().events if e.type in ("done", "error")]


def test_attach_resumes_the_conversation_on_a_fresh_process(tmp_path):
    a, _ = adapter(tmp_path)
    ref = a.start("probe", tmp_path)
    a.stop()
    b, procs = adapter(tmp_path)
    assert b.attach(ref, tmp_path) is True
    assert procs[0].argv[procs[0].argv.index("--conversation") + 1] == ref.removeprefix("agy:")
    b.stop()


def test_deliver_after_an_interrupt_continues_the_conversation(tmp_path):
    make, procs = fake_factory()
    a = AgyStreamAdapter(make, log_dir=tmp_path / "l", start_timeout_s=2, label_timeout_s=1)
    a.start(PROBE_LONG_BRIEF, tmp_path)
    a.interrupt()
    pump(a, lambda e: e.type == "error")
    assert a.deliver("[rev=2] continue").ok
    assert len(procs) == 2 and "--conversation" in procs[1].argv
    a.stop()


def fake_agy_suite():
    def make_factory(model):
        make, _ = fake_factory()
        return lambda: AgyStreamAdapter(make, model=model, start_timeout_s=2, label_timeout_s=1)
    return fq.Suite(runtime="agy", adapter_version="1", make_factory=make_factory, probes=agy_capability_probes)


def test_agy_qualifies_everything_but_permission_relay_and_so_is_not_dispatchable(steward_home):
    roster = fr.validate_roster({"schema_version": 1, "revision": 1, "entries": [
        {"id": "agy-worker", "provider": "google", "runtime": "agy", "model": LABEL, "roles": ["worker"],
         "capabilities": ["edit"], "strengths": [], "enabled": True, "label": "agy"}]})
    store = Store(steward_home)
    res = fq.qualify(store, "agy", roster=roster, wait_s=0.01, polls=100, suites={"agy": fake_agy_suite()})
    assert res["results"].pop("permission_relay") is False
    assert all(res["results"].values()), res["evidence"]
    assert "auto-denied" in res["evidence"]["permission_relay"]
    assert "docs/fleet-evidence/agy-permission-audit.md" in res["evidence"]["permission_relay"]
    assert res["qualified"] is False and not FleetStore(store).dispatchable(roster.entries[0])


def test_the_permission_audit_covers_every_route_and_records_no_relay():
    """The 2026-10-04 audit: every route the job named, each with commands, output and a verdict."""
    from pathlib import Path
    doc = (Path(__file__).resolve().parent / "fixtures/agy/agy-permission-audit.md").read_text()
    for route in ("agy --help", "agy help", "--remote-control", "remote-control daemon", "--prompt-interactive",
                  "--mode", "--sandbox", "Plugins", "Hooks", "PreToolUse", "MCP", "Settings and permission files",
                  "Changelog", "Stream-json control frames", "control_request"):
        assert route in doc, route
    assert "No route exists" in doc and "NOT qualified" in doc
    assert "--dangerously-skip-permissions` was never used" in doc


def test_the_agy_lane_never_carries_an_auto_approving_flag_or_mode():
    argv = agy_argv(model="m", effort="high", conversation="c", log_file="/l")
    assert not {"--dangerously-skip-permissions", "--sandbox", "--mode"} & set(argv)


def test_agy_is_a_registered_suite():
    assert fq.SUITES["agy"].probes is agy_capability_probes and fq.SUITES["agy"].runtime == "agy"


# -- the hook gate (agy-permission-audit.md "Hook-gate route") ------------------------------------------------------

import os  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
from pathlib import Path  # noqa: E402

from steward.agy_worker import AGY_MARKER, agy_lane_factory  # noqa: E402


class GatedFakeAgyProc(FakeAgyProc):
    """Models live agy 1.2.16 with a Steward gate home: on a `touch` request it runs every PreToolUse hook in
    `$HOME/.gemini/config/hooks.json` exactly as agy does (`sh -c`, payload on stdin, cwd = the hooks.json dir) and runs
    the command only on an explicit allow; blank output or `ask` would fall through to the broad allow rule."""

    def __init__(self, argv, cwd, label=LABEL, env=None):
        self.env = dict(env or {})
        super().__init__(argv, cwd, label=label)

    def write(self, line):
        for raw in line.splitlines():
            msg = json.loads(raw)
            text = (msg.get("message") or {}).get("content", "")
            if msg.get("event") == "user" and "`touch " in text:
                self.received.append(msg)
                if not self.init_sent:
                    self._init()
                self._emit({"event": "step_update", "step_update": {"step_type": "user_input", "state": "DONE"}})
                target = text.split("`touch ", 1)[1].split("`", 1)[0]
                threading.Thread(target=self._tool, args=(f"touch {target}",), daemon=True).start()
            else:
                super().write(raw + "\n")

    def _real(self, path: Path) -> Path:
        """Resolve a path through the bwrap mounts in argv (the last mount covering it wins), as the sandbox would."""
        best = None
        i = 0
        while i < len(self.argv) and self.argv[i] != "--":
            if self.argv[i] in ("--bind", "--ro-bind"):
                src, dst = Path(self.argv[i + 1]), Path(self.argv[i + 2])
                if path == dst or dst in path.parents:
                    best = src / path.relative_to(dst)
                i += 3
            else:
                i += 1
        return best or path

    def _decide(self, command):
        cfg = self._real(Path(self.env["HOME"]) / ".gemini/config/hooks.json")
        payload = json.dumps({"conversationId": self.conv, "stepIdx": 2, "workspacePaths": [self.cwd],
                              "toolCall": {"name": "run_command", "args": {"CommandLine": command, "Cwd": self.cwd}}})
        for spec in json.loads(cfg.read_text()).values():
            for group in spec.get("PreToolUse", []):
                for h in group["hooks"]:
                    r = subprocess.run(["sh", "-c", h["command"]], input=payload, capture_output=True, text=True,
                                       cwd=cfg.parent, timeout=h.get("timeout", 30))  # noqa: S604
                    if r.returncode != 0:
                        return "deny", "hook failed"
                    out = r.stdout.strip()
                    d = json.loads(out) if out else {}
                    if d.get("decision") == "deny":
                        return "deny", d.get("reason", "")
        return "allow", ""

    def _tool(self, command):
        decision, reason = self._decide(command)
        if decision == "allow":
            subprocess.run(command, shell=True, cwd=self.cwd, check=True)
            step = {"step_type": "tool", "state": "DONE", "tool_name": "run_command"}
        else:
            step = {"step_type": "tool", "state": "ERROR", "tool_name": "run_command",
                    "tool_info": {"error": {"type": "TOOL_ERROR", "message": f"tool call denied by pre-tool hook: {reason}"}}}
        if self.returncode is not None:
            return
        self._emit({"event": "step_update", "step_update": step})
        self.tokens += 1000
        self._emit({"event": "result", "result": {"conversation_id": self.conv, "status": "SUCCESS",
                                                  "usage": {"total_tokens": self.tokens}}})


def gated_factory(procs=None):
    procs = procs if procs is not None else []

    def make(argv, cwd, env=None):
        holder = {}

        def popen(a, **kw):
            p = GatedFakeAgyProc(a, cwd, env=kw.get("env"))
            holder["p"] = p
            procs.append(p)
            return p

        return NdjsonProcess(argv, cwd=cwd, env=env, popen=popen, killpg=lambda pid, sig: holder["p"].signal(sig),
                             grace_s=0.5)
    return make, procs


def user_home(tmp_path):
    th = tmp_path / "userhome"
    (th / ".gemini/antigravity-cli").mkdir(parents=True)
    (th / ".gemini/antigravity-cli/antigravity-oauth-token").write_text("{}")
    return th


def gated_adapter(tmp_path, **kw):
    make, procs = gated_factory()
    a = AgyStreamAdapter(make, log_dir=tmp_path / "logs", start_timeout_s=2, label_timeout_s=1,
                         gate_dir=tmp_path / "gate", gate_token_home=user_home(tmp_path), **kw)
    return a, procs


def repo(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    return wt


def test_gated_agy_runs_sandboxed_in_the_steward_gate_home_never_the_users(tmp_path, monkeypatch):
    monkeypatch.setenv("META_API_KEY", "must-not-leak")
    a, procs = gated_adapter(tmp_path)
    a.start("probe", repo(tmp_path))
    p = procs[0]
    assert p.argv[0].endswith("bwrap") and p.argv[p.argv.index("--") + 1:][:1] == ["agy"]
    assert "--dangerously-skip-permissions" not in p.argv
    env = p.env
    assert env["HOME"] == str((tmp_path / "gate/home").resolve()) and "META_API_KEY" not in env
    settings = json.loads(p._real(Path(env["HOME"]) / ".gemini/antigravity-cli/settings.json").read_text())
    assert "command(*)" in settings["permissions"]["allow"]
    a.stop()


def test_a_tool_call_becomes_a_permission_request_and_a_deny_keeps_it_from_running(tmp_path):
    a, _ = gated_adapter(tmp_path)
    wt = repo(tmp_path)
    a.start("Run the shell command `touch m1` now.", wt)
    req = pump(a, lambda e: e.type == "permission_request", n=300)
    assert req[0].body["tool"] == "Bash" and req[0].body["input"]["command"] == "touch m1"
    assert a.answer_prompt(False, req[0].body["request_id"]).detail == "denied"
    end = pump(a, lambda e: e.type in ("done", "error"), n=300)
    assert end and not (wt / "m1").exists()
    a.stop()


def test_an_approval_lets_the_tool_run(tmp_path):
    a, _ = gated_adapter(tmp_path)
    wt = repo(tmp_path)
    a.start("Run the shell command `touch m2` now.", wt)
    req = pump(a, lambda e: e.type == "permission_request", n=300)
    assert a.answer_prompt(True).detail == "approved"  # the only open request
    assert pump(a, lambda e: e.type == "done", n=300) and (wt / "m2").exists()
    assert a.answer_prompt(True, req[0].body["request_id"]).detail == "unknown_request"  # answered once only
    a.stop()


def test_stopping_the_lane_denies_what_is_still_waiting(tmp_path):
    a, procs = gated_adapter(tmp_path)
    wt = repo(tmp_path)
    a.start("Run the shell command `touch m3` now.", wt)
    pump(a, lambda e: e.type == "permission_request", n=300)
    a.stop()
    time.sleep(0.5)
    assert not (wt / "m3").exists()
    assert a.answer_prompt(True).ok is False


def test_an_unanswered_request_expires_as_a_deny(tmp_path):
    a, _ = gated_adapter(tmp_path, gate_answer_timeout_s=0.3)
    wt = repo(tmp_path)
    a.start("Run the shell command `touch m4` now.", wt)
    req = pump(a, lambda e: e.type == "permission_request", n=300)
    pump(a, lambda e: e.type in ("done", "error"), n=300)
    assert not (wt / "m4").exists()
    assert "expired" in a.answer_prompt(True, req[0].body["request_id"]).detail
    a.stop()


def test_the_lane_factory_puts_each_lane_behind_its_own_gate(tmp_path):
    make, _ = gated_factory()
    f = agy_lane_factory(make_process=make, gate_home=tmp_path)
    a = f(job_id="j1", lane_id="l1", task_id="t", attempt_no=1, epoch=1)
    assert a.gate_dir == tmp_path / "agy-gate" / "j1" / "l1"
    assert agy_lane_factory()(job_id="j", lane_id="l", task_id="t", attempt_no=1, epoch=1).gate_dir is None


def gated_agy_suite(tmp_path):
    th = user_home(tmp_path)

    def make_factory(model):
        make, _ = gated_factory()
        return lambda: AgyStreamAdapter(make, model=model, start_timeout_s=2, label_timeout_s=1,
                                        gate_dir=tmp_path / "gate", gate_token_home=th)
    return fq.Suite(runtime="agy", adapter_version="2", make_factory=make_factory, probes=agy_capability_probes)


def test_gated_agy_qualifies_all_ten_capabilities_by_executed_probes(steward_home, tmp_path):
    roster = fr.validate_roster({"schema_version": 1, "revision": 1, "entries": [
        {"id": "agy-worker", "provider": "google", "runtime": "agy", "model": LABEL, "roles": ["worker"],
         "capabilities": ["edit"], "strengths": [], "enabled": True, "label": "agy"}]})
    store = Store(steward_home)
    res = fq.qualify(store, "agy", roster=roster, wait_s=0.01, polls=300, suites={"agy": gated_agy_suite(tmp_path)})
    assert all(res["results"].values()), res["evidence"]
    ev = res["evidence"]["permission_relay"]
    assert "marker absent=True" in ev and "marker present=True" in ev and "denied" in ev and "approved" in ev
    assert not (Path(res["worktree"]) / AGY_MARKER).exists()
    assert res["qualified"] is True and FleetStore(store).dispatchable(roster.entries[0])


def test_the_production_qualify_suite_and_fleet_lane_are_gated(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path))
    a = fq._agy_suite().make_factory(None)()
    assert a.gate_dir is not None and tmp_path in a.gate_dir.parents
    from steward import fleet_live
    b = fleet_live.default_structured("agy", model=None, job_id="j", lane_id="l", task_id="t", attempt_no=1, epoch=1,
                                      lease_check=None, head_fn=None, home=tmp_path)
    assert b.gate_dir == tmp_path / "agy-gate" / "j" / "l"


def test_the_audit_records_the_hook_gate_route_and_every_safety_test():
    root = Path(__file__).resolve().parents[1]
    doc = (root / "tests/fixtures/agy/agy-permission-audit.md").read_text()
    gate = doc[doc.index("## Hook-gate route"):]
    for n, name in enumerate(("Deny honoured", "Approve honoured", "Fail-closed", "Coverage", "Tamper", "No leakage"), 1):
        head = next(line for line in gate.splitlines() if line.startswith(f"#### {n}. {name}"))
        assert "PASS" in head, head
    assert "proof.py" in gate and "exp.py" in gate  # the exact commands
    assert "40812 files in both listings" in gate and "`diff` is empty" in gate
    assert "Route rejected" not in doc
    ev = root / "tests/fixtures/agy/agy-hookgate"
    import gzip
    before = gzip.decompress((ev / "gemini-sha256-before.txt.gz").read_bytes())
    assert before == gzip.decompress((ev / "gemini-sha256-after.txt.gz").read_bytes())
    assert len(before.splitlines()) == 40812
    live = (root / "tests/fixtures/agy/qualify-agy-live-3.txt").read_text()
    assert "agy: QUALIFIED to dispatch" in live and "NOT qualified" not in live
    assert live.count("  qualified  ") == 10 and "marker absent=True" in live and "marker present=True" in live
