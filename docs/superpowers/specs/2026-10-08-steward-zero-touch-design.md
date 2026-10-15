# Steward zero-touch — design

Date: 2026-10-08 · Branch: fleet-registry (c5056f5, 494 ahead of master) · Status: spec, awaiting the owner's review
Brief: `docs/superpowers/briefs/2026-10-08-steward-zero-touch.md` (its "Already decided" section is binding here)
Review it builds on: `~/.claude/responses/2026-10-08/steward-failure-review.md` (RC1–RC8)

## 1. Problem

Steward is reliable on the happy path and needs a human whenever something happens off it. Verified against the live
DB and git on 2026-10-08 (read-only):

| Measure (since 2026-10-07 00:00 PDT) | Value |
|---|---|
| Landings on fleet-registry: manual merge commits vs automatic (Incorporate/Land) | 15 vs 4 |
| Human-judgment events: operator released / runner_restarted / operator stop / approval denied / break_glass | 167 / 39 / 13 / 27 / 8 |
| Approvals still `pending` on finished jobs (acceptance / delivery / tech_lead / general) | 117 (61 / 25 / 24 / 7) |
| `tech_lead_review_deferred` reason `allowance` | 754 |
| Orphan hours (b352b5e6 STOPPING, cdb0fd37, a50c7ca9) | ~96 h |
| ACCEPTED jobs now | 9, all in non-train repos |
| Stale `index.lock.stale-*` quarantines in the main checkout | 15 |

Mechanical touches per merged job today: ~6. Target: < 0.3.

## 2. Corrections the evidence forced

These change the design relative to the review and must not be re-litigated into the old shape:

1. **O2 is wrong as stated.** All 9 ACCEPTED jobs in other repos carry an `incorporated` + `auto_merged` event and
   kv `incorporated_commit`. Operator accept outside the train repo already merges through
   `autopilot.after_accept → ensure_attempted → _try_merge` (autopilot.py:1136–1160, 500–520). Their only gap is
   release (RC2). Two of them (steward-android) merged into a branch that is not the checked-out HEAD; four live in
   `~/.local/state/steward/worktrees/*` paths that no longer exist. "Train every repo" is therefore not needed.
2. **No approval is ever "notified".** `relayed_at` means the permission answer reached the worker; the phone
   notifier defaults to null. The owner sees owner requests as blockers in disposition `confirmed_human` rendered
   "needs you" in the dash. The liveness model uses that, not a notified flag.
3. **The sweep cannot live in the watchdog.** W is a separate 30 s process that only heals processes
   (`running_jobs` is `WHERE state='RUNNING'`, watchdog.py:257) and relays through control. Job-level remedies
   need the store writer and the relay ops control owns, so the sweep is a control `TickStep`.
4. **TL waits have no deadline unless `until=time`.** `_do_wait` (tech_lead_actions.py:785–801) writes `wake_at`
   only for time waits; an `evidence_change` wait on evidence that cannot change is forever (cdb0fd37).
5. **A superseded check run is reported as a crash.** `_supersede` cancels the older run; `shared_check_fn` raises
   `Lost` (check_runs.py:569–580); bg_verify maps it to `failed` → runner.py:2436 "crashed" → NEEDS_HUMAN
   (runner.py:2442) with no retry (`_lost_requeue` only for VANISHED).
6. **Blockers are all resolved today (331/331)**; the cleanup rule still hooks them, since `ownership._clear` runs
   only on the next reconcile sweep, not on the transition.

## 3. Decisions (the owner, 2026-10-08, one per open question)

