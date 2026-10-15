#!/usr/bin/env bash
# The clean-container smoke for the installable package (Phase 19, G20 tier 4).
#
#   scripts/container_smoke.sh [--mode docker|clean-home|auto] [--tarball PATH] [--dry-run]
#
# Both modes run scripts/container/smoke_inside.sh: install the tarball offline, start a headless Herdr, type
# `steward init --yes` into a pane, assert the tabs, re-run, `doctor`, `uninstall`; the last line is `SMOKE PASS` or
# `SMOKE FAIL: <reason>`. Exit 78 with HERDR_HEADLESS_UNAVAILABLE means the headless Herdr did not come up.
#
#   docker      Node 24 + Python 3.12 + the host's static herdr binary in an image built here, then run with no
#               network, a read-only root, no capabilities, no home, no docker socket and no privileged flag; only
#               the tarball and smoke_inside.sh are mounted, both read-only.
#   clean-home  the fallback, on this host: `env -i` with a throwaway HOME and a PATH of symlinks to a short list of
#               tools, and a throwaway named Herdr session `steward-test-smoke-<pid>` that a trap stops and deletes.
#   auto        docker when `docker info` succeeds, otherwise clean-home (and it says why).
#   --dry-run   print the exact commands, run none, create nothing.
#   --tarball   the package tarball (default: build one with packaging/npm/steward_pack_build.py --pack).
#
# Nothing here pushes, publishes or contacts a registry; the live home, config and Herdr sessions are never used.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
INSIDE="$REPO/scripts/container/smoke_inside.sh"
DOCKERFILE="scripts/container/Dockerfile.smoke"
CONTEXT="scripts/container"
IMAGE="steward-smoke:local"

# The throwaway roots live under a SHORT base: Herdr's socket path (<HOME>/.config/herdr/sessions/<name>/herdr.sock)
# must fit the 108 bytes of a unix socket address, which a deep $TMPDIR (a test runner's, for one) would exceed.
ROOT_BASE="${TMPDIR:-/tmp}"
if [ "${#ROOT_BASE}" -gt 24 ]; then
    ROOT_BASE=/tmp
fi

MODE=auto
TARBALL=""
DRY=0

usage() {
    sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --mode)
            [ $# -ge 2 ] || { echo "error: --mode needs a value" >&2; exit 2; }
            MODE="$2"
            shift 2
            ;;
        --tarball)
            [ $# -ge 2 ] || { echo "error: --tarball needs a path" >&2; exit 2; }
            TARBALL="$2"
            shift 2
            ;;
        --dry-run)
            DRY=1
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "error: unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done
case "$MODE" in
    docker | clean-home | auto) ;;
    *)
        echo "error: --mode must be docker, clean-home or auto (got $MODE)" >&2
        exit 2
        ;;
esac

# Print a command; run it unless this is a dry run.
step() {
    printf '+'
    printf ' %q' "$@"
    printf '\n'
    [ "$DRY" = 1 ] || "$@"
}

# -- the tarball ------------------------------------------------------------------------------------------------
BUILD_DIR=""
cleanup() {
    local rc=$?
    set +e
    if [ -n "${SESSION:-}" ]; then
        pkill -f -- "herdr --session $SESSION" >/dev/null 2>&1
    fi
    [ -n "$BUILD_DIR" ] && rm -rf -- "$BUILD_DIR"
    [ -n "${HOME_ROOT:-}" ] && [ "$CREATED_ROOT" = 1 ] && rm -rf -- "$HOME_ROOT"
    exit "$rc"
}
CREATED_ROOT=0
trap cleanup EXIT

if [ -z "$TARBALL" ]; then
    if [ "$DRY" = 1 ]; then
        BUILD_DIR=""
        TARBALL="${TMPDIR:-/tmp}/steward-smoke-build.XXXXXX/dist/<built tarball>.tgz"
        echo "+ (build a tarball) python3 packaging/npm/steward_pack_build.py --out <tmp>/dist --pack"
    else
        BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/steward-smoke-build.XXXXXX")"
        mkdir -p "$BUILD_DIR/npm-cache"
        built="$(cd "$REPO" && env npm_config_cache="$BUILD_DIR/npm-cache" npm_config_update_notifier=false \
            python3 packaging/npm/steward_pack_build.py --out "$BUILD_DIR/dist" --pack)" || {
            echo "error: could not build the tarball" >&2
            exit 1
        }
        TARBALL="$(printf '%s\n' "$built" | sed -n 's/^packed //p' | tail -n 1)"
        [ -f "$TARBALL" ] || { echo "error: the build printed no tarball path" >&2; exit 1; }
    fi
