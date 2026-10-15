#!/usr/bin/env bash
# Before/after table for docs/perf/2026-10-05-tui-speed-plan.md. "before" = the base commit exported to .perf/base-src, "after" =
# this tree; each side gets a fresh STEWARD_HOME copied from .perf/home-template (DB snapshot + state files), so both
# read identical data. Everything runs through sandbox.sh (no network, no Herdr, read-only outside the worktree).
set -euo pipefail
W="$(cd "$(dirname "$0")/../.." && pwd)"
SB="$W/scripts/perf/sandbox.sh"
N="${N:-5}"
for side in before after; do
  src=$W; [ "$side" = before ] && src="$W/.perf/base-src"
  home="$W/.perf/home-$side"
  rm -rf "$home"; cp -a "$W/.perf/home-template" "$home"
  export PERF_HOME="$home" PERF_SRC="$src"
  echo "== $side ($src)"
  STEWARD_HEALTH=0 "$SB" python -m steward.dash_jobs >/dev/null 2>&1  # warm-up: first open (after: builds the indexes)
  for i in $(seq "$N"); do
    STEWARD_HEALTH=0 "$SB" /usr/bin/time -f "dash_jobs one-shot: %e s wall, %U+%S s cpu, %M KB" python -m steward.dash_jobs 2>&1 >/dev/null | tail -1
  done
  for i in 1 2 3; do
    "$SB" /usr/bin/time -f "status --json --recent 7 one-shot: %e s wall, %U+%S s cpu, %M KB" python -m steward.cli status --json --recent 7 2>&1 >/dev/null | tail -1
  done
  "$SB" python "$W/scripts/perf/bench.py" status --n "$N" | tail -1
  "$SB" python "$W/scripts/perf/bench.py" tokens --n "$N" | tail -1
  "$SB" python "$W/scripts/perf/bench.py" frame --n "$N" | tail -1
  "$SB" python "$W/scripts/perf/bench.py" tick --n "$N" 2>/dev/null | tail -1
  "$SB" python "$W/scripts/perf/bench.py" sweep --n "$N" 2>/dev/null | tail -1
  # plan items 9-12 (job 33157637)
  for i in 1 2 3; do
    "$SB" /usr/bin/time -f "tokens --json one-shot: %e s wall, %U+%S s cpu, %M KB" python -m steward.cli tokens --json 2>&1 >/dev/null | tail -1
  done
  "$SB" python "$W/scripts/perf/bench.py" steps --n "$N" 2>/dev/null | tail -2 | head -1
  "$SB" python "$W/scripts/perf/bench.py" permission --n "$N" 2>/dev/null | tail -2 | head -1
  "$SB" python "$W/scripts/perf/bench.py" dashapp --secs "${SECS:-30}" 2>/dev/null | tail -1
done
echo "== snapshot volume (this tree's code, read-only except the checks compaction on a scratch copy)"
home="$W/.perf/home-volume"; rm -rf "$home"; cp -a "$W/.perf/home-template" "$home"
PERF_HOME="$home" PERF_SRC="$W" "$SB" python "$W/scripts/perf/event_volume.py" --compact
