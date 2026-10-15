#!/usr/bin/env bash
# The smoke steps shared by both modes of scripts/container_smoke.sh (Phase 19, G20 tier 4).
#
# Runs INSIDE the clean container, or on the host under `env -i` with a throwaway HOME (clean-home mode). It never
# reads or writes anything outside its own temp directory: every path below hangs off $WORK, and Herdr runs as a
# throwaway named session `steward-test-smoke-*` (in clean-home mode under that throwaway HOME, so not even the
# user's own Herdr directory is involved).
#
#   install the tarball offline -> start a headless Herdr -> type `steward init --yes` into a pane -> assert the tabs
#   -> re-run (nothing may change) -> `steward doctor` -> `steward uninstall` -> SMOKE PASS
#
# Last output line: `SMOKE PASS` or `SMOKE FAIL: <reason>`. Exit 78 with HERDR_HEADLESS_UNAVAILABLE when the headless
# Herdr server does not answer within 15 seconds (the caller then uses clean-home mode instead).
set -euo pipefail
set -E  # the ERR trap below also covers the functions

: "${SMOKE_TARBALL:?SMOKE_TARBALL must name the package tarball}"
SESSION="${SMOKE_SESSION:-steward-test-smoke-$$}"
SERVER_PID=""
WORK=""

fail() {
    echo "SMOKE FAIL: $*"
    exit 1
}

case "$SESSION" in
    steward-test-smoke-*) ;;
    *) fail "the session name must start with steward-test-smoke- (got $SESSION)" ;;
esac
[ -f "$SMOKE_TARBALL" ] || fail "tarball not found: $SMOKE_TARBALL"

hd() { herdr --session "$SESSION" "$@"; }

cleanup() {
    local rc=$?
    trap - ERR  # the cleanup's own failed commands (a session that is already gone) are not errors
    set +e
    if [ -n "$SERVER_PID" ] || [ -n "$WORK" ]; then
        hd session stop "$SESSION" >/dev/null 2>&1
        [ -n "$SERVER_PID" ] && kill "$SERVER_PID" >/dev/null 2>&1
        hd session delete "$SESSION" >/dev/null 2>&1
        pkill -f -- "herdr --session $SESSION" >/dev/null 2>&1
    fi
    if [ -n "$WORK" ] && [ "${SMOKE_KEEP:-0}" != "1" ]; then
        rm -rf -- "$WORK"
    fi
    exit "$rc"
}
trap cleanup EXIT
trap 'rc=$?; echo "SMOKE FAIL: unexpected error (exit $rc) at line $LINENO"; exit 1' ERR

WORK="$(mktemp -d "${TMPDIR:-/tmp}/sr.XXXXXX")"  # short: Herdr's socket path has to fit a unix socket address
mkdir -p "$WORK/h" "$WORK/xdg/config" "$WORK/xdg/data" "$WORK/xdg/cache" "$WORK/fakebin" "$WORK/prefix" \
    "$WORK/repo" "$WORK/npm-cache"

# An empty machine: nothing of the caller's HOME, XDG_*, STEWARD_* or Herdr environment is used.
export HOME="$WORK/h" XDG_CONFIG_HOME="$WORK/xdg/config" XDG_DATA_HOME="$WORK/xdg/data" \
    XDG_CACHE_HOME="$WORK/xdg/cache"
export STEWARD_HOME="$WORK/steward_home" STEWARD_CONFIG_DIR="$WORK/steward_config" STEWARD_DATA_DIR="$WORK/steward_data"
export npm_config_cache="$WORK/npm-cache" npm_config_prefix="$WORK/prefix" npm_config_userconfig="$WORK/npmrc" \
    npm_config_globalconfig="$WORK/npmrc-global" npm_config_update_notifier=false npm_config_fund=false \
    npm_config_audit=false
export SHELL=/bin/sh LANG=C.UTF-8 TERM=xterm-256color
unset HERDR_ENV HERDR_SOCKET_PATH HERDR_PANE_ID HERDR_TAB_ID HERDR_WORKSPACE_ID HERDR_SESSION \
    STEWARD_OWNER_NAME TYPESAFE_API_KEY
export PATH="$WORK/fakebin:$WORK/prefix/bin:$PATH"

# Fake claude and codex: fixed strings only, no account data, no network.
cat > "$WORK/fakebin/claude" <<'FAKE'
#!/bin/sh
case "$1" in
  --version) echo "fake claude 0.0.0"; exit 0 ;;
  auth) echo '{"loggedIn": true}'; exit 0 ;;
esac
echo "fake claude ready"
while read -r _line; do :; done
FAKE
cat > "$WORK/fakebin/codex" <<'FAKE'
#!/bin/sh
case "$1" in
  --version) echo "fake codex 0.0.0"; exit 0 ;;
esac
exit 0
FAKE
chmod 0755 "$WORK/fakebin/claude" "$WORK/fakebin/codex"

# -- 1. install the tarball, offline ------------------------------------------------------------------------------
npm install -g --prefix "$WORK/prefix" --offline --no-audit --no-fund --ignore-scripts "$SMOKE_TARBALL" \
    >"$WORK/npm-install.log" 2>&1 || fail "the offline install failed: $(tail -n 3 "$WORK/npm-install.log" | tr '\n' ' ')"
