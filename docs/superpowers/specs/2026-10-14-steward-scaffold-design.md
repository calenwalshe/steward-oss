# Steward scaffold: spec → plan → execute in one submission — design (2026-10-14)

Unit 3 of `docs/superpowers/briefs/2026-10-14-steward-approver-and-scaffold.md` (Part B).

It builds on:
- `docs/superpowers/specs/2026-10-14-steward-approver-design.md` (rev 2, "the approver spec");
- `docs/superpowers/specs/2026-10-13-steward-pause-review-design.md` ("pause-review");
- `steward/eta_structure.py` (phases as units).

Status: design. Engineering choices are decided here ("Decided"). The owner answered the owner value questions on
2026-10-14 (`.steward/questions/af319063/STEWARD-ANSWERS.json`, §12):
- **`plan-approval` = the approver, with the owner only on high-risk plans.**
- `risk-paths` = broad; `go-live-bar` = as recommended (approver spec §12).

Plan approval never activates AUTO-MERGE. That follows the approver spec's §9 stages.

Naming: `steward/scaffold.py` already exists (`steward init`). The feature is called "scaffold" in docs and
`specflow` in code (`steward/specflow.py`, CLI `steward spec`).

## 1. Problem and goal

**Goal.** The owner submits one feature spec. Steward then:
1. runs intake;
2. writes a GSD plan;
3. has the plan reviewed;
4. executes the plan phase by phase with a tech-lead review at every unit;
5. sends each finished job through gate 2 as the approver spec defines it.

The owner is asked only for value decisions.

| Proven | Evidence |
|---|---|
| Executing a committed GSD plan phase by phase in one job | Trinity v0.2 (371e92a4): 5 phases, about 2 h (events 03:22–05:23Z on 10-14), $13 worker, 326 tests |
| `.planning/phases` read as units | `steward/eta_structure.py` (`planning` source) |
| A TL review at every unit boundary | pause-review spec; contract fields `pause`, `units` |
| Ordered launch after a predecessor merges | `steward/sequencing.py` (`seq_after`, `seq_after_merge`) |

| Missing | Section |
|---|---|
| Intake (blocks worker start; questions only on material ambiguity; checks proposed, validated, remembered) | §3 |
| Plan generation from a spec | §4 |
| Plan review before execution | §5 |
| A job must not rewrite its own supervisor mid-run (Steward repo) | §7 |
| Bounds for long jobs | §8 |

Trinity's supervision cost was 784% of worker spend, per the brief. This could not be re-derived from the ledger: its
`spend_ledger` rows are 634 Jev + 4 TL + 1 resolver calls, almost all unpriced. So §8 bounds supervision by call
count as well as dollars.

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | **Intake is a job state, `INTAKE`**, between `CREATED` and `CONTRACTED`. Worker start is impossible while in it | The brainstorm leaned this way; one job id carries the lineage from spec to merge |
| D2 | Intake is a **Steward actor (model call), not a worker pane**. It runs on Sol 6.1 high (codex lane), with Opus 5.5 as fallback; Fable only on the triggers in §3.2 | Brief's earlier decision; codex is a separate budget pool |
| D3 | **Plan generation is the job's first unit (unit 0, docs-only)**, run by a registered worker model (Opus 5.5 for planning) using GSD `new-milestone` / `plan-phase` with the plan-checker. All phases are planned up front; replanning a later phase is a TL-approved gap plan at its boundary | Trinity proved committed plans execute well; reviewing the whole plan needs it to exist |
| D4 | **The plan gate** sits between unit 0 and unit 1. The plan is ruled by the **approver** in a plan mode (APPROVE / REVISE / ESCALATE). The owner sees a card only on plan-risk triggers or an escalation. Decided by the owner 2026-10-14 (`plan-approval`). Recommend mode, where every plan goes to the owner with the approver's recommendation, runs until the approver reaches S2 (§5.3) | Independent of the TL who will supervise; the same actor and guards as acceptance |
| D5 | **Units = GSD plans (`NN-MM`)**; a phase boundary is a unit boundary with a stronger finish line (§6). Pause mode is `unit` | Matches pause-review D4 and Trinity's grain |
| D6 | **A fresh worker attempt per phase** (same worktree, new session, phase brief) | Context × calls is the cost driver (GSD cost-lever notes); F1/F2 compound with session length |
| D7 | **Steward repo: the scaffold plans, then queues one job per phase**, each launched only after the previous one has landed by the train (`seq_after_merge`) | Brief: a job must not rewrite its own supervisor mid-run |
| D8 | **Length bound**: one execution job covers at most 4 h of estimated work, 6 phases or 30 plans. A bigger plan is split into chained jobs, each holding a run of whole phases | Bounds F1/F2/F6 compounding and approver diff size |
| D9 | **Cost bounds per phase**, in dollars *and* calls, worker and supervision separately (§8) | Unpriced calls cannot be bounded in dollars alone |
| D10 | **Each execution job ends at gate 2 exactly as the approver spec defines it.** On the clean path, the TL `finished` ruling passes the acceptance guards. On any hold, the approver rules. Intake's job class, constraints and the plan ruling are inputs to both | One acceptance path; specflow adds no route of its own |