| # | Question | Decision |
|---|---|---|
| Q1 | Liveness model | Actor is **derived** at sweep time from existing records, per state (§5.1). Orphan threshold **30 min**. Sweep = control-loop TickStep throttled to 60 s. W unchanged. |
| Q2 | Remedy ladder | Ladder in §5.2; each rung ≤ 2 tries per job per 24 h; final ask = one `confirmed_human` blocker + one Herdr owner line with `retry / stop / release`. |
| Q3 | Release on incorporation | ACCEPTED→RELEASED with `Judgment.code` **only** under rule `released_on_incorporation`, verified by receipt + ancestry (§5.3). Release-without-merge stays human-only. |
| Q4 | Cleanup scope | On terminal transition or incorporation: close stale approvals (all kinds), resolve blockers, stop merge fix-ups of a done parent in **any** state incl. NEEDS_HUMAN, STOPPED+FAILED into `sequencing._DONE`. Owner decisions are never closed; carried forward. One-time backfill. |
| Q5 | Train red | Gate = **no new failing ids vs base at the same env**; rerun failing ids once; drop the base-red freeze (30-min base re-check); superseded runs **reattach**; autopilot's merged-tree check uses the same gate. |
| Q6 | One path | Enqueue inside the ACCEPTED transition; fix-merge/incorporate on a train repo enqueue at head instead of refusing; parked retries on target move, 2nd park → one rebase fix-up (depth cap 1), then ask. Train stays single-repo. |
| Q7 | TL budget | No reserved headroom. Supervision idle cap: no non-completion TL review while a job has had no worker turn for 30 min; deferred facts fold. |
| Q8 | index.lock | Hermetic conftest (`GIT_OPTIONAL_LOCKS=0`, `STEWARD_LIVE_ROOT` = test tree) + per-test guard that **fails** a test touching the main `.git/index`; stale-lock heal moves into the sweep; external unlock-watcher retired. |

## 4. Architecture

```
control loop (single thread, 5 s tick)
 ├─ questions_delivery.tick · herdr_tabs · push · deadline · autopilot_bg · queue_sweep · governor   (unchanged)
 ├─ landing_train.tick  (180 s)  ──► train run: build → RELATIVE GATE → land → cleanup hooks
 └─ liveness.sweep      (60 s)   ──► per non-terminal job: actor? ─no─► orphan clock ≥ 30 min ─► remedy ladder
                                                                         │                              │
                                                                         └─ heal index.lock (any state) └─ last rung: ask once

store.set_state / incorporate (same transaction, every caller)
 ├─ → ACCEPTED         : landing_queue.enqueue (train repo)            [C3]
 ├─ → RELEASED/STOPPED/FAILED, or incorporation recorded:
 │     close stale approvals · resolve blockers · stop done-parent fix-ups · carry owner decisions   [C2]
 └─ ACCEPTED→RELEASED with Judgment.code only under rule released_on_incorporation (machine + store check) [C2]
```

Everything a human did today becomes either a code judgment with evidence (release, stop completion, cleanup,
enqueue, re-verify, nudge) or exactly one plainly worded ask after the ladder is exhausted.

## 5. Components

### 5.1 Liveness sweep — `steward/liveness.py` (new), wired as a `TickStep` in `cli.py` TickSteps

**Actor derivation.** For every job not in `TERMINAL_STATES`, in this order; the first rule that holds names the actor.

| State | Actor exists when | Source |
|---|---|---|
| RUNNING / PAUSED | runner live (`launch.runner_alive`) **and** one of: worker turn open; a live check run; a non-noise job event within `idle_s` (30 min) | kv `liveness` written by the runner each tick: `{turn_open, last_progress_ts, pid}` (new, small); `check_run_waiters ⋈ check_runs` state queued/running with fresh heartbeat; `events` excluding `runner_self_restart`, `heartbeat*`, `tech_lead_review_deferred`, `jev_*`, `ownership_status` |
| WORKER_FINISHED / CHECKS_PASSED | a live check-run waiter, or runner live and a closeout event within `idle_s` | as above |
| STOPPING | runner live | `launch.runner_alive` |
| NEEDS_HUMAN | an open blocker in `confirmed_human` or `waiting_external`, **or** a pending approval of kind acceptance / general / permission | `blockers`, `approvals` |
| ACCEPTED | incorporated (→ release, §5.3); or `landing_queue.status='queued'`; or a live delivery worker (kv `delivery_worker.pid`) or autopilot_bg run; or a TL/owner wait with unexpired deadline | `landing_queue`, kv, `blockers.diagnosis_json.wake_at` |
| CREATED / CONTRACTED | staged in a sequencing queue, or holding a deadline slot | `sequencing.is_queued`, `deadline_slots` |
| any | a TL wait with `wake_at > now` | `blockers.diagnosis_json`; **every** TL wait now gets `wake_at` (evidence_change waits default to 60 min, cap 1440) in `_do_wait` |

