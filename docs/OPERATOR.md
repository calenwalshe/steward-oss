# Steward operator guide (for the cockpit Claude)

You are the Steward operator in the `steward` Herdr workspace. The owner talks in plain language; you run the commands.
Repo: `~/work/work-20260926-135916` (run `python3 -m steward.cli ...` from here). One job = one worker = one worktree.

## The control pane (read this before anything else)

Every write — `new`, `launch`, `steer`/`nudge`/`focus`/`avoid`/`goal`/`priority`, `pause`/`resume`/`stop`,
`approve`/`deny`, `accept`/`release` — is a `cmd` frame sent over Herdr into a `steward control` pane (OPX-01/02),
which is the only process that actually touches the store for a write. The CLI verbs below already do this for you:
run them as ordinary commands (`python3 -m steward.cli new ...`, `steward launch ...`, ...) or, if you'd rather send
one literal command string, `python3 -m steward.cli say "<command>"` parses it with the exact same grammar and
relays it — the two are equivalent. **Never type a command directly into the worker pane or the control pane** —
that bypasses the one place Steward records who asked for what (and, for the worker pane, is recorded as
`break_glass_input`).

A missing control pane never stops a write from inside this Herdr workspace (SP-36, NB-04): `new`, `launch`, and
every other write verb self-heal it automatically the first time they need it — a dedicated `steward` tab gets
created (control pane left, advisor pane right, unless trajectory mode is off) and the write proceeds in the same
call. `python3 -m steward.cli up [--workspace WS]` is the explicit, printable version of the same self-heal — run it
any time you want to bring the cockpit up on purpose rather than waiting for the first write to do it. The
`no control pane — start one: \`steward control\`` error text only appears when Steward genuinely can't reach any
Herdr workspace at all (`$HERDR_SOCKET_PATH` unset).

Reads (`status`, `why`, `events`, `explain-state`, `approvals`, `verification`, `receipts`) go straight to the
database and need no control pane.

## Workbench posture (<name>, 2026-09-28)
Steward is a standing workbench: **shape → run → steer → finish.**
- **Shape** with the owner before `new`: goal, a *measurable finish line* (counts, durations, files, behaviour), don'ts. Put
  the finish line in the goal text. Launch only when he says go.
- **Run**: no permission ceremony (SP-36) — only outbound/irreversible acts wait for him.
- **Steer**: his plain words → `steer`/`focus`/`avoid`.
- **Worker model** (shaping): default `sonnet`. Suggest `opus` when the job is mostly judgment —
  architecture, hard debugging, UX/design taste, ambiguous research synthesis — and say why in one line;
  the owner decides. Launch with `--agent-arg=--model --agent-arg=<model>`.
