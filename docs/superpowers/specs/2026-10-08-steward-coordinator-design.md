# Steward coordinator — design

Date: 2026-10-08 · Branch: fleet-registry · Status: spec, awaiting the owner's review
Brief: `docs/superpowers/briefs/2026-10-08-steward-coordinator.md` (its §3 "Already decided" is binding here)
Builds on: `docs/superpowers/specs/2026-10-08-steward-zero-touch-design.md` (ZT; §5.1 ladder, §5.5 relative gate,
§5.7 metrics) and its plan (Phase 3 = Tasks 7–11, Task 10 = ladder + ask). Nothing here forks that design.

## 1. Problem

Every off-path failure in Steward ends with the owner, even when the fix is mechanical. Job 5b165868: verification
reported 10 new failures (`ValueError: too many values to unpack`) at 10:47 PDT; closeout told the worker to fix
them; the worker could not reproduce; recovery guided, then replaced the worker, then exhausted its budget →
NEEDS_HUMAN. The chair settled it in ~3 minutes: the 10 tests pass on the job head and on base; they read live
machine state that a deploy-self changed at 10:47. Steward's recovery never questions the failure itself.

Verified against the repo and the live DB (read-only, 2026-10-08):

| Claim in the brief | What the code/DB shows | Consequence |
|---|---|---|
| Recovery-exhausted NEEDS_HUMAN carries a `confirmed_human` blocker | `runner._apply_action` (runner.py:2712–2744) does `set_state(NEEDS_HUMAN, rule=recovery_budget_exhausted)` and **opens a pending `general` approval** (`approval 23a97758`); the blockers raised are `recovery_exhausted` and `repeated_failure`, both **candidate**, never confirmed | ZT §5.1 counts a pending `general` approval as an actor, so the ladder never sees this shape. The gap holds; the mechanism differs. |
| The owner was asked | Event 126557 `owner_notice_held {why: awaiting_technical_review}`: the NEEDS_YOU notice was held behind a TL review that was itself budget-deferred (754 `allowance` deferrals) | Nobody was told for 3.5 h. The coordinator must wake on the **state transition**, not on the notice. |
| "Existing Herdr owner line" | `push.py`: the control pane types one line into the creator's Claude pane; `notify.py` Telegram is off (`[alerts] telegram` unset); `health.tick` alerts only while the dash runs | The ask stays on that push line. Coordinator self-failure needs its own watchdog + `tg`. |
| No re-verify path | No CLI verb re-runs verification; `python3 -m steward.bg_verify JOB` is a detached module entry (bg_verify.py:187); ZT Task 10 adds control-internal `requeue_verify` | The coordinator gets a job re-verified by making the worker re-report (`approve` the recovery approval + `steer`, or `send-back`), exactly what the chair did (events 126629, 126718). |
| — | An in-job **unblock agent** already exists: `steward unblock JOB` (unblock_agent.py) runs Astra→Fable with a guarded verb allow-list, forced origin `unblock-agent`, and a control-pane `guard` | Reuse its three-layer pattern (check_command → forced origin → control guard). The coordinator is not a second copy of it: it is event-driven, cross-job, deterministic-first, and emits rules. |
| — | `events` has no actor column; `actor.bind` stamps `evidence.actor` for relayed non-owner commands (store.py:591); origins are an unauthenticated string; `ORIGINS` in cli.py:177 is the accepted `--origin` list | "actor=coordinator" = origin `coordinator` classified `operator_agent:coordinator`, forced non-owner. |

## 2. Decided by architect

One line of why each. The brief's §3 stays as decided; these settle its §4 open questions and what the evidence forced.

