# Steward

Steward is a durable, steerable job runner for coding agents that live in
Herdr panes. You give it a goal and a repo; it runs one
worker per job in an isolated git worktree, has a **tech lead** review the job
at each unit pause, checks the result against a recorded baseline, and
auto-merges when the checks pass. Everything it does is an append-only event
with its evidence, so you can steer, pause, or audit any job at any time.

```
You ── goal / redirect / pause / stop / inspect
 ▼
Steward controller (ordinary code, SQLite state)
 ├─ owns instructions, permissions, budgets, retries, recovery
 ├─ records events + steering receipts (judgment tagged human|model|code)
 └─ one worker ─► isolated git worktree ─► tech-lead review ─► checks ─► merge
```

The long-running thing is the job and its evidence, not an agent's
interpretation of the job. Herdr hosts the worker's terminal; it is for
observation and operator access, never a completion authority.

## Requirements

- Linux or macOS
- Python 3.12+ (standard library only at runtime)
- git
- Node.js >= 18 (for the npm launcher)
- Herdr
- Claude Code, logged in

Optional: Codex CLI (extra worker runtime), Jev (an observer; needs a
TypeSafe API key), Telegram alerts, a Cloudflare tunnel (public connector),
Android (phone surface). `steward doctor` checks all of this and creates
nothing.

## Quickstart

Build the package and install it (nothing is published to any registry):

```console
$ python3 packaging/npm/steward_pack_build.py --pack     # audits, then writes dist/<name>-<version>.tgz
$ npm i -g dist/<name>-<version>.tgz
```

Then, inside a Herdr pane:

```console
$ steward init            # shows the plan, then creates the steward and cockpit tabs
```

In the cockpit operator pane just say what you want:

> Run Steward on /path/to/repo: make reconnect reliable, don't touch the db

or do it by hand:

```console
$ steward new "Make reconnect reliable. Don't touch the db." --repo /path/to/repo
$ steward launch <job>
$ steward status          # every active job
$ steward result <job>    # one readable report: evidence, output, next commands
```

`steward doctor` re-checks prerequisites at any time; `steward uninstall`
undoes what `init` and `enable` set up (your config, state and job work stay).

## Configuration

Config lives in `~/.config/steward/`:

| File | Controls |
|---|---|
| `steward.toml` | acceptance mode, owner name, alerts |
| `roster.toml` | which runtimes/models act as workers and tech lead |
| `trajectory.toml` | trajectory-supervision cadence and budgets |

Steward never overwrites these once written; when a new version ships a
changed template, the new text lands beside yours as `<file>.new`.

Defaults: `acceptance = "auto"` (local acceptance is pre-authorized when the
checks pass), a Claude-only roster, and a tech-lead review at every unit
pause. Optional components are all off until you turn them on:

```console
$ steward components          # list them, each on or off, with the reason
$ steward enable <name>       # mcp, tunnel, openai-tunnel, moshi, telegram, android, jev, herdr-plugin
$ steward disable <name>      # undoes exactly what enable did
```

## Roles

- **Worker** - the coding agent that does the job, alone in the job's
  worktree (single-writer lease).
- **Tech lead** - reviews the job at each unit pause: unblocks, sends back
  with the first failing test named, or escalates to you. A hard-coded floor
  (deploys, destructive acts, anything outside the contract) always goes to a human.
- **Approver** - planned: resolves acceptance holds that still need a human today.
- **Landing train** - merges accepted jobs continuously, one at a time, so
  you don't merge or fix up by hand.

Authority is split on purpose: humans own the goal, boundaries and release;
models interpret and implement; Steward's code enforces lifecycle, budgets
and closeout. No code path reaches `RELEASED` without `judgment=human`
(`steward/machine.py`).

## Job lifecycle

```
CREATED → CONTRACTED → RUNNING ⇄ PAUSED → STOPPING → STOPPED
                          │
                          ├──► WORKER_FINISHED ──► CHECKS_PASSED ──► ACCEPTED ──► RELEASED (human only)
                          └──► NEEDS_HUMAN ──► ACCEPTED (human only) / RUNNING / STOPPED
```

`CHECKS_PASSED` separates new failures from pre-existing baseline failures.
A new failure, a dirty worktree, a hard-constraint violation, or detected
test-weakening (deleted tests, new skips, edited CI config) sends the job to
`NEEDS_HUMAN` rather than an invented `ACCEPTED`.

## Steering

Natural-language steering compiles into durable, revisioned instructions with
receipts (`received → delivered → acknowledged`):

```console
$ steward goal <job> "..."          # intent; needs a new contract revision
$ steward avoid <job> "migrations/**"   # hard: a diff touching it blocks closeout
$ steward focus <job> "retry-loop tests first"
$ steward nudge | priority | pause | resume | stop
$ steward why <job>                 # last event's evidence, rule, judgment
$ steward events <job> --json
$ steward incorporate <job>         # land an accepted job
```

`pause`, `resume` and `stop` are idempotent. `accept` and `release` are
human-only.

## More

- [docs/FLOW.md](docs/FLOW.md) - the job flow end to end
- [docs/OPERATOR.md](docs/OPERATOR.md) - operating Steward day to day
- [docs/supervision.md](docs/supervision.md) - the tech lead, floor and policy
- [CONTRIBUTING.md](CONTRIBUTING.md) - fork workflow, tests
- [KNOWN-ISSUES.md](KNOWN-ISSUES.md) - rough edges in this release
- [CHANGELOG.md](CHANGELOG.md)

## Development

```console
$ python -m pytest -n 8 -q       # needs pytest-xdist
```

## License

MIT. See [LICENSE](LICENSE).