version_line="$(steward --version 2>&1)" || fail "steward --version failed: $version_line"
case "$version_line" in
    "steward "*"install root: "*"/lib/node_modules/"*) ;;
    *) fail "unexpected --version line: $version_line" ;;
esac
echo "installed: $version_line"

# -- 2. a headless Herdr server for the throwaway session -------------------------------------------------------
if command -v setsid >/dev/null 2>&1; then
    setsid herdr --session "$SESSION" server >"$WORK/herdr-server.log" 2>&1 </dev/null &
else
    herdr --session "$SESSION" server >"$WORK/herdr-server.log" 2>&1 </dev/null &
fi
SERVER_PID=$!
ready=0
for _ in $(seq 1 60); do
    if hd workspace list >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 0.25
done
if [ "$ready" != 1 ]; then
    echo "HERDR_HEADLESS_UNAVAILABLE: the herdr server did not answer within 15 seconds ($(tail -n 1 "$WORK/herdr-server.log" 2>/dev/null))"
    exit 78
fi

ws_json="$(hd workspace create --cwd "$WORK/repo")" || fail "could not create a workspace"
PANE="$(printf '%s' "$ws_json" | python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]; print((r.get("root_pane") or r.get("pane"))["pane_id"])')" \
    || fail "could not read the root pane of the new workspace"

# Type a command into the pane and wait for its exit code: run_in_pane <name> <command...>; the code is in $RC.
RC=""
run_in_pane() {
    local name="$1"
    shift
    rm -f "$WORK/rc-$name"
    hd pane run "$PANE" "( $* ) > $WORK/out-$name.txt 2>&1; echo \$? > $WORK/rc-$name" >/dev/null \
        || fail "could not type into the pane for $name"
    local waited=0
    while [ ! -s "$WORK/rc-$name" ]; do
        sleep 0.25
        waited=$((waited + 1))
        [ "$waited" -lt 360 ] || fail "'$*' did not finish within 90 seconds"
    done
    RC="$(tr -d '[:space:]' < "$WORK/rc-$name")"
}

tab_labels() {
    hd tab list | python3 -c 'import json,sys; print(" ".join(sorted(t.get("label", "") for t in json.load(sys.stdin)["result"]["tabs"])))'
}

# Tab ids, pane ids and the sha256 of every config file: what a second run must leave exactly as it is.
snapshot() {
    {
        hd tab list | python3 -c 'import json,sys; print("\n".join(sorted(t["tab_id"] for t in json.load(sys.stdin)["result"]["tabs"])))'
        hd pane list | python3 -c 'import json,sys; print("\n".join(sorted(p["pane_id"] for p in json.load(sys.stdin)["result"]["panes"])))'
        (cd "$STEWARD_CONFIG_DIR" && find . -type f | sort | xargs sha256sum)
    }
}

# -- 3. first run -----------------------------------------------------------------------------------------------
run_in_pane init1 steward init --yes
[ "$RC" = 0 ] || fail "steward init --yes exited $RC: $(tail -n 3 "$WORK/out-init1.txt" | tr '\n' ' ')"
[ -f "$STEWARD_HOME/install.json" ] || fail "install.json was not written under STEWARD_HOME"
labels="$(tab_labels)"
for tab in steward cockpit; do
    printf '%s\n' $labels | grep -qx "$tab" || fail "the $tab tab does not exist (tabs: $labels)"
done
grep -q "Run Steward on" "$WORK/out-init1.txt" || fail "the first-use sentence was not printed"
echo "first run: tabs $labels"
first="$(snapshot)"

# -- 4. re-run: nothing new, nothing overwritten ------------------------------------------------------------------
run_in_pane init2 steward init --yes
[ "$RC" = 0 ] || fail "the second steward init --yes exited $RC"
second="$(snapshot)"
[ "$first" = "$second" ] || fail "a second run changed tabs, panes or config files: $(diff <(printf '%s' "$first") <(printf '%s' "$second") | head -n 4 | tr '\n' ' ')"
echo "re-run: nothing changed"

# -- 5. doctor ---------------------------------------------------------------------------------------------------
run_in_pane doctor steward doctor
[ "$RC" = 0 ] || fail "steward doctor exited $RC: $(head -n 6 "$WORK/out-doctor.txt" | tr '\n' ' ')"
if grep -q "^✗" "$WORK/out-doctor.txt"; then
    fail "steward doctor reports a missing required item"
fi
echo "doctor: every required item ok"

# -- 6. uninstall ------------------------------------------------------------------------------------------------
run_in_pane uninstall steward uninstall
[ "$RC" = 0 ] || fail "steward uninstall exited $RC: $(tail -n 3 "$WORK/out-uninstall.txt" | tr '\n' ' ')"
[ ! -e "$STEWARD_HOME/install.json" ] || fail "uninstall left install.json behind"
[ -f "$STEWARD_CONFIG_DIR/steward.toml" ] || fail "uninstall removed the config"
gone=0
for _ in $(seq 1 120); do
    labels="$(tab_labels)"
    if ! printf '%s\n' $labels | grep -qx -e steward -e cockpit; then
        gone=1
        break
    fi
    sleep 0.25
done
[ "$gone" = 1 ] || fail "uninstall left the steward or cockpit tab open (tabs: $labels)"
echo "uninstall: tabs closed, install.json removed, config kept"

echo "SMOKE PASS"
