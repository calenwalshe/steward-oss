"""Production wiring for an explicitly configured fleet job.

The factory is intentionally per-job: every adapter and the tech-lead model are selected from the immutable
roster snapshot, while all decisions and writes remain behind the one JobController.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from steward import fleet_closeout, fleet_integration, fleet_live, fleet_runtime, fleet_tech_lead
from steward import fleet_lease
from steward import fleet_registry as fr
from steward.fabric_worker import FabricWorker
from steward.fleet_store import FleetStore
from steward.herdr_transport import FleetMailbox, HerdrApi, Mailbox
from steward.runner_exec import SubprocessRunner
from steward.worker import codex_lane_factory


class FleetServicesError(RuntimeError):
    pass


def _rollout_gate(store):
    """Use an exact-build, revalidated gate only inside a prepared fleet-dev instance.

    Normal installs have no dev build identity and stay shadow-only until a separate release workflow
    supplies independently verified evidence. A partial or invalid dev identity must fail closed.
    """
    build_id = os.environ.get("STEWARD_FLEET_DEV_BUILD_ID")
    build_digest = os.environ.get("STEWARD_FLEET_DEV_BUILD_DIGEST")
    if not build_id and not build_digest:
        return fleet_runtime.RolloutGate(store)
    if not build_id or not build_digest:
        raise FleetServicesError("fleet-dev build identity is incomplete")
    from steward import fleet_dev

    build = {"id": build_id, "digest": build_digest}

    def identity_probe():
        return fleet_dev.instance_identity_from_env(os.environ)

    try:
        identity_probe()
    except fleet_dev.DevIsolationError as exc:
        raise FleetServicesError("fleet-dev identity is invalid: " + "; ".join(exc.problems)) from exc
    return fleet_runtime.RolloutGate(store, build=build, instance_probe=identity_probe)


class FleetModelAdvisor:
    """One-shot, tool-disabled advisor selected from the job's frozen tech-lead roster entry.

    The prompt is `fleet_tech_lead.proposal_prompt` - the closed schema `parse_proposal` enforces (live 2026-10-06:
    a private prompt here asked for an untyped `expected_effect` and allowed empty actions, so every answer was
    recorded `tech_lead_proposal_malformed`). With `gated_only` the paid model is asked only for gated (Arbiter
    outer, `fact:`) triggers, as `OrderedTechLead` does; other material events return None at no cost."""

    def __init__(self, runtime: str, model: str, runner_exec, cwd: Path, *, timeout_s: float = 180,
                 gated_only: bool = False):
        self.runtime, self.model, self.runner_exec, self.cwd, self.timeout_s = runtime, model, runner_exec, cwd, timeout_s
        self.gated_only = gated_only

    def __call__(self, packet: dict):
        trigger_id = packet.get("trigger_id")
        observed = packet.get("observed")
        if not isinstance(trigger_id, str) or not isinstance(observed, dict):
            raise FleetServicesError("tech-lead packet lacks trigger or revision bindings")
        if self.gated_only and not any(str(e.get("ref", "")).startswith("fact:")
                                       for e in packet.get("events", []) if isinstance(e, dict)):
            return None
        prompt = ("You have no tools and cannot edit files. Do not include goals, permissions, grants, roster edits, "
                  "secrets, or executable commands.\n" + fleet_tech_lead.proposal_prompt(packet))
        if self.runtime in ("claude-code", "claude-fabric"):
            res = self.runner_exec.run([
                "env", "-u", "ANTHROPIC_API_KEY", "claude", "-p", prompt, "--model", self.model,
                "--output-format", "json", "--restricted", "--strict-mcp-config", "--tools", "",
            ], cwd=self.cwd, timeout=self.timeout_s)
            if res.exit_code:
                raise FleetServicesError(f"tech-lead command exited {res.exit_code}")
            payload = json.loads(res.stdout)
            raw = payload.get("result") if isinstance(payload, dict) else None
            return json.loads(fleet_tech_lead.extract_json(raw)) if isinstance(raw, str) else payload
        if self.runtime == "codex":
            with tempfile.TemporaryDirectory(prefix="steward-fleet-advisor-") as tmp:
                output = Path(tmp) / "proposal.json"
                res = self.runner_exec.run([
                    "codex", "exec", "--ephemeral", "--sandbox", "read-only", "--model", self.model,
                    "--output-last-message", str(output), prompt,
                ], cwd=self.cwd, timeout=self.timeout_s)
                if res.exit_code:
                    raise FleetServicesError(f"tech-lead command exited {res.exit_code}")
                return json.loads(fleet_tech_lead.extract_json(output.read_text()))
        raise FleetServicesError(f"unsupported tech-lead runtime: {self.runtime}")


def _codex_process_status(identity: dict | None) -> str:
    pid = (identity or {}).get("pid")
    if type(pid) is not int or pid <= 1:
        return fleet_runtime.UNKNOWN
    proc = Path("/proc") / str(pid)
    try:
        command = (proc / "cmdline").read_bytes().replace(b"\0", b" ").lower()
    except FileNotFoundError:
        return fleet_runtime.DEAD
    except OSError:
        return fleet_runtime.UNKNOWN
    if b"codex" not in command or b"app-server" not in command:
        return fleet_runtime.UNKNOWN
    return fleet_runtime.ALIVE


def _lane_runtime_plan(fs: FleetStore, job_id: str, snapshot: dict, roster) -> dict[str, str]:
    """The runtime each graph lane will start on: its latest frozen assignment, else its pin, else the first
    eligible roster worker - the same rule `JobController._start_lane` applies to a `new --fleet-plan` job."""
    view = fs.graph_view(job_id) or {}
    eligible = set(snapshot.get("eligible", []))
    first = next((e for e in roster.entries if e.id in eligible and "worker" in e.roles), None)
    out = {}
    for task in view.get("tasks") or []:
        lane = fleet_runtime.lane_id_for(task["id"])
        rows = fs.assignments(job_id, lane)
        entry = roster.get(rows[-1]["roster_id"]) if rows else None
        pin = (snapshot.get("pins") or {}).get(lane)
        entry = entry or (roster.get(pin) if pin else first)
        if entry is not None:
            out[lane] = entry.runtime
    return out


def needs_herdr(store, job_id: str) -> bool:
    """True when a lane of this snapshot-bound fleet job will run as a Claude shim pane (Herdr required).
    Structured-only jobs (codex, muse, agy, pi) run their lanes as child process groups of the runner."""
    fs = FleetStore(store)
    snapshot = fs.get_snapshot(job_id)
    roster = fs.get_roster(snapshot.get("registry_hash")) if snapshot else None
    if roster is None:
        return True
    return any(r in fr.PANE_RUNTIMES for r in _lane_runtime_plan(fs, job_id, snapshot, roster).values())


def build_fleet_services(store, job_id: str, args, runner_exec=None, *, runner=None, adapter_factory=None,
                         probe=None, advisor=None, mailbox_source=None, mailbox_router=None,
                         structured=fleet_live.default_structured, fabric_spawn=None
                         ) -> tuple[object, object, object, object, object]:
    """Build and attach the controller, integration writer, closeout and tech-lead supervisor for one fleet job.

    `adapter_factory`, `probe`, `advisor`, `structured` (the muse/agy/pi adapter constructor) and `fabric_spawn`
    are injectable for isolated tests. Production defaults never infer qualifications: JobController checks every
    operation against the recorded runtime capability evidence. Which runtimes have an adapter at all is the one
    table `fleet_registry.PRODUCTION_RUNTIMES`, the same table the roster's `dispatchable` reads.
    Herdr (`--sock`/`--pane`) is required only when a lane runs as a Claude shim pane; the returned mailbox is
    None for a structured-only job.
    """
    job = store.get_job(job_id)
    if job is None:
        raise FleetServicesError(f"unknown job: {job_id}")
    runner_exec = runner_exec or SubprocessRunner()
    fs = FleetStore(store)
    snapshot = fs.get_snapshot(job_id)
    if not snapshot:
        raise FleetServicesError("fleet job has no frozen roster snapshot")
    if not snapshot.get("tech_lead"):
        raise FleetServicesError("fleet job has no active tech lead in its frozen roster snapshot")
    roster = fs.get_roster(snapshot.get("registry_hash"))
    if roster is None:
        raise FleetServicesError("fleet job's frozen roster is unavailable")
    eligible = set(snapshot.get("eligible", []))
    unsupported = sorted(e.id for e in roster.entries if e.id in eligible and "worker" in e.roles
                         and e.runtime not in fr.PRODUCTION_RUNTIMES["worker"])
    if unsupported:
        raise FleetServicesError("eligible worker runtime has no production adapter: " + ", ".join(unsupported))
    sock = getattr(args, "sock", None) or os.environ.get("HERDR_SOCKET_PATH")
    pane = getattr(args, "pane", None) or os.environ.get("HERDR_PANE_ID")
    herdr = bool(sock and pane)
    if not herdr and (mailbox_router is not None or needs_herdr(store, job_id)):
        raise FleetServicesError("fleet run requires the caller pane and explicit Herdr socket")
    api = HerdrApi(sock) if herdr else None
    base_dir = store.home / "worktrees"

    def selected(job_, lane):
        assignments = fs.assignments(job_, lane)
        if not assignments:
            raise FleetServicesError(f"lane {lane} has no frozen roster assignment")
        assignment = assignments[-1]
        entry = roster.get(assignment["roster_id"])
        if entry is None or not entry.enabled or entry.id not in set(snapshot.get("eligible", [])):
            raise FleetServicesError(f"lane {lane} assignment is not eligible in the frozen snapshot")
        if (entry.provider, entry.runtime, entry.model) != (assignment["provider"], assignment["runtime"],
                                                             assignment["model"]):
            raise FleetServicesError(f"lane {lane} assignment differs from its frozen roster identity")
        return entry

    def make_adapter(*, job_id, lane_id, task_id, attempt_no, epoch, lease_check=None):
        entry = selected(job_id, lane_id)
        if entry.runtime not in fr.PRODUCTION_RUNTIMES["worker"]:
            raise FleetServicesError(f"unsupported worker runtime: {entry.runtime}")
        if entry.runtime in fr.PANE_RUNTIMES:
            if mailbox is None:
                raise FleetServicesError(f"{entry.runtime} lanes need Herdr: run the fleet runner inside a Herdr pane")
            spawn_fn = fabric_spawn or __import__("steward.cli", fromlist=["_fabric_spawn_pane"])._fabric_spawn_pane
            spawn = spawn_fn(
                runner_exec, pane, job_id, sock=sock, agent="claude-code", agent_script=None,
                extra_args=[], model=entry.model, task=task_id, lane=lane_id, attempt=attempt_no, epoch=epoch,
            )
            return FabricWorker(
                api, mailbox.endpoint(job_id, task_id, lane_id, attempt_no, epoch), pane, job_id, spawn,
                task=task_id, lane=lane_id, attempt=attempt_no, epoch=epoch, sender=f"controller:{lane_id}",
                lease_check=lease_check, kind="claude",
            )
        if entry.runtime == "codex":
            factory = codex_lane_factory(model_for=lambda j, l: selected(j, l).model)
            return factory(job_id=job_id, lane_id=lane_id, task_id=task_id, attempt_no=attempt_no,
                           epoch=epoch, lease_check=lease_check)
        # muse / agy / pi: the same adapters `launch --fleet` lanes and `steward fleet qualify` use, as a child
        # process group of this runner, with the lane preamble and the frozen identity on the attempt record.
        inner = structured(entry.runtime, model=entry.model, job_id=job_id, lane_id=lane_id, task_id=task_id,
                           attempt_no=attempt_no, epoch=epoch, lease_check=lease_check,
                           head_fn=fleet_live.git_head, home=store.home)
        return fleet_live.StructuredLane(inner, preamble=fleet_live.lane_preamble(store, job_id),
                                         requested={"provider": entry.provider, "runtime": entry.runtime,
                                                    "model": entry.model})

    pid_probe = fleet_live.runtime_probe(None)

    def process_probe(job_, lane, identity):
        try:
            entry = selected(job_, lane)
        except Exception:
            return fleet_runtime.UNKNOWN
        if entry.runtime == "codex":
            return _codex_process_status(identity)
        if entry.runtime not in fr.PANE_RUNTIMES:
            return pid_probe(job_, lane, identity)
        ref = (identity or {}).get("ref")
        if api is None or not isinstance(ref, str) or not ref:
            return fleet_runtime.UNKNOWN
        try:
            panes = api.call("pane.list", {}).get("panes", [])
            if not any(p.get("pane_id") == ref for p in panes):
                return fleet_runtime.DEAD
            result = api.call("pane.process_info", {"pane_id": ref})
        except Exception:
            return fleet_runtime.UNKNOWN
        info = result.get("process_info", result)
        fg, shell = info.get("foreground_process_group_id"), info.get("shell_pid")
        if fg is None or shell is None:
            return fleet_runtime.UNKNOWN
        return fleet_runtime.ALIVE if fg != shell else fleet_runtime.DEAD

    lead_entry = roster.get(snapshot.get("tech_lead")) if snapshot.get("tech_lead") else None
    if snapshot.get("tech_lead") and (lead_entry is None or "tech_lead" not in lead_entry.roles):
        raise FleetServicesError("frozen tech-lead roster assignment is invalid")
    if lead_entry is not None and lead_entry.runtime not in fr.PRODUCTION_RUNTIMES["tech_lead"]:
        raise FleetServicesError(f"unsupported tech-lead runtime: {lead_entry.runtime}")
    # Qualification is mandatory for production fleet lanes even when this
    # runtime has no prior capability observations. Without the job-level
    # gate, JobController preserves legacy permissive behavior for runtimes
    # absent from the capability table, which is not valid for fleet jobs.
    fleet_runtime.require_capabilities(store, job_id)
    lead_advisor = advisor
    if lead_entry is not None and lead_advisor is None:
        lead_advisor = FleetModelAdvisor(lead_entry.runtime, lead_entry.model, runner_exec, Path(job.repo),
                                         gated_only=True)
    head_res = runner_exec.run(["git", "rev-parse", "HEAD"], cwd=job.repo)
    if head_res.exit_code:
        raise FleetServicesError(f"cannot pin fleet base commit: {head_res.stderr[:200]}")
    base_sha = store.kv_get(job_id, "fleet_base_sha")
    if base_sha is None:
        base_sha = head_res.stdout.strip()
        with store.immediate() as tx:
            base_sha = tx.kv_get(job_id, "fleet_base_sha") or base_sha
            tx.kv_set(job_id, "fleet_base_sha", base_sha)
    owns_mailbox = mailbox_router is None
    mailbox = mailbox_router or (FleetMailbox(mailbox_source if mailbox_source is not None else Mailbox())
                                 if herdr else None)
    # On restart, register every persisted lease before the first adapter drains stdin. Otherwise that adapter
    # could consume and discard a sibling's rebind/permission frames before the sibling endpoint exists.
    for lease in (fleet_lease.live(store, job_id) if mailbox is not None else []):
        lane_row = store._conn.execute("SELECT task_id FROM fleet_lanes WHERE job_id=? AND lane_id=?",
                                       (job_id, lease["lane_id"])).fetchone()
        if lane_row:
            mailbox.endpoint(job_id, lane_row["task_id"], lease["lane_id"], lease["attempt_no"], lease["epoch"])
    try:
        controller = fleet_runtime.JobController(
            store, job.repo, runner_exec, adapter_factory or make_adapter, base_dir=base_dir,
            probe=probe or process_probe, base_sha=base_sha,
            rollout=_rollout_gate(store),
            frame_handler=runner.handle_fleet_frame if runner is not None else None,
        )
        writer = fleet_integration.IntegrationWriter(store, job.repo, runner_exec, base_dir=base_dir,
                                                     base_sha=base_sha)
        controller.integration = writer
        closer = fleet_closeout.FleetCloseout(store, runner_exec, base_dir=base_dir, base_sha=base_sha)
        supervisor = fleet_tech_lead.TechLeadSupervisor(store, controller, lead_advisor, facts=controller.arbiter)
    except Exception:
        if owns_mailbox and mailbox is not None:
            mailbox.close()
        raise
    return controller, writer, closer, supervisor, mailbox