| # | Question | Decision | Why |
|---|---|---|---|
| D1 | Host | **Python daemon under `systemd --user`** (`steward-coordinator.service`) on this box, own state dir `~/.local/state/steward-coordinator/`, own budget; its Herdr tab is a view (log tail), not the process. (the owner chose this option.) | Survives Herdr and Steward dying; has the DB, worktrees, pytest env and relay locally; Kripke would need SSH for all four; a Steward lane shares Steward's failure domain. |
| D2 | Model lane | **Codex first** (`codex exec -s read-only -m gpt-5.5`), fallback `env -u ANTHROPIC_API_KEY claude -p --model sonnet --max-budget-usd 2`; Opus only on a re-opened episode (the owner said retry, or overturned). The model returns a JSON plan; the daemon validates and executes. | Separate budget pool that kept working when the TL went dark for allowance; Sonnet default per the tier policy; Opus reserved for the hard second look. |
| D3 | Wake mechanism | **Poll the Steward DB read-only** (`file:…?mode=ro`) every 15 s: `events.seq` cursor (global AUTOINCREMENT) + `jobs.updated_at`. Triggers in §4. No hook inside Steward's escalation path. | A hook would put the coordinator back inside Steward's failure domain; the seq cursor is already the incremental read Steward uses (`events_since`). |
| D4 | Dedupe | **One open episode per job.** Key `(job_id, trigger_kind, trigger_seq)`; a new trigger on a job with an open episode is appended to it; after close, the same job re-opens only on a trigger with `seq` > last seen and after a 30-min cooldown; ≥ 3 episodes per job per 24 h → open as **ask-only** (no actions, no model). | Bounded, idempotent, and the counter is what stops a ping-pong between coordinator and recovery. |
| D5 | Relation to the ZT ladder | The coordinator is **the rung after the ladder's ask is raised and before the owner hears it**, not a replacement for the ask. The ask (and recovery's NEEDS_YOU) is raised as today; its owner notice is **held for ≤ 20 min under `awaiting_coordinator`** while the coordinator's heartbeat is fresh; held notices fall open at once when the heartbeat is stale or the episode closes `asked`. | Keeps Task 10 intact (one integration line in the notice gate); the owner is never silently delayed by a dead coordinator; the held window is the only delay he sees. **the owner confirmed (P1): hold ≤ 20 min.** |
| D6 | Action whitelist | The table in §5; each action is one CLI verb, with its evidence standard and the control-pane guard that refuses it without that evidence. | Code can check evidence; the model cannot be trusted to. |
| D7 | Rule-proposal sink | **tasks-hub task** (project `steward`, tag `rule-proposal`), one per novel unblock, carrying the episode id and the proposed deterministic rule; absorbed when a task note cites the landing commit. The coordinator never launches Steward jobs for itself. | Launching jobs spends budget and breeds churn (RC7) while the ZT train is still landing; a task is visible on the digest and the owner says "build it". **the owner confirmed (P2): tasks only, never self-launched jobs.** |
| D8 | Budget | Per episode: ≤ 2 rerun pairs (head+base, ≤ 20 min wall each), ≤ 2 model calls, each capped $2 (Claude) or one codex turn. Daily: **$10 Claude + 20 codex calls**; on exhaustion every new episode closes `asked` with "coordinator budget exhausted" in the diagnosis, and one `tg` per day. | Cheap compared with one TL allowance; exhaustion degrades to today's behaviour, never to silence. **the owner confirmed (P3): $10 Claude + 20 codex calls per day.** |
| D9 | Safety: injection | Pane text, transcripts, diffs and files reach the model only inside a fenced `UNTRUSTED` block; the system prompt names them as data; the model's only output is a JSON plan; the daemon validates every action against §5 (verb, job id, flags, evidence) **before** running it; verbs run with `STEWARD_FORCED_ORIGIN=coordinator` and the control pane's `coordinator_guard` refuses anything off the list. | Same three layers the unblock agent already proves; the model never holds a shell. |
| D10 | Safety: audit, overturn, kill | Audit = Steward events with `evidence.actor=operator_agent:coordinator` + one `coordinator_episode` event per close (shown inline in the dash) + the episode row in the coordinator's own DB. Overturn = `steward coordinator overturn <episode> "<why>"` (the owner or the chair for him): records `overturned`, counts in precision, reverses nothing automatically. Kill = `systemctl --user stop steward-coordinator`, or file `~/.local/state/steward-coordinator/KILL` (the daemon exits; the guard refuses origin `coordinator` while it exists). | Two independent kill paths; overturn is a record, not a second automation. |
| D11 | Where the code lives | `steward/coordinator/` subpackage in this repo (separate process, same package, same test gate), plus `steward coordinator …` CLI group. The daemon re-execs itself when the deployed HEAD changes (as control does). | Keeps the package gate, npm manifest and deploy-self discipline; "outside the failure domain" is about the process, not the repo. |
| D12 | Pre-ZT-P3 behaviour | Until ZT Phase 3 lands, the coordinator wakes on NEEDS_HUMAN and health red, and computes "orphan > 1 h" itself by the ZT §5.7 gaps method; rungs that need a verb that does not exist yet (finish a STOPPING job, re-queue a verify) close `asked` with the exact diagnosis. When Phase 3 lands it stops computing orphans and wakes on `liveness_ask`. | No fork of the ZT design; the coordinator never grows verbs the ladder is about to own. |
| D13 | Triage precision target | **≤ 10 % of actions overturned** over a rolling 14 days; two overturns of the same rung in 7 days disable that rung (ask-only) until the owner re-enables it. | A rung that is wrong twice is a bad rule, not bad luck. |
| D14 | Touch accounting | ZT `zero_touch_metrics` excludes `evidence.actor` starting `operator_agent:coordinator` from touches and reports them in a `coordinator` column; asks that reach the owner are the `asked` episodes. | The brief's metric counts the owner's hands, not the coordinator's. |

