# Parallel agent fleet for Steward — system design

Date: 2026-10-02  
Status: design for review; no implementation is implied by this document  
Scope: one parent Steward job, one or many coding workers, one verified candidate

## 1. Purpose and agreed outcome

Steward should keep a versioned roster of eligible engineering models and run a job with one or more workers selected from that roster. For a parallel job, each worker owns an explicit task in an isolated worktree. A frontier model serves as the active tech lead: it notices stalls, regressions, dependency and schedule risk, proposes assignments and tradeoffs, and helps unblock the fleet while Steward remains the only controller. Every choice, attempt, artifact and outcome is retained for later analysis of which models worked well on which kinds of work.

The owner sees **one job** with its original goal, finish line and authority; an understandable task graph; the active worker and tech lead roster; live progress and risks; then **one integrated candidate** with exact verification evidence. The owner can steer the job or a lane and remains the authority for accepting and incorporating the candidate. A job with one worker remains the normal, fully supported case.

This design follows the approved direction of bounded autonomous coordination: the tech lead may split work within approved scope, select and replace workers within the job-approved roster, reorder independent tasks, issue bounded steering through Steward, and recommend integration repair. It may not change the goal, required outcome, deadline commitment, grants, permissions, concurrency or spend limits, nor perform external or irreversible actions, acceptance, release or incorporation.

### Non-goals

- Turning the tech lead into a second scheduler or giving workers direct channels to each other.
- Running GSD, Odin, Orchestra, Loom or another harness as a second authority under Steward.
- Parallel writes to one checkout, automatic merging into the owner's branch, or automatic release.
- Claiming a model is supported merely because its CLI is installed or listed in a roster.
- Automatic model performance rankings, promotions or cost claims before comparable outcome data exists.
- General distributed execution, remote worker hosts, or a new messaging fabric in the first release.

## 2. Current constraints and code seams

| Existing mechanism | Evidence | Required change |
|---|---|---|
| One runner worker, attempt and worktree keyed by job | steward/runner.py:173-189, 983-1017 | Introduce job-scoped lane state keyed by lane ID. Keep one job controller. |
| One job worktree and branch | steward/worktree.py:71-105 | Retain the job branch as the integration branch; create separate task branches/worktrees for coding lanes. |
| Job-wide lease stored as a KV list | steward/lease.py:24-92 | Add transactional lane leases with epochs and stop confirmation. The current read/append/write is unsafe for parallel admission. |
| Typed WorkerAdapter and Herdr fabric | steward/worker.py:47-59; steward/fabric_worker.py:1-21, 169-190 | Reuse the lifecycle seam per lane and route typed frames by lane and attempt. |
| Frame identity is job plus sequence; dedupe is job plus sequence | steward/fabric.py:50-57, 119-132 | Add sender, lane and attempt identity to prevent collisions in a shared fleet mailbox. |
| Default fabric is Claude; structured Codex adapter is a stub | steward/launch.py:90-103; steward/worker.py:149-165 | Qualify each runtime adapter before marking that roster entry dispatchable. |
| Role defaults and raw worker launch args | steward/roles.py:25-57; steward/cli.py:1453-1491 | Add a versioned, runtime-qualified roster and immutable per-job assignment snapshots. |
| Cheap heartbeat plus bounded recovery | steward/heartbeat.py:1-8, 96-136; steward/recovery.py:1-10 | Apply per lane and add a job-wide recovery and spend ceiling. |
| Advisor trajectory loop with stale-advice and acknowledgement controls | steward/trajectory.py:1-28, 372-398, 450-524 | Extend into one tech lead action arbiter; do not create a competing independent steering loop. |
| Append-only events, exact-SHA checks, closeout and human release | steward/store.py:30-103, 355-388, 493-524; steward/closeout.py:55-114, 264-290 | Add task/lane/integration provenance and verify the final combined SHA once for the parent. |
| Accepted job branch merges into owner's checkout | steward/incorporate.py:1-9, 205-255 | Preserve this outward merge boundary; lane integration occurs only inside the job branch. |
| Deadline plan and ETA follow one worker's linear progress | steward/deadline_plan.py:1-13; steward/deadline_status.py:110-190; steward/eta.py:1-16 | Derive fleet milestones and forecast from the task DAG, lane evidence, integration and verification tail. |

