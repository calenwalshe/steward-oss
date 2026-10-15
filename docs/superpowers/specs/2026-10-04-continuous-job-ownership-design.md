# Continuous job ownership in Steward

Date: 2026-10-04
Status: Approved by the owner on 2026-10-05 for handoff to a new Steward job to create detailed execution plans and then drive implementation. See the execution handoff for authorization and completion boundaries.
Authoring workflow: superpowers brainstorming → writing-plans. Execution handoff is settled separately; Steward agents retain freedom to choose their working methods.
This initiative does not modify `.planning/` or use GSD for planning or execution.

## 1. Outcome and scope

Once the owner authorizes a job, Steward owns getting it to its agreed finish line. It diagnoses failures, challenges incorrect blockers, continues authorized work, and maintains responsibility across worker completion, verification, parking, acceptance, incorporation, and activation where required. Human attention is for actual owner decisions, not routine recovery or stale internal state.

Strengthen the existing controller and tech lead. Do not add a supervisory agent above Steward or a second source of job truth. Keep the existing Store, control loop, worker lifecycle, permission mechanisms, verification machinery, and audit events; introduce narrow components where responsibility needs a clear boundary.

The work covers controller continuity, authorization propagation, technical review and diagnostic access, recovery and advisory budgets, evidence reconciliation, and delivery. It does not implement the Circuit Tracks device repair, Turing front-door changes, or unrelated product features. Those incidents supply regression scenarios.

### Agent freedom in execution methods

Steward supervises outcomes, progress, evidence, authority, recovery, and completion. Workers and technical leads may choose GSD, Superpowers, other available tools, or direct work as appropriate within the job's instructions. The owner need not name later-stage skills. Using Superpowers to author this initiative's design and plans does not prescribe a package for every Steward job or require a particular execution method.

Preserve access to available skill libraries, but do not add a mandatory skill-selection service, fixed skill sequence, or skill-use approval gate. The lead may challenge an ineffective approach based on observed progress and evidence without requiring a named replacement skill. Existing skill suggestions remain guidance; skill invocation is not proof of correctness or completion. Framework-internal stop/ask defaults follow the compatibility rule in §3; actual owner decisions remain binding. Steward retains job ownership regardless of the selected method.

## 2. Owner decisions carried forward

- Prefer continuous ownership in the existing controller over isolated patches or a new supervisory layer.
- The tech lead reviews ambiguous blockers before they interrupt the owner. Review latency is acceptable.
- A `NEEDS_HUMAN` label is revisable when its underlying classification is mistaken; it cannot prevent technical inspection.
- Judgment-based recovery uses the existing confidence threshold of 0.8. Confidence never substitutes for authorization or completion evidence.
- Unsuccessful recovery triggers independent diagnosis and a revised approach, not automatic owner escalation after a fixed number of attempts.
- Ordinary budget estimates are advisory: alert, revise the estimate, and continue authorized work. Alerts do not require acknowledgment.
- Explicit owner-imposed hard limits remain binding. No authority expansion follows from an advisory budget.
- When diagnosis finds no credible next action, park the affected work with a precise blocker and wake-up condition, cheaply monitor it, and continue independent work.
- The lead inherits the job’s authorized read-only diagnostic access, including named remote hosts and services, without broader write rights or secret exposure.
- Independent diagnosis uses a fresh reviewer context and preferably a different model.
- Repeated escalations update one blocker; newer evidence can automatically clear an obsolete blocker when its explicit clearing condition is satisfied.
- Human, operator-agent, tech-lead, and controller decisions remain distinguishable.
- Acceptance remains human-owned for the workflow discussed here. Existing unrelated jobs retain their explicit acceptance policies; do not globally flip acceptance configuration.
- After acceptance, continue incorporation or activation already authorized by the owner. Neither is inferred merely from acceptance.
- Use superpowers for the written design and implementation plans for this initiative.
- Leave execution methods to the agents within the job's instructions; Steward is a general autonomous work supervisor, not a GSD or Superpowers runner.
- Owner approved bounded, redacted excerpts of specifically cited artifacts being sent to Jev for evidence support. This supersedes the imported research’s no-new-excerpts restriction for this lane only; it is a design decision, not live activation.

## 3. Current implementation and reuse