**Orphan clock.** The first sweep that finds no actor writes event `liveness_orphan` `{state, since, checked}` and
kv `liveness_orphan_since`; a later sweep that finds an actor writes `liveness_actor` and clears it. The ladder runs
when `now - since ≥ orphan_s` (30 min, `[liveness] orphan_s`). Orphan intervals are therefore recorded, not inferred.

**Remedy ladder** (§3 Q2). Each rung writes `liveness_remedy {rung, try, result}`; a rung is skipped after 2 tries
in 24 h (kv `liveness_tries`); a remedy that creates an actor resets the clock.

| Orphan shape | Rung 1 | Rung 2 | Rung 3 | Then |
|---|---|---|---|---|
| RUNNING/PAUSED/WORKER_FINISHED/CHECKS_PASSED, runner dead | heal runner in that state (`_h_runner_restart mode=heal` loses its RUNNING-only check; `launch.heal_runner` takes an allowed-states set) | — | — | ask |
| RUNNING, runner live, worker idle after a turn end with commits | code nudge "report done or continue" (steering verb `nudge`, origin `liveness`, never urgent) | second nudge | TL **completion** review (exempt from allowance) | ask |
| STOPPING, runner dead | confirm the worker pane is gone (`launch.runner_pane_id` → none, or kill via `_stop_runner_process`), then `set_state(STOPPED, what="stop finished by sweep", rule="liveness_finish_stop", judgment=code)` | — | — | ask |
| WORKER_FINISHED/CHECKS_PASSED, no live check run | re-queue the verify (`bg_verify` start; a lost run is `verify_requeued`, never "crashed") | second re-queue | — | ask |
| NEEDS_HUMAN, nothing pending | if the last verification was green and no blocking owner question: resume closeout (`closeout.closeout` from the stored verification) | re-run verify once | — | ask |
| ACCEPTED, parked | handled by the train (§5.5): retry on target move, 2nd park → one rebase fix-up | — | — | ask |
| ACCEPTED, incorporated | release (§5.3) | — | — | — |
| CONTRACTED, not staged, no slot | re-launch once (`_h_launch` with stored `launch_args`) | — | — | ask |
| TL wait expired | treat as "no actor": go to the row matching the state | | | |

**The ask.** One blocker `raise_blocker(kind="liveness_stuck", subject=job, disposition confirmed_human)` with a
`HumanRequest` whose options are `retry` (reset tries, run the ladder again) / `stop` / `release` (allowed only when
incorporated; otherwise the option is omitted). Rendered in the dash as "needs you" and sent once as a Herdr owner
line: `<id8> <goal≤60>: stuck in <STATE> for <N>h. Tried: <rung, rung>. Say: retry / stop / release.` Because the
blocker is itself an actor, the job is not re-asked. Answers come through the existing owner-request path.

**Index.lock heal** moves here: each sweep calls `incorporate.heal_index_lock` under `target_lock(timeout=0)` for
every repo that has a live job, in any state (today only the train tick and incorporate do it, for the train repo).
The external unlock-watcher loop (not in this repo) is retired at rollout; `steward health` gains component
`liveness` = red when the sweep is older than 300 s or any orphan interval exceeds 60 min.