Steward's source principles currently say one controller and one active worker per task workspace, no agent-to-agent conversation, one component owns dispatch and worker input, confirmed stop before replacement, and exact-code-state closeout (docs/SOURCE-PRINCIPLES.md:6-50). Fleet mode interprets a task workspace as a **lane worktree** and explicitly amends SP-1 and SP-14 to permit several distinct task workspaces under one parent job. SP-2, SP-4, SP-9, SP-11, SP-16 through SP-21, and the Herdr message-fabric rules remain in force. This principle revision must be included in the implementation plan, not left as an implicit exception.

## 3. User workflow and authority

1. **Shape.** The owner gives Steward a goal, finish line, boundaries, optional deadline, approved worker roster or selection policy, maximum concurrent lanes and spend limit. Defaults are one lane for an ordinary job and two lanes when an explicit graph has two independent, owned tasks. A human-pinned model is never silently replaced.
2. **Plan.** GSD remains the planning authority when used. Steward compiles a task graph only from explicit task metadata: stable ID, objective, done criteria, dependencies, owned paths or modules, evidence expectation, and required/optional classification. It must not infer safe parallelism from arbitrary prose in a PLAN.md. A GSD plan that opts into fleet mode supplies a versioned companion JSON manifest named after the plan, for example 20-01-FLEET.json beside 20-01-PLAN.md, with the committed PLAN.md Git blob ID and those required fields. GSD owns that file and .planning/STATE.md. Without explicit metadata, Steward uses one task and one lane. Tech lead decomposition may create runtime child tasks inside the approved work item and finish line; it does not rewrite GSD plans or alter required scope.
3. **Assign.** Steward snapshots the exact registry revision and selected model/runtime for each assignment. The tech lead may propose an initial team from eligible entries. The controller checks capabilities, authority and caps before dispatch. The owner can override an assignment; the override is an append-only revision with its source and reason.
4. **Run.** Ready tasks run concurrently in separate worktrees. Steward shows each lane's owner, model, activity, current task, dependency status, cost and warnings. Global steering fans out to active lanes with individual receipts; a lane steer targets only that lane. Newly created lanes inherit active global constraints.
5. **Integrate.** Each lane reports a committed candidate SHA and evidence. Steward validates ownership and serially integrates ready task commits into one job branch in stable dependency order. Integration conflicts and combined check failures become bounded repair work. No task self-report alone marks the parent complete.
6. **Review.** Steward verifies the final combined SHA against the original contract and required tasks. The parent presents one candidate, one diff and one evidence bundle. Every fleet job enters NEEDS_HUMAN for explicit owner acceptance, regardless of the legacy auto-accept setting. The owner accepts or sends it back and separately decides whether to incorporate it into the source checkout. Existing deadline jobs also retain their exact outbound-approval controls.

## 4. Architecture

The parent **JobController** is the only action arbiter. It owns admission, task readiness, lane allocation, worker input, stop/recovery, integration and parent state transitions. A **TaskGraph** validates plan revisions and derives readiness. A **RosterResolver** loads and validates the model registry. Each **LaneRuntime** wraps the existing WorkerAdapter, heartbeat and permission/steering paths for one task worktree. An **IntegrationWriter** is the sole writer to the job integration branch. A **TechLeadSupervisor** reads snapshots and emits bounded proposals; it never holds WorkerAdapter references. Existing Jev, doctor, trajectory, efficiency and deadline components supply observations to one arbiter.

Herdr continues to host the parent job's control/runner context and the lane worker panes. Store remains durable memory and policy authority; Herdr frames remain component-to-component messaging (SP-31 and SP-32, docs/SOURCE-PRINCIPLES.md:68-77). A worker's native subagents, if allowed by its runtime, belong to that lane's process tree and spend. They cannot create Steward lanes, claim sibling tasks, or write sibling worktrees. GSD is the source of formal phase plans; the tech lead coordinates execution of their approved work units through Steward's typed proposal API.