Inspected source branch: `fleet-registry`, HEAD `2817345` when drafting. These are observations, not assumptions that execution may skip rechecking.

- Job `bf5237fe` implements parked review/reopen on `steward/bf5237fe-e754-423b-bdad-c665cb7a5640`, ending at `1f416b2`. Its recorded verification reports 4680 passed, zero failed, but it remains NEEDS_HUMAN with no merge commit. Reuse reviewed code rather than rebuild it. Green tests do not establish every finish-line item or authorize incorporation.
- The earlier parked-reopen spec and plan are in this directory and `../plans/2026-10-04-tech-lead-parked-reopen.md`. Their fixed reopen/exhaustion cap, narrow inspection scope, and acceptance classification are superseded where they conflict with this design. A STEWARD-DONE marker alone must not make a technical blocker owner-only.
- Job `f6eb72cf` delivered baseline exception handling (`bad0b83`) and late-baseline attribution (`06e0ca4`), incorporated as `2817345`. Preserve these fixes. Incorporation does not establish which code running processes loaded.
- `steward/questions_delivery.py:tick` already visits resting jobs through the control loop. Preserve its cheap, nonblocking character; schedule model/network work asynchronously.
- `steward/contract.py` has authority, boundaries, keys, and revisions but no structured operational grant collection. `propose_contract` adds a fixed outbound boundary. Grants need explicit provenance and consistent downstream interpretation.
- `steward/tech_lead_policy.py` defaults to confidence 0.8, active cadence 10 minutes, quiet cadence 60 minutes, review timeout 300 seconds, and fix_rounds 2. Preserve useful scheduling defaults. A round threshold becomes a strategy-review trigger rather than proof the owner is needed.
- Reuse `fleet_tech_lead.py`, `tech_lead_actions.py`, notebook/playbook, resolver, permissions, and floor checks. Diagnostic authority requires an explicit scoped path; do not simply remove SSH/outside-worktree protections.
- `steward/bg_baseline.py`, `bg_verify.py`, `verify.py`, and `closeout.py` own check execution and attribution. `steward/autopilot.py` owns incorporation/fixups; there is no `autopilot_merge.py`.
- Duplicate fixup claiming was already repaired in `a48a15d`. Preserve it and investigate actual remaining chain/closure failures rather than assuming every historical case is still broken.

Source evidence: `/home/user/.claude/responses/2026-10-04/steward-authorization-audit.md`; `docs/diagnostics/2026-10-04-merge-housekeeping/GOAL.md`; Happy transcripts `212fb583-ce96-45bd-bde4-b655bb030bb7`, `2fa6145e-d931-4631-bd6f-f81d358e64a4`, and `20efeafa-6c53-47e8-978f-5c35a7800703` under `/home/user/.claude/projects/-home-agent/`. Historical reports contain corrected diagnoses; fixtures must use the final supported evidence, not earlier confident claims.

### Compatibility rule settled in review

Classify each existing guard by origin and purpose before adapting it:

| Class | Required treatment |
|---|---|
| Explicit owner instruction: stop, scope prohibition, hard spending limit | Binding; technical review cannot override it. |
| System default/workflow restriction: running-only review, fixed retry exhaustion, default job spending threshold | Replace when inconsistent with this design; it cannot independently establish a human requirement. |
| Execution protection: writer lease, stale-revision fence, bounded invocation | Preserve; waiting and recovery remain controller responsibilities. |

Each implementation plan must map the old guard, its source location, its category, its replacement or retained behavior, and a full-path regression test. At minimum cover `flow_actions.validate`'s RUNNING-only restriction, `tech_lead_floor.check`'s SSH/outside-world and spend checks, `runner` recovery/finish/verification escalation sites and research-cap handling, and `flow_investigator`'s per-call budget enforcement and response rejection. Use this inventory to change the actual execution path, not just the new reconciliation layer. Flow's existing permission/final-action restrictions must not be widened wholesale; delegate only to mechanisms authorized for the specific operation.

An unfamiliar restriction prompts investigation of provenance, not automatic owner escalation or automatic bypass. Independent authorized work continues. If provenance cannot establish permission for the affected action, keep that action waiting and surface only a concrete unresolved owner choice when needed.

## 4. Durable ownership and reconciliation

