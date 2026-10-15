"""Unified control API (API-01..API-04, SP-23).

`StewardAPI` is the one control surface both the CLI (Moshi's operations
console) and the MCP server (ChatGPT's semantic console) go through
(SP-23): every read method returns a plain JSON-able dict (or list of
dicts) built from `Store`/`Steering`/`steward.approvals`, and every write
method either goes through `steward.steering.Steering.record` or
`steward.control.Controller.submit` or `steward.approvals.decide` directly
— never writes to the store directly — or, when constructed with an
injected `relay` (OPX-03, 11-CONTEXT), sends the same write as a `cmd`
frame into the live Herdr control pane instead and returns whatever receipt
comes back. Either way every write returns a receipt dict that always
includes the job's current `revision` (SP-27), so a caller can immediately
retry a steer with `expected_revision` set correctly after a `CONFLICT`.

The `relay` mode exists for `steward.mcp_server`: OPX-01/02 moved every CLI
write verb onto the same control-pane relay path, and OPX-03 asks ChatGPT's
writes to be visible there too, so a `StewardAPI` built with `relay=` never
mutates the store itself for a write — the live `steward control` pane's own
(non-relaying) `StewardAPI` instance is the one that actually does, via
`steward.cli._build_control_handlers`. `relay(op, args, *, origin,
request_id) -> dict` is the one seam (`steward.control_pane.relay`, bound to
a store/HerdrApi pair, is the production implementation); a `NoControlPane`
from it becomes a plain `RuntimeError("Steward control pane is not
running")` so MCP's generic tool-call error handling turns it into an
`isError` result without a stack trace leaking through.

Nothing here calls a model or a herdr/git subprocess directly except
`get_diff_summary`, which goes through an injected `runner_exec` (default
`SubprocessRunner`, swapped for `FakeRunner` in tests) exactly like every
other subprocess call in this codebase (SP-1).
"""

from __future__ import annotations

import time
import uuid
from typing import Callable

from steward import approvals as approvals_module
from steward import actor as actor_module
from steward import eta as eta_module
from steward import exceptions, herdr_tabs, tabnames
from steward import transcript as transcript_module
from steward import push as push_module
from steward.contract import Contract, load_contract
from steward.control import Controller
from steward.control_pane import NoControlPane
from steward.models import ControlRequest, JobState, Receipt
from steward.recovery import RecoveryPolicy
from steward.runner_exec import Runner as CommandRunner
from steward.runner_exec import SubprocessRunner
from steward.steering import Steering
from steward.store import Store

_DIFF_STAT_CAP = 4096

# `relay(op, args, *, origin, request_id) -> receipt dict` — see the module
# docstring. `steward.control_pane.relay` bound to a store/HerdrApi pair is
# the production implementation (wired by `steward.cli`'s MCP-serving
# commands); tests inject an in-process fake.
RelayFn = Callable[..., dict]


