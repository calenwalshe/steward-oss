#!/bin/sh
# Install the Steward watchdog unit (issue W). DRY RUN unless --yes: a live systemd write needs the owner's OK.
#   deploy/install-steward-watchdog.sh          print the plan and the environment check; change nothing
#   deploy/install-steward-watchdog.sh --yes    link the unit, daemon-reload, enable --now
# It never enables lingering itself; it prints the command when lingering is off.
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
UNIT=steward-watchdog.service
SRC="$REPO_DIR/deploy/$UNIT"
DEST_DIR="$HOME/.config/systemd/user"
APPLY=no
[ "${1:-}" = "--yes" ] && APPLY=yes

echo "plan:"
echo "  1. ln -sf $SRC $DEST_DIR/$UNIT"
echo "  2. systemctl --user daemon-reload"
echo "  3. systemctl --user enable --now $UNIT"
if loginctl show-user "${USER:-$(id -un)}" -p Linger 2>/dev/null | grep -q '^Linger=yes'; then
    echo "lingering: on"
else
    echo "lingering: off - the watchdog stops at logout; to keep it (The owner's call): loginctl enable-linger ${USER:-$(id -un)}"
fi
echo "environment check:"
"$REPO_DIR/deploy/run-steward-watchdog.sh" --check-env || echo "  (fix the FAIL lines before relying on the watchdog)"

if [ "$APPLY" != yes ]; then
    echo "dry run: nothing changed (re-run with --yes to install)"
    exit 0
fi
mkdir -p "$DEST_DIR"
ln -sf "$SRC" "$DEST_DIR/$UNIT"
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"
echo "installed: systemctl --user status $UNIT"
