# Deadline-aware supervised tasks — design spec (experimental)

Date: 2026-10-02 · Status: design, not built · Steward job 05fe42e9 (spec-only; no code, tests or `.planning/` changes)

This spec turns the owner's approved product decisions into an architecture for Steward. A deadline job runs as an
**adaptive supervised run with bounded autonomy**: Steward drafts a plan at once, starts work without waiting for
approval, checks in at milestone times, warns early, and adapts only within fixed bounds. The deadline and the core
outcome belong to the owner. The release is a review-ready candidate. The owner alone decides whether to merge it.

Labelled assumptions are marked **[A-n]** and collected in §14. Open design questions are in §15. Each question
names its recommended option, and this spec assumes that option.

---

## 1. Product decisions (captured verbatim in intent)

| # | Decision |
|---|---|
| PD-1 | A Steward job stays **one task with one deadline**. No decomposition into multiple jobs. |
| PD-2 | Given the task and deadline, Steward **immediately drafts an initial plan with milestone targets and starts useful work**. It does not block on a planning negotiation. |
| PD-3 | A clear **status sign: On track / At risk / Unlikely**, with an explanation of each change and its reasons. |
| PD-4 | **Scheduled check-ins at milestone target times, even when on track**. |
| PD-5 | **Earlier warnings** when evidence shows schedule risk. |
| PD-6 | Bounded autonomy. Steward may **reorder independent work, switch reversible approaches, stop unproductive retries, and drop only scope explicitly classified optional**. |
| PD-7 | Steward must **not silently change the core outcome or extend the deadline**. It surfaces infeasibility and useful tradeoffs and keeps doing authorized work, so the owner can steer or change the scope or deadline. |
| PD-8 | **Done = a review-ready candidate with verification evidence.** The owner alone decides whether to merge. |
| PD-9 | **JEV** may help classify tasks and inform rough estimate ranges. Observed milestones and durations calibrate these later. **Estimates are never overstated beyond what the data supports.** |
| PD-10 | Direction: adaptive supervised run, not a planner-only feature and not an unrestricted autonomous sprint. |
| PD-11 | **First release: one deadline job at a time.** |
| PD-12 | The deadline commitment and its milestone/risk status are **distinct from the general ETA and the efficiency watcher**. Reuse them, but do not duplicate their responsibilities. |

---

## 2. Approaches compared

| | A. Deadline planner | B. Autonomous sprint | **C. Adaptive supervised run (chosen)** |
|---|---|---|---|
| Shape | A plan and milestone schedule are produced up front and shown. Execution is an ordinary job. | The worker gets the deadline and full latitude to hit it: cut scope, change approach, decide done. | Steward drafts the plan and starts work at once. Code watches commitment vs evidence, checks in, warns, and allows only enumerated adaptations. |
| Starts work immediately | Usually no: the planner output invites a negotiation (violates PD-2). | Yes | Yes (PD-2) |
| Live status / early warning | None after planning: the schedule goes stale silently. | Only the worker's self-report. | Code-derived from committed evidence plus the ETA (PD-3, PD-5). |
| Scope / deadline integrity | Intact, but nothing enforces it during the run. | **Violated**: the worker may cut core scope or redefine done (PD-7). | Commitment is immutable except by the owner. Only owner-classified optional scope may be dropped (PD-6, PD-7). |
| Merge authority | Unchanged (autopilot may still merge). | Often bundled into "done". | Every auto-merge path is closed for deadline jobs (PD-8, §9). |
| Cost / complexity | Low | Low in Steward, high in risk | Medium. Reuses ETA, efficiency, doctor, trajectory, notify, questions and steering. |
| Failure mode | A plan nobody checks. | A fast, wrong deliverable. | Noise (too many check-ins) or false alarms. Bounded by the hysteresis and caps in §7. |

