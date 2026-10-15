# Tech lead parked reopen — design (2026-10-04)

**Status:** approved in conversation (the owner, 2026-10-04); spec for the writing-plans handoff.
**Builds on:** `docs/supervision.md` (v2.6 ladder), `docs/superpowers/specs/2026-10-03-supervision-tech-lead-prd.md`
(PRD D-02..D-10), `docs/decisions/2026-09-29-watcher-authority.md` (human-only list, trust ladder, risk-routed
resolvers).

## 1. Problem

The v2.6 ladder promises that the owner is pulled in **only** for floor hits, final decisions, or a tech lead that has run
out of options (supervision.md rung 7). That holds while a job is RUNNING. It is false once a job is in `NEEDS_HUMAN`:

- `runner.py:98` lists `NEEDS_HUMAN` in `_DISPATCHABLE_TERMINAL_STATES`; `run_forever` (`runner.py:2161`) exits on it.
  The tech lead's outer loop rides on runner ticks, so a parked job is not observed at all. Live proof: `c137ca65`
  sat 21 h in `NEEDS_HUMAN` with zero `tech-lead notes` entries; `8025f70f` sat 13 h likewise.
- `_idle_final_closeout` (`runner.py:1504`) treats a worker that went idle after a final message as finished, runs
  closeout, and the deterministic acceptance hold (`closeout._decide_acceptance`) parks it. Both Tracks jobs said
  "I did not reach the finish line … waiting for the owner" and were parked as if they had asked to be accepted.

So a worker that parks itself mid-contract waits for a human even when the tech lead, had it been looking, could have
sent it back with instructions. This spec closes that gap for one class of hold and leaves every final decision with
The owner.

Out of scope (recorded so nobody re-derives them): investigation reach outside the worktree (ssh to owned hosts is a
floor hit, `tech_lead_floor._OUTSIDE_RE`; a separate decision), contract STOP rules that say "wait for the owner" (goal
text is human-only), and reopening acceptance holds.

## 2. Decisions (the owner, 2026-10-04)

| # | Decision | Chosen |
|---|---|---|
| D-1 | Scope of "overrule" | **B** — the tech lead may send back a *stuck* parked job itself; acceptance stays the owner's |
| D-2 | Evidence bar for a reopen | **confidence only** — a proposal at or above `judgment_confidence` (0.8) reopens; no requirement to cite disconfirming evidence |
| D-3 | Where it runs | **control-pane parked pass** — the control pane's resting-job tick, not the runner, not a phase-15 watcher |
| D-4 | Reopen cap per job | reuse `fix_rounds` (2); the third becomes `tech_lead_exhausted` (default, not asked) |
| D-5 | Steer verb | a reopen is always `focus`, never `goal` (goal changes are human-only) (default, not asked) |

## 3. Hold kind — code-only classification

A new module `steward/hold.py` classifies every `NEEDS_HUMAN` transition at the moment it is recorded. No model.

| `hold_kind` | Signal (all from the Store) | Who acts |
|---|---|---|
| `acceptance` | the most recent `WORKER_FINISHED` transition carries `evidence.marker == "STEWARD-DONE"` (`runner.py:1174-1179`) or a fabric `done` frame (`_fabric_worker_finished`) — the worker claimed done | The owner only (floor class 2) |
| `stuck` | the most recent `WORKER_FINISHED` carries `marker == "idle_after_final_message"` (`runner.py:1522`), **or** the `NEEDS_HUMAN` rule is one of `finish_line_bounces_exhausted`, `background verification crashed`, `worker startup blocked` | tech lead (this spec) |
| `question` | `questions.load(store, job)` has a waiting item at transition time | The owner via the question queue (unchanged) |
| `floor` | `closeout_blocked` kv set, or the reason text contains `hard constraint violation` | The owner only |

Precedence when several match: `floor` > `question` > `acceptance` > `stuck`. The result is written as
`kv hold_kind = {kind, rule, marker, at}` and echoed in the transition evidence (`evidence.hold_kind`). Existing parked
jobs with no kv get classified lazily on the first parked pass from their event log (same function; idempotent).

Test-weakening findings (`closeout.py:219`) do **not** change the kind: weakening blocks *acceptance*, not a send-back
whose instructions restore the test. The floor still refuses any reopen text that itself weakens tests (class 6).

## 4. Parked pass

`questions_delivery.tick` (`questions_delivery.py:667`) already visits every job in `_TICK_STATES` from the control
pane. Add, for resting jobs whose `hold_kind == "stuck"` and that are not `rework_pending`:

```
if hold.kind(store, jid) == "stuck" and parked_due(store, jid, now, pol):
    job_lead.on_event(jid, sweep=True, reason="parked_stuck")
```

- **Cadence:** new policy table `[parked]` in `tech-lead.toml`: `cadence_min = 10` (1..1440), `reopen = true`.
  `parked_due` keys off `kv parked_last_review_at`. The tech lead's own one-review-in-flight rule still applies.
- **Wiring:** the control pane builds a `JobTechLead` through `runner.wire_job_lead` with the control pane's `Runner`
  (it already has one for git), the same roster (Astra → Fable), the same executor class. `worker_fn` returns `None`
  for parked jobs by construction; nothing else changes.
- **Packet additions** (`fleet_tech_lead.JobTechLead.packet`): `hold_kind`, `hold_reason` (the transition `what`),
  `last_said` (from the `idle_after_final_message` event evidence), and `allowed_actions = ["reopen", "escalate"]`
  when `state == NEEDS_HUMAN`. `unblock` and `takeover` are refused by the gate for parked jobs (there is no worker to
  unblock and no lease to take); a lead that proposes them gets `policy_blocked: parked` and the proposal falls through
  to its other actions.
- The orientation brief, notebook and playbook sections are unchanged and already Store-only.

## 5. The `reopen` action

`tech_lead_actions.JOB_ACTION_TYPES += ("reopen",)`, params `{"text", "evidence", "confidence"}` (`text` ≤ MAX_TEXT,
`evidence` ≤ 200, `confidence` float 0..1; a missing confidence is 0).

**Gate** (`tech_lead_actions.gate`, floor first as today, then in this order):

1. floor check on `{"type": "reopen", "params": …}` — the instruction text is scanned like a send-back's; a push,
   publish, outbound contact, or test-weakening instruction is refused and escalated;
2. `job.state == NEEDS_HUMAN` else `policy_blocked: not parked`;
3. `hold_kind == "stuck"` else `policy_blocked: hold_kind=<kind>` — **an acceptance/question/floor hold is never
   reopened by the tech lead**;
4. `policy.reopen` is true else `policy_blocked: reopen off`;
5. `confidence >= policy.judgment_confidence` else `policy_blocked: confidence` (D-2);
6. reopens already executed on this job `< policy.fix_rounds` else `policy_blocked: fix_rounds` (D-4);
7. `not sendback.rework_pending(store, job)` else `policy_blocked: rework pending`.

Any block or refusal becomes an `escalate` **carrying the proposal's text and rationale** ("tech lead would reopen:
…"), so the owner receives a diagnosed, ready-to-send instruction instead of silence. This is the fallback for every
parked job the tech lead cannot act on.

**Execution** (`_do_reopen`):

```
res = sendback.send_back(store, job, text, origin=f"tech_lead:{lead_id}",
                         judgment=Judgment.model_informed_code_authorized)
if res["accepted"]:
    sendback.launch(store, job, runner)      # the factored launch half
```

- `sendback.send_back` gains `judgment: Judgment = Judgment.human`. Everything it records (`operator_send_back` event,
  `REWORK_KEY.by`) uses the passed judgment/origin; the CLI, phone and MCP paths keep passing nothing and stay
  `Judgment.human`. The `NEEDS_HUMAN -> RUNNING` transition is not in `machine._HUMAN_ONLY_TRANSITIONS`, so no state-
  machine change.
- `sendback.launch(store, job_id, runner)` is the launch half factored out of `cli._h_send_back` (open a fresh job tab
  whose `steward run` sees the marker and calls `Runner.start_rework`). The CLI calls the same function after this
  change; behaviour is identical.
- `text` is prefixed `Tech lead (<lead_id>): ` in the steer so the worker and the receipts show who sent it back.

## 6. Caps, precision, spend

- Each executed reopen is a notebook sighting of the hold signature (`tech_lead_notebook.sightings`); a third sighting
  escalates as `tech_lead_exhausted` exactly as today.
- Precision (`tech_lead_actions.resolve_precision`): a reopen is `succeeded` when the reopened attempt next reaches
  `WORKER_FINISHED` with `marker == STEWARD-DONE`, `CHECKS_PASSED`, or a `NEEDS_HUMAN` of a *different* `hold_kind`
  (e.g. it is now an acceptance hold — the job got unstuck); `failed` when it returns to `NEEDS_HUMAN` with
  `hold_kind == stuck` and the same signature, or is stopped by the owner within the attempt.