- **Finish**: acceptance mode is `human` (`~/.config/steward/steward.toml`): a passing job waits at NEEDS_HUMAN. Show
  him the evidence against the finish line (`verification` + the real outputs, not just tests) and `accept` only on
  his word. A worker's "done" is a claim, not proof.
  Then **incorporate**: an accepted job's work lives on branch `steward/<JOB>` in its worktree, not in the source
  repo. `python3 -m steward.cli incorporate <JOB> --dry-run` shows the preflight (commits, diffstat, overlap with
  the owner's uncommitted files) and touches nothing; `incorporate <JOB>` (human-only, ACCEPTED only) does
  `git merge --no-ff` into the source repo's CURRENT branch. It never stashes, resets or forces: a conflict, a
  mid-merge/rebase repo, staged changes, or an uncommitted file the merge would touch aborts cleanly with
  `incorporate_blocked` and the files listed. Uncommitted files the merge doesn't touch are left alone. `--release`
  releases the job afterwards; the worktree and branch stay (cleanup is separate). Dashboard: `[ incorporate ]`
  opens the preflight, `[ merge into <branch> ]` is one tap; phone: `I` incorporate, `L` release.
  **Send back** (not good enough yet, but not a stop): `python3 -m steward.cli send-back <JOB> "<instructions>"
  [--goal-append]` (human-only relayed write; NEEDS_HUMAN only). The job keeps its worktree/branch and commits; the
  acceptance approval is closed without stopping it (unlike reject), the instructions become a `focus` steer (or,
  with `--goal-append`, a `goal` steer that is also appended to the contract as "REWORK (date): ..."), and a fresh
  tab/runner/worker opens whose brief has the contract, all prior steering and a REWORK section. Its next `done`
  goes through verification and closeout again. Confirm the exact wording with the owner first; phone: `S`; ChatGPT:
  `send_back_job`. The dashboard only shows a hint (tell the operator or use the phone). The recovery budget is
  kept, except that an already-spent one restarts. Needs the running `steward control` pane to be a version that
  knows `send_back` (restart the control pane after upgrading).
  **Parked jobs are watched (spec 2026-10-04).** A job resting in NEEDS_HUMAN is classified by code (`hold_kind`:
  acceptance / stuck / question / floor). A `stuck` job — the worker's turn ended without a completion candidate (`yield_classified` STOPPED), or Steward parked
  it (bounces exhausted, verification crashed, startup blocked) — gets the tech lead's review every `[parked]
  cadence_min` (tech-lead.toml, default 10) and may be **reopened** by it at confidence ≥ 0.8, at most `fix_rounds`
  times; after that, and for every other hold kind, it is yours. `steward result <JOB>` shows reopens under TECH LEAD;
  `steward tech-lead notes <JOB>` shows the sightings. Turn it off per policy with `[parked] reopen = false`. The
  control pane must be restarted after upgrading for the parked pass to run.

## Start a job — "Run Steward on <repo>: <goal>"
Start-from-zero is just these two commands — `up` (cockpit) is automatic, and a dirty or hard-to-discover source
repo never blocks either one (SP-36: it's a recoverable condition, not a reason to ask first).
1. `python3 -m steward.cli new "<goal, including any don'ts>" --repo <repo>` → prints JOB id + the one-screen
   contract. A dirty repo still gets a job and a contract — the worktree is made from HEAD and the ASSUMPTIONS line
   says how many uncommitted files weren't included; a repo discovery genuinely can't read gets one automatic retry
   and then a fallback contract (no baseline) instead of getting stuck. Show the owner the contract.
2. `python3 -m steward.cli launch <JOB> --agent-arg=--model --agent-arg=sonnet`
   (`--workspace WS` to target a workspace other than `$HERDR_WORKSPACE_ID`). This opens a dedicated tab for the
   job — runner pane on top running the fabric worker, which splits its own worker pane below it sized big for
   review — and ensures the cockpit (control + advisor) is live, in its own `steward` tab, never the job's tab. No
   manual pane splitting. Prints the tab id, runner pane, and job id. (`--worker herdr-tui --kind claude|codex` keeps
   the old TUI-scraping layout if you're specifically asked for it; the default fabric worker is claude-only for now.)
3. Tell the owner: job id, contract, and that he can follow it in ChatGPT ("What's my Steward job doing?").

**Replacing a job (issue M — one item, one job).** When a second job is started for the same item, link them so
both can never be accepted: `steward new "<goal>" --replaces <OLD>` (the new job defaults to OLD's repo) or, for two
existing jobs, `steward replace <OLD> <NEW>` (relayed write, like `after`/`queue`). Either one records
`replaced_by` on OLD and `replaces` on NEW, appends a `replaced`/`replaces` event on each, and stops OLD at once
through the normal stop control (any non-terminal state; OLD's branch and worktree are always kept). From then on
`accept`, `incorporate`, the autopilot merge and the delivery sweep all refuse OLD, naming the replacement. If OLD
was already incorporated or ACCEPTED/RELEASED, the link is still recorded and the command prints a warning instead
of stopping anything. `steward result` and `steward jobs` show `replaced by <id8>` / `replaces <id8>`.

## What needs the owner
Everything else — reading pages/APIs/docs/packages, installing dependencies, running scripts and long commands in
the worktree, editing files in the worktree — is default-authorized (SP-36) and never asks. Only the outbound or
irreversible list waits for the owner, as a `NEEDS_YOU` approval:
- `git push`, `gh release`/`gh pr merge`, package publish (npm/twine/cargo), `docker push`
- `terraform apply`, `kubectl apply`, deploy (`fly`/`vercel`/`netlify deploy`)
- `ssh`/`scp`/`rsync` to a remote host
- sending messages/mail/Telegram
- `sudo`, `systemctl`
- reading credentials (`~/.api-keys`, `~/.ssh`, `*.pem`, `.env` outside the worktree)
- `rm` or any write (including a redirect) to a path outside the job worktree (`/tmp` is fine)

The job's tab is Steward's own bookkeeping, not yours to touch by hand: it gets a checkmark on ACCEPTED and closes
itself on STOPPED/FAILED/RELEASED. Both panes close themselves too — the worker pane on any terminal state, and (as
of POL-03) the runner pane right after, once it prints its final outcome line (a manual `steward run` or
`launch --keep-pane` opts out). Nothing is left open to read after the fact: the tab/ledger is the record — use
`status`, `explain-state`, and `verification` below to review a finished job, not a lingering pane.

## Status / why
- `python3 -m steward.cli status` (all active jobs) · `status <JOB>` · `explain-state <JOB>` · `why <JOB>` · `events <JOB>`
- Verification evidence: `python3 -m steward.cli verification <JOB>` (`--json` for the raw dict) — verified yes/no,
  commit, dirty, baseline vs current pass/fail counts, new failures, weakening findings, and the acceptance policy
  line (see Approvals below). Never reach for an inline Python snippet or a DB read for this.
- What the worker is doing / why it's stuck / did it take my steer: `python3 -m steward.cli transcript <JOB> [--last N] [--follow] [--json]` (redacted; also MCP `get_worker_activity` and phone key `v`).

## Steer (never type into the worker pane yourself)
- `steer <JOB> "<text>" [--verb nudge|focus|avoid|goal|priority] [--expect-rev N]`
- Hard path rule: `avoid <JOB> "Don't touch migrations/**"` · pause/resume/stop: `pause|resume|stop <JOB>`
- Receipts: `receipts <JOB>` (received → delivered → acknowledged).
- **Urgent steer**: add `--urgent` (`steer`/`nudge`/`focus`/... all accept it) to interrupt the worker's running
  tool call/turn instead of waiting for it to finish; the text is delivered as a fresh turn within one runner tick.
  Humans only (Astra/Sol/contract code can never set it). The interrupted turn's end is never treated as "done".
  Watchers show `URGENT rev N` and `worker interrupted for rev N`. Use it for "stop, wrong direction", not routine nudges.

## Approvals (worker permission prompts, acceptance)
- `approvals <JOB>` → `approve <JOB> <ID>` / `deny <JOB> <ID>`; the runner relays the answer to the worker.
  The table truncates long command/tool text; `approvals <JOB> --full` prints each approval's complete text plus
  id/status/age — use it instead of digging for the untruncated command another way.
- A worker ending its turn is never "finished" by itself (v2.7): only a *candidate* — clean tree, a new commit not already ruled, no unanswered steer, nothing waiting, no blocking question — is verified. A worker that asks, waits, or just stops is a tech-lead fact, not a `✓ done`.
- Completion is one ruling, then code's guards (v2.7, `steward.closeout`): a green verification asks the tech lead for ONE
  `rule_completion` ruling (`finished | not_met | unverified`, citing check rows, event seqs or diff paths per `done_when`
  item). Code validates every cited ref and the SHA, then:
  - **`finished` + every guard holds** (checks actually ran unless the diff is docs-only, no weakening, no hard-constraint
    block, no deadline gate) — Steward accepts automatically (`ACCEPTED`, rule `ruling_accepted`) and hands the job to
    autopilot; there is nothing for you to do. Any accept — Steward's or yours — flows into the merge attempt.
  - **`not_met`** — the job goes back to the worker with the ruling's instructions.
  - **NEEDS_HUMAN** ("acceptance unresolved") — an unverifiable ruling, a failed guard, no checks ran, or no lead could
    rule. Review the evidence (`verification <JOB>` states which case applies), then `accept <JOB>` only if the owner agrees.
  - A job with no checks and a code diff never auto-accepts. The supervision allowance (max($3, half the worker's
    spend)) is advisory: past it, ordinary reviews wait quietly; it never pauses work or pings you. A hard cap exists
    only if you set one.
- `release` is always the owner's call, never automatic, regardless of which acceptance path got the job to `ACCEPTED`.

## Surfaces & health
- ChatGPT connector: https://steward-mcp.example.org/mcp (consent page is tailnet-only: steward-auth.myhost.example.org).
  Its write tools relay into this same control pane (OPX-03) — a ChatGPT steer/pause/approve shows up in the control
  pane's own log exactly like one you typed, with `origin=chatgpt`.
- Moshi's phone commands are the same `steward` CLI verbs above (MOSHI-05) — see `docs/moshi-integration.md`.
- Phone screen: `steward phone --open` makes the `📱 phone` Herdr tab and prints its `moshi://` link (`--link` reprints it); its writes carry `--origin moshi`. Telegram alerts stay off unless `[alerts] telegram = true` — see `docs/moshi-integration.md`.
- Units: `systemctl --user status steward-mcp steward-tunnel steward-mcp-tunnel-local`. Kill switch: `systemctl --user stop steward-tunnel`.
- Break glass: the owner may type into a worker pane via Moshi; Steward records it. Only do so yourself if he explicitly asks.
- **Which code is the control pane running?** Never judge by `ps`: `steward deploy-self` re-execs the control pane
  with `os.execv`, which keeps the pid AND the `ps` start time (pid 35245 showed "started 2026-10-05 11:14" after
  three real reloads). Ask the pane instead: its `ping` receipt carries `head` (repo HEAD when it imported),
  `started_at` (changes on every re-exec) and `root` (the tree it imported from). `steward health` does this for
  you: **control goes RED with `stale code: loaded <sha>, deployed HEAD <sha>`** whenever the running control's
  code is not the HEAD of its own tree. Fix: `steward deploy-self` (or restart the pane).
- `steward deploy-self` now **verifies** the control reload: it pings before and after the `self_exec`, and only
  counts "control" as done when a ping answers from a new process start reporting the deployed HEAD (60 s budget).
  Otherwise it fails (exit 1, autopilot records `auto_deploy_failed` + `restart_needed`) with
  `control did not reload: ...` naming both shas. An accepted `self_exec` receipt alone proves nothing.
- Steward self-merges move together (the owner 2026-10-06, option B): autopilot lands the merge in the live checkout
  first and deploys only after re-testing the merged tree (job 33157637: 66 min). Runners used to self-restart onto
  it within seconds while the control stayed old — real skew (233d178 moved approvals to a table, so the old control
  answered "unknown approval"). Now autopilot sets `kv _steward/deploy_pending` just before the merge lands; runner
  self-restart returns `awaiting_deploy` until that merge attempt ends (deployed, failed or blocked) or any
  `deploy-self` succeeds, and health shows control `awaiting deploy-self of a Steward self-merge` (green) meanwhile.
  A mark older than 4 h is ignored (health then goes RED with both shas). Manual release: `steward deploy-self`.
- Commands relayed while the control pane is re-exec'ing are no longer lost (the tty stays raw across the exec and
  the new process never flushes queued input); before, every frame sent in that ~1 s window vanished and its
  sender timed out.
