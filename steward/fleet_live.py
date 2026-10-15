"""Live wiring of the fleet machinery into the runner (Phases 26-29 made reachable).

A job becomes a fleet job only through `steward launch --fleet` (`steward.fleet_launch`), which seeds its graph.
For such a job `wire()` builds the ONE `JobController` (lane admission, leases, frames, stop/recovery), the
`IntegrationWriter` (sole writer of the job branch), the `FleetCloseout` (combined-SHA verification, one candidate,
always NEEDS_HUMAN) and the `TechLeadSupervisor` (the outer loop; proposals go through the controller's arbiter
and the default `RolloutGate` keeps them shadowed), and hands them to the `Runner`. Legacy jobs never reach here.

Each lane runs on the runtime its frozen assignment names (v2.5, FLEET-30): `runtime_lane_factory` builds a Claude
fabric worker (one `steward shim --lane ...` pane per lane attempt, speaking v2 lane frames into the runner's single
mailbox, which `LaneMailboxHub` demultiplexes by lane so two lanes can never read (and drop) each other's frames) for
`claude-fabric`, and the structured adapter (codex app-server, muse serve, agy stream-json, pi rpc) as a child process
group of the runner otherwise. A lane with no assignment in a job that has assignments is refused, and an
unknown runtime fails its start: nothing ever falls back to another runtime. A lane's permission requests are decided by the same authority policy the
single-worker runner uses (`permissions.decide` + in-contract commands), judged against the lane's own worktree;
anything the policy would hold for the owner is denied to the lane (fail closed), never auto-approved.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from steward import fleet_launch
from steward import fleet_runtime as rt
from steward import permissions
from steward import worktree as wt
from steward.contract import load_contract
from steward.fabric_worker import FabricWorker
from steward.fleet_closeout import FleetCloseout
from steward.fleet_integration import IntegrationWriter
from steward.fleet_store import FleetStore
from steward.fleet_tech_lead import TechLeadSupervisor
from steward.herdr_transport import HerdrApiError, HerdrUnavailable
from steward.models import Judgment

BASE_KEY = "fleet_base_sha"  # kv: the pinned parent base every independent lane starts from


class LaneMailboxHub:
    """One runner mailbox shared by every lane worker. Reads are demultiplexed by the frame's lane so a worker only
    ever sees its own lane's frames; v1 / lane-less frames are counted and dropped (no legacy worker here)."""

    def __init__(self, mailbox):
        self.mailbox = mailbox
        self._queues: dict[str, list] = {}
        self.orphans = 0

    def _pump(self, timeout: float) -> None:
        for frame in self.mailbox.read(timeout=timeout):
            if frame.v >= 2 and frame.lane:
                self._queues.setdefault(frame.lane, []).append(frame)
            else:
                self.orphans += 1

    def view(self, lane_id: str) -> "_LaneView":
        return _LaneView(self, lane_id)

    def close(self) -> None:
        close = getattr(self.mailbox, "close", None)
        if callable(close):
            close()


@dataclass
class _LaneView:
    hub: LaneMailboxHub
    lane_id: str

    def read(self, timeout: float) -> list:
        if not self.hub._queues.get(self.lane_id):
            self.hub._pump(timeout)
        return self.hub._queues.pop(self.lane_id, [])


@dataclass
class LaneWorker(FabricWorker):
    """A `FabricWorker` bound to one lane attempt. `stop()` blocks (bounded) until the stop is confirmed, because the
    controller asks exactly once and treats anything else as uncertain; `requested` is what the attempt record
    shows as the model actually asked for."""

    requested: dict = field(default_factory=dict)
    preamble: str = ""
    stop_wait_s: float = 12.0
    sleep: Callable[[float], None] = time.sleep

    def start(self, brief: str, worktree: Path) -> str:
        return super().start(self.preamble + brief, worktree)

    def stop(self) -> bool:
        deadline = self._now() + self.stop_wait_s
        while True:
            if FabricWorker.stop(self):
                self.close_pane()
                return True
            if self._now() >= deadline:
                return False
            self.sleep(0.2)

    def process_identity(self) -> dict:
        return {"pane": self.pane_id}


