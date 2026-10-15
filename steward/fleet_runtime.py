"""Single-controller fleet runtime (Phase 27, FLEET-06..10).

One `JobController` admits explicitly declared, ownership-disjoint tasks into isolated lane
worktrees (default one lane, hard cap two), binds workers to lanes under transactional epoch
leases (`steward.fleet_lease`), routes v2 lane frames by full identity, and drives pause/stop,
restart reconciliation and bounded recovery across every live lane. It is the only code that binds
a worker adapter to a lane. Jobs with no fleet graph are legacy: `admit` does nothing and the
existing single-worker `Runner` path owns them.

Everything external is injected: the adapter factory, the process/pane `probe`, the command
`Runner`. No model call is made anywhere in this module.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from steward import fleet_graph as fg
from steward import fleet_lease as fl
from steward import fleet_registry as fr
from steward import worktree as wt
from steward.fabric import Frame
from steward.fleet_store import FleetStore
from steward.store import Store

ALIVE, DEAD, UNKNOWN = "alive", "dead", "unknown"

# kv keys (per job)
_FENCE = "fleet_fence"
_BLOCKED = "fleet_dispatch_blocked"
_CONTROLS = "fleet_controls"
_LANE_ACTIONS = "fleet_lane_actions"
_RECEIPTS = "fleet_receipts"
_GLOBAL_STEER = "fleet_global_steering"
_RECOVERY_RESULTS = "fleet_recovery_results"
_INT_GATE = "fleet_integration_gate"   # Phase 28: dependents wait for INTEGRATED (not merely finished) tasks
_INT_HOLD = "fleet_integration_hold"   # task_id -> reason; downstream of a blocked/conflicted integration
_CAP_GATE = "fleet_capability_gate"     # Phase 29: every lane operation re-checks its qualified capabilities
_ARBITER = "fleet_arbiter"
_DIAGNOSES = "fleet_diagnoses"
_OWNER_DECISIONS = "fleet_owner_decisions"
_OUTER = "fleet_outer_state"           # last outer-loop (direction change) time + replans used
_LAST_ACTIVITY = "fleet_last_activity" # lane -> last typed activity frame time
MAX_REPLANS = 3
MAX_OPEN_DECISIONS = 5
MAX_TASKS = 50


def lane_id_for(task_id: str) -> str:
    return f"lane-{task_id}"


@dataclass(frozen=True)
class FrameDecision:
    accepted: bool
    reason: str


def fleet_graph_row(store: Store, job_id: str):
    return store._conn.execute(
        "SELECT r.* FROM fleet_job_state s JOIN fleet_graph_revisions r ON r.job_id=s.job_id AND r.graph_rev=s.graph_rev"
        " WHERE s.job_id=?", (job_id,)).fetchone()


def is_fleet_job(store: Store, job_id: str) -> bool:
    """True once the job has a current fleet graph (explicit or implicit); False = legacy single worker."""
    return fleet_graph_row(store, job_id) is not None


def require_integration(store: Store, job_id: str) -> None:
    """Gate dependents on integrated candidates. Set automatically by the IntegrationWriter on the first
    admitted candidate; jobs that never submit candidates keep the Phase 27 finished-task semantics."""
    store.kv_set(job_id, _INT_GATE, True)


def hold_downstream(store: Store, job_id: str, task_id: str, reason: str) -> None:
    """Hold every transitive dependent of `task_id` (a blocked or conflicted integration)."""
    with store.immediate() as tx:
        holds = tx.kv_get(job_id, _INT_HOLD, {})
        holds[task_id] = reason
        tx.kv_set(job_id, _INT_HOLD, holds)


def clear_hold(store: Store, job_id: str, task_id: str) -> None:
    with store.immediate() as tx:
        holds = tx.kv_get(job_id, _INT_HOLD, {})
        if holds.pop(task_id, None) is not None:
            tx.kv_set(job_id, _INT_HOLD, holds)


def integration_holds(store: Store, job_id: str) -> dict:
    return store.kv_get(job_id, _INT_HOLD, {})


def require_capabilities(store: Store, job_id: str) -> None:
    """Turn on per-operation capability enforcement for every runtime of this job. A runtime that has ANY
    recorded capability observation is governed regardless."""
    store.kv_set(job_id, _CAP_GATE, True)


def honest_usage(raw: dict | None) -> dict:
    """Normalize adapter-reported usage. Missing/unparseable tokens or cost stay None (never 0), with an explicit
    unknown source/confidence; a reported figure carries the adapter's own source."""
    raw = raw if isinstance(raw, dict) else {}

    def num(v, kind):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            return None
        return int(v) if kind == "int" else float(v)

    tokens, cost = num(raw.get("tokens"), "int"), num(raw.get("cost_usd"), "float")
    known = tokens is not None or cost is not None
    return {"tokens": tokens, "cost_usd": cost,
            "source": str(raw.get("source") or "unknown") if known else "unknown",
            "confidence": str(raw.get("confidence") or "reported") if known else "none"}


def limit_status(limits: dict, usage: dict) -> dict:
    """Measurable limits are enforced; an unmeasurable one is `unverifiable` - never `met`."""
    out = {}
    for key, field_ in (("max_tokens", "tokens"), ("max_cost_usd", "cost_usd")):
        cap = limits.get(key)
        if cap is None:
            continue
        used = usage.get(field_)
        out[key] = ({"status": "unverifiable", "enforced": False, "used": None, "cap": cap} if used is None else
                    {"status": "exceeded" if used > cap else "within", "enforced": True, "used": used, "cap": cap})
    return out


def qualify_runtime(fs: FleetStore, runtime: str, adapter_version: str, probes: dict, *,
                    roster_capabilities=None, source: str = "probe", cli_version: str | None = None) -> dict:
    """Run each capability probe independently and record ONE observation per capability. A probe that raises or
    returns False is recorded unqualified with its evidence; nothing is inferred from another capability, from the
    CLI being installed or from roster presence. If `roster_capabilities` is given, the runtime-level roster
    qualification is also written: qualified+available only when every capability dispatch needs (`start`) passed."""
    results = {}
    for cap, probe in probes.items():
        try:
            ok, evidence = probe()
        except Exception as exc:  # noqa: BLE001 - a crashing probe is a failed demonstration
            ok, evidence = False, f"{type(exc).__name__}: {exc}"
        fs.record_runtime_capability(runtime, cap, qualified=bool(ok), adapter_version=adapter_version,
                                     evidence=evidence, source=source, cli_version=cli_version)
        results[cap] = bool(ok)
    if roster_capabilities is not None:
        start_ok = not fr.missing_capabilities({c for c, v in results.items() if v}, "start")
        fs.record_qualification(runtime, adapter_version, roster_capabilities, qualified=start_ok,
                                available=start_ok, evidence=json.dumps(results, sort_keys=True),
                                cli_version=cli_version)
    return results


JEV_TRIGGERS = "jev_tech_lead_triggers"   # rollout evidence kind: the owner's shadow/active switch (D-13)


def jev_trigger_mode(store: Store) -> str:
    """`shadow` (default) or `active`: whether per-lane Jev triggers' tech-lead proposals may act. In shadow they are
    recorded with `would_act` and never applied; only the owner flips it (`steward fleet tech-lead --active`)."""
    rows = FleetStore(store).rollout_evidence(JEV_TRIGGERS)
    return "active" if rows and rows[-1]["payload"].get("mode") == "active" else "shadow"


def set_jev_trigger_mode(store: Store, mode: str, *, actor: str, reason: str = "") -> dict:
    if mode not in ("shadow", "active"):
        raise ValueError(f"trigger mode must be shadow or active, not {mode!r}")
    FleetStore(store).record_rollout_evidence(JEV_TRIGGERS, payload={"mode": mode, "actor": actor,
                                                                      "reason": reason[:200]})
    return {"mode": jev_trigger_mode(store)}


