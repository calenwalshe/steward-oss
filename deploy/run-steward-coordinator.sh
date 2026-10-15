#!/bin/sh
# ExecStart wrapper for steward-coordinator.service and steward-coordinator-watch.service. POSIX sh / dash-safe.
#
# Sources NO secrets: Codex and Claude run on their own logins (`env -u ANTHROPIC_API_KEY`),
# `pixel` and `tg` read their own config, `tasks` is local. A systemd unit inherits almost no environment, so
# everything is made explicit here: absolute interpreter, repo root, STEWARD_HOME, the Herdr socket and a PATH that
# finds `tg`, `pixel`, `tasks`, `herdr`, `git`, `codex` and `claude`.
#
# Usage: run-steward-coordinator.sh [run|watch|...]   (default: run)
# Overrides (optional, `systemctl --user edit steward-coordinator`):
#   STEWARD_PYTHON      interpreter (default: ~/claude-stack-env/bin/python3, else python3 on PATH)
#   STEWARD_HOME        store directory (default: ~/.local/state/steward)
#   HERDR_SOCKET_PATH   Herdr API socket (default: ~/.config/herdr/herdr.sock)
#   STEWARD_NODE_BIN    directory holding the real `codex`/`claude` binaries, prepended to PATH (default: the newest
#                       existing ~/.nvm/versions/node/*/bin; skipped when none exists). ~/bin/codex and ~/bin/claude
#                       are wrappers that need it: without it every model call would exit 127.
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$REPO_DIR"

PATH="$HOME/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin${PATH:+:$PATH}"
if [ -z "${STEWARD_NODE_BIN:-}" ]; then
    # newest nvm node by numeric version (v9 < v24); an unmatched glob stays literal and fails the -d test
    found=""
    for d in "$HOME"/.nvm/versions/node/v*/bin; do
        [ -d "$d" ] || continue
        found="$found$d
"
    done
    if [ -n "$found" ]; then
        STEWARD_NODE_BIN=$(printf '%s' "$found" | awk -F/ 'NF {
            v = $(NF - 1); sub(/^v/, "", v); n = split(v, p, ".")
            printf "%09d%09d%09d\t%s\n", p[1], (n > 1 ? p[2] : 0), (n > 2 ? p[3] : 0), $0
        }' | sort | tail -n 1 | cut -f 2-)
    fi
fi
if [ -n "${STEWARD_NODE_BIN:-}" ] && [ -d "$STEWARD_NODE_BIN" ]; then
    PATH="$STEWARD_NODE_BIN:$PATH"
fi
export PATH
PYTHON=${STEWARD_PYTHON:-$HOME/claude-stack-env/bin/python3}
if [ ! -x "$PYTHON" ]; then
    PYTHON=$(command -v python3)
fi
STEWARD_HOME=${STEWARD_HOME:-$HOME/.local/state/steward}
HERDR_SOCKET_PATH=${HERDR_SOCKET_PATH:-$HOME/.config/herdr/herdr.sock}
export STEWARD_HOME HERDR_SOCKET_PATH

exec env -u ANTHROPIC_API_KEY "$PYTHON" -m steward.coordinator "${1:-run}"