**Config** `[liveness]`: `enabled=true`, `every_s=60`, `orphan_s=1800`, `idle_s=1800`, `tries_per_day=2`.
`STEWARD_LIVENESS=0` disables (tests set it off by default in conftest, on in their own fixtures).

### 5.2 Supervision idle cap — `steward/supervision_allowance.py`, `fleet_tech_lead.py`

A job is *idle* when kv `liveness.turn_open` is false and `last_progress_ts` is older than `idle_s`. While idle,
`fleet_tech_lead` skips non-completion reviews for that job (`tech_lead_idle_paused` event once, not per fact) and
folds pending facts into one; a worker turn or evidence change (new commit, check-run result, owner answer) lifts the
pause. Completion reviews stay exempt as today. No reserved headroom.

### 5.3 Code-judged cleanup — `machine.py`, `store.py`, `approvals.py`, `blockers.py`, `train_housekeeping.py`, `sequencing.py`

**Release on incorporation.** `machine.transition(cur, nxt, *, judgment, rule="")` accepts `Judgment.code` for
`(ACCEPTED, RELEASED)` only when `rule in CODE_RELEASE_RULES = {"released_on_incorporation"}`. `store.set_state`
passes its `rule`; before calling `transition` with that rule it verifies, in `incorporate.release_evidence(store, job)`:

1. kv `incorporated_commit` is set and equals the `merge_commit` of an `incorporated`, `auto_merged` or
   `landed_by_train` event of this job;
2. in `job.repo`, that commit is an ancestor of the branch named `source_branch` in that event, or, when the event
   names none, of some local branch head (`git branch --contains`);
3. if `job.repo` does not exist, the receipt alone suffices (recorded as `repo_gone: true`).

If the evidence fails, the store raises `AuthorityError` like any other unauthorised code release. The transition is
emitted as `what="released on incorporation", rule="released_on_incorporation", judgment=code` with the evidence
dict. `train_land._release` and `mark_parents_incorporated` use this path and stop passing `Judgment.human`.
`landing_queue.prune` releases through it too. Called from: every `_record_incorporation`, the train, and the
liveness sweep (ACCEPTED row).

**Close stale items** — one function `cleanup.on_resolved(store, job_id, *, cause)` called inside `store.set_state`
for RELEASED/STOPPED/FAILED (next to the existing `close_tech_lead_superseded` hook, store.py:1164) and from
`_record_incorporation`:

- approvals: every `pending` record of kind acceptance, delivery, tech_lead, permission, general → `status=stale`,
  `closed_reason="superseded: <cause>"`, event `approvals_superseded {n, kinds}`;
- blockers: every open blocker → `resolved` with reason `superseded: <cause>` (`ownership._clear` logic, run now,
  not at the next reconcile), event `blockers_superseded`;
- **never closed:** owner decisions — open owner questions (`questions`), blockers of kind `owner_question`, and
  approvals carrying `owner_decision: true`. They are re-homed: event `owner_decision_carried {ids}`, and the daily
  digest / train report lists them under "still waiting on you" until answered. This is the fix-queue item 1 lesson.
- merge fix-ups: `train_housekeeping.stop_superseded_fixups` loses the `!= NEEDS_HUMAN` clause and runs from this
  hook for the parent's children (`kv children` and `autopilot_followup` chains) in any non-terminal state; a stopped
  fix-up's owner questions are carried the same way. Event `fixup_stopped_parent_done {parent, cause}`.
- `sequencing._DONE = {ACCEPTED, RELEASED, STOPPED, FAILED}` (display and grouping only; launch gating already pauses
  a successor of a STOPPED predecessor via `_PAUSING`).

**Backfill.** `steward cleanup --backfill [--dry-run]` runs `on_resolved` for every RELEASED/STOPPED/FAILED job with
pending approvals or open blockers (117 records today), and `release_evidence` for every ACCEPTED job (9 today),
printing what it would do first. Run once at rollout, with the owner watching the dry run.

