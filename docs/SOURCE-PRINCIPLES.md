# Source principles (distilled, authoritative for design)

Distilled from the owner's design conversation (HERDR / GSD / Superpowers / Jev → vetted "final form"
→ LLM/human/code audit). Numbered so plans can cite them as SP-N.

- SP-1 One controller (ordinary code), one active coding worker per task workspace. No standing
  committee of agents; no agent-to-agent conversation — communicate via files, diffs, test output.
  Fleet interpretation (Phase 26+): one controller may supervise several distinct task workspaces
  (one isolated worktree per lane); each workspace still has exactly one active writer. The controller
  alone binds workers to lanes; a worker and its native children never claim or write a sibling lane or
  the parent integration branch (Phase 27: lane leases are epoch-fenced, first-release cap two).
- SP-2 Only one component decides what runs next; only one component owns input to the worker.
- SP-3 HERDR = where the worker lives (hosting, inspection, operator access). Its idle/done states
  and `prompt --wait` are NOT task-completion receipts. No tmux inside Herdr.
- SP-4 GSD = optional planning when decomposition is needed; never run a second autonomous
  scheduler under the controller. One planning authority per job.
- SP-5 Superpowers = selected engineering discipline (systematic debugging, regression tests,
  evidence before completion) chosen per task; mandatory gates live in code, not instructions.
