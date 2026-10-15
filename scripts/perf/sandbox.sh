#!/usr/bin/env bash
# Run a command against a DB copy with the live system unreachable: read-only root, no network, no Herdr socket,
# no user bus; only this worktree is writable. Usage: scripts/perf/sandbox.sh <cmd...>
# PERF_HOME (default .perf/home) picks the STEWARD_HOME copy, PERF_SRC (default: this tree) the steward code under test.
W="$(cd "$(dirname "$0")/../.." && pwd)"
HOME_DIR="${PERF_HOME:-$W/.perf/home}"
SRC="${PERF_SRC:-$W}"
case "$HOME_DIR" in "$W"/.perf/*) ;; *) echo "refusing: PERF_HOME must be under $W/.perf" >&2; exit 2 ;; esac
exec env -u HERDR_PANE_ID -u HERDR_SOCKET_PATH -u HERDR_ENV -u HERDR_TAB_ID -u HERDR_WORKSPACE_ID \
  STEWARD_HOME="$HOME_DIR" STEWARD_CONFIG_DIR="$W/.perf/config" PYTHONPATH="$SRC" \
  bwrap --ro-bind / / --bind "$W" "$W" --dev /dev --proc /proc --tmpfs /tmp \
  --tmpfs /home/user/.config/herdr --tmpfs /run/user --unshare-net --unshare-ipc --unshare-pid --die-with-parent \
  --chdir "$SRC" "$@"
