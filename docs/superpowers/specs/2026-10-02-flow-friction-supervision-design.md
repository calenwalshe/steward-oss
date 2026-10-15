# Flow Friction Supervision

**Status:** Proposed for v2.3 planning  
**Date:** 2026-10-02  
**Architect:** GPT-6 Sol (xhigh)

## Problem

Steward can supervise individual job health and route explicit failures, but it does not retain a coherent signal when work is technically progressing while its direction or coordination is poor. Examples include a worker noticing it took the wrong angle, repeated retries or rebuilds, a supervisor correcting the plan, an interrupt/restart, a blocked handoff, or supervision that repeatedly has to re-steer a job. These episodes are useful evidence for later root-cause analysis and, when enabled, bounded remediation.

## Product outcome

JEV classifies job-local conversation and event context for flow friction. Steward stores a compact, redacted, deduplicated episode snapshot. In shadow mode this is observation only. In active mode, one smart investigation may explain the episode and recommend a bounded action; Steward applies it only through existing job, steering, watcher, recovery, and approval controls. Every proposal, no-op, action, and outcome is auditable.

## Architecture

```text
Existing per-job JEV observer (10s, RUNNING job)
    ├── existing liveness/rider questions and routes (unchanged)
    └── Flow rider: finite label + confidence + short evidence refs
              │
              ▼
     Flow episode gate (off / shadow / active)
       ├── uncertain/smooth/no new episode ──> no-op
       └── friction + dedupe key
              │
              ▼
     Store transaction: masked snapshot + one event
              │
       shadow ─┴─> stop (strict record-only)
              │ active
              ▼
     one smart Flow investigation per episode
       structured root cause + evidence + bounded proposal
              │
              ▼
     Steward deterministic policy/authority checks
       ├── reject / defer / human approval / no-op
       └── permitted existing mechanism (revisioned steer,
           watcher trust gate, or independently qualified recovery)
              │
              ▼
     append proposal, decision, receipt, adjudication/outcome events
```

The Flow classifier is a typed additional question in the current JEV request. It is not a new polling loop or a replacement for liveness triage. Keep its detector policy independent from legacy JEV shadow diagnosis, which may currently diagnose stalls and nudge workers. Existing ETA/deadline labels and behavior are preserved. The application-local rider cap is raised from seven to eight and covered by a regression test; this is not an asserted upstream API cap.

## Classification contract

Finite labels: `smooth`, `wrong_direction`, `self_correction`, `supervisor_correction`, `retry_or_rebuild`, `blocked_handoff`, `supervision_churn`, `uncertain`. JEV returns a confidence and bounded evidence references, not arbitrary commands. Missing, malformed, low-confidence, or unavailable classification is a no-op. Classification is per supervised job and uses only recent same-job transcript/context and relevant event metadata.

An episode has a stable identifier derived from job identity, source event sequence/context window and detector version. Store an immutable, versioned compact snapshot with redacted bounded excerpts or summaries, source event sequence references, job revision/SHA when available, label, confidence, and timestamp. Enforce uniqueness transactionally so overlapping observers and restarts cannot create duplicates. Never store secrets or unbounded transcripts in the episode record. Preserve source logs under existing retention/masking rules.

## Modes and action boundary

Flow mode is independent of the existing JEV liveness mode: `off`, `shadow`, `active`. `off` performs no Flow work. `shadow` classifies and records qualifying episodes and stops. `active` may invoke one separately budgeted smart investigator per episode. The investigator returns a strict schema (`root_cause`, `evidence_refs`, `recommended_action`, `instruction`) and cannot execute tools or steer directly. Its model is the configured supervisor model (Sol-quality path); do not add an ad hoc Luna call in this milestone.

Steward, not JEV or the investigator, validates evidence and maps the proposal to existing approved mechanisms. A Flow steering origin must be explicitly registered as nonhuman; steering uses current revision checks, stale-human-steer protection, idempotency and pending-ack fencing. Watcher changes use existing trust gates. Recovery is permitted only when an independent runner predicate qualifies it and existing recovery budgets allow it. Unknown actions, stale revisions, weak evidence, budget exhaustion, or missing infrastructure result in a recorded no-op/defer. Human authority over stop, permission, acceptance, merge, release and outbound contact remains unchanged. Flow cannot broaden authority or bypass approval.

## Learning and operations

Append events for detection, investigation, proposal, controller decision, action receipt, human adjudication and later outcome. Reuse existing event store and efficiency insights for analysis; do not collapse semantic flow labels into deterministic efficiency metrics. Keep raw job turns out of cross-job aggregation. Initial aggregate analysis is read-only and must preserve privacy boundaries. No automatic threshold tuning, policy promotion, or cross-job steering is in scope.

## Three-phase delivery

1. **Phase 23 — Observe and log:** add typed JEV Flow rider; bounded redacted episode snapshot; transactional dedupe; off/shadow/active config with shadow record-only; replay coverage and operator-visible events.
2. **Phase 24 — Investigate:** add one smart structured investigation per episode in active mode; separate it from legacy stall diagnosis and trajectory advisor; strict parsing, evidence validation, timeout/budget limits, and no action authority.
3. **Phase 25 — Bounded action and learning:** map only allowlisted recommendations to existing mechanisms and approval gates; record receipts/adjudication/outcome; add replay/evaluation and operational inspection of false positives, misses, duplicates, and action safety.

## Acceptance criteria

- Existing JEV triage and ETA/deadline rider outputs remain compatible; one existing observer request carries the Flow rider.
- In shadow, seeded friction creates exactly one redacted episode and cannot call an investigator, steer, recover, or modify watcher policy.
- Smooth, uncertain, unavailable, malformed, stale, and repeated evidence safely no-op; duplicate/concurrent/restarted processing remains idempotent.
- Active mode calls the investigator at most once per episode and validates a strict response under a bounded budget.
- Each allowed action goes through its existing authority mechanism; all other recommendations are rejected/deferred with a reason.
- Event records make detector evidence, decisions, approvals, action receipt and eventual outcome inspectable.
- Replay tests cover wrong-direction recovery of work, self-correction, supervisor correction, retry/rebuild, blocked handoff, churn, false positive, duplicate, restart, masking, unavailable model and every prohibited action boundary.

## Non-goals

No new global detector service; no raw transcript warehouse; no changes to core liveness classifications; no autonomous merge/accept/release/permission or outbound contact; no free-form investigator tools; no automatic model training or threshold/policy tuning; no replacement of existing efficiency watcher, doctor, trajectory ledger, or job/approval systems.