## 3. Architecture

```
steward-coordinator.service (systemd --user, own process, own budget)
 └─ daemon loop, 15 s
     ├─ read-only poll: events.seq cursor, jobs.updated_at, health.json (or `steward health --json` every 5 min)
     ├─ triggers (§4) ──► episode open (coordinator.db) ──► heartbeat file
     ├─ code rungs (§5.1): evidence from DB/worktree/test reruns → whitelisted verb via `python3 -m steward.cli …`
     │                      with STEWARD_FORCED_ORIGIN=coordinator  ──► control pane: coordinator_guard
     ├─ model rung (§5.2): codex exec (fallback claude -p) → JSON plan → validated → same verbs
     ├─ close: resolved | asked | gave_up  ──► `steward coordinator episode …` (relay) → coordinator_episode event
     │                                        ──► rule proposal → tasks-hub
     └─ view: Herdr tab `coordinator` tails coordinator.log

steward-coordinator-watch.timer (5 min): heartbeat older than 10 min → `tg send` once per outage
daily 07:00 PT: digest → `pixel update steward-coordinator`
```

Steward-side changes (the integration point, all small): origin `coordinator` + `coordinator_guard` in the control
pane; notice hold `awaiting_coordinator`; `steward coordinator` CLI group; dash inline episode line; health
component `coordinator`; metrics actor exclusion.

## 4. Triggers and episodes — `steward/coordinator/triggers.py`, `episodes.py`

| Trigger | Detected by | Opens |
|---|---|---|
| NEEDS_HUMAN entered | `jobs.state='NEEDS_HUMAN'` with `updated_at` > cursor; the transition event's `rule` is the trigger kind (`recovery_budget_exhausted`, `verify_rounds_exhausted`, `bg_verify_unverified`, merge fix-up rules, …) | episode `needs_human:<rule>` |
| Liveness ask (post ZT P3) | event `liveness_ask` | episode `liveness_ask` |
| Health red | `~/.local/state/steward/health.json` fresher than 120 s, else `steward health --json --no-ping` every 5 min; any component `ok=false` for ≥ 120 s (`RED_ALERT_S`) | episode `health:<component>` (job-less) |
| Orphan ≥ 1 h | post P3: `liveness_orphan` without a later `liveness_actor` for 60 min; pre P3: non-terminal job whose last non-noise event (ZT §5.1 noise list) is ≥ 60 min old and whose runner pid is dead or absent | episode `orphan:<state>` |