For every unfinished job, persist a projection in the existing Store containing remaining outcomes, evidence references, blockers, required delivery milestones, and the next responsible actor. Preserve lifecycle compatibility: initially add blocker/review status alongside existing job states rather than redesign every state consumer.

A blocker records its stable identity, kind, claim, evidence/version references, affected work, responsible actor, review state, clearing predicate or required decision, wake-up condition, and history. Distinguish candidate, reviewing, confirmed-human, waiting-external, and resolved dispositions. Different blockers can coexist; clearing one does not clear others.

Reconcile on relevant events and a periodic control-loop sweep. A repeat of the same snapshot must not duplicate a worker launch, review, notification, send-back, or merge fixup. Persist action intents/claims and completion receipts with the existing transaction/lease conventions. After a crash, reconcile uncertain actions against receipts and actual worker state before retrying. Never create a second writer because a timeout hid the first result.

Worker-finished is not job-finished. Accepted is not incorporated. Released/stopped/cancelled jobs must not be reopened by this loop; explicit owner stops prevail. Accepted jobs remain tracked if authorized delivery is outstanding, without reopening their coding worker unnecessarily.

Reconciliation is deterministic where facts suffice. Uncertain clearing conditions go to technical review rather than being guessed true. One broken job must not block other jobs or the control loop.

## 5. Review before human escalation

An ambiguous worker refusal, test failure, exhausted recovery, or policy misunderstanding creates a candidate blocker. Apply cheap checks for existing grants, answers, valid verification, and duplicate evidence first. If unresolved, enqueue technical review and show “awaiting technical review.” Do not send an owner-action notification merely because a legacy job state says NEEDS_HUMAN.

The lead may overrule the candidate, investigate it, or confirm a genuine human requirement. A confirmed human request states the exact decision/access/physical action required, evidence, why existing authority is insufficient, and independent work that continues. Known owner-only decisions can go directly to the owner without paying for model review.

Apply this path to already parked jobs as well as new incidents. Preserve genuine unanswered owner questions. A lead may withdraw its own mistaken escalation; deterministic evidence may resolve an obsolete request. Neither can invent an owner answer, reverse a stop, widen scope, or reinterpret an explicit prohibition away.

Review scheduling uses blockage impact, age, and changed evidence; retain active/quiet cadence defaults and immediate routed facts. Aging must prevent low-priority jobs from starving. Permit only one in-flight review per job and coalesce related facts. Persist diagnoses and reuse them when evidence is unchanged. Reviewer timeout/unavailability is a service blocker with fallback/backoff, not evidence that the owner must solve the engineering problem.

### Reviewer outage behavior settled in review

Try the configured reviewer, then an eligible fallback. Timeout, malformed output, or provider failure is a failed invocation, never a substantive negative verdict. If all eligible reviewers are unavailable, show “technical review unavailable,” the affected work, and the next retry time. Keep that work pending, continue independent authorized work, and preserve pending review through controller restart.

Retry service availability with backoff; changed job evidence is not required for an outage retry. This differs from repeatedly paying to reconsider an unchanged engineering diagnosis. Bound availability checks and coalesce review requests so recovery does not launch duplicates.

Send one operational alert when the outage prevents progress, updates only for material changes, and a recovery notice. These notices require no acknowledgment and must not imply the owner owes an engineering decision. An actual owner remedy, such as restoring account access, may produce a precise request with evidence. Review failure alone never establishes NEEDS_HUMAN.

## 6. Authorization and independent diagnostic reach

Represent operational grants as revisioned job authority with source, granting actor, resource scope, permitted operation, and relevant conditions. All deciders use the same effective authority: worker preauthorization, permission resolver, controller, lead packet, and action gate. Required grant details must not disappear under prompt truncation. Large scopes require structured lookup/validation, not silent omission.

Keep the lead’s own action boundary distinct from worker authorization. The lead can direct a permitted worker operation without granting itself the worker’s mutation rights. Explicit prohibitions and revocations win; actions revalidate the current authority revision immediately before execution.

Diagnostic reads may use job-authorized named hosts/services and existing credentials through supported adapters. Record the target, operation, authority reference, and sanitized evidence. An SSH transport is not inherently read-only: validate the actual diagnostic operation and scope, including compound commands and redirects. Do not expose secret values in packets or logs, inherit all machine access, or add a generic shell bypass.

