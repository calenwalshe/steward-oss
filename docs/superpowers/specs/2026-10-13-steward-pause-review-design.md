# Steward — pause at each unit, tech-lead review on every pause (design, 2026-10-13)

Status: design approved section by section by the owner (2026-10-13, workspace wA9). Next: written-spec review, then writing-plans.
Evidence base: `docs/diagnostics/2026-10-13-cadenced-supervision/REPORT.md` (17 actors, 14 steer origins, TL cadence data),
`docs/diagnostics/2026-10-11-stuck-after-sendback/REPORT.md` (RC-1..RC-8). Brainstorm log:
`~/.claude/responses/2026-10-13/steward-pause-review-brainstorm.md`.

## 1. Problem

Supervision runs on clocks and many voices, not on finished work.

| Fact (last 7 days unless noted) | Value |
|---|---|
| Worker yields (turn ends) | 205 across 50 jobs, about 4 per job |
| Yields refused as STOPPED | 106; 70 of them `same_as_last_ruled` (the F2 deadlock), 26 `unacked_steering` |
| TL reviews | median 9.1 per active worker-hour; 76% end in `wait`/`diagnose` |
| Distinct steer origins per job | 3–6 origins in 45 of 104 jobs |
| Dark stretches | ≥30 active min with no commit in 27/94 jobs; ≥60 min in 7 |
| Jev stall flags | 314, all shadow, none acted on |
| F4 (TL answered, the owner asked anyway) | 130 incidents |
| Supervision cost | $3.79 per active worker-hour (known-priced) |

## 2. Goal and success bar

Check in when work is done, not on a clock. Every worker pause gets exactly one tech-lead ruling. The tech lead is the only
actor that steers the worker; every other actor reports facts to it.

Success is measured by a trial (section 9): fewer the owner interventions per job, time-to-done no worse, cost per job no worse
than +10%, no job stuck more than 30 minutes.

