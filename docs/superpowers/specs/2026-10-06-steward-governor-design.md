# Steward governor: machine barrier + Claude budget advisory (issue E)

Date: 2026-10-06 · Status: spec approved by the owner 2026-10-06 (critical = pause + cancel runs; budget counts every account)

## Why

8 jobs + 3 Claude sessions drove load avg to 11, filled `/tmp` (15 GB tmpfs) twice, and the
Claude weekly limit reached 96% with no Steward signal. The owner's intent: the **machine limit is a
hard barrier** — hitting it should force us to fix and clean things up — while the **Claude budget
is advisory only** for now.

## Decisions (from the owner)

| Question | Answer |
|---|---|
| Protect what | Both; machine is a barrier, Claude budget advisory |
| Over the machine limit | New launches wait in a queue; running jobs untouched; critical → pause newest |
| Mechanism | Extend the existing sequencing queue (`steward/sequencing.py`), no new service |
| Override | None; change the config numbers instead |

## 1. Machine barrier

New module `steward/governor.py`.

**Readings** (from `/proc/meminfo`, `/proc/loadavg`, `os.statvfs`, the store):

| Signal | Wait threshold | Critical threshold |
|---|---|---|
| Running jobs (state RUNNING or STOPPING) | > 6 | — |
| Available memory | < 4 GB | < 2 GB |
| `/tmp` used | > 70% | > 90% |
| Home filesystem used | > 85% | > 95% |
| 5-min load average | > 24 | — |

Thresholds live in Steward config (`governor.*` keys) with the defaults above.
`governor.read(reader=None) -> Reading` returns every value plus a list of breaches
`[{signal, value, limit, level: "wait"|"critical"}]`. The reader is injectable for tests.

**Gate.** Every launch path goes through `governor.gate(store)`: control-pane `_h_launch`, manual
`steward launch`, and `sequencing.sweep`. On any wait-level breach the job is not launched. It is
marked queued for room (kv `seq_room` = reason text), and an event `governor_wait` records the
breaches. No path bypasses the gate.

**Release.** `sequencing.sweep` (already run on every control-pane tick) launches room-queued jobs
oldest first (by queue time), **at most one per tick**, and only when `gate` passes. The existing
`after`/`lock` conditions still apply on top. Event: `governor_released`.

**Critical.** On the same tick, if any critical breach holds, the governor pauses the newest RUNNING
job via the existing pause control **and cancels that job's check runs** (queued withdrawn, running
killed, via the issue-15 kill helper; a run another live job shares is kept). Steward's pause only
stops dispatch, and the test runs are what fill `/tmp` and memory, so this is what actually relieves the
machine. The runs are requested again normally after resume (event `governor_paused`, with the reading). It pauses one job per
tick while critical persists. It resumes a job it paused (and only those, tracked by kv
`governor_paused`) once every signal has stayed below its wait threshold for 10 consecutive minutes
(event `governor_resumed`).

## 2. Claude budget advisory

`governor.budget(db=~/projects/token_efficiency/data/usage.db) -> Budget` opens the DB read-only and
takes the newest `seven_day` reading **per account** (the scraper tracks several, e.g. `primary`,
`secondary`). The owner can switch accounts easily, so both count: the state is driven by the account
with the **most room**, and the line names every account. When one account is high but another has room,
the advice is to switch accounts (`secondary 99% · primary 9% → switch to primary`); only when
every account is high does the advice become `consider --kind codex`.

| State | Rule |
|---|---|
| ok | best account < 75% |
| warning | best account ≥ 75% (or any account ≥ 90% while another has room → `switch` advice) |
| high | best account ≥ 90% |
| unknown | no reading in the last 6 h, or the DB is missing or unreadable (never an exception) |

- **Dash and `steward health`:** yellow line at warning/high (`Claude weekly 91% · resets Thu 09:00`);
  grey `budget reading stale` at unknown. Health never goes red for the budget.
- **Notice:** the first crossing of 90% per budget week sends one operational notice through Steward's
  existing notification path. Deduplicated by kv `governor_budget_noticed=<week id>`.
- **Launch:** when the budget is high and the job is on a Claude worker, the launch output adds
  `Claude budget 91%; consider --kind codex`. Nothing is blocked.

## 3. Visibility

- `steward governor`: current readings against limits, jobs queued for room (with reason), jobs the
  governor paused, and the budget line.
- `steward queue` and the dash show `waiting for room: <reason>` for room-queued jobs.
- Events `governor_wait`, `governor_released`, `governor_paused`, `governor_resumed` make limit hits visible over time. That is the signal for what to clean up.

## Testing

All with an injected fake reader, temp store, fake notifier, `TMPDIR=~/.cache/steward-tmp/<name>`:

1. Over a wait limit → launch is held, `seq_room` set, event recorded; manual `launch` is held too.
2. Readings drop → sweep releases held jobs oldest first, one per tick.
3. Critical → newest running job paused; resumed only after 10 min fully under the wait thresholds;
   jobs the user paused are never auto-resumed.
4. Budget states ok/warning/high/unknown, including a stale reading and a missing DB.
5. The 90% notice is sent once per week.
6. Full suite passes.

**Live proof:** temporarily set the running-jobs limit to 1, launch two jobs: the second shows
`waiting for room`, then starts when the first finishes. Restore the limit.

## Out of scope

- cgroup or systemd-slice hard caps (a possible later backstop).
- Enforcing the Claude budget, which stays advisory until the owner says otherwise.
- Load from non-Steward Claude sessions is measured (it shows in the readings) but not controlled.