def lane_preamble(store, job_id: str) -> str:
    contract = load_contract(store, job_id)
    goal = contract.goal if contract else (store.get_job(job_id).goal if store.get_job(job_id) else "")
    return ("You are one lane of a Steward fleet job (several workers, each on its own task, in its own git "
            f"worktree and branch).\nParent job goal: {goal}\n"
            "Work only in the current directory and only on the paths you own. Do not push, merge or touch other "
            "branches. When your task is done and its tests pass, `git add` and `git commit` your changes on the "
            "current branch: that commit is your candidate, which Steward integrates and verifies.\n\n")


def lane_factory(store, *, api, hub: LaneMailboxHub, runner_pane: str,
                 spawn_lane: Callable[..., str], print_fn: Callable[[str], None] = print):
    """The `JobController` adapter factory for Claude fabric lanes. The requested model is the lane's latest frozen
    assignment if one exists, else the worker the `--fleet` launch chose from the roster."""
    fs = FleetStore(store)

    def factory(*, job_id, lane_id, task_id, attempt_no, epoch, lease_check=None):
        choice = store.kv_get(job_id, fleet_launch.LAUNCH_KEY) or {}
        rows = fs.assignments(job_id, lane_id)
        a = rows[-1] if rows else None
        requested = ({"provider": a["provider"], "runtime": a["runtime"], "model": a["model"]} if a else
                     {"provider": choice.get("worker_provider", "unknown"),
                      "runtime": choice.get("worker_runtime", "unknown"),
                      "model": choice.get("worker_model", "unknown")})
        model = requested["model"] if requested["model"] != "unknown" else None
        return LaneWorker(
            api, hub.view(lane_id), runner_pane, job_id,
            spawn_pane=lambda worktree: spawn_lane(job_id, lane_id, task_id, attempt_no, epoch, model, worktree),
            print_fn=print_fn, task=task_id, lane=lane_id, attempt=attempt_no, epoch=epoch,
            lease_check=lease_check, requested=requested, preamble=lane_preamble(store, job_id))

    return factory


FABRIC_RUNTIME = "claude-fabric"
STRUCTURED_RUNTIMES = ("codex", "muse", "agy", "pi")


def git_head(worktree: Path) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True,
                           timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def default_structured(runtime: str, *, model, job_id, lane_id, task_id, attempt_no, epoch, lease_check, head_fn,
                       home: Path):
    """The real structured adapter for one lane attempt; its process group is a child of the runner."""
    kw = dict(model=model, job_id=job_id, task=task_id, lane=lane_id, attempt=attempt_no, epoch=epoch,
              lease_check=lease_check, head_fn=head_fn)
    if runtime == "codex":
        from steward.worker import CodexAppServerAdapter, StdioAppServerTransport
        return CodexAppServerAdapter(StdioAppServerTransport(["codex", "app-server"]), **kw)
    if runtime == "muse":
        from steward.muse_worker import MuseServeAdapter, muse_transport
        return MuseServeAdapter(muse_transport(), **kw)
    if runtime == "agy":
        from steward.agy_gate import gate_root
        from steward.agy_worker import AgyStreamAdapter
        return AgyStreamAdapter(log_dir=Path(home) / "fleet" / "agy-logs" / job_id / lane_id,
                                gate_dir=gate_root(home, job_id, lane_id), **kw)
    if runtime == "pi":
        from steward.pi_worker import PiRpcAdapter
        return PiRpcAdapter(session_dir=Path(home) / "fleet" / "pi-sessions" / job_id / lane_id, **kw)
    raise RuntimeError(f"no fleet adapter for runtime {runtime!r}")


class StructuredLane:
    """A structured adapter bound to one lane attempt: the lane preamble goes in front of the brief and `requested`
    is the frozen assignment the attempt record shows. Everything else is the adapter's own."""

    def __init__(self, inner, *, preamble: str, requested: dict):
        self.inner, self.preamble, self.requested = inner, preamble, requested

    def start(self, brief: str, worktree: Path) -> str:
        return self.inner.start(self.preamble + brief, worktree)

    def process_identity(self) -> dict:
        ident = getattr(self.inner, "process_identity", None)
        return {**(ident() if callable(ident) else {}), "runtime": self.requested.get("runtime")}

    def __getattr__(self, name):
        return getattr(self.inner, name)