### Invariants

- One parent controller makes every dispatch, delivery, stop, assignment and integration decision.
- At most one live coding attempt writes a lane worktree; at most one integration attempt writes the job branch.
- A task is ready only after all required dependencies have integrated and its declared ownership is conflict-free with active tasks.
- A lane candidate is immutable by SHA. Integration records exact input and output SHAs; parent verification runs on the final combined SHA.
- No worker or tech lead model obtains authority from its role label. Every applied model proposal carries a code-enforced rule and judgment class.
- Pausing or stopping the parent fences new dispatch before signalling all live lanes. An unconfirmed stop never grants a replacement or integration writer.

## 5. Registry, plans and durable data

### 5.1 Model registry

The operator-owned registry is a versioned section of Steward configuration, with a canonical, validated snapshot in Store. Preserve the existing flat models.toml role defaults during migration; new roster records use a separate namespace and schema version. A record has a stable roster ID, provider, runtime adapter, native model ID, eligible roles (worker or tech lead), qualified capabilities, descriptive task strengths, enabled status, optional dated price/quota observations, and a human label. Secrets and credentials never enter the registry snapshot. Capability flags describe tested adapter behavior, while strengths are advisory and cannot authorize an action.

At job creation Steward records registry revision/hash, eligible entries, chosen tech lead, assignment policy, human pins, default limits and the resolver's reason. Each lane attempt records the actual provider/runtime/model and adapter version. A config edit affects future assignments only; it cannot rewrite an earlier job snapshot. A disabled or unavailable model does not erase historical assignments. Where USD is unavailable, show cost as unknown and enforce the available wall-time/token budget; never present an unmeasured dollar cap as satisfied. An autonomously selected model must be eligible under the job snapshot, runtime qualification and current availability check.

### 5.2 Parent, task and lane records

Use new normalized SQLite tables with ordered migrations; Store's append-only events remain the audit stream. Mutable current-state rows are projections with compare-and-swap versions. Historical task/assignment/plan/outcome rows are insert-only. The minimum logical schema is:

| Entity | Stable key and required fields |
|---|---|
| Fleet job | job_id; mode; pinned base SHA; contract revision; current graph revision; registry hash; roster allowlist/pins; max lanes; cost and time caps; tech lead mode; final integration SHA |
| Task revision | job_id, task_id, graph_rev; parent task; objective; done criteria; required/optional; dependency IDs; owned path/module claims; evidence contract; priority; status |
| Lane | job_id, lane_id; task_id; worktree path; branch; current assignment revision; current attempt; lease epoch; observed process identity; state |
| Assignment revision | job_id, lane_id, assignment_rev; registry hash; roster ID; runtime/model; actor; reason; policy rule; created_at |
| Attempt | job_id, lane_id, attempt_no; model/runtime actually started; Herdr pane/session; started/stopped timestamps; confirmed-stop evidence; termination; usage with source and confidence |
| Integration batch | job_id, batch_id; graph_rev; ordered task/input SHAs; integration base/output SHA; ownership/preflight result; conflicts; verification refs; writer lease epoch |
| Proposal and action receipt | proposal ID; trigger ID; observed graph/contract/steer revisions; exact proposed actions; validation result; applied action IDs; actor/rule/judgment |
| Outcome/feedback | job_id, task/lane/attempt references; exact SHA; observed checks, elapsed time, spend, rework, recovery, human verdict and optional human notes |

Each event includes job ID and, where relevant, task ID, lane ID, attempt number, graph and assignment revisions, registry hash, proposal/trigger IDs, before/after SHA, actor/origin, rule, judgment class, and bounded evidence references. The event log records transitions atomically with their state writes. Redact secrets before persistence as Store does for events (steward/store.py:355-383). Do not use the thinned trajectory ledger as the only retrospective source: it deliberately compresses older facts (steward/ledger.py:73-110).

### 5.3 Task graph and ownership