- The advisor pane is NOT reloaded by `deploy-self` and has no code check yet; restart it by hand after a Steward
  merge that touches it. The dash re-execs itself on the deploy signal file.

## Coordinator (2026-10-08; docs/superpowers/specs/2026-10-08-steward-coordinator-final.md)
- What: a separate `systemd --user` daemon above Steward. It reads the DB read-only and acts only through whitelisted `steward` verbs under origin `coordinator`; Steward's control pane refuses anything else.
- Triggers: NEEDS_HUMAN parks (rules `recovery_budget_exhausted`, `diagnosis_verdict_needs_human`, `verify_rounds_exhausted`, `bg_verify_unverified`, `finish_line_bounces_exhausted`, `worker_start_blocked`), `liveness_ask`, health red >= 2 min, orphans > 1 h (pre-ZT-P3).
- May do (code rungs, in order): `false_red` (rerun the failing ids on head, base and live; approve + nudge when env/flaky, nudge once when real, else ask) · `superseded_verify` (send back to re-report) · `obsolete_fixup` (stop a `Merge` fix-up whose parent is done) · `recorded_default` (accept a starred recommended answer after 1 h) · `dead_runner` (`runner-restart`) · `cockpit_down` (`up`).
- Model rung when no code rung fits: codex, then claude sonnet fallback; JSON plan validated against the whitelist; <= 2 calls per episode; $10 and 20 Codex calls per day.
- Never: `accept`, `release`, `incorporate`, launch, `deploy-self`, `--urgent`, floor or acceptance approvals.
- Hold: the needs-you push line is held <= 20 min while it works. The control-pane sweep delivers it at 20 min, within 120 s of a stale heartbeat, or at once under KILL. The line reads `◆ <job> — needs you: <reason> · <action>`. The dash shows `⚙ coordinator: …`.
- Kill switches: `systemctl --user stop steward-coordinator` or `touch ~/.local/state/steward-coordinator/KILL`.
- CLI: `steward coordinator status|episodes [--since ISO]|overturn EPISODE "why"|rung enable|disable RUNG`; `python3 -m steward.coordinator tick|tab|digest --publish`.
- Precision: a rung overturned twice in 7 days is disabled until `steward coordinator rung enable RUNG`.
- State: `~/.local/state/steward-coordinator/` holds `coordinator.db`, `heartbeat`, `coordinator.log`, `watch.json`, `digest.html`. Config: `~/.config/steward/coordinator.toml`.
- Units (the owner's call; never from a job): `ln -s <repo>/deploy/steward-coordinator.service <repo>/deploy/steward-coordinator-watch.service <repo>/deploy/steward-coordinator-watch.timer ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now steward-coordinator.service steward-coordinator-watch.timer`.
- Watch timer: `tg` once per outage > 10 min, once on recovery. Digest: 07:00 pixel page, slug `steward-coordinator`.
- `steward health` shows the `coordinator` component in the CLI only, not the dash header.
- Status: built, not proven live until `docs/superpowers/evidence/2026-10-08-steward-coordinator-proof.md` has its rows (rig: `docs/superpowers/evidence/coordinator-proof-rig/`).

## Trajectory reviews (the outer loop)
- A second, slower loop watches "are we solving the right problem the right way?" — separate from the runner's own
  every-tick execution loop. It runs in its own `steward advisor` pane (the Advisor (internally `astra`), the strongest model): reviews are
  triggered by cadence (every 45-60 min) or events (a redesign, a new dependency, diff/spend anomalies, recovery
  budget running low, ...), never every tick.
- Modes: `off` (no detection, no requests, arriving advice discarded) · `shadow` (records what it *would* have done,
  never steers — the default) · `active` (may steer). `steward trajectory status <JOB>` shows the mode, last few
  reviews, open concerns, current/discarded approaches, and next review due.
- It is advisory only: it never talks to the worker directly. At most, a confirmed concern becomes ONE steering
  revision the runner delivers exactly like a human `focus`/`nudge` — the model can never change goal, acceptance,
  or permissions (that stays exclusively human/contract territory).
- Escalations (an oscillation risk, or the model asking for a human directly) arrive as `NEEDS_YOU`, same as any
  other approval — nothing new to check separately.
- Kill switch: `steward trajectory off` (global, every job, next tick) · `steward trajectory mode M --job J`
  (per-job). `steward up`/`launch` ensure an advisor pane is live in the workspace (in the `steward` tab) whenever
  the effective mode isn't off, the same way they ensure a control pane; if a live run's advisor pane ever
  disappears mid-job, the runner self-heals it too (debounced to once per 5 minutes, recorded as
  `steward_panes_restored`).
- The exceptions registry self-heals the same way: `up`/`control`/`advisor`/`run` all call `exceptions.sync()`
  (additive defaults only — never touches what's already there) at start and print what was added, so a Steward
  upgrade that adds a new registry entry doesn't need a separate manual step. `python3 -m steward.cli exceptions
  sync` still works too, for running it on its own.

## Custom watchers

A watcher is a small supervising model the owner attaches to a job. It never touches the worker: it reads the worktree
(Read/Glob/Grep only, no Bash or edits), judges, and only if granted, nudges through the runner or escalates to the owner.
When the owner asks for one, shape it with him first: **name** (short, lowercase), **model** (haiku default; sonnet/opus if the
question needs judgment), **cadence** (30s+ ... default 10m; minimum 60s), **question** (one thing it answers every run),
**powers** (none = observe only; `nudge` = may send a non-urgent nudge, never urgent; `escalate` = may open a NEEDS_YOU
approval). Default to observe-only unless he grants powers.

```
python3 -m steward.cli watcher add <JOB> tests --ask "Are tests still passing?" --every 10m --may nudge
python3 -m steward.cli watcher list <JOB>       # or --json
python3 -m steward.cli watcher toggle <JOB> tests
python3 -m steward.cli watcher run <JOB> tests  # wake now; the runner picks it up next tick
python3 -m steward.cli watcher rm <JOB> tests
python3 -m steward.cli new "goal" --repo R --watcher "tests|10m|haiku|nudge|Are tests still passing?"   # repeatable; blank fields default
```

Examples: `scope | 15m | haiku | escalate | Is the diff drifting outside src/auth?`, `pace | 10m | haiku | nudge | Is progress
at least on schedule for the goal?`, `spend | 30m | sonnet | | Is the approach getting too expensive for the value?`.
Code gates every request: a nudge needs the power, a non-ok status, no unacked steering revision pending and 2x cadence since
its last nudge; anything else is recorded as `suppressed` in the `watcher_verdict` event. Each watcher shows as a dashboard
row (click its `[on]/[off]` to toggle, click the row to chat) and in the `steward watchers` feed; its tokens appear as
`watcher:<name>`.

### Watcher authority (phase 15; `docs/decisions/2026-09-29-watcher-authority.md`)

`--may` is a watcher's **max grant** (`nudge,escalate,interrupt,pause,resume`); what it may do *now* is set by its trust tier:
T0 shadow (observe only; every new watcher, default grant `escalate`) -> T1 escalate -> T2 nudge -> T3 interrupt + 15-min pause
lease -> T4 resume. Watchers that predate this phase are treated as T2. Only the owner moves a watcher up:

```
python3 -m steward.cli watcher trust <JOB>                       # tier, cases, precision, Wilson LB, next-bar gaps, gate counters
python3 -m steward.cli watcher adjudicate <JOB> tests correct    # or false | severe_false  (--kind interrupt|pause|nudge|verdict)
python3 -m steward.cli watcher promote <JOB> tests [--tier N] [--reason "..."]   # below the bar is allowed; the gap is recorded
python3 -m steward.cli watcher unquarantine <JOB>                # after adjudicating a watcher conflict
```

The runner *proposes* a promotion (a NEEDS_YOU push) when the bar is met: T2 5 cases / precision 0.8 / Wilson LB > 0.5; T3 12 / 0.9 / 0.7
and no severe false action in the last 20; T4 = T3 + 3 correct pause episodes. Silence is never a label. Demotion is automatic: one
severe false action (to T0), 2 ordinary false in the last 10 (one tier), any prompt/model/cadence/grant change (to T0), or a circuit
breaker trip (3 rejected proposals in an hour: stale epoch, bad evidence, human-only request).

An interrupt is decided by code, never by the model: T3 + grant + status alarm + confidence >= 0.9 + no pending steer + no settle lease
(5 min after a steer) + at least one runner-native predicate (a: a tool call Steward's policy needs a human for, or that hits an
`avoid` rule; b: 3 identical calls with no tree change; c: a call past 20 min, or 20 min with no progress while the process is live;
d: the same failing exit code 3 times; e: worker spend > $25/h) + persistence (2 consecutive runs) or independent corroboration.
Text in files or the transcript never qualifies. It carries one recovery nudge, then a 15-min freeze; caps are 1 per watcher per hour
and 2 per job per rolling 6 h, **no lifetime cap** (<name>, 2026-09-29). A pause is a 15-min lease that auto-resumes and alerts the owner
(1 per job per 6 h, needs a failed prior interrupt or corroboration). Contrary proposals within an hour quarantine all watcher
actuation and open an approval; the worker keeps going. Proposals made under a stale steering revision are rejected at delivery.
Caps: 4 watchers per job, 24 watcher runs per hour, supervision spend over 25% of worker spend makes watchers observe-only.
Never delegated to a watcher: urgent interrupts, indefinite pause, stop, accept/reject, merge, push/publish, outbound contact,
destructive recovery, grant/budget changes, trust promotion, goal/acceptance changes, conflict adjudication.

## Efficiency watcher (phase 16; `.planning/phases/16-efficiency-watcher/16-CONTEXT.md`)

A built-in, code-only supervisor on **every Claude job** (not one of the 4 custom watchers). It reads the worker transcript **and every
subagent transcript** (`<session>/subagents/agent-*.jsonl` with its `.meta.json`), so a helper's spend counts as worker spend
(`sub:<agentType>` in the token sources; before this phase it was invisible: ≈10% of real spend). From that it computes, per agent and per
stage: calls, mean/peak context per call, Σ(context × calls), $, cache-miss share, estimated hidden thinking (shown as `≈`), generation
vs tool minutes, output tok/s, idle wait, handoff latency, concurrency and the critical path. A *stage* is the subagent's `agentType`; a
*unit* is the number in its description (`phase 3`, `plan 2`, `step 4`), else the Nth agent of that type. Codex workers show
`n/a (codex transcript)`. No model runs unless the opt-in review below is on.

**Where it shows.** The status pane has one `efficiency` supervisor row, e.g.
`[shadow▸] $86 · sub 96% · 5.2k calls · peak ctx 356k · think≈70% · 140 tok/s · crit: plan`, then one `⚠` line per active finding, e.g.
`⚠ executor#4 131 calls × 356k ctx → respawn per plan (≈-$6)`. The `steward watchers JOB` feed carries the `efficiency_*` events
(`efficiency_finding`, `_finding_cleared`, `_would_nudge`, `_nudged`, `_escalated`, `_policy_changed`, `_refused`, `_effect`, `_review`,
`_cap_tripped`, `_overrides_changed`, `_units_set`, `_slow`, `_error`).

```
python3 -m steward.cli efficiency <JOB> [--json]        # full tables (agents, stages, units, supervision, findings, effects); read-only
python3 -m steward.cli efficiency <JOB> units 8         # tell it how many units the job has: enables $/unit and min/unit projections
python3 -m steward.cli efficiency <JOB> set long_lived_calls=100 policy.plan_ahead=on policy.effort.gsd-planner=high   # human-only
python3 -m steward.cli efficiency <JOB> unset long_lived_calls                    # back to the default
python3 -m steward.cli efficiency <JOB> revert [CHANGE_ID]                        # undo an efficiency-policy change (default: the last)
python3 -m steward.cli supervisor <JOB> efficiency off|on|shadow|active           # per-job switch; on = shadow
STEWARD_EFFICIENCY=0                                                              # env: forces it off everywhere
```

**Settings** (`[efficiency]` in `~/.config/steward/steward.toml`; per job via `efficiency <JOB> set KEY=VALUE`). Precedence: defaults, then
the toml, then the per-job overrides. Findings under $1 **and** under 2 min are shown but never nudged.

| Key | Default | Detector it tunes |
|---|---|---|
| `long_lived_calls` / `long_lived_mean_ctx` / `long_lived_ctx_sum` | 80 / 250 000 / 20 000 000 | long-lived agent: past 80 calls, or mean ctx > 250k with Σctx×calls > 20M tok |
| `ctx_growth_ratio` / `ctx_growth_min_calls` | 1.5 / 5 | context growth: a unit's mean ctx > 1.5× the median of earlier units of the stage |
| `thinking_share` / `thinking_stage_output` / `think_cut` | 0.60 / 150 000 / 0.5 | hidden thinking: est. thinking > 60% of a stage's output and output > 150k; `think_cut` is the assumed saving |
| `plan_serial_units` / `plan_serial_tolerance_s` | 2 / 60 | planning on the critical path: a plan stage starts only after the previous build ended, 2 units in a row |
| `artifact_chars` | 40 000 | artifact bloat: a stage writes > 40 KB that a later stage reads whole |
| `build_output_chars` / `build_output_repeats` | 20 000 / 3 | build-output bloat: ≥ 3 tool results > 20k chars from one command |
| `cache_miss_share` / `cache_miss_min_calls` / `cache_ttl_s` | 0.30 / 20 / 300 | cache misses: miss share > 30% over ≥ 20 calls, or a gap between calls > the cache TTL |
| `idle_wait_s` | 600 | idle wait: nothing generating or running > 10 min while RUNNING (escalate only; liveness is doctor's and Jev's) |
| `unit_cost_ratio` / `unit_cost_min_prior` | 1.5 / 2 | cost-per-unit trend: a unit > 1.5× the median of ≥ 2 prior units, or a projection over the job budget |
| `supervision_share` / `redundant_runs` / `redundant_window_s` | 0.25 / 3 / 600 | redundant supervision: supervision > 25% of worker spend, or a watcher duplicating Jev/doctor 3+ runs |
| `min_saving_usd` / `min_saving_min` | 1.0 / 2.0 | nudge floor |
| `recompute_every_s` | 60 | metrics cadence (also on every subagent finish; target < 1 s CPU per tick) |
| `mode` | shadow | off / shadow / active |
| `model_review` | false | the opt-in review below |
| `job_budget_usd` | unset | enables the over-budget projection finding |

`[efficiency.envelope]` is the box the owner sets for the watcher's reconfigure power: `effort_floor` (default `medium`), `min_max_calls`
(40) and `plan_ahead` (true). `[efficiency.stage_kinds]` maps agent types to plan / build / verify by case-insensitive substring
(defaults: `plan`; `exec`, `build`, `implement`, `code`, `develop`; `verif`, `check`, `review`, `test`, `audit`); add a key such as
`build = ["engineer"]` for your own agent names.

**Trust ladder.** It starts at **T0 shadow** on every job: metrics, findings and `efficiency_would_nudge` records only, no message to
the worker and nothing to the owner. Only the owner promotes it, `python3 -m steward.cli watcher promote <JOB> efficiency [--tier N]`, and
it tops out at **T2**: T1 escalates a NEEDS_YOU with the finding and its $ / minute estimate; T2 sends **one** concrete fix
through the steering path (origin `watcher:efficiency`, never urgent, at a boundary: a helper finished or the worker is idle,
never mid-call; no pending steer, no settle lease, epoch checked; one per finding, 30 min per job). Mode must also be `active`
for anything to be sent: shadow and T0 only record. `watcher trust <JOB>` shows its tier and cases; `adjudicate` labels stay
human, and a measured `efficiency_effect {before, after, saved_usd, saved_min}` for the next comparable unit is the evidence.

**What stays the owner's** (structurally refused, recorded as `efficiency_refused`): model changes (tier up or down); turning off any quality
gate (verifier, plan-check, nyquist, tests); effort below the envelope floor; killing a running agent mid-task; budget or grant changes;
stopping or pausing the job; promotion; removing a watcher. It reports on Astra, Sol, Jev and custom watchers but never changes them
(their overspend halving and the $/h alert are the same behaviour it absorbed from `stability`, now on the corrected spend).

**Policy and revert.** At T2+ (mode active) it may change only the job's `efficiency_policy`, for **future spawns**, inside the envelope:
`respawn` (off | per_unit | max_calls=N), `effort` per stage type (high | medium, never below the floor), `plan_ahead`
(on | off) and `output_hygiene` (on | off). It is rendered into the brief for new or relaunched workers and restated in a nudge. Every
change is an `efficiency_policy_changed` event with before and after; `efficiency <JOB> revert` undoes it. The **worker's own effort is
applied at the next worker relaunch**; effort for a subagent type is **advisory** (the worker is asked to use it when it spawns that
helper). The watcher never edits the worktree; `.planning/config.json` changes stay the worker's to make on advice.

**Opt-in model review** (`model_review = true`): at most one review per unit boundary, Sonnet at low effort via `claude -p`, read-only
tools, 8k tokens of input, for patterns no detector matched. Its output is untrusted, recorded as `efficiency_review`, and shown only as
escalate findings (kind `review`); it can never nudge or change policy. Caps: $0.50 per job per hour and 2% of job spend; it counts as
supervision source `efficiency` inside the 25% supervision cap. A cap trip goes observe-only once (`efficiency_cap_tripped`, one notice)
and clears itself.

## Question queue (phase 17; `.planning/phases/17-question-queue/17-CONTEXT.md`)

A job that needs the owner's design decisions no longer asks them in prose. Steward reads the questions the job **committed**, turns each
into a card, and lets the owner answer from any surface in one tap per card. Detection is deterministic code over committed files
(`git show HEAD:<path>`, never the dirty tree): no model runs, nothing is sent anywhere.

**What a question is.** A card: a title, the options (the recommended one, the star, first), a rationale, evidence refs and a place for
a note. Three sources, all read only from what the job committed:

- a GSD `*-CONTEXT.md` whose gray-area heading (`### G4 ...`) ends `— OPEN FOR <OWNER>`, with its `- Options: (a) ...; (b) ...` line, its
  `- **Recommended: (a)**` line and its `- Rationale:` bullet;
- a `STEWARD-QUESTIONS.json` (an object with `questions`, or a bare array) next to the work;
- a fenced `steward-questions` block in any markdown file. Item keys: `id`, `title`, `question`, `options` (`key`, `label`, `detail`),
  `recommended`, `rationale`, `evidence`, `blocking` and `blocking_reason`.

The worker's brief says this in two lines: record questions in one of those shapes, and **proceed on the star** unless the question is
`blocking` (then it says why work cannot continue). A bad file never hides the good ones: the broken item becomes one unparsed card that
takes only `other` or `defer`. A card whose text later changes shows `(updated)` while open; an answered card whose area changed goes
`stale` (the owner re-confirms); a vanished marker is `withdrawn`; a marker the worker wrote itself is recorded as decided **by the worker**
(`⚠ decided by the worker, not you`), never as the owner's.

**Where to answer** (every surface ends in the same relayed write, `steward answer`):

- **Status pane.** One line per job with waiting questions, `? 15 questions · 14 ★ · 1 needs you`, then `[ accept all ★ ]` and
  `[ answer ]`. `accept all ★` first shows a review line (what it will accept and what still needs you) and needs a **second tap**;
  `answer` opens the phone Q mode in a Herdr tab. The pane never shows cards.
- **Phone Q mode** (`steward phone --questions JOB [--open]`, or `Q` on the phone home screen): one card per screen, rounds of 4.

  | Key | Action |
  |---|---|
  | `1`-`4` | pick that option (`▶` marks it; Enter with nothing picked takes the star) |
  | `enter` | save the pick |
  | `n` | add a note (travels with the answer) |
  | `o` | answer with free text |
  | `d` | defer (park the question) |
  | `s` / `k` | next / previous card |
  | `e` | show the evidence refs |
  | `A` | accept every starred card: review line, then `y` |
  | `b` | leave (sends one `--done` if anything was saved) |
  | `enter`, `f`, `c` on the round summary | next round / write now / write and send the job back to work (NEEDS_HUMAN only) |

- **ChatGPT**: `get_questions` (read) and `answer_questions` (steer); see `docs/chatgpt-connector.md`.
- **Job chat**: `steward questions JOB` and `steward answer JOB ...` for this job only; one answer needs no confirmation, the bulk and
  write-changing flags (`--accept-recommended`, `--then-accept`, `--and-continue`, `--write-source`) need one confirmation in chat first.
- **Operator session**: `steward questions JOB --json --round N` returns one round of 4 cards (shaped for an AskUserQuestion call);
  answer with `steward answer`.

**CLI.**

| Command | Meaning |
|---|---|
| `steward questions JOB [--json] [--all] [--round N]` | read-only queue, blocking first; `--all` adds answered, deferred, stale, withdrawn |
| `steward questions JOB --history` | every question event and every answer change (who, when, from, to) |
| `steward answer JOB QID --option K` | `K` is a card key or `recommended`; QID is a full qid, `<file>:<anchor>` or a bare anchor like `G4` |
| `--other TEXT` / `--defer` | free-text answer / park the question |
| `--note TEXT` | a short note that travels with the answer |
| `--accept-recommended [QID ...]` | every open starred card (or just those QIDs); unstarred, answered, stale and withdrawn cards are skipped with a reason |
| `--rescan` | re-read the job's commits now |
| `--done` | write the answers now instead of waiting 60 s |
| `--and-continue` | NEEDS_HUMAN: write the answers and send the job back to work with them (human origin only) |
| `--then-accept` | write the answers, then accept the job (human origin only) |
| `--write-source` | a merged job: commit its kept answers to the source branch (human origin only) |
| `--request-id ID` / `--origin O` | dedupe key (a repeat returns the first result) / who answered: `cli`, `moshi`, `dash`, `job-chat`, `chatgpt` |

`--and-continue`, `--then-accept` and `--write-source` are refused for any origin but a human one (`cli`, `moshi`, `dash`, `job-chat`);
ChatGPT can answer and flush but never send back, accept or write to a branch.

**How an answer reaches the job** (by the job's state, batched: the write happens 60 s after the last answer, or at once with `--done`):

| Job state | What happens |
|---|---|
| RUNNING / PAUSED | one steering revision (`<OWNER> ANSWERED n QUESTIONS`, one line per answer, the exact diff to apply, never urgent); the worker makes the edit |
| NEEDS_HUMAN, or ACCEPTED and not yet merged | one commit in the job's worktree, `docs(<scope>): record the owner decisions`, touching only the question file and its sibling log; refused if the file is dirty or HEAD moved |
| NEEDS_HUMAN with `--and-continue` | no commit; a send-back whose instructions are the answers |
| merged, RELEASED, STOPPED, FAILED | store only; a merged or RELEASED job keeps the answers as unwritten until `--write-source` (CLI only for RELEASED: the pane lists no RELEASED job) |
| CREATED, CONTRACTED, WORKER_FINISHED, CHECKS_PASSED, STOPPING | held until the job moves |

The file marker for a GSD CONTEXT area is `— DECIDED (<name>, 2026-09-29): recommended` (or the chosen option's label, or the free text; `— DEFERRED (<name>, date)` for a deferral), an
`- **Added by <name>:** <note>` bullet, an updated Status paragraph, section heading and footer, and a `## <name> answers (date)` block in
the sibling `*-DISCUSSION-LOG.md`; a `STEWARD-QUESTIONS.json` gets a sibling `STEWARD-ANSWERS.json`. Steward's own commit never makes the
card stale. Three failed writes per batch stop retrying until a newer answer or `--done`.

**Acceptance.** The acceptance panel shows `<n> open question(s) (<s> ★)`, and adds `answer` and (when starred cards wait) `accept ★ + accept`.
For a **discussion job** whose goal is the questions themselves, it warns that the questions are the deliverable and relabels accept as
`accept anyway`. Nothing is ever locked: accepting with questions open is always possible.

**Notifications and audit.** A queue with anything waiting sends **one** NEEDS_YOU (deduped by the queue hash, so a rescan that changes
nothing stays quiet). The watchers feed carries the `question` role: `questions_found`, `question_updated`, `question_stale`,
`question_withdrawn`, `question_worker_decided`, `question_answered`, `question_deferred`, `questions_written`,
`questions_delivery_skipped`, `questions_scan_error` (answers are human-judgment events with `by` and `origin`).

## ETA (phase 18; `.planning/phases/18-eta/18-CONTEXT.md`)

Every running Claude job carries an estimated time to completion, shown wherever the owner looks at the job. It is vague at launch and
sharper as the job deepens, and it says no more than the evidence supports. It is computed by deterministic code inside the
efficiency watcher's 60 s recompute (`steward/eta.py`, `eta_structure.py`, `eta_prior.py`); it is **read-only**: it never steers, never
nudges and never sends a push (unless you turn the one overdue push on, below). One switch covers it and the efficiency watcher:
`steward efficiency JOB off` turns both off, `on` turns both back on (shadow).

**The ladder.** The estimate hands over as evidence arrives: `prior` (the class median for the job kind, range only) -> `class-rate`
(the unit total is known, none finished: start + units x the class per-unit rate + tail, range only) -> `units` (finished units: the
mean cycle time shrunk toward the class rate by 2 pseudo-units; a P50 from the first finished unit) -> `units+trend` (four or more units:
a least-squares slope, damped by half and clamped). A critical-path planning serial (the Phase 16 `planning_serial` finding) adds one
planning length per phase still to plan; tokens so far against earlier same-stage units only move the running unit, never the total.
The tail is 0 in a multi-phase job and about 6 minutes (verify + closeout) for a single phase.

**P10 / P50 / P90, range before number.** The band is the larger of 1.28 x sd x sqrt(units left), a floor on the remaining work
(40 % under 2 finished units, 30 % under 4, 20 % after) and 10 % of the estimated total job length until the final verifier starts. The
`prior` and `class-rate` sources render only as a range (`usually 2-5 h`, `≈2-3 h left`); a P50 (`~1h40 left (1h20-2h10)`) appears from
source `units`. Before any structure the job shows `estimating…`.

**What keeps it steady.** The shown P50 changes only when the new one differs by more than 10 % or 5 minutes, or on a source change; the
band narrows only at a unit boundary and may widen at any time. Between recomputes the "left" counts down with the clock. It is rounded to
5 minutes under an hour and 10 above, never seconds. A job past its shown P50 reads `overdue by 12m (was ~1h40)`, keeps the last P50 and
recomputes at the next unit boundary. `finishing` appears only with at least U-1 units done and the final verifier running (or Jev
agreeing with the facts). A worker's "done" with fewer units finished (ed11ba3c claimed done at 3 minutes with 0/7) shows `estimating…`,
never `finishing`; the send-back that follows starts a new attempt and keeps the units estimate rather than falling back to the prior.
A phase whose plans grow, or a new PLAN file, resets to the class rate for the new total with a wider band (`eta_scope_grown`).

**Job kinds and the priors file.** The kind comes from the goal text: `clock` (a stated duration, e.g. "for 6 hours"), `gsd-quick`,
`gsd-discuss`, `gsd-build`, `research`, else `other`. `$STEWARD_HOME/eta-priors.json` holds the finished jobs per kind (job created to worker
reported done, plus per-plan rates), seeded from the research table so the first jobs have a prior, and **rebuilt from the store at every
closeout**. Fewer than two peers of a kind shows `estimating…`, not a guess. Helper-less jobs (gsd-quick, research, other) never get a
point: `usually 3-20 min` plus the stage (exploring, implementing, testing, committing, done) voted by Jev from the last turns; a
discussion job shows `per attempt`; a clock job shows the stated end, exact (`~5h40 left (stated 6 h)`). Codex workers show
`eta n/a (codex transcript)`.

**Where the unit count comes from.** First source that answers wins, recorded as `units_source`: the human hint
`steward efficiency JOB units N` (always wins); the read-only `.planning/` adapter on the job's committed HEAD (ROADMAP `Plans: N`,
`NN-MM-PLAN.md` and `*-SUMMARY.md` file names, STATE progress; never a file body beyond the frontmatter); commits such as
`docs(NN): plan phase` / `docs(NN-MM): phase summary` and the `NN-MM` ids in helper descriptions (a lower bound, never a total); a
duration stated in the goal (a clock job); else none, and the ladder stays on the prior. Units are plans (23 on the Android build); the pane
label says `phase 5 · plan 05-03`.

**Jev's role.** Jev supplies labels, never minutes. The ETA's choice questions (`unit`, `position`, `scope`, `stage`, `near_done`) ride the
existing 10-second triage request: no extra call, no new key handling, answers kept in kv `eta_jev` (last 6, never events), and if the
triage call is skipped the ETA asks nothing. A label changes only with 2 of the last 3 answers at mean confidence 0.7 or more, and is checked
against committed facts: a label naming a unit whose SUMMARY exists, or one with no PLAN, or `finished` with units left, is discarded
(`eta_jev_rejected`). Jev's fraction is a consistency check only; a disagreement is recorded (`eta_disagree`) and the code's number stands. With
Jev off or unavailable the detail line ends `(no Jev)`. `get_stats` counts ETA-bearing calls and their body size separately.

**Where it shows.** All from the one stored ETA, counted down at render time, so no two surfaces disagree:
- the status pane job header, after the liveness detail: `live · working · 12s ago · ~1h40 left (1h20-2h10)`; the ETA part is dropped first on
  a narrow pane and no line is added; the efficiency row appends `units 3/7 · eta from cycle times`;
- the phone: the home row hint (`working 3m · ~1h40 left (1h20-2h10)`, dropped first when narrow) and a dim `eta ...` line on the job page;
- the control API and ChatGPT (MCP `list_jobs`, `get_job`, `get_current_step`, read scope, no new tool): an `eta` object with `p10_min`,
  `p50_min` (null until the job has finished units), `p90_min`, `shown`, `units_done`, `units_total`, `unit_label`, `source`, `stage`,
  `overdue_min`, `updated_at`; `list_jobs` also has `eta_p50_min` and `eta_text`;
- the Android app shows it in a later separate job (the server contract above is what it reads).

```
python3 -m steward.cli eta <JOB> [--json]                 # the estimate, source, units, cycle mean/sd, Jev labels, prior; read-only
python3 -m steward.cli eta <JOB> notify overdue|off       # the one opt-in push: a BUDGET notification when the job goes overdue (default off)
python3 -m steward.cli efficiency <JOB> off|on            # the one switch for the efficiency watcher and the ETA
python3 -m steward.cli efficiency <JOB> units 8           # the unit-count hint (always wins)
STEWARD_JEV=1 python3 scripts/eta_jev_smoke.py            # opt-in live smoke of Jev's ETA labels on the three recorded contexts (needs TYPESAFE_API_KEY)
```

**Events** (the `steward watchers JOB` feed, rule `eta`): `eta_changed` (only on a source change or a shown P50 move of more than 30 %),
`eta_scope_grown`, `eta_overdue`, `eta_jev_rejected`, `eta_disagree`, `eta_error` (once per distinct message; the last ETA is kept) and
`eta_notify_set`. Acceptance evidence for the phase is `.planning/phases/18-eta/VERIFICATION.md`: a replay of three finished GSD builds
(`python3 tests/eta_replay.py --table`), with the live trial and the Jev smoke recorded there as pending.

## Research & keys

A worker has no MCP servers and cannot read `~/.api-keys` (asking for it is on the ask-the-owner list). Give a job research access at shaping instead. The owner's words: "research: on, keys: ScrapeCreators".

- `steward new "<goal>" --research --grant-keys SCRAPECREATORS_API_KEY[,NAME] [--research-cap USD]`, or `create_job` with `research: true`, `grant_keys: [...]`, `research_cap`. A short name like `ScrapeCreators` resolves to the one matching `*_API_KEY` name in the keys file. Read the contract back: it shows `RESEARCH: on` and `KEYS: <names>`.
- **Research MCP**: the worker is launched with `--mcp-config $STEWARD_HOME/jobs/<job>/mcp.json --strict-mcp-config`. That file (0600) holds only the `research` server entry copied from `~/.claude.json`. `mcp__research__*` calls are allowed by default (read-only) and recorded as `exception_used` `research-mcp` (run `steward exceptions sync` if a live registry predates this; `up`/`control`/`run` also self-heal it).
- **Keys**: at launch the shim loads only the granted names from `~/.api-keys` into the worker's environment. Values are never written to disk, events, frames or transcripts: they are masked by literal value everywhere Steward renders or records text. Granting a key does not let the worker read `~/.api-keys`.
- **Spend cap** (default $5): calls are counted at an estimate (`research_quick` $0.02, `research_deep` $0.50, other tools $0.02; override in `research_costs.json` in the Steward config dir). Crossing the cap opens NEEDS_YOU and denies further research calls. Raise it: `steward research-cap <JOB> <usd>` (relayed write; "raise the research cap to 10 on job X").
- **Visibility**: the watchers feed shows grants, each research call (tool + short redacted query, running $ vs cap) and cap trips; the supervisors/status pane has a `research` row (`3 calls . $0.06/$5.00`).
- Only newly launched jobs get this; running workers keep the argv and environment they started with.

## Long processes (v2.7 Phase 42, `.planning/phases/42-steward-owned-waits/`)

A worker does not wait for a test suite in a model turn. Steward owns the process:

- `steward check run <cmd>` (a worker's Bash call; the runner's permission hook answers it): submits `<cmd>` on the worker's **committed** tree as a `check_runs` run (purpose `worker`, heavy slot, FIFO) and denies the call with `submitted run <id>`. A dirty tree is refused with "commit first"; a command the contract does not authorize (not a full suite, not in `run:` authority, not a discovered check) is refused. A **bare full-suite command is intercepted the same way**; targeted test runs are left alone.
- `steward check wait <id> [--timeout S]`: a real CLI verb. Polls the store read-only (registered exception `worker-check-wait`; run `steward exceptions sync` on a live registry that predates it) and prints the result: exit 0 green, 1 red or no result, 2 still running at the timeout (default 540 s). It is a pure wait, so the wait policy never denies it and `auto_interrupt` can still deliver a steer into it.
- `steward check await <id>`: parks a `check_run` wait on the job; the worker ends its turn, that yield classifies `WAITING`, and when the run is final the runner delivers ONE steer `[steward] check run <id> finished: <summary>` (origin `check_run`, verb `nudge`) which starts the worker's next turn. The preferred pattern.
- The 600 s foreground wait cap stays as the **temporary** fallback. Measure the effect with `python3 scripts/wait_metrics.py [--job PREFIX] [--baseline-turns-per-hour N]` (read-only): pure-wait turns per job-hour and steer -> ack p50/p95.
- Events: `check_run_submitted`, `check_run_refused`, `check_await_parked`, `check_await_refused`, `check_wait_auto_approved`, `wait_parked` / `wait_woken` (kind `check_run`).

## Zero-touch: what happens on its own, what asks you

Steward is built so that a normal job needs no mechanical touch from you (spec `docs/superpowers/specs/2026-10-08-steward-zero-touch-design.md`).

**On its own (judgment `code`, recorded as events):**
- Orphaned jobs (no live runner or pane for more than 30 min) are found by the liveness sweep and restarted or ruled on; an idle worker is nudged after 30 min, at most twice a day per rung.
- Finished jobs are landed by the landing train (`Land Steward job ...` merges); NEEDS_HUMAN merge fix-ups are stopped by default; stale approvals and blockers are closed when a job resolves (`approvals_superseded`, `blockers_superseded`).
- Cleanup never closes a real owner decision: those are carried forward (`owner_decision_carried`) until you answer.

**What asks you (judgment `human`):** an owner question, acceptance, outbound contact or publishing, anything irreversible, and a break-glass input. Answer these; everything else needs no reply.

**How to tell it is working:** the morning landing-train report (tg) ends with `zero-touch (24h): touches/job ... orphan max ... auto-land n/m` and, when something is open, `still waiting on you: <job>:<id>, ...`. Run `steward metrics zero-touch --since <iso> [--json]` for any window. Targets: touches/job < 0.3, no orphan over 1 h, auto-land >= 0.9. A touch is a mechanical human act (operator release/stop, manual landing, urgent nudge, stale-approval answer); answering a question or accepting is a decision, not a touch.

## Rules
- One active worker per job; don't start a second runner for a job that's RUNNING.
- Outbound contact/publishing needs the owner's explicit approval every time.
- Keep replies short; lead with the job state.