class StewardAPI:
    def __init__(self, store: Store, runner_exec: CommandRunner | None = None, relay: RelayFn | None = None):
        self.store = store
        self.controller = Controller(store)
        self.runner_exec = runner_exec or SubprocessRunner()
        self.relay = relay

    # -- read ---------------------------------------------------------------

    def _eta(self, job) -> dict | None:
        """The one stored ETA counted down to now (G13, G17): only for a RUNNING job whose efficiency switch is on.
        Reads kv `efficiency_state.eta` and renders it; computes nothing."""
        if job.state != JobState.RUNNING:
            return None
        try:
            from steward import efficiency_watch
            if efficiency_watch.mode(self.store, job.job_id) == "off":
                return None
            state = self.store.kv_get(job.job_id, efficiency_watch.STATE_KEY)
            stored = state.get("eta") if isinstance(state, dict) else None
            return eta_module.api_view(stored, time.time()) if isinstance(stored, dict) else None
        except Exception:  # noqa: BLE001 - the ETA is decoration; it never breaks a read
            return None

    def list_jobs(self, states: set[JobState] | None = None) -> list[dict]:
        rows = []
        for job in self.store.list_jobs(states):
            view = self._eta(job)
            rows.append({
                "job_id": job.job_id,
                "goal": job.goal,
                "state": job.state.value,
                "revision": self.store.get_revision(job.job_id),
                "name": tabnames.job_name(self.store, job, persist=False),
                "eta": view,
                "eta_p50_min": view["p50_min"] if view else None,
                "eta_text": view["shown"] if view else None,
            })
        return rows

    def get_job(self, job_id: str) -> dict | None:
        job = self.store.get_job(job_id)
        if job is None:
            return None
        steering = Steering(self.store)
        contract = load_contract(self.store, job_id)
        return {
            "job_id": job.job_id,
            "goal": job.goal,
            "repo": job.repo,
            "state": job.state.value,
            "revision": self.store.get_revision(job_id),
            "contract": _contract_summary(contract),
            "active_constraints": [
                {
                    "verb": c["verb"],
                    "kind": c["kind"],
                    "enforcement": c["enforcement"],
                    "text": c["text"],
                }
                for c in steering.active_constraints(job_id)
            ],
            "recoveries_used": self.store.kv_get(job_id, "recoveries_used", default=0),
            "max_recoveries": RecoveryPolicy().max_recoveries,
            "herdr_workspace": self.store.kv_get(job_id, "herdr_workspace"),
            "herdr_tab": self.store.kv_get(job_id, "herdr_tab"),
            "herdr_pane": self.store.kv_get(job_id, "herdr_pane"),
            "worker_kind": self.store.kv_get(job_id, "worker_kind"),
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "parent_job": self.store.kv_get(job_id, "parent_job"),
            "children": self.store.kv_get(job_id, "children", default=[]) or [],
            "created_by": self.store.kv_get(job_id, "created_by"),
            "eta": self._eta(job),
            "deadline": self._deadline(job_id),
        }

    def _deadline(self, job_id: str) -> dict | None:
        """The canonical stored deadline object (DL-08): a pure projection, separate from `eta`."""
        try:
            from steward import deadline_status

            return deadline_status.project(self.store, job_id)
        except Exception:  # noqa: BLE001 - decoration; never breaks a read
            return None

    def get_flow(self, job_id: str) -> dict | None:
        """v2.3 Flow inspection (FLOW-09): the job's Flow mode and its recorded episodes, each linked to its one
        investigation (state, reason, validated result or bounded rejected excerpt). Read only."""
        if self.store.get_job(job_id) is None:
            return None
        from steward import config as config_module

        investigations = {r["episode_id"]: r for r in self.store.flow_investigations(job_id)}
        decisions = {r["episode_id"]: r for r in self.store.flow_decisions(job_id)}
        reviews = {r["episode_id"]: r for r in self.store.flow_reviews(job_id)}
        episodes = [{**ep, "investigation": investigations.get(ep["episode_id"]),
                     "decision": decisions.get(ep["episode_id"]), "review": reviews.get(ep["episode_id"])}
                    for ep in self.store.flow_episodes(job_id)]
        return {"job_id": job_id, "mode": config_module.flow_mode(self.store, job_id), "episodes": episodes}

    def flow_report(self, job_ids: list[str] | None = None) -> dict:
        """Read-only Flow quality report (FLOW-10): counts and rates with denominators; labels only, no turns."""
        from steward import flow_actions

        return flow_actions.evaluate(self.store, job_ids)

    def adjudicate_flow(self, episode_id: str, label: str, *, origin: str = "cli", note: str | None = None,
                        action_verdict: str | None = None) -> dict:
        """A human's verdict on a Flow episode (human origins only; feeds evaluation, never policy)."""
        from steward import flow_actions

        return flow_actions.adjudicate(self.store, episode_id, label, actor=origin, note=note,
                                       action_verdict=action_verdict)

    def get_current_step(self, job_id: str) -> dict | None:
        job = self.store.get_job(job_id)
        if job is None:
            return None
        contract = load_contract(self.store, job_id)
        pending = Steering(self.store).pending(job_id)
        return {
            "job_id": job_id,
            "state": job.state.value,
            "heartbeat_class": self.store.kv_get(job_id, "last_class"),
            "heartbeat": self.store.kv_get(job_id, "last_class"),
            "next_step": contract.next_step if contract else None,
            "worker": self._worker_now(job_id),
            "pending_receipts": [
                {"rev": i["rev"], "verb": i["verb"], "text": i["text"], "status": i["status"]}
                for i in pending
            ],
            "eta": self._eta(job),
        }

    def _worker_now(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        if job is not None and job.state.value in ("ACCEPTED", "RELEASED", "STOPPED"):
            # a finished job has no live worker line; skipping its transcript parse keeps list reads fast
            return {"now": None, "running_tool": None, "since": None, "last_said": None, "last_activity_ts": None}
        try:
            return transcript_module.live_line(self.store, job_id)
        except Exception as exc:  # a bad transcript must not break the step read
            return {"now": f"worker transcript unreadable: {type(exc).__name__}", "running_tool": None,
                    "since": None, "last_said": None, "last_activity_ts": None}

    def get_recent_events(self, job_id: str, limit: int = 20) -> list[dict]:
        events = self.store.events(job_id)
        return [
            {
                "seq": e.seq,
                "ts": e.ts,
                "what": e.what,
                "evidence": e.evidence,
                "rule": e.rule,
                "judgment": e.judgment.value,
            }
            for e in events[-limit:]
        ]

    def _finish_line_items(self, job_id: str, events) -> list[dict]:
        """`[{text, met, evidence}]` for the goal's FINISH LINE items (the lines `steward result` prints).
        `met` comes from the latest finish-line gate event: True/False for the item(s) the gate's command
        covers (all items when it names none), None when the gate hasn't judged it."""
        from steward import evidence as evidence_module
        from steward.diagnosis import redact

        job = self.store.get_job(job_id)
        _, finish = evidence_module.split_goal(job.goal) if job else ("", "")
        texts = evidence_module.finish_items(finish) if finish else []
        gate = next((e for e in reversed(events) if e.what in ("finish_line_met", "finish_line_not_met")), None)
        command = (gate.evidence.get("command") or "") if gate else ""
        covered = [bool(command) and command in t for t in texts]
        if not any(covered):
            covered = [True] * len(texts)
        items = []
        for text, hit in zip(texts, covered):
            met, proof = None, None
            if gate is not None and hit:
                met = gate.what == "finish_line_met"
                proof = (f"finish check `{command}` passed" if met and command
                         else None if met else gate.evidence.get("first_failing_line"))
            items.append({"text": redact(text), "met": met, "evidence": redact(proof) if proof else None})
        return items

    def get_verification(self, job_id: str) -> dict:
        result = self._verification(job_id)
        finish_line = self._finish_line_items(
            job_id, self.store.events_of(job_id, ("finish_line_met", "finish_line_not_met")))
        if finish_line:
            result["finish_line"] = finish_line
        fleet = self._fleet_candidate(job_id)
        if fleet is not None:
            result["fleet_candidate"] = fleet
        return result

    def get_outcome_comparison(self, *, repository: str, task_class: str) -> dict:
        """Read-only (Phase 29-02, FLEET-20): descriptive comparison of append-only outcomes whose repository and task
        class match EXACTLY what the caller names. Reports sample counts, outcome facts and missing-data rates per
        runtime identity; no rank, recommendation or assignment input, and nothing is written."""
        from steward import fleet_learning

        return fleet_learning.outcome_view(self.store, repository=repository, task_class=task_class)

    def get_rollout_status(self, job_id: str | None = None, *, build_id: str | None = None,
                           build_digest: str | None = None) -> dict:
        """Read-only (Phase 29-02, FLEET-21): the tech-lead authority state - requested mode, kill switch, which
        ordinary-active evidence exists for the named build, and (with a job) the trial authority and the decision
        a proposal would get now. Evidence can only be written by trusted operator code, never through this API."""
        from steward import fleet_runtime

        build = {"id": build_id, "digest": build_digest} if build_id and build_digest else None
        gate = fleet_runtime.RolloutGate(self.store, build=build)
        return gate.status(job_id)

    def get_dev_trial_status(self, job_id: str, *, build_id: str, build_digest: str) -> dict:
        """Read-only (Phase 29-03, FLEET-22): the isolated dev trial's evidence for one exact build - preflight
        bindings (isolation, outbound policy, live rollback build), the raw run, the completed-trial receipt and the
        promotion gate. Answers only; it never writes evidence and nothing here promotes, deploys or approves."""
        from steward import fleet_runtime

        build = {"id": build_id, "digest": build_digest}
        gate = fleet_runtime.RolloutGate(self.store, build=build)

        def last(kind):
            rows = [e for e in gate.fleet.rollout_evidence(kind, job_id=job_id)
                    if e["build_id"] == build_id and e["build_digest"] == build_digest]
            return rows[-1] if rows else None

        pre, run, rec = last("trial_preflight"), last("trial_run"), last("trial_receipt")
        active_approvals = [e for e in gate.fleet.rollout_evidence("active_owner_approval")
                            if e["build_id"] == build_id and e["build_digest"] == build_digest]
        production_approvals = [e for e in gate.fleet.rollout_evidence("production_approval")
                     if e["build_id"] == build_id and e["build_digest"] == build_digest]
        return {
            "build": build, "job_id": job_id,
            "preflight": None if pre is None else {
                "receipt_id": pre["seq"], "instance": pre["instance"], "expires_at": pre["expires_at"],
                "max_actions": pre["payload"].get("max_actions"), "bindings": pre["payload"].get("bindings", {}),
                "kill_switch_generation": pre["payload"].get("kill_switch_generation")},
            "live_rollback": None if pre is None else pre["payload"].get("bindings", {}).get("rollback"),
            "run": None if run is None else {"run_id": run["seq"], **run["payload"]},
            "receipt": None if rec is None else {"receipt_id": rec["seq"], **rec["payload"]},
            "active_owner_approval_recorded": bool(active_approvals),
            "production_approval_recorded": bool(production_approvals),
            "owner_approval_recorded": bool(production_approvals),
            "promotion": gate.promotion_status(),
        }

    def _fleet_candidate(self, job_id: str) -> dict | None:
        """Read-only projection of the persisted final combined candidate of a fleet job (None for legacy jobs
        and before a combined closeout passed). Never selects an individual lane result."""
        from steward import fleet_closeout

        return fleet_closeout.projection(self.store, job_id)

    def _verification(self, job_id: str) -> dict:
        last = self.store.last_event(job_id, ("verification recorded", "verify_worktree_dirty"))
        if last is None:
            return {
                "verified": False,
                "commit_sha": None,
                "dirty": None,
                "new_failures": [],
                "fixed": [],
                "preexisting_failures": [],
                "weakening": [],
                "unverified": [],
                "unverified_reason": None,
            }
        dirty = last.what == "verify_worktree_dirty"
        broken = [] if dirty else list(last.evidence.get("unverified") or [])
        return {
            "verified": not broken,
            "unverified": broken,
            "unverified_reason": "; ".join(f"`{b['command']}`: {b['reason']}" for b in broken) or None,
            "commit_sha": last.evidence.get("commit_sha"),
            "dirty": dirty,
            "new_failures": [] if dirty else last.evidence.get("new_failures", []),
            "fixed": [] if dirty else last.evidence.get("fixed", []),
            "preexisting_failures": [] if dirty else last.evidence.get("preexisting_failures", []),
            "weakening": self._weakening_summary(job_id),
        }

    def _weakening_summary(self, job_id: str) -> list[str]:
        e = self.store.last_event(job_id, ("checks passed; acceptance unresolved",
                                            "checks unverified; acceptance unresolved"))
        if e is None:
            return []
        return [r for r in e.evidence.get("reasons", []) if r.startswith("test-weakening detected")]

    def get_diff_summary(self, job_id: str) -> dict:
        from steward import jobdiff

        job = self.store.get_job(job_id)
        found = jobdiff.job_diff_base(self.store, job) if job is not None else None
        if found is None:
            return {"base": None, "stat": "", "files": [], "insertions": 0, "deletions": 0,
                    "file_stats": [], "note": "no baseline commit or merge-base found"}
        base, worktree, head = found
        rng = f"{base}..{head}"
        stat = self.runner_exec.run(["git", "diff", "--stat", rng], cwd=worktree)
        nums = self.runner_exec.run(["git", "diff", "--numstat", rng], cwd=worktree)
        file_stats, ins, dele = [], 0, 0
        for ln in nums.stdout.splitlines():
            parts = ln.split("\t", 2)
            if len(parts) != 3:
                continue
            a_, d_, path = parts
            a_n = int(a_) if a_.isdigit() else 0  # "-" for binary files
            d_n = int(d_) if d_.isdigit() else 0
            ins, dele = ins + a_n, dele + d_n
            file_stats.append({"path": path, "insertions": a_n, "deletions": d_n})
        return {
            "base": base,
            "stat": stat.stdout[:_DIFF_STAT_CAP],
            "files": [f["path"] for f in file_stats],
            "insertions": ins,
            "deletions": dele,
            "file_stats": file_stats,
        }

    def explain_blocker(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        if job is None:
            return {"blocker": None, "detail": f"unknown job {job_id}"}

        if job.state == JobState.NEEDS_HUMAN:
            events = self.store.events(job_id)
            last = events[-1] if events else None
            return {
                "blocker": "needs_human",
                "detail": last.what if last else "needs_human (no evidence recorded)",
                "evidence": last.evidence if last else {},
                "rule": last.rule if last else None,
            }

        last_class = self.store.kv_get(job_id, "last_class")
        if last_class == "waiting":
            return {
                "blocker": "waiting",
                "detail": "worker is waiting (permission prompt or blocked lifecycle)",
            }
        if last_class == "suspect_stall":
            return {
                "blocker": "suspect_stall",
                "detail": "heartbeat classifier suspects a stall; one diagnosis is budgeted",
            }
        return {"blocker": None, "detail": "no blocker"}

    # -- write ---------------------------------------------------------------

    def steer(
        self,
        job_id: str,
        instruction: str,
        *,
        origin: str = "cli",
        expected_revision: int | None = None,
        human_instruction: str | None = None,
        interpreted_instruction: str | None = None,
        verb: str = "nudge",
        request_id: str | None = None,
        urgent: bool = False,
    ) -> dict:
        if self.relay is not None:
            return self._relay_call(
                verb,
                {
                    "job_id": job_id,
                    "text": instruction,
                    "verb": verb,
                    "expected_revision": expected_revision,
                    "human_instruction": human_instruction,
                    "interpreted_instruction": interpreted_instruction,
                    **({"urgent": True} if urgent else {}),
                },
                origin=origin,
                request_id=request_id,
            )
        receipt = Steering(self.store).record(
            job_id,
            verb,
            instruction,
            request_id,
            origin=origin,
            human_instruction=human_instruction,
            interpreted_instruction=interpreted_instruction,
            expected_revision=expected_revision,
            urgent=urgent,
        )
        return self._receipt_dict(job_id, receipt)

    def pause(self, job_id: str, *, origin: str = "cli", request_id: str | None = None) -> dict:
        if self.relay is not None:
            return self._relay_call("pause", {"job_id": job_id}, origin=origin, request_id=request_id)
        return self._control(job_id, "pause", request_id)

    def resume(self, job_id: str, *, origin: str = "cli", request_id: str | None = None) -> dict:
        if self.relay is not None:
            return self._relay_call("resume", {"job_id": job_id}, origin=origin, request_id=request_id)
        return self._control(job_id, "resume", request_id)

    def cancel(self, job_id: str, *, origin: str = "cli", request_id: str | None = None) -> dict:
        if self.relay is not None:
            # The control pane's own (non-relaying) `api.cancel` already
            # does the STOPPED-tab-close cleanup below once it actually runs
            # `stop` — nothing further to do here.
            return self._relay_call("stop", {"job_id": job_id}, origin=origin, request_id=request_id)
        result = self._control(job_id, "stop", request_id)
        # A job stopped before it ever ran (CREATED/CONTRACTED) goes straight
        # to STOPPED with no Runner ever attached to close its tab later
        # (bug 2c) — close it here instead. A running job instead lands on
        # STOPPING, and `Runner._handle_stopping` closes the tab once the
        # worker actually confirms stopped.
        job = self.store.get_job(job_id)
        if job is not None and job.state == JobState.STOPPED:
            push_module.notify_creator(self.store, job_id, "stopped", self.runner_exec)
        return result

    def approve(
        self,
        job_id: str,
        approval_id: str,
        *,
        origin: str = "cli",
        decision: str = "approve",
        request_id: str | None = None,
        decided_by: str | None = None,
    ) -> dict:
        if self.relay is not None:
            return self._relay_call(
                decision, {"job_id": job_id, "approval_id": approval_id}, origin=origin, request_id=request_id
            )
        if decided_by is None:
            who = actor_module.classify(origin)
            decided_by = "human" if who.kind == actor_module.Actor.OWNER else who.label()
        receipt = approvals_module.decide(
            self.store, job_id, approval_id, decision, request_id=request_id,
            decided_by=decided_by,
        )
        job = self.store.get_job(job_id)
        if job is not None and not receipt.duplicate and receipt.accepted:
            if job.state == JobState.ACCEPTED:
                push_module.notify_creator(self.store, job_id, "accepted", self.runner_exec)
                if decision == "approve":  # SRD-10: approving the acceptance approval is an accept; it must merge too
                    from steward import autopilot as autopilot_module
                    from steward.runner_exec import SubprocessRunner

                    autopilot_module.after_accept(self.store, job_id, self.runner_exec or SubprocessRunner())
            elif job.state == JobState.STOPPED:
                push_module.notify_creator(self.store, job_id, "stopped", self.runner_exec)
        return self._receipt_dict(job_id, receipt)

    # -- relay-only writes (accept/release/incorporate/supervisors/watchers/new/launch) --
    # These ops only ever run inside the control pane (SP-2), so there is no
    # direct fallback: an API built without a relay refuses them.

    def relay_only(self, op: str, args: dict, *, origin: str = "cli", request_id: str | None = None) -> dict:
        if self.relay is None:
            raise RuntimeError(f"{op} needs the Steward control pane (no relay is configured)")
        return self._relay_call(op, args, origin=origin, request_id=request_id)

    # -- helpers --------------------------------------------------------------

    def _relay_call(self, op: str, args: dict, *, origin: str, request_id: str | None) -> dict:
        """Every relay-mode write (OPX-03): record the inbound exception-path
        use (`chatgpt-mcp`, direction "in") before handing off, then send the
        `cmd` frame and return whatever receipt the control pane produces —
        `Steering.record`'s own CONFLICT detail passes straight through
        unchanged, since the control pane calls the very same method."""
        exceptions.use(self.store, args.get("job_id"), "chatgpt-mcp", {"op": op})
        try:
            return self.relay(op, args, origin=origin, request_id=request_id)
        except NoControlPane as exc:
            raise RuntimeError("Steward control pane is not running") from exc

    def _control(self, job_id: str, kind: str, request_id: str | None) -> dict:
        request_id = request_id or str(uuid.uuid4())
        receipt = self.controller.submit(
            ControlRequest(request_id=request_id, job_id=job_id, kind=kind)
        )
        return self._receipt_dict(job_id, receipt)

    def _receipt_dict(self, job_id: str, receipt: Receipt) -> dict:
        return {
            "request_id": receipt.request_id,
            "job_id": receipt.job_id,
            "kind": receipt.kind,
            "accepted": receipt.accepted,
            "duplicate": receipt.duplicate,
            "detail": receipt.detail,
            "revision": self.store.get_revision(job_id),
        }


def _contract_summary(contract: Contract | None) -> dict | None:
    if contract is None:
        return None
    return {
        "goal": contract.goal,
        "boundaries": contract.boundaries,
        "done_when": contract.done_when,
        "next_step": contract.next_step,
        "authority": contract.authority,
        "assumptions": contract.assumptions,
        "contract_revision": contract.revision,
    }