The daemon does not patrol jobs: the 15 s poll reads cursors only; test reruns and model calls happen inside an
episode. An episode row: `episode_id, job_id, trigger, trigger_seq, opened_at, diagnosis_json, evidence_json,
actions_json, outcome, closed_at, model_calls, cost_usd, rule_proposal_id, overturned_at, overturn_why`. Outcomes:
`resolved` (the job moved to a live state or terminal state by the coordinator's action and stayed there 10 min),
`asked` (notice released to the owner with the diagnosis appended), `gave_up` (budget/cap), `overturned` (set later by
The owner). Time-to-unstick = `closed_at − opened_at` for `resolved`.

Heartbeat: the daemon writes `~/.local/state/steward-coordinator/heartbeat` (epoch + cursor + loaded sha) every tick.

## 5. Actions

### 5.1 Code rungs (deterministic, tried first, in this order within an episode)

| Rung | When (trigger + evidence) | Verb(s), all `python3 -m steward.cli … ` with forced origin `coordinator` | Evidence standard (recorded in the episode) |
|---|---|---|---|
| **false-red check** | `needs_human:recovery_budget_exhausted` or `verify_rounds_exhausted`, and the last `verification recorded` event has `new_failures ≠ []` | Reruns (no verb): the failing ids on a detached worktree of the job head **and** of the baseline sha (`checks` label `baseline`), **2 runs each**, `-p no:randomly`, hermetic env (temp `STEWARD_HOME`, `GIT_OPTIONAL_LOCKS=0`, scratch `TMPDIR`), ≤ 20 min wall per run | ids, shas, 4 outcomes, command lines, durations |
| → *env-dependent / flaky* (head 2/2 pass, or mixed) | | `approve <job> <approval>` when a pending `general` approval with `recovery_rule` exists (resumes with a fresh budget) **then** `steer <job> --verb nudge "<ids> pass in isolation on head <sha> and base <sha>; they read live machine state; report STEWARD-DONE now"`. For `verify_rounds_exhausted` (no approval): `send-back <job> "<same text>"`. Never `--urgent`. | reproduced green = head 2/2 (base recorded); plus a rule proposal (§6) naming the ids |
| → *pre-existing* (head fails, base fails same ids) | | same verbs; text says "fail on base too; not yours" | base 2/2 red on the same ids |
| → *real* (head fails 2/2, base passes 2/2) | | `approve` (if present) + `steer` "<ids> are real regressions (pass on base <sha>, fail on head <sha>); fix these only" — one more worker round; a second exhaustion on the same ids → ask | — |
| **obsolete fix-up** | NEEDS_HUMAN or RUNNING merge fix-up (goal starts `MERGE_GOAL_PREFIX`, kv `autopilot_followup`/parent link) whose parent is RELEASED/STOPPED or has kv `incorporated_commit` | `stop <job>` | parent id, parent state/commit (guard re-checks both) |
| **superseded verify** | NEEDS_HUMAN whose reason contains `lost: superseded by cr-…` (pre ZT §5.5 reattach) | `send-back <job> "your verification run was superseded, not failed; re-report STEWARD-DONE"` | the two check-run ids |
| **dead runner** | orphan/liveness episode, state RUNNING, `launch.runner_alive` false | `runner-restart <job>` (heal) | pid, last runner event |
| **cockpit down** | `health:control` with no control pane in the workspace | `up --workspace <ws>` | health json excerpt |
| **recorded default** | owner question open ≥ 1 h with a recorded recommended option and the job blocked on it | `answer <job> <qid> --accept-recommended` | question id, the default |

A rung is skipped after 2 tries per job per 24 h (`tries` in the episode DB, recorded **before** acting, as in ZT Task 10).
Nothing in 5.1 ever runs: `accept`, `release`, `incorporate`, `launch`, `new`, `deploy-self`, `--urgent`,
`--then-accept`, `--write-source`, `--and-continue`, `--done`, `--release`, `--origin`.

### 5.2 Model rung — `steward/coordinator/model.py`

Reached only when 5.1 has no applicable rung or its action did not resolve within 10 min. Prompt = charter (§5 verbs
and refusals, verbatim) + the episode's evidence + an `UNTRUSTED` block (pane screen via `worker_view.pane_screen`,
last 20 transcript turns via `worker_view.transcript_turns`, both runtimes, redacted; `events --json` tail; diff
stat; the brief/spec/plan files the job's goal names). Output: `{"diagnosis": "...", "actions": [[verb, job, ...]],
"ask": "..."|null, "rule": "..."|null}` (same shape as `unblock_agent.parse_plan`). Allowed verbs for the model:
`steer` (nudge/focus/avoid/goal/priority), `send-back`, `answer <qid> --option K`, `answer <qid> --other "<text>"`
**only with a `cite:` line quoting the roadmap/spec/plan file and line the answer comes from**, `runner-restart`,
`approve`/`deny` of non-floor, non-acceptance approvals, `stop` only with the obsolete-fix-up evidence. Anything
else in the plan is dropped and logged `plan_action_refused`. Max 4 actions per call, 2 calls per episode.

### 5.3 The ask

If no rung resolves: the daemon writes the diagnosis on the episode, runs `steward coordinator episode … --outcome
asked`, and the notice hold lifts: the existing push line / NEEDS_YOU goes to the owner **with the diagnosis appended**
(≤ 160 chars: `coordinator: <classification>; tried <rungs>`). No second line, no new channel.

### 5.4 Control-pane guard — `steward/coordinator_guard.py` (Steward side)

Like `unblock_agent.guard`: for `origin == "coordinator"`: op ∈ {`steer` family, `send_back`, `answer`, `approve`,
`deny`, `runner_restart`, `stop`, `coordinator_episode`} (`up` is a direct command, not relayed: it is the one
verb the daemon runs when the control pane itself is gone, with `--workspace` only); `steer` never `urgent`; `approve`/`deny` via
`steer_agent.approval_refusal` (no acceptance, no floor tier) and the approval's kind must be `general` or
`permission`; `answer` never `then_accept|write_source|and_continue|done`; `stop` only when the guard itself finds
the parent terminal/incorporated; everything refused while `~/.local/state/steward-coordinator/KILL` exists.
`actor.py`: `coordinator` joins `_OPERATOR_AGENT_ORIGINS` and `_FORCED_NON_OWNER`; cli `ORIGINS` gains it.

## 6. Rule proposals — `steward/coordinator/rules.py`

Every `resolved` episode whose rung was the model rung, or whose code rung found a new class (first time this
`(rung, classification)` pair resolves a job in 30 days), files one tasks-hub task: title `rule: <one line>`, body =
episode id, trigger, evidence, proposed deterministic rule (where in Steward it belongs, e.g. "closeout: rerun
failing ids in isolation before 'fix the failing tests'"), and the ZT task it overlaps if any. The episode stores
`rule_proposal_id`. A later task note `absorbed: <commit>` marks it absorbed; the digest counts proposed vs absorbed.
The 5b165868 class proposes exactly the closeout rerun rule above and "quarantine env-dependent ids" naming the 10.

## 7. Observability

- **Record:** every verb → Steward event with `evidence.actor=operator_agent:coordinator`; every close → relay op
  `coordinator_episode {episode_id, trigger, outcome, diagnosis≤300, actions, rule_proposal_id}` written as event
  `coordinator_episode` (judgment `code`; `model_informed_code_authorized` when the model rung acted).
- **Dash:** `dashrender.job_block` prints the latest `coordinator_episode` as one line under the question lines:
  `⚙ coordinator: <outcome> — <diagnosis≤80> (<rungs>)`.
- **Live view:** Herdr tab `coordinator` (created by the daemon on start if absent, `herdr tab create` + `pane run
  tail -F coordinator.log`); never a worker/control pane.
- **Needs you:** the existing push line (§5.3), asks only.
- **Health:** `steward health` component `coordinator`: green when the heartbeat is < 120 s old and its loaded sha
  = deployed HEAD; red otherwise (dash shows it like control's stale-code red).
- **Self-failure:** `steward-coordinator-watch.timer` (systemd --user, 5 min) runs
  `python3 -m steward.coordinator watch`: heartbeat older than 10 min, or unit inactive → `tg send "steward-coordinator:
  silent since <t> (<reason>)"` once per outage (state file), and again on recovery. `tg` exit code is honoured.
