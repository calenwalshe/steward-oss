# Steward

One durable, steerable job runner for coding agents that live in [Herdr](https://herdr.dev) panes. You give it a
goal, it runs one coding worker per task workspace, keeps the evidence, and tells you what needs you.

This package is a small Node launcher around Steward's Python source (standard library only, no Python
dependencies). Installing it changes nothing on your machine.

## Requirements

- Node 18 or newer (to run the launcher)
- Python 3.12 or newer, or [uv](https://docs.astral.sh/uv/) (the first run offers `uv python install 3.12` on one keypress)
- Herdr 0.9.1 or newer
- git
- the `claude` CLI, logged in

## Install

This package is distributed as a local tarball only. It is not published to any registry.

```
npm i -g ./steward-<version>.tgz
```

Remove it again with `npm rm -g <package name>` (`steward uninstall` undoes what the first run set up and tells you
the exact command).

## First run

Run `steward` inside a Herdr pane. It shows one screen with what is ready and what is missing, the exact plan it
will carry out, and four keys. Press Enter and everything is set up; `q` leaves nothing behind. Outside a terminal,
`steward init --yes` does the same without the screen.

## Commands

| Command | What it does |
| --- | --- |
| `steward` | first run: the intro; afterwards, help |
| `steward init` | set up (the same plan as the intro; `--yes` skips the screen, `--dry-run` only prints it) |
| `steward intro` | open the first-run screen again |
| `steward doctor` | check the prerequisites (read-only) |
| `steward up` | bring the Steward panes up, and apply an upgrade after you install a newer tarball |
| `steward cockpit` | the cockpit panes (control, status panel) in the current Herdr workspace |
| `steward dash` | the live status panel |
| `steward enable <component>` | turn an optional component on (it shows what it will do first) |
| `steward disable <component>` | turn it off again |
| `steward components` | list the optional components and whether each is on |
| `steward uninstall` | undo what this install set up (jobs and worktrees are never deleted) |
| `steward --version` | version, install root and Python |

Optional components are all off by default: a local MCP server, a public connector with your own tunnel, a phone
tab, Telegram alerts, an Android token, the Jev advisor key and a Herdr plugin link.

## Upgrade

Install the newer tarball over the old one. The next `steward` or `steward up` migrates your config, restarts the
panes and shows what is new. There is no update check and nothing is sent anywhere.

## License

MIT. See LICENSE in the repository.