Graph revisions are immutable. A fleet manifest has schema_version 1, plan_blob_id, and tasks with id, objective, done_when, depends_on, owns, evidence, required and priority. All owned paths are repository-relative; empty owns means the task cannot run concurrently with another writer. Validation rejects unknown dependencies, cycles, duplicate IDs, required items omitted from a revision, and a task whose done criteria cannot be connected to the parent finish line. A GSD companion manifest is accepted only when plan_blob_id matches the committed source plan; an absent or stale manifest yields one lane and a visible reason. A tech lead split retains the original parent task as a non-dispatchable container and creates children whose combined done criteria cover its required outcome; the container is complete only when every required child is integrated. Dropping or reclassifying required work is refused. A graph revision applies only at a safe boundary: unaffected running lanes keep their task revision, while changed tasks are paused/replaced only after confirmed stop. Pending tasks are replanned under the new revision.

Owned paths/modules are explicit, repository-relative, normalized and checked against each active lane's declared claims. Overlapping claims, global config/migration files, generated files and cross-cutting integration points are serialized or assigned to a dedicated integration task. A worker may need to edit outside its initial claims; Steward records the diff as an ownership exception proposal, then updates claims or routes the change to integration before considering the task candidate. It never silently merges an out-of-claim file. Two active lanes with overlapping write claims are forbidden. This rule controls concurrent scheduling; exact Git diffs, not claimed paths alone, control integration.

## 6. Scheduling, lane lifecycle and recovery

The scheduler is deterministic code. On every relevant event it recomputes ready tasks from the committed graph, integrated dependency set, active ownership claims, available qualified models, lane cap and budget. It orders ready tasks by graph priority, then stable task ID; a tech lead reorder is a recorded graph revision within approved scope. A task becomes assigned, claimed, running, candidate, integrated or blocked/failed; the parent remains RUNNING while any required task or integration work is open. The parent reaches WORKER_FINISHED only when all required tasks are integrated, all lane processes are confirmed stopped, and no required steering receipt is outstanding.

Admission and lease claim use one BEGIN IMMEDIATE transaction with a unique active lane lease and monotonically increasing epoch. The controller reserves a launching attempt **before** spawning the process, then binds its pane/process identity to that attempt; restart reconciles a reserved-but-unbound launch before considering any retry. The token is carried on dispatch, frames and completion receipts. An expired timestamp alone does not prove the prior worker stopped: the controller also verifies Herdr/process identity and descendants. A stale worker frame or old lease epoch cannot update the current attempt or promote a candidate. A new attempt starts only after the prior process tree is confirmed stopped and the old lease is fenced; otherwise the lane stays blocked and the parent may continue only independent tasks.

Each lane has its own maximum of two recovery actions, matching current SP-13, plus a job-wide recovery and supervision spend ceiling so parallelism cannot multiply retries without bound. A dead lane blocks its descendants, while unrelated ready tasks may continue. One focused doctor diagnosis per stall episode feeds the tech lead; liveness does not require a model call on every tick. An integration failure uses a separate bounded repair budget. The tech lead can propose reassignment to another approved roster entry after stop confirmation; a human-pinned model stays pinned until the human revises it.

Pause fences new dispatch immediately and applies to every live lane. Resume revalidates graph, registry eligibility, budget and leases before dispatch. Stop fences all lanes, sends typed stop through each adapter, cancels outside the model, and records per-lane confirmed or uncertain stop; the parent is not marked stopped while active children remain uncertain. Controller restart reconstructs task state and leases from Store, probes live Herdr/process identities, reattaches valid lanes, and refuses duplicate launch when identity is unresolved. Every control request is idempotent by request ID, and every lane action by proposal/action ID and lease epoch.

## 7. Heterogeneous adapters and Herdr transport

WorkerAdapter remains the common lifecycle surface, but a runtime is marked fleet-qualified only after it can demonstrate: typed start/ready, exact model and session identity, steer delivery and acknowledgement, permission relay with the same authority policy, typed done/error, stop/interrupt with process-tree confirmation, restart/reattach behavior, transcript/usage provenance, and absence of silent authority escalation. The first parallel gate uses two Claude fabric workers. Codex and later providers enter the roster as visible but unavailable until their structured adapters pass qualification; the legacy Herdr TUI path may remain usable for a single lane without being represented as fleet-qualified. Model-name normalization happens inside an adapter, while the snapshot keeps both the requested roster ID and actual native model ID.

