#!/usr/bin/env bash
# Install the SIP listener as a user service. No root needed.
#
#   ./scripts/install-voice-service.sh
#
# The listener waits for the phone to ring and never exits, so it cannot be
# run from a shell an agent is holding open -- the tool call times out, the
# process dies with it, and the phone silently stops working. This makes it
# a service that survives that, restarts on failure, and logs to journald
# where it can be read without holding anything.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/common.sh
. "${HERE}/scripts/lib/common.sh"

UNIT_NAME="orchestrator-voice.service"
TEMPLATE="${HERE}/systemd/${UNIT_NAME}.template"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="${UNIT_DIR}/${UNIT_NAME}"

echo "installing ${UNIT_NAME} for $(id -un)"
echo

if [ "$(id -u)" = 0 ]; then
  fail "run this as your own user, not root -- it installs a user service"
  summarise "not installed" "" ""
  exit 1
fi

if ! have systemctl; then
  fail "no systemctl here; run ./bin/orchestrate-voice --transport sip yourself"
  summarise "not installed" "" ""
  exit 1
fi

if [ ! -r "$TEMPLATE" ]; then
  fail "missing ${TEMPLATE}"
  summarise "not installed" "" ""
  exit 1
fi

mkdir -p "$UNIT_DIR"
if sed "s|%REPO%|${HERE}|g" "$TEMPLATE" > "${UNIT}.new" && mv "${UNIT}.new" "$UNIT"; then
  pass "wrote ${UNIT}"
else
  rm -f "${UNIT}.new"
  fail "could not write ${UNIT}"
fi

if systemctl --user daemon-reload 2>/dev/null; then
  pass "systemd reloaded"
else
  fail "systemctl --user is not available (no user session bus?)"
  info "if this is an ssh session, try: sudo loginctl enable-linger $(id -un)"
fi

# Without lingering, the service stops the moment the last session ends --
# which for a phone line means it works until you log out and then does not.
if have loginctl && [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" = "yes" ]; then
  pass "lingering enabled -- the service survives logout and reboot"
else
  warn "lingering is off: the service will stop when your session ends"
  info "fix: sudo loginctl enable-linger $(id -un)"
fi

if systemctl --user enable --now "$UNIT_NAME" 2>/dev/null; then
  pass "enabled and started"
else
  fail "could not enable the service"
  info "see: systemctl --user status ${UNIT_NAME}"
fi

sleep 2
if systemctl --user is-active --quiet "$UNIT_NAME"; then
  pass "running"
else
  fail "not running -- it may be missing credentials or configuration"
  info "see why: journalctl --user -u ${UNIT_NAME} -n 30 --no-pager"
fi

echo
info "follow the log:  journalctl --user -u ${UNIT_NAME} -f"
info "restart:         systemctl --user restart ${UNIT_NAME}"
summarise \
  "not installed; fix the FAIL lines above" \
  "installed, with caveats -- read the WARN lines" \
  "installed and running; the phone is live"