- **Digest:** daily 07:00 PT `python3 -m steward.coordinator digest --publish` renders `digest.html` (episodes with
  trigger/diagnosis/actions/outcome/time-to-unstick, unblocks, overturns, proposed vs absorbed rules, touches/job from
  `steward metrics zero-touch`, budget used) and runs `pixel update steward-coordinator digest.html`. Also
  `steward coordinator status [--json]` (open episodes, budget, heartbeat) and `steward coordinator episodes
  [--since]`.

## 8. Config and data

- `~/.config/steward/coordinator.toml`: `enabled=true`, `poll_s=15`, `hold_s=1200`, `rerun_wall_s=1200`,
  `episodes_per_job_day=3`, `tries_per_rung_day=2`, `model.primary="codex:gpt-5.5"`, `model.fallback="claude:sonnet"`,
  `model.reopen="claude:opus"`, `budget.daily_usd=10`, `budget.daily_codex_calls=20`, `budget.call_usd=2`,
  `digest.slug="steward-coordinator"`, `digest.at="07:00"`.
- `~/.local/state/steward-coordinator/`: `coordinator.db` (episodes, tries, budget ledger, cursor), `heartbeat`,
  `coordinator.log`, `KILL`, `digest.html`.
- Steward DB: no new tables. New events: `coordinator_episode`, `owner_notice_held {why: awaiting_coordinator}`
  (existing kind, new reason), `plan_action_refused` (coordinator log only). kv: none.