For legacy prose grants, recover unambiguous authorization from the recorded owner source with provenance. Do not fabricate grants from worker output, retrieved documents, or a model’s assertion that permission exists. Ambiguous scope is reviewed; only an actual missing owner choice warrants a new question. Empty/missing structured grants alone must not make the owner repeat clearly recorded authorization.

## 7. Recovery, independent review, and spend

Each attempt records a hypothesis, supporting evidence, what changes, and the observation that would prove progress. A repeated failure without new evidence triggers a strategy change or independent diagnosis. The existing two-round threshold may trigger diagnosis, but does not by itself prove NEEDS_HUMAN.

An independent reviewer gets a fresh context, original evidence, attempts, grants, constraints, and the claimed diagnosis to challenge. Prefer another eligible model; if unavailable, a fresh context on the same model is allowed and disclosed. A new attempt needs a credible mechanism and observable success condition, not just another confidence score.

Distinguish advisory estimates from explicitly binding owner limits in persisted policy, including provenance. Advisory overruns produce a deduplicated notification with known spend, uncertainty, progress, and revised approach/estimate; continue without acknowledgment. Missing price data is unknown, not zero. Worker, supervision, and independent-review spending all contribute to reporting. Do not implement a hidden maximum multiplier that converts the estimate back into a stop.

Explicit owner hard limits, provider limits, credential failures, and actual resource exhaustion remain real constraints. For owner hard caps, account for in-flight commitments and avoid starting work whose bounded cost cannot fit. Legacy limits require a provenance-aware migration: do not silently soften an explicit cap or treat a system default as an owner instruction. This change in default behavior must reach every stop path, not merely the dashboard.

Individual model calls remain bounded execution attempts, not hard job budgets. Preserve useful partial evidence/checkpoints when an invocation ends at its allowance; incomplete or unvalidated output cannot authorize an action. Distinguish interruption from a completed negative verdict. Resume from a checkpoint, narrow the question, or use another reviewer when that has a credible reason to help. A larger subsequent call may be justified and recorded without owner approval merely to exceed a system-default allowance, subject to existing authorization and explicit hard limits.

Do not discard otherwise useful returned evidence solely because reported cost exceeded a system-default call allowance. Validate it normally and account for the actual cost; invalid action proposals remain ineligible for execution. Repeated call exhaustion without progress triggers independent diagnosis of the approach rather than endless fresh allowances. Summed default invocation limits never become an implicit job cap. Aggregate worker/reviewer spending still contributes to advisory alerts and explicit owner hard-limit enforcement.

When no credible action exists, wait on a precise condition. Cheap bounded probes may check that condition; expensive diagnosis requires new evidence or a recorded novel approach. No endless paid timer loop. Continue independent authorized work without violating shared-resource locks.

## 8. Evidence and escalation hygiene

Use a stable issue identity plus an evidence generation to combine repeated sightings while allowing a resolved problem to recur as a new episode. Keep one pending escalation per issue episode. Retain history when superseding or resolving it.

Evidence references identify the candidate revision, check scope, environment/configuration, and relevant contract revision. Current matching verification can automatically clear “verification missing”; an answer clears only its question; a valid grant clears only its scope blocker. Changed code invalidates code verification. Changed relevant authority/configuration invalidates affected decisions, not unrelated evidence indiscriminately.

Verification proves what it exercised. An idle-session port check does not prove an active transfer recovers. Unit checks do not establish deployment activation. A green suite does not fulfill untested finish-line outcomes.

Every decision carries actual actor and origin. Operator-agent disagreement is not human overrule. Corrected blockers and independent-review outcomes update notebook/playbook scoring using the final evidence, while preserving prior recorded judgments.

Coordinate expensive check runs. Reuse only results matching candidate, check selection, environment, and baseline/attribution requirements. Claim equivalent pending runs instead of launching duplicates. Queue heavy suites with an initial default of one heavy suite per host; scoped checks can proceed when resource limits permit. Make queue progress visible; long-running checks are not automatically worker failures. Durable receipts and recovery handle lost processes without accepting missing results.

## 9. Delivery continuity

Track only milestones required by the job: worker completion, verification, acceptance, incorporation, activation. Each has evidence and a responsible actor. Existing per-job acceptance policy is honored; this design does not grant universal auto-acceptance, merge, restart, or deployment.

