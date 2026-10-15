# PRD — Autonomous supervision: detect, route, fix (Milestone v2.6)

Date: 2026-10-03 · Owner: the owner · Predecessor: Milestone v2.5 Multi-runtime parallel fleet (job 14305aeb, phases 30–32)

## 1. Problem

Job 14305aeb finished all of v2.5 and then sat in NEEDS_HUMAN with a one-line, mechanical defect (the owner's name in
two comments in `steward/fleet_runtime.py`) that broke 16 tests. No rung of the supervision ladder caught or fixed it:

- The finish-line gate (`runner._finish_line_gate`, `stability.FINISH_TIMEOUT_S = 120`) ran the finish line's command,
  `python3 -m pytest -q` (~30 min), timed out four times, nudged the worker with "finish check timed out after 120s"
  (no failing test named) and escalated to NEEDS_HUMAN — returning before `verify_worker` + `closeout`, the code path
  that compares against the baseline and auto-sends new failures back. The background baseline (same suite, 37 min)
  proved Steward can run a long suite; the candidate was simply never verified.
- Jev was down for the whole job (TypeSafe HTTP 402, one `jev_unavailable` event, no alert), so `done_unproven` never ran.
- The doctor (Sol) owns stalls only; no watchers were configured; the trajectory advisor saw `thrash` once, after
  NEEDS_HUMAN, and a first sighting is only recorded.
- The tech lead exists only for fleet jobs (`fleet_live.py` wires `TechLeadSupervisor`; `on_event` skips legacy jobs)
  and is advice-only (typed proposals; `FORBIDDEN_ACTIONS` includes shell/exec/grant/pause).

The owner fixed it by hand with one send-back. That send-back is exactly what a supervisor should have done. More broadly,
a large share of worker stalls are workers misreading their own authority; a capable tech lead (Astra, Fable — models
The owner rates above himself for this) should audit and unblock them autonomously.

## 2. Goals

1. A finish check that cannot finish never blocks real verification; new failures reach the worker named.
2. Detection is layered and never single-point: Jev (fuzzy, every 10 s), deterministic code facts (floor), and a slow
   adaptive outer loop; a Jev outage alerts the owner instantly.
3. One tech lead role for every job (single-worker and fleet) that diagnoses, unblocks, sends back, and — when needed —
   takes over editing, deciding autonomously within a tunable policy above a hard-coded floor.
4. The tech lead remembers: a per-job notebook and a cross-job playbook.
5. The tech lead knows what the system is orienting toward at goal/milestone level, without being fed whole plans.
6. The owner sees only exceptions plus a digest.

## 3. Non-goals

- Changing the hard floor (§5 D-04) by policy. Ever.
- Replacing any rung. Jev, doctor, watchers, trajectory advisor and the closeout checker all stay; they feed facts.
- Live-feed notifications per autonomous fix.
- Race / best-of-N (still a later milestone).

## 4. Users and UX

The owner speaks in plain language. Target interactions:

- He does nothing for an obvious defect: the tech lead fixes it and the job reaches CHECKS_PASSED; `steward result`
  shows a digest line ("tech lead (Astra) took over: removed owner name from 2 comments, owner sweep green").
- "What has the tech lead been doing?" → `steward result <job>` digest section; `steward tech-lead notes <job>` notebook.
- "What has it learned?" → `steward tech-lead playbook`.
- "Make it more/less autonomous" → edit `~/.config/steward/tech-lead.toml` (Claude does it on request).
- He is interrupted (NEEDS_YOU) only for: a floor hit, a final decision (accept/merge), or the tech lead exhausted.
- A Jev outage reaches the Notifications bot (`tg send`) within one tick.

## 5. Decisions (locked with the owner, 2026-10-03)

- **D-01** Roles: the **Arbiter** (code, no model) routes each fact to exactly one rung, queues and coalesces. The
  **tech lead** (roster-ordered Astra → Fable, D-12 of v2.5) owns the fix. There is no separate "fixer" role.
- **D-02** Fix mode is hybrid (option C): default send-back with exact fix instructions; the tech lead **takes over**
  (pause worker → hold the attempt lease → edit → commit → resume worker with a note) when the worker is dead, when the
  same issue survived two send-backs, or when the fix is small (≤ 20 changed lines by default, policy-tunable).
  Exactly one writer per worktree at any time.
- **D-03** Autonomy is broad from the start: the tech lead decides by itself within `~/.config/steward/tech-lead.toml`
  (permissions, grants, paths, send-backs, takeovers, judgment-based fixes). The owner tunes the policy by reviewing outcomes.
  Judgment-based fixes (Jev/advisor concerns, confidence ≥ 0.8) are ACTIVE, not shadow; precision is still scored.
- **D-04** Hard floor, enforced in code before any tech-lead action, never openable by policy:
  1. no push, publish, deploy, or contacting anyone outside;
  2. no accept, merge, incorporate or release;
  3. no spending past the job's money or rate caps;
  4. no exposing secrets or granting keys the job was not given;
  5. no deleting or rewriting outside the job's worktree; never the live Steward store or services;
  6. no weakening, skipping or deleting tests to get green.
  A floor-blocked action is refused, logged, and escalated.
