"""Durable steering: instruction revisions with honest receipts (SP-9, SP-22).

Instruction revisions are persisted through `Store.kv_get`/`kv_set` (the
existing `kv` table) rather than a dedicated schema — no `steward/store.py`
change is needed for this phase. Each revision records a full lifecycle
(`received -> delivered -> acknowledged`); `delivered_at`/`acknowledged_at`
are set *only* from observed evidence (an adapter's `DeliveryResult.ok`, or
`STEWARD-ACK rev=<N>` seen in worker output) — never from intent alone.

Every instruction also carries provenance (API-02, SP-28): `origin`
(`cli|moshi|chatgpt|contract`), `human_instruction` (verbatim text as the
human/surface sent it — defaults to `text` when not given separately), and
`interpreted_instruction` (only set for an LLM-interpreted surface that
translated the human's words into what was actually delivered). `record`
takes `expected_revision` for optimistic concurrency (SP-27): a stale value
returns a `CONFLICT` receipt naming `current_revision` and records nothing
else — no event, no instruction row — though the request id is still stored
for the request-id-level dedupe (SP-20), so retrying the same stale request
returns the same conflict rather than re-evaluating it against a now-moved
target.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import replace

from steward.models import Judgment, Receipt
from steward.policy import classify_instruction, parse_avoid_globs, violations
from steward.store import Store, now_iso

_INSTRUCTIONS_KEY = "instructions"
_ACK_RE = re.compile(r"STEWARD-ACK rev=(\d+)")
_STAGE_ORDER = {"received": 0, "delivered": 1, "acknowledged": 2}

# `steward.contract.seed_boundary_instructions` seeds contract boundaries
# with a deterministic request id of this shape; recognizing the prefix here
# (rather than contract.py passing origin= explicitly) keeps origin
# attribution in one place without adding a steering-specific parameter to
# contract.py's call site.
_CONTRACT_REQUEST_PREFIX = "contract-boundary-"

# Origin "trajectory" (12-CONTEXT D8, TRJ-09): the model-informed controller
# conversion may only ever steer execution, never intent or permissions
# (SP-10) — `goal`/`avoid`/`authority` are refused outright.
_TRAJECTORY_ALLOWED_VERBS = frozenset({"focus", "nudge", "priority"})


# Origin "deadline" (v2.2, DL-13): the deadline supervisor may only remind (nonurgent priority/nudge), never change
# intent, scope or permissions, never interrupt, and never stack on an unacknowledged deadline/trajectory steer.
_DEADLINE_ALLOWED_VERBS = frozenset({"priority", "nudge"})
_UNACKED_FENCE_ORIGINS = frozenset({"deadline", "trajectory"})

# Origin "flow" (v2.3, FLOW-08): the Flow controller may only nudge or refocus execution, never change intent, scope,
# priority or permissions, never interrupt, and never stack on ANY unacknowledged instruction (a human's included).
_FLOW_ALLOWED_VERBS = frozenset({"nudge", "focus"})

# Origins that are code/model paths: they may never interrupt a running turn.
_NON_HUMAN_ORIGINS = frozenset({"trajectory", "steward", "contract", "diagnosis", "deadline", "flow", "tech_lead",
                                "check_run", "liveness"})


def _is_non_human(origin: str) -> bool:
    return origin in _NON_HUMAN_ORIGINS or origin.startswith("watcher:")


# Spec 2026-10-13 §4.5: with pause review on (contract pause != off) only these code origins reach the worker; a
# human/CLI origin always does. `check_run` is the worker's own requested check result and `deadline` keeps its role
# (spec §4.5 "code-only guards keep their current role"). Every other code origin reports to the tech lead instead.
PAUSE_ALLOWED_ORIGINS = frozenset({"tech_lead", "steward", "contract", "check_run", "deadline"})
PAUSE_FOLDED_ORIGINS = frozenset({"trajectory", "diagnosis", "flow", "liveness"})  # plus every "watcher:<name>"


def pause_folds(origin: str) -> bool:
    return origin in PAUSE_FOLDED_ORIGINS or origin.startswith("watcher:")


_REPORTED_VIOLATIONS_KEY = "hard_violation_paths_reported"


class Steering:
    def __init__(self, store: Store):
        self.store = store

    # -- persistence (kv-backed) -------------------------------------------

    def _load(self, job_id: str) -> list[dict]:
        return self.store.kv_get(job_id, _INSTRUCTIONS_KEY, default=[])

    def _save(self, job_id: str, instructions: list[dict]) -> None:
        self.store.kv_set(job_id, _INSTRUCTIONS_KEY, instructions)

    # -- recording ------------------------------------------------------

    def record(
        self,
        job_id: str,
        verb: str,
        text: str,
        request_id: str | None = None,
        *,
        origin: str | None = None,
        human_instruction: str | None = None,
        interpreted_instruction: str | None = None,
        expected_revision: int | None = None,
        urgent: bool = False,
        gated_by: str | None = None,
    ) -> Receipt:
        # `gated_by` is code-only (never reachable from the API/MCP/CLI surfaces): the watcher-authority gate is the one
        # non-human path allowed to interrupt, and only after it has re-derived its evidence itself (phase 15).
        gated_urgent = gated_by == "watcher_authority" and (origin or "").startswith("watcher:") and verb == "nudge"
        if urgent and _is_non_human(origin or "cli") and not gated_urgent:
            raise ValueError(
                f"origin={origin!r} may never send an urgent steer (only a human interrupts a worker mid-turn)"
            )
        if origin == "trajectory" and verb not in _TRAJECTORY_ALLOWED_VERBS:
            raise ValueError(
                f"origin=trajectory may only steer verbs {sorted(_TRAJECTORY_ALLOWED_VERBS)}, got {verb!r} "
                "(SP-10: the model can never change intent or permissions)"
            )

        if (origin or "").startswith("watcher:") and verb != "nudge":
            raise ValueError(f"origin={origin!r} may only send a nudge, got {verb!r} (custom watchers are advisory)")
        if origin == "deadline" and verb not in _DEADLINE_ALLOWED_VERBS:
            raise ValueError(
                f"origin=deadline may only steer verbs {sorted(_DEADLINE_ALLOWED_VERBS)}, got {verb!r} "
                "(the deadline supervisor never changes intent, scope or permissions)"
            )

        if origin == "check_run" and verb != "nudge":
            raise ValueError(f"origin=check_run may only send a nudge, got {verb!r} (a finished check run resumes the "
                             "worker with its result; it never changes intent or scope)")

        if origin == "flow" and verb not in _FLOW_ALLOWED_VERBS:
            raise ValueError(
                f"origin=flow may only steer verbs {sorted(_FLOW_ALLOWED_VERBS)}, got {verb!r} "
                "(Flow never changes intent, scope, priority or permissions)"
            )

        request_id = request_id or str(uuid.uuid4())
        existing = self.store.get_receipt(request_id)
        if existing is not None:
            return replace(existing, duplicate=True)

        if origin is not None and pause_folds(origin):
            from steward import pause_checkpoint

            if pause_checkpoint.pause_mode(self.store, job_id) != "off":
                return self._fold_to_fact(job_id, verb, text, request_id, origin=origin, urgent=urgent)

        if origin in ("deadline", "flow"):
            # Flow fences on every pending steer; standing contract boundaries are constraints, not pending steers.
            unacked = [i for i in self._load(job_id)
                       if (i.get("origin") != "contract" if origin == "flow" else i.get("origin") in _UNACKED_FENCE_ORIGINS)
                       and i.get("status") in ("received", "delivered") and not i.get("expired_at")]
            if unacked:
                receipt = Receipt(request_id=request_id, job_id=job_id, kind=verb, accepted=False,
                                  detail=f"SKIPPED pending_unacked: {unacked[-1]['origin']} instruction rev "
                                         f"{unacked[-1]['rev']} is not acknowledged yet", ts=now_iso())
                self.store.put_receipt(receipt)
                return receipt

        current_revision = self.store.get_revision(job_id)
        if expected_revision is not None and expected_revision != current_revision:
            ts = now_iso()
            receipt = Receipt(
                request_id=request_id,
                job_id=job_id,
                kind=verb,
                accepted=False,
                detail=(
                    f"CONFLICT current_revision={current_revision}: "
                    "job has changed since you inspected it"
                ),
                ts=ts,
            )
            # Still stored under request_id so a retried stale request
            # dedupes to this same conflict (SP-20) instead of re-evaluating
            # against a target that has since moved again. No event, no
            # instruction row, no revision bump.
            self.store.put_receipt(receipt)
            return receipt

        if origin is None:
            origin = "contract" if request_id.startswith(_CONTRACT_REQUEST_PREFIX) else "cli"
        if human_instruction is None:
            human_instruction = text

        kind, enforcement = classify_instruction(verb, text)
        instructions = self._load(job_id)
        rev = len(instructions) + 1
        ts = now_iso()
        new_revision = self.store.bump_revision(job_id)
        instructions.append(
            {
                "rev": rev,
                "verb": verb,
                "kind": kind,
                "enforcement": enforcement,
                "text": text,
                "scope": "job",
                "status": "received",
                "received_at": ts,
                "delivered_at": None,
                "acknowledged_at": None,
                "expired_at": None,
                "origin": origin,
                "human_instruction": human_instruction,
                "interpreted_instruction": interpreted_instruction,
                "job_revision": new_revision,
                **({"urgent": True} if urgent else {}),
            }
        )
        self._save(job_id, instructions)

        self.store.append_event(
            job_id,
            what=f"{verb} instruction recorded",
            evidence={
                "rev": rev,
                "kind": kind,
                "enforcement": enforcement,
                "text": text,
                "origin": origin,
                "human_instruction": human_instruction,
                "interpreted_instruction": interpreted_instruction,
                "revision": new_revision,
                **({"urgent": True} if urgent else {}),
            },
            rule="steering_instruction",
            judgment=provenance_judgment(origin),
        )
        if urgent:
            self.store.append_event(
                job_id,
                what="urgent_steer_sent",
                evidence={"rev": rev, "verb": verb, "text": text, "origin": origin},
                rule="urgent_steer",
                judgment=provenance_judgment(origin),
            )
        if verb == "goal":
            self.store.append_event(
                job_id,
                what="contract revision required",
                evidence={"rev": rev},
                rule="goal_steering_requires_contract_revision",
                judgment=Judgment.code,
            )

        receipt = Receipt(
            request_id=request_id,
            job_id=job_id,
            kind=verb,
            accepted=True,
            detail=self._receipt_text(
                rev,
                enforcement,
                delivered_at=None,
                acknowledged_at=None,
                revision=new_revision,
            ),
            ts=ts,
        )
        self.store.put_receipt(receipt)
        return receipt

    def _fold_to_fact(self, job_id: str, verb: str, text: str, request_id: str, *, origin: str,
                      urgent: bool) -> Receipt:
        """Spec 2026-10-13 §4.5: a non-TL steer on a pause job becomes a pending fact for the tech lead's next review.
        Liveness nudges and urgent (gated watcher) steers are urgent: they also wake the tech lead now."""
        from steward import pause_checkpoint

        wake = bool(urgent) or origin == "liveness"
        pause_checkpoint.append_fact(self.store, job_id, source=origin, kind=f"steer_{verb}", text=text,
                                     at=time.time(), urgent=wake)
        if wake:
            from steward.fleet_runtime import Arbiter

            Arbiter(self.store).submit(job_id, "pause", "urgent_fact", text=" ".join(str(text).split())[:400],
                                       origin=origin, urgent=True)
        self.store.append_event(job_id, what="steer_folded_to_fact",
                                evidence={"origin": origin, "verb": verb, "text": str(text)[:400], "urgent": wake},
                                rule="pause_review_single_writer", judgment=Judgment.code)
        receipt = Receipt(request_id=request_id, job_id=job_id, kind=verb, accepted=False,
                          detail=f"FOLDED pause_review: {origin} reports to the tech lead; recorded as a pending fact",
                          ts=now_iso())
        self.store.put_receipt(receipt)
        return receipt

    # -- lifecycle --------------------------------------------------------

    def pending(self, job_id: str) -> list[dict]:
        """Instructions received but not yet delivered."""
        return [i for i in self._load(job_id) if i["status"] == "received"]

    def all_instructions(self, job_id: str) -> list[dict]:
        return self._load(job_id)

    def mark_delivered(self, job_id: str, rev: int, result) -> dict:
        instructions = self._load(job_id)
        record = _find_rev(instructions, rev, job_id)
        if result.ok:
            record["status"] = "delivered"
            record["delivered_at"] = now_iso()
            self._save(job_id, instructions)
        self.store.append_event(
            job_id,
            what="instruction delivery observed",
            evidence={"rev": rev, "ok": result.ok, "detail": result.detail},
            rule="adapter_deliver_result",
            judgment=Judgment.code,
        )
        return record

    def scan_acks(self, job_id: str, output_text: str) -> list[int]:
        acked_revs = {int(m) for m in _ACK_RE.findall(output_text)}
        if not acked_revs:
            return []
        instructions = self._load(job_id)
        ts = now_iso()
        changed: list[int] = []
        for record in instructions:
            if record["rev"] in acked_revs and record["acknowledged_at"] is None:
                record["status"] = "acknowledged"
                record["acknowledged_at"] = ts
                changed.append(record["rev"])
        if changed:
            self._save(job_id, instructions)
            self.store.append_event(
                job_id,
                what="instruction acknowledged",
                evidence={"revs": changed},
                rule="observed_ack_output",
                judgment=Judgment.code,
            )
        return changed

    def mark_acknowledged(
        self, job_id: str, rev: int, stage: str, *, evidence: dict | None = None, at: float | None = None
    ) -> dict:
        """Advance an instruction's receipt lifecycle from an observed ack
        frame (`stage` = `"delivered"` | `"acknowledged"`), grounded in the
        fabric shim's own report rather than adapter-send success or scraped
        output (SP-9, SP-35, WRK-02) — the fabric-path counterpart to
        `scan_acks` (which stays text-based for the legacy `herdr-tui` path).

        Monotonic: a retransmitted or out-of-order frame that names a stage
        the instruction has already reached (or passed) is a no-op, so a
        duplicate `delivered` ack can never move `acknowledged` back down
        (SP-20-style idempotence for the ack stream, not just the request).
        """
        instructions = self._load(job_id)
        record = _find_rev(instructions, rev, job_id)
        if _STAGE_ORDER.get(record["status"], 0) >= _STAGE_ORDER.get(stage, 0):
            return record

        record["status"] = stage
        ts = now_iso()
        if at is not None and stage == "acknowledged":
            # the shim's own replay time (latency #2): when the model consumed it, not when the runner drained it
            from datetime import datetime, timezone

            try:
                ts = datetime.fromtimestamp(float(at), tz=timezone.utc).isoformat()
                record["consumed_at"] = ts
            except (TypeError, ValueError, OverflowError, OSError):
                pass
        if stage == "delivered":
            record["delivered_at"] = ts
        elif stage == "acknowledged":
            record["acknowledged_at"] = ts
        self._save(job_id, instructions)
        self.store.append_event(
            job_id,
            what=f"instruction {stage} (typed ack frame)",
            evidence={"rev": rev, "stage": stage, **(evidence or {})},
            rule="fabric_ack_frame",
            judgment=Judgment.code,
        )
        return record

    def active_constraints(self, job_id: str) -> list[dict]:
        return [i for i in self._load(job_id) if i.get("expired_at") is None]

    def check_compliance(self, job_id: str, worktree, runner) -> list[str]:
        hard_globs: list[str] = []
        for instruction in self.active_constraints(job_id):
            if instruction["kind"] == "policy" and instruction["enforcement"] == "hard":
                hard_globs.extend(g for g in parse_avoid_globs(instruction["text"]) if g not in hard_globs)
        if not hard_globs:
            return []

        diff_paths = self._diff_paths(job_id, worktree, runner)
        hits = violations(diff_paths, hard_globs)
        if hits:
            # One event per path: this runs every tick, and the same standing violation used to be re-logged each time
            # (278 events on 7 jobs in the efficiency audit). The block itself is still re-asserted every call.
            reported = list(self.store.kv_get(job_id, _REPORTED_VIOLATIONS_KEY) or [])
            fresh = [h for h in hits if h not in reported]
            if fresh:
                self.store.kv_set(job_id, _REPORTED_VIOLATIONS_KEY, reported + fresh)
                self.store.append_event(
                    job_id,
                    what="hard constraint violation detected",
                    evidence={"paths": fresh, "globs": hard_globs},
                    rule="hard_constraint_avoid",
                    judgment=Judgment.code,
                )
            self.store.kv_set(job_id, "closeout_blocked", True)
        return hits

    def _diff_paths(self, job_id: str, worktree, runner) -> list[str]:
        baseline_checks = self.store.get_checks(job_id, label="baseline")
        base = baseline_checks[0]["commit_sha"] if baseline_checks else "HEAD"

        diff = runner.run(["git", "diff", "--name-only", base], cwd=worktree)
        paths = [p for p in diff.stdout.splitlines() if p]

        status = runner.run(["git", "status", "--porcelain"], cwd=worktree)
        for line in status.stdout.splitlines():
            if line.startswith("??"):
                paths.append(line[3:].strip())

        return sorted(set(paths))

    # -- receipts ---------------------------------------------------------

    def receipt_text(self, job_id: str, rev: int) -> str:
        record = _find_rev(self._load(job_id), rev, job_id)
        return self._receipt_text(
            rev,
            record["enforcement"],
            delivered_at=record["delivered_at"],
            acknowledged_at=record["acknowledged_at"],
            revision=record.get("job_revision"),
        )

    @staticmethod
    def _receipt_text(
        rev: int,
        enforcement: str,
        *,
        delivered_at: str | None,
        acknowledged_at: str | None,
        revision: int | None = None,
    ) -> str:
        text = (
            f"Instruction revision {rev} recorded ({enforcement}). "
            f"Delivered: {delivered_at or 'pending'}. "
            f"Acknowledged: {acknowledged_at or 'pending'}."
        )
        if revision is not None:
            text += f" Job revision: {revision}."
        return text


def provenance_judgment(origin: str) -> Judgment:
    """The `steering_instruction` event's judgment, by origin (12-CONTEXT
    D8's provenance fix): `trajectory` is model-informed-but-code-authorized
    (the hysteresis policy decided, not the model directly); `steward`/
    `contract` are code; every other origin (a human, or a surface relaying a
    human's words) is `human`."""
    if origin in ("trajectory", "flow", "tech_lead") or origin.startswith("watcher:"):
        return Judgment.model_informed_code_authorized
    if origin in ("steward", "contract", "deadline", "check_run", "liveness"):
        return Judgment.code
    return Judgment.human


def _find_rev(instructions: list[dict], rev: int, job_id: str) -> dict:
    for record in instructions:
        if record["rev"] == rev:
            return record
    raise KeyError(f"no instruction revision {rev} for job {job_id}")