**Why C:** only C satisfies PD-2, PD-3/4/5 and PD-7 together. A is a subset of C (C's first minutes *are* a planner).
B's scope latitude is exactly what PD-6 bounds. C stays cheap because it adds **one code-only supervisor step plus
a durable commitment record**. It adds no new model loop: the worker drafts the plan, and Jev rides an existing call.

---

## 3. Vocabulary

- **Deadline commitment**: The owner's immutable-by-code record of *due time + core outcome + acceptance + scope
  classification*. Revised only by a human-judgment write. Each revision is kept (§5.1).
- **Plan**: the worker's mutable schedule. It lists items, milestones with target times, the approach, and
  adaptations. It is committed in the worktree and validated against the commitment (§5.2).
- **Item**: a piece of the outcome, classed `required` or `optional`. Only items marked optional by the
  commitment are droppable (§8).
- **Milestone**: a named checkpoint with a `target_at`. It is *met* only by code-verifiable evidence (§6.3). The
  last milestone is always `candidate`.
- **Candidate**: committed work at a resolvable SHA whose closeout verification passed (SP-16). This is "done" per PD-8.
- **Schedule status**: On track / At risk / Unlikely. This is a *forecast against the commitment*. Before the due time it
  is not an ETA (§4). After the due time a fact banner replaces it (§7.5).
- **Check-in**: a durable, once-per-slot update at a milestone target time (§7.6).
- **Warning**: an out-of-schedule update when the status worsens (§7.6).

---

## 4. Ownership boundaries

The deadline supervisor answers **one question: "is the commitment going to be met, and what should the owner know or
decide?"** Every other question stays with its current owner. The supervisor **reads** their outputs and never
writes their state.

| Component | Owns (unchanged) | Deadline supervisor's relationship | Must NOT happen |
|---|---|---|---|
| **ETA** (`eta.py`, `eta_structure.py`, `eta_prior.py`, computed in `EfficiencyWatcher._eta`, stored in kv `efficiency_state.eta`) | "How long is left?" Its ladder, bands, hysteresis of the *shown* P50, `eta_changed`/`eta_overdue`. | Reads the stored ETA dict (`shown.p10_end/p50_end/p90_end`, `source`, `fin_stage`, `fin_p90_end`) as one risk input (§7.2). | The deadline never feeds the ETA. It is not a `units_total` hint, a `clock` stated duration, or an estimate. The supervisor never recomputes the ETA and never writes `efficiency_state`. The ETA stays the general time display. The deadline sign sits beside it, not instead of it. |
| **Efficiency watcher** (`efficiency_watch.py`, detectors, `efficiency_policy`) | Cost and process waste: spend shape, findings, policy reconfiguration, effect measurement. | Shows active findings as *context* in updates. They never trigger a status change on their own. Efficiency is about cost, not schedule. | The supervisor never nudges for efficiency reasons and never changes `efficiency_policy`. |
| **Heartbeat + stall doctor (Sol)** (`heartbeat.classify`, `recovery.decide`, `diagnoser`) | Detecting and recovering stalls, dead workers and bounded recovery. | Consumes the heartbeat class, `stall diagnosis produced`, recovery actions and `watcher_authority.runner_predicates` (`identical_calls_no_change`, `repeated_failure_code`) as risk evidence (§7.2 R3). | The supervisor never triggers recovery, restarts or interrupts. "Stop unproductive retries" is a worker rule (§8), not a supervisor action. |
| **Trajectory / Astra** (`trajectory.py`) | Semantic direction: reviews and redirects (`focus`/`nudge`/`priority`). | Independent. It does not request reviews in v1. Its notices yield to an unacknowledged trajectory redirect (§9.4). | The supervisor never redirects the approach. Choosing an approach is the worker's bounded autonomy, and Astra may still redirect it. |
| **Jev** (`jev_observer.py`, `eta_jev.py`) | 10-second triage labels: choice answers only, vote of 3, code numbers always stand. | Adds at most two *choice* questions to the existing triage body (no extra call) and one classification at creation (§10). | Jev never produces minutes, never marks a milestone met, and never sets the status. |
| **Closeout / autopilot / incorporate** | Verification, acceptance, auto-merge, human merge. | Deadline jobs end at a candidate routed to the owner. Every automatic merge path is gated (§9). | No automatic ACCEPTED→merge for a deadline job, by any path. |
| **Steering** (`steering.py`) | Durable instructions with receipts and optimistic concurrency. | Records schedule notices only as origin `deadline`, verbs `{priority, nudge}`, never urgent (§9.4). | `goal`, `avoid`, `authority`, `focus` from origin `deadline` are refused structurally, mirroring `_TRAJECTORY_ALLOWED_VERBS`. |
| **Notify / alerts** (`notify.py`, `alerts.py`) | Delivery to the owner's phone, dedupe, failure recording. | Check-ins and warnings go through `notify.emit` with a deterministic dedupe key (§7.6). | No new transport, and no contact with anyone but the owner. |

---

## 5. State and persistence

All state lives in the existing store: kv rows and append-only events. **No schema migration** is needed, following the
precedent of steering, approvals, questions and the ledger. The worker-facing plan is a committed file read the
way `questions.scan` reads files: committed content only, never the dirty tree.

### 5.1 Commitment (kv `deadline_commitment`): human-owned, append-only revisions

```json
{
  "v": 1,
  "current": 2,
  "revisions": [
    {"rev": 1, "due_at": "2026-10-02T17:00:00-07:00", "due_at_utc": "2026-10-03T00:00:00Z",
     "core_outcome": "Add a CSV import endpoint with validation. Optional: CSV export.",
     "acceptance": ["review-ready candidate: committed, closeout verification passed",
                    "authorized checks pass with no new failures vs baseline"],
     "items": [{"id": "R1", "text": "Add a CSV import endpoint with validation.", "class": "required"},
               {"id": "O1", "text": "Optional: CSV export.", "class": "optional"}],
     "set_by": "cli", "judgment": "human", "request_id": "deadline-3f2a-1", "at": "2026-10-02T13:05:00-07:00",
     "reason": "initial"},
    {"rev": 2, "due_at": "2026-10-02T18:30:00-07:00", "due_at_utc": "2026-10-03T01:30:00Z",
     "core_outcome": "(unchanged from rev 1)", "acceptance": ["(unchanged from rev 1)"], "items": ["(unchanged from rev 1)"],
     "set_by": "moshi", "judgment": "human", "request_id": "deadline-3f2a-2", "at": "2026-10-02T15:10:00-07:00",
     "reason": "The owner moved deadline to 18:30 after At-risk check-in"}
  ]
}
```

Each revision stores the full record. The `(unchanged …)` strings above abbreviate this example only.

Rules:
- **Write path is human-only.** `deadline.revise_commitment(store, job_id, ..., judgment)` raises unless
  `judgment == Judgment.human`. This mirrors `machine._HUMAN_ONLY_TRANSITIONS`. It is called only from relayed
  human writes (`steward new --deadline`, `steward deadline JOB set|scope|clear`, and later phone/MCP equivalents
  carrying human provenance). No supervisor, watcher, worker frame or plan file can call it.
- Revisions are **append-only**. Earlier revisions are never edited. `current` points at the active one. The original
  (rev 1) is always reconstructable. Each revision emits `deadline_commitment_revised` with a before/after diff.
- `core_outcome` is the task text. Items are seeded by a deterministic parse of that text (§8.1), and the owner may
  reclassify them later. Changing the core outcome text is a revision like any other: human-only, recorded.
- The contract (`contract.py`) is untouched in shape. `render` gains one line, `DEADLINE: <local time> (rev N)`, read
  from this kv. Contract revisions and commitment revisions are independent counters.

### 5.2 Plan (committed file + kv `deadline_plan`): worker-owned, mutable, validated

The worker commits `.steward/deadline/<job8>/PLAN.json`. The per-job directory follows the questions-file collision
rule.

```json
{
  "v": 1, "commitment_rev": 2,
  "approach": "Parse with the stdlib csv module; validate rows against the existing schema; reuse the upload handler.",
  "items": [{"id": "R1", "class": "required"}, {"id": "O1", "class": "optional"},
            {"id": "W1", "text": "Retry UI for rejected rows", "class": "proposed-optional"}],
  "milestones": [
    {"id": "M1", "title": "parser + validation", "items": ["R1"], "target_at": "2026-10-02T14:30:00-07:00", "evidence": "commit"},
    {"id": "M2", "title": "endpoint wired", "items": ["R1"], "target_at": "2026-10-02T15:45:00-07:00", "evidence": "commit"},
    {"id": "candidate", "title": "review-ready candidate", "target_at": "2026-10-02T16:50:00-07:00", "evidence": "verify"}
  ],
  "adaptations": [{"at": "2026-10-02T15:20:00-07:00", "kind": "drop_optional", "detail": "O1 CSV export",
                   "reason": "M2 late; optional per the task"}],
  "tradeoffs": ["Mark W1 optional to save ~the last hour (worker-planned, uncalibrated)"]
}
```

Steward reads the file at the committed HEAD whenever HEAD moves. It validates the file (§9.3) and stores the last
**valid** version in kv `deadline_plan`, together with `plan_rev` (a monotonic counter Steward assigns) and the source SHA.
An invalid revision is never adopted. The previous valid plan stays in force.

### 5.3 Status (kv `deadline_status`)

`{status, since, reasons:[{rule, evidence}], basis:"evidence|weak|none", last_assessed_at, clean_streak,
milestones:{id: {target_at, met_at, met_sha, missed_noted}}, commitment_rev, plan_rev}`. This is a cache. Each
transition is also an event, so the status can be rebuilt from events after a restart.

### 5.4 Check-in slots (kv `deadline_checkins`, events authoritative)

`{slot_key: {due_at, state: "pending|recorded|pushed|push_failed", attempts}}` with
`slot_key = "<commitment_rev>:<milestone_id>:<target_at_utc>"`. See §7.6 for the durability protocol.

### 5.5 Events (all `judgment=code` unless noted; the feed renders each)

`deadline_commitment_revised` (judgment human), `deadline_plan_adopted`, `deadline_plan_rejected`,
`deadline_plan_missing`, `deadline_milestone_met`, `deadline_milestone_missed`, `deadline_status_changed`,
`deadline_checkin`, `deadline_warning`, `deadline_adaptation`, `deadline_scope_dropped`, `deadline_guardrail`,
`deadline_steer_recorded`, `deadline_steer_skipped`, `deadline_passed`, `deadline_candidate_ready`,
`deadline_error`. Volume stays bounded: status/milestone events fire only on change, and check-ins once per slot.

### 5.6 Calibration file (`$STEWARD_HOME/deadline-calibration.json`)

The `eta-priors.json` pattern: atomic write, a corrupt file falls back to empty, rebuilt from the store at closeout.
One row per finished deadline job: `{job, kind, jev_kind, window_min, milestones: [{id, planned_offset_min,
actual_offset_min|null}], candidate_at_offset_min|null, outcome: met|late|stopped}`. It is used only as §10
describes.

---

## 6. User flow

### 6.1 Create
`steward new "<task>" --deadline "<when>"`. `<when>` is an absolute local time ("17:00", "2026-10-03 09:00") or a
relative one ("+4h"). It is parsed in the configured owner timezone **[A-1]** and stored with both the local offset and UTC.
The deadline lives in its own flag, never in the goal text. This keeps `eta_prior.job_kind` from mistaking it for a
`clock` stated duration.

Creation checks, all deterministic:
1. The feature flag is on (`steward.toml [deadline] enabled`, default `false` while experimental **[A-2]**).
2. **One-at-a-time (PD-11):** if another job with a current commitment is non-terminal, the create is refused with
   that job's id. Queueing it with `--after` remains allowed: the commitment is recorded, the clock still runs, and
   the status will reflect the wait honestly.
3. The due time is in the future, and at least `MIN_WINDOW_MIN` (15) away **[A-3]**.
4. Commitment rev 1 is written (§5.1) and items are seeded (§8.1).

### 6.2 Immediate start (PD-2)
The job is contracted and launched as usual. The brief gains a **DEADLINE** section (§8.2). The worker's first act is to
commit `PLAN.json`. It then continues straight into work. **Nothing waits for the owner.** If no valid plan is committed by
`T_plan = min(15 min, 10 % of the window)`, Steward records `deadline_plan_missing` and adopts a **fallback plan**:
- if `eta_structure` reports units, one milestone per unit boundary, with targets spaced evenly up to the
  candidate target;
- otherwise time checkpoints at 50 % of the window plus `candidate`.

The candidate target is `due_at − tail`, where `tail` is the prior's `tail_min`, or `eta.TAIL_SINGLE_MIN`. The status
basis becomes `weak` (§7.3), and one `nudge` asks for the plan.

### 6.3 Run
- Every assessment (§7.1) updates the status shown on: the pane/job-tab header (next to the ETA, never merged into it),
  the phone row, `steward explain` (a new `Deadline:` line), `steward deadline JOB`, and the API/MCP job view
  (`deadline` object).
- Milestone met: code-verifiable evidence only (§6.3.1).
- Check-ins at target times (§7.6). Warnings on worsening (§7.6).
- The owner steers at any time: `steward deadline JOB set <when>` (move the deadline), `scope <item> optional|required`,
  `clear` (end deadline mode, after which the job becomes an ordinary job and autopilot rules apply again on a *later* acceptance),
  plus all existing steering verbs.

#### 6.3.1 Milestone evidence (worker self-report alone never suffices)
| `evidence` | Met when |
|---|---|
| `commit` | A commit on the job branch whose subject starts `deadline(<id>):`, at a resolvable SHA. |
| `units:<label>` | `eta_structure` reports that unit done (same source the ETA uses: planning files, summary commits, finished helpers). |
| `verify` (candidate only) | Closeout verification at the candidate SHA passed with no new failures vs baseline (SP-16). |

`met_at` is the commit/verification time, not the time Steward noticed it.

### 6.4 Done (PD-8)
When the candidate milestone is met, the job goes to `NEEDS_HUMAN` with reason `deadline_candidate_ready`
(`CHECKS_PASSED → NEEDS_HUMAN` is already legal), not to auto-`ACCEPTED`. The owner gets one READY push with a
candidate report:
- met on time / late by X;
- milestones planned vs actual;
- adaptations made;
- optional items dropped;
- verification evidence (SHA, checks, baseline comparison);
- open questions.

The owner then accepts (`NEEDS_HUMAN → ACCEPTED`, already human-only), sends it back, or stops it. Merging stays a separate
human `steward incorporate`. The calibration row is written at this terminal point.

---

## 7. Risk assessment, hysteresis and updates

### 7.1 When
A `deadline.sweep(store, job_id, now)` step, code-only and never raising. Two callers:
1. **The runner tick**, placed before the state early-returns, so PAUSED/NEEDS_HUMAN/WORKER_FINISHED jobs still get
   check-ins.
2. **The control pane's periodic tick**, the `sequencing.sweep` precedent, so slots fire even when no runner is alive.

Both callers are safe because every side effect is keyed (§7.6). The full assessment runs at most once per
`ASSESS_EVERY_S = 60` and on any new committed HEAD, verification result or ETA recompute. Check-in due-ness is checked on every
call.

### 7.2 Rules (worst wins; every rule cites its evidence in `reasons`)

`grace(m) = max(5 min, 10 % of the gap between m's target and the previous milestone's target, or the job start)`.
"P50-grade ETA" means `source ∈ {units, units+trend}` with ≥ 1 unit done. "Range-grade" means `prior`, `peers`, `class-rate`.

**Unlikely** if any of these hold:
- **U1** A P50-grade ETA's shown `p10_end > due_at`: even the optimistic end misses.
- **U2** `now ≥ due_at − tail`, required milestones other than candidate are unmet, and no verification is running.
- **U3** Two or more milestones missed, and the most recent missed one is still unmet more than one full milestone gap
  after its target (no catch-up).
- **U4** The ETA is in a finishing stage whose `fin_p90_end > due_at`, and the stage is `fixing <check>`.

**At risk** if any of these hold:
- **R1** A milestone's `target_at + grace` passed without evidence.
- **R2** P50-grade: shown `p90_end > due_at`. Range-grade: `p10_end > due_at` (weak basis, §7.3).
- **R3** Progress risk for ≥ 10 min: heartbeat `suspect_stall`/`dead`, or a runner predicate
  (`identical_calls_no_change`, `repeated_failure_code`) fired, or a stall diagnosis/recovery action happened in the window.
- **R4** Start feasibility: the window is shorter than the fastest comparable job (`prior.range_min[0]`, `peers ≥ 2`).
- **R5** No valid plan (fallback in force) **and** any of R1–R3 holds. A missing plan alone is never a risk.

**On track**: none of the above.

The thresholds are initial defaults to be calibrated (§10.3) **[A-4]**.

### 7.3 Evidence strength caps the severity (PD-9: never overstate)
- **Unlikely requires strong evidence**: U1 or U4 (P50-grade timing), or U2/U3 (observed misses against the clock).
  Priors, peers, Jev labels and fallback plans can never produce Unlikely.
- R2 on a range-grade ETA and R4 set `basis: weak`. The sign then reads "At risk (early signal)".
- With no usable ETA (`none`, `n/a` codex worker) the status uses only milestones and progress rules. The basis is
  shown as "milestones only".

### 7.4 Hysteresis
- **Worsening applies immediately.**
- **Improving** requires that the triggering rules no longer hold, plus one of:
  - a milestone met since the downgrade;
  - two consecutive clean assessments ≥ 10 min apart (`clean_streak`).
- At most one improvement per 30 min. Worsening is never rate-limited.
- A commitment revision by the owner (new deadline or scope) **resets** hysteresis. The status is recomputed from scratch,
  so a moved deadline can return the job to On track at once. That return is labelled as caused by the owner's change.
- Each change emits `deadline_status_changed {from, to, reasons, basis, commitment_rev, plan_rev}`.

### 7.5 Deadline passed
At `due_at` without a candidate, Steward records `deadline_passed` (once per commitment rev) and sends a check-in. The
sign is replaced by the fact banner **"Deadline passed · <X> over"**, followed by the remaining milestones. Work
**continues** on the authorized task **[A-5]**, so no completed work is thrown away. The due time is never moved: the
banner counts up and the commitment stays as the owner set it. The owner may move the deadline, narrow scope, or stop.

### 7.6 Check-ins and warnings: durable, idempotent, restart-safe

**Slots.** Each non-candidate milestone in the adopted plan, plus the deadline itself, defines a slot (§5.4). A slot
is due at its target time. The candidate milestone needs no slot: reaching it sends the READY push (§6.4). If the
candidate is not met by its target, R1 raises a warning. The deadline slot is retired unfired if the candidate is ready
before `due_at`. For milestones, the check-in waits `grace` past the target. This lets a just-in-time commit report "met"
rather than "missed". When a plan revision moves a target, the old slot key is retired unless it has already fired, and a
new key is created. A slot that has already fired is never re-fired.

**Protocol** (claim before send, the `autopilot.ensure_attempted` precedent):
1. If an event `deadline_checkin` with this `slot_key` exists, **stop**. The event log is the truth, and the kv
   row is only a cache rebuilt from events on load.
2. Append `deadline_checkin {slot_key, status, milestone, met|missed, changes_since_last, next, tradeoffs}`. This is
   the durable, exactly-once record, and every surface shows it.
3. Push it via `notify.emit` with kind `DEADLINE` (one new kind added to `_KINDS`) and `revision = slot_key`. This is
   the existing `(job, kind, revision)` dedupe. A recorded push failure is retried on later sweeps, up to
   `alerts.MAX_ATTEMPTS`. A crash between steps 2 and 3 loses at most that one push, never the record.

**Catch-up after downtime.** On the first sweep after a restart, all past-due unfired slots are recorded individually
(step 2). They are pushed as **one** combined message: "missed check-ins while Steward was down: M1 (met), M2 (missed)…".
No burst of pushes.

**Warnings.** When the status worsens (`deadline_status_changed` to a worse sign), the event key is
`warn:<commitment_rev>:<status-change event seq>`. It is pushed once as kind `DEADLINE`. Cooldown is 15 min between
warnings, except any change into Unlikely. Within the cooldown, the latest change still shows on surfaces, and it rides
the next push.

**Quiet.** On track between milestones: no pushes. Pushes per job are capped at 1 per milestone slot + warnings +
deadline + candidate. If the owner has no push channel enabled, everything still appears on surfaces. The CLI
says so once. Moshi pushes to the owner's own phone are covered by the standing exception, and nobody else is ever
contacted.

### 7.7 Message format (plain, short; template-built, no model)
```
⏱ <job name> · At risk (was On track) · due 17:00 (2h10 left)
Why: M2 "API wired" passed its 14:30 target with no commit; ETA ~2h40 (2h–3h20) from cycle times > time left.
Did: reordered R3 before R2 (independent); stopped retrying flaky fixture after 3 identical failures;
     dropped O1 "CSV export" (optional in your task).
Next: M3 "tests green" target 15:45.
Options (yours): mark W1 "retry UI" optional (worker-proposed) · move deadline (ETA P90 from cycle times ~19:10) ·
         narrow core outcome · proceed as is. Work on required items continues meanwhile.
```
Every number names its source ("from cycle times", "from 4 similar jobs", "worker-planned"). With no P50-grade data,
there is no suggested new deadline. The message says "no timing data yet" instead (PD-9).

---

## 8. Bounded autonomy (PD-6, PD-7)

### 8.1 Scope classification
- At creation, a deterministic parse of the task text (the `contract._BOUNDARY_SENTENCE` style) marks a sentence or
  bullet as `optional` when it contains `optional`, `nice to have`, `if time`, `stretch`, or `bonus`. Everything else
  is `required`.
- The owner reclassifies with `steward deadline JOB scope <id> optional|required`. That is a human commitment revision.
- The worker may add items it thinks are optional as `proposed-optional` in the plan. **They are treated as required
  until the owner classifies them** (Q1, recommended). The first check-in lists them, with one-tap phone actions in a
  later phase.

### 8.2 What the worker may do (stated in the brief's DEADLINE section; recorded as `adaptations`)
| Allowed without asking | Condition | Steward records |
|---|---|---|
| Reorder independent work | No unmet dependency. Milestone targets may move, but the candidate target never goes past `due_at − tail`. | `deadline_adaptation kind=reorder` |
| Switch to another reversible approach | Inside the worktree only. No deleted committed work (a revert commit is fine). No floor action. | `kind=switch_approach` |
| Stop an unproductive retry | Same failure ≥ 3 times, or a runner predicate fired. Note the blocker and move to other work. | `kind=stop_retry` |
| Drop an optional item | Commitment class `optional` (task- or the owner-classified) only. | `deadline_scope_dropped` + an update line |

**Never without the owner:**
- changing the core outcome or acceptance;
- moving the deadline;
- dropping a required or proposed-optional item;
- weakening checks (already caught by `weakening.py`);
- any floor action;
- accepting or merging.

When the worker believes the commitment is infeasible, it writes `tradeoffs` in the plan, commits, and **keeps working on
required items**. Steward surfaces the tradeoffs (§7.7). Optionally the worker asks a non-blocking question through the
existing question queue.

### 8.3 How the bounds are enforced (code, not trust)
Plan validation (§9.3) rejects any revision that breaks a bound. The commitment is unreachable from the worker (§5.1).
Autopilot is gated (§9.1). A dropped item that is actually required shows up as a rejected plan, not an adopted one.

---

## 9. Guardrails and failure behaviour

### 9.1 No merge without the owner (PD-8): every automatic path is closed

Steward has these automatic routes to a merge today. Each gets an independent gate for jobs with a current commitment:

| Path | Today | Deadline mode |
|---|---|---|
| Closeout auto-acceptance (`closeout`, acceptance = `auto`, rule `preauthorized_local_acceptance`) | `CHECKS_PASSED → ACCEPTED` by code | **Gate 1**: the deadline job routes `CHECKS_PASSED → NEEDS_HUMAN` (`deadline_candidate_ready`). Code never sets ACCEPTED. |
| Autopilot merge (`runner` calls `autopilot.ensure_attempted` → `try_merge` → incorporate, then `deploy-self` for the Steward repo) | Merges any ACCEPTED, not-yet-incorporated job | **Gate 2**: `ensure_attempted` and `try_merge` return `{"outcome": "skipped", "detail": "deadline_mode"}` and record `autopilot_skipped`. This holds even after the owner's human accept, because accepting is not merging. |
| Autopilot merge follow-ups and `fix-merge` children (`_default_followup`, `start_fix_merge`) | A child job is started to resolve, and the parent is re-attempted | Not started for a deadline job (Gate 2 runs first). The `parent_job` chain is walked, so a child of a deadline job inherits Gate 2. |
| `deploy-self` after an auto-merge | Runs on a tested merge | Unreachable without a merge. |
| Sequencing `--after-merge` successors | Wait for incorporation | Unchanged. They simply wait for the owner's merge. |

Gate 2 is defence in depth. It holds even if Gate 1 is bypassed (for example, a job whose commitment was added after
it was accepted). The **only** merge path is a human `steward incorporate` (judgment human, relayed write). `deadline
clear` removes both gates only for transitions *after* the clear. A job already ACCEPTED while in deadline mode
records `autopilot_attempt` as consumed, so clearing never triggers a retroactive auto-merge.

### 9.2 No silent deadline drift (PD-7)
- No code path writes `due_at` (§5.1). The plan's own times are targets *inside* the commitment. A plan whose candidate
  target exceeds `due_at` is rejected (`deadline_plan_rejected reason=beyond_deadline`).
- The ETA never re-anchors the deadline. The shown due time always comes from `revisions[current]`.
- After the due time, the banner counts up (§7.5). Nothing re-baselines.
- Every surface that shows the due time shows `rev N` once N > 1. The candidate report lists every revision with who
  set it and why.

### 9.3 Plan validation (on every new committed HEAD that changed `PLAN.json`)
Rejected, with the previous valid plan kept, the worker nudged once per rejection reason per plan SHA, and a
`deadline_guardrail` event recorded, when the plan:
- names a `commitment_rev` older than the current one (stale plan). It is not adopted. The last valid plan stays in
  force with its targets clamped to the new `due_at − tail` until a plan for the current rev arrives;
- omits a required item, or reclassifies `required → optional`;
- shows a dropped optional item that the commitment classes as required;
- has a candidate target `> due_at − tail`;
- marks a milestone met in prose: ignored, since only §6.3.1 evidence counts;
- is malformed JSON or uses unknown fields. Unknown fields are ignored; malformed JSON is rejected with the reason.

### 9.4 Steers from the supervisor (and stale steers)
- Origin `deadline`, verbs `priority|nudge` only, never urgent. `Steering.record` refuses anything else, the
  `_TRAJECTORY_ALLOWED_VERBS` precedent.
- At most one steer per status worsening, per plan rejection reason, and per missing plan. The text always names
  `commitment rev N` and `plan rev M`.
- **Stale-steer protection**, all enforced at record time:
  - (a) `expected_revision` = the steering revision seen at assessment. A human steer since then yields `CONFLICT`,
    recorded as `deadline_steer_skipped reason=stale_after_human_steer`, and nothing is sent.
  - (b) No new deadline steer while an earlier deadline or trajectory instruction is unacknowledged
    (`reason=pending_unacked`).
  - (c) A commitment revision makes every not-yet-delivered deadline-origin instruction obsolete. If steering offers no
    withdraw, (b) guarantees there is at most one. The brief rule tells the worker that a deadline notice naming an older
    commitment rev than the latest DEADLINE section or human `deadline set` notice is void.
- The owner's `deadline set`/`scope` writes also record a human steering instruction (origin cli, verb `priority`)
  announcing the new commitment rev. This makes the owner's change itself the ordering fence for (a).
- Priority (from the watcher-authority decision): human > safety policy > advisor (trajectory) > **deadline
  supervisor** > doctor > custom watchers. The supervisor never contradicts a human steer issued since its
  assessment.

### 9.5 Failure isolation
- `sweep` never raises. Errors are recorded as `deadline_error`, once per distinct message (the ETA `_eta_error`
  pattern), and the last good status is kept.
- ETA unavailable or stale (older than 10 min): rules R2/U1/U4 are skipped, and the basis says so.
- Jev unavailable: no effect on status. Labels are display-only.
- Runner down: the control-pane sweep keeps check-ins durable. The catch-up message follows §7.6.
- Store or kv corrupt: the commitment can be rebuilt from `deadline_commitment_revised` events, whose evidence carries the
  full revision. The status is rebuilt from events.
- Kill switches: `[deadline] enabled=false` stops new deadline jobs and silences pushes. Existing commitments stay
  recorded, and **both merge gates stay active** while a commitment is current. Per job:
  `steward deadline JOB mode off|shadow|active`. Shadow computes and shows but sends no steers or pushes.

---

## 10. Estimation and JEV role (PD-9)

### 10.1 What is shown, by evidence level
| Evidence | Shown | Never shown |
|---|---|---|
| No comparable history (`prior_for` is None) | "no comparable history" | any duration |
| ≥ 2 peers of the kind | "similar jobs took 40 min–3 h (n=4)", the range from `eta_prior` | a point estimate |
| P50-grade ETA | the ETA's own `~X left (lo–hi)` (owned by the ETA) | a deadline-derived number |
| Calibration ≥ 5 jobs of the kind | "plans like this usually run 1.3× their targets (n=7)" | a calibrated factor applied silently |

The worker plans milestone targets itself (labelled "worker-planned"). Steward does not invent targets, except in the
fallback plan, which is labelled "fallback".

### 10.2 JEV
- **Classification at creation.** One choice request through the existing Jev client (`jev_observer`, TypeSafe choice API): the task kind among `eta_prior.KINDS +
  unclear`. It is recorded beside the code's `job_kind`. On disagreement **the code kind stands** for priors, and the Jev
  label is stored for calibration grouping only. No quantity questions are asked (eta_jev: Jev is poor at quantities).
- **During the run.** At most two choice questions added to the existing 10-second triage body (`eta_jev.questions`
  precedent, no extra call):
  - "Which milestone is the worker on?" (open milestone ids + `unclear`);
  - "Has the worker stopped working toward required items?" (`yes|no|unclear`).

  The vote of 3 plus `FLAG_CONFIDENCE` applies. Labels drive only the "current milestone" display and corroborate R3.
  They never mark a milestone met, and they never move the status on their own.

### 10.3 Calibration loop
At each terminal deadline job, a calibration row is written (§5.6). Once ≥ 5 rows exist for a kind (with Jev-kind
agreement recorded), Steward:
- shows the observed target-overrun ratio in the first update;
- uses the ratio to recalibrate R1's `grace` and the start-feasibility check R4.

**Thresholds change only by a recorded config revision**, proposed in the candidate report and confirmed by the owner,
never automatically **[A-4]**. Until then, every threshold in §7.2 is a labelled default.

---

## 11. Acceptance criteria (for the build phases; each is a test or a scripted live check)

| # | Criterion |
|---|---|
| AC-1 Immediate start | `new --deadline` launches the worker at once. No state waits on the owner. The worker brief contains DEADLINE rules. A committed `PLAN.json` is adopted within one assessment. |
| AC-2 Fallback | With no plan by `T_plan`, a fallback plan is adopted, `deadline_plan_missing` is recorded once, the basis is `weak`, and one nudge is sent. |
| AC-3 One at a time | A second `new --deadline` while one is non-terminal is refused, naming the active job. |
| AC-4 Commitment immutability | No code-judgment path can write a commitment revision (the call raises). `deadline set` by a human appends rev N+1, rev 1 stays readable, and the event carries the diff. |
| AC-5 No silent drift | Across plan revisions, ETA changes and passing the due time, the shown due time is the same until a human revision. A plan targeting beyond `due_at − tail` is rejected. After `due_at`, the banner counts up and never re-anchors. |
| AC-6 Late milestone | A milestone with no evidence at `target + grace` produces `deadline_milestone_missed`, status ≥ At risk, and a check-in that says "missed". A commit landing inside the grace window yields "met". A late commit after the miss records `met_at` honestly and does not erase the miss. |
| AC-7 Restart safety | Kill the runner and control pane across two milestone targets, then restart: each slot gets exactly one `deadline_checkin` event, there is exactly one combined catch-up push, and a second restart produces zero new events or pushes. |
| AC-8 Idempotent check-ins | Running `sweep` N times at the same instant (both callers concurrently) produces one event and one push per slot. |
| AC-9 Hysteresis | Worsening is immediate. Improving needs a met milestone or two clean assessments ≥ 10 min apart, at most once per 30 min. A human deadline move recomputes at once, labelled as caused by the owner's change. |
| AC-10 Evidence caps | Priors/peers-only data can never produce Unlikely. A range-grade ETA can produce at most "At risk (early signal)". |
| AC-11 Optional vs required | Dropping a commitment-optional item is adopted and reported. Dropping a required or `proposed-optional` item is rejected, the previous plan is kept, and a guardrail event plus one nudge follow. Reclassification works only via a human revision. |
| AC-12 Stale steers | A human steer between assessment and record yields `deadline_steer_skipped stale_after_human_steer`, and nothing is delivered. No second deadline steer while one is unacked. Origin `deadline` with verbs `goal/avoid/authority/focus` or `urgent=True` is refused. |
| AC-13 No auto-merge | With acceptance=auto and autopilot merge on, a deadline job reaching passing checks ends at `NEEDS_HUMAN deadline_candidate_ready`. After a human accept, `ensure_attempted`/`try_merge` return `skipped: deadline_mode`. No follow-up or fix-merge child starts. The source branch SHA is unchanged until a human `incorporate`. A follow-up child of a deadline job is also gated. |
| AC-14 Ownership | `efficiency_state.eta` is byte-identical with the feature on vs off for the same inputs. The supervisor writes no efficiency, trajectory or recovery state. |
| AC-15 Failure isolation | An exception inside `sweep` records `deadline_error` and the tick continues. With Jev or the ETA unavailable, status still computes from milestones. |
| AC-16 Candidate report | It contains on-time/late, planned vs actual milestones, adaptations, dropped optional items, commitment revisions, the verification SHA and check results. It writes a calibration row. |
| AC-17 Quiet | An on-track run with M non-candidate milestones sends exactly M + 1 pushes (M check-ins + the READY push), and no deadline-slot push. |

---

## 12. Non-goals (first release)
- More than one deadline job at a time. Multi-job portfolios or deadline-aware queue reordering.
- Decomposing one task into several jobs. Steward-side LLM planning (the worker drafts the plan).
- Auto-extending, auto-negotiating or auto-suggesting a deadline without P50-grade evidence.
- Any automatic accept or merge of a deadline job. Changes to `incorporate`'s human path.
- Changing the ETA ladder, the efficiency detectors, doctor recovery or trajectory behaviour.
- Integration with an external calendar (for example, reading the deadline from one). Contacting anyone other than the owner.
- Phone one-tap actions for scope and deadline. These come after the CLI versions (§13 phase D5).

---

## 13. Implementation sequencing and GSD integration

The feature ships as a new GSD milestone in the Steward repo, **"Deadline-aware supervised tasks (experimental)"**. Each
phase runs `gsd-discuss-phase → gsd-plan-phase → gsd-execute-phase`, with this spec as the discuss input. **This job
does not touch `.planning/`.** The milestone is created by a later job on the owner's go. Phases are ordered so each one
ships a safe, testable slice. Shadow comes before active, mirroring efficiency/trajectory.

| Phase | Delivers | Proves |
|---|---|---|
| **D1 Commitment & merge gates** | kv commitment + human-only revisions; `new --deadline`; `steward deadline JOB [set/scope/clear/mode]`; one-at-a-time; Gate 1 + Gate 2 (+ follow-up chain); contract render line; feature flag | AC-3, AC-4, AC-5 (commitment part), AC-13. **Ships first, because the no-auto-merge guarantee must exist before anything else can run.** |
| **D2 Plan & milestones (shadow)** | brief DEADLINE section; `PLAN.json` read/validate/adopt; fallback plan; milestone evidence; status rules + hysteresis computed and shown on surfaces; no pushes or steers | AC-1, AC-2, AC-5, AC-6 (events), AC-9, AC-10, AC-11, AC-14, AC-15 |
| **D3 Check-ins & warnings (active)** | slots, claim-before-send, the `DEADLINE` notify kind, catch-up coalescing, control-pane sweep, cooldowns, message template | AC-6 (check-in), AC-7, AC-8, AC-17 |
| **D4 Bounded autonomy & steers** | adaptation recording, scope-drop reporting, origin `deadline` steering + stale protection, tradeoff rendering, candidate report | AC-11, AC-12, AC-16 |
| **D5 Estimation & JEV** | creation classification, triage questions, calibration file + display, threshold proposal in the candidate report; then phone actions | §10 table; calibration display gated at n ≥ 5 |

**Live proof** (memory rule: "built, not proven live"): after D3, one real deadline job runs end to end with a short
window and a deliberately missed milestone, together with a restart. Only after that live run may the feature be
described as working.

**Model tier for the build** (Steward worker model rule): sonnet by default. D2's rule engine and D1's gates are
judgment-heavy enough to suggest opus at shaping.

---

## 14. Assumptions

- **[A-1]** Owner timezone defaults to `America/Los_Angeles`, configurable in `steward.toml [deadline] tz`. The UTC value is stored alongside the local one.
- **[A-2]** The feature flag defaults to off while experimental. The owner turns it on (`steward enable deadline` or the toml).
- **[A-3]** Minimum window 15 min, because shorter windows cannot fit plan + work + verification.
- **[A-4]** All numeric thresholds in §7 (grace, 10-min persistence, 30-min improvement limit, 15-min warning cooldown,
  `T_plan`) are uncalibrated starting defaults. Calibration proposes changes, and the owner confirms them.
- **[A-5]** After the deadline passes, work continues by default (Q2, recommended). Stopping would discard progress the owner may still want.
- **[A-6]** The runner keeps ticking jobs in PAUSED/NEEDS_HUMAN states, or the control-pane sweep covers them. D3 verifies which is
  true and relies on the control-pane sweep regardless.
- **[A-7]** `notify` gains one kind (`DEADLINE`). The existing six stay untouched.

---

## 15. Open questions (non-blocking; recommended options assumed above)

```steward-questions
{"questions": [
  {"id": "deadline-proposed-optional",
   "title": "Worker-proposed optional items",
   "question": "When the worker labels an item it thinks is optional, should it stay required until you classify it, or count as optional unless you object?",
   "options": [{"key": "required-until-classified", "label": "Required until you classify it"},
               {"key": "optional-unless-objected", "label": "Optional unless you object"}],
   "recommended": "required-until-classified",
   "rationale": "PD-6 says drop only scope explicitly classified optional; silence is never consent (watcher-authority precedent).",
   "blocking": false, "blocking_reason": ""},
  {"id": "deadline-after-due",
   "title": "Work after the deadline passes",
   "question": "When the deadline passes without a candidate, should the job keep working (with a 'deadline passed' banner) or pause for you?",
   "options": [{"key": "continue", "label": "Keep working, banner + check-in"},
               {"key": "pause", "label": "Pause and wait for you"}],
   "recommended": "continue",
   "rationale": "PD-7 asks Steward to keep doing authorized work while surfacing infeasibility; pausing throws away overnight time.",
   "blocking": false, "blocking_reason": ""}
]}
```

---

## 16. Self-review record
- Placeholders: none. Every threshold has a value and is labelled [A-4]. Every new name (module, kv key, event, flag,
  notify kind) is stated once and used consistently.
- Contradictions checked:
  - Three status signs vs the deadline-passed banner: the banner is a fact after the due time, not a fourth forecast (§7.5).
  - "Continue authorized work" vs "no auto-merge": continuing stops at a candidate routed to the owner (§6.4, §9.1).
  - Reusing the ETA vs not duplicating it: the supervisor reads the stored ETA and never computes one (§4, AC-14).
  - Check-ins "even when on track" vs quiet rules: exactly one per milestone slot (AC-17).
- Product decisions PD-1…PD-12 map to: PD-1 §6.1/§12; PD-2 §6.2; PD-3 §7.2–7.4; PD-4 §7.6; PD-5 §7.2/§7.6;
  PD-6 §8; PD-7 §5.1/§9.2; PD-8 §6.4/§9.1; PD-9 §7.3/§10; PD-10 §2; PD-11 §6.1; PD-12 §4.