When an owner decision is genuinely required, present the verified result and exact next decision. Once supplied, carry existing delivery authorization forward. Activation evidence names the running revision and required live checks; a repository merge alone is insufficient.

Preserve the existing atomic fixup claim. When a target branch advances, re-read it, diagnose current conflict/evidence, and reuse the existing repair episode when possible. Do not reflexively spawn a child of a merge-fixup job. Real new conflicts receive revised diagnosis and appropriate verification. Serialize incorporation per target branch and verify the actual candidate to be incorporated; a stale pre-merge green result does not prove the merged tree.

## 10. Implementation plan boundaries and order

Use four separate superpowers implementation plans, linked to this shared design. Each must deliver testable behavior and define exact interfaces for dependent work. These are plan scopes, not approval to execute or deploy. Sections 13–15 extend their responsibilities with Jev evaluation, system-wide investigation, and cross-job learning; all belong in the same four-plan dependency structure.

1. **Ownership and evidence reconciliation.** Review/adapt the parked-reopen branch; add durable blocker ownership, deterministic clearing, idempotent review/action scheduling, honest actor attribution, legacy-state display/notification compatibility, and restart recovery. Preserve existing closeout fixes. Surface remaining genuine human decisions accurately.
2. **Authorization and diagnostic access.** Carry effective grants to every decider, distinguish worker/lead scope, support scoped remote diagnostics, and migrate recorded grants with provenance. Integrate with Plan 1’s blocker identities and clearing predicates.
3. **Recovery and advisory budgets.** Add independent diagnosis, evidence-based retry strategy, waiting conditions, scheduling/backoff, and provenance-aware advisory versus hard limits across execution and reporting. Depend on Plans 1–2 for ownership and diagnostic authority.
4. **Verification and delivery continuity.** Coordinate heavy verification, reconcile acceptance/delivery milestones, repair evidenced merge-chain/closure defects, and prove authorized activation. Reuse Plans 1–3 rather than invent a separate delivery supervisor.

Planning must inspect the existing flow-observer/investigator modules before introducing overlapping mechanisms. Prefer extension to a duplicate ledger or investigation framework. Each plan must include the compatibility map specified in §3 and tests through downstream guards, not only new component tests. Plan 1 owns persistent review-outage handling and notification semantics; Plan 3 owns per-call continuation and aggregate spending semantics. Each plan should list retained existing behavior and any intentional supersession of old requirements. Do not edit governed `.planning/` files to express those changes.

## 11. Verification and rollout

Use isolated Stores, fake clocks, fake worker/model/host adapters, and deterministic event replays for regression coverage. Never use production job state as a mutable test fixture. Reference sanitized evidence shapes from the actual incidents.

Required scenarios:

1. A parked unfinished job receives review with no live runner; duplicate ticks launch only one review/worker.
2. A lead uses an authorized remote read to disprove a local worker claim; unrelated hosts and mutating commands remain denied.
3. A grant beyond the old goal-prefix truncation reaches resolver and lead; revoked authority invalidates a queued action.
4. Current green verification clears its stale blocker while a separate genuine owner decision remains open.
5. Answer delivery resumes eligible work once; unanswered questions and owner stop remain binding.
6. Repeated failure triggers fresh independent diagnosis; no credible next step parks with a wake-up condition and no paid polling loop.
7. An advisory overrun alerts once per meaningful update and continues without acknowledgment; an explicit hard cap remains enforced.
8. Operator-agent decisions retain provenance and cannot train the playbook as human overrules.
9. Concurrent/restarted controllers do not duplicate actions; older review responses cannot act against newer evidence or authority.
10. Equivalent heavy suites share a claim; different revision/environment results are not reused; queued work eventually runs.
11. Acceptance plus existing delivery authorization progresses through incorporation/activation; absence of that authority produces the exact owner decision.
12. A moving target branch reuses a repair episode where possible and verifies the merged candidate without duplicate fixup chains.
13. A narrow green test cannot close an outcome requiring an untested live flow.
14. Legacy jobs are reconciled conservatively without losing explicit grants, stops, hard caps, approvals, or pending delivery.
15. All reviewers unavailable: one visible operational alert, no false owner-action request, independent work continues, pending review survives restart, and service recovery resumes it exactly once with a recovery notice.
16. Default call allowance exhausted: preserve useful partial evidence, forbid execution from incomplete proposals, and continue with a justified checkpoint/narrowed question/different reviewer/larger allowance without owner acknowledgment; explicit hard caps still bind.
17. A mistaken blocker passes through the real legacy guards, is corrected by technical review, and the authorized worker actually resumes; counting a review event alone is insufficient.
18. A confirmed human request includes the exact decision and why current authorization cannot resolve it. No alert or display may conceal an unresolved job to improve the prompt count.
19. Equivalent jobs using a skill-guided method or direct work are judged against the same required outcome evidence and authority. Absence of a named skill is not a blocker; invoking one cannot bypass verification. Exercise an internal workflow stop through technical review while preserving an explicit owner stop.