### 5.4 One merge path — `store.py`, `landing_queue.py`, `autopilot.py`, `delivery.py`, `cli.py`

- **Enqueue on accept.** In `store.set_state`, a transition to ACCEPTED for a repo where `config.train_owns(repo)`
  calls `landing_queue.enqueue` in the same connection transaction, event `landing_enqueued {origin, priority}`.
  `sync_accepted` on the train tick stays as the safety net and now also records `landing_enqueued {origin: "sync"}`
  (a sync that finds work is a bug signal, counted by the metrics).
- **Route, don't refuse.** `landing_queue` gains `priority INTEGER DEFAULT 0` (migration); `queued()` orders by
  `priority DESC, enqueued_at`. `fix-merge` and `incorporate` (cli) on a train-owned repo call
  `enqueue(..., priority=1, origin="fix-merge"|"incorporate")` and answer `queued for the next train (head of queue)`
  instead of "landing train owns merges". `autopilot._try_merge`, `ensure_attempted` and `delivery` keep stepping
  aside (the train is the path) but their receipts say "queued: <position>" rather than "skipped".
- **Parked entries.** The train run re-checks parked entries: if the target head differs from the head recorded at
  the last park (`last_target` column), the entry goes back to `queued` with `attempts` kept; after the second park
  on different heads, the train spawns exactly one rebase fix-up through `autopilot._repair` (goal
  `MERGE_GOAL_PREFIX`), records kv `rebase_fixup` on the entry, and the parent waits on it (actor = fix-up alive). A
  third park, or a fix-up that fails, is the ladder's ask with the diff stat in the text. Repair depth is capped at 1
  for train-spawned fix-ups (`repair_episode` gets a `max_depth` honoured by `_repair`).

### 5.5 Train red handling — `train_gates.py`, `landing_train.py`, `check_runs.py`, `bg_verify.py`, `autopilot.py`

- **Relative gate.** `train_gates.run_suite` writes JUnit (`--junitxml`); `train_gates.gate(wt, base_sha, env)` returns
  `{new_failures, preexisting, fixed, reran}`. The base run is cached per `(base_sha, env_digest)` in
  `kv _train/base_suite:<sha>` with its failing ids; computed once per target head in the train worktree (a detached
  checkout of `base_sha`, then the candidate is rebuilt). The train is green when `new_failures == []` and
  `tests_removed` is empty. Pre-existing failures are reported, never block. The 30-min base re-check: a cached base
  older than 30 min is recomputed on the next run (so a flaky base self-corrects).
- **Rerun before believing.** Failing ids from the candidate run are rerun once (`pytest <ids> -p no:randomly`); ids
  that pass on rerun are dropped from `new_failures` and listed as `reran_green` in the report line. Only then is
  `bisect` entered.
- **Base-red freeze removed.** `base_red_head` and its `skipped: base red` branch go away; a red base is a report line
  and one notice per base head (`once(...)` stays) and nothing more.
- **Reattach, not Lost.** `check_runs._supersede` re-points the consumed waiter to `keep` (`UPDATE check_run_waiters
  SET run_id=keep` where the run is cancelled with "superseded by …") so `shared_check_fn` never sees `how="lost"` for
  a supersede; it sees the superseding run and waits on it. A genuinely lost run (`VANISHED`, dead owner) makes
  bg_verify report `status="lost"` and the runner re-queues (`verify_requeued`) instead of `NEEDS_HUMAN "crashed"`;
  after 2 re-queues the ladder's ask applies. O4's budget counting excludes superseded rounds.
- **Autopilot uses the same gate.** `_test_merged_tree` compares against the base suite cache for `(HEAD, env)` and
  returns red only on new failures (the a087745c case lands).

### 5.6 Test hermeticity and the index.lock creator — `tests/conftest.py`, new `tests/test_hermeticity_guard.py`

