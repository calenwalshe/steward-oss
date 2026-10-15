# REA / Steward fit

Date: 2026-10-11

Status: Approved by the owner on 2026-10-11; implementation remains unauthorized

Brief: [REA / Steward fit](../briefs/2026-10-11-rea-steward-fit.md)

Checkout: `fleet-registry`, `6d00847bca5b4793d48c39d9781311652d954d29`

## 1. Verdict

**REA fits as an optional investigation method for a Steward worker. It does not currently warrant a Steward module, model provider, or controller-owned analysis service.** Keep REA itself independently installed and operated by the agent that performs the analysis. Reuse its upstream investigation skill rather than maintaining a Steward fork of the analysis instructions.

Steward adds value when an investigation needs durable scope, steering, handoffs, evidence, and a reviewed deliverable. REA supplies observations about software whose behavior cannot be established from available source. These responsibilities fit together, but they are different responsibilities. A quick question about one application should still be answered directly in an REA-equipped agent; it need not become a Steward job.

The smallest useful Steward boundary is **a worker method for turning saved REA evidence into an accountable investigation report**. Start with that boundary, without connecting REA's whole MCP surface to a Steward worker. Direct invocation inside a managed job is a possible later extension, contingent on demonstrated permission and process-lifecycle enforcement. It is not a capability this checkout already provides.

This recommendation distinguishes skill-level fit from present integration readiness. A skill tells the worker how to investigate and report. It cannot grant target access, enforce tool restrictions, or prove that an analysis process stopped.

## 2. What was verified

