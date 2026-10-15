# Steward self-healing: the watchdog (issue W)

Date: 2026-10-06 · Status: design consolidated from the owner's 7 decisions (w-consult-brief.md) and the accepted MUST
items of the Sol 6.1 review (w-consult-sol.md). Installing/enabling the live systemd unit is NOT part of this work and
waits for the owner's explicit OK.

## 1. Goal

**No Steward process stays dead without a human** (SP-36: defaults, not blockers). On 2026-10-06 the control pane
died twice on `database is locked`, six runners stayed dead for about 60 minutes, and nothing restarted them, because
the only self-heal (`health.tick`) runs inside the dashboard render (`dashrender.py:472`), heals control at most once
per outage, and never heals runners.

Done means: when control or a runner dies, is wedged, or loses its pane, Steward restores it in a bounded time
with no human, never starts a second copy, never touches a worker, and tells the owner only when control itself
cannot be brought back.

## 2. Decisions

| # | Decision |
|---|---|
| D1 | The watchdog is a `systemd --user` service (`steward-watchdog.service`) running `steward watchdog`, a loop that makes one pass every 30 s. It lives outside Herdr and Steward, so it survives control crashes and deploys and starts at login/boot. |
| D2 | Healers sit in an extensible registry in `steward/watchdog.py`. Each healer has `find()` (which yields the broken targets), `heal(target)`, and `limits` (backoff and breaker settings). `<steward home>/watchdog.toml` enables and tunes each healer. The first two healers are `control` and `runners`. Later ones (dash, MCP, tunnels, stuck autopilot runs) are new registry entries, not new loops. |
| D3 | Deploys are unchanged in behaviour. The watchdog heals whatever a deploy leaves dead. The only additions are that a successful deploy records its revision, and that the watchdog coordinates with the deploy lock (§5). |
| D4 | Dead runners are restarted automatically. |
| D5 | The watchdog is the **only** background healer. The dashboard only displays: `health.tick` no longer heals, and it no longer alerts about the components the watchdog owns. |
| D6 | Single instance: the watchdog holds `<home>/locks/watchdog.lock` (flock). A second copy exits at once. |
| D7 | Watchdog state lives in the file `<home>/watchdog.json`, never in the DB. Every heal is recorded as an event (§8). |

## 3. Principle fit

- **SP-1/SP-2/SP-33: control stays the one actor on jobs.** The watchdog restores **control** directly, because
  nothing else can. It restores **runners only through control's normal `runner_restart` relay**, with
  `origin="watchdog"` and `mode="heal"`. Control decides, rechecks, and launches. While control is down, runner heals
  wait.
- **SP-14: the worker is never touched.** No healer stops, restarts, or replaces a worker or its pane. A restored
  runner takes the existing resume path (`Runner.resume_all` → `attach()` to the live shim). A missing worker is
  reported as `missing_worker` and left to the runner's own recovery policy. The proof checks that the worker's
  identity (pane and process) is unchanged.
- **Continuous ownership: the breaker never gives up.** A tripped breaker means a cooldown plus a durable tech-lead
  blocker, not abandonment (§7).
- **SP-18: judgment labels.** Restarts by the watchdog and by control on its behalf are labelled `Judgment.code`
  (controller/code judgment), never `human`.
- **SP-31/SP-34: messaging.** Control commands still travel as Herdr frames. The single owner notice goes through the
  registered `tg` notifications path (phone-notify exception).
- **Governor (issue E).** A paused job (owner pause or governor pause, kv `governor_paused`) is not RUNNING, so its
  runner is not restored. Restoring the supervisor of an already-admitted job is not new worker admission, so it is
  not gated by the governor's launch wait. Mass recovery is rate-limited (§6).

## 4. Process identity and exclusivity (Sol MUST 1)

New module `steward/proclock.py`, built on file locks and `/proc`, with no DB dependency:

- **Identity** = `{pid, start_ticks, pane, head, root, started_at}`. `start_ticks` is field 22 of `/proc/<pid>/stat`.
  A pid with a different start time is a different process, so a recycled pid is never mistaken for the old one.
  `ps` start time survives `execv`, which is correct here: a self-restart is the same process.
- **Runner lock.** `steward run <job>` takes `<home>/locks/runner-<job_id>.lock` (flock `LOCK_EX|LOCK_NB`) before it
  does anything else, and holds it for life. It writes its identity to `<home>/runners/<job_id>.json`. If the lock is
  already held, the new process prints who holds it and exits with code 3 without touching the store or the worker.
  This final guard sits at the action boundary, so even a race between two launchers cannot yield a second runner.