def needs_herdr(store, job_id: str) -> bool:
    """True when any of the job's lanes is a Claude fabric pane (every v2.4 fleet job, with no assignments)."""
    choice = store.kv_get(job_id, fleet_launch.LAUNCH_KEY) or {}
    assigned = choice.get("assignments") or {}
    if not assigned:
        return True
    return any(a.get("runtime") == FABRIC_RUNTIME for a in assigned.values())


def runtime_lane_factory(store, *, fabric: Callable[..., Any] | None = None,
                         structured: Callable[..., Any] | None = default_structured):
    """The JobController adapter factory that dispatches each lane attempt to its assigned runtime (FLEET-30)."""
    fs = FleetStore(store)

    def factory(*, job_id, lane_id, task_id, attempt_no, epoch, lease_check=None):
        rows = fs.assignments(job_id, lane_id)
        a = rows[-1] if rows else None
        if a is None:
            if (store.kv_get(job_id, fleet_launch.LAUNCH_KEY) or {}).get("assignments"):
                raise RuntimeError(f"{lane_id} has no frozen runtime assignment; refusing to guess a runtime")
            runtime = FABRIC_RUNTIME   # a v2.4 fleet job: every lane is a Claude fabric pane
        else:
            runtime = a["runtime"]
        if runtime == FABRIC_RUNTIME:
            if fabric is None:
                raise RuntimeError("claude-fabric lanes need Herdr: run the fleet runner inside a Herdr pane")
            return fabric(job_id=job_id, lane_id=lane_id, task_id=task_id, attempt_no=attempt_no, epoch=epoch,
                          lease_check=lease_check)
        if structured is None:
            raise RuntimeError(f"no fleet adapter for runtime {runtime!r}")
        inner = structured(runtime, model=a["model"], job_id=job_id, lane_id=lane_id, task_id=task_id,
                           attempt_no=attempt_no, epoch=epoch, lease_check=lease_check, head_fn=git_head,
                           home=store.home)
        return StructuredLane(inner, preamble=lane_preamble(store, job_id),
                              requested={"provider": a["provider"], "runtime": runtime, "model": a["model"]})

    return factory


def runtime_probe(herdr_probe: Callable | None) -> Callable[[str, str, dict | None], str]:
    """Process-tree probe for any lane: a structured lane's recorded pid (its process-group leader) is checked
    directly; a fabric lane's pane goes to the Herdr probe; anything else is UNKNOWN (never a confirmed stop)."""

    def probe(job_id: str, lane_id: str, identity: dict | None) -> str:
        ident = identity or {}
        pid = ident.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and not ident.get("pane"):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return rt.DEAD
            except PermissionError:
                return rt.ALIVE
            return rt.ALIVE
        if herdr_probe is not None:
            return herdr_probe(job_id, lane_id, identity)
        return rt.UNKNOWN

    return probe


def herdr_lane_probe(api) -> Callable[[str, str, dict | None], str]:
    """Process-tree probe for a lane attempt: its pane gone, or back at a bare shell, is DEAD; a foreground process
    other than the shell is ALIVE; anything unreadable is UNKNOWN (never a confirmed stop)."""

    def probe(job_id: str, lane_id: str, identity: dict | None) -> str:
        pane = (identity or {}).get("pane") or (identity or {}).get("ref")
        if not pane:
            return rt.UNKNOWN
        try:
            result = api.call("pane.process_info", {"pane_id": pane})
        except HerdrApiError:
            return rt.DEAD
        except HerdrUnavailable:
            return rt.UNKNOWN
        info = result.get("process_info", result) if isinstance(result, dict) else {}
        fg, shell = info.get("foreground_process_group_id"), info.get("shell_pid")
        if fg is None or shell is None:
            return rt.UNKNOWN
        return rt.DEAD if fg == shell else rt.ALIVE

    return probe