class RolloutGate:
    """Shadow-to-active authority for the tech lead (FLEET-21). Held by the one `JobController`; every decision is
    re-derived from insert-only evidence (`fleet_rollout_evidence`) on every call, so nothing is cached and a
    revocation takes effect on the very next action.

    Three authorities, strictly separate:
      shadow - the default. Proposals are validated-and-recorded as `shadowed`; no lane action is taken.
      trial  - ONE capped, expiring authority minted from a 29-03 isolation preflight: exact build id/digest, exact
               dev-instance identity and one disposable low-risk job. It never enables ordinary active mode.
      active - ordinary authority. Needs a measured false-action review, an exercised kill switch, a passing
               FLEET-22 isolated-dev trial receipt and owner approval, all bound to the identical build, AND an
               accepted `request_mode('active')`.
    The kill switch outranks all three and fences new tech-lead proposals immediately. Deterministic stop, pause,
    recovery and closeout paths do not pass through this gate. No model or API caller can write evidence: the
    record_* methods are for trusted operator code (and tests) only; `StewardAPI` exposes reads."""

    TRIAL_MAX_ACTIONS = 3
    TRIAL_MAX_TTL_S = 3600.0
    MIN_REVIEWED_ACTIONS = 20
    MAX_FALSE_ACTION_RATE = 0.02
    ACTIVE_REQUIRES = ("false_action_review", "kill_switch_exercise", "trial_receipt", "active_owner_approval")
    TRIAL_SOURCE = "isolated_dev_trial"

    def __init__(self, store: Store, *, build: dict | None = None,
                 instance_probe: Callable[[], dict] | None = None, clock: Callable[[], float] = time.time):
        self.store = store
        self.fleet = FleetStore(store)
        self.build = dict(build) if build else None   # {"id", "digest"} of the RUNNING build
        self.instance_probe = instance_probe
        self.clock = clock

    # -- helpers -------------------------------------------------------------------------------------------------
    def _same_build(self, ev: dict) -> bool:
        return bool(self.build and ev["build_id"] == self.build.get("id")
                    and ev["build_digest"] == self.build.get("digest"))

    def _bound(self, build: dict | None) -> tuple[str, str]:
        b = build or self.build
        if not b or not b.get("id") or not b.get("digest"):
            raise ValueError("rollout evidence needs an exact build id and digest")
        return b["id"], b["digest"]

    # -- kill switch ---------------------------------------------------------------------------------------------
    def kill_switch_active(self) -> bool:
        rows = self.fleet.rollout_evidence("kill_switch")
        return bool(rows and rows[-1]["payload"].get("active"))

    def kill_switch_generation(self) -> int:
        """How many times the switch has been written. A trial bound to a generation dies when it changes."""
        return len(self.fleet.rollout_evidence("kill_switch"))

    def set_kill_switch(self, active: bool, *, actor: str, reason: str = "") -> int:
        return self.fleet.record_rollout_evidence("kill_switch", payload={
            "active": bool(active), "actor": actor, "reason": reason[:200]})

    def exercise_kill_switch(self, *, actor: str, build: dict | None = None) -> dict:
        """Actually flip the switch, observe that a proposal is fenced, restore the prior state, and record the
        observation (passed only if the fence was observed)."""
        bid, dig = self._bound(build)
        prior = self.kill_switch_active()
        self.set_kill_switch(True, actor=actor, reason="exercise")
        try:
            fenced = self.authorize("kill-switch-exercise", 1)
            passed = (not fenced["allowed"]) and fenced["reasons"][:1] == ["kill_switch"]
        finally:
            self.set_kill_switch(prior, actor=actor, reason="exercise_restore")
        self.fleet.record_rollout_evidence("kill_switch_exercise", build_id=bid, build_digest=dig,
                                           payload={"passed": passed, "actor": actor})
        return {"passed": passed}

    # -- evidence recorders (trusted operator code only) ---------------------------------------------------------
    def record_false_action_review(self, *, reviewed_actions: int, false_actions: int, reviewer: str,
                                   build: dict | None = None) -> dict:
        bid, dig = self._bound(build)
        if not (isinstance(reviewed_actions, int) and isinstance(false_actions, int)
                and 0 <= false_actions <= reviewed_actions):
            raise ValueError("false-action review needs measured counts")
        rate = false_actions / reviewed_actions if reviewed_actions else None
        passed = (reviewed_actions >= self.MIN_REVIEWED_ACTIONS and rate is not None
                  and rate <= self.MAX_FALSE_ACTION_RATE)
        self.fleet.record_rollout_evidence("false_action_review", build_id=bid, build_digest=dig, payload={
            "reviewed_actions": reviewed_actions, "false_actions": false_actions, "rate": rate,
            "reviewer": reviewer, "passed": passed})
        return {"passed": passed, "rate": rate}

    def record_trial_preflight(self, *, job_id: str, instance: dict, isolated: bool, low_risk: bool,
                               disposable: bool, ttl_s: float, max_actions: int, build: dict | None = None,
                               bindings: dict | None = None, bind_kill_switch: bool = False) -> int:
        """A 29-03 isolation preflight receipt. Only an isolated, low-risk, disposable job with a short expiry and a
        small fixed cap can mint trial authority; anything else is refused at the door. `bindings` (29-03) carries the
        preflight facts the receipt must keep (outbound policy, live-state sentinel digest, rollback identity);
        `bind_kill_switch` pins the current kill-switch generation: any later write to the switch revokes the trial."""
        bid, dig = self._bound(build)
        if not (isolated and low_risk and disposable):
            raise ValueError("trial preflight must be isolated, low-risk and disposable")
        if not instance:
            raise ValueError("trial preflight needs the exact dev-instance identity")
        if not (0 < ttl_s <= self.TRIAL_MAX_TTL_S) or not (0 < max_actions <= self.TRIAL_MAX_ACTIONS):
            raise ValueError("trial preflight expiry/cap out of bounds")
        return self.fleet.record_rollout_evidence(
            "trial_preflight", build_id=bid, build_digest=dig, job_id=job_id, instance=instance,
            payload={"isolated": True, "low_risk": True, "disposable": True, "max_actions": int(max_actions),
                     "bindings": dict(bindings or {}),
                     **({"kill_switch_generation": self.kill_switch_generation()} if bind_kill_switch else {})},
            expires_at=self.clock() + float(ttl_s))

    def record_trial_receipt(self, *, job_id: str, passed: bool, source: str, build: dict | None = None,
                             detail: str = "", evidence: dict | None = None) -> dict:
        """The completed trial's receipt. Only `source='isolated_dev_trial'` for the preflighted build+job can ever
        count; a replay (or any other source) is stored as NOT passing."""
        bid, dig = self._bound(build)
        pre = [e for e in self.fleet.rollout_evidence("trial_preflight", job_id=job_id)
               if e["build_id"] == bid and e["build_digest"] == dig]
        ok = bool(passed and source == self.TRIAL_SOURCE and pre)
        seq = self.fleet.record_rollout_evidence("trial_receipt", build_id=bid, build_digest=dig, job_id=job_id, payload={
            "passed": ok, "source": source, "claimed_passed": bool(passed), "preflighted": bool(pre),
            "detail": detail[:200], "evidence": dict(evidence or {})})
        return {"passed": ok, "receipt_id": seq}

    def record_owner_approval(self, *, purpose: str, actor: str, build: dict | None = None) -> int:
        """Record a fresh exact-build approval for ONE authority only.

        Active-mode approval and production-promotion approval are intentionally different immutable evidence kinds.
        A generic/legacy `owner_approval` row is never read by either gate.
        """
        kinds = {"active": "active_owner_approval", "production": "production_approval"}
        if purpose not in kinds:
            raise ValueError("approval purpose must be active or production")
        bid, dig = self._bound(build)
        return self.fleet.record_rollout_evidence(kinds[purpose], build_id=bid, build_digest=dig,
                                                  payload={"passed": True, "actor": actor, "purpose": purpose})

    PHASE_GATES = tuple(f"FLEET-{n}" for n in range(15, 22))

    def record_phase_gates(self, gates: dict, *, actor: str, build: dict | None = None) -> dict:
        """Deprecated assertion-only path. Caller-provided booleans can never prove a rollout requirement."""
        raise ValueError("phase gates require verified exact-build receipts; caller-supplied booleans are not evidence")

    def _record_phase_gate_receipt(self, gate: str, *, actor: str, source_digest: str,
                                   command: list[str], output_digest: str, exit_code: int,
                                   build: dict | None = None) -> int:
        """Internal receipt writer. The trusted verifier runs the fixed command before calling this method."""
        bid, dig = self._bound(build)
        if gate not in self.PHASE_GATES or source_digest != dig or not command or not output_digest:
            raise ValueError("phase-gate receipt is not bound to this exact source build")
        return self.fleet.record_rollout_evidence("phase_gate_receipt", build_id=bid, build_digest=dig,
                                                  payload={"gate": gate, "passed": exit_code == 0,
                                                           "source_digest": source_digest, "command": command,
                                                           "output_digest": output_digest,
                                                           "exit_code": int(exit_code), "actor": actor})

    def _phase_gates_passed(self) -> bool:
        latest = {}
        for row in self.fleet.rollout_evidence("phase_gate_receipt"):
            if not self._same_build(row):
                continue
            gate = row["payload"].get("gate")
            if gate in self.PHASE_GATES:
                latest[gate] = row["payload"]
        return all(g in latest and latest[g].get("passed") is True
                   and latest[g].get("source_digest") == self.build.get("digest")
                   for g in self.PHASE_GATES)

    def promotion_status(self) -> dict:
        """Read-only (FLEET-22): may THIS build be proposed for a separate production promotion? Needs every phase
        gate, a passing completed isolated-trial receipt and an explicit owner approval, all for the identical
        build. This only answers; nothing here deploys, activates, releases or incorporates anything."""
        missing = []
        if not self._phase_gates_passed():
            missing.append("phase_gates")
        for kind in ("trial_receipt", "production_approval"):
            rows = [e for e in self.fleet.rollout_evidence(kind) if self._same_build(e)]
            if not (rows and rows[-1]["payload"].get("passed")):
                missing.append(kind)
        if self.build is None:
            missing.append("running_build_unknown")
        return {"eligible": not missing, "missing": missing, "performs_promotion": False,
                "ordinary_active": self.eligibility()["eligible"]}

    # -- ordinary active eligibility -----------------------------------------------------------------------------
    def eligibility(self) -> dict:
        have, missing = {}, []
        for kind in self.ACTIVE_REQUIRES:
            rows = [e for e in self.fleet.rollout_evidence(kind) if self._same_build(e)]
            have[kind] = bool(rows and rows[-1]["payload"].get("passed"))
            if not have[kind]:
                missing.append(kind)
        if self.build is None:
            missing.append("running_build_unknown")
        return {"eligible": not missing and not self.kill_switch_active(), "evidence": have, "missing": missing,
                "kill_switch": self.kill_switch_active()}

    def request_mode(self, mode: str, *, actor: str) -> dict:
        """Ask for `shadow` (always granted: a downgrade) or `active` (granted only with full evidence). A refused
        request is recorded and changes nothing."""
        if mode not in ("shadow", "active"):
            raise ValueError("mode must be shadow or active")
        elig = self.eligibility()
        ok = mode == "shadow" or elig["eligible"]
        self.fleet.record_rollout_evidence("mode_request", build_id=(self.build or {}).get("id"),
                                           build_digest=(self.build or {}).get("digest"),
                                           payload={"mode": mode, "accepted": ok, "actor": actor,
                                                    "missing": [] if ok else elig["missing"]})
        return {"accepted": ok, "mode": mode if ok else self.requested_mode(),
                "reasons": [] if ok else [f"missing:{m}" for m in elig["missing"]]
                + (["kill_switch"] if elig["kill_switch"] else [])}

    def requested_mode(self) -> str:
        acc = [e for e in self.fleet.rollout_evidence("mode_request") if e["payload"].get("accepted")]
        return acc[-1]["payload"]["mode"] if acc else "shadow"

    # -- trial authority -----------------------------------------------------------------------------------------
    def trial_authority(self, job_id: str, *, audit: bool = True) -> dict:
        pres = [e for e in self.fleet.rollout_evidence("trial_preflight", job_id=job_id) if self._same_build(e)]
        if not pres:
            return {"valid": False, "reasons": ["no_trial_preflight"], "remaining": 0}
        pre = pres[-1]
        reasons = []
        if any(e["payload"].get("preflight") == pre["seq"] for e in self.fleet.rollout_evidence(
                "trial_revoked", job_id=job_id)):
            reasons.append("trial_revoked")  # sticky: restoring the old identity/clock does not restore authority
        if self.clock() >= (pre["expires_at"] or 0):
            reasons.append("trial_expired")
        bound_gen = pre["payload"].get("kill_switch_generation")
        if bound_gen is not None and bound_gen != self.kill_switch_generation():
            reasons.append("kill_switch_generation_changed")
        if self.instance_probe is None:
            reasons.append("instance_unverifiable")
        else:
            try:
                if self.instance_probe() != pre["instance"]:
                    reasons.append("instance_identity_changed")
            except Exception:  # noqa: BLE001 - an unreadable identity is a changed identity
                reasons.append("instance_unverifiable")
        if any(e["seq"] > pre["seq"] and self._same_build(e) for e in self.fleet.rollout_evidence(
                "trial_receipt", job_id=job_id)):
            reasons.append("trial_complete")
        used = sum(e["payload"].get("n", 0) for e in self.fleet.rollout_evidence("trial_use", job_id=job_id)
                   if e["payload"].get("preflight") == pre["seq"])
        remaining = max(0, pre["payload"]["max_actions"] - used)
        if remaining <= 0:
            reasons.append("trial_cap_exhausted")
        if audit and reasons and "trial_revoked" not in reasons and not any(e["payload"].get("preflight") == pre["seq"]
                               for e in self.fleet.rollout_evidence("trial_revoked", job_id=job_id)):
            self.fleet.record_rollout_evidence("trial_revoked", build_id=pre["build_id"],
                                               build_digest=pre["build_digest"], job_id=job_id,
                                               payload={"preflight": pre["seq"], "reasons": reasons})
        return {"valid": not reasons, "reasons": reasons, "remaining": remaining, "preflight": pre["seq"],
                "expires_at": pre["expires_at"]}

    def consume_trial(self, job_id: str, n: int) -> None:
        t = self.trial_authority(job_id)
        if t["valid"]:
            self.fleet.record_rollout_evidence("trial_use", job_id=job_id, payload={
                "n": int(n), "preflight": t["preflight"]})

    # -- the one decision ----------------------------------------------------------------------------------------
    def authorize(self, job_id: str, n_actions: int, *, audit: bool = True) -> dict:
        """May a model-origin proposal of `n_actions` act on `job_id` right now? mode is shadow|trial|active."""
        if self.kill_switch_active():
            return {"mode": "shadow", "allowed": False, "reasons": ["kill_switch"]}
        if self.requested_mode() == "active":
            elig = self.eligibility()
            if elig["eligible"]:
                return {"mode": "active", "allowed": True, "reasons": []}
        t = self.trial_authority(job_id, audit=audit)
        if t["valid"]:
            if n_actions > t["remaining"]:
                return {"mode": "trial", "allowed": False, "reasons": ["trial_cap_exhausted"]}
            return {"mode": "trial", "allowed": True, "reasons": [], "remaining": t["remaining"]}
        if t["reasons"] != ["no_trial_preflight"]:  # a trial existed and is blocked/revoked: say why, fail closed
            return {"mode": "trial", "allowed": False, "reasons": t["reasons"]}
        return {"mode": "shadow", "allowed": False, "reasons": ["shadow_mode"]}

    def status(self, job_id: str | None = None) -> dict:
        elig = self.eligibility()
        out = {"requested_mode": self.requested_mode(), "kill_switch": elig["kill_switch"],
               "active_eligibility": elig, "build": self.build,
               "instance_verifiable": self.instance_probe is not None, "promotion": self.promotion_status()}
        if job_id:
            out["trial"] = self.trial_authority(job_id, audit=False)
            out["decision"] = self.authorize(job_id, 1, audit=False)
        return out