- SP-6 Jev = optional advisory classifier, shadow mode first ("do these observations justify closer
  inspection?"), record-only, promote only after measured discrimination. Never owns permissions,
  acceptance, completion. Unavailable Jev must not stop the baseline.
- SP-7 Minimal configuration: discover mechanics (instructions files, manifests, tests, CI), expose
  assumptions. Discovering a command does not authorize everything it does. Inferred acceptance
  criteria stay visible and are never silently redefined.
- SP-8 Working contract on one screen: GOAL / BOUNDARIES / DONE WHEN / NEXT / AUTHORITY.
- SP-9 Steering is durable: constraints survive worker replacement, compaction, controller restart;
  expire with their scope. Receipts distinguish received → delivered → acknowledged, plus whether
  subsequent actions comply. "Got it" is not a receipt.
- SP-10 Hard restrictions (enforced by execution boundary / code checks) are distinct from
  preferences (need inspection) and review-required properties (e.g. API compatibility). The UI
  labels which is which. Permission changes go through the policy lifecycle, not a prompt sentence.
- SP-11 Stop is independent of model cooperation: block new dispatch immediately, cancel outside
  the model, report what actually stopped and any uncertainty.
- SP-12 Heartbeat observes (process health; activity incl. tests, tool waits, permission prompts;
  task evidence). Quiet ≠ stalled. A suspected stall triggers ONE focused diagnosis, not periodic
  essays. No LLM call on every heartbeat.
- SP-13 Recovery budget: 2 recovery attempts per task, surviving process replacement; then stop with
  evidence and the unresolved decision.
- SP-14 Replacement worker may not write until the previous worker and its children are confirmed
  stopped (single writer). Fleet interpretation: this holds per workspace/lane - a lane's replacement
  waits for confirmed stop of that lane's previous worker and its children; other lanes are unaffected.
  Neither lease expiry, controller restart, pane idleness nor silence counts as confirmation: an
  unconfirmed or ambiguous stop keeps the lane's lease epoch fenced (Phase 27).
- SP-15 Checkpoint at useful boundaries (task boundary, material redirect, genuine recovery):
  hypothesis, commands tried, results, next concrete action. Fresh worker must not repeat work.
- SP-16 Completion references exact code state, task revision, commands, results. Distinguish
  pre-existing failures from new. Changes to tests/verification config are inspected — deleting the
  failing test is not a fix.
- SP-17 Closeout labels, not one "done": WORKER_FINISHED, CHECKS_PASSED, ACCEPTED, RELEASED.
  Ambiguous requirements → "checks passed; acceptance unresolved". Release never automatic.
- SP-18 A code `if` around a model answer is a hybrid decision, not deterministic. Audit log records
  what happened, evidence, authorizing rule, judgment class (human / model / code-only /
  model-informed-code-authorized).
- SP-19 Human in the loop only for intent, additional authority, unresolved acceptance. Ordinary
  technical difficulty is not a human interruption.
- SP-20 Idempotent control: the same control request delivered twice is recognized as a duplicate.
- SP-21 Cap self-reflection: one diagnosis, maybe one replan, then execute or escalate.
- SP-22 Steering surface: /goal /focus /avoid /priority /status /why /nudge /pause /resume /stop
  /explain-state — natural language compiled into policy/state by the system.

## Addendum 2026-09-26 — phone surfaces (Moshi + ChatGPT)

- SP-23 Two surfaces, one authority: Moshi = low-level operations console (status, notifications, approvals, pause/stop, break-glass terminal); ChatGPT = semantic console (what/why, summaries, translating intent into structured steering). Both terminate at the same runner control API.
- SP-24 NORMAL path: Human → Moshi → `steward` CLI → runner → agent. BREAK GLASS: Human → Moshi → agent terminal (Herdr pane). Break-glass is allowed but is not the control path; the runner records it when detected.
- SP-25 Runner emits only high-information events to the phone: RUNNING, NEEDS_YOU, RECOVERY, CHECKS_FAILED, READY, BUDGET — each with a `moshi://herdr?workspace=<ws>&pane=<pane>` deep link. No polling dashboards.
- SP-26 ChatGPT gets a tiny purpose-built MCP surface. Read: list_jobs, get_job, get_current_step, get_recent_events, get_verification, get_diff_summary, explain_blocker. Write: steer_job(job_id, expected_revision, instruction, human_instruction), pause_job, resume_job, cancel_job, approve_action. Never: shell, file write, send-keys, arbitrary tool.
- SP-27 `expected_revision` = optimistic concurrency for human steering: stale revision → CONFLICT with current_revision; caller must re-read.
- SP-28 LLM-interpreted steering stores both `human_instruction` (verbatim) and `interpreted_instruction` (delivered), with `origin` (cli|moshi|chatgpt). CLI steering has no interpretation layer.
- SP-29 The MCP server authenticates and authorizes every request itself (scopes runner:read / steer / pause / cancel / approve / admin); tool annotations declare readOnly/destructive but never replace server-side checks. MCP never touches worker credentials.
- SP-30 ChatGPT Pro today: read MCP is the dependable path; write actions are gated by plan (Business/Enterprise beta) — ship write tools behind scopes, default read-only. Don't make ChatGPT-mobile custom MCP a hard dependency. Public exposure (tunnel/deploy) requires the owner's explicit approval.

## Addendum 2026-09-26 (the owner decisions) — Herdr as the message fabric

- SP-31 ALL message passing between Steward parts (operator ↔ Steward, runner ↔ worker) travels through Herdr
  (pane send-text / events.subscribe / report-agent / report-metadata), as framed structured lines — never
  scraped TUI text. Every message is visible in a Herdr pane.
- SP-32 Steward's database is MEMORY (the ledger), not messaging; direct reads/writes by Steward components are allowed.
- SP-33 Operator commands go through Herdr too: the operator writes framed commands into a Steward control pane;
  the Steward process there executes them and replies in the pane.
- SP-34 Any non-Herdr path is an EXCEPTION: listed in a versioned registry with name, direction, purpose and audit
  rule; each use is recorded as an event; anything unregistered is a violation (blocked where Steward controls it).
  Initial candidates: git (worktree artifacts), model APIs (worker's own LLM calls), chatgpt-mcp, phone-notify.
- SP-35 Workers run headless (structured Claude/Codex) inside a Herdr pane behind a thin shim; done/ack/permission are
  typed events, not printed markers.
- SP-36 (the owner, 2026-09-28) **Defaults, not blockers.** Steward must work in almost any circumstance: it never
  refuses or stalls on a recoverable condition — it picks the sensible default, records what it did, and carries on.
  The ONLY things that wait for the owner are outbound or irreversible acts: deploy, publish, push, release, sending
  messages/email/posts, paying, signing up, logging in, touching credentials, writing outside the job worktree,
  sudo/system services. Everything else is allowed by default for every job — including read-only network (fetching
  pages, APIs, docs, packages), running scripts, and long-running commands. Supersedes the v1 blanket
  "no deploy/publish/network" boundary and narrows SP-7/SP-19's human-approval surface to the outbound list above.
