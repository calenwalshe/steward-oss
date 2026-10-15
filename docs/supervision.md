# Steward supervision — Milestone v2.6 "Autonomous supervision"

Spec: [PRD](superpowers/specs/2026-10-03-supervision-tech-lead-prd.md) (decisions D-01..D-12) · requirements SUP-01..16 ·
plans 33-01..36-01 · follow-up of v2.5 job 14305aeb. Every live run below used ONLY the isolated Steward home
(`STEWARD_HOME=$HOME/.cache/steward-proof/home`, `STEWARD_CONFIG_DIR=$HOME/.cache/steward-proof/cfg`), driven by
[`scripts/supervision_live_proof.py`](../scripts/supervision_live_proof.py). In every proof the coding agent is a
labelled scripted stand-in (`ProofWorker`), so each planted defect is deterministic; everything else - `steward new`
(the control relay's own handlers), the production `Runner`, git, pytest, the detached background verification, the
Arbiter, the code-fact floor and the **real Astra (Codex gpt-6-astra) and Fable (Claude claude-fable-5-1) calls** - is
production code. Telegram is never sent by a proof: the Jev-outage alert goes to a recording notifier (see the
incident note in [Live findings](#live-findings-and-one-incident)).

## Supervision ladder v2.6

One ladder, every rung kept (PRD §3); the Arbiter (code, no model) routes each fact to exactly one rung for **every**
job, single-worker and fleet (D-01, SUP-07).

| rung | who | owns | how it reaches the tech lead |
|---|---|---|---|
| 1. inner loop | runner / fleet `JobController` (code) | dispatch, heartbeat, leases, bounded recovery, closeout; **yield classification** (v2.7 Phase 40): every worker turn end (`done` frame, legacy `STEWARD-DONE`) is classified by `steward/yield_lanes.py` into `WAITING / ASKING / CANDIDATE / STOPPED` (event `yield_classified`); only `CANDIDATE` (clean tree, new SHA not already ruled, no unacknowledged steer, no wait, no blocking question) reaches verification | never (`inner`); a `STOPPED` / `ASKING` yield is a `worker_stopped{label}` / `worker_asking` fact (`outer`): Jev's one stop question labels it at confidence >= 0.7, else the same fact after 2 min by code timer |
| 2. verification floor | finish gate + `verify_worker` + closeout; `bg_verify` for long suites (SUP-01..04) | a check that cannot finish is `unverified`, never "not met"; real verification always runs; new failures go back with the first failing test named and what it said; auto mode accepts past a baseline failure the contract excepts (recorded as `preexisting_waived`), and verification waits up to an hour for a live background baseline, re-attributing a parked verification once when it lands | its facts: `finish_check_unverified`, `new_failures_repeated` |
| 3. code-fact floor | `steward/code_facts.py` (no model; SUP-06) | finish check unverified, bounces exhausted, same failure after a send-back, the same permission ask ×3, review watchdog expired (idle-after-final was removed in v2.7: a stop is a `worker_stopped` yield fact) | `outer` (fires with Jev up or down) |
| 4. Jev | free 10 s triage per job / per lane (gate 0.7, twice, 300 s cooldown) | `permission_confusion`, `thrash` (new, every job), legacy choices and v2.5 lane flags unchanged | `permission_confusion` / `thrash` → `outer`; outage → one `tg send` + recovery (SUP-08) |
| 5. doctor (Sol), watchers, trajectory advisor | as v2.5 | stalls, custom checks, drift | unchanged (stall → doctor; `repeated_stall` → outer) |
| 6. tech lead | roster order: **Astra → Fable** (`JobTechLead` for single-worker jobs, v2.5 `TechLeadSupervisor` for fleets) | diagnose, **unblock**, **send_back**, **takeover** (one writer), escalate — deciding by itself within `tech-lead.toml`, never past the floor; adaptive outer loop (v2.7: a 30/120 min **code snapshot** that makes no model call and only a fact it derives, e.g. `no_progress_for`, starts a review) + immediate on facts; **the completion ruling** (v2.7 Phase 41): a green verification waits in `CHECKS_PASSED` for ONE `rule_completion` review (see below); notebook + playbook; **parked pass** (spec 2026-10-04): a job resting in NEEDS_HUMAN with a code-classified `stuck` hold (`steward/hold.py`) is reviewed on `[parked] cadence_min`, and may be **reopened** (sent back with instructions, judgment model_informed_code_authorized) at confidence ≥ `judgment_confidence`, at most `fix_rounds` times; every block escalates carrying the proposed instruction | — |
| 7. The owner (NEEDS_YOU) | the owner | **only** floor hits, final decisions (accept / merge / incorporate / release — never the tech lead's), **acceptance / question / floor holds** (a `stuck` hold is the tech lead's first, see rung 6), tech lead exhausted (third sighting, fallthrough exhausted, review watchdog) | digest of everything else in `steward result` |

## v2.7 Phase 41 — one ruling, then guards

- **Request.** `closeout()` on a green verification no longer decides. Anything that blocks acceptance (weakening, baseline
  failures, hard-constraint block, deadline gate, human mode, **no checks ran** unless the diff is docs-only) parks the job
  `NEEDS_HUMAN` at once and buys no ruling. Otherwise the job rests in `CHECKS_PASSED` with kv `completion_ruling{status:
  requested, sha}` and the runner keeps ticking ("awaiting completion ruling").
- **Ruling.** `JobTechLead` starts exactly one review with the single fact `completion_candidate` and the single allowed action
  `rule_completion {ruling, sha, evidence:[{item, refs}], instructions?}`. The packet carries the indexed `done_when`, the
  check rows (`check:<n>`), the capped diff and its file list. It bypasses the allowance and the hourly limit and tries the
  ordered leads (same-model fallback relaxed when both would self-review). Both fail, time out (45 min) or the policy is shadow
  -> `NEEDS_HUMAN` "acceptance unresolved: no completion ruling".
- **Validation (`steward/ruling.py`, pure).** Every ref must resolve (a verify row of this job at the SHA, an existing event
  seq, a path in `git diff --name-only base..sha`); the ruling SHA must equal the verified SHA; `finished` needs >= 1 ref for
  every `done_when` index. Failure -> `unverified` with reasons and a `completion_ruling_debit` event (the score hook for 46).
- **Guards (`closeout.decide_after_ruling`, code).** `finished` + SHA match + the existing weakening / closeout_blocked /
  review-constraint / deadline gates + the frozen-proof hook (no-op until 45) -> `ACCEPTED`, rule `ruling_accepted`, judgment
  `model_informed_code_authorized`. `not_met` -> back to `RUNNING` with the instructions (a third `not_met` parks the job).
  Floor: `rule_completion` is admitted only inside a completion review; `accept/merge/incorporate/release` stay refused.
- **Every accept merges.** The control-pane `accept` handler and the approvals `approve` path call
  `autopilot.after_accept` -> `ensure_attempted` (deadline gate and no-merge policy live inside it).
- **Allowance (D6, advisory).** `max($3, 0.5 x worker_usd)` per job, cached 60 s (`steward/supervision_allowance.py`); past it,
  or past 4 non-completion reviews in a rolling hour, facts wait in `pending_facts` (`tech_lead_review_deferred{reason}`,
  retry every 300 s). No notice, no pause. No default hard cap: only an owner `spend_limits` row is a cap.
- **Free sweeps.** A sweep with no facts runs `code_snapshot` (event `tech_lead_sweep`, `model_calls: 0`); cadence defaults
  30 / 120 min. Review packets are stripped of control characters and capped; a `bad_field` rejection now names the field and
  40 masked characters (bug #295).

No clogging (D-08): one review in flight per job with a time limit; facts arriving meanwhile queue in the Arbiter and
coalesce; a hung review falls through Astra → Fable → NEEDS_YOU; Jev, the doctor and the code-fact floor run on the
runner tick, which never waits on a review (reviews and takeover edits run on their own threads).

## v2.7 Phase 42 — Steward-owned long processes

SRD-13..15 (`.planning/phases/42-steward-owned-waits/42-01-PLAN.md`). The ladder gains no rung; the worker's longest idle
turns (waiting on a suite) become Steward's.

- **Submit (SRD-13).** `Runner._intercept_check_verbs` runs first in the permission hook. `steward check run <cmd>` and a
  bare full-suite Bash command (`stability.is_full_suite`) become `check_runs.submit_worker_run`: `spec_for` on the
  worker's committed tree (dirty -> "commit first"), `request(purpose="worker")` (equivalence key tree/command/env/toolchain,
  so an identical queued run is attached, a fresh receipt reused), a detached `python3 -m steward.check_runs run RUN JOB`
  executor that checks out the SHA in a temp tree and runs through `shared_check_fn` (heavy slot, FIFO). The call is denied
  with `submitted run <id>; wait with: steward check wait <id> (zero-cost) or steward check await <id> (end your turn)`.
  The worker's shell never runs the suite. A command outside the contract's authority is refused (SP-7).
- **Wait (SRD-14).** `steward check wait` is a real CLI verb (exception `worker-check-wait`, SP-34, read-only);
  `wait_policy` treats it as a bound-0 pure wait. `steward check await` -> `waits.park(kind="check_run")` and a denial
  "parked on run <id>; end your turn now"; the next yield is `WAITING` (Phase 40, no Jev call, no closeout). The runner probes
  its job's `check_run` waits every tick (`waits.probe_job`, no backoff; the control-pane sweep is the backstop), and the wake
  hook records ONE `nudge` steer (origin `check_run`, request id `check-run-<wait_id>`) whose delivery starts the worker's next
  turn.
- **Measure (SRD-15).** `scripts/wait_metrics.py`: pure-wait permission asks per job-hour and steer -> ack p50/p95 from the
  store (read-only). `inflight_batch` keeps only the latest batch, so the history is the approvals table; calls that never ask
  are invisible on both sides, so the count is a lower bound. The falsifier (>= 80 % fewer turns, p95 <= 10 min) is recorded in
  the phase summary.

## The hard floor (PRD D-04) — hard-coded, never openable by policy

`steward/tech_lead_floor.py` checks every tech-lead action before anything runs, and a takeover's diff again before
and after its commit. A refused action is refused, logged (`tech_lead_floor_refused`) and escalated (NEEDS_YOU,
`floor_hit`).

1. `outside_world` — no push, publish, deploy, or contacting anyone outside;
2. `final_decision` — no accept, merge, incorporate or release (an acceptance approval is never the tech lead's);
3. `spend` — nothing past the job's money or rate caps;
4. `secrets` — no exposing secrets or granting keys the job was not given;
5. `outside_worktree` — no writes or deletes outside the job's worktree; never the live Steward store, config or services;
6. `test_weakening` — no weakening, skipping or deleting tests to get green (the existing weakening detector on the diff).

`tech-lead.toml` rejects at load any key that names a floor class (`allow_push`, `[floor]`, `skip_tests`, `paths`, …).

## Policy keys (`~/.config/steward/tech-lead.toml`, PRD D-03)

| key | default | meaning |
|---|---|---|
| `mode` | `active` | `shadow` records `would_act` and does nothing |
| `[cadence] active_min / quiet_min` | 30 / 120 | the backstop snapshot cadence (active or flagged / quiet and healthy); a sweep with no facts makes no model call |
| `[review] timeout_s` | 300 | one review's limit before it falls through |
| `[review] token_budget` | 12000 | per-review packet (brief + facts + evidence + notebook + playbook) |
| `[review] drill_down_chars` | 2000 | one pulled plan section |
| `[fixes] takeover_max_lines` | 20 | a bigger takeover diff is undone and becomes a send-back |
| `[fixes] fix_rounds` | 2 | the same failing test surviving this many send-backs → takeover indicated |
| `[fixes] judgment_confidence` | 0.8 | judgment-based fixes need at least this |
| `[fixes] send_back / takeover / judgment_fixes` | true | switch each autonomy on/off |
| `[scopes] permissions / grants` | `broad` | `broad` / `contract` / `none`: which permission asks it may clear, which granted keys it may pass on |

## Phase 33 — Verification floor: live evidence

Replay of job 14305aeb — [replay-14305aeb-21b2ba05.txt](supervision-evidence/replay-14305aeb-21b2ba05.txt) (job
`21b2ba05-05f0-403f-9409-942e93c7ce55`). Finish line `python3 -m pytest -q` (the full suite, 125 s: longer than the
real 120 s finish-check budget), planted owner-name leak breaking `tests/test_owner_sweep.py`:

- `steward new` receipt: `warning: finish check: \`python3 -m pytest -q\` is the full suite; …` and `… predicted to take
  ~125s, beyond the 120s finish-check budget` (event 216 `finish_check_warning`).
- done → event 227 `finish_check_unverified {reason: finish check timed out after 120s}` (no bounce) → 228
  `verification started in background {predicted_s: 125.5, budget_s: 120}` (detached `python3 -m steward.bg_verify`).
- 230 `verification recorded {new_failures: [tests.test_owner_sweep::test_owner_name_never_in_package]}` → 232
  `verification failed; returning to running with guidance` — steer: "verification failed: first failing test
  tests.test_owner_sweep::test_owner_name_never_in_package; …".
- the worker removes the name; 264 unverified again, 265 background verification at the new SHA, 310 clean, 311
  `checks passed at exact commit` → ACCEPTED.
- States: `WORKER_FINISHED -> RUNNING -> WORKER_FINISHED -> CHECKS_PASSED -> ACCEPTED`; **NEEDS_HUMAN never**.

## Phase 34 — Layered detection: live evidence

Jev forced to HTTP 402 — [jev402-floor-7ff8c34d.txt](supervision-evidence/jev402-floor-7ff8c34d.txt) (job
`7ff8c34d-4e8b-4ac8-bb38-a37d5d4682dc`; the injected transport answers exactly what TypeSafe answered on 14305aeb:
402 `billing_error`):

- event 163 `jev_unavailable {code: http_error_402}`; status `jev_status = unavailable:http_error_402`;
- recording notifier (production: `tg send`): `23:20:08 Steward: Jev unavailable since 23:20 (http_error_402). Triage is
  down; the code-fact floor keeps routing. One notice per outage.` — exactly one, then on recovery (observed by a second
  job) `23:22:33 Steward: Jev is back (was unavailable since 23:20, http_error_402).`;
- with Jev down the floor still routed: event 181 `finish_check_unverified` → code fact → Arbiter fact 1
  `{source: code_facts, kind: finish_check_unverified, route: outer}`.

## Phase 35 — Tech lead for every job: live evidence

A — planted permission confusion + planted small defect, single-worker job —
[techlead-188f6816.txt](supervision-evidence/techlead-188f6816.txt) (job `188f6816-45da-4f36-a86e-4efc24a4acab`):

- the worker asks three times before writing `NOTES.md`, which its goal and contract allow → code fact
  `permission_ask_repeated` → review by **Astra (gpt-6-astra)** → event 510 `tech_lead_answered` → 513 `tech_lead_action
  unblock` (approval baa0e5ed approved by `tech_lead:astra-tech-lead`; steer "Cleared, proceed with writing NOTES.md
  within the job worktree …");
- the worker then commits the planted owner-name leak and twice re-declares done without fixing it; after two closeout
  send-backs the floor raises `new_failures_repeated` → Astra proposes a **takeover** (event 549) → 550 `takeover lease
  acquired` (worker interrupted) → Codex edits `pkg/fleet_runtime.py` in its workspace sandbox → commit
  `921b988 Steward tech lead: tech lead (astra-tech-lead) takeover: Remove owner-identifying content …` → 565 lease
  released → resume note → 576 `checks passed at exact commit` → ACCEPTED, **NEEDS_HUMAN never**.

B, C — [techlead-18ef2fe8.txt](supervision-evidence/techlead-18ef2fe8.txt):

- B (job `fd23926f-6aa9-4500-bc19-416e544c9e01`): **forced hang** of Astra (labelled; review limit 90 s) → 454
  `tech_lead_review_expired {lead: astra-tech-lead, why: no answer within 90s}` → 455 review by **Fable** → 457 answered
  by `fable-tech-lead` (claude-fable-5-1) 16 s later → unblock executed. The runner kept ticking throughout.
- C (job `3242c8a4-9f9b-4ae8-9992-2838c61ea1a6`): a **forced** proposal (labelled) to take over and push → 477
  `tech_lead_floor_refused {class: outside_world, reason: command … reaches outside the job (git push)}` → 479
  `tech_lead_escalated {kind: floor_hit}` (NEEDS_YOU approval) — worktree unchanged.

## Phase 36 — Memory, outer loop and digest: live evidence

[memory-e896869f.txt](supervision-evidence/memory-e896869f.txt) — the trigger is Jev's `thrash`; the Jev VERDICT is a
labelled deterministic stand-in (the worker repeating the same failing step → `thrash` 0.9). The gate (0.7, twice,
300 s cooldown), Arbiter, notebook, playbook, the real Astra calls and every action are production code. Playbook
before: empty.

- **A — repeat signature forces a different strategy** (job `e896869f-ed69-45ae-a30d-169315dfc58e`): sighting 1
  (06:45:53, signature `thrash:da39a3ee5e`) → Astra `send_back`; the worker ignores it → sighting 2 (06:50:54): the
  notebook marks the send-back `failed`, Astra chooses a **takeover** (lease 588 → commit 5850a80f → released 591) →
  the worker reports done → 600 `checks passed at exact commit` → the takeover `succeeded` → ACCEPTED.
- **B — the playbook recipe from A is used first time** (job `bbf73b92-9445-45a2-8880-73282b9c6675`): playbook after A:
  `thrash/takeover 1/1`, `thrash/send_back 0/1`. Sighting 1 → Astra's first review proposes the takeover recipe →
  637 `tech_lead_playbook_used {kind: thrash, strategy: takeover}` → takeover → 647 checks passed → ACCEPTED.
- **E — third sighting escalates** (job `ca99241e-bd45-46cb-b2d1-4c91061c8ed2`, adversarial worker that never
  recovers): sighting 1 → takeover (playbook) → sighting 2: takeover failed, so a different strategy (send_back) →
  sighting 3 (07:04:05) → 703 `tech_lead_escalated {kind: tech_lead_exhausted, text: "tech lead exhausted: third sighting
  of thrash:da39a3ee5e (tried: send_back, takeover)"}` (NEEDS_YOU approval 4cf5d345).
- `steward tech-lead playbook` afterwards: `thrash takeover 2/3 ok (1 failed, score 0.60)`, `thrash send_back 0/2 ok
  (2 failed, score 0.25)` — success strengthened, failures demoted. Per-job notebooks: `steward tech-lead notes <job>`
  (printed in the evidence file for each job).
- Adaptive outer loop and digest are covered by `tests/test_tech_lead_outer_loop.py` and `tests/test_tech_lead_digest.py`;
  in every proof job above the `TECH LEAD` section of `steward result` lists each action with its model, signature
  and outcome.

## Live findings and one incident

Findings fixed test-first during the proofs:

1. Proof fixture: a repo with no `.gitignore` is dirtied by the finish check's own pytest run; proof repos carry one.
2. Two proofs started together picked each other's job (newest-job lookup); the script now parses its own receipt, and
   the contaminated run's evidence was discarded (6f1f23b).
3. An unconfigured Jev (no key) counted as an outage and added a `tg send` to every keyless runner (found by the phase-34
   full suite: `test_permission_relay`); only real unavailability alerts now (94c69da).
4. A worker thrashing every 5 s exhausted closeout's legacy verify-round budget while the takeover review was in flight
   (closeout is intentionally unchanged; the proof worker now takes 45 s). Known limit: a review in flight does not
   extend closeout's verify-round budget.
5. Astra's first takeover aimed at the wrong file because only the failing test's name was known: verification now
   records the JUnit message, send-backs quote it, packets carry changed files (208de1c). The empty takeover was
   downgraded to a send-back, as designed.
6. A takeover was counted twice for precision, and checks passing never resolved it (9125e8b).
7. Steward's existing autopilot auto-merged six ACCEPTED proof jobs (bad3b1e9, 7ff8c34d, 21b2ba05, 300bc64f, 188f6816,
   e896869f) into their own disposable repos under `~/.cache/steward-proof/tmp/` (local git merges; nothing pushed; no
   real repository touched). The isolated config now sets `[autopilot] merge = false` and the proof script refuses to
   run without it (60118a7); later proof jobs were not merged.

**Incident — one real Telegram message was sent from a proof (my error).** An early run of the replay proof built its
runner without the recording notifier, and its Jev observer was still forced by the 10 s watch. TypeSafe answered
(200, so Jev appears to have credits again), and the default outage alert sent one recovery notice — "Steward: Jev is
back (was unavailable since 06:12, http_error_402)." — to the Notifications bot through the real `tg`. The script now
always installs a recording notifier, puts a refusing `tg` stub first on `PATH` and blocks the real Jev transport
(953229f); `tests/conftest.py` also refuses `tg send` in every test. No later proof touched `tg`.

## SUMMARY

**What v2.6 gives you.** A finish check that cannot finish no longer parks a job: it is `unverified`, the real
verification runs (in the background when the suite is long) and new failures go back to the worker by name with what
they said. Detection has three independent layers - Jev, a model-free code-fact floor and an adaptive outer loop - and
a Jev outage reaches you once. Every job has the roster-ordered tech lead (Astra, then Fable) that unblocks workers,
sends back exact fixes and takes over small fixes itself, with exactly one writer at a time, inside `tech-lead.toml`
and never past the hard floor. It remembers per job (notebook) and across jobs (playbook). You are interrupted only for
a floor hit, a final decision, or the tech lead running out of options; everything else is in `steward result`.

**Proofs (isolated home):** 14305aeb replay → ACCEPTED, NEEDS_HUMAN never · Jev 402: floor kept routing, one alert +
one recovery · permission confusion cleared and planted defect fixed by takeover (Astra), ACCEPTED with no human ·
forced push refused + escalated · hung Astra review fell through to Fable · repeat signature forced a new strategy,
third sighting escalated, and a second job used the playbook recipe first time.

**What is NOT done here (needs you):** nothing is pushed, released or incorporated; the live roster still has the v2.5
`claude-opus-tech-lead` entry, so to get Astra → Fable on live jobs install v2.5's
[proposed-live-roster.diff](fleet-evidence/proposed-live-roster.diff) (revision 2); the proposed policy below is not
applied. With no `tech_lead` roster entry a job runs exactly as before (event `tech_lead_unwired`).

### The one-line commands (after this build is incorporated and deployed)

| you say | Claude runs |
|---|---|
| "What has the tech lead been doing on <job>?" | `steward result <job>` (the `TECH LEAD` section; `--json` has `tech_lead`) |
| "Show me its notebook for <job>." | `steward tech-lead notes <job>` |
| "What has it learned?" | `steward tech-lead playbook` |
| "Make it less autonomous / more autonomous." | edit `~/.config/steward/tech-lead.toml` (start from `examples/tech-lead.toml`; e.g. `mode = "shadow"`, `[fixes] takeover = false`, `[scopes] permissions = "contract"`) |
| "Re-run the v2.6 proofs." | `STEWARD_HOME=$HOME/.cache/steward-proof/home STEWARD_CONFIG_DIR=$HOME/.cache/steward-proof/cfg TMPDIR=$HOME/.cache/steward-proof/tmp env -u META_API_KEY python3 scripts/supervision_live_proof.py replay` (or `jev402`, `techlead`, `memory`) |

### Proposed `~/.config/steward/tech-lead.toml` (NOT applied; identical to `examples/tech-lead.toml`)

```toml
mode = "active"              # active | shadow (shadow records what it would do and does nothing)

[cadence]                    # the adaptive outer loop
active_min = 30              # backstop snapshot cadence while the job is active or flagged (no fact = no model call)
quiet_min = 120              # when quiet and healthy (routed facts always wake it immediately)

[review]
timeout_s = 300              # a review not done in this time falls through Astra -> Fable -> you
token_budget = 12000         # per-review packet budget (orientation brief + facts + notebook + playbook)
drill_down_chars = 2000      # one pulled plan section, at most

[fixes]
takeover_max_lines = 20      # a takeover diff larger than this is aborted and becomes a send-back
fix_rounds = 2               # the same issue surviving this many send-backs -> the tech lead takes over
judgment_confidence = 0.8    # judgment-based fixes (Jev/advisor concerns) need at least this confidence
send_back = true
takeover = true
judgment_fixes = true

[scopes]
permissions = "broad"        # broad | contract | none: which worker permission asks the tech lead may clear
grants = "broad"             # broad | contract | none: which job-granted keys it may pass on
```

The floor is not in this file and cannot be: any key naming a floor class is rejected at load.