- systemd: `steward-coordinator.service` (`ExecStart` wrapper doing `set -a; . ~/.api-keys; set +a; exec env -u
  ANTHROPIC_API_KEY python3 -m steward.coordinator run`, `Restart=on-failure`), `steward-coordinator-watch.timer`.

## 9. Invariants and safety

- The coordinator never touches the Steward DB except read-only; every write goes through the control relay.
- Never: floor items (push, deploy/publish, messages beyond `tg` about itself and the pixel digest, payments,
  credentials), editing Steward code or config, `deploy-self`, loosening a gate, `--urgent`, acceptance or floor
  approvals, release, incorporate, launching jobs.
- `tg` is send-only; no webhook; Telegram stays the notifications bot. Moshi exception unchanged.
- Every rung is idempotent and bounded (2 tries/rung/job/day; 3 episodes/job/day; daily budget).
- The notice hold is ≤ `hold_s` and only while the heartbeat is fresh; a dead coordinator changes nothing about how
  fast the owner hears.
- Untrusted text never reaches a shell; the model's plan is data until validated.
- Proven-live discipline (memory: steward-prove-live): nothing in §10 is "done" until its row is observed on a real job.

## 10. Done means (brief §6) → metric + live proof

| Done means | Metric (where) | Live proof |
|---|---|---|
| Touches per merged job < 0.3 | `steward metrics zero-touch` with the coordinator column (D14); digest shows touches, coordinator actions, asks separately | one day of real jobs with the coordinator on: touches < 0.3 while asks are listed separately |
| 5b165868-shaped case resolved with no human touch | episode `needs_human:recovery_budget_exhausted` → rung `false-red` → `resolved`, 0 owner-origin events on the job after the trigger | a real job hits recovery exhaustion on env-dependent red; the coordinator approves+steers; the job reaches ACCEPTED; the owner's push line never fires for it |
| Time-to-unstick < 1 h | `closed_at − opened_at` for `resolved`; digest p50/max | the first 5 resolved episodes all < 1 h |
| Triage precision | overturned ÷ actions, rolling 14 d, target ≤ 10 % (D13); digest | the overturn verb used once on a throwaway episode shows in the next digest and in precision |
| Every novel unblock yields a rule | episodes with `rule_proposal_id` ÷ novel resolved episodes = 1.0; absorbed count | the 5b165868 class files the closeout-rerun rule task; it is absorbed when the matching ZT/closeout change lands |
| Own silence → `tg`; daily pixel digest | watch timer state; `pixel versions steward-coordinator` | `systemctl --user stop steward-coordinator` for 11 min → one `tg` line; start → recovery line; the digest URL exists next morning |

## 11. Testing (new files only, package gate)

| File | Covers |
|---|---|
| `tests/test_coordinator_triggers.py` | seq/updated_at cursors; each trigger kind; dedupe, cooldown, per-job cap; heartbeat |
| `tests/test_coordinator_rungs.py` | false-red classification from injected rerun results (4 classes); verb argv per class; tries before acting; obsolete fix-up evidence; superseded verify; dead runner; recorded default |
| `tests/test_coordinator_model.py` | plan parsing; refused verbs dropped; `--other` without `cite:` refused; untrusted block framing; call caps |
| `tests/test_coordinator_guard.py` | control-pane refusals per op/flag; KILL file; approval kinds; stop evidence re-check |
| `tests/test_coordinator_notice_hold.py` | hold ≤ hold_s while heartbeat fresh; falls open on stale heartbeat or `asked` |
| `tests/test_coordinator_episode_event.py` | relay op writes `coordinator_episode`; dash line; health component; metrics exclusion |
| `tests/test_coordinator_watch_digest.py` | watch: tg once per outage, recovery line; digest HTML content; budget ledger and exhaustion behaviour |

## 12. Out of scope

Rebuilding TL/recovery/resolver; ZT changes beyond §3's integration lines; multi-repo train; floor changes; the
deprecated vault; CGL; a re-verify CLI verb (ZT Task 10 owns requeue); fixing the held-notice-behind-TL defect
(logged for ZT as a rule proposal on day one).