- Spend: the reopened worker attempt counts against the job's money/rate caps as every attempt does; the floor's
  class 3 is unchanged. The parked pass itself costs one tech-lead review per cadence per stuck job; the existing
  supervision-spend cap (≤ 25 % of worker spend, watcher-authority item 6) applies to it.
- Watch metric: `steward tech-lead playbook` gains the `reopen` strategy row; if its score drops below 0.5 over ≥ 10
  attempts, set `[parked] reopen = false` and revisit D-2.

## 7. What remains the owner's (unchanged)

accept / incorporate / release; every `acceptance`, `question` and `floor` hold; goal changes (`reopen` is `focus`);
trust promotion; everything the hard floor names. A worker's contract STOP rule that says "wait for the owner" is still
honoured by the worker; the tech lead may reopen the resulting stuck hold under D-2, and the instruction it sends must
pass the floor — it cannot instruct the worker to break its own contract (goal text is in the packet; the prompt says
so).

## 8. Audit

- New event `tech_lead_reopen` (`rule=tech_lead_reopen`, `judgment=model_informed_code_authorized`, evidence:
  `lead`, `model`, `confidence`, `text`, `steering_rev`, `hold_kind`, `signature`).
- `steward result <job>` TECH LEAD section lists reopens with their outcome; `steward tech-lead notes <job>` shows the
  sighting; `steward receipts <job>` shows the steering revision with `origin tech_lead:<lead>`.
- The phone/dashboard NEEDS_YOU digest for a parked job shows "tech lead reviewing (next in N min)" while a stuck
  hold is within cadence, and the escalation text when it could not act.

## 9. Testing (TDD; each test written red first)

| test file | covers |
|---|---|
| `tests/test_hold_kind.py` | classification from synthetic event logs: STEWARD-DONE → acceptance; idle_after_final → stuck; bounces exhausted → stuck; waiting question → question; closeout_blocked → floor; precedence; lazy classification of a pre-existing parked job |
| `tests/test_parked_pass.py` | `tick` calls `on_event` only for `stuck` resting jobs; respects cadence and `rework_pending`; never for RUNNING/ACCEPTED; no runner required; a raising `on_event` does not stop other jobs |
| `tests/test_tech_lead_reopen.py` | gate matrix — acceptance hold → blocked+escalated with text; stuck + 0.79 → escalated; stuck + 0.80 → reopened; second reopen → reopened; third → `tech_lead_exhausted`; `reopen = false` → escalated; text containing `git push` → floor refused; `unblock`/`takeover` on a parked job → `policy_blocked: parked` |
| `tests/test_send_back.py` (extend) | non-human judgment path records `origin`/`judgment`/`by`; default stays `Judgment.human`; `launch` is called by both CLI and executor |
| `tests/test_tech_lead_precision.py` (extend) | reopen → succeeded / failed resolution rules |
| replay proof | the existing isolated-home proof harness replays `8025f70f`'s real event log: expected `hold_kind == stuck`, one parked review within cadence, outcome recorded (reopen or escalate-with-text per the lead's confidence) and **no** `tg send`, no real tab |

## 10. Files

new `steward/hold.py`; `steward/runner.py` and `steward/closeout.py` (record `hold_kind` at every `NEEDS_HUMAN`
`set_state`: the closeout acceptance hold in `closeout.py`, and `finish_line_bounces_exhausted`, verification crashed
and startup blocked in `runner.py` — the implementer greps `JobState.NEEDS_HUMAN, what=` to find them all); `steward/questions_delivery.py`
(parked pass in `tick`); `steward/fleet_tech_lead.py` (packet fields, `allowed_actions`, control-pane wiring);
`steward/tech_lead_actions.py` (`reopen` type, gate rules 2-7, `_do_reopen`, precision rules);
`steward/tech_lead_policy.py` (`[parked]` table); `steward/sendback.py` (`judgment` param, `launch`); `steward/cli.py`
(`_h_send_back` → `sendback.launch`); `docs/supervision.md` (rung 6/7 rows amended); `examples/tech-lead.toml`.

## 11. Build

As a Steward job on this repo (Steward upgrades itself that way: phases 15-19), worker `sonnet`, GSD discuss → plan →
execute → verify, finish line: all tests in §9 green, full suite green, replay proof recorded, docs amended, committed;
never push. The control pane must be restarted after incorporation for the parked pass to run (it is a long-lived
process; see OPERATOR.md "restart the control pane after upgrading").