Non-goals: merge-train behaviour, RC-6 (docs commits turning health red), F10 (non-pytest checks), goal quality (the intake
step's job), widening TL authority to merge, push, deploy or anything outbound.

## 3. Decisions

| # | Decision | Source |
|---|---|---|
| D1 | Every pause triggers a TL review. Jev is not the reviewer. | The owner |
| D2 | Jev and liveness watch **between** pauses (no pause for too long). They decide nothing. | The owner |
| D3 | Pausing is a per-job parameter, default = pause at each unit. | The owner |
| D4 | Units come from intake (units + per-unit finish line in the working contract; a named plan's tasks become units). Until intake exists, or with no plan, the worker names `next unit: …` at each pause. | The owner |
| D5 | Pause enforcement is both: the brief tells the worker to stop and report after each unit commit, and the runner sends a soft stop if it keeps going. | The owner |
| D6 | While a review is outstanding, the worker waits up to 5 minutes, then proceeds provisionally to the next unit. A "done" claim always waits. | The owner |
| D7 | Build by evolving the existing tech lead plus one checkpoint record per job; no new supervisor actor. | The owner |
| D8 | The TL is a fresh call per review (the ledger and checkpoint are its memory), not a persistent session. | architect |
| D9 | Record review-wait metrics; build more capacity only if they show waits are common. | The owner |

## 4. Architecture

### 4.1 Contract (`steward/contract.py`)

- New field `pause: str = "unit"`. Values: `unit` (default), `natural` (review at whatever turn ends occur, no forced stops),
  `off` (today's behaviour, for opt-out and for the trial's control arm).
- New field `units: list[Unit]`, each `Unit = {id, title, finish_line}`. Filled by intake when it exists. Empty is allowed:
  the worker then names its next unit at each pause, and the checkpoint records the emerging list.
- `units_total` stays as a hint; when `units` is non-empty it is `len(units)`.

### 4.2 Checkpoint record (new, `steward/checkpoint.py`)

One record per job, stored in the existing store (kv or a small table, chosen in the plan):

```
{n, unit_id, last_reviewed_sha, verdict, verdict_reason, open_instruction,
 pending_facts: [{source, at, kind, text, urgent}], provisional_units: [unit_id], review_in_flight}
```

It is the single shared object for supervision. Other actors append to `pending_facts`; only the TL review writes `verdict`
and `open_instruction`.

### 4.3 Runner (`steward/runner.py`, `steward/yield_lanes.py`)

- On every worker turn end, the yield classifier still runs. Its output becomes **review packet facts**, not a gate:
  - `WAITING` (a background task) and permission prompts are handled by code as today; no review.
  - Everything else (CANDIDATE, STOPPED for any reason, ASKING) produces one TL review request.
- The Jev `ask_stop` label and the 5-minute `_STOP_DUE_KEY` timer are removed from this path.
- `unacked_steering` no longer blocks closeout. The open instruction rides in the review packet, and the TL rules with it in
  view.
- **Soft stop:** when `pause=unit` and a commit lands that matches the current unit (subject or worker's report), and the
  worker is still active about 2 minutes later, the runner records one `stop and report` steer (origin `steward`,
  delivered at the next tool boundary, never a hard cancel).
- **Backstop:** 20 active minutes without a pause → a `commit and report` nudge. 35 minutes → a TL snapshot review of the
  dirty state. 60 minutes → the existing liveness and stall recovery take over.

### 4.4 Tech lead (`steward/fleet_tech_lead.py`, `steward/tech_lead_policy.py`)

- The pause review is the primary trigger. The 30/120-minute code-snapshot clock is removed. Facts marked urgent still
  wake the TL between pauses.
- **Review packet:** current unit and finish line; diff since `last_reviewed_sha`; check and test results; the worker's last
  message; `pending_facts`; `open_instruction`; the checkpoint history (verdicts so far).
- **Outcomes**, exactly one per review:
  - `continue`: names the next unit and its finish line; delivered as the only steer.
  - `send_back`: says what is unmet; the worker fixes it and pauses again.
  - `done`: all units met; goes to closeout and the landing train.
  - `ask_calen`: only for items outside TL authority (floor, outbound, value calls); the worker waits.
- A pause at an already-ruled SHA after a send-back gets a fresh ruling (`done` or `send_back` with reason). This removes
  the `same_as_last_ruled` deadlock (RC-2) without a separate spec.
- The TL may re-scope units within the goal. It may not widen the goal.
- Models: Astra → Fable → Opus fallback.

### 4.5 Other actors become fact writers

These stop writing steers to the worker and append to `pending_facts` instead: Sol stall diagnosis, flow investigator,
efficiency watch, custom watchers, liveness nudges for live runners, independent-review focus. Code-only guards (yield
classifier, code-fact floor, deadline, hung-call watch) keep their current role. Human/CLI steers are unaffected.

A guard test enforces that only origins `tech_lead`, `steward` (soft stop and backstop), `contract` and human/CLI can
reach the worker when `pause != off`.

### 4.6 Metrics (`steward metrics`)

New `pause-review` view: reviews per job, review wait p50/p90, 5-minute timeouts, provisional units later sent back,
outcome mix, plus the trial numbers (the owner interventions per job, time to done, $ per job).

## 5. Data flow (one unit)

```
1. Worker finishes unit k → commits → stops and reports ("unit k done; next: k+1")
      (still active about 2 min after the commit → soft "stop and report")
2. Runner sorts the pause:
      background wait / permission → code handles it, no review
      anything else → TL review request with packet
3. TL rules on unit k against its finish line → verdict into the checkpoint
      continue  → "go: unit k+1 (finish line …)"
      send_back → "unit k not met: <reason>"; worker fixes, pauses again
      done      → closeout → landing train (only when all units are met)
      ask_calen → notice; worker waits
4. No verdict within 5 min → worker starts k+1 provisionally; the late verdict can still send k back
5. No pause for 20 active min → "commit and report"; 35 min → TL snapshot review
```

## 6. Concurrency and capacity

- One review in flight per job. A new pause during a review is folded into it.
- Pause reviews are exempt from the hourly non-completion cap (today's `tech_lead_review_deferred` loop). The cap moves to
  per-job supervision spend.
- A small global pool (about 4 concurrent reviews), longest-waiting job first.

## 7. Error handling

| Failure | Handling |
|---|---|
| TL unreachable (all three models) | Provisional continue; "done" waits. Three consecutive failures → one notice to the owner. |
| Same unit sent back three times | `ask_calen` (loop guard). |
| Worker never pauses | 20-minute nudge, 35-minute snapshot review, 60-minute liveness and stall recovery. |
| Pause mid-unit with a dirty tree | Reviewed anyway; verdict is "commit, or continue". |
| Lost acknowledgement | The open instruction is in the packet; it cannot block "done". |
| Supervision spend cap reached | Review "done" claims only; one notice. |
| TL verdict contradicts a code check (e.g. "done" with failing tests) | The code check wins; the verdict becomes `send_back`. |

## 8. Rollout

1. Contract fields and the checkpoint record, with `pause=off` everywhere (no behaviour change).
2. Runner review-on-pause plus TL outcomes behind `pause=unit|natural`.
3. Other actors switched to fact writers when `pause != off`.
4. Metrics view.
5. Trial (section 9), then flip the default to `unit`.

Intake (separate design, paused) later fills `units`; this design works without it.

## 9. Testing and trial

**Tests (pytest, existing suite; xdist-safe):**
- Each pause kind produces exactly one review request, or none for background waits and permissions.
- Each outcome drives the right next state.
- The 5-minute provisional path, including a late send-back of a provisional unit.
- Soft stop at about 2 minutes, and the 20/35/60-minute backstops, on a fake clock.
- Guard: no non-TL origin reaches the worker when `pause != off`.
- Regression: the 2026-10-11 send-back deadlock (4060c0b0 shape) and the 2026-10-13 lost-ack shape (9dd959b8, 201550e0)
  both reach `done`.

**Live trial:** 3–5 real jobs with `pause=unit`; all other jobs on `off`. Baseline is the last 7 days. Pass bar: fewer the owner
interventions per job, cost per job no worse than +10%, no job stuck more than 30 minutes. A pass flips the default; a fail
produces a short report before anything else changes.

## 10. Open items

- Checkpoint storage shape (kv versus table): plan-level choice.
- How a commit is matched to the current unit when the worker names units ad hoc (subject prefix versus the worker's report):
  plan-level choice, with the worker's report taking precedence.