Implement with failing-then-passing tests per behavior and atomic commits. Run scoped suites for each task and the existing full suite serially at integration. Live qualification follows only after isolated tests and uses explicitly authorized scratch jobs/targets. Production incorporation/activation is a distinct, reviewable step under existing authorization; writing this spec authorizes neither.

Qualification must inspect the status and notifications the owner actually receives, in addition to Store events. Assert whether an owner-action request was emitted, whether the worker resumed, whether unrelated decisions remained open, and whether required authorized delivery completed. A “review executed” or “action proposed” event is not a successful outcome.

Record unnecessary owner requests, missed genuine owner decisions, blocked duration, review wait time, repeated reviews on unchanged evidence, duplicate actions, recovery outcomes, supervision spend versus estimates, and time between verified and delivered. In deterministic fixtures the expected human requirement is known. In live observation, classify necessity from explicit requirements and recorded review/outcome evidence, retain unknown cases, and do not let a model's self-rating establish success. Report observations without inventing numerical success claims. Fewer prompts alone is not success: hidden stalled work or unauthorized action fails qualification. A rollout succeeds when the regression scenarios pass and controlled authorized live qualification demonstrates correct progress and human decisions with actual receipts.

## 12. Proposed implementation defaults for written review

The owner agreed the behavioral design above. The following concrete defaults are design proposals, not separately recorded owner instructions: retain 10-minute active/60-minute quiet cadence and 300-second review timeout; use two unsuccessful rounds as an independent-diagnosis trigger; start with one heavy suite per host; use fresh same-model context when an alternative reviewer is unavailable. Configuration may tune these without disabling authority checks or converting advisory spending into an implicit hard cap.


## 13. Jev research integration

Imported at the owner's request from workspace w6S, “RESEARCH · Steward — Jev governance design.” The [research import and mapping](../../research/continuous-job-ownership/jev-import/README.md) preserves the source documents, conversation decisions, provenance hashes, historical limitations, and opportunities O-01–O-16 / requirements JT-01–JT-17. These are design inputs; they do not merge or activate any existing Jev job.

Jev supplies bounded noticing/classification signals to the existing controller and tech lead. It may help notice drift, permission confusion, repeated failure, fleet overlap/runtime trouble, or potentially unsupported evidence. It does not grant permission, clear a blocker, establish completion, accept/merge work, or decide the owner is required. Its output enters candidate review and is revisable as evidence changes.

Measurement and replay precede new Jev questions, tuned gates, or promotion. Preserve separate evaluation/activation for each supervision use and the independent interactive skill-selection lane. Reuse the existing Store and flow/fleet evidence rather than create a second ownership ledger. Bounded judgment recording follows §14 and identifies caller/pack/model/config versions, probabilities, status, latency, and sanitized evidence references, with a defined retention policy. Replay uses isolated state and cannot contact real providers or notifiers. A reproducible recorded decision is not proof of live model correctness.

Jev may prioritize costly reviews but cannot suppress mandatory review of an unresolved blocker; code facts and aging continue to route work when Jev is uncertain or unavailable. Distinguish a Jev outage from unavailability of the tech-lead reviewers. Reuse the existing outage alert mechanism, extending missing call-site coverage rather than producing duplicate alerts. Deterministic fallback covers only what code can observe; do not claim it reproduces semantic drift judgment.