class Arbiter:
    """One place where every supervisor's facts meet (FLEET-17). Authority, by route:
      inner   - heartbeat/lifecycle/recovery facts: the controller's deterministic tick already acted; recorded only.
      doctor  - an individual stall belongs to the doctor/Sol path; it only escalates to the outer loop as a
                `repeated_stall` after STALL_ESCALATE_AT stalls on the same lane.
      advisory- Jev and efficiency: evidence in the packet, never an action and never a tech-lead trigger.
      outer   - trajectory/deadline direction facts: the facts that wake the tech lead.
    v2.5 narrow amendment (FLEET-33, D-11) for the per-lane fleet Jev (`source="jev_fleet"`): ONLY
    `lanes_overlapping`, `runtime_misbehaving`, and `lane_off_task` after a watcher/advisor already acted on that
    lane are outer facts; `lane_stalled` is a stall (the doctor path, `repeated_stall` after STALL_ESCALATE_AT);
    every other fleet Jev flag is advisory. The legacy `jev` source stays advisory.
    v2.6 (SUP-07): the Arbiter serves every job, single-worker included (job-level facts, no lane). Two new general
    triggers reach the tech lead (`outer`): the model-free code-fact floor (`source="code_facts"`, its KINDS only)
    and Jev's general `permission_confusion` / `thrash` (`source="jev"`). Every other route is unchanged.
    The settle window (one direction change per window) is enforced by `JobController.apply_proposal`."""

    INNER = frozenset({"heartbeat", "recovery", "lifecycle"})
    ADVISORY = frozenset({"jev", "efficiency"})
    OUTER = frozenset({"trajectory", "deadline"})
    JEV_FLEET = "jev_fleet"
    JEV_PROMOTED = frozenset({"lanes_overlapping", "runtime_misbehaving"})
    LANE_ACTORS = frozenset({"watcher", "advisor"})
    STALL_ESCALATE_AT = 2
    MAX_FACTS = 50
    CODE_FACTS = "code_facts"
    YIELD = "yield"
    PAUSE = "pause"  # spec 2026-10-13: a worker pause, a 35-min snapshot or an urgent fact is the tech lead's to read
    PAUSE_KINDS = frozenset({"worker_paused", "pause_snapshot", "urgent_fact"})
    OWNERSHIP = "ownership"  # Plan 1: a reviewable blocker on a RUNNING job
    CODE_FACT_KINDS = frozenset({"finish_check_unverified", "finish_bounces_exhausted", "new_failures_repeated",
                                 "permission_ask_repeated", "review_watchdog_expired"})
    JEV_GENERAL = frozenset({"permission_confusion", "thrash"})

    def __init__(self, store: Store, clock: Callable[[], float] = time.time):
        self.store, self.clock = store, clock

    def _doc(self, job_id: str) -> dict:
        return self.store.kv_get(job_id, _ARBITER, None) or {"seq": 0, "facts": [], "stalls": {}}

    def submit(self, job_id: str, source: str, kind: str, *, lane: str | None = None, **data) -> dict:
        with self.store.immediate() as tx:
            doc = tx.kv_get(job_id, _ARBITER, None) or {"seq": 0, "facts": [], "stalls": {}}
            acted = doc.setdefault("acted", {})
            if source == self.JEV_FLEET and kind == "lane_stalled":
                kind_for_stall = "stall"
            else:
                kind_for_stall = kind
            if source == self.OWNERSHIP:
                route = "outer"
            elif source == self.JEV_FLEET:
                if kind == "lane_stalled":
                    route = "doctor"
                elif kind in self.JEV_PROMOTED or (kind == "lane_off_task" and lane and acted.get(lane)):
                    route = "outer"
                else:
                    route = "advisory"
            elif source == self.YIELD:  # v2.7 Phase 40: a worker that stopped or asked is the tech lead's to read
                route = "outer" if kind in ("worker_stopped", "worker_asking") else "advisory"
            elif source == self.PAUSE:
                route = "outer" if kind in self.PAUSE_KINDS else "advisory"
            elif source == self.CODE_FACTS:
                route = "outer" if kind in self.CODE_FACT_KINDS else "advisory"
            elif source == "jev" and kind in self.JEV_GENERAL:
                route = "outer"
            elif source in self.INNER:
                route = "inner"
            elif source in self.ADVISORY:
                route = "advisory"
            elif source in self.OUTER:
                route = "outer"
            elif source == "doctor" or kind == "stall":
                route = "doctor"
            else:
                route = "advisory"  # unknown sources can inform, never trigger
            if kind_for_stall == "stall" and route in ("inner", "doctor"):
                route = "doctor"
            if source in self.LANE_ACTORS and lane:   # remembered beyond the bounded fact list
                acted[lane] = acted.get(lane, 0) + 1
            doc["seq"] += 1
            fact = {"id": doc["seq"], "at": self.clock(), "source": source, "kind": kind, "lane": lane,
                    "route": route, "data": data, "reviewed": route != "outer"}
            doc["facts"] = (doc["facts"] + [fact])[-self.MAX_FACTS:]
            if kind_for_stall == "stall" and lane:
                n = doc["stalls"][lane] = doc["stalls"].get(lane, 0) + 1
                if n >= self.STALL_ESCALATE_AT and n % self.STALL_ESCALATE_AT == 0:
                    doc["seq"] += 1
                    doc["facts"].append({"id": doc["seq"], "at": self.clock(), "source": "trajectory",
                                         "kind": "repeated_stall", "lane": lane, "route": "outer",
                                         "data": {"stalls": n}, "reviewed": False})
            tx.kv_set(job_id, _ARBITER, doc)
        return fact

    def pending_for_tech_lead(self, job_id: str) -> list[dict]:
        return [f for f in self._doc(job_id)["facts"] if f["route"] == "outer" and not f["reviewed"]]

    def mark_reviewed(self, job_id: str) -> None:
        with self.store.immediate() as tx:
            doc = tx.kv_get(job_id, _ARBITER, None)
            if doc:
                for f in doc["facts"]:
                    f["reviewed"] = True
                tx.kv_set(job_id, _ARBITER, doc)

    def summary(self, job_id: str) -> dict:
        doc = self._doc(job_id)
        by = {}
        for f in doc["facts"]:
            by[f["route"]] = by.get(f["route"], 0) + 1
        return {"by_route": by, "stalls": doc["stalls"],
                "recent": [{"source": f["source"], "kind": f["kind"], "lane": f["lane"]} for f in doc["facts"][-8:]]}