## 3. Intake

### 3.1 Entry

```
steward spec <spec.md> --repo <repo> [--workspace W] [--name N]
```

- `spec.md` must be a file committed in `<repo>`, or it is copied into the job worktree as
  `docs/superpowers/specs/<date>-<slug>.md` and committed by Steward as the job's first commit.
- The spec SHA is recorded (kv `spec_source = {path, sha}`) and is immutable for the job. A changed spec means a new
  job.
- The job is created in `CREATED`, then moves to `INTAKE` with rule `intake_started`.

### 3.2 The intake call

Input:
- the spec text;
- repo facts: tree summary, existing `.planning/` state, the repo's remembered checks (§3.4), recent landed jobs on
  the repo (titles only), risk paths (approver spec §5);
- the Steward-repo flag.

Model ladder and call budget. These are *generation calls*; every one is validated by §3.3.

| Call | Model | Runs when | Counts |
|---|---|---|---|
| G1 | Sol 6.1 high (`codex exec`, read-only sandbox) | always | 1 |
| G2 | Sol 6.1 high, the G1 prompt plus G1's validation errors | G1 failed validation | 2 |
| G3 | Opus 5.5 headless, same prompt plus the last errors | G2 failed validation | 3 |
| G4 | Fable | **Trigger F-a:** G3 failed validation. **Trigger F-b:** a call G1–G3 produced a *valid* proposal, the target is the Steward repo, and that proposal predicts a risk-path touch (`risk_prediction.risk_paths` non-empty) | 4 |

- **Generation budget = 4 calls.** That is enough for both triggers in the worst case:
  - F-a happens only after 3 calls;
  - F-b happens after at most 3 calls, since it follows the first valid proposal, and at most once per job.
- **F-b is a check, not a replacement.** Fable re-derives the proposal independently. If its proposal is invalid, the
  earlier valid one stands, marked `fable_check_failed`, and that becomes a material question ("intake second opinion
  failed on a Steward risk-path spec; proceed?").
- **Disagreement** between the two valid proposals on `class`, any `prohibition` constraint, `done_when` count,
  `risk_prediction` or `size.est_phases` → each difference becomes a material ambiguity question (§3.5) with Fable's
  value as the recommendation.
- **Fold budget = 2 calls**, separate from generation. After the owner's answers or the defaults, a fold call merges them
  into the contract (same model as the accepted proposal), with one retry on a validation failure.
- **Total ceiling = 6 calls per intake**, 20 min wall-clock each. Exhausting the generation budget without a valid
  proposal, or the fold budget without a valid contract → NEEDS_HUMAN `intake_failed`, carrying the last validation
  errors.

Fable is used for intake only, never as a worker (registered-worker rule).

Output schema: the *working contract proposal*, validated by code.

```
{goal, done_when[], class: build|research|owner_review,
 constraints: [{text, kind: prohibition|authority|focus, globs[], source_quote, source_line}],
 checks: [{cmd, purpose, expected: pass|baseline}],
 phases_hint: [{title, outcome}], size: {est_hours, est_phases},
 predicted_paths[], risk_prediction: {risk_paths[], floor_needs[]},
 ambiguities: [{id, question, options[], recommended, rationale, material: bool, why_material, blocking}]}
```

### 3.3 Code validation of the proposal

1. **`done_when`.** Every item is checkable: a check id, a file, or a stated observation. None is empty.
2. **Prohibitions are quoted, never invented.** Every `prohibition` constraint has a `source_quote` that occurs
   verbatim in the spec.
3. **Prohibitions are never dropped.** `policy.parse_avoid_globs` is run over every spec sentence with a prohibition
   chunk, and every glob it yields must appear in some `prohibition` constraint. Each stored constraint gets:
   - `origin=intake`;
   - `source_chunk` = the quote;
   - `parser_version`.

   With these, approver §3.5 E1–E5 resolve directly: a quoted owner sentence is an **owner prohibition**. Intake
   cannot create a parser artifact.
4. **Conflicts escalate.** A deliverable or done_when item that matches a prohibition glob is an intake-time
   `constraint_conflict`, which is a material ambiguity (§3.5). This is the same precedence as approver §3.5, applied
   before any work starts.
5. **Class.** `class` is one of the three values. `owner_review` is forced when the spec asks the owner to choose or judge
   taste (approver C3).
6. **Checks.** Each `checks[].cmd` is a single command with no pipes or chaining: the same rule as `steward check run`.

A failed validation moves to the next rung of the §3.2 ladder.

### 3.4 Checks: propose → validate at baseline → remember

- **Proposed** by intake (§3.2). The repo's remembered checks are offered first.
- **Validated at baseline.** Steward runs each check on the base commit, using the existing background baseline.
  - A check that *cannot run* (command not found, collection error) is rejected; intake revises it once.
  - A check that runs and fails on base is kept with `expected: baseline`, so its failures are pre-existing evidence
    for the approver and not new failures.
  - If no check survives on a code-touching spec → material ambiguity: "no runnable checks".
- **Remembered per repo.** The kv registry job `_repo_checks` is keyed by repo path. It holds
  `{cmd, first_validated_sha, last_ok_sha, uses, last_failed_reason}`, updated on every validated run.
  - This is Steward state, not config, because it is learned.
  - The owner or the TL can retire an entry with `steward checks forget <repo> <cmd>`.
  - Remembered checks feed later intakes.

### 3.5 Questions: only on material ambiguity, 30-minute auto-default

- An ambiguity is **material** when its answer changes at least one of:
  - scope (what is built or what lands);
  - an external effect (outbound, deploy, credential, spend above the job caps);
  - a risk-path touch;
  - an acceptance criterion;
  - a constraint conflict (§3.3.4).
- Everything else intake decides itself, recorded in the contract as `assumed: <choice> (<reason>)`.
- Material ambiguities become questions in the questions store (`steward/questions.py`), each with `recommended` and
  `rationale`. They are delivered as one batched card.
- **Auto-default after 30 minutes.** An unanswered non-blocking question takes its recommended answer, recorded as
  `defaulted`. The intake owns this timer (`intake_default_s = 1800`) instead of the coordinator's `recorded_default`
  3600 s rung, because worker start is waiting on it.
- **Blocking** (no auto-default) applies only to:
  - floor-class needs (outside_world, secrets, spend above the caps);
  - constraint conflicts.

  These wait for the owner; the job stays `INTAKE`.
- The owner's answers (or the defaults) are folded into the contract by a second, short intake call (same model chain)
  and re-validated.

### 3.6 Leaving intake

`INTAKE` → `CONTRACTED` when the contract validates and no blocking question is open. The contract records:
- `class`;
- `pause = unit`;
- `units = [unit 0: "plan"]` (the plan units are added after the plan gate);
- `checks`;
- `constraints` with provenance;
- `spec_source`;
- `assumed[]`;
- `intake_model`, `intake_cost`.

Then the job launches as usual, with unit 0 as its first unit.

Intake limits are the §3.2 budget:
- 4 generation calls + 2 fold calls, 20 min each;
- exhaustion → NEEDS_HUMAN `intake_failed`, carrying the last validation errors.

## 4. Spec → GSD plan generation (unit 0)

- **Worker.** Opus 5.5 (registered; judgment-heavy planning), docs-only unit. Brief:
  - the contract;
  - the spec path;
  - "create the GSD milestone for this spec: run gsd-new-milestone (or gsd-new-project when `.planning/` is absent,
    or a `GSD_PROJECT` workstream when the repo already has one) and then gsd-plan-phase for every phase with the
    plan-checker. Commit `.planning/` only. Do not write product code."
