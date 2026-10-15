"""`steward fleet qualify <runtime>` (FLEET-18): run a runtime's capability probes for real and record what was observed.

Each runtime registers a `Suite`: how to build a fresh adapter for a requested model, and its probe builder (one probe
per `fleet_registry.RUNTIME_CAPABILITIES` entry; `worker.codex_capability_probes` is the reference). Probes run in a
scratch Git repo under `$STEWARD_HOME/qualify/`, every adapter a probe creates is stopped before the next probe runs,
and results go through `fleet_runtime.qualify_runtime` - so a capability is qualified only by an executed probe, never
by installation or roster presence."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from steward import fleet_registry as fr
from steward import fleet_runtime as rt
from steward.fleet_store import FleetStore
from steward.store import Store


class QualifyError(Exception):
    """Refused before any probe ran; nothing was recorded."""


@dataclass(frozen=True)
class Suite:
    runtime: str
    adapter_version: str
    make_factory: Callable[[str | None], Callable[[], Any]]   # model -> make_adapter()
    probes: Callable[..., dict]                                # (make, worktree, *, wait, polls) -> {cap: probe}
    version: Callable[[], str | None] | None = None           # the runtime CLI's version now (FLEET-27)
    qualify_each_rostered_model: bool = False                 # shared runtime evidence requires every model


def _cli_version(runtime: str) -> Callable[[], str | None]:
    def read():
        from steward import fleet_versions
        return fleet_versions.read_version(runtime)
    return read


def _codex_suite() -> Suite:
    from steward.worker import CodexAppServerAdapter, StdioAppServerTransport, codex_capability_probes

    def make_factory(model):
        return lambda: CodexAppServerAdapter(StdioAppServerTransport(["codex", "app-server"]), model=model,
                                             sender="codex:qualify")
    return Suite("codex", CodexAppServerAdapter.adapter_version, make_factory, codex_capability_probes, _cli_version("codex"))


def _muse_suite() -> Suite:
    from steward.muse_worker import MuseServeAdapter, muse_capability_probes, muse_transport

    def make_factory(model):
        return lambda: MuseServeAdapter(muse_transport(), model=model, sender="muse:qualify")
    return Suite("muse", MuseServeAdapter.adapter_version, make_factory, muse_capability_probes, _cli_version("muse"))


def _agy_suite() -> Suite:
    from steward.agy_worker import AgyStreamAdapter, agy_capability_probes

    def make_factory(model):
        from steward.agy_gate import gate_root
        from steward.store import resolve_home
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        gate = gate_root(resolve_home(), "qualify", stamp)  # shared: reattach reopens the conversation in its home
        return lambda: AgyStreamAdapter(model=model, sender="agy:qualify", gate_dir=gate)
    return Suite("agy", AgyStreamAdapter.adapter_version, make_factory, agy_capability_probes, _cli_version("agy"))


def _pi_suite() -> Suite:
    from steward.pi_worker import PiRpcAdapter, pi_capability_probes

    def make_factory(model):
        import tempfile
        sessions = Path(tempfile.mkdtemp(prefix="steward-pi-qualify-"))   # shared: reattach reopens a session
        return lambda: PiRpcAdapter(model=model, session_dir=sessions, sender="pi:qualify")
    return Suite("pi", PiRpcAdapter.adapter_version, make_factory, pi_capability_probes, _cli_version("pi"))


class ClaudeFabricQualificationAdapter:
    """Qualification view over the same Herdr shim + FabricWorker path used by Claude fleet lanes."""

    def __init__(self, *, model: str, api, mailbox, runner_pane: str, spawn_lane, job: str | None = None,
                 task: str | None = None, lane: str | None = None):
        from steward.fleet_live import LaneWorker

        self.model = model
        self.job = job or f"qualify-{uuid.uuid4().hex[:16]}"
        self.task = task or f"task-{uuid.uuid4().hex[:12]}"
        self.lane = lane or f"lane-{uuid.uuid4().hex[:12]}"
        self.attempt, self.epoch = 1, 1
        self.api, self.mailbox, self.runner_pane, self.spawn_lane = api, mailbox, runner_pane, spawn_lane
        self.session_id = None
        self.native_model = None
        self._active = False
        self._done = None
        self._interrupted = False
        self.worker = LaneWorker(
            api, mailbox, runner_pane, self.job,
            spawn_pane=lambda worktree: spawn_lane(self.job, self.lane, self.task, self.attempt, self.epoch,
                                                    model, worktree),
            task=None, lane=None, attempt=None, epoch=None,
            requested={"provider": "anthropic", "runtime": "claude-fabric", "model": model},
            stop_wait_s=4.0,
        )

        class _Transport:
            def __init__(inner_self, adapter):
                inner_self.adapter = adapter

            def alive(inner_self):
                pane = inner_self.adapter.worker.pane_id
                if not pane:
                    return False
                try:
                    res = inner_self.adapter.api.call("pane.process_info", {"pane_id": pane})
                except Exception:  # noqa: BLE001 - unreadable liveness is not a pass
                    return False
                info = res.get("process_info", res)
                fg, shell = info.get("foreground_process_group_id"), info.get("shell_pid")
                return fg is not None and shell is not None and fg != shell

        self.transport = _Transport(self)
        self.turn_id = None

    def start(self, brief: str, worktree: Path):
        ref = self.worker.start(brief, worktree)
        self.turn_id, self._active = "active", True
        return ref

    def observe(self):
        from dataclasses import replace
        obs = self.worker.observe()
        events = []
        for event in obs.events:
            if event.type == "event" and event.body.get("kind") == "identity":
                self.session_id = event.body.get("session_id") or self.session_id
                self.native_model = event.body.get("model") or self.native_model
            if event.type == "event" and event.body.get("kind") == "interrupted":
                self._interrupted = True
                events.append(replace(event, type="error", body={"status": "interrupted", **event.body}))
                continue
            if event.type in ("done", "error"):
                self._done = event.body
                self._active = False
                self.turn_id = None
            events.append(event)
        return replace(obs, events=tuple(events))

    def process_identity(self):
        pane = self.worker.pane_id
        pid = None
        if pane:
            try:
                res = self.api.call("pane.process_info", {"pane_id": pane})
                info = res.get("process_info", res)
                candidate = info.get("foreground_process_group_id")
                if candidate != info.get("shell_pid"):
                    pid = candidate
            except Exception:  # noqa: BLE001
                pass
        native = self.native_model
        if native and self.model.casefold() not in native.casefold():
            native = None  # a model alias must still be visible in Claude's own init record
        return {"session_id": self.session_id, "native_model": native, "requested_model": self.model, "pid": pid,
                "pane": pane}

    def deliver(self, text: str, *, urgent: bool = False):
        return self.worker.deliver(text, urgent=urgent)

    def interrupt(self):
        self.worker.deliver("[rev=9001] interrupt qualification turn", urgent=True)

    def answer_prompt(self, approve: bool, request_id: str | None = None, message: str | None = None):
        return self.worker.answer_prompt(approve, request_id, message)

    def stop(self):
        self._active = False
        self.turn_id = None
        return self.worker.stop()

    def attach(self, ref):
        ok = self.worker.attach(str(ref))
        if ok:
            self.turn_id, self._active = "active", True
        return ok

    def reattach_copy(self):
        return ClaudeFabricQualificationAdapter(model=self.model, api=self.api, mailbox=self.mailbox,
                                               runner_pane=self.runner_pane,
                                               spawn_lane=self.spawn_lane,
                                               job=self.job, task=self.task, lane=self.lane)

    def usage(self):
        body = self._done or {}
        values = [body.get(k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                         "cache_creation_input_tokens")]
        if not any(isinstance(v, int) and not isinstance(v, bool) for v in values):
            return None
        return {"tokens": sum(v for v in values if isinstance(v, int) and not isinstance(v, bool)),
                "cost_usd": body.get("cost"), "source": "claude_stream_json", "confidence": "reported"}


def _claude_fabric_suite() -> Suite:
    from steward.herdr_transport import HerdrApi, Mailbox
    from steward.worker import PROBE_BRIEF, codex_capability_probes

    context = {}

    def make_factory(model):
        def make():
            if not model:
                raise QualifyError("claude-fabric qualification requires an explicit roster model")
            sock, pane = os.environ.get("HERDR_SOCKET_PATH"), os.environ.get("HERDR_PANE_ID")
            if not sock or not pane:
                raise QualifyError("claude-fabric qualification requires HERDR_SOCKET_PATH and HERDR_PANE_ID")
            if not context:
                from steward import cli
                from steward.cli import SubprocessRunner
                api = HerdrApi(sock)
                mailbox = Mailbox()
                from steward.store import resolve_home
                spawn_lane = cli._fleet_qualification_spawn(SubprocessRunner(), pane, sock=sock,
                                                            steward_home=resolve_home())
                context.update(api=api, mailbox=mailbox, runner_pane=pane, spawn_lane=spawn_lane)
            return ClaudeFabricQualificationAdapter(model=model, **context)
        return make

    # The shared probe exercises Claude's typed shim frames and the real fleet adapter surface.
    # Reattach is separately probed by binding a fresh controller endpoint to the still-running lane.
    def probes(make, worktree, *, wait=lambda: None, polls=20):
        target = worktree / f".steward-qualification-permission-{uuid.uuid4().hex[:8]}"
        approval = (f"Immediately invoke the Bash tool with exactly `touch {target.as_posix()}`. Do not reply in "
                    "text or use another method. Steward will decide whether to allow the command.")
        out = codex_capability_probes(make, worktree, wait=wait, polls=polls, approval_brief=approval)

        def identity():
            a = make()
            ref = a.start(PROBE_BRIEF, worktree)
            last = {}
            for _ in range(polls):
                a.observe()  # the session/model identity arrives as a typed shim event after the ready handshake
                last = a.process_identity()
                if ref and last.get("session_id") and last.get("native_model") and last.get("pid") is not None:
                    return True, (f"session={last['session_id']} model={last['native_model']} pid={last['pid']}")
                wait()
            return False, (f"session={last.get('session_id')} model={last.get('native_model')} pid={last.get('pid')}")

        def permission_relay():
            a = make()
            a.start(approval, worktree)
            try:
                retried = False
                for _ in range(polls * 2):
                    events = a.observe().events
                    hit = next((e for e in events if e.type == "permission_request"), None)
                    if hit:
                        result = a.answer_prompt(False, hit.body.get("request_id"))
                        return result.ok and not target.exists(), (f"relayed {hit.body.get('tool')}: {result.detail}"
                                                                   f"; retry={retried}")
                    terminal = next((e for e in events if e.type in ("done", "error")), None)
                    if terminal:
                        if not retried:
                            retried = True
                            result = a.deliver(f"[rev=29] {approval}")
                            if result.ok:
                                continue
                            return False, f"turn ended without permission request; retry={result.detail}"
                        detail = {k: terminal.body.get(k) for k in ("status", "result_subtype", "is_error")
                                  if k in terminal.body}
                        return False, (f"turn ended without permission request after retry ({json.dumps(detail)}); "
                                       f"command_ran={target.exists()}")
                    wait()
                return False, f"no approval request observed after retry; command_ran={target.exists()}"
            finally:
                target.unlink(missing_ok=True)

        def reattach():
            a = make()
            ref = a.start("Reply with exactly OK. Do not use any tools.", worktree)
            b = a.reattach_copy()
            ok = b.attach(ref)
            return ok, f"fresh fabric adapter reattached pane={bool(ref)} observed={ok}"

        out["reattach"] = reattach
        out["identity"] = identity
        out["permission_relay"] = permission_relay
        return out

    return Suite("claude-fabric", "fabric-v2", make_factory, probes, _cli_version("claude-fabric"),
                 qualify_each_rostered_model=True)


SUITES: dict[str, Suite] = {"codex": _codex_suite(), "muse": _muse_suite(), "agy": _agy_suite(), "pi": _pi_suite(),
                            "claude-fabric": _claude_fabric_suite()}


def scratch_repo(home: Path, runtime: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    wt = Path(home) / "qualify" / f"{runtime}-{stamp}"
    wt.mkdir(parents=True)
    (wt / "README.md").write_text(f"Steward qualification scratch repo for runtime {runtime}.\n")
    env = ["-c", "user.name=steward-qualify", "-c", "user.email=steward-qualify@localhost"]
    for cmd in (["init", "-q"], ["add", "README.md"], [*env, "commit", "-q", "-m", "qualify scratch"]):
        subprocess.run(["git", "-C", str(wt), *cmd], check=True, capture_output=True)
    return wt


def _stop_quietly(adapter) -> None:
    try:
        adapter.stop()
    except Exception:  # noqa: BLE001 - cleanup must never mask a probe result
        pass


def qualify(store: Store, runtime: str, *, model: str | None = None, roster: fr.Roster | None = None,
            only=None, wait_s: float = 0.5, polls: int = 240, suites: dict[str, Suite] | None = None) -> dict:
    suites = SUITES if suites is None else suites
    suite = suites.get(runtime)
    if suite is None:
        raise QualifyError(f"no probe suite for runtime {runtime!r} (known: {', '.join(sorted(suites)) or 'none'})")
    unknown = sorted(set(only or ()) - set(fr.RUNTIME_CAPABILITIES))
    if unknown:
        raise QualifyError(f"unknown capabilities: {', '.join(unknown)}")
    cli_version = None
    if suite.version is not None:
        cli_version = suite.version()
        if not cli_version:
            raise QualifyError(f"cannot read the {runtime} CLI version (is it installed?); nothing was probed")
    models = [model]
    if suite.qualify_each_rostered_model:
        models = sorted({e.model for e in roster.entries if e.runtime == runtime}) if roster else []
        if not models:
            raise QualifyError(f"runtime {runtime!r} requires a roster so every rostered model can be qualified")
        if model and (len(models) != 1 or model != models[0]):
            raise QualifyError(f"runtime {runtime!r} must qualify every rostered model ({', '.join(models)}); "
                               "omit --model")
    worktree = scratch_repo(store.home, runtime)
    model_probe_sets = {}
    for model_name in models:
        made: list = []
        base_make = suite.make_factory(model_name)

        def make(base_make=base_make, made=made, model_name=model_name):
            a = base_make()
            made.append(a)
            if suite.qualify_each_rostered_model:
                try:
                    a.model = model_name
                except (AttributeError, TypeError):
                    pass
            return a
        make.model = model_name

        def isolated(probe, made=made):
            def run():
                first = len(made)
                try:
                    return probe()
                finally:
                    for a in made[first:]:
                        _stop_quietly(a)
            return run

        probes = suite.probes(make, worktree, wait=lambda: time.sleep(wait_s), polls=polls)
        model_probe_sets[model_name] = {c: isolated(p) for c, p in probes.items() if not only or c in only}
    per_model_results: dict[str, dict[str, bool]] = {name: {} for name in models}
    probes = {}
    for cap in dict.fromkeys(c for probe_set in model_probe_sets.values() for c in probe_set):
        def all_models(cap=cap):
            if not suite.qualify_each_rostered_model:
                probe = model_probe_sets[models[0]].get(cap)
                if probe is None:
                    return False, "probe missing"
                return probe()
            details = []
            statuses = []
            for model_name in models:
                probe = model_probe_sets[model_name].get(cap)
                if probe is None:
                    ok, evidence = False, "probe missing"
                else:
                    try:
                        ok, evidence = probe()
                        ok = bool(ok)
                    except Exception as exc:  # noqa: BLE001 - each model must independently demonstrate it
                        ok, evidence = False, f"{type(exc).__name__}: {exc}"
                per_model_results[model_name][cap] = ok
                statuses.append(ok)
                details.append(f"{model_name}: {'qualified' if ok else 'NOT qualified'} ({str(evidence)[:300]})")
            return all(statuses), json.dumps(details)
        probes[cap] = all_models
    roster_caps = None
    if roster is not None:
        entries = [e for e in roster.entries if e.runtime == runtime]
        if entries:
            roster_caps = sorted({c for e in entries for c in e.capabilities})
    fs = FleetStore(store)
    results = rt.qualify_runtime(fs, runtime, suite.adapter_version, probes, roster_capabilities=roster_caps,
                                 cli_version=cli_version)
    passed = {c for c, ok in results.items() if ok}
    return {"runtime": runtime, "adapter_version": suite.adapter_version, "model": model, "model_results": per_model_results,
            "worktree": str(worktree),
            "cli_version": cli_version,
            "results": results, "qualified": not fr.missing_capabilities(passed, "start"),
            "evidence": {c: o["evidence"] for c, o in fs.runtime_capabilities(runtime).items() if c in results}}