- conftest (autouse): `GIT_OPTIONAL_LOCKS=0` for the session; per test, `stability.LIVE_ROOT` is monkeypatched to
  a tmp tree (it is computed at import from `STEWARD_LIVE_ROOT`, so the env var alone is too late) and
  `STEWARD_HOME` is already isolated, so `launch.code_root()` / `deployed.launch_root` never resolve to the deployed
  checkout; `STEWARD_LIVENESS=0`.
- Guard (autouse, function): stat the main checkout's `.git/index` and `.git/index.lock` (the repo containing
  `steward/`, found once from `__file__`) before and after each test; if the index mtime/inode changed or a lock
  appeared, fail the test with `main checkout .git touched by <nodeid>`. Tests that must read the real repo
  (`test_package_gate`, `test_live_files_untouched`) mark `@pytest.mark.real_repo`, which allows reads (git with
  `GIT_OPTIONAL_LOCKS=0` does not write the index) but still fails on a write. The constraint in
  `tests/test_package_gate.py::test_pre_existing_tests_keep_their_assertions` applies: new tests live in new files;
  the conftest edit is recorded in `MODIFIED_PRE_EXISTING`/`ALLOWED_REMOVED_LINES`.
- The heal lives in the sweep (§5.1). The external unlock-watcher is stopped at rollout.

### 5.7 Metrics — `steward/zero_touch_metrics.py` (new), `steward metrics zero-touch --since <iso> [--json]`

Computed from the DB plus `git log --first-parent` of each train/autopilot target; printed daily in the train report.

| Metric | Numerator / definition | Denominator | Today | Target |
|---|---|---|---|---|
| Mechanical touches per merged job | human-judgment events: `operator released`, `operator stop`, `runner_restarted` (origin not watchdog/liveness), `incorporated`/`incorporate_noop` with a human actor, `approval approved`/`approval denied` whose subject was already resolved at decision time (job terminal or incorporated, or kind delivery/tech_lead/general/permission), `landing_enqueued {origin: manual}`, `break_glass_input`, urgent steers (`nudge instruction recorded` with `urgent: true` and a human origin); plus `Manual merge` first-parent commits on the target | distinct jobs with an incorporation event in the window | ~6 | < 0.3 |
| Orphan hours | sum of `liveness_orphan → liveness_actor` intervals (post-rollout); `--method gaps` reproduces the review's estimate (gaps with only noise events) for the baseline | per job; report max and total | ~96 h / 3 jobs | no job > 1 h |
| Auto-land ratio | first-parent commits on the target whose subject starts `Land Steward job` / `Incorporate Steward job`, plus `auto_merged` events | all landings (those + `Manual merge`) | 4/19 | ≥ 0.9 |