Herdr frame protocol v2 identifies job, task, lane, attempt, sender endpoint, lease epoch and per-sender sequence. Deduplication uses that full identity; stale attempt frames are acknowledged for transport if appropriate but ignored for state. V1 job-only frames remain accepted for legacy single-worker jobs. A job may use separate lane mailboxes or one multiplexed controller mailbox, but both must preserve the same identity and idempotency rules. Herdr pane idle/done state is still an observation, never a task completion receipt (SP-3). Worker-to-worker traffic is not exposed; any dependency handoff is a committed artifact plus bounded Store context, delivered by the controller.

## 8. Active tech lead and supervisor loops

The tech lead runs on material events: graph admission, lane candidate, dependency unblock, failed check, integration conflict, repeated stall or recovery, material spend/deadline risk, and a bounded cadence for long quiet jobs. Its packet contains the original goal/finish line, graph revision, task and lane states, exact SHAs, recent evidence, budget and deadline status, unresolved human decisions, pending steers and previous actions. Packets are bounded; raw transcripts stay linked artifacts. Only one tech lead review is in flight per job and trigger cohort.

The tech lead response is a typed proposal with proposal_id, trigger_id, observed contract/graph/steer revisions, evidence refs, rationale, expected time/cost effect with uncertainty, and an ordered action list. Allowed action types are: split an approved runtime task without changing its outcome; assign/reassign an unpinned task to an eligible roster entry; dispatch or reorder ready independent tasks within caps; add task- or job-scoped focus/nudge/priority steering; request one bounded diagnosis or integration repair; and surface an owner decision with options. Each action has task/lane target and idempotency key. The controller validates the proposal against one state snapshot and applies it atomically or rejects it with action-specific reasons; the tech lead may resubmit a smaller proposal. A stale revision yields no model-origin write; the tech lead must reread before proposing again. Human steering wins, and unacknowledged steering prevents another redirect to that lane.

The **inner loop** observes typed worker events, liveness, permissions, checks and process termination at low cost. It handles immediate safety/lifecycle actions and emits facts. Jev may classify ambiguous signals but cannot authorize completion or permissions. The **outer loop** (existing trajectory advisor extended as tech lead) checks whether the team is solving the right work, allocation and schedule problem. It consumes inner-loop facts, triggers on evidence changes, and emits at most one bounded direction change per settle window. Sol/doctor remains focused on an individual stall. Efficiency remains cost/process advice and cannot independently swap models. One controller action arbiter combines their inputs by authority and priority, so no two supervisors can issue conflicting steers. Repeated identical verdicts, pending acknowledgements, cooldowns and a one-diagnosis/one-replan cap prevent oscillation (steward/trajectory.py:14-28, 474-524; docs/SOURCE-PRINCIPLES.md:30-50).

Tech lead proposals cannot alter the owner's goal, required scope, deadline, grants, permissions, model pins or limits; cannot write outside authorized worktrees; cannot send external notices; cannot accept, release, incorporate, push or deploy. Where a proposal informed a code-authorized action, the event judgment is model_informed_code_authorized, with its validating rule and evidence, not code-only (SP-18). A request beyond authority becomes a local owner decision card. Each outbound item requires its own exact recipient, channel, content and attempt approval under the existing hard stop.

## 9. Integration, verification and one candidate

The job integration worktree is based on the pinned parent base SHA and owns branch steward/<job_id>, preserving the existing incorporation contract. Independent coding lanes may start from the same pinned base; a dependent lane starts from an integration SHA that already contains every dependency. Coding lanes never write the integration worktree. The IntegrationWriter holds an exclusive lease and consumes only committed, exact lane candidate SHAs whose task revisions and ownership checks are current. It processes dependency-ready candidates in stable topological order then task ID. For each merge/cherry-pick it records before SHA, input SHA, result SHA, changed paths and any conflict, making retries idempotent. It refuses dirty or stale integration state and never stashes, resets or overwrites owner's source checkout.