Both evidence entries in the brief were read: Steward's full [README](../../../README.md) and [REA's repository README](https://github.com/morluto/rea#readme). The investigation also followed the implementation paths below and consulted REA's current primary documentation. Upstream `main` documentation was retrieved on 2026-10-11; it describes current source, which may exceed a published package's capabilities. No installed REA version or live client compatibility was tested.

| Load-bearing claim | Evidence and implication |
| --- | --- |
| A durable job, rather than the model's conversation, is Steward's unit of work. | [store.py](../../../steward/store.py#L26) defines jobs, events, instructions, attempts, checks, and key/value state. The live database has these tables and both append-only event triggers. Investigation results should attach to a job and its committed report. |
| Workers receive methods, but there is no general REA skill-selection facility here. | [brief.py](../../../steward/brief.py#L15) renders a skill list; the dispatch call in [runner.py](../../../steward/runner.py#L1189) passes `None`, selecting the two defaults. Naming a skill in a design does not make it available to a launched worker. |
| A worker adapter is a lifecycle interface, not an analysis engine. | [worker.py](../../../steward/worker.py) requires start, delivery, interruption, stop, observation, and permission answers. [fleet_registry.py](../../../steward/fleet_registry.py#L20) describes model workers and qualified runtime capabilities. REA should be a tool used by a worker, not a roster entry impersonating one. |
| Steward already has one narrowly admitted external research capability. | [research.py](../../../steward/research.py#L124) writes a per-job config containing only the `research` server; [cli.py](../../../steward/cli.py#L6161) launches the Claude shim with project settings and adds that config when enabled. This is an example of explicit admission, not a generic MCP plugin framework. |
| A skill and an MCP connection are separate things. | REA's [upstream skill](https://github.com/morluto/rea/blob/main/.agents/skills/reverse-engineer-anything/SKILL.md) explicitly separates instructions from tools and skips ordinary complete-source repository analysis. A host installation cannot establish that a particular Steward worker sees the tools. |
| Static analysis is a useful narrow starting point. | REA documents JavaScript/Electron and .NET static inspection without a native engine; native analysis uses Hopper, Ghidra, or IDA. Runtime inspection has different host and access needs. See its [target table](https://github.com/morluto/rea#what-you-can-analyze). |
| Tool availability is not target authorization. | REA's [MCP contracts](https://github.com/morluto/rea/blob/main/docs/mcp-contracts.md#request-scope-and-local-effects) leave approval to the client and describe annotations as hints. Steward's present unknown-tool handling does not supply a REA-specific gate. |

### Read-only database observations

The database inspected was `/home/user/.local/state/steward/steward.db`. It was opened directly with SQLite URI `mode=ro`, `PRAGMA query_only=ON`, and a read transaction. `Store()` was deliberately avoided: its constructor creates directories, applies schema, and runs migrations. No Steward CLI job command was executed.

Job, contract, capability, and release counts rechecked in one read transaction at `2026-10-11 17:09:50 UTC`; event judgments and triggers inspected in the initial transaction:

- 289 jobs and 289 stored contracts; all contracts' `authority` arrays contained the exact current read-only-network grant. Jobs were 241 `RELEASED` and 48 `STOPPED`.
- Events contained all four judgment categories: `code`, `human`, `model`, and `model_informed_code_authorized`. The `events_no_update` and `events_no_delete` triggers existed.
- Fourteen explicit capability records were present: twelve enabled research and two did not. This supports the existing per-job capability precedent, not REA availability.
- Release-rule observations included 190 `operator_release` events with human judgment and 18 `released_on_incorporation` events with code judgment. These are event counts, not a reconstruction of every released job's history.

Read-only policy probes called only Steward's pure decision functions; they did not invoke REA or any target. The current permission function allowed synthetic calls named `mcp__rea__analyze_javascript_application` and `mcp__rea__capture_process_scenario`, as well as the shell text `rea setup`, through its default policy. This demonstrates the missing classification boundary, not that these tools are installed or callable.

## 3. Where the README differs from current behavior

Three differences materially affect this design:

1. **Network permission.** The README's quickstart still shows a blanket network prohibition. [contract.py](../../../steward/contract.py#L41) now grants read-only network access and worktree scripts by default. A future REA job needs explicit target and effect boundaries; it cannot rely on that old README example.
2. **Acceptance and release.** The README's unconditional human-only release statement is stronger than current code. [machine.py](../../../steward/machine.py#L65) permits code release under `released_on_incorporation`, and [incorporate.py](../../../steward/incorporate.py#L415) checks merge evidence for that path. There are also direct no-change release transitions. The database confirms code release occurs. The inspected host config selects `acceptance = "auto"`; absent an explicit merge setting, [autopilot.py](../../../steward/autopilot.py#L61) follows acceptance mode. The brief's stricter human-accepts-and-releases requirement remains the requirement for the proposed workflow.
3. **Documentation checks.** [verify.py](../../../steward/verify.py#L100) treats files under `docs/` as documentation and can skip tests for a documentation-only diff. A report and JSON evidence index under that directory do not receive semantic validation merely by passing closeout.

These are findings, not permission to change Steward's global policy. No README, configuration, or lifecycle code is changed by this investigation.

## 4. Alternatives considered

| Boundary | Assessment |
| --- | --- |
| **Worker investigation skill, independently managed REA; saved evidence first** | Recommended. Reuses Steward's durable report job and REA's existing method while adding no analysis authority to the controller. Direct REA use remains convenient for small questions. |
| Steward module/provider without a worker method | Poor fit. It adds installation, tool admission, and recovery responsibilities without teaching the worker how to distinguish observations from conclusions. REA is not a model-worker runtime. |
| Both skill and an optional direct-analysis adapter now | Potentially useful later, but premature. It would need effect-aware permissions, worker-specific tool admission, retained evidence, and confirmed cancellation. Existing generic MCP allowance does not establish those properties. |
| Neither; use REA independently for everything | Suitable for brief investigations. It misses Steward's value when findings must survive interruption, changing instructions, or multiple review rounds. |

The reason to defer an adapter is concrete: the current authority path cannot distinguish a static REA read from a runtime interaction based on an unfamiliar MCP tool name. The reason to keep the skill boundary is equally concrete: a worker can already produce a durable, reviewable report from supplied evidence.

## 5. Smallest viable responsibility boundary

This is a proposed usage design, not an implementation plan or a skill being authored here.

| Owner | Responsibility |
| --- | --- |
| The owner | Chooses the question and authorized target; approves any additional access; reviews the conclusion; accepts and releases the report job. |
| Independent analysis agent and REA | Perform the separately authorized inspection, using upstream instructions and target-appropriate engines. Save portable evidence and disclose incomplete coverage. |
| Steward worker | Read supplied evidence, investigate specific gaps within the job's authority, and write a report with reproducible references. Recommend further inspection when needed. |
| Steward controller | Enforce its existing contract, steering, lease, lifecycle, recovery, and closeout rules. It does not become a decompiler or interpret a tool's success as acceptance. |

The reusable method consists of a question list, target/evidence identity checks, a finding ledger, limits on inference, and a reviewable report convention. Its application to a job must be explicit in the job instructions, with an accessible method reference. Do not invent a `--skill rea` switch or assume global personal skills are loaded. Keep upstream REA analysis guidance upstream; Steward-specific instructions should cover job boundaries, handoff, and deliverables only.

The initial managed job receives saved outputs, not a new REA MCP registration or a target runtime endpoint. Existing worker transcripts and controller events remain the operational trail. The report and its index carry the domain evidence. There is no automatic REA-result ingestion or semantic verification in the current database, and this recommendation does not imply otherwise.

For direct analysis in a future worker, the contract would need to identify exact input paths or endpoints, allowed operations, scratch/output destinations, time/resource bounds, and prohibited effects. Target replacement or a move from static analysis to execution would require renewed authority through Steward. An instruction alone cannot supply enforcement that the runtime lacks.

### What stays outside Steward

REA distribution, setup, engine installation/licensing, provider selection internals, target acquisition, device/browser attachment, and analysis algorithms stay with REA and the operator's tool environment. Do not add REA to Steward's package dependencies, create a daemon for it, expose it through Steward's public connector, or copy the owner's complete MCP configuration into workers.

Steward's optional `mcp` component serves Steward's own control API; it is not required to consume REA evidence. The explicit [component inventory](../../../steward/components.py#L37) has no REA entry. Likewise, the existing `research` server and its estimated spend accounting are not a substitute for REA admission, process supervision, or CPU/memory limits. REA must not be relabeled as `research` to inherit that server's special treatment.

## 6. Concrete end-to-end usage path

Illustrative future case: The owner wants to understand how search crosses the renderer/main-process boundary in an owner-supplied Electron application's `app.asar`, and wants an evidence-backed explanation before deciding whether to build a similar feature. No real target is selected or analyzed in this investigation.

1. **Scope the independent inspection.** The owner supplies the artifact and authorizes static inspection only. The analysis agent records its digest and version, the exact question, allowed output location, and the prohibition on launching or interacting with the application. REA setup, if needed, is a separate operator action.
2. **Produce portable evidence.** The independently equipped agent uses the documented JavaScript application analysis workflow and saves its JSON result. It produces focused supporting excerpts and lists unanswered questions. REA's [CLI guide](https://github.com/morluto/rea/blob/main/docs/cli.md) supports saved-file workflows; it warns that successful operations can still contain partial findings. A zero exit status is therefore not proof that the search behavior is established.
3. **Hand the evidence to a report job.** The owner makes an approved, sanitized evidence copy readable to the Steward worker and retains the complete original privately. The job specifies the question, absolute evidence location and digest, report destination, no implementation, no direct target execution, and no merge/publish. Normal isolated-worktree behavior applies. Uncommitted files in the source checkout are not presumed to appear in the worker's worktree.
4. **Establish the human closeout posture before dispatch.** A future operator must verify effective `acceptance=human` and `[autopilot] merge=false` settings for both the runner and the process that handles acceptance, including environment overrides. This is not the inspected host's present posture. A sentence requesting review is insufficient: current [closeout.py](../../../steward/closeout.py#L392) can waive review constraints in auto mode. If the required posture cannot be established, continue the whole investigation independently rather than claiming this workflow meets the brief.
5. **Write and verify the report.** The one active worker checks evidence identity and available references, separates direct observations from inferred behavior, and commits the report and small index in its job worktree. If static evidence cannot establish runtime ordering, the result says so and recommends a separately authorized probe. It does not silently execute the target. Steward verifies the finished commit under its existing rules; the owner reviews the evidence and unresolved claims.
6. **Accept and release explicitly.** The owner can request revisions, accept the report, and release the job through existing human control surfaces. This path does not invoke incorporation or landing automation. Building an implementation is a separately scoped job, with its own authority and checks.

This path works at the existing report/artifact boundary and does not depend on native engines, a new schema, or new controller logic. It remains conditional on the explicitly verified human closeout posture. It is not a claim that an unattended direct REA integration is ready today.

## 7. Evidence and handoff contract

Keep a small report and machine-readable index under a unique investigation directory in the job's repository. Retain large or sensitive originals in an approved private evidence directory; include stable references and digests. Do not commit an application, credentials, or an entire large output merely to make the report durable.

The proposed index records:

- The question, target version, input digest, and inspected scope; for a directory, a defined file manifest rather than a fictitious single-file hash.
- The analysis tool/package version, engine/profile where relevant, operation and parameters, capture time, and output-file digest/location.
- Each conclusion's evidence references and source locations, whether it is observed or inferred, and remaining uncertainty.
- Coverage limits, failures, and any unavailable or excluded areas; a handoff note identifying the next useful question.

The report is the worker's interpretation. An output hash binds bytes; it does not establish that a claim is true or that a supplied output came from the claimed target. Human review checks that provenance. Current Steward events establish controller actions and commit identity, not the authenticity of arbitrary imported analysis.

REA's [MCP result contract](https://github.com/morluto/rea/blob/main/docs/mcp-contracts.md#tool-results) supplies evidence identities and normalized results. Retained references are connection-local; export needed records before closing or replacing the session. A replacement worker must receive portable saved evidence, not just an evidence ID from a dead connection. Use focused views for model context while preserving complete approved originals. These are evidence-handling requirements, not a new Steward evidence format replacing REA's canonical records.

## 8. Dependencies and optional-component posture

| Dependency | Required when |
| --- | --- |
| Steward's existing runtime and a supported worker | Running the managed report job. No new Steward runtime dependency is proposed. |
| Readable, retained evidence and enough storage | Always for the report path. REA outputs can be large; selective model input and durable original storage have separate purposes. |
| REA package, compatible Node.js and npm | Performing independent REA analysis; not required on a host that only consumes saved evidence. Current [package metadata](https://github.com/morluto/rea/blob/main/package.json#L39) declares Node `^22.19.0 || ^24.11.0 || >=26.0.0`. |
| Hopper, Ghidra, or IDA | Deep native analysis only. They are not prerequisites for static JavaScript or .NET investigation. |
| Browser, device/debugging tools, firmware tools, and host-specific permissions | Only for their corresponding target workflows; outside the initial saved-evidence/report path. |
| REA MCP registration | Optional agent transport. CLI-based evidence production does not require it. Steward's own MCP/public connector is unnecessary for this design. |

REA's [setup documentation](https://github.com/morluto/rea/blob/main/docs/installation.md) describes reviewed configuration changes and version-pinned persistent registrations; installing instructions alone does not establish a live connection. Installation and updates remain independently authorized operator work. Match the skill to the actual package and tool schemas, rather than automatically following repository `main` or installing `latest` during a job.

If REA is absent, Steward should continue to run normally. An evidence-consumption job can still proceed from saved records. If evidence itself is missing, the worker reports that specific gap; it must not silently install tools or substitute guesses. No `steward enable rea` component is proposed.

## 9. Risks, unknowns, and conditions for revisiting

| Material issue | Consequence and design response |
| --- | --- |
| Unknown MCP tools default to allowed. | [permissions.py](../../../steward/permissions.py#L483) and [authority.py](../../../steward/authority.py#L290) do not classify REA effects/targets. Direct MCP admission needs a separately designed code gate with validated input scope; neither a skill nor a read-only annotation supplies it. |
| Existing permission policy is heuristic. | Shell invocation is not a workaround for missing enforcement. The default policy allowed the text `rea setup` in the read-only probe. General worker hardening is outside this design; the first boundary introduces no new REA tool exposure and does not claim sandboxing. |
| Analysis is not a security sandbox. | REA's [security policy](https://github.com/morluto/rea/blob/main/SECURITY.md#security-boundary) says providers operate with the user's permissions. Untrusted artifacts require an appropriate independent analysis environment. No claim of safe hostile-binary parsing is made. |
| Local analysis can still disclose data to a model. | REA's [README FAQ](https://github.com/morluto/rea#faq) distinguishes local analysis from the agent provider's data policy. Approve and sanitize the evidence sent to the worker; exclude credentials, unrelated personal content, and unauthorized proprietary material. |
| Completion checks do not establish research truth. | Documentation verification may skip tests; static observations do not establish runtime behavior. Require explicit human review and retain unresolved findings. Do not let a REA success flag or completion summary close the job. |
| Stop, timeout, and recovery are not equivalent. | REA's [CLI guide](https://github.com/morluto/rea/blob/main/docs/cli.md#choose-a-provider) warns that provider work can outlive a client wait. A direct adapter would need confirmed owned-process cleanup before replacement; Steward's [lease](../../../steward/lease.py#L1) already requires confirmed stopping of previous writers. |
| Retention and compatibility are unqualified. | No live REA package, target format, worker harness, evidence size, or interruption scenario was tested. A saved path is durable only while the operator retains its bytes. This investigation does not establish an automatic evidence store or cross-client support. |

Revisit a thin optional adapter only if repeated real investigations show that importing evidence materially prevents useful steering or wastes work. Its job would be controlled tool admission, bounded process ownership, and durable evidence references. REA would still own engines and analysis. Qualifying one static workflow, refusing unauthorized runtime/target expansion, retaining evidence across restart, and confirming cancellation would be prerequisites to claiming that adapter is usable. Those are acceptance conditions for a possible later design, not an implementation sequence approved here.

## 10. Mapping to “Done means”

| Brief requirement | Delivered by this design |
| --- | --- |
| Plain-language, evidence-based skill/module verdict | Sections 1 and 4: skill-level worker method fits; no controller module/provider now; independent REA for direct analysis. |
| Smallest viable boundary and one concrete end-to-end path | Sections 5–7: saved evidence into a durable report job, with an Electron static-analysis example and explicit handoff/closeout conditions. |
| Authority, optional components, dependencies, risks and unknowns | Sections 3, 5, 8 and 9: current code discrepancies, human closeout requirements, no mandatory REA install, permission gaps, data handling and unqualified runtime behavior. |
| Support from current REA docs and Steward source/docs; no installation/code changes | Section 2 and linked evidence throughout: source inspection, read-only SQLite observations, and pure policy probes. The initial investigation produced this design without REA execution, job actions, application changes, `.planning/` edits, a plan, or a commit. Approval was received before recording the design or drafting a follow-up plan. |

## Approval recorded

The owner approved the written design on 2026-10-11: “i approve it...”. This approves the design boundary: independently managed REA and a Steward worker method for durable evidence reports, starting with saved evidence. Implementation, installation, target analysis, and Steward job actions remain outside this investigation's authority. A follow-up plan requires separate review before execution.
