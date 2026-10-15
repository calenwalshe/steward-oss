#!/bin/sh
# ExecStart wrapper for steward-mcp.service (LIVE-04). POSIX sh / dash-safe.
#
# Deliberately does NOT source ~/.api-keys: the MCP server needs no API
# secrets. Its only secret inputs are the scrypt consent hash named in
# ~/.config/steward/mcp.json and the OAuth state in $STEWARD_HOME/oauth.db.
#
# MCP write tools (steer_job, pause_job, resume_job, cancel_job,
# approve_action) relay into the live `steward control` pane instead of
# touching the store directly (OPX-03, 11-02) — this service is a systemd
# unit, not a Herdr pane, so it can never inherit $HERDR_SOCKET_PATH the way
# an interactive pane would. The socket it relays through comes from
# ~/.config/steward/mcp.json's own `herdr_socket` field (written by `steward
# mcp bootstrap`, defaulting to the live/default Herdr session's own socket,
# ~/.config/herdr/herdr.sock) unless overridden below.
#
# Overrides (optional, set via `systemctl --user edit steward-mcp`):
#   STEWARD_PYTHON        interpreter (default: python3 on PATH)
#   STEWARD_MCP_CONFIG    config path (default: ~/.config/steward/mcp.json)
#   STEWARD_MCP_HERDR_SOCKET  Herdr API socket override (default: mcp.json's
#                             own herdr_socket, else ~/.config/herdr/herdr.sock)
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$REPO_DIR"

PYTHON=${STEWARD_PYTHON:-python3}
CONFIG=${STEWARD_MCP_CONFIG:-$HOME/.config/steward/mcp.json}
HERDR_SOCK=${STEWARD_MCP_HERDR_SOCKET:-}

if [ -n "$HERDR_SOCK" ]; then
    exec "$PYTHON" -m steward.cli mcp serve --config "$CONFIG" --herdr-socket "$HERDR_SOCK"
fi
exec "$PYTHON" -m steward.cli mcp serve --config "$CONFIG"