fi
case "$TARBALL" in
    /*) ;;
    *) TARBALL="$(cd "$(dirname "$TARBALL")" && pwd)/$(basename "$TARBALL")" ;;
esac
SESSION="steward-test-smoke-$$"

# -- mode: auto -------------------------------------------------------------------------------------------------
if [ "$MODE" = auto ]; then
    if ! command -v docker >/dev/null 2>&1; then
        why="docker not found"
    elif why="$(docker info 2>&1 >/dev/null | head -n 1)" && docker info >/dev/null 2>&1; then
        why=""
    else
        why="${why:-docker info failed}"
    fi
    if [ -z "$why" ]; then
        MODE=docker
        echo "auto: docker works here; using docker"
    else
        MODE=clean-home
        echo "auto: docker is unusable ($why); using clean-home"
    fi
fi

# -- mode: docker -----------------------------------------------------------------------------------------------
run_docker() {
    local herdr_bin
    herdr_bin="$(command -v herdr || true)"
    if [ -z "$herdr_bin" ]; then
        if [ "$DRY" = 1 ]; then
            herdr_bin="<path of the host herdr>"
        else
            echo "error: herdr is not on PATH: the image copies the host's static binary" >&2
            return 1
        fi
    fi
    if [ "$DRY" != 1 ] && ! docker info >/dev/null 2>&1; then
        echo "docker unusable: $(docker info 2>&1 >/dev/null | head -n 1)" >&2
        return 1
    fi
    step cp "$herdr_bin" "$REPO/$CONTEXT/herdr"
    step docker build -f "$DOCKERFILE" -t "$IMAGE" "$CONTEXT"
    local rc=0
    step docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
        --tmpfs /tmp:rw,exec,mode=1777,size=512m --tmpfs /home/smoke:rw,exec,uid=10001,gid=10001,size=256m \
        -v "$TARBALL:/smoke/package.tgz:ro" -v "$INSIDE:/smoke/smoke_inside.sh:ro" \
        -e SMOKE_TARBALL=/smoke/package.tgz -e "SMOKE_SESSION=$SESSION" \
        "$IMAGE" bash /smoke/smoke_inside.sh || rc=$?
    if [ "$rc" = 78 ]; then
        echo "HERDR_HEADLESS_UNAVAILABLE: herdr did not run headless in the container; use --mode clean-home instead" >&2
    fi
    return "$rc"
}

# -- mode: clean-home -------------------------------------------------------------------------------------------
# Symlink the short list of tools the smoke needs; deliberately not uv, systemctl, tg or cloudflared.
TOOLS="bash sh env cat sleep date tput stty head tail grep sed awk cut tr wc sort uniq ls mkdir rm cp mv dirname \
basename readlink uname id hostname ps pgrep pkill find xargs tee printf touch chmod ln mktemp sha256sum seq clear \
tty kill expr diff setsid node npm git herdr python3"

make_farm() {
    local bin="$1" name target py
    mkdir -p "$bin"
    for name in $TOOLS; do
        target="$(command -v "$name" || true)"
        [ -n "$target" ] || continue
        if [ "$name" = python3 ]; then
            target=""
            for py in python3.12 python3.13 python3.14 python3; do
                if command -v "$py" >/dev/null 2>&1 \
                    && "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
                    target="$(readlink -f "$(command -v "$py")")"
                    break
                fi
            done
            [ -n "$target" ] || continue
        elif [ "$name" = node ] || [ "$name" = npm ]; then
            target="$(readlink -f "$target")"
        fi
        ln -sf "$target" "$bin/$name"
    done
}

run_clean_home() {
    if [ "$DRY" = 1 ]; then
        HOME_ROOT="$ROOT_BASE/steward-smoke.XXXXXX"
    else
        HOME_ROOT="$(mktemp -d "$ROOT_BASE/steward-smoke.XXXXXX")"
        CREATED_ROOT=1
    fi
    echo "+ make a throwaway home and a PATH of symlinks: $HOME_ROOT/home, $HOME_ROOT/realbin (no docker)"
    if [ "$DRY" != 1 ]; then
        mkdir -p "$HOME_ROOT/home"
        make_farm "$HOME_ROOT/realbin"
    fi
    local rc=0
    step env -i "HOME=$HOME_ROOT/home" "PATH=$HOME_ROOT/realbin" "TMPDIR=$HOME_ROOT" LANG=C.UTF-8 \
        "SMOKE_TARBALL=$TARBALL" "SMOKE_SESSION=$SESSION" \
        "$HOME_ROOT/realbin/bash" "$INSIDE" || rc=$?
    if [ "$rc" = 78 ]; then
        echo "HERDR_HEADLESS_UNAVAILABLE: herdr did not run headless on this host either" >&2
    fi
    return "$rc"
}

RC=0
case "$MODE" in
    docker) run_docker || RC=$? ;;
    clean-home) run_clean_home || RC=$? ;;
esac
exit "$RC"