- **Control lock.** `steward control` takes `<home>/locks/control.lock` the same way and writes
  `<home>/control.json` (its identity plus `beat_at`). A second control exits with code 3.
- **Heartbeat.** Control refreshes `beat_at` in `control.json` every loop pass and between tick steps. Liveness and
  wedge detection use the lock and the heartbeat file, never a DB ping, so a locked DB never looks like a dead
  control.
- **Legacy processes.** If no lock is held, `proclock.find_processes` scans `/proc` for this home's
  `steward.cli control` or `steward.cli run <job>` cmdline. A process started before this change (which holds no lock)
  therefore still counts as alive, and rollout never creates doubles.
- **Recheck right before launch.** `launch.heal_runner` (used by control's `runner_restart` handler) rechecks in this
  order, immediately before typing the command:
  1. the job is RUNNING;
  2. no runner holds the lock and there is no legacy process;
  3. no fresh start marker (`<home>/runners/<job_id>.starting`, younger than `STARTING_GRACE_S` = 45 s).

  If any check fails, the request is a no-op and returns `{"accepted": true, "noop": true, "detail": ...}`. A
  duplicate request therefore never launches anything.
- **Missing pane.** If the recorded and labelled runner pane is gone, a new job tab is created (`launch_tab`), its
  root pane becomes the runner pane, and `runner_pane` is updated. The worker pane is untouched.
- **Deliberate operator restart** (`steward runner-restart`, `mode="restart"`). The live runner is stopped first
  (pid and start time verified). After the stop, the lock must be free. If it is still held, the restart is refused
  and nothing is launched.
- **Control exclusivity on the heal side.** The control healer never trusts role-token discovery alone. It
  classifies control from the lock, the identity, and the heartbeat, and it starts control on a pane only after the
  lock is confirmed free.

## 5. Deploys and the revision to restore from (Sol MUST 2)

There are two different conditions, and they are kept apart:

- **A deploy is actively changing processes**: `deploy-self.lock` is held. The watchdog holds its heals while the
  lock is held, but only for a bounded time (`deploy_hold_s` = 180 s, counted from the first held observation). After
  that it heals anyway, because a deploy wedged past three minutes must not keep Steward dead. The two-consecutive-
  observation rule (§6) already hides the brief gap of an `execv`.
- **A merge is awaiting deploy**: the 4 h `deploy_pending` mark. This **never blocks healing**. It only matters for
  the revision question below.

**Restore from the deployed revision, never from an untested checkout.** New module `steward/deployed.py`:

- After a fully successful `deploy-self`, the deploy writes `<home>/deployed.json` = `{head, root, at}`.
- The deployed head is resolved in this order:
  1. `deployed.json`;
  2. otherwise the head recorded in `control.json` by the running or last control (that is what is live);
  3. otherwise the live HEAD, if no `deploy_pending` mark is set;
  4. otherwise unknown. In that case the heal is held and escalated (control: the owner notice; runner: a blocker),
     never started from the checkout.
- `launch_root(home, live_root)`: if the live checkout HEAD equals the deployed head, start from the live checkout as
  today. Otherwise start from a **pinned tree**, a detached `git worktree` at the deployed head under
  `<home>/deployed/<sha12>`. It is created on demand, reused, and at most 3 are kept; the newest is never pruned.
- A process started from a pinned tree gets `STEWARD_LIVE_ROOT=<live checkout>`. `stability.LIVE_ROOT` is that root.
  Three things compare against `LIVE_ROOT`:
  - the "outdated" count (`runner_outdated`);
  - the control `ping`'s `root`, so health reports a pinned control as stale code, which is awaiting deploy while the
    mark is set;
  - the self-restart and `self_exec` re-exec, which chdir to `LIVE_ROOT` and drop the variable.

  So a pinned process moves to the live code exactly when deploys move everything else (deploy-pending option B is
  preserved).
- The control start (`start_control_on`) and the runner command (`worker_run_command`) both take their code root from
  `deployed.launch_root`.

## 6. Structured health states (Sol MUST 3)

The watchdog's probes yield structured observations, not red strings:

| State | Meaning | Action |
|---|---|---|
| `herdr_down` | the Herdr socket does not answer `pane.list` | hold every heal; escalate after `herdr_escalate_s` (300 s) with the one owner notice. The watchdog never restarts Herdr, because that would kill the workers' panes (SP-14). |
| `control_ok` | the control lock is held and `beat_at` is fresh | none |
| `control_dead` | no lock holder and no legacy control process | heal after 2 consecutive observations |
| `control_wedged` | the lock is held but `beat_at` is older than `wedged_s` (300 s) | diagnose (identity, `/proc` state and wchan, last beat); **confirm stopped** (SIGINT, then SIGTERM, then SIGKILL on the identity-verified pid, each waited) until the lock is free; then start. If it cannot be confirmed stopped, nothing is started. |
| `control_stale` | control answers but runs code other than the deployed HEAD | reported only: moving code is deploy's job |
| `runner_ok` | the runner lock is held (or a legacy runner process is alive) | none |
| `runner_dead` | RUNNING job, no lock holder, no legacy process | after 2 consecutive observations, ask control (`runner_restart`, mode heal) |
| `missing_worker` | runner alive, worker pane gone | reported only; the runner's recovery owns it (SP-14) |
| `unknown` | a probe failed | never healed; recorded |

Each probe runs in isolation: an exception makes that target `unknown` and the pass continues. Runner heals are
rate-limited to `max_runner_heals_per_pass` (default 2), so 25 dead runners come back over about 6 minutes instead of
in one burst. A heal counts as successful only after readiness is observed: the new lock holder is seen on a later
pass, or within the heal's own wait for control.

STOPPING and PAUSED jobs are not restored (their state says so). When a paused job is resumed it becomes RUNNING, and
the next pass restores its dead runner. This is how `steward resume` ends up with a runner.

## 7. Breaker (Sol MUST 5)

- **What counts.** Each target (`control`, `runner:<job>`) counts **unexpected exits**: a confirmed death that the
  watchdog then heals. A death does not count if it happens while a deploy is active or within `deploy_hold_s` of one,
  if the job is not RUNNING (an intentional stop), or if the observation is `unknown` or deferred.
- **Trip.** `breaker_max` (3) counted exits within `breaker_window_s` (600 s) open the breaker. A tripped breaker
  then:
  - stops healing that target for `cooldown_s` (900 s);
  - for a runner, raises a durable **tech-lead blocker**, kind `watchdog_breaker`, responsible `tech_lead`,
    impact 3, clearing predicate `watchdog_target_healthy`;
  - for control, which has no job to hang a blocker on, sends the one owner notice (§9).
- **Never permanent.** After the cooldown the breaker is half-open, and one attempt is made. If the target then stays
  healthy for `reset_after_s` (600 s), the counts reset, the breaker closes, and the blocker's predicate clears it
  (control's ownership reconcile resolves it).
- **Manual reset.** `steward watchdog --reset <target>` resets a breaker at once.
- Breaker state lives in `watchdog.json`, so it survives watchdog restarts.

## 8. Working with a locked DB (Sol MUST 4)

- **Watchdog.** Its state is file-based (`watchdog.json`, locks, identity files). It reads RUNNING jobs through a
  read-only connection with a short timeout. If that read fails, it falls back to the job list cached from its last
  good pass. Audit events go to `<home>/watchdog-events.jsonl` (append-only, always written). DB events go out through
  a spool, `<home>/watchdog-spool.jsonl`: an event that cannot be written now is spooled and flushed on a later pass.
  Control-level heals have no job row, so they are recorded only in `watchdog-events.jsonl` and `watchdog.json`.
  Runner heals are also recorded as DB events on the job: `watchdog_healed`, `watchdog_failed`, and
  `watchdog_breaker_open`.
- **Control.** `Store._conn` gets a 30 s busy timeout (until now it had Python's default 5 s; only `immediate()` had
  30 s). The receipt retry in `_put_receipt` is bounded by a **total deadline** (`RECEIPT_DEADLINE_S` = 45 s) rather
  than by a count, so 30 s waits cannot multiply into minutes.
- **Durable request/effect reconciliation.** Control appends each cmd to `<home>/control-journal.jsonl` before it
  executes the cmd (`received`), and appends the result after (`done`, with the result). Both writes are file writes,
  with no DB involved.
  - On startup and on every pass, control reconciles the journal against the DB receipts. A `done` without a receipt
    gets its receipt written; this replaces the in-memory `_unsaved` dict, which an exec or a crash would lose.
  - A `received` with no `done` was interrupted mid-execution, so its effect is unknown. It gets a receipt
    `{"accepted": false, "interrupted": true, "detail": "control restarted while executing <op>; effect unknown"}` and
    is **not** re-executed, which would risk a double effect.
  - The journal is compacted once every entry has a saved receipt.

## 9. Owner notice

There is exactly one `tg` message (notifications bot, through `notify.TelegramNotifier`) per outage, and only when
**control cannot be restored**. That means one of:

- the control breaker trips;
- Herdr is down longer than `herdr_escalate_s`;
- the deployed revision is unknown, so control cannot be safely started.

A runner trouble never pages the owner: it is a tech-lead blocker. The notice is marked in `watchdog.json` and cleared
once control is healthy again. `watchdog.toml` `notify = false` silences it.

## 10. Configuration and environment

`<home>/watchdog.toml` (every key is optional; the defaults are the ones above):

```toml
interval_s = 30
notify = true
[healers.control]
enabled = true
wedged_s = 300
[healers.runners]
enabled = true
max_heals_per_pass = 2
[limits]
breaker_max = 3
breaker_window_s = 600
cooldown_s = 900
reset_after_s = 600
deploy_hold_s = 180
herdr_escalate_s = 300
```

Shipped, **not installed**:

- `deploy/steward-watchdog.service`: `Type=simple`, `Restart=always`, `RestartSec=10`, no `EnvironmentFile`.
- `deploy/run-steward-watchdog.sh`: an ExecStart wrapper that runs from the repo with an absolute interpreter and
  exports `STEWARD_HOME` and `HERDR_SOCKET_PATH` from the defaults. It sources no secrets; `tg` reads its own
  configuration.
- `deploy/install-steward-watchdog.sh`: dry-run by default. It installs and enables only with `--yes`, and it checks
  lingering (`loginctl show-user`), printing the command to enable it rather than running it.

`steward watchdog --check-env` qualifies the environment:

- the absolute interpreter;
- the repo root is a git checkout;
- the resolved `STEWARD_HOME` exists;
- the Herdr socket exists and answers;
- `tg` and `herdr` are on PATH;
- lingering is on.

`steward watchdog --once` runs a single pass. Also new: `--status` and `--reset <target>`.

The watchdog records the head it loaded in `watchdog.json`. When the live HEAD equals the deployed head and differs
from its own head, it re-execs itself onto the new code, after passing an import check.

## 11. Proof (finish line)

There is an automated sandbox test (`tests/test_watchdog_sandbox.py`) with an isolated `STEWARD_HOME`, a scratch git
repo, an in-process fake Herdr, and real stub processes that take the real `proclock` locks under the real cmdline
shapes. The real watchdog passes, the real `launch.heal_runner`/`restart_runner`, the real `ensure`/start code, and
the real control-journal code all run against them. Only the Herdr transport and the process each pane command
starts are faked. Each case asserts that it **recovers with no human**, that **there is never a second runner**
(exactly one lock holder, and no second stub process ever reaches the work loop), and that the **worker identity is
unchanged** (same worker pane id, same worker stub pid):

1. Kill control (SIGKILL): it is restored in its pane within two passes.
2. Kill a runner: it is restored through the control relay, and the worker is unchanged.
3. Lock the DB (another connection holds a write transaction): the watchdog does not crash. Control is still
   restored; runner events are spooled and flushed after unlock; control's receipt writes are deadline-bounded and
   journaled.
4. Delete a runner pane: a new tab and pane are created, `runner_pane` is updated, and the worker is unchanged.
5. Send duplicate restart requests (concurrently, and repeatedly): one runner; every extra request is a no-op.
6. Set a pending deploy (mark set, live HEAD ahead of `deployed.json`): healing is not blocked, and control and the
   runner are restored from the pinned deployed tree, never from the checkout.

The same test file also covers:

- a wedged control, confirmed stopped before it is replaced;
- Herdr down: held, then the single notice;
- a breaker trip: cooldown, blocker, half-open retry, and reset;
- the watchdog's single-instance lock.

The full suite must stay green.

## 12. Out of scope

- Installing or enabling the live systemd unit, enabling lingering, or any live systemd/infra write. These need
  the owner's OK. Ship the unit file and the install script only.
- Restarting Herdr. Replacing workers.
- Control never blocking on the merge lock (handing a busy merge to the background queue), and deploy-self coalescing
  a refused redeploy. These are follow-ups from the handoff's late additions. The watchdog bounds their damage
  (wedge detection), but it does not fix them.
- Healers for the dash, MCP, tunnels, and autopilot runs. These are future registry entries.
