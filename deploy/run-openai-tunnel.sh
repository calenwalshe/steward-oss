#!/bin/sh
# ExecStart wrapper for steward-openai-tunnel.service. POSIX sh / dash-safe.
# Loads OPENAI_TUNNEL_API_KEY (Tunnels Read+Use) from ~/.api-keys WITHOUT
# EnvironmentFile= (systemd drops `export` lines and journals the values).
set -eu
set -a
. "$HOME/.api-keys"
set +a
: "${OPENAI_TUNNEL_API_KEY:?OPENAI_TUNNEL_API_KEY missing from ~/.api-keys}"
CONTROL_PLANE_API_KEY=$OPENAI_TUNNEL_API_KEY
export CONTROL_PLANE_API_KEY
exec "$HOME/.local/opt/openai-tunnel-client/tunnel-client" run --profile steward