- **Required plan properties.** Code checks these at the unit-0 pause before the plan gate:
  1. `ROADMAP.md` phases, each with a goal and success criteria. Each phase maps to ≥1 contract `done_when` item, and
     every `done_when` item is covered by some phase.
  2. Each `NN-MM-PLAN.md` has `files_modified` and a verify step. The plan-checker passed, with ≤2 revision rounds per
     phase; its result is committed.
  3. `files_modified` ∪ intake `predicted_paths` is checked against risk paths (approver spec §5) and prohibition
     globs (§3.3). Any match is recorded as **plan risk**, which feeds §5.
  4. Size: the sum of plan estimates against the length bound (D8). Over the bound → the plan is split into execution
     jobs (§8.1) before the gate.
- **Budget.** Unit 0 is capped at 60 min and $15 worker spend, or 150 worker calls. Over the cap → the TL rules
  `continue` / `send_back` / `ask_calen` as for any unit (pause-review §4.4).
- **Existing `.planning/`** (e.g. a repo mid-milestone). Planning starts a new milestone and never rewrites completed
  phases. The `eta_structure` base..HEAD scoping already counts only this job's plans.

## 5. Plan review (the plan gate)

### 5.1 Who reviews

Decided: the **approver**, in plan mode: same module, same independence guard (not the job's TL), fresh context,
read-only.

The owner sees a card only when:
- a code trigger fires (§5.2);
- the approver escalates;
- the plan gate is still in recommend mode (§5.3: while the approver is in shadow).

The TL still gives its normal pause-review ruling on unit 0 first. That covers plan *quality as work*: plan-checker
passed, files committed. The approver's plan ruling covers *fitness to the spec and risk*, so they do not duplicate.

### 5.2 Rubric (plan mode)

| Step | Who | Rule | Result |
|---|---|---|---|
| P-C1 | code | plan risk from §4 step 3: a risk path, or a prohibition-glob match | owner hold `plan_risk` |
| P-C2 | code | coverage gap (a done_when item with no phase), or a plan-checker failure or missing result | REVISE (back to the worker via the TL) |
| P-C3 | code | class `owner_review` | owner hold `owner_review` |
| P-C4 | code | `floor_needs` (outside_world, secrets, spend above the caps) in intake's `risk_prediction` or in any plan step | owner hold `floor` |
| P-M1 | model | phases deliver the spec's outcomes; no phase adds unrequested scope; ordering and dependencies hold; each finish line is checkable | APPROVE, or REVISE with cited phase/plan ids |
| P-M2 | model | a spec ambiguity that intake missed and that matters to the plan | ESCALATE with questions (recommended answers) |
| P-G1 | code | the 3rd REVISE | ESCALATE `rework_cap` |

**REVISE** follows approver D6: it becomes a TL-owned blocker, and the TL writes the instruction. The approver never
writes to the worker.

**Deterministic gates bind in every routing mode.** P-C1–P-C4 and P-G1 are code. They run on every plan whatever
`plan-approval` says (§5.3), before and independently of whoever gives the fitness ruling. The P-M rows are the only
part an answer can reassign.

**Owner holds.** P-C1, P-C3, P-C4, a P-M2 escalation and P-G1 each create blocker kind `plan_owner_hold`
(`owner_only=True`).
- Only an **explicit owner decision** clears it: a card answer whose actor kind is OWNER (approve plan / revise:
  <reason> / stop), recorded with the hold id.
- These are refused, by the same actor check as non-owner `accept()` (approver §3.4):
  - a TL ruling;
  - an approver ruling;
  - an operator agent;
  - the coordinator;
  - any auto-default.
- Questions attached to the hold are stored `blocking=true`, so neither the intake 30-min timer nor the coordinator's
  `recorded_default` rung can answer them.
- P-C2 is not an owner hold. It is mechanical: REVISE until it passes, then P-G1 at the 3rd round.

The plan ruling record is the approver verdict record (approver spec §6.2) with an added field `kind: plan`; acceptance
rulings carry `kind: acceptance`. Its `mode` field keeps its meaning (shadow or live). The record is stored in the
packet of the job's eventual acceptance ruling.

### 5.3 Routing (`plan-approval` decided: approver, the owner on high-risk only)

**Recommend mode** runs while the approver is at stage S1 (shadow, approver spec §9). It also runs for any
`plan-approval` answer other than the decided one. The decided answer makes autonomous plan APPROVE the target; it
starts when the approver reaches S2, so the plan gate never relies on an approver that has produced no reviewed
rulings yet. In recommend mode:
- the approver rules;
- every plan, including an APPROVE, becomes a the owner card;
- the card's recommendation is the approver's ruling;
- the options are "approve plan", "revise: <reason>" and "stop".

Routing per answer (P-C1–P-C4 and P-G1 run first in every mode, as above). The decided answer is the first:
- **Answer "approver; the owner on high-risk only"** (decided, the owner 2026-10-14): with no owner hold open, an approver APPROVE proceeds
  without the owner. ESCALATE and every owner hold reach him.
- **Answer "TL"**: the TL's unit-0 ruling replaces the approver's *fitness* ruling (P-M1/P-M2) only.
  - A TL `continue` past unit 0 is refused while any `plan_owner_hold` is open or P-C2 fails.
  - The TL has no ESCALATE verdict; it uses `ask_calen`, which creates the same owner hold.
- **Answer "The owner always"**: recommend mode stays. The deterministic gates still annotate the card and still create
  holds, so a high-risk plan cannot be approved by an answer to a lower-risk card.

Plan approval never activates AUTO-MERGE. Acceptance activation follows the approver spec's §9 stages and cards
independently.

### 5.4 After approval

- The contract's `units` are rewritten from the approved plan: one unit per `NN-MM`, each with `finish_line` = the
  plan's verify and success criteria. Contract revision n+1, actor `specflow`.
- **Baseline-fix obligations are mapped to phases.** Code builds kv `baseline_fix_obligations` at approval, with
  entries `{test_id, check, due, source}`. It considers only test ids that failed at the job's first baseline:
  - A `done_when` item that names the id (or a check whose purpose is to make it pass): `due` = the phase that §4
    step 1's coverage map assigns that done_when item to.
  - A phase success criterion in ROADMAP.md that names the id: `due` = that phase.
  - A job-level done_when item with no single phase (e.g. "the whole suite passes"), or one the coverage map spreads
    over several phases: `due = closeout`. Every baseline failing id it covers becomes a closeout obligation.
  - A done_when obligation that maps to no phase and is not job-level → P-C2 coverage gap (REVISE).
  - The map is shown on the plan card. It changes only with a plan re-ruling (§7.5), never through a TL ruling or a
    gap plan.
- The plan SHA is frozen (kv `approved_plan = {sha, ruling_id}`). Later plan edits are allowed only as TL-approved gap
  plans (`gsd-plan-phase --gaps`). The approver's acceptance ruling sees the diff between the approved plan and the
  final plan.

## 6. Execution: pause-review per phase

pause-review is used as specified: `pause = unit`, one TL review per pause, outcomes
`continue / send_back / done / ask_calen`, the TL is the only voice. Scaffold adds one TL outcome, `recover`, valid
only in a failed phase (item 4), plus the following:

1. **Unit = plan.** The worker executes one `NN-MM` plan (GSD execute-plan semantics), commits, pauses, and reports
   "unit NN-MM done; next NN-MM+1".
2. **Phase boundary** (the last plan of a phase). The unit's finish line also requires the **baseline-aware phase
   gate** to pass, and the phase SUMMARY to be committed. The TL rules `continue` into the next phase only on both.
   The gate is code, not TL judgment; it uses the same comparison as closeout verification (`verification recorded`:
   `new_failures`, `preexisting_failures`, `fixed`).
   - **Run.** Every contract check, as a Steward check run (`steward check run`) at the unit SHA. The worker's own
     claim never counts.
   - **Reference.** The job's first `baseline recorded` result, which holds the per-check failing ids at the base
     commit.
   - **Pass requires all of:**
     1. every check *ran* (no cannot-run or collection error);
     2. every `expected: pass` check passes;
     3. every `expected: baseline` check fails only on ids that also failed at baseline: no new failing id
        (`new_failures == []`);
     4. every obligation in `baseline_fix_obligations` (§5.4) with `due` ≤ this phase is now passing. Obligations due in
        later phases or at closeout are **not** required here.
   - Baseline failures that remain and are not yet due are allowed. They are carried forward and recorded in the
     checkpoint: `phase_gate: {phase, sha, ran, new_failures, preexisting_remaining, fixed, obligations_due,
     obligations_met}`.
   - **At job closeout.**
     - Every obligation, including `due = closeout`, must pass. Code refuses a TL `done` while any obligation fails,
       turning it into `send_back` (pause-review §7: a code check beats a TL verdict).
     - Baseline failures that are *not* obligations still produce the usual "baseline failure(s) still failing"
       reason. Under the approver spec, that reason is evidence for the approver, not a the owner park (approver §3.1).
   - **Fail →** the phase enters recovery (item 4). The TL cannot rule `continue` into the next phase.
3. **Fresh attempt per phase** (D6). After a phase-boundary `continue`, the runner closes the worker session and
   starts a new attempt in the same worktree. The brief carries the contract, the approved plan path, the phase
   SUMMARYs so far and the TL's checkpoint history. Within a phase, the session persists.
4. **Phase recovery: one transition for a failed phase gate.** Gap work is allowed, the failed phase stays
   incomplete, and the next phase is locked until the gate passes.

   **Phase states** (checkpoint `phase_state[k]`): `open → gate_pending → passed`, and `gate_pending → recovering →
   gate_pending`.

   **Unit states** (contract `units[i].state`):
   - `pending`;
   - `active`;
   - `met`;
   - `met_gate_failed`: the phase's last unit, whose own work is done but whose phase gate failed;
   - `met_pending_gate`: a recovery (gap) unit of phase k that its pause-review ruling found met, waiting for the
     re-run gate. It is never final on its own;
   - `locked`: every unit of phases after k while phase k is not `passed`.

   Transitions, all by code except the TL ruling:

   | Event | Phase k | Units | Allowed TL rulings |
   |---|---|---|---|
   | Last unit of phase k met → gate run starts | `open → gate_pending` | last unit `met` (provisional) | none until the gate result arrives |
   | Gate passes (first run: `gate_pending` entered from `open`) | `gate_pending → passed` | phase k+1 units `locked → pending`; the first one becomes `active` on `continue` | `continue` (into k+1; the fresh attempt per item 3), `done` (if no phases remain) |
   | Gate passes (re-run: `gate_pending` entered from `recovering`) | `gate_pending → passed`, in **one store transaction** with the unit changes | **atomically**, in the same transaction: the original `met_gate_failed` unit → `met`; every phase-k recovery unit in `met_pending_gate` → `met`; `recovery_round` recorded as closed; then phase k+1 units `locked → pending`. If the transaction fails, nothing changes and the phase stays `gate_pending` (re-run) | `continue` (into k+1, fresh attempt) or `done` (if no phases remain), only after the transaction commits |
   | Gate fails | `gate_pending → recovering`; `recovery_round += 1` | last unit `met → met_gate_failed`; phase k+1.. stay `locked` | `recover`, `send_back`, `ask_calen` only |
   | TL `recover` with a gap plan | stays `recovering` | gap units `NN-MM` appended **to phase k** (`pending → active`); a gap plan touching a risk path, a prohibition glob or a later phase's plan → `plan_owner_hold` / `plan_change` (§5.2, §7.5) | pause-review rulings on each gap unit; `continue` may only name a phase-k unit |
   | TL `send_back` (fix without a new plan) | stays `recovering` | `met_gate_failed → active` | — |
   | A gap unit's pause-review ruling finds it met | stays `recovering` | that unit `active → met_pending_gate` (not `met`) | `continue` to the next phase-k gap unit |
   | Last gap unit (or the reworked unit) met | `recovering → gate_pending` | last gap unit → `met_pending_gate`; gate re-run at the new SHA | none until the gate result arrives |
   | Re-run gate fails | `gate_pending → recovering`; `recovery_round += 1` | `met_gate_failed` and every `met_pending_gate` unit stay as they are (not `met`) | `recover`, `send_back`, `ask_calen` only |
   | Gate fails for the 3rd time in phase k | `recovering` | unchanged | `ask_calen` only (pause-review loop guard); the owner card names the failing checks and ids |

   - **The ruling.** `recover` is a new TL outcome, recorded as `{verdict: recover, phase: k, gap_plan: <path>,
     reason}`. It is only valid in phase state `recovering`.
   - **What code refuses**, as invalid verdicts converted to `send_back` with the refusal reason:
     - `continue` naming a unit outside phase k;
     - `done`;
     - `recover` in any other phase state;
     - **any unlock of phase k+1, `continue` into it, or `done` while any phase-k unit is `met_gate_failed` or
       `met_pending_gate`**, i.e. before the re-run gate's atomic transaction has marked the original unit and every
       completed recovery unit `met`;
     - any write that marks the original unit or a recovery unit `met` outside that transaction (piecemeal marking,
       a TL ruling, or the first-run row).
   - **Gap units are within the goal.** They use the TL's re-scope authority (pause-review §4.4) and never widen it.
     They do not change `baseline_fix_obligations` and do not count as new phases for the length bound.
   - **Session.** The fresh worker attempt (item 3) happens only on `passed`, never on entering `recovering`. Recovery
     keeps the phase's session.
5. **Done.** When every unit is met, the TL rules `done` and closeout verifies. Gate 2 then runs per the approver
   spec: the TL completion ruling plus the acceptance guards on the clean path, or the approver on any hold.

## 7. Steward-repo variant: plan, then one job per phase

Applies when the target repo is Steward's own code root (`launch.code_root()`).

1. **Scaffold job.** Intake → unit 0 (plan) → plan gate → the job ends. Its diff is `.planning/` only.
   - Gate 2 runs as for any job: the TL clean-path ruling plus the acceptance guards. The plan files are not risk
     paths. The approver is involved only if a hold reason appears.
   - It lands through the train like any job.
2. **Phase jobs.** On plan approval, `specflow.queue_phase_jobs` creates one staged job per phase (state
   `CONTRACTED`):
   - goal = "Execute phase NN of the approved plan <plan path>@<plan sha>";
   - contract units = that phase's plans;
   - checks and constraints inherited;
   - `seq_after` = the previous phase job (the first one is after the scaffold job);
   - `seq_after_merge = true`.

   `sequencing.sweep` launches each one only after its predecessor has **landed by the train** (and the train's
   deploy-self succeeded). So every phase runs under the supervisor its predecessor produced, never under code it is
   rewriting. This is how the pause-review parts landed.
3. Each phase job is a normal job: pause-review per plan, the approver at closeout, the acceptance guards, the train.
   A phase job whose diff touches `steward/approver.py` or other Steward risk paths escalates through the guards. That
   is intended.
4. **Failure in the chain.** A phase job that is STOPPED or escalated holds the chain: later jobs stay queued
   (`seq_after`). The chain does not skip. The card names the chain position and the remaining phases.
5. **Plan drift between phases.** A phase job may add gap plans *within its phase* (as in §6.4). A change to a later
   phase's plan needs a new plan ruling (approver plan mode on the changed phase only).
   - Trigger: a commit touching `.planning/phases/<later>/`.
   - Detection is specflow's own unit-pause fact (`plan_change`, a code fact in the pause-review packet), not a new
     acceptance guard. The TL cannot rule `continue` or `done` while a `plan_change` fact lacks a plan ruling.

The same chaining is used for non-Steward specs that exceed the length bound (§8.1). There, `seq_after_merge` is
`false` (launch after ACCEPTED), unless the repo deploys from its target branch.

## 8. Cost and length bounds

### 8.1 Length

| Bound | Value | On breach |
|---|---|---|
| Execution job size (planned) | ≤4 h estimated, ≤6 phases, ≤30 plans | Split at plan time into chained jobs of whole phases (§7 chaining) |
| Execution job wall-clock (actual) | 2× the planned estimate, at most 8 h | TL review with `ask_calen` option; notice to the owner with phase progress |
| Unit (one plan) | pause-review backstops: 20 min nudge, 35 min snapshot, 60 min liveness | unchanged |
| Send-backs per unit | 3 (pause-review loop guard) | `ask_calen` |
| Intake | 4 generation + 2 fold calls (§3.2), 20 min each | NEEDS_HUMAN `intake_failed` |

### 8.2 Cost (per phase unless stated)

Enforced through the existing spend ledger and limits (`spend_policy`, `spend_limits`) with source tags. Call caps
cover unpriced calls.

| Budget | Default | Basis | On breach |
|---|---|---|---|
| Worker spend per phase | 2× the phase's share of the planned estimate; at least $5, at most $25 | Trinity: $13 / 5 phases ≈ $2.6 per phase | TL review: `continue` (raises the cap once by 50%), `send_back` (narrow), or `ask_calen` |
| Supervision $ per phase (TL + independent review + approver + intake/plan rulings) | ≤ max($3, 1.0× the phase worker spend) | Brief: Trinity at 784%; pause-review moves the cap to per-job supervision spend | Review "done" claims only (pause-review §7), plus one notice |
| Supervision calls per phase | TL ≤ 1 per pause + 4; Jev observer ≤ 1 per 60 s while the worker is active (Trinity: 634 Jev rows in 2 h ≈ 1 per 11 s) | Unpriced calls (Jev, most TL rows) escape a $ cap | Excess Jev observations are dropped (it decides nothing, pause-review D2); an excess TL ruling waits for the next pause |
| Intake | ≤$3 for G1–G3 and folds (Sol/Opus); G4 Fable ≤$10 when F-a/F-b fires | estimate | NEEDS_HUMAN `intake_failed` |
| Unit 0 (plan) | $15 worker / 150 calls / 60 min | estimate | TL ruling |
| Approver | approver spec §4: $5 per ruling, $60 per day | — | approver spec §8 |

All defaults are config (`steward.toml [specflow]`), reported per job in `steward metrics specflow`: planned vs actual
hours and $, supervision ratio, calls per actor. They are engineering defaults, not owner values. The owner may change
them, but no card is needed.

## 9. Handoff to the approver

Each execution job (single, chained or Steward per-phase) ends at gate 2 as the approver spec defines it (§3 there):
- the TL clean-path ruling, guarded by `acceptance_guards`;
- or, on any hold, the approver.

Specflow adds the following to the approver packet. The guards and the TL completion ruling also see the plan ruling
and the constraints.

| Input | Use in the approver rubric |
|---|---|
| `class` from intake | C3 (`owner_review` → ESCALATE) |
| `constraints` with `origin=intake`, `source_quote`, `parser_version` | §3.5 provenance resolves directly; a quoted owner sentence = owner prohibition |
| `spec_source` path@sha and intake `assumed[]` | M2: done_when judged against the spec, including what intake assumed |
| Plan ruling (`kind: plan`) and the approved-plan SHA vs the final plan diff | M2: delivered vs approved; unapproved plan changes are a finding |
| Per-unit TL verdicts and phase check runs | Evidence for M2; replaces asking the worker for proof |
| Chain position (phase k of n) | The card names the remaining phases; "bank the finished part" maps to accepting phase k |

The acceptance guards (approver §3.4) run on every phase job exactly as on any job. AUTO-MERGE for specflow jobs
follows the approver rollout (§9 there). It needs the `risk-paths` and `go-live-bar` cards answered, and the repo in
`live_repos`. Until then, every specflow hold the approver rules on ends in an ESCALATE card recommending its verdict.
Clean-path acceptance is unchanged from today, apart from the guards.

## 10. Relation to existing actors

| Actor | Role in specflow |
|---|---|
| Intake (new, a state plus a model call) | Writes the working contract; asks material questions; the only actor in `INTAKE` |
| Tech lead | Pause-review rulings on unit 0 and every plan unit; owns REVISE and SEND-BACK blockers; the only voice to the worker; gap replans within the goal |
| Approver | Plan mode at the plan gate; acceptance mode at each job's closeout; never writes to the worker |
| Coordinator | Skips `INTAKE` jobs and plan-gate waits (it never answers intake questions early: the intake timer owns defaults). Unchanged otherwise |
| Sequencing / landing train | Chain execution jobs (`seq_after[_merge]`); land each job; the train's revert-on-red feeds approver demotion |
| Jev / liveness | Watch between pauses, decide nothing (pause-review D2); Jev observation rate-capped (§8.2) |

## 11. Rollout, testing, metrics

Rollout (config `[specflow] mode = off | intake_only | plan | full`):

1. **intake_only.** `steward spec` runs intake and produces the contract and questions; the owner launches by hand. Exit:
   5 specs with ≤1 material question each on average, and no dropped prohibition (validator).
2. **plan.** Adds unit 0 and the plan gate in recommend mode (§5.3). Exit: 3 plans that the owner approved without
   revision, and the approver at stage S2.
3. **full, non-Steward repo.** Adds execution with pause-review per plan. Trial on one real spec of Trinity size.
   Compare with Trinity v0.2 on hours, $ and supervision ratio. Pass: no worse than Trinity on hours and $; supervision
   ratio ≤100%.
4. **full, Steward repo.** The §7 variant. The first run is a small spec, 2 phases.

Testing:
- **Intake validator.** A quoted prohibition is required. A dropped prohibition is caught by the parse cross-check.
  A deliverable matching a prohibition → `constraint_conflict` (blocking). An invented prohibition with no quote is
  rejected. Class forcing. A check with pipes is rejected.
- **Checks lifecycle.** A cannot-run check is rejected. A base-failing check is kept as `expected: baseline`.
  Remember/forget per repo.
- **Questions.** Non-material ambiguity → `assumed`, no question. Material → a question with a recommendation. The
  30-min default fires and is recorded as `defaulted`. A blocking question never defaults.
- **State machine.** No worker start in `INTAKE`; `INTAKE` → `CONTRACTED` only on a valid contract with no blocking
  question; `intake_failed` when the §3.2 generation or fold budget is exhausted.
- **Intake ladder and budget.**
  - G1 valid → stop at 1 call.
  - G1, G2 invalid, G3 valid → 3 calls, no Fable.
  - G1–G3 invalid → G4 Fable (F-a), then valid, or `intake_failed` at 4.
  - Steward repo + predicted risk path on G1's valid proposal → G4 (F-b) runs once. Steward repo with no risk
    prediction → no G4.
  - G3 valid + F-b → G4 is the 4th call and within budget.
  - F-b invalid → the earlier proposal stands, plus a `fable_check_failed` material question.
  - F-b disagreement → one question per differing field.
  - Fold: 2 calls max, then `intake_failed`.
  - No path exceeds 6 calls.
- **Baseline-aware phase gate.**
  - A baseline-failing id still failing at the phase boundary → pass, carried forward.
  - A new failing id in an `expected: baseline` check → fail.
  - An `expected: pass` check failing → fail.
  - A check that cannot run → fail.
  - An obligation due in this phase still failing → fail.
  - **Obligation due later:** an obligation mapped to phase 2 still fails at the phase 1 boundary → the phase 1 gate
    passes. At the phase 2 boundary, the same id still failing → fail.
  - An obligation with `due = closeout` still failing at every phase boundary → those gates pass, but the TL `done` is
    refused and turned into `send_back`.
  - A non-obligation baseline failure remaining at closeout → an approver evidence reason, not a refusal.
  - Obligation mapping at plan approval:
    - a done_when naming an id → the covering phase;
    - "whole suite passes" → closeout;
    - an unmappable item → P-C2 REVISE.
  - The TL `continue` is refused on a failed gate.
  - At closeout, remaining non-obligation baseline failures become an approver evidence reason, not a the owner park.
- **Phase recovery.**
  - Gate fail → phase `recovering`, last unit `met_gate_failed`, next-phase units `locked`.
  - **Atomic close on a successful re-gate:**
    - After `recover` with two gap units, both met → both `met_pending_gate`. The re-gate passes → the original unit
      and both gap units are `met` and phase k+1 is unlocked, all in one transaction.
    - A failure injected mid-transaction leaves every unit and the phase state unchanged.
    - `done` or `continue` into k+1 attempted between "gap units met" and the re-gate result → refused.
    - A direct attempt to mark a `met_pending_gate` unit `met` outside the transaction → refused.
    - A re-gate failure leaves `met_gate_failed` and the `met_pending_gate` units unchanged.
  - TL `continue` into phase k+1 is refused (converted to `send_back`); TL `done` is refused.
  - TL `recover` with a gap plan → gap units appended to phase k, worked and reviewed. The gate re-runs after the last
    gap unit; pass → `passed`, next phase unlocked, fresh attempt.
  - TL `send_back` during recovery → the unit goes back to `active` and the gate re-runs on the next pause.
  - `recover` outside `recovering` is refused.
  - A gap plan touching a risk path → `plan_owner_hold`; touching a later phase's plan → `plan_change`.
  - A 3rd gate failure in the same phase → only `ask_calen` is accepted.
  - No fresh session on entering recovery.
- **Plan gate.**
  - P-C1..P-C4, P-G1 with a fake approver.
  - **Deterministic gates in every mode:** for each `plan-approval` answer (unanswered, approver, TL, the owner-always), a
    plan with a risk path, a prohibition-glob match, a floor need, class `owner_review`, a coverage gap or a checker
    failure is held or revised exactly as in §5.2.
  - Under "TL", a TL `continue` past unit 0 with an open `plan_owner_hold` is refused.
  - **Only the owner clears a hold:** a TL ruling, an approver APPROVE, an operator-agent answer, a coordinator
    `recorded_default` and the intake 30-min timer each fail to clear `plan_owner_hold`. An OWNER-actor card answer
    clears it.
  - Recommend mode while the approver is at S1 (or under a non-decided answer): an APPROVE still produces a card. At
    S2 under the decided answer: an APPROVE with no owner hold proceeds without a card.
  - The answered modes route as in §5.3.
  - Contract units are rewritten from the plan; the approved-plan SHA is frozen.
- **Execution.** Phase-boundary finish line requires a Steward check run. A fresh attempt starts after a phase
  boundary. A gap plan touching a risk path → `ask_calen`.
- **Steward variant.** Phase jobs are queued with `seq_after_merge`; the next launches only after `landed_by_train`. A
  stopped phase holds the chain. A later-phase plan change triggers a plan re-ruling.
- **Bounds.** Over-length plan → split into chained jobs. Phase $ and call caps trigger the TL or a drop as specified.
  The Jev rate cap is applied.

Metrics (`steward metrics specflow`):
- intake questions per spec (material, defaulted, answered);
- intake model mix and cost;
- plan revisions and escalations;
- planned vs actual hours and $ per phase;
- supervision ratio per phase;
- chain latency (landed → next launch);
- The owner touches per spec, correctly attributed (approver spec job A0).

## 12. Owner decision (the owner, 2026-10-14)

**`plan-approval` → the approver** (fresh-context Opus, not the job's TL), with the owner only on high-risk plans: risk
paths or his stated prohibitions, floor needs, owner-review deliverables, or an approver escalation. Note: "go with
recommendation".
- Applied in §5.3.
- Autonomous plan APPROVE starts at approver stage S2.
- The deterministic gates and owner-only clearing (§5.2) are unchanged.