- **D-05** Unblocking is a core duty: when a worker asks for, refuses, or waits on something, the tech lead audits it
  against the contract, grants and policy and either clears it ("cleared, proceed") or redirects ("out of bounds, do X").
  Within policy it may grant; outside the floor it never may.
- **D-06** Memory: a per-job, append-only **notebook** (issue signature, evidence refs, plan item, action, outcome,
  acting model). Same signature seen once → change strategy; third sighting → escalate. A cross-job **playbook** of
  issue types with fix recipes and success rates; success strengthens, failure or overrule demotes; the top relevant
  entries are read at the start of each review.
- **D-07** Outer loop is adaptive: every 10 min while the job is active or flagged, every 60 min when quiet and
  healthy, immediately on any routed fact. Applies to every job.
- **D-08** No clogging: at most one review in flight per job with a time limit; facts arriving meanwhile queue in the
  Arbiter and coalesce into the next review; a hung review falls through Astra → Fable → the owner; Jev, doctor and code
  facts run independently of the tech lead; "no review completed within its limit" is itself a fact and escalates.
- **D-09** Orientation, not plans: every review gets an **orientation brief** (goal, finish line, contract + floor
  summary, milestone goals, one-line status per phase, the current phase's must_have truths; ≤ ~1,500 tokens; cached;
  rebuilt only when plan or contract hashes change). Drill-down into one named plan section is pulled on request and
  bounded. Full reads happen only on takeover, scoped to the touched files and their plan task. A per-review token
  budget lives in the policy file.
- **D-10** Escalation to the owner: NEEDS_YOU only for floor hits, final decisions, or tech-lead exhausted (third sighting,
  fallthrough exhausted, review watchdog). Jev outage → `tg send` immediately. Everything autonomous → per-job digest
  in `steward result`.
- **D-11** Verification floor: a finish check that times out or cannot run is "unverified", not "not met"; Steward then
  runs the real checker (`verify_worker` + closeout), in the background for suites beyond the tick budget, and new
  failures go back to the worker with the first failing test named — never to NEEDS_HUMAN on a timeout alone.
- **D-12** v2.6 is a follow-up of v2.5 (`new --follow-up 14305aeb` after the owner accepts it). It extends v2.5's tech
  lead (roster, fallthrough, no self-review, shadow switch, per-lane Jev, Arbiter); v2.5's advice-only limit and
  fleet-only wiring are superseded by D-02/D-03 and "every job".

## 6. Verified facts (recon, 2026-10-03, job 14305aeb branch at 824874e)

- `runner._run_closeout`: fleet jobs branch to `_run_fleet_closeout` first; single-worker jobs run `_commit_nudge`,
  then `_finish_line_gate`, then `verify_worker` → `closeout.closeout` (which sends `new_failures` back to RUNNING).
- `stability.finish_check_command` picks the first runnable backticked command after "Finish line:" — including one
  inside the Don'ts or a full-suite command.
- `bg_baseline` already runs a background baseline with `wait(... timeout_s)`; reuse its pattern for candidate checks.
- `fleet_tech_lead.TechLeadSupervisor.packet()` carries fleet graph, lanes, facts, usage, last 5 proposals, owner
  decisions — no goal, finish line, contract or plan context. `ACTION_TYPES = (split, assign, reorder, steer, diagnose,
  owner_decision)`; `FORBIDDEN_ACTIONS` includes stop/pause/shell/exec/grant/set_permission.
- `jev_observer.CHOICES` includes `done_unproven`, `looping`, `off_track`, `risky_action`; `FLEET_CHOICES` the four
  fleet flags; `jev_watch.ROUTES` maps `done_unproven` → nudge.
- Permission flow: `permissions.decide` → `resolver` tiers → approvals (`approval opened/approved`; 218 on 14305aeb).
- `lease.acquire/release` hold the single attempt lease; `auto_interrupt` and steering deliver pauses/notes.
- The full suite on a clean clone of 824874e: 4465 passed, 1 failed (`test_enable_mcp_with_systemd`, pre-existing).

## 7. Success metrics

- Replay of the 14305aeb failure (slow suite + planted owner-name leak) reaches CHECKS_PASSED with no human.
- With Jev forced to 402: the code-fact floor still routes facts, and the outage alert is delivered.
- A single-worker job with a planted permission confusion and a planted small defect: the tech lead clears the first,
  takes over for the second, the notebook and digest record both, and the playbook gains an entry.
- A forced floor violation proposal is refused in code and escalated.
- A hung tech-lead review falls through to Fable, then escalates; Jev and the doctor keep running meanwhile.
- Full test suite green throughout; finish lines never name the 30-minute suite as the finish check.

## 8. Delivery

Four GSD phases (ROADMAP Milestone v2.6), requirements SUP-01–SUP-16:
33 verification floor → 34 layered detection → 35 tech lead for every job (orientation, policy + floor, unblock,
send-back, takeover, no-clog) → 36 tech-lead memory, adaptive outer loop and digest.
