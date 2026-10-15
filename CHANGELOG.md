# Changelog

## 0.1.0 (2026-10-14)

First public release.

- **Job runner**: one worker per job in an isolated git worktree, single-writer
  lease, SQLite state, append-only events with evidence/rule/judgment.
- **Contracts**: a working contract (goal, boundaries, done-when, authorized
  checks) compiled at `steward new`, with a recorded baseline so pre-existing
  failures never block a job.
- **Steering**: durable, revisioned goal/avoid/focus/nudge/priority with
  receipts; idempotent pause/resume/stop.
- **Tech-lead review** at unit pauses (now the default per unit): unblock,
  send back naming the first failing test, take over, or escalate, under a
  hard floor; per-job notebook and cross-job playbook.
- **Verification**: checks re-run at the worker's exact commit, test-weakening
  detection, background candidate verification.
- **Landing train / autopilot merge**: accepted jobs merge continuously;
  `acceptance = "auto"` is the default.
- **Roster**: Claude-only by default; Codex and other runtimes opt in, with
  per-lane runtime assignment for parallel fleets.
- **Optional components** (all off by default): MCP server and public
  connector, tunnels, Moshi, Telegram, Android, Jev observer, Herdr plugin;
  `steward components` / `enable` / `disable`.
- **Cockpit and dash**: `steward init` scaffolds the steward and cockpit tabs;
  a live status panel; `steward doctor` and `steward uninstall`.
- **Clean-install fixes**: config migration race, root-level pytest discovery,
  first-run flow.