The tech lead's 0.8 action threshold is not automatically a Jev classification threshold. Pin and evaluate each question pack separately, report misses/false wakes and coverage, and test option-order sensitivity and worker-text steering. Code retains quantities, authorization, revision identity, and verification outcomes. Independent questions may share a request; dependent judgments need their own evidence stage.

The owner has now approved small, redacted excerpts of specifically cited artifacts for Jev evidence review, settling the question left open in the imported brainstorm. This extends the evidence lane’s permitted data categories; the existing observation lanes retain their 1,800-character envelope. The new evidence lane must have its own explicit, enforced request/excerpt size limits before activation, rather than silently expanding every Jev request. The implementation plan will specify those numerical limits and fixture coverage.

The controller resolves the cited artifact under the job’s existing read authority, selects a bounded relevant excerpt, redacts it, and attaches source identity, revision or immutable capture digest, selected range, and truncation/context-completeness metadata. It binds the response to the exact claim, source snapshot, and question/configuration version. Code enforces selection, scope, and request size before transport. Jev receives supplied evidence; it does not receive a general filesystem, remote-shell, or retrieval capability. Existing explicit job data restrictions and revocations remain binding.

Jev may classify the supplied evidence as supporting, contradicting, or insufficient to assess the claim. Missing, stale, truncated, or ambiguous context cannot be turned into a claim of fabrication or a definitive contradiction of the complete artifact. An unresolved reference is distinguished from an artifact proven absent at the relevant revision. Evidence text is untrusted data; instructions embedded in it cannot change criteria, grants, or authority.

The result informs technical review only. It cannot establish completion, approve permission, resolve a blocker, or independently stop the job. Negative signals enter the same candidate-review and deduplication path as other concerns; positive signals do not bypass required verification. Jev unavailability or insufficient evidence leaves the normal controller/tech-lead path available. Extra excerpt access does not authorize a shared raw-data archive: recording follows the bounded system-wide diagnostic policy in §14. Keep the historical source snapshots unchanged and record this later decision in the import synthesis.

The four implementation plans must incorporate the mapping in the import README: Plan 1 candidate/review integration, Plan 2 redaction and diagnostic-versus-egress boundaries, Plan 3 measurement foundation and independently gated attention/triage experiments, Plan 4 outcome evaluation and promotion/rollback evidence. The measurement-first rule gates Jev expansion, not deterministic controller repairs. Old research caps, immutable-default assumptions, and GSD handoff instructions do not supersede our agreed advisory-budget, compatibility, and superpowers decisions.

### Evidence response policy agreed in brainstorming

- Supporting evidence is corroboration only; required verification remains mandatory.
- Contradictory evidence prioritizes technical review with the claim and cited evidence attached; independent authorized work continues.
- Insufficient evidence may prompt selection of a more relevant bounded excerpt when available. If uncertainty remains and the claim matters to progress or completion, route it to the lead. Reuse existing diagnoses and require a changed excerpt or evidence basis for another paid assessment.
- Repeated unchanged findings reuse the existing blocker/review episode rather than generating another escalation or investigation.
- Response urgency follows the consequence of the claim. A questionable progress update does not inherently interrupt work. An unresolved material concern about a claim used to justify completion or a consequential next action must be assessed before the controller relies on that claim. Jev raises the concern; controller rules and technical review determine whether the dependent action must wait. Jev itself never halts the job or clears verification, and unrelated work continues.

Qualification must exercise each response, including no interruption for a low-consequence progress claim, resolution before reliance on materially disputed completion evidence, continuation of independent work, and deduplication of repeated unchanged findings.

Additional qualification scenarios: Jev “ok”/abstention/outage cannot starve a blocked job's required technical review; a Jev concern cannot accumulate into an unwithdrawable hold; malicious worker text cannot widen authority; replay cannot access live services; a skill-lane benchmark cannot promote a supervision lane; only authorized cited excerpts within enforced limits reach the evidence lane; registered-secret canaries are masked before transport and recording; stale source responses are discarded; incomplete context produces insufficient evidence; injected instructions cannot alter authority; positive and negative evidence labels cannot directly complete or halt a job.


## 14. System-wide run feedback and investigation

The owner may flag a problematic run with a description of what went wrong. This opens a read-only investigation across the entire supervision chain: controller, Jev, resolver, tech lead, independent reviewer, and worker. Jev is one potential contributor, not the presumed cause or the exclusive subject of tracking.