Owner decisions (`question_answered`, product accepts, answers to the ladder's ask) are counted in a separate
"decisions" column and never in touches.

## 6. Data and event changes

- `landing_queue`: `priority INTEGER NOT NULL DEFAULT 0`, `last_target TEXT NOT NULL DEFAULT ''` (migration in
  `migrations.py`).
- kv: `liveness` (runner-written), `liveness_orphan_since`, `liveness_tries`, `_train/base_suite:<sha>`,
  `rebase_fixup` (queue entry).
- New events: `liveness_orphan`, `liveness_actor`, `liveness_remedy`, `liveness_ask`, `stop finished by sweep`,
  `verify_requeued`, `released on incorporation`, `approvals_superseded`, `blockers_superseded`,
  `owner_decision_carried`, `fixup_stopped_parent_done`, `landing_enqueued`, `train_gate`, `base_suite_cached`,
  `check_run_reattached`, `tech_lead_idle_paused`. All `judgment=code` except the ask's answer.
- Config: `[liveness]` section (§5.1). `[train]` unchanged.
- Blocker kind `liveness_stuck` registered with `HumanRequest` options retry / stop / release.

## 7. Invariants and safety

- Only `rule="released_on_incorporation"` may release with code judgment, and only with reachability evidence; a
  revert of the merge after release does not un-release (events are append-only), but the metric's auto-land count
  excludes reverted trains as today.
- The sweep never sends an urgent steer and never writes to a worker's worktree (SP-1).
- Every remedy is idempotent and bounded (2 tries per rung per 24 h); the ask is single per orphan episode.
- Owner decisions are never auto-closed; the carry-forward event is the audit trail.
- The train still never deletes branches, rewrites history, or pushes. Gate changes only ever relax
  *pre-existing* failures; a new failure still blocks.
- Hooks inside `set_state` must not raise into the transition: cleanup failures are logged as `cleanup_failed`
  events and retried by the sweep.
- Nothing here changes the floor: deploy/publish, push, messages, payments, credentials still come to the owner.

## 8. Testing (new files only, per the package gate)

| Area | File | Covers |
|---|---|---|
| Liveness | `tests/test_liveness_sweep.py` | actor rules per state against a fixture store; orphan clock; each rung once and twice; ask created once; `STOPPING` finish; TL wait gets `wake_at`; health component |
| Cleanup | `tests/test_cleanup_on_resolved.py` | approvals stale by kind; blockers resolved; owner decisions carried not closed; fix-ups stopped incl. NEEDS_HUMAN; `_DONE`; backfill dry-run |
| Release rule | `tests/test_release_on_incorporation.py` | machine refuses code release without the rule; store refuses without evidence; ancestry on `source_branch`; repo-gone path; train and `mark_parents_incorporated` no longer pass human |
| One path | `tests/test_landing_queue_priority.py` | enqueue inside the ACCEPTED transaction; fix-merge/incorporate route to head; parked retry on target move; one rebase fix-up then ask; depth cap |
| Red handling | `tests/test_train_relative_gate.py`, `tests/test_check_run_reattach.py` | new-vs-preexisting from JUnit; base cache keyed by sha+env; rerun drops flaky ids; no base-red freeze; superseded waiter reattaches; bg_verify "lost" re-queues |
| Idle cap | `tests/test_tl_idle_cap.py` | reviews paused while idle; lifted on turn/evidence; completion exempt |
| Hermeticity | `tests/test_hermeticity_guard.py` | guard fails a synthetic test that writes the main index; `real_repo` marker allows reads |
| Metrics | `tests/test_zero_touch_metrics.py` | each numerator term on a synthetic event log; the three targets computed from a fixture DB + throwaway git repo |

Full suite under `pytest -n 8`, gated on the real exit code.

## 9. Live proof (built ≠ proven live)

| Change | Live proof (a real job observed end-to-end) | Metric moved |
|---|---|---|
| 5.3 release on incorporation | after backfill: the 9 ACCEPTED jobs become RELEASED with `released on incorporation` events; the next train landing releases its jobs with `judgment=code` | touches (release ×7 class) |
| 5.3 close stale items | backfill closes the 117 pending approvals as stale; the next RELEASED job shows 0 pending approvals and 0 open blockers 1 s after the transition | touches (deny ×5 + stale approvals) |
| 5.3 fix-up stop | the next merge fix-up whose parent lands is STOPPED by the hook within one tick, including one in NEEDS_HUMAN | touches (stop ×3 class) |
| 5.1 sweep: STOPPING | an operator stop on a job whose runner is then killed by hand (test rig, authorised) reaches STOPPED within 31 min with `stop finished by sweep` | orphan hours (b352b5e6 class) |
| 5.1 sweep: idle worker | a worker that ends its turn without a done frame gets `liveness_remedy nudge` within 31 min and reports done, with no human steer | touches (a50c7ca9 steer), orphan hours |
| 5.5 reattach | a job whose verify is superseded by a newer commit shows `check_run_reattached` and no `bg_verify_unverified` NEEDS_HUMAN | touches (cdb0fd37 class) |
| 5.5 relative gate | a train with a pre-existing red on base lands a green candidate (`train_gate preexisting=[…] new_failures=[]`) | auto-land ratio |
| 5.4 enqueue on accept | an operator accept shows `landing_enqueued {origin: accept}` in the same second; `sync` origin count stays 0 for a day | auto-land ratio, touches (manual enqueue) |
| 5.4 parked retry | a parked entry lands on the next train after the target moves, or spawns one rebase fix-up that lands | auto-land ratio |
| 5.2 idle cap | `tech_lead_review_deferred` on idle jobs drops to 0; `tech_lead_idle_paused` once per idle job | TL cost (guardrail) |
| 5.6 hermeticity | one full suite run with the guard: zero failures named "main checkout .git touched"; no new `index.lock.stale-*` for 48 h with the external watcher off | touches (lock removals) |
| 5.7 metrics | `steward metrics zero-touch --since 2026-10-07` reproduces today's baseline (≈6, ≈96 h, 4/19) before any change lands; the daily train report carries the three numbers | all three |

## 10. Today's touch table → the change that removes it

| Job(s) | Manual fix today | Removed by |
|---|---|---|
| b352b5e6 STOPPING 24 h | `launch.restart_runner` by hand | 5.1 STOPPING rung (finish stop) + heal in any state |
| cdb0fd37 "verification crashed" | hand test run, accept, incorporate | 5.5 reattach + 5.1 NEEDS_HUMAN rung (resume closeout) |
| 66d46eb0, a50c7ca9 train red / base-red freeze | fixed test, 2 manual merges, mark landed | 5.5 relative gate + rerun + no freeze; 5.3 release on incorporation |
| 66d46eb0 not in queue, fix-merge refused | `q.enqueue` by hand | 5.4 enqueue on accept + route not refuse |
| a50c7ca9 idle 33 h | human steer | 5.1 idle rung (code nudge) + 5.2 idle cap |
| e639665a, 49fc54db, 9270199e NEEDS_HUMAN repairs | stop ×3 | 5.3 fix-up stop incl. NEEDS_HUMAN |
| 840ca812, a1c37c66, d51bdf18, 6abab3ec, e8dd45f7, 64a457d4, e453803a ACCEPTED for days | release ×7, deny stale approvals | 5.3 release on incorporation + close stale items |
| fe50ef1c 2 pending "Incorporate?" | approve/clear, release | 5.3 close stale items + release |
| e058f1b8, 60641146, c08d2346, 33157637, c050d7ba delivery escalations | deny ×5 | 5.3 close stale items (+ fix C already stopped the loop) |
| 94bae1e9 group never closes | unlink seq_after | 5.3 `_DONE` includes STOPPED/FAILED |
| control pane stale pid | kill + restart | out of scope (fix-queue item 3); the sweep does not touch control |
| TL "deferred: allowance" all morning | — | 5.2 idle cap; unsticking no longer needs the TL (5.1) |

## 11. Rollout order

1. **Metrics first** (5.7): establish the baseline numbers before anything else lands.
2. **Cleanup + release rule** (5.3) with the backfill dry run → proof rows 1–3. Largest touch reduction, lowest risk.
3. **Liveness sweep + idle cap + heal + TL deadlines** (5.1, 5.2) → orphan hours.
4. **Train: one path + red handling** (5.4, 5.5) → auto-land ratio.
5. **Hermeticity** (5.6): the guard will name offending tests on its first run; fix them in the same job.

Each step lands through the train and is proven by its row in §9 before the next starts.

## 12. Out of scope (from the brief)

Worker polling cost (O1), TL malformed reviews (O8), floor-matcher precision (O9/O10), label wording (O11),
feb2cc6e's ownership port, control-reload (fix-queue item 3; the sweep does not depend on it), multi-repo train.