If clean integration fails, Steward preserves all lane branches and opens one bounded repair task in the integration worktree after the integration writer is fenced. The tech lead supplies diagnosis and a proposed resolution; the repair worker has exclusive write ownership, and the resulting commit is checked against all affected task criteria. A conflict cannot be waived by a model opinion. Once all required tasks are integrated, Steward runs the existing baseline-relative checks, weakening scan and deadline evidence gate at the **combined** SHA. Lane-local tests are useful evidence but cannot substitute for combined verification. New failures, missing required evidence, stale task revisions, dirty integration state or live writers block a candidate and route a bounded repair/rework cycle. The parent keeps one WORKER_FINISHED → CHECKS_PASSED → NEEDS_HUMAN → ACCEPTED sequence; fleet mode disables legacy pre-authorized auto acceptance and automatic incorporation. Release and incorporation retain current human controls. The source checkout sees only the final job branch after explicit incorporation.

## 10. Deadline and outbound interaction

A fleet job still has one human-owned deadline commitment and one review-ready candidate. The former design's “one task” means one user-facing job and deadline, not one internal worker lane. Human-only commitment revisions and optional-scope rules in steward/deadline.py remain authoritative. Runtime task splits cannot extend the due time or drop a required commitment item. The deadline plan may be compiled from the explicit GSD task graph or an adopted committed worker plan, but its milestone evidence comes from integrated SHAs and exact task/check facts. A lane's unintegrated commit is progress evidence, not proof that the parent milestone or candidate is met.

The fleet forecast is a critical-path range over remaining DAG tasks, observed lane durations and queue/capacity limits, plus expected serial integration and final verification tail. It accounts for active parallelism without summing all tasks as if sequential, and never assumes perfect speedup. Until enough comparable evidence exists, show a range and “estimating”; unknown Codex usage or latency stays unknown. Existing On track / At risk / Unlikely evidence caps and hysteresis remain; worsening can be immediate, improvement needs positive evidence. A deadline check-in is tied to the original milestone slot even if the graph is revised. Existing local outbox, one-item exact approval and consumed-attempt delivery rules remain unchanged (steward/deadline_outbox.py:1-14, 248-299, 382-443). No tech lead action may send a notification or approve one.

## 11. Read surfaces, retrospective and security

CLI, dashboard and MCP read views show: roster with qualification and observed availability; parent graph with blocked/dependency status; lane assignment/model/pane/attempt/lease and recent activity; exact integration queue and candidate SHA; spend and time by lane, tech lead and other supervisors; deadline risk; and outstanding owner decisions. Writes use the same controller API and request IDs regardless of surface. Read projections may lag an event briefly but must expose source event sequence/revision.

The retrospective query groups comparable tasks by explicit task category and criteria, runtime/model version, repo context, attempt and outcome. Show measured elapsed time, tokens/USD where known, integration conflicts, check failures, rework/recovery, accepted/rejected outcome and human notes, with sample count and missing-data rate. Worker totals include its native subagents once; tech lead, doctor, observer and integration spend are separately attributed and summed once at the parent. A model's proposed rationale is labelled opinion. A worker's “done” is labelled claim. Neither is silently turned into success. There is no automatic leaderboard or assignment feedback policy in this release; a later policy would need its own calibrated evaluation and human promotion.

Registry entries cannot carry shell templates, credential values or arbitrary commands supplied by a model. Adapter invocation is code-owned, permission policy is runtime-qualified, and worktree boundaries are enforced independently of prompt text. Store event masking and bounded evidence apply to all lane and proposal fields. Model calls and git operations stay covered by Steward's registered non-Herdr exceptions (SP-34). Read views must not expose raw credentials or full unredacted transcripts. The tech lead can inspect only granted job artifacts and bounded summaries.

## 12. Migration and failure behavior