def permission_relay(store, base_dir: Path, in_contract: Callable[[str, list, Path], bool]):
    """Answer one lane's permission request with the single-worker authority policy, judged in the lane worktree."""
    fs = FleetStore(store)

    def relay(job_id: str, lane_id: str, frame, adapter) -> None:
        tool = frame.body.get("tool", "") or ""
        tool_input = frame.body.get("input") or {}
        request_id = frame.body.get("request_id")
        worktree = wt.lane_path(base_dir, job_id, lane_id)
        command = tool_input.get("command", "") if tool == "Bash" else ""
        contract = load_contract(store, job_id)
        if command and contract and in_contract(command, contract.authority, worktree):
            allow, rule = True, "in_contract_command"
        else:
            from steward import authority

            v = authority.decide_worker(store, job_id, tool, command if tool == "Bash" else tool_input, worktree)
            allow, rule = v.allow, (v.basis if v.grant_id or v.basis.startswith("prohibited") else v.reason)
        detail = "no adapter"
        if adapter is not None:
            res = adapter.answer_prompt(approve=allow, request_id=request_id, message=None if allow else (
                f"Not authorized for a fleet lane ({rule}); outbound or irreversible actions need the owner."))
            detail = res.detail
        fs.append_event(job_id, what="lane_permission_" + ("allowed" if allow else "denied"), actor="fleet_controller",
                        rule="fleet_lane_permission_policy", judgment=Judgment.code, lane_id=lane_id,
                        attempt_no=frame.attempt,
                        evidence={"tool": tool, "request_id": request_id, "policy_rule": rule, "relay": detail})

    return relay


def pinned_base(store, job_id: str, runner_exec, base_dir: Path) -> str:
    """The parent base SHA, pinned once (the job worktree's HEAD at fleet start) and reused after restarts."""
    base = store.kv_get(job_id, BASE_KEY)
    if base:
        return base
    res = runner_exec.run(["git", "rev-parse", "HEAD"], cwd=Path(base_dir) / job_id)
    if res.exit_code != 0:
        raise RuntimeError(f"cannot pin fleet base: {res.stderr.strip()[:200]}")
    base = res.stdout.strip()
    store.kv_set(job_id, BASE_KEY, base)
    return base


def wire(runner, job_id: str, *, adapter_factory: Callable[..., Any], probe: Callable, runner_exec,
         permission_handler: Callable | None = None, advisor: Callable | None = None,
         clock: Callable[[], float] = time.time) -> rt.JobController:
    """Construct the controller, integration writer, combined closeout and tech lead for a fleet job and wire them
    into `runner`. Raises ValueError for a job that is not a fleet job (legacy jobs are never wired)."""
    store = runner.store
    if not rt.is_fleet_job(store, job_id):
        raise ValueError(f"{job_id} is not a fleet job")
    job = store.get_job(job_id)
    base_dir = store.home / "worktrees"
    base = pinned_base(store, job_id, runner_exec, base_dir)
    ctl = rt.JobController(store, job.repo, runner_exec, adapter_factory, base_dir=base_dir, probe=probe,
                           base_sha=base, controller_id=f"runner:{job_id[:8]}", clock=clock)
    writer = IntegrationWriter(store, job.repo, runner_exec, base_dir=base_dir, base_sha=base)
    ctl.integration = writer
    ctl.permission_handler = permission_handler
    runner.fleet_controller = ctl
    runner.integration_writer = writer
    runner.fleet_closeout = FleetCloseout(store, runner_exec, base_dir=base_dir, base_sha=base)
    runner.tech_lead = TechLeadSupervisor(store, ctl, advisor, clock=clock, facts=ctl.arbiter)
    # v2.5 (FLEET-32): every live lane gets its own Jev triage on Jev's timer; gated flags reach the arbiter only
    from steward import jev_watch
    observer = getattr(runner, "jev", None)
    runner.fleet_triage = jev_watch.FleetLaneTriage(store, observer, ctl, clock=clock) if observer is not None else None
    return ctl
