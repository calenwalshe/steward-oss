# Phase 16 — Efficiency watcher — Context

**Gathered:** 2026-09-29
**Status:** Discussed, no plans. Discuss mode: no human available. **Every gray area below is a recommended default,
not a decision.** Each is marked **OPEN FOR SAM**; planning may proceed on the defaults, and Sam's answer
overrides any of them.

<domain>
## Phase boundary

A built-in watcher that watches **any** job for cost and wall-clock waste, says what the waste is in numbers, and
gets it fixed at its cause. It is generic: it reads what every Claude job produces (the worker transcript, its
`subagents/*.jsonl` + `*.meta.json`, Steward's own events and the tokens ledger) and it has no GSD-specific code
path. Its first real case is a GSD build, the Android job `06ea6a04`, and its acceptance replay is built from that
job.

In scope: correct accounting (subagents included), deterministic metrics, deterministic waste detectors with a
fixed remedy catalog, a status-pane row, advice through the steering path, a bounded job-config power, its own cost
cap, and a replay gate.

Out of scope: stall/liveness (doctor, Jev), trajectory ("right problem?", Astra), permission holds (resolver),
changing models or quality gates on its own, and non-Claude (codex) worker transcripts (see G14).
</domain>

<evidence>
## Evidence from the live job (06ea6a04, read 2026-09-29 ~23:00, read-only)

Recomputed from `…/234e987a-…/subagents/*.jsonl` with `steward.tokens.price_usd` (23 subagent transcripts):

| Agent (in start order) | Dur | Calls | Mean ctx/call | Output | tok/s | $ |
|---|---|---|---|---|---|---|
| doc classifiers ×5, synthesizer, roadmapper | ~11 min | 31 | 25–101k | 107k | 120–156 | 2.4 |
| planner 1 / exec 1 | 18 / 13 min | 40 / 68 | 190k / 152k | 163k / 86k | 148 / 112 | 3.8 / 3.4 |
| planner 2 / exec 2 | 28 / 17 min | 69 / 75 | 212k / 211k | 232k / 168k | 137 / 163 | 6.8 / 5.6 |
| planner 3 / exec 3 | 33 / 38 min | 39 / 83 | 317k / 331k | 284k / 318k | 146 / 141 | 6.6 / 9.9 |
| planners 4–6 | 26–29 min | 49–62 | 306–319k | 222–258k | 141–148 | 6.8–7.8 |
| exec 4 | 48 min | **131** | **356k** | 342k | 118 | **14.1** |
| verifiers ×4 | 2–5 min | 14–29 | 82–119k | 14–28k | 83–134 | 0.7–1.3 |

- Subagent spend ≈ **$86**; Steward showed a few dollars because `tokens.py` globs only `<projectdir>/*.jsonl`.
- Mean context per call grows phase over phase (150k → 356k): cost per phase rises while the work does not.
- Output rate is flat at ~110–165 tok/s, so a stage's wall time ≈ output tokens / ~140. Thinking text is empty
  in the transcripts (redacted, only a signature). Hidden thinking has to be **estimated**:
  output_tokens − visible (text + tool_use input) chars / 4.
- Planning was serial with execution for phases 1–3 (planner N+1 starts when exec N ends). Overlap began at phase 3/4.
- `meta.json` carries `agentType` and `description` ("Verify phase 3") for every subagent. That's the generic
  stage label.
</evidence>

<decisions>
## Gray areas — recommended defaults (all OPEN FOR SAM)

Format: options → **recommended** → one-line rationale.

### G1 Accounting fix (tokens.py counts subagents) — OPEN FOR SAM
- Options: (a) fix inside this phase as plan 16-01; (b) separate quick fix first; (c) leave as is, and the watcher does its own parse.
- **Recommended: (a)**, and ship it first. `job_tokens` also reads `<session>/subagents/*.jsonl`. Source key
  `sub:<agentType>` (fallback `sub:unknown`). The cache is keyed like the existing one, and message-id dedupe stays.
- Rationale: every existing budget (spend-rate alert, 25% supervision cap, overspend halving) is currently computed
  on ~10% of real spend, so it is wrong today, watcher or not. One parser, not two.

### G2 Which metrics, and how they are computed — OPEN FOR SAM
- Options: (a) cost metrics only; (b) cost + time; (c) cost + time + critical path + trends (the full list).
- **Recommended: (c), all deterministic code over transcripts + events, computed per agent then rolled up per stage:**
  - *Cost:* per-agent calls, mean/peak context per call, **Σ(context × calls)**, $ per agent and per stage,
    cache-miss share (cache_creation ÷ (cache_read + cache_creation)), cost-per-unit trend.
  - *Hidden thinking:* estimated thinking tokens and their share of output, per agent. Shown as "≈", because it
    is an estimate.
  - *Time:* per-stage wall time, generation minutes (output ÷ observed tok/s) vs tool minutes, output tok/s,
    idle wait (no agent generating and no tool running), handoff latency (agent A ends → next agent starts),
    concurrency factor (Σ agent-minutes ÷ wall minutes).
  - *Critical path:* the chain of stages that bounds wall time, attributed by stage type (e.g. "plan 78 min of
    214").
  - *Projection:* $/unit and min/unit × remaining units, only when the unit count is known (G3).
- Rationale: the audits needed all of these to explain both $ and minutes. Every one comes from data already on
  disk, so the only cost is CPU.

### G3 What "stage" and "unit" mean for a generic job — OPEN FOR SAM
- Options: (a) GSD adapter (phases/plans from `.planning/`); (b) generic only; (c) generic core + optional unit hint.
- **Recommended: (c).** Stage = subagent `agentType` (worker turns form their own stage). Unit = a number parsed
  from the subagent `description` (`phase 3`, `plan 2`, `step 4`), else the Nth occurrence of the same agentType.
  An optional `units_total` hint in the job contract enables projections.
- Rationale: stays generic (no `.planning/` reads) and still gets GSD phases for free from descriptions.

### G4 Status pane landing page — OPEN FOR SAM
- Options: (a) one supervisor-tree row + ⚠ lines for findings; (b) a dedicated efficiency block; (c) CLI only.
- **Recommended: (a).**
  - A new `efficiency` row in `supervisor_rows`, e.g.
    `efficiency  $86 · sub 96% · 5.2k calls · peak ctx 356k · think≈70% · 140 tok/s · crit: plan`.
  - One `⚠` line per active finding, reusing `warn_lines` priority, e.g.
    `⚠ executor#4 131 calls × 356k ctx → respawn per plan (≈-$6)`.
  - The full table goes in `steward efficiency JOB [--json]`; the watchers feed gets `efficiency_*` events.
- Rationale: the pane must fit its height (commit 5007cfb drops low-priority lines first). One row plus findings
  keeps the landing page readable, and the detail is one tap away.

### G5 Deterministic metrics vs model judgment, and which model — OPEN FOR SAM
- Options: (a) all deterministic; (b) deterministic detectors + model to phrase/choose remedies; (c) a model reviews
  everything.
- **Recommended: (a) for detection and remedy (detector → fixed remedy catalog, see G7).** An optional model
  review runs at unit boundaries only (**Sonnet, low effort, `claude -p`, read-only, 8k input**). It is **off by
  default** and used for patterns no detector matches.
- Rationale: the waste found so far is arithmetic (context × calls, thinking share, serial stages), so a model adds
  cost and false concerns. The Haiku pace watcher raised a false concern and duplicated Jev. Sonnet over Haiku
  because remedy wording must be exact; Opus is unjustified for this.

### G6 Authority on the trust ladder — OPEN FOR SAM
- Options: (a) observe only; (b) Phase-15 ladder T0→T2 (observe → escalate → nudge); (c) (b) plus a bounded
  `reconfigure` power.
- **Recommended: (c), starting at T0 shadow, promoted only by Sam under the Phase 15 bars:**
  - **T0 observe:** metrics, findings and would-have-nudged records.
  - **T1 escalate:** a NEEDS_YOU with the finding and its $ / minute estimate.
  - **T2 nudge:** one concrete fix through the steering path (G8).
  - **New T2+ `reconfigure`:** it changes **only the job's efficiency policy** (G9), only for **future spawns**,
    only inside an envelope Sam sets, and every change is reversible and logged.
- **Stays with Sam:**
  - model changes (tier up or down);
  - turning off any quality gate (verifier, plan-check, nyquist, tests);
  - effort below the envelope floor;
  - killing a running agent mid-task;
  - budget/grant changes;
  - stopping or pausing the job;
  - promotion.
- Rationale: the main fixes (respawn per plan, effort medium, plan ahead) are config-shaped and reversible, while
  quality and model choices trade correctness for cost, and that trade is Sam's.

### G7 Detectors, thresholds and remedy catalog (defaults) — OPEN FOR SAM
All thresholds live in `[efficiency]` in steward.toml and can be overridden per job. Each finding carries an estimated
saving; findings under $1 and under 2 min are shown but never nudged.

| Detector | Fires when (default) | Remedy (catalog) |
|---|---|---|
| long-lived agent | an agent is past 80 calls **or** mean ctx > 250k with Σctx×calls > 20M tok | end-and-respawn: one fresh agent per plan/unit; pass a file handoff, not history |
| context growth trend | mean ctx/call of unit N > 1.5× the median of earlier units of the same stage | respawn per unit; trim what each spawn reads |
| hidden thinking | est. thinking > 60% of output **and** stage output > 150k | effort one step down (high → medium) for that stage type |
| planning on critical path | a plan-type stage starts only after the previous build stage ends, 2 units in a row | plan N+1 while N builds |
| artifact bloat | a stage writes an artifact > 40 KB that the next stage reads whole | request a smaller output (coarser granularity / shorter template); Sam-gated if it drops a quality section |
| build-output bloat | ≥ 3 tool_results > 20k chars from the same command | quiet flags / `| tail -n 80` / write to a file and grep |
| cache misses | cache-miss share > 30% over an agent with ≥ 20 calls, or idle gaps > cache TTL between calls | keep spawns warm, avoid idle gaps between calls, keep the prompt prefix stable |
| idle wait | nothing generating and no tool running for > 10 min while RUNNING | escalate only (liveness belongs to doctor/Jev) |
| cost-per-unit trend | unit cost > 1.5× the median of prior units, or projection > job budget | escalate with the projection |
| redundant supervision | a watcher whose findings duplicate Jev/stability for 3+ runs, or supervision > 25% of worker spend | escalate: "remove watcher X" (removal is Sam's) |

- Rationale: each row is a finding from the two audits or the 06ea6a04 table. The thresholds sit just below where
  the live job crossed them (exec 4 at 131 calls / 356k), so the replay fires early and a steady job stays quiet.

### G8 How advice reaches the worker, generically — OPEN FOR SAM
- Options: (a) steering nudge; (b) write into the job's config files; (c) a policy block the brief carries.
- **Recommended: (a) + (c).**
  - **(a)** A nudge goes through `Steering` with origin `watcher:efficiency`, never urgent. It is gated like any
    Phase 15 watcher (no pending steer, no settle lease, epoch check) and delivered at a boundary: when a subagent's
    tool_result lands or the worker is idle, never mid-call. The text is one change, the evidence line, the
    expected saving and how the effect will be measured, and it is phrased generically ("from now on, when you
    spawn a helper for each plan…").
  - **(c)** The job's **efficiency policy** (G9) is rendered into the brief for new or relaunched workers and
    restated in the nudge.
  - The watcher never edits the worktree: `.planning/config.json` changes are the worker's to make on advice.
- Rationale: the worker is the only thing that spawns subagents, so it is the only generic lever. Writing a
  project's files would make the watcher GSD-specific and a second writer (SP-1).

### G9 Job efficiency policy (what `reconfigure` may change) — OPEN FOR SAM
- **Recommended:** a per-job kv `efficiency_policy` with `respawn` (off | per_unit | max_calls=N), `effort`
  per stage type (high | medium, never below the floor), `plan_ahead` (on | off) and `output_hygiene` (on | off).
  Envelope defaults: effort floor = medium, max_calls ≥ 40, `plan_ahead` allowed. Each change is an
  `efficiency_policy_changed` event with before and after, and `steward efficiency JOB revert` undoes it.
- Rationale: these are the ranked time and cost wins from the time audit (effort medium −10–12 min/exec phase,
  plan ahead ~60 min, respawn per plan) with low quality risk.
- Unknown to verify at research: whether effort can be set per spawned subagent from the brief (e.g. agent
  frontmatter or `--effort`) or only per worker session. If only per session, `effort` applies to the next worker
  relaunch and is advisory for subagents.

### G10 On by default, or opt-in — OPEN FOR SAM
- Options: (a) everything on; (b) metrics on, actuation opt-in; (c) all opt-in.
- **Recommended: (b).** Metrics, the pane row and T0 findings are **on for every job** (code only, no model
  spend). Nudge and reconfigure turn on only by promotion. The model review is off. `steward supervisor JOB
  efficiency off|on`, `STEWARD_EFFICIENCY=0` forces it off.
- Rationale: the undercount showed that "not measured" is the expensive failure. Measuring costs no tokens, while
  actuating has to earn trust.

### G11 Interaction with Jev, doctor, custom watchers, stability — OPEN FOR SAM
- **Recommended:**
  - A **built-in role** (`efficiency`, added to `RESERVED_NAMES`), not a custom watcher, so it does not count
    against the 4-watcher cap.
  - Priority slot = the watcher tier (below human, safety, advisor, doctor), under the same conflict quarantine.
  - It **absorbs** `stability.apply_spend_shape` (watcher overspend halving, $/h alert): same behaviour, reported
    as efficiency findings, with thresholds re-based on the corrected spend.
  - Jev and doctor keep liveness and stalls. The efficiency watcher's idle-wait finding never nudges and defers
    to them.
  - It reports on other supervisors' cost (Astra, Sol, Jev, custom watchers) but never changes them. Removal or
    cadence beyond the existing halving goes to Sam.
- Rationale: one owner per concern. Spend-shape logic is already an efficiency concern and should not run on a
  second, wrong number.

### G12 Its own cost cap — OPEN FOR SAM
- **Recommended:**
  - Deterministic part: incremental parse (tokens cache, per-file offsets), recompute ≤ once per 60 s and on
    subagent-finished events, target < 1 s CPU per tick.
  - Model part: ≤ 1 review per unit boundary, ≤ $0.50/job/h and ≤ 2% of job spend. It counts as supervision
    source `efficiency` inside the 25% cap. On a trip the watcher goes observe-only and a notice is sent.
- Rationale: an efficiency watcher that costs more than it saves is the pace-watcher failure again.

### G13 Measuring whether advice worked — OPEN FOR SAM
- **Recommended:**
  - Each nudge/reconfigure records a baseline (the metric that fired, for the last comparable unit). The
    watcher then reports the same metric for the next unit as `efficiency_effect {before, after, saved_usd,
    saved_min}`.
  - This is shown to Sam as evidence for adjudication, and labels stay human (Phase 15 A3).
- Rationale: estimates alone would let a wrong remedy look useful. Measured effect is what promotion should rest on.

### G14 Scope of "any job" — OPEN FOR SAM
- **Recommended:** v1 covers Claude-transcript jobs (worker + subagents, any project type, GSD or not). Codex
  workers show `efficiency n/a (codex transcript)`, and a codex adapter is deferred.
- Rationale: all evidence and the first case are Claude jobs. A second transcript format doubles the parser surface
  before the core is proven.

### G15 Acceptance gate — OPEN FOR SAM
- **Recommended:**
  - A **replay** of 06ea6a04 from a trimmed fixture (usage lines, meta.json and timestamps only; no prompts or
    code), with a fake clock. It must show:
    - accounting within 2% of the independent recompute;
    - the long-lived-executor finding before exec 4 passes call 90;
    - planning-on-critical-path by unit 2;
    - the hidden-thinking finding on planners;
    - 0 findings on a synthetic steady job.
  - It must run 3× with identical output, keep the full suite green and trust 8/8, and leave no steward-test-*
    session behind.
- Rationale: it copies the Phase 12 replay pattern, and a fixture keeps the gate deterministic and free of the
  live job's content.

### Claude's discretion (planning may decide)
- Module layout (`steward/efficiency.py` + dash row), event names beyond those named above, CLI output formatting,
  the fixture trimming script.
</decisions>

<canonical_refs>
## Canonical references (read before planning)
- `/home/user/.claude/responses/2026-09-29/android-job-efficiency-audit.md` — the cost audit, including the undercount
- `/home/user/.claude/responses/2026-09-29/android-job-time-audit.md` — the wall-time audit and critical path
- `docs/decisions/2026-09-29-watcher-authority.md` — trust ladder, gates, caps, human-only list, resolver tiers
- `.planning/phases/15-watcher-authority/15-CONTEXT.md` — D1–D9 and A1–A13 as built
- `steward/tokens.py` — accounting (G1), `price_usd`
- `steward/stability.py` — `spend_findings` / `apply_spend_shape` (absorbed, G11)
- `steward/custom_watchers.py`, `steward/watcher_authority.py` — gating, caps, steering origin
- `steward/jev_watch.py`, `steward/supervisors.py` — roles, switches, supervisor rows
- `steward/watchers.py` — the feed formatter for new `efficiency_*` events
- `deploy/cockpit/steward-dash-jobs.py` — landing page (`supervisor_lines`, `warn_lines`, height fit)
- Live example (read-only): `~/.claude/projects/-home-agent--local-state-steward-worktrees-06ea6a04-d03c-49ec-8997-9153efcf3feb/`
</canonical_refs>

<code_context>
## Existing code insights
- `tokens._files_for` globs `d.glob("*.jsonl")`. `subagents/` sits one level down per session, which is the
  undercount.
- `tokens._parse_file` already dedupes streamed messages by id and prices per message. It can be extended to
  return per-message ctx, output and visible chars without a second pass.
- `stability.SPEND_CHECK_EVERY_S = 60` is the natural tick for recompute.
- `custom_watchers.SPEND_RATIO = 0.25` / `MAX_WATCHERS = 4` / `RESERVED_NAMES` apply as-is to the built-in role.
- `supervisors.SWITCH_KEYS` / `ROLE_STATES` gain `efficiency` (states `off|shadow|active`, default shadow).
</code_context>

<deferred>
## Deferred ideas
- Codex transcript adapter (G14).
- Automatic parallel fresh executors for independent wave-1 plans (−25–30% exec time). This needs disjoint-file
  analysis and is a separate phase.
- Overlapping verify N with exec N+1. The risk of building on an unverified base makes it Sam's call, not a
  remedy.
- Cross-job learning (the default policy learned from past jobs).
</deferred>

---
*Phase: 16-efficiency-watcher · context gathered 2026-09-29 · no human in the loop; all G-items OPEN FOR SAM*