Existing jobs remain legacy single-lane jobs with the current branch, launch args, state and CLI semantics. A read projection may show an implicit task/lane labelled legacy and an inferred or unknown model; migration never fabricates registry provenance or historical cost. New fleet tables are additive, and a feature flag defaults new jobs to legacy behavior until each gate is qualified. The single-lane path uses the same outward API and final candidate contract. Old v1 Herdr frames continue to route only to legacy jobs. A registry file error or unavailable new adapter falls back to a supported approved entry when possible and records the choice; it does not silently bypass a human pin or a hard budget.

Failure handling follows these rules:

| Failure | Controller response |
|---|---|
| Lane process disappears | Fence lane, confirm process/children stopped, preserve branch, apply bounded recovery or mark blocked; independent lanes continue. |
| Herdr transport unavailable | Stop new dispatch, retain durable state, retry boundedly; do not infer done or stop from silence. |
| Duplicate/stale frame or control request | Return prior receipt or ignore stale attempt after logging; never duplicate a worker or integration commit. |
| Tech lead timeout, malformed or conflicting proposal | Record skipped/rejected advice; deterministic scheduler and inner loop continue under existing contract. |
| Ownership overlap or out-of-claim diff | Serialize/hold affected tasks or require explicit claim revision; do not integrate silently. |
| Integration conflict or combined-check failure | Preserve input SHAs; assign bounded integration repair/rework, then reverify combined SHA. |
| Budget exhausted or unmeasurable hard spend limit | Stop new autonomous dispatch, preserve running work safely, surface exact unmet decision; never claim cap compliance without data. |
| Controller restart | Rehydrate from Store and Herdr/process identity; reattach or fence; no duplicate live writer. |
| Deadline passes | Preserve commitment and required work; show overdue/risk facts and local warning draft under existing approval rules. |

## 13. Delivery gates and acceptance proofs

### Gate 1 — registry and task records

Add roster validation, runtime qualification state, immutable per-job snapshots, explicit task DAG/ownership schema and additive migrations. Ship read-only roster/graph views and legacy implicit-lane projection. Prove malformed or prose-only GSD plans produce one lane; cycles and overlaps cannot be marked ready; a registry edit does not rewrite a job snapshot; no existing job changes behavior. The GSD plan for this gate specifies exact metadata shape; it does not edit .planning/STATE.md through this spec.

### Gate 2 — safe lane runtime

Extract lane-aware runner state and transactional leases, create isolated worktrees, route lane-aware Herdr frames, and fan out pause/stop/steering with per-lane receipts. Qualify two simultaneous Claude fabric lanes before raising default parallelism beyond two. Prove forced controller crash/restart does not duplicate a live lane; expired lease with a live process cannot be stolen; same job/sequence from different lanes cannot collide; stop fences all lanes; dependency and ownership admission are deterministic; late frames from an old attempt cannot act.

### Gate 3 — integration and closeout

Add exclusive integration writer, deterministic candidate queue, conflict repair, exact input/output SHA ledger and combined verification. Keep the source checkout untouched until normal human incorporation. Prove two independent lane commits produce one integrated SHA; dependency order is respected; an ownership exception, dirty integration tree or failing combined check blocks candidate; a recovered integration attempt cannot duplicate a commit; one human acceptance covers the final branch only.

### Gate 4 — active tech lead, heterogeneous runtime and learning

Extend trajectory into the bounded tech lead proposal loop and action arbiter; qualify a structured Codex adapter and later runtimes separately; add per-attempt outcome/cost records, read-only comparisons and fleet-aware deadline critical-path view. Prove a mixed Claude/Codex job with a stall and one reassignment produces one candidate; stale tech lead advice after human steering has no effect; permission/authority parity holds across adapters; unknown cost is reported honestly; deadline risk uses integrated progress and no outbound item is sent without exact approval. Normal active mode requires a measured false-action review, a kill switch and a passing low-risk trial receipt from the isolated development instance.

### Isolated development and promotion

