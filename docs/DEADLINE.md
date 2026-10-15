# Deadline jobs — operator guide (v2.2, experimental)

Companion to `docs/OPERATOR.md` (that file is a guarded live cockpit file, so the deadline guide lives here until the owner chooses to fold it in; see `.steward/questions/e058f1b8/STEWARD-QUESTIONS.json`).

## Phase 20: commitment, start and merge safety

The owner's words: "Run Steward on <repo>: <goal>, due 17:00". A deadline is **his commitment**, separate from the ETA: due time,
core outcome, acceptance and required/optional items. It is never an ETA input and never moves on its own.

**Start.** `steward new "<goal>" --deadline "+4h"` (also `17:00`, `5pm`, `2026-10-03 09:00`, ISO with an offset). One relayed
command prevalidates (feature on, human origin, due time strictly in the future, workspace from `--workspace` or
`$HERDR_WORKSPACE_ID`, worker, repo, the one-active slot), creates and contracts the job, records commitment rev 1 and launches
it at once: no plan approval, no synchronous baseline (background baseline when available, otherwise deferred and stated in the
contract's assumptions), no Jev call. Defaults: `fabric` worker, `claude`, `--model sonnet`, current workspace; `--worker`,
`--kind`, `--agent-arg`, `--workspace` override and the provenance of each is recorded. Times resolve in the owner zone
(`steward.toml [deadline] tz`, default America/Los_Angeles); a DST-ambiguous or nonexistent local time is refused with a request
for an explicit offset. A very short window (even +1 minute) is admitted and simply reports infeasibility. A failed launch is
retried once on the same job; if it still fails, any partial tab is closed, the job goes CONTRACTED→STOPPED and the slot frees
(`deadline_launch_failed`). Re-sending the same `--request-id` never creates a second job.

**One at a time; never queued.** A second deadline while one is active (nonterminal, not cleared) is refused naming the active job.
`--deadline` with `--after` is refused: the clock is a promise about an active run. Queue the job without a deadline, then give it
one with `steward deadline JOB set WHEN` once it is running (validated at that moment).

**Revise (human only, relayed).** `steward deadline JOB` shows due time, rev, items with classes, mode and every revision with
who/why. `deadline JOB set WHEN`, `scope ITEM optional|required`, `clear` append a full revision (`--expect-rev N` refuses a stale
write with exit 3). `clear` ends the clock; history stays. `deadline JOB mode off|shadow|active` (default shadow). Only cli, moshi,
dash and job-chat origins may write; the steer/unblock agents, watchers, plans and workers cannot. Items are optional only when the
goal text says so (optional, nice to have, if time, stretch, bonus).

**Merge is the owner's, for ever.** The first commitment sets a lifetime `deadline_ever` marker. Closeout never accepts the job
(passing checks go to NEEDS_HUMAN, `deadline verification pending`), and autopilot (`ensure_attempted`, `try_merge`, fix-merge,
follow-ups, deploy-self) skips it and every follow-up descendant, even after `clear`, `mode off`, `[deadline] enabled = false`, a
runner restart or the owner's own accept. Accepting and merging stay two separate human acts: `steward accept`, then
`steward incorporate`.

**Notices stay local.** From rev 1 on, the inherited RUNNING/READY/NEEDS_YOU phone notices, creator-pane pushes and Telegram alerts
for the job (and its descendants) are recorded locally (`deadline_notice_local`, `creator_notify_local`) and never sent. An outbound
deadline notice needs the owner's approval of that exact item (Phase 22).

## Phase 21: plan, evidence and the shadow forecast

**The worker's plan.** The brief's DEADLINE section tells the worker the due time, commitment rev and item classes,
and asks it to commit `.steward/deadline/<job8>/PLAN.json` within minutes and keep working (no approval wait).
Steward reads it only from committed HEAD, validates it against the current commitment revision and adopts the last
valid version into the store with its exact SHA and raw text. Rejected (one `deadline_plan_rejected` +
`deadline_guardrail` per SHA and reason, previous plan kept): malformed JSON, a stale `commitment_rev`, a missing or
reclassified required item, dropping anything not owner-optional (worker-proposed optional items stay required), an
unknown adaptation, a stop-retry without ≥3 identical failures or a runner predicate, a commit milestone without
declared paths, bad dependencies/ordering, and a candidate later than `due − 6 min` while that is still in the future.
A window already shorter than the tail is adopted and shown as infeasible, never refused. No plan by
`min(15 min, 10 % of the window)` adopts a labelled fallback (`deadline_plan_missing`). The worker removes the plan
file in a cleanup commit before reporting done; a candidate whose final diff still has it is sent back.

**Target slots.** Every adopted target is kept as `(commitment_rev, milestone, target_utc)`; moving M1 from 15:00 to
16:00 keeps the 15:00 slot and adds 16:00 (`steward deadline JOB` lists both).

**Evidence.** A commit milestone is met at the first commit where every declared path has changed and still exists
(the commit's own time); a subject line or a claim is never proof. `units:<label>` uses the ETA's git-derived unit
completion. The candidate is READY only when every required item has its declared paths changed and its declared
tests passing at the exact verified SHA in the same closeout run; otherwise `acceptance unresolved` with the missing
items named. READY goes to NEEDS_HUMAN `deadline_candidate_ready`; accepting and merging stay the owner's.

**The sign.** `At risk · estimating` until there is an adopted worker plan and affirmative progress (a met milestone,
or a units-grade ETA whose P90 fits). Weak priors can only say `At risk (early signal)`; `Unlikely` needs units-grade
timing or observed misses. A missed target is `target + max(5 min, 10 % of the gap)` without evidence and stays
recorded even if the work lands later. Worsening is immediate; improving needs a met milestone or two clean
assessments 10 min apart, at most once per 30 min; the owner's revision recomputes at once. After the due time without a
candidate: `Deadline passed · X over`, and work continues. The runner tick and the control pane's tick are the only
writers; every read (`steward deadline/status/explain`, API/MCP `get_job`, the pane and the phone row) is a pure
projection. Shadow mode sends no pushes and no steers.

## Phase 22: local check-ins, warnings, approval-gated outbound

**Modes.** `steward deadline JOB mode off|shadow|active` (default shadow). Shadow computes and shows; active also
records scheduled local check-ins, miss and warning drafts and nonurgent deadline reminders to the worker. No mode,
channel setting or earlier approval ever sends anything. `off` stops supervision; merge protection stays.

**Check-ins (local).** In active mode every adopted target slot — each original and revised milestone target, the
candidate target, and the due time while no candidate is ready — records one `deadline_checkin` event and one local
draft at its time, even when the job is on track (`steward deadline JOB notices`). A missed target
(`target + max(5 min, 10 % of the gap)` without evidence) records a separate miss. After downtime each overdue slot is
recorded individually and one catch-up draft summarizes them. The runner and the control pane both sweep (paused and
waiting jobs too); the Store transaction makes each record exactly once.

**Warnings.** A worsening sign (On track→At risk, At risk→Unlikely) records a warning draft with the reason; At risk
warnings are at most one per 15 min, Unlikely is never delayed. In active mode the supervisor may also add one
nonurgent `nudge` to the worker per worsening, plan rejection or missing plan — never urgent, never `goal`/`avoid`/
`authority`/`focus`, skipped if the owner steered since the assessment or a deadline/trajectory instruction is still
unacknowledged. The owner's own `deadline set`/`scope` records a `priority` note naming the new revision.

**Sending one notice (the owner's exact approval each time).** `steward deadline JOB notice ITEM show` prints the exact
recipient, channel, title, body, link, version and next attempt. Only after the owner approves that exact item:
`steward deadline JOB notice ITEM approve --expect-version V --attempt N` (relayed, human-only). The control pane then
makes one notifier call for that attempt. Any edit (`notice ITEM edit --title/--body`) or a channel/recipient change
makes a new version and voids the approval. A failed send, a send that raised, or a crash mid-send is recorded
failed/unknown and is never retried; a retry is a new attempt that needs a new exact approval. A catch-up draft needs
its own approval and never covers its children. With no channel configured (`[deadline] channel`/`STEWARD_NOTIFIER`
unset) everything stays local; the approval check and the sender always use the same configured channel. A worker
process relays any `steward` write as origin `worker`, which can never approve a notice or revise a commitment.

**Candidate report.** `steward deadline JOB report`: commitment revisions with who/why, each item with its proof at
the exact SHA, checks, planned vs actual slots (revisions and misses), adaptations, dropped optional items, open
questions and on-time/late. READY waits for the owner: `steward accept`, then `steward incorporate`.

**Jev and calibration.** Up to three choice labels (current milestone, has work on required items stopped, task kind)
ride the existing 10-second triage call when the question budget allows; they are display-only and never move the
sign, a milestone or a number. Finished deadline jobs add one descriptive row to `$STEWARD_HOME/deadline-calibration.json`
(sample size and observed ranges only; thresholds change only by the owner's config revision).

**Live trial: PENDING.** See `.planning/phases/22-deadline-checkins/VERIFICATION.md`.