Lightweight decision tracking is on by default. Link run/attempt, blocker episode, actor, authority revision, evidence references, model/question/config versions where applicable, decision, action receipt, and observed outcome. Record whether a signal was used, ignored, or overruled. Preserve the difference between a proposal, an attempted action, and a completed effect. Unknown causal links remain unknown.

Use bounded retention of sanitized replay inputs, including Jev observation/excerpt snapshots and response vectors/status/timing, with source revisions/digests. This later owner-agreed tracking decision supersedes the earlier off-by-default recording proposal for these bounded diagnostic records; it does not authorize retaining arbitrary raw transcripts, credentials, or a shared cross-lane prompt archive. The implementation plan must specify a consistent numerical retention/size policy across the recorder, status, and replay surfaces before activation. Flagging a run preserves its available sanitized evidence until the investigation closes. Do not imply that expired or never-recorded input can be reconstructed; report missing evidence explicitly. After closure, the ordinary bounded retention policy resumes for the preserved evidence.

The investigation reconstructs inputs, judgments, routing, actions, and outcomes, identifies supported contributing causes, and proposes a concrete remedy. Distinguish bad input/evidence, a mistaken model judgment, a bad controller/routing rule, and a downstream execution error. Multiple contributors are possible. A component being present is not proof of causation. Replaying a recorded decision with a signal removed or replaced can establish a change in that decision under the replay assumptions, not what the entire live job would have done.

Collect evaluation labels automatically from code-verifiable outcomes and independently assessed semantic judgments, preserving their provenance and uncertainty. An independent model assessment is not human ground truth. Preserve the owner's actual corrections without labeling operator-agent actions as the owner's. Agreement between reviewers, a lack of complaints, or an unexamined “review executed” event cannot establish correctness. Keep unresolved examples unresolved.

A flag alone neither stops the run nor changes policy. If the investigation finds a credible recoverable issue in an active job, submit it through the existing tech-lead recovery path and revalidate current authority/evidence before any action. Duplicate flags for the same incident may add evidence without launching duplicate investigations; investigate again when materially new information warrants it. Existing advisory spending and explicit hard-limit behavior applies to investigation work too.

Plan ownership: Plan 1 supplies common run/decision correlation and flag records; Plan 2 enforces data scope/redaction across capture paths; Plan 3 supplies the read-only investigation, independent assessment, and remedy handoff; Plan 4 verifies end-to-end attribution, replay limitations, retention behavior, and owner-visible outcomes. This extends the existing Store/notebook/flow mechanisms rather than adding a parallel supervisory system.

Qualification: a flag preserves available evidence across restart; a Jev mistake can be distinguished from bad input or downstream misuse; non-Jev incidents are equally investigable; missing evidence is disclosed; repeated flags do not duplicate paid work; replay stays isolated; a flag alone cannot stop a job or alter policy; a supported remedy reaches only the existing authorized recovery path; model judgments and owner corrections retain distinct provenance.


## 15. Learning across jobs

Automatically retain evidence-backed findings from run investigations and surface relevant prior cases to the tech lead. Each lesson links the original incident, supporting evidence, outcome, applicable conditions, and any later correction. Retrieval offers advisory guidance, not an instruction that outranks the current job's goal, grants, or owner decisions.

Before applying a prior remedy, the lead checks that its assumptions fit the current job, revision, environment, and authority. A successful remedy is not a universally correct rule. Negative outcomes and contradictory evidence can demote or supersede a lesson while preserving history. Unresolved diagnoses remain uncertain rather than becoming established facts through repeated retrieval.

Recurring defects yield concrete proposed code/configuration fixes and regression cases. A finding alone never silently changes authority, acceptance policy, escalation thresholds, or model promotion status. Durable behavioral changes follow the existing tested, reviewable change workflow and its applicable authorization; do not create a new human approval requirement for work already authorized through that workflow.

Extend the existing notebook/playbook rather than introduce a competing memory system. Plan 3 owns investigation-to-lesson capture and guarded advisory reuse; Plan 4 verifies that relevant lessons are retrieved, mismatched assumptions prevent blind reuse, corrected lessons lose authority as advice, and proposed policy changes do not silently become active. Keep machine-derived judgments distinct from owner corrections throughout learning.