Implementation and the first active-mode trial use a side-by-side development Steward installation, leaving the current live installation as the stable version. After the isolation preflight passes, a dedicated `trial` authority may enable bounded tech-lead proposals only for the identified dev instance and disposable low-risk trial job. It cannot enable ordinary `active` mode, reach live state or outbound destinations, or authorize promotion. The development installation uses its own `STEWARD_HOME` database/state, `STEWARD_CONFIG_DIR`, `STEWARD_DATA_DIR`, Herdr workspace, service/socket identities and local-only or fake outbound integrations. It must not read or write the live Store, credentials, job worktrees, workspace, or notification destinations. Use a development label plus the exact Git commit/build identity; do not invent a new public release version for each experiment. Record and test the isolation boundary with a dev-only sentinel and explicit resolved-path checks. Keep the current live build available as rollback. Promote the same tested source commit/build only after all GSD phase gates pass and the owner explicitly approves the production change; deployment, activation, external notices, and release remain separate human decisions.

The implementation plan should preserve one atomic commit per focused task and document the specific required gates before code execution. The spec does not authorize production deployment, external notices, acceptance, release or incorporation.

## 14. Research inputs and transfer limits

These are design inputs from the named projects' own documentation, not adopted dependencies:

| Project | Pattern used here | Boundary in Steward |
|---|---|---|
| [SuperHarness](https://github.com/backmeupplz/superharness/blob/main/AGENTS.md) | Independent task fanout, isolated worktrees and completion-triggered dependency release | Steward uses typed events and verified SHA, not prompt-only worker “done” or direct merge to the owner's branch. |
| [Loom (llvm-x86)](https://github.com/llvm-x86/loom/blob/main/README.md) | Per-invocation model choice, parallel isolated subagents and structured outputs | No live peer chat, nested scheduler or direct subagent claim of Steward tasks. |
| [Harness Kit / Odin](https://github.com/deepklarity/harness-kit) | Explicit DAG, proof per task and capability/cost-aware model suggestions | GSD remains formal planning authority; Steward does not embed Odin or taskit. |
| [Orchestra (lunarnexus)](https://github.com/lunarnexus/orchestra) | Role catalog, host/runtime separation, bounded handoffs and mixed model roles | Steward retains Herdr, Store, policy and one controller; no second host plugin or planning loop. |
| [COORD-Harness](https://github.com/0marm0/COORD-Harness) | Transactional claims, lease/version fencing, derived liveness and proof-gated completion | Store is Steward memory and Herdr remains its message fabric; agents do not gain direct board authority. |
| [MCO](https://github.com/mco-org/mco) | Explicit team selection and provider adapter detect/run/poll/cancel boundaries | Raw model output does not itself become a verdict or authorization. |
| [Zenith](https://github.com/Intelligent-Internet/zenith) | Gap-finding against original intent and stopping discipline | Avoid a free-running LLM orchestrator that independently dispatches each turn. |
| [twaldin/harness](https://github.com/twaldin/harness) | Explicit backend capability, honest usage nulls and typed cancellation result | Process-group cleanup alone does not prove every child stopped; Steward keeps its confirmed-stop fence. |

Any cost, token or latency figures in these projects are their authors' reported measurements on their own workloads, not general guarantees for Steward. The initial max concurrency of two is a safety and observability default, not a predicted speedup.

## 15. Decisions fixed by this spec and review record

The selected defaults are: one lane without explicit graph metadata; two maximum concurrent coding lanes in the first fleet release; explicit path/module claims with overlap serialized; stable topological/task-ID integration order; one parent integration branch; one live writer per lane and integration worktree; human pins respected; bounded tech lead autonomy within job-approved roster/scope/caps; one in-flight outer review per job; per-lane recovery limit two plus job-wide ceiling; Claude parallel qualification before Codex; read-only retrospective analysis. Future changes to these defaults require a versioned policy or contract revision and an event explaining the change.

Self-review checklist for this document: intent and non-goals are explicit; every state-changing model action has a controller rule; graph metadata is required for parallelism; worker/lane/integration identities and exact SHAs are durable; stop/restart and duplicate-message cases are specified; combined verification precedes the single candidate; GSD, deadline, outbound and human merge authority are preserved; external project claims are attributed and bounded.
