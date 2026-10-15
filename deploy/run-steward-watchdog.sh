#!/bin/sh
# ExecStart wrapper for steward-watchdog.service (issue W). POSIX sh / dash-safe.
#
# Deliberately sources NO secrets (never ~/.api-keys): the watchdog only talks to Herdr, the Steward store and `tg`
# (which reads its own config). A systemd unit inherits almost no environment, so everything is made explicit here:
# absolute interpreter, repo root, STEWARD_HOME, the Herdr socket, and a PATH that finds `herdr`, `tg` and `git`.
#
# Overrides (optional, `systemctl --user edit steward-watchdog`):
#   STEWARD_PYTHON      interpreter (default: python3 on PATH, resolved to an absolute path)
#   STEWARD_HOME        store directory (default: ~/.local/state/steward)
#   HERDR_SOCKET_PATH   Herdr API socket (default: ~/.config/herdr/herdr.sock)
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$REPO_DIR"

PATH="$HOME/bin:$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin${PATH:+:$PATH}"
export PATH
PYTHON=${STEWARD_PYTHON:-$(command -v python3)}
STEWARD_HOME=${STEWARD_HOME:-$HOME/.local/state/steward}
HERDR_SOCKET_PATH=${HERDR_SOCKET_PATH:-$HOME/.config/herdr/herdr.sock}
export STEWARD_HOME HERDR_SOCKET_PATH

exec "$PYTHON" -m steward.cli watchdog "$@"