class JobController:
    def __init__(self, store: Store, repo: Path | str, runner, adapter_factory: Callable[..., Any], *,
                 base_dir: Path | str, probe: Callable[[str, str, dict | None], str],
                 base_sha: str | None = None, controller_id: str = "controller",
                 clock: Callable[[], float] = time.time, settle_s: float = 300.0,
                 diagnose_fn: Callable[[str, str | None, str], Any] | None = None,
                 rollout: "RolloutGate | None" = None, frame_handler: Callable | None = None):
        self.store = store
        self.fleet = FleetStore(store)
        self.repo = Path(repo)
        self.runner = runner
        self.factory = adapter_factory
        self.base_dir = Path(base_dir)
        self.probe = probe
        self.controller_id = controller_id
        self._base_sha = base_sha
        self._adapters: dict[tuple[str, str], Any] = {}
        self.last_observation: dict[tuple[str, str], dict] = {}   # (job, lane) -> last adapter observation summary
        self._last_observations: dict[str, dict[str, dict]] = {}
        self.integration = None  # Phase 28: optional IntegrationWriter that receives lane candidates
        # optional (job_id, lane_id, frame, adapter): answers a current lane's permission_request under the
        # job's authority policy. None = permission requests are only recorded as accepted frames (as before).
        self.permission_handler: Callable[[str, str, Frame, Any], None] | None = None
        self.clock = clock
        self.settle_s = settle_s          # at most one outer-loop direction change per window
        self.diagnose_fn = diagnose_fn    # the doctor/Sol seam (injected); None = recorded request only
        self.frame_handler = frame_handler  # optional Runner policy for lane permissions and worker events
        self.arbiter = Arbiter(store, clock)
        # Phase 29-02: the default gate knows no build and no trial, so it can only ever say `shadow`.
        self.rollout = rollout if rollout is not None else RolloutGate(store, clock=clock)

    # -- helpers ---------------------------------------------------------------------------------

    def _kv(self, job_id: str, key: str, default):
        return self.store.kv_get(job_id, key, default)

    def _update_kv(self, job_id: str, key: str, fn, default):
        with self.store.immediate() as tx:
            val = tx.kv_get(job_id, key, default)
            out = fn(val)
            tx.kv_set(job_id, key, val)
        return out

    def base_sha(self) -> str:
        if self._base_sha is None:
            res = self.runner.run(["git", "rev-parse", "HEAD"], cwd=self.repo)
            if res.exit_code != 0:
                raise RuntimeError(f"cannot pin base: {res.stderr}")
            self._base_sha = res.stdout.strip()
        return self._base_sha

    def parent_worktree(self, job_id: str) -> Path:
        return self.base_dir / job_id

    def _graph(self, job_id: str) -> fg.Graph | None:
        view = self.fleet.graph_view(job_id)
        if not view or view["mode"] != "fleet":
            return None
        tasks = [fg.Task(id=t["id"], objective=t["objective"], done_when=tuple(t["done_when"]),
                         depends_on=tuple(t["depends_on"]), owns=tuple(t["owns"]), required=t["required"],
                         priority=t["priority"]) for t in view["tasks"]]
        return fg.Graph(tasks=tasks, implicit=view["implicit"], fallback_reason=view["reason"] or "",
                        errors=list(view["errors"]))

    def limits(self, job_id: str) -> dict:
        snap = self.fleet.get_snapshot(job_id) or {}
        lim = snap.get("limits", {})
        return {"max_lanes": fl.effective_max_lanes(lim.get("max_lanes")),
                "job_recovery_ceiling": int(lim.get("job_recovery_ceiling", fl.DEFAULT_JOB_RECOVERY_CEILING))}

    def fence(self, job_id: str) -> dict | None:
        return self._kv(job_id, _FENCE, None)

    def _event(self, job_id: str, what: str, lane_id=None, attempt_no=None, **evidence) -> None:
        self.fleet.append_event(job_id, what=what, actor=self.controller_id, rule="fleet_controller",
                                judgment="code", evidence=evidence, lane_id=lane_id, attempt_no=attempt_no)

    def adapter(self, job_id: str, lane_id: str):
        return self._adapters.get((job_id, lane_id))

    # -- admission ---------------------------------------------------------------------------------

    def _governed(self, job_id: str, runtime: str | None) -> bool:
        """Capability checks apply when the job opted in, or the runtime has any recorded capability observation."""
        return bool(self._kv(job_id, _CAP_GATE, False) or (runtime and self.fleet.runtime_capabilities(runtime)))

    def capability_check(self, job_id: str, runtime: str | None, action: str, *, force: bool = False,
                         lane_id: str | None = None) -> list[str]:
        """Missing qualified capabilities for `action` on `runtime` (empty = allowed), re-read from the Store on
        every call. `force` checks even an ungoverned runtime (tech-lead actions always do)."""
        if not force and not self._governed(job_id, runtime):
            return []
        ok, missing = self.fleet.runtime_can(runtime, action)
        if not ok:
            self._event(job_id, "capability_denied", lane_id, None, runtime=runtime, action=action, missing=missing)
        return missing

    def _assignment_ok(self, job_id: str, lane_id: str) -> tuple[bool, dict | None, list[str]]:
        """Revalidate roster qualification for the lane's latest immutable assignment (if any)."""
        rows = self.fleet.assignments(job_id, lane_id)
        if not rows:
            return True, None, []
        a = rows[-1]
        roster = self.fleet.get_roster(a["registry_hash"])
        entry = roster.get(a["roster_id"]) if roster else None
        if entry is None or not self.fleet.dispatchable(entry):
            return False, a, []
        missing = self.capability_check(job_id, entry.runtime, "start", lane_id=lane_id)
        return not missing, a, missing

    def admit(self, job_id: str) -> list[dict]:
        """Start every eligible task up to the lane allowance. Returns one result dict per attempt
        (`started` or `skipped` with a reason). Legacy jobs and fenced jobs start nothing."""
        graph = self._graph(job_id)
        if graph is None:
            return []
        if self.fence(job_id):
            return [{"task": None, "status": "blocked", "reason": "dispatch_fenced"}]
        if self._kv(job_id, _BLOCKED, None):
            return [{"task": None, "status": "blocked", "reason": "dispatch_blocked_ambiguous_identity"}]
        cap = self.limits(job_id)["max_lanes"]
        states = fl.task_states(self.store, job_id)
        done = {t for t, s in states.items() if s == "finished"}
        if self._kv(job_id, _INT_GATE, False):
            # Phase 28: a lane `done` frame only means "finished"; dependents wait for the integrated candidate.
            done &= set(self.fleet.integrated_tasks(job_id))
        live = fl.live(self.store, job_id)
        lane_task = {r["lane_id"]: r["task_id"] for r in self.store._conn.execute(
            "SELECT lane_id, task_id FROM fleet_lanes WHERE job_id=?", (job_id,))}
        active = {lane_task[l["lane_id"]] for l in live if l["lane_id"] in lane_task}
        active |= {t for t, s in states.items() if s in ("exhausted", "failed")}
        # a lane that already had an attempt and is not finished is recovery's, never admission's
        active |= {task for lane, task in lane_task.items() if fl.get(self.store, job_id, lane) is not None} - done
        held = set(integration_holds(self.store, job_id))
        if held:  # transitive dependents of a blocked/conflicted integration stay held
            changed = True
            while changed:
                changed = False
                for t in graph.tasks:
                    if t.id not in held and held & set(t.depends_on):
                        held.add(t.id)
                        changed = True
        active |= held
        results = []
        for task in graph.ready_tasks(done=frozenset(done), active=frozenset(active)):
            if len(fl.live(self.store, job_id)) >= cap:
                results.append({"task": task.id, "status": "skipped", "reason": "lane_cap"})
                break
            results.append(self._start_lane(job_id, task, graph))
        return results

    def _brief(self, job_id: str, task: fg.Task) -> str:
        lines = [f"Task {task.id}: {task.objective}", "Done when: " + "; ".join(task.done_when)]
        if task.owns:
            lines.append("You own only: " + ", ".join(task.owns))
        for item in self._kv(job_id, _GLOBAL_STEER, []):
            lines.append("Constraint: " + item["text"])
        return "\n".join(lines)

    def _start_lane(self, job_id: str, task: fg.Task, graph: fg.Graph | None = None) -> dict:
        lane_id = lane_id_for(task.id)
        ok, assignment, missing = self._assignment_ok(job_id, lane_id)
        if assignment is None and self.store.kv_get(job_id, "fleet_mode_requested", False):
            snap = self.fleet.get_snapshot(job_id) or {}
            roster = self.fleet.get_roster(snap.get("registry_hash"))
            pin = (snap.get("pins") or {}).get(lane_id)
            choices = [roster.get(pin)] if roster is not None and pin else (
                [e for e in roster.entries if e.id in set(snap.get("eligible", [])) and "worker" in e.roles]
                if roster is not None else [])
            choices = [e for e in choices if e is not None and "worker" in e.roles
                       and e.id in set(snap.get("eligible", []))]
            if not choices:
                return {"task": task.id, "lane": lane_id, "status": "skipped", "reason": "no_eligible_worker"}
            selected = choices[0]
            self.fleet.record_assignment(job_id, lane_id, roster_id=selected.id, actor="initial_policy",
                                         reason="pinned" if pin else "first eligible roster worker",
                                         policy_rule="frozen_snapshot_default")
            assignment = self.fleet.assignments(job_id, lane_id)[-1]
            ok, assignment, missing = self._assignment_ok(job_id, lane_id)
        if not ok:
            self._event(job_id, "lane_not_dispatched", lane_id, reason="roster_unqualified", missing=missing)
            return {"task": task.id, "lane": lane_id, "status": "skipped",
                    "reason": "roster_unqualified" if not missing else "capability_unqualified: " + ",".join(missing)}
        path, branch = wt.lane_path(self.base_dir, job_id, lane_id), wt.lane_branch(job_id, lane_id)
        exists = self.store._conn.execute("SELECT 1 FROM fleet_lanes WHERE job_id=? AND lane_id=?",
                                          (job_id, lane_id)).fetchone()
        if not exists:
            self.fleet.record_lane(job_id, lane_id, task_id=task.id, worktree_path=str(path), branch=branch)
        try:
            lease = fl.acquire_lane(self.store, job_id, lane_id, owner=self.controller_id,
                                    max_lanes=self.limits(job_id)["max_lanes"])
        except fl.LeaseHeld as exc:
            return {"task": task.id, "lane": lane_id, "status": "skipped", "reason": f"lease_held: {exc}"}
        epoch, attempt_no = lease["epoch"], lease["attempt_no"]
        try:
            path = wt.create_lane_worktree(self.repo, job_id, lane_id, self.runner, self.base_dir,
                                           base_sha=self.base_sha(), store=None)
        except Exception as exc:  # noqa: BLE001 - nothing was spawned: confirmed no writer
            fl.release(self.store, job_id, lane_id, epoch, confirmed=True,
                       evidence={"reason": "worktree_failed_before_spawn", "error": str(exc)[:200]})
            return {"task": task.id, "lane": lane_id, "status": "failed", "reason": f"worktree: {exc}"}
        adapter = self.factory(job_id=job_id, lane_id=lane_id, task_id=task.id, attempt_no=attempt_no, epoch=epoch,
                               lease_check=lambda frame, j=job_id: self.frame_is_current(j, frame))
        try:
            ref = adapter.start(self._brief(job_id, task), path)
        except Exception as exc:  # noqa: BLE001 - a worker may have spawned: only a probe can confirm
            self._adapters[(job_id, lane_id)] = adapter
            self._stop_lane(job_id, lane_id, fl.get(self.store, job_id, lane_id), reason=f"start_failed: {exc}"[:200])
            return {"task": task.id, "lane": lane_id, "status": "failed", "reason": f"start: {exc}"}
        identity = {"ref": ref, "attempt": attempt_no, "epoch": epoch}
        extra = getattr(adapter, "process_identity", None)
        if callable(extra):
            identity.update(extra())
        self._adapters[(job_id, lane_id)] = adapter
        fl.bind(self.store, job_id, lane_id, epoch, identity)
        # No frozen assignment: the adapter's own `requested` identity (what it actually asked its runtime for).
        started = assignment or getattr(adapter, "requested", None) or {}
        self.fleet.record_attempt(
            job_id, lane_id, attempt_no, provider=started.get("provider", "unknown"),
            runtime=started.get("runtime", "unknown"), model=started.get("model", "unknown"),
            adapter_version="1", herdr_ref=str(ref))
        self._event(job_id, "lane_started", lane_id, attempt_no, epoch=epoch, worktree=str(path))
        return {"task": task.id, "lane": lane_id, "status": "started", "epoch": epoch, "attempt_no": attempt_no,
                "worktree": str(path)}

    # -- write containment ---------------------------------------------------------------------------------

    def authorize_write(self, job_id: str, lane_id: str, path: Path | str) -> bool:
        """A lane (and its native children) may write only inside its own worktree: never the parent
        integration worktree and never a sibling lane's."""
        own = wt.lane_path(self.base_dir, job_id, lane_id)
        if wt.path_within(self.parent_worktree(job_id), path) and not wt.path_within(own, path):
            return False
        return wt.path_within(own, path)

    def authorize_claim(self, job_id: str, lane_id: str, claim: str) -> bool:
        """Only the lane's own declared claims; a worker cannot claim a sibling task's paths."""
        row = self.store._conn.execute("SELECT task_id FROM fleet_lanes WHERE job_id=? AND lane_id=?",
                                       (job_id, lane_id)).fetchone()
        graph = self._graph(job_id)
        if row is None or graph is None:
            return False
        mine = next((t for t in graph.tasks if t.id == row["task_id"]), None)
        return bool(mine) and not any(
            fg.claims_conflict((claim,), t.owns) for t in graph.tasks if t.id != mine.id) and any(
            claim == c or claim.startswith(c.rstrip("/") + "/") for c in mine.owns)

    # -- frames ---------------------------------------------------------------------------------------------

    def frame_is_current(self, job_id: str, frame: Frame) -> bool:
        if frame.v < 2 or frame.job != job_id:
            return False
        lease = fl.get(self.store, job_id, frame.lane)
        if not lease or lease["state"] not in fl.HELD:
            return False
        row = self.store._conn.execute("SELECT task_id FROM fleet_lanes WHERE job_id=? AND lane_id=?",
                                       (job_id, frame.lane)).fetchone()
        if not row or row["task_id"] != frame.task or lease["epoch"] != frame.epoch \
                or lease["attempt_no"] != frame.attempt:
            return False
        runtime = (self.fleet.lane_runtime(job_id, frame.lane) or {}).get("runtime")
        if runtime in (None, "unknown"):
            from steward import fleet_launch
            if self.store.kv_get(job_id, fleet_launch.LAUNCH_KEY):
                runtime = "claude-fabric"  # a `launch --fleet` lane with no assignment is a Claude fabric lane
        if runtime in ("claude-fabric", "muse", "agy", "pi"):
            # v2.5 runtimes: the sender is enforced where the frame enters, by the lane's own adapter (LaneWorker
            # drops any frame not from `shim:<lane>`; structured adapters build their frames in-process).
            return True
        # Codex remediation lanes (and lanes with no recorded runtime): the controller checks the runtime's sender.
        expected_sender = {"claude-code": f"shim:{frame.lane}", "codex": f"codex:{frame.lane}"}.get(runtime)
        if runtime in (None, "unknown"):
            expected_sender = f"shim:{frame.lane}"
        return expected_sender is not None and frame.sender == expected_sender

    def accept_frame(self, job_id: str, frame: Frame) -> FrameDecision:
        """Identity is validated against the Store lease BEFORE any state is touched."""
        if frame.v < 2:
            if is_fleet_job(self.store, job_id):
                self._event(job_id, "frame_rejected", None, None, reason="legacy_frame_in_fleet_job", seq=frame.seq)
                return FrameDecision(False, "legacy_frame_in_fleet_job")
            return FrameDecision(True, "legacy")
        if not self.frame_is_current(job_id, frame):
            self._event(job_id, "frame_rejected", frame.lane, frame.attempt, reason="stale_identity",
                        epoch=frame.epoch, seq=frame.seq, type=frame.type, sender=frame.sender)
            return FrameDecision(False, "stale_identity")
        request_ids = []
        if frame.type == "permission_request":
            request_ids = [str(frame.body.get("request_id") or "")]
        elif frame.type == "event" and frame.body.get("kind") == "pending_permissions":
            request_ids = [str(p.get("request_id") or "") for p in frame.body.get("pending") or []]
        for rid in request_ids:
            if rid:
                route_key = f"{frame.lane}:{frame.attempt}:{frame.epoch}:{rid}"
                self._update_kv(job_id, "fleet_permission_routes",
                                lambda routes: routes.setdefault(route_key, {
                                    "request_id": rid, "lane": frame.lane, "attempt": frame.attempt,
                                    "epoch": frame.epoch}), {})
        if self.frame_handler is not None and (frame.type == "permission_request" or
                (frame.type == "event" and frame.body.get("kind") in
                 ("permission_answered", "permission_unknown", "pending_permissions", "batch"))):
            try:
                self.frame_handler(job_id, frame, self._adapters.get((job_id, frame.lane)))
            except Exception as exc:  # noqa: BLE001 - a supervisor fault must not bypass or crash the arbiter
                self._event(job_id, "fleet_frame_policy_error", frame.lane, frame.attempt,
                            type=frame.type, error=f"{type(exc).__name__}: {exc}"[:200])
        try:
            if frame.type == "done":
                fl.set_task_state(self.store, job_id, frame.lane, frame.epoch, frame.attempt, frame.task, "finished")
                self._route_candidate(job_id, frame)
            elif frame.type == "error":
                fl.mark(self.store, job_id, frame.lane, frame.epoch, "stopping",
                        {"reason": "worker_error", "body": frame.body})
            elif frame.type == "ack" and "rev" in frame.body:
                self._ack_receipt(job_id, frame)
            elif (frame.type == "permission_request" and self.permission_handler is not None
                  and self.frame_handler is None):  # one policy answers a request: never both wirings
                self.permission_handler(job_id, frame.lane, frame, self._adapters.get((job_id, frame.lane)))
        except fl.LeaseFenced:
            return FrameDecision(False, "stale_identity")
        return FrameDecision(True, "accepted")

    def _route_candidate(self, job_id: str, frame: Frame) -> None:
        """A `done` frame that names a committed candidate SHA hands it to the IntegrationWriter's admission.
        The frame is a claim only; the writer validates it against Git and the Store before anything else."""
        sha = frame.body.get("candidate_sha") if isinstance(frame.body, dict) else None
        if not sha or self.integration is None:
            return
        res = self.integration.submit_candidate(job_id, frame.task, candidate_sha=str(sha),
                                                evidence_ref=str(frame.body.get("evidence_ref") or ""))
        self._event(job_id, "candidate_" + res["status"], frame.lane, frame.attempt, task=frame.task,
                    reason=res.get("reason"))

    # -- steering ---------------------------------------------------------------------------------------------

    def steer(self, job_id: str, text: str, *, request_id: str, lane_id: str | None = None, rev: int = 0) -> list[dict]:
        """Lane-targeted or job-wide steering. Each lane gets its own delivery and receipt (lane, attempt,
        epoch); a repeated request id returns the original receipt and delivers nothing twice."""
        live = {l["lane_id"]: l for l in fl.live(self.store, job_id)}
        if lane_id is None:
            def add(items):
                if not any(i["request_id"] == request_id for i in items):
                    items.append({"request_id": request_id, "text": text, "rev": rev})
            self._update_kv(job_id, _GLOBAL_STEER, add, [])
            targets = sorted(live)
        else:
            targets = [lane_id]
        out = []
        for lane in targets:
            key = f"{request_id}:{lane}"
            receipts = self._kv(job_id, _RECEIPTS, {})
            if key in receipts:
                out.append(receipts[key])
                continue
            lease = live.get(lane)
            adapter = self._adapters.get((job_id, lane))
            missing = self.capability_check(job_id, (self.fleet.lane_runtime(job_id, lane) or {}).get("runtime"),
                                            "steer", lane_id=lane) if lease is not None else []
            if lease is None or adapter is None or missing:
                receipt = {"key": key, "lane": lane, "stage": "undeliverable", "rev": rev,
                           **({"detail": "capability_unqualified: " + ",".join(missing)} if missing else {})}
            else:
                res = adapter.deliver(f"[rev={rev}] {text}")
                receipt = {"key": key, "lane": lane, "attempt": lease["attempt_no"], "epoch": lease["epoch"],
                           "rev": rev, "stage": "delivered" if res.ok else "failed", "detail": res.detail}
            self._update_kv(job_id, _RECEIPTS, lambda r, k=key, v=receipt: r.setdefault(k, v), {})
            self._event(job_id, "steer_" + receipt["stage"], lane, receipt.get("attempt"), request_id=request_id,
                        epoch=receipt.get("epoch"))
            out.append(receipt)
        return out

    def _ack_receipt(self, job_id: str, frame: Frame) -> None:
        def upd(receipts):
            for r in receipts.values():
                if (r.get("lane"), r.get("attempt"), r.get("epoch"), r.get("rev")) == (
                        frame.lane, frame.attempt, frame.epoch, frame.body.get("rev")) and r["stage"] == "delivered" \
                        and frame.body.get("stage") == "acknowledged":
                    r["stage"] = "acknowledged"
        self._update_kv(job_id, _RECEIPTS, upd, {})

    def receipts(self, job_id: str) -> dict:
        return self._kv(job_id, _RECEIPTS, {})

    # -- observation / tick -------------------------------------------------------------------------------------

    def observe(self, job_id: str) -> list[FrameDecision]:
        decisions = []
        summaries = {}
        last_activity = self._kv(job_id, _LAST_ACTIVITY, {})
        if not isinstance(last_activity, dict):
            last_activity = {}
        for lease in fl.live(self.store, job_id):
            lane = lease["lane_id"]
            adapter = self._adapters.get((job_id, lane))
            if adapter is None:
                continue
            obs = adapter.observe()
            self.last_observation[(job_id, lease["lane_id"])] = {"lifecycle": obs.lifecycle, "ts": obs.ts,
                                                                 "pid_alive": obs.pid_alive}
            lane_decisions = []
            for frame in obs.events:
                decision = self.accept_frame(job_id, frame)
                decisions.append(decision)
                lane_decisions.append(decision)
            activity = any(decision.accepted and frame.type == "event"
                           and frame.body.get("kind") in ("activity", "done")
                           for frame, decision in zip(obs.events, lane_decisions))
            prior = last_activity.get(lane)
            if (not isinstance(prior, dict) or prior.get("attempt_no") != lease["attempt_no"]
                    or prior.get("epoch") != lease["epoch"]):
                prior = {"attempt_no": lease["attempt_no"], "epoch": lease["epoch"], "at": self.clock()}
            elif activity:
                prior["at"] = self.clock()
            last_activity[lane] = prior
            waiting = obs.lifecycle in ("idle", "blocked") or any(
                decision.accepted and frame.type == "permission_request"
                for frame, decision in zip(obs.events, lane_decisions))
            summaries[lane] = {"lifecycle": obs.lifecycle or "unknown", "activity": activity,
                               "waiting": waiting, "last_activity_at": prior["at"],
                               "attempt_no": lease["attempt_no"], "epoch": lease["epoch"]}
            current = fl.get(self.store, job_id, lane)
            if obs.lifecycle == "exited" and current and current["state"] == "running":
                self._stop_lane(job_id, lane, current, reason="worker_exited")
        if last_activity != self._kv(job_id, _LAST_ACTIVITY, {}):
            self.store.kv_set(job_id, _LAST_ACTIVITY, last_activity)
        self._last_observations[job_id] = summaries
        return decisions

    def tick(self, job_id: str) -> dict:
        """Deterministic, model-free: observe lanes, recover dead unfinished ones (bounded), admit more."""
        decisions = self.observe(job_id)
        recovered = []
        states = fl.task_states(self.store, job_id)
        for lease in fl.live(self.store, job_id):
            row = self.store._conn.execute("SELECT task_id FROM fleet_lanes WHERE job_id=? AND lane_id=?",
                                           (job_id, lease["lane_id"])).fetchone()
            if lease["state"] == "running" and states.get(row["task_id"]) == "finished":
                self._stop_lane(job_id, lease["lane_id"], lease, reason="task_finished")
        if not self.fence(job_id):
            states = fl.task_states(self.store, job_id)
            lanes = self.store._conn.execute(
                "SELECT lane_id, task_id FROM fleet_lanes WHERE job_id=? ORDER BY lane_id", (job_id,)).fetchall()
            for row in lanes:
                lease = fl.get(self.store, job_id, row["lane_id"])
                if lease and lease["state"] == "stopped" and states.get(row["task_id"]) not in (
                        "finished", "exhausted", "failed"):
                    recovered.append(self.recover_lane(
                        job_id, row["lane_id"], f"auto:{row['lane_id']}:{lease['attempt_no']}", reason="worker_dead"))
        return {"frames": decisions, "recovered": recovered, "admitted": self.admit(job_id),
                "observations": dict(self._last_observations.get(job_id, {}))}

    # -- stop / pause / resume -----------------------------------------------------------------------------------

    def _stop_lane(self, job_id: str, lane_id: str, lease: dict, *, reason: str) -> dict:
        """Stop one lane. Confirmed only when the adapter reports stopped AND the process-tree probe sees
        the process gone; silence, idleness or lease expiry never count."""
        epoch = lease["epoch"]
        if lease["state"] == "reserved" or lease["state"] == "running":
            lease = fl.mark(self.store, job_id, lane_id, epoch, "stopping", {"reason": reason})
        adapter = self._adapters.get((job_id, lane_id))
        stopped = None
        if adapter is not None:
            try:
                stopped = bool(adapter.stop())
            except Exception:  # noqa: BLE001
                stopped = False
        self._record_usage(job_id, lane_id, lease, adapter)
        status = self.probe(job_id, lane_id, lease.get("identity"))
        confirmed = status == DEAD and stopped is not False
        evidence = {"reason": reason, "adapter_stopped": stopped, "probe": status}
        fl.release(self.store, job_id, lane_id, epoch, confirmed=confirmed,
                   evidence=evidence if confirmed else {**evidence, "uncertain": True})
        if confirmed:
            self._adapters.pop((job_id, lane_id), None)
        return {"lane": lane_id, "attempt": lease["attempt_no"], "epoch": epoch,
                "status": "confirmed" if confirmed else "uncertain", "evidence": evidence}

    def _control(self, job_id: str, action: str, request_id: str) -> dict:
        done = self._kv(job_id, _CONTROLS, {})
        if request_id in done:
            return done[request_id]
        # 1. persist the dispatch fence before contacting any lane
        with self.store.immediate() as tx:
            fence = {"action": action, "request_id": request_id, "state": "stopping" if action == "stop" else "pausing"}
            tx.kv_set(job_id, _FENCE, fence)
        self._event(job_id, f"fleet_{action}_fence", None, None, request_id=request_id)
        # 2. address every live lane
        lanes = {}
        for lease in fl.live(self.store, job_id):
            lane = lease["lane_id"]
            akey = f"{request_id}:{lane}"
            actions = self._kv(job_id, _LANE_ACTIONS, {})
            if akey in actions:
                lanes[lane] = actions[akey]
                continue
            if action == "stop":
                res = self._stop_lane(job_id, lane, lease, reason="parent_stop")
            else:
                adapter = self._adapters.get((job_id, lane))
                if adapter is not None:
                    adapter.interrupt()
                res = {"lane": lane, "attempt": lease["attempt_no"], "epoch": lease["epoch"],
                       "status": "paused" if adapter is not None else "uncertain"}
            self._update_kv(job_id, _LANE_ACTIONS, lambda a, k=akey, v=res: a.setdefault(k, v), {})
            self._event(job_id, f"lane_{action}_{res['status']}", lane, res["attempt"], request_id=request_id,
                        epoch=res["epoch"])
            lanes[lane] = res
        uncertain = [l for l, r in lanes.items() if r["status"] == "uncertain"]
        held = [l["lane_id"] for l in fl.live(self.store, job_id)]
        if action == "stop":
            state = "stopped" if not held else "stopping"
        else:
            state = "paused" if not uncertain else "pausing"
        with self.store.immediate() as tx:
            tx.kv_set(job_id, _FENCE, {"action": action, "request_id": request_id, "state": state})
        result = {"request_id": request_id, "action": action, "state": state, "lanes": lanes,
                  "uncertain": sorted(set(uncertain) | (set(held) if action == "stop" else set()))}
        self._update_kv(job_id, _CONTROLS, lambda d: d.setdefault(request_id, result), {})
        return result

    def pause(self, job_id: str, request_id: str) -> dict:
        return self._control(job_id, "pause", request_id)

    def stop(self, job_id: str, request_id: str) -> dict:
        return self._control(job_id, "stop", request_id)

    def resume(self, job_id: str, request_id: str) -> dict:
        done = self._kv(job_id, _CONTROLS, {})
        if request_id in done:
            return done[request_id]
        fence = self.fence(job_id)
        if fence and fence["action"] == "stop":
            result = {"request_id": request_id, "action": "resume", "state": fence["state"], "refused": "stopped"}
        elif any(l["state"] == "uncertain" for l in fl.live(self.store, job_id)):
            result = {"request_id": request_id, "action": "resume", "state": fence["state"] if fence else "running",
                      "refused": "uncertain_lane"}
        else:
            with self.store.immediate() as tx:
                tx.kv_set(job_id, _FENCE, None)
            result = {"request_id": request_id, "action": "resume", "state": "running"}
            self._event(job_id, "fleet_resume", None, None, request_id=request_id)
        self._update_kv(job_id, _CONTROLS, lambda d: d.setdefault(request_id, result), {})
        return result

    # -- restart reconciliation ----------------------------------------------------------------------------------

    def reconcile(self, job_id: str) -> dict:
        """After a controller restart: reattach lanes whose persisted identity probes alive, fence ones
        confirmed dead, and keep dispatch blocked (no duplicate worker) while any identity is ambiguous."""
        out = {"reattached": [], "fenced": [], "ambiguous": []}
        for lease in fl.live(self.store, job_id):
            lane, epoch = lease["lane_id"], lease["epoch"]
            status = self.probe(job_id, lane, lease.get("identity"))
            if status == DEAD:
                fl.release(self.store, job_id, lane, epoch, confirmed=True,
                           evidence={"reason": "restart_probe_dead", "probe": status})
                self._adapters.pop((job_id, lane), None)
                out["fenced"].append(lane)
            elif status == ALIVE and lease["state"] == "running" and lease.get("identity"):
                attached = (job_id, lane) in self._adapters
                if not attached:
                    row = self.store._conn.execute("SELECT task_id FROM fleet_lanes WHERE job_id=? AND lane_id=?",
                                                   (job_id, lane)).fetchone()
                    adapter = self.factory(job_id=job_id, lane_id=lane, task_id=row["task_id"],
                                           attempt_no=lease["attempt_no"], epoch=epoch,
                                           lease_check=lambda frame, j=job_id: self.frame_is_current(j, frame))
                    attach = getattr(adapter, "attach", None)
                    runtime = (self.fleet.lane_runtime(job_id, lane) or {}).get("runtime")
                    allowed = not self.capability_check(job_id, runtime, "reattach", lane_id=lane)
                    attached = bool(attach(lease["identity"]["ref"])) if callable(attach) and allowed else False
                    if attached:
                        self._adapters[(job_id, lane)] = adapter
                if attached:
                    out["reattached"].append(lane)
                else:
                    self._ambiguous(job_id, lane, epoch, "attach_failed", out)
            else:
                self._ambiguous(job_id, lane, epoch, status, out)
        blocked = bool(out["ambiguous"]) or any(l["state"] == "uncertain" and l["lane_id"] not in out["fenced"]
                                                for l in fl.live(self.store, job_id))
        with self.store.immediate() as tx:
            tx.kv_set(job_id, _BLOCKED, {"lanes": out["ambiguous"]} if blocked else None)
        return out

    def _ambiguous(self, job_id: str, lane: str, epoch: int, why: str, out: dict) -> None:
        lease = fl.get(self.store, job_id, lane)
        if lease["state"] != "uncertain":
            fl.mark(self.store, job_id, lane, epoch, "uncertain", {"reason": "restart_ambiguous", "detail": why})
        out["ambiguous"].append(lane)

    # -- bounded recovery ------------------------------------------------------------------------------------------

    def recover_lane(self, job_id: str, lane_id: str, action_id: str, *, reason: str) -> dict:
        """Replace a lane's worker: confirmed stop first, then at most 2 recoveries per lane and a job-wide
        ceiling. Idempotent by action id. An exhausted lane blocks its descendants only."""
        results = self._kv(job_id, _RECOVERY_RESULTS, {})
        if action_id in results:
            return results[action_id]
        if self.fence(job_id):
            return {"action_id": action_id, "lane": lane_id, "status": "blocked", "reason": "dispatch_fenced"}
        row = self.store._conn.execute("SELECT task_id FROM fleet_lanes WHERE job_id=? AND lane_id=?",
                                       (job_id, lane_id)).fetchone()
        lease = fl.get(self.store, job_id, lane_id)
        if row is None or lease is None:
            return {"action_id": action_id, "lane": lane_id, "status": "refused", "reason": "unknown_lane"}
        missing = self.capability_check(job_id, (self.fleet.lane_runtime(job_id, lane_id) or {}).get("runtime"),
                                        "recover", lane_id=lane_id)
        if missing:  # replacing a worker needs a stop we can confirm; an unqualified runtime is not touched
            return {"action_id": action_id, "lane": lane_id, "status": "blocked",
                    "reason": "capability_unqualified: " + ",".join(missing)}
        if lease["state"] in fl.HELD:
            stop = self._stop_lane(job_id, lane_id, lease, reason=reason)
            if stop["status"] != "confirmed":
                return {"action_id": action_id, "lane": lane_id, "status": "blocked", "reason": "stop_unconfirmed"}
        granted, why = fl.spend_recovery(self.store, job_id, lane_id, action_id,
                                         job_max=self.limits(job_id)["job_recovery_ceiling"])
        if not granted:
            fl.force_task_state(self.store, job_id, row["task_id"], "exhausted")
            result = {"action_id": action_id, "lane": lane_id, "status": "exhausted", "reason": why}
        else:
            graph = self._graph(job_id)
            task = next((t for t in graph.tasks if t.id == row["task_id"]), None) if graph else None
            started = self._start_lane(job_id, task, graph) if task else {"status": "failed", "reason": "no_task"}
            result = {"action_id": action_id, "lane": lane_id, "status": started["status"], "reason": "recovered",
                      "start": started}
        self._update_kv(job_id, _RECOVERY_RESULTS, lambda d: d.setdefault(action_id, result), {})
        return result

    # -- usage provenance (FLEET-18) ----------------------------------------------------------------------------------

    def _record_usage(self, job_id: str, lane_id: str, lease: dict, adapter) -> None:
        """One honest usage event per stop: unknown tokens/cost stay null with an unknown source."""
        try:
            raw = adapter.usage() if adapter is not None and callable(getattr(adapter, "usage", None)) else None
        except Exception:  # noqa: BLE001 - telemetry must never break a stop
            raw = None
        rt_ = self.fleet.lane_runtime(job_id, lane_id) or {}
        self._event(job_id, "lane_usage", lane_id, lease.get("attempt_no"), runtime=rt_.get("runtime"),
                    model=rt_.get("model"), usage=honest_usage(raw))

    def usage_summary(self, job_id: str) -> dict:
        """Aggregate recorded usage. `tokens`/`cost_usd` are totals of KNOWN figures only; `complete` is False if
        any attempt was unknown, so a partial total is never presented as the whole."""
        rows = [e for e in self.fleet.fleet_events(job_id) if e["what"] == "lane_usage"]
        toks, costs, unknown = [], [], 0
        for e in rows:
            u = json.loads(e["evidence"]).get("usage", {})
            if u.get("tokens") is None and u.get("cost_usd") is None:
                unknown += 1
            if u.get("tokens") is not None:
                toks.append(u["tokens"])
            if u.get("cost_usd") is not None:
                costs.append(u["cost_usd"])
        total = {"tokens": sum(toks) if toks else None, "cost_usd": sum(costs) if costs else None}
        lim = self.limits_raw(job_id)
        return {"attempts": len(rows), "unknown_attempts": unknown, "complete": bool(rows) and unknown == 0
                and len(toks) == len(rows) and len(costs) == len(rows), **total,
                "limits": limit_status(lim, total)}

    def limits_raw(self, job_id: str) -> dict:
        return (self.fleet.get_snapshot(job_id) or {}).get("limits", {})

    # -- proposal arbiter (FLEET-15..17) --------------------------------------------------------------------------------

    def lane_tasks(self, job_id: str) -> dict:
        return {r["lane_id"]: r["task_id"] for r in self.store._conn.execute(
            "SELECT lane_id, task_id FROM fleet_lanes WHERE job_id=? ORDER BY lane_id", (job_id,))}

    def owner_decisions(self, job_id: str) -> list:
        return self._kv(job_id, _OWNER_DECISIONS, [])

    def contract_digest(self, job_id: str) -> str:
        """Digest of everything the owner fixed: goal, finish line, boundaries, authority and the deadline
        commitment. A model proposal is bound to it; any owner change invalidates stale advice."""
        from steward import deadline
        from steward.contract import load_contract
        c = load_contract(self.store, job_id)
        job = self.store.get_job(job_id)
        body = {"goal": c.goal if c else (job.goal if job else ""), "done_when": c.done_when if c else [],
                "boundaries": c.boundaries if c else [], "authority": c.authority if c else [],
                "revision": c.revision if c else 0, "deadline": deadline.commitment(self.store, job_id)}
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:24]

    def bindings(self, job_id: str) -> dict:
        """The revisions a proposal must have observed, read together."""
        cur = self.fleet.current_graph(job_id)
        snap = self.fleet.get_snapshot(job_id) or {}
        return {"contract": self.contract_digest(job_id), "graph_rev": cur[0] if cur else None,
                "registry_hash": snap.get("registry_hash"), "snapshot_hash": snap.get("snapshot_hash"),
                "steer_rev": self.store.get_revision(job_id),
                "leases": {l["lane_id"]: l["epoch"] for l in fl.live(self.store, job_id)}}

    def record_advice(self, job_id: str, trigger_id: str, outcome: str, reason: str) -> None:
        self.fleet.append_event(job_id, what=f"tech_lead_advice_{outcome}", actor="tech_lead_supervisor",
                                rule="tech_lead_fail_open", judgment="code",
                                evidence={"trigger_id": trigger_id, "reason": reason})

    def _unacked_human_steer(self, job_id: str) -> bool:
        from steward.steering import Steering
        return any(i.get("status") in ("received", "delivered") and not i.get("expired_at")
                   for i in Steering(self.store).all_instructions(job_id))

    def _full_tasks(self, job_id: str) -> list[fg.Task]:
        cur = self.fleet.current_graph(job_id)
        view = self.fleet.graph_view(job_id)
        extra = {r["task_id"]: r for r in self.store._conn.execute(
            "SELECT task_id, parent_task, evidence_json FROM fleet_tasks WHERE job_id=? AND graph_rev=?",
            (job_id, cur[0]))}
        return [fg.Task(id=t["id"], objective=t["objective"], done_when=tuple(t["done_when"]),
                        depends_on=tuple(t["depends_on"]), owns=tuple(t["owns"]), required=t["required"],
                        priority=t["priority"], parent=extra[t["id"]]["parent_task"],
                        evidence=tuple(json.loads(extra[t["id"]]["evidence_json"]))) for t in view["tasks"]]

    def _revise_graph(self, job_id: str, tasks: list[fg.Task], *, replaces: dict | None = None) -> int:
        """Record graph revision N+1 and compare-and-swap it current. Raises on validation or a lost race."""
        cur = self.fleet.current_graph(job_id)
        row = fleet_graph_row(self.store, job_id)
        tasks = sorted(tasks, key=lambda t: (t.priority, t.id))
        errors = fg.validate_graph(tasks)
        if errors:
            raise ValueError("invalid_graph: " + "; ".join(errors[:3]))
        g = fg.Graph(tasks=tasks, plan_blob_id=row["plan_blob_id"], implicit=False)
        rev = max(r[0] for r in self.store._conn.execute(
            "SELECT MAX(graph_rev) FROM fleet_graph_revisions WHERE job_id=?", (job_id,))) + 1
        self.fleet.record_graph(job_id, rev, g, replaces=replaces)
        if not self.fleet.set_current_graph(job_id, rev, expected_version=cur[1]):
            raise RuntimeError("graph_changed_concurrently")
        return rev

    def _graph_has_evidence(self, job_id: str) -> bool:
        return bool(self.fleet.candidates(job_id) or self.fleet.integration_history(job_id))

    def _task_started(self, job_id: str, task_id: str) -> bool:
        return task_id in self.lane_tasks(job_id).values()

    def _validate(self, job_id: str, prop, act, ctx: dict) -> str | None:
        """Reason this ONE action must be rejected against the shared snapshot `ctx`, else None. Pure reads."""
        tasks = ctx["tasks"]
        by_id = {t.id: t for t in tasks}
        snap = ctx["snap"]
        lane_of = {v: k for k, v in self.lane_tasks(job_id).items()}
        p = act.params

        def task_ref(ref):
            if ref is None:
                return None
            if ref.startswith("lane-") and ref[5:] in by_id:
                return by_id[ref[5:]]
            return by_id.get(ref)

        if act.type in ("split", "assign", "reorder", "steer"):
            if ctx["unacked_human"]:
                return "pending_unacked_steer"
            if self.fence(job_id):
                return "dispatch_fenced"
        if act.type == "split":
            t = by_id.get(act.target)
            if t is None:
                return "unknown_task"
            if self._graph_has_evidence(job_id):
                return "graph_has_integration_evidence"
            if self._task_started(job_id, t.id):
                return "task_started"
            if ctx["replans"] >= MAX_REPLANS:
                return "replan_cap"
            if not t.owns:
                return "parent_has_no_scope"
            ids = [c["id"] for c in p["children"]]
            if len(set(ids)) != len(ids) or any(i in by_id for i in ids):
                return "child_id_conflict"
            if len(tasks) - 1 + len(ids) > MAX_TASKS:
                return "too_many_tasks"
            try:
                claims = [fg.normalize_claim(o) for c in p["children"] for o in c["owns"]]
            except ValueError as exc:
                return f"bad_claim: {exc}"
            for claim in claims:
                if not any(claim == o or claim.startswith(o.rstrip("/") + "/") for o in t.owns):
                    return f"child_claim_outside_parent_scope: {claim}"
            covered = {d for c in p["children"] for d in c["done_when"]}
            if not set(t.done_when) <= covered:
                return "children_do_not_cover_outcome"
            for c in p["children"]:
                for d in c["depends_on"]:
                    if d not in ids and (d not in by_id or d == t.id):
                        return f"unknown_dependency: {d}"
            return None
        if act.type == "assign":
            t = by_id.get(act.target)
            if t is None:
                return "unknown_task"
            lane = lane_id_for(t.id)
            if t.id in self.fleet.integrated_tasks(job_id) or fl.task_states(self.store, job_id).get(t.id) == "finished":
                return "task_finished"
            pins = snap.get("pins", {})
            if lane in pins or t.id in pins:
                return "pinned"
            roster = self.fleet.get_roster(snap["registry_hash"])
            entry = roster.get(p["roster_id"]) if roster else None
            if entry is None or p["roster_id"] not in snap.get("eligible", []):
                return "not_eligible"
            if "worker" not in entry.roles:
                return "not_a_worker"
            if not self.fleet.dispatchable(entry):
                return "roster_unqualified"
            missing = self.capability_check(job_id, entry.runtime, "assign", force=True, lane_id=lane)
            if missing:
                return "capability_unqualified: " + ",".join(missing)
            cur = self.fleet.assignments(job_id, lane)
            if cur and cur[-1]["roster_id"] == p["roster_id"]:
                return "no_change"
            live = fl.get(self.store, job_id, lane)
            if live and live["state"] in fl.HELD:
                old = (self.fleet.lane_runtime(job_id, lane) or {}).get("runtime")
                missing = self.capability_check(job_id, old, "stop", force=True, lane_id=lane)
                if missing:
                    return "capability_unqualified(stop): " + ",".join(missing)
            return None
        if act.type == "reorder":
            names = p["order"]
            integrated = set(self.fleet.integrated_tasks(job_id))
            ts = []
            for n in names:
                t = by_id.get(n)
                if t is None:
                    return f"unknown_task: {n}"
                if self._task_started(job_id, n):
                    return f"task_started: {n}"
                if not set(t.depends_on) <= integrated and t.depends_on:
                    return f"not_ready: {n}"
                ts.append(t)
            if self._graph_has_evidence(job_id):
                return "graph_has_integration_evidence"
            for i, a in enumerate(ts):
                for b in ts[i + 1:]:
                    if fg.claims_conflict(a.owns, b.owns):
                        return f"not_independent: {a.id},{b.id}"
            if ctx["replans"] >= MAX_REPLANS:
                return "replan_cap"
            return None
        if act.type == "steer":
            if act.params["reach"] == "task":
                t = task_ref(act.target)
                if t is None:
                    return "unknown_task"
                lanes = [lane_of.get(t.id)]
            else:
                lanes = [l["lane_id"] for l in fl.live(self.store, job_id)]
            if not [l for l in lanes if l]:
                return "no_live_lane"
            for lane in lanes:
                lease = fl.get(self.store, job_id, lane) if lane else None
                if lease is None or lease["state"] not in fl.HELD:
                    return f"lane_not_live: {lane}"
                missing = self.capability_check(job_id, (self.fleet.lane_runtime(job_id, lane) or {}).get("runtime"),
                                                "steer", force=True, lane_id=lane)
                if missing:
                    return f"capability_unqualified({lane}): " + ",".join(missing)
                if any(r.get("lane") == lane and r.get("stage") == "delivered" for r in self.receipts(job_id).values()):
                    return f"pending_unacked_steer: {lane}"
            return None
        if act.type == "diagnose":
            ref = task_ref(act.target)
            if act.target and ref is None:
                return "unknown_task"
            lane = lane_of.get(ref.id) if ref else None
            lease = fl.get(self.store, job_id, lane) if lane else None
            tag = f"{lane}:{lease['attempt_no'] if lease else 0}"
            if tag in self._kv(job_id, _DIAGNOSES, {}):
                return "diagnosis_cap"
            return None
        if act.type == "owner_decision":
            if len([d for d in self.owner_decisions(job_id) if d.get("status") == "open"]) >= MAX_OPEN_DECISIONS:
                return "decision_cap"
            return None
        return "unknown_action"

    def _conflicts(self, prop) -> str | None:
        """Two structural actions on one task in one proposal contradict each other: reject the whole batch."""
        by: dict = {}
        for a in prop.actions:
            if a.type == "reorder":
                targets = a.params["order"]
            elif a.type in ("split", "assign"):
                targets = [(a.target or "").removeprefix("lane-")]
            else:
                continue
            for t in targets:
                by.setdefault(t, []).append(a.type)
        for t, kinds in sorted(by.items()):
            if len(kinds) > 1:
                return f"conflicting_actions: {'+'.join(kinds)} on {t}"
        return None

    def apply_proposal(self, job_id: str, raw, *, trigger_id: str | None = None,
                       shadow_reason: str | None = None) -> dict:
        """The sole validator/applier of tech-lead advice. Returns the durable receipt (also in the Store).
        Order: parse -> replay -> revision bindings (stale advice is discarded before any effect) ->
        per-action authority validation -> conflicts/settle window -> atomic apply."""
        from steward import fleet_tech_lead as tl
        def rec(pid, tid, outcome, reason, bindings, actions):
            row, fresh = self.fleet.record_proposal(job_id, pid, trigger_id=tid, outcome=outcome, reason=reason,
                                                    bindings=bindings, actions=actions)
            if fresh:
                self.fleet.append_event(
                    job_id, what=f"tech_lead_proposal_{outcome}", actor="tech_lead", rule="tech_lead_arbiter",
                    judgment=("model_informed_code_authorized" if outcome in ("applied", "partial") else "code"),
                    evidence={"proposal_id": pid, "trigger_id": tid, "reason": reason, "actions": actions})
            return {"outcome": row["outcome"], "reason": row["reason"], "proposal_id": pid, "actions": row["actions"],
                    "replayed": not fresh}

        try:
            prop = tl.parse_proposal(raw)
        except tl.ProposalError as exc:
            blob = raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True, default=str)
            pid = "malformed-" + hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]
            return rec(pid, trigger_id or "unknown", "malformed", str(exc), {}, [])
        prior = self.fleet.proposal(job_id, prop.proposal_id)
        if prior is not None:
            return {"outcome": prior["outcome"], "reason": prior["reason"], "proposal_id": prop.proposal_id,
                    "actions": prior["actions"], "replayed": True}
        if not is_fleet_job(self.store, job_id):
            return rec(prop.proposal_id, prop.trigger_id, "rejected", "legacy_job", {}, [])
        if shadow_reason:   # D-13: recorded with what it WOULD do; nothing is validated into effect
            acts = [{"key": a.key, "type": a.type, "status": "shadowed", "reason": shadow_reason}
                    for a in prop.actions]
            return rec(prop.proposal_id, prop.trigger_id, "shadowed", shadow_reason,
                       {"mode": "shadow", "would_act": [a.to_dict() for a in prop.actions]}, acts)
        auth = self.rollout.authorize(job_id, len(prop.actions))
        if not auth["allowed"]:
            shadow = auth["reasons"][0] == "shadow_mode"
            acts = [{"key": a.key, "type": a.type, "status": "shadowed" if shadow else "rejected",
                     "reason": ",".join(auth["reasons"])} for a in prop.actions]
            return rec(prop.proposal_id, prop.trigger_id, "shadowed" if shadow else "rejected",
                       ",".join(auth["reasons"]), {"mode": auth["mode"]}, acts)
        now = self.bindings(job_id)
        obs = prop.observed
        stale = [k for k in ("contract", "graph_rev", "registry_hash", "steer_rev") if obs.get(k) != now.get(k)]
        if obs.get("snapshot_hash") is not None and obs["snapshot_hash"] != now["snapshot_hash"]:
            stale.append("snapshot_hash")
        stale += [f"lease:{lane}" for lane, ep in obs.get("leases", {}).items() if now["leases"].get(lane) != ep]
        if stale:
            return rec(prop.proposal_id, prop.trigger_id, "discarded_stale", "stale_revision: " + ",".join(stale),
                       {"observed": obs, "current": now}, [])
        ctx = {"tasks": self._full_tasks(job_id), "snap": self.fleet.get_snapshot(job_id) or {},
               "unacked_human": self._unacked_human_steer(job_id),
               "replans": (self._kv(job_id, _OUTER, {}) or {}).get("replans", 0)}
        results = []
        for a in prop.actions:
            why = self._validate(job_id, prop, a, ctx)
            results.append({"key": a.key, "type": a.type, "status": "rejected" if why else "validated",
                            "reason": why or ""})
        direction = [a for a in prop.actions if a.type in ("split", "assign", "reorder", "steer")]
        conflict = self._conflicts(prop)
        outer = self._kv(job_id, _OUTER, {}) or {}
        if conflict:
            for r in results:
                r["status"], r["reason"] = "rejected", r["reason"] or "proposal_conflict"
            return rec(prop.proposal_id, prop.trigger_id, "rejected", conflict, {"observed": obs}, results)
        if any(r["status"] == "rejected" for r in results):
            return rec(prop.proposal_id, prop.trigger_id, "rejected", "action_rejected", {"observed": obs}, results)
        if direction and outer.get("last_at") is not None and self.clock() - outer["last_at"] < self.settle_s:
            for r in results:
                r["status"], r["reason"] = "rejected", "settle_window"
            return rec(prop.proposal_id, prop.trigger_id, "rejected", "settle_window", {"observed": obs}, results)
        if auth["mode"] == "trial":  # the capped authority is spent when the actions are attempted
            self.rollout.consume_trial(job_id, len(prop.actions))
        # apply (validation above was against one snapshot; each step is idempotent by proposal+action key)
        failed = False
        for a, r in zip(prop.actions, results):
            if failed:
                r["status"], r["reason"] = "skipped", "earlier_action_failed"
                continue
            if self.rollout.kill_switch_active():  # enforced before EVERY action, not only at proposal entry
                r["status"], r["reason"] = "skipped", "kill_switch"
                failed = True
                continue
            try:
                r.update(self._apply_action(job_id, prop, a, ctx))
            except Exception as exc:  # noqa: BLE001 - recorded per action; remaining actions are skipped
                r["status"], r["reason"] = "failed", f"{type(exc).__name__}: {exc}"[:200]
            failed = r["status"] == "failed"
        if direction:
            with self.store.immediate() as tx:
                st = tx.kv_get(job_id, _OUTER, {}) or {}
                st["last_at"] = self.clock()
                st["replans"] = st.get("replans", 0) + sum(1 for a in prop.actions if a.type in ("split", "reorder"))
                tx.kv_set(job_id, _OUTER, st)
        outcome = "applied" if all(r["status"] == "applied" for r in results) else (
            "partial" if any(r["status"] == "applied" for r in results) else "rejected")
        return rec(prop.proposal_id, prop.trigger_id, outcome, "" if outcome == "applied" else "action_failed",
                   {"observed": obs, "applied_at": self.bindings(job_id)}, results)

    def _apply_action(self, job_id: str, prop, act, ctx: dict) -> dict:
        p, key = act.params, f"{prop.proposal_id}:{act.key}"
        by_id = {t.id: t for t in ctx["tasks"]}
        if act.type == "split":
            t = by_id[act.target]
            kids = []
            for c in p["children"]:
                kids.append(fg.Task(id=c["id"], objective=c["objective"], done_when=tuple(c["done_when"]),
                                    depends_on=tuple(sorted(set(t.depends_on) | set(c["depends_on"]))),
                                    owns=tuple(sorted({fg.normalize_claim(o) for o in c["owns"]})),
                                    required=t.required, priority=t.priority, parent=t.id))
            kid_ids = [k.id for k in kids]
            rest = [fg.Task(**{**x.__dict__, "depends_on": tuple(sorted(
                (set(x.depends_on) - {t.id}) | (set(kid_ids) if t.id in x.depends_on else set())))})
                for x in ctx["tasks"] if x.id != t.id]
            rev = self._revise_graph(job_id, rest + kids, replaces={t.id: kid_ids})
            return {"status": "applied", "graph_rev": rev, "children": kid_ids}
        if act.type == "reorder":
            names = p["order"]
            base = min(by_id[n].priority for n in names)
            new = {n: base + i for i, n in enumerate(names)}
            out = []
            for x in ctx["tasks"]:
                if x.id in new:
                    pr = new[x.id]
                elif x.priority >= base:
                    pr = x.priority + len(names)
                else:
                    pr = x.priority
                out.append(fg.Task(**{**x.__dict__, "priority": pr}))
            return {"status": "applied", "graph_rev": self._revise_graph(job_id, out), "order": names}
        if act.type == "assign":
            t = by_id[act.target]
            lane = lane_id_for(t.id)
            lease = fl.get(self.store, job_id, lane)
            restart = None
            if lease and lease["state"] in fl.HELD:
                stop = self._stop_lane(job_id, lane, lease, reason=f"reassign:{key}")
                if stop["status"] != "confirmed":
                    return {"status": "failed", "reason": "stop_unconfirmed"}
                restart = True
            n = self.fleet.record_assignment(job_id, lane, roster_id=p["roster_id"], actor="tech_lead",
                                             reason=prop.rationale[:200] or "tech lead proposal",
                                             policy_rule="tech_lead_proposal")
            out = {"status": "applied", "assignment_rev": n}
            if restart:
                rec_ = self.recover_lane(job_id, lane, f"reassign:{key}", reason="reassign")
                out["recovery"] = rec_["status"]
                if rec_["status"] not in ("started",):
                    out["status"], out["reason"] = "failed", f"restart_{rec_['status']}"
            return out
        if act.type == "steer":
            lane = None
            if p["reach"] == "task":
                lane = lane_id_for((act.target or "").removeprefix("lane-"))
            text = f"[{p['verb']}] {p['text']}"
            receipts = self.steer(job_id, text, request_id=key, lane_id=lane, rev=self.store.get_revision(job_id))
            bad = [r for r in receipts if r["stage"] not in ("delivered", "acknowledged")]
            return {"status": "failed" if bad or not receipts else "applied", "reason": "undeliverable" if bad else "",
                    "receipts": [{"lane": r["lane"], "stage": r["stage"]} for r in receipts]}
        if act.type == "diagnose":
            lane = lane_id_for((act.target or "").removeprefix("lane-")) if act.target else None
            lease = fl.get(self.store, job_id, lane) if lane else None
            tag = f"{lane}:{lease['attempt_no'] if lease else 0}"
            self._update_kv(job_id, _DIAGNOSES, lambda d, t=tag: d.setdefault(t, key), {})
            self._event(job_id, "fleet_diagnosis_requested", lane, None, key=key, question=p["question"])
            ref = None
            if self.diagnose_fn is not None:
                ref = self.diagnose_fn(job_id, lane, p["question"])
            return {"status": "applied", "diagnosis_ref": ref if isinstance(ref, (str, int, type(None))) else str(ref)}
        card = {"id": key, "question": p["question"], "options": p["options"], "status": "open",
                "proposal_id": prop.proposal_id}
        self._update_kv(job_id, _OWNER_DECISIONS, lambda d, c=card: d.append(c) if not any(
            x["id"] == c["id"] for x in d) else None, [])
        self._event(job_id, "owner_decision_card", None, None, key=key, question=p["question"])
        return {"status": "applied", "card": key}
