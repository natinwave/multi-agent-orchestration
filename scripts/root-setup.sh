#!/usr/bin/env bash
# The ONLY part of this system that needs root. Run once.
#
#   sudo ./scripts/root-setup.sh
#
# Everything else -- preflight, bootstrap, the supervisor, every agent --
# runs as your ordinary user. This script exists so that root actions are
# one reviewable file in git rather than a handful of commands pasted from
# a README, and so a relay agent needs one approval instead of several.
#
# It is idempotent: re-running changes nothing that is already correct.
#
# What it does:
#   1. creates /srv/orchestration and /run/orchestration/secrets, owned by you
#   2. installs a systemd-tmpfiles rule so the /run directory comes back
#      with the right owner after every reboot (/run is a tmpfs)
#   3. reports whether you can reach the docker daemon
#
# What it deliberately does NOT do without being asked: add you to the
# docker group. Membership of that group is equivalent to root on this
# machine, so it takes an explicit --add-docker-group.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/common.sh
. "${HERE}/scripts/lib/common.sh"

ORCH_ROOT="${ORCH_ROOT:-/srv/orchestration}"
ORCH_SECRETS="${ORCH_SECRETS:-/run/orchestration/secrets}"
TMPFILES_CONF="/etc/tmpfiles.d/orchestration.conf"
ADD_DOCKER_GROUP=0
TARGET_USER=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --user)              TARGET_USER="${2:?--user needs a name}"; shift 2 ;;
    --add-docker-group)  ADD_DOCKER_GROUP=1; shift ;;
    --help|-h)           sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)                   echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" != 0 ]; then
  echo "FAIL  this script must run as root: sudo $0"
  exit 1
fi

# Who the directories should belong to. SUDO_USER is the person who typed
# sudo, which is nearly always the answer; running as bare root without it
# is ambiguous enough to refuse rather than guess.
TARGET_USER="${TARGET_USER:-${SUDO_USER:-}}"
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
  echo "FAIL  cannot tell which user should own these directories"
  echo "      run with sudo from your own account, or pass --user <name>"
  exit 1
fi
if ! id "$TARGET_USER" >/dev/null 2>&1; then
  echo "FAIL  no such user: ${TARGET_USER}"
  exit 1
fi

TARGET_UID="$(id -u "$TARGET_USER")"
TARGET_GID="$(id -g "$TARGET_USER")"

# Refuse to recurse over anything alarming, however we got the value.
for path in "$ORCH_ROOT" "$ORCH_SECRETS"; do
  case "$path" in
    /|/usr|/etc|/var|/home|/root|"")
      echo "FAIL  refusing to operate on ${path:-<empty>}"
      exit 1
      ;;
    /*) ;;
    *)
      echo "FAIL  ${path} is not an absolute path"
      exit 1
      ;;
  esac
done

echo "orchestration root setup -- for user ${TARGET_USER} (${TARGET_UID}:${TARGET_GID})"
echo

# --- 1. runtime directories ------------------------------------------------

if mkdir -p "$ORCH_ROOT"/{jobs,worktrees,scratch,repos,logs} 2>/dev/null; then
  chown -R "${TARGET_UID}:${TARGET_GID}" "$ORCH_ROOT"
  chmod 0755 "$ORCH_ROOT"
  pass "${ORCH_ROOT} ready, owned by ${TARGET_USER}"
else
  fail "could not create ${ORCH_ROOT}"
fi

if mkdir -p "$ORCH_SECRETS" 2>/dev/null; then
  # -R matters: an earlier run may have left per-agent subdirectories owned
  # by root, which is the failure this script exists to stop repeating.
  chown -R "${TARGET_UID}:${TARGET_GID}" "$(dirname "$ORCH_SECRETS")"
  chmod 0700 "$ORCH_SECRETS"
  pass "${ORCH_SECRETS} ready, owned by ${TARGET_USER} (mode 0700)"
else
  fail "could not create ${ORCH_SECRETS}"
fi

# --- 2. survive a reboot ---------------------------------------------------
# /run is a tmpfs. Without this rule the directory is gone after every
# restart and someone has to be root again before bootstrap can run.

template="${HERE}/systemd/orchestration.conf.template"
if [ ! -r "$template" ]; then
  fail "missing ${template}"
elif ! command -v systemd-tmpfiles >/dev/null 2>&1; then
  warn "no systemd-tmpfiles here -- ${ORCH_SECRETS} will need recreating after each reboot"
else
  mkdir -p "$(dirname "$TMPFILES_CONF")"
  if sed "s/%USER%/${TARGET_USER}/g" "$template" > "${TMPFILES_CONF}.new" \
     && mv "${TMPFILES_CONF}.new" "$TMPFILES_CONF" \
     && chmod 0644 "$TMPFILES_CONF"; then
    if systemd-tmpfiles --create 2>/dev/null; then
      pass "tmpfiles rule installed -- ${ORCH_SECRETS} now survives a reboot"
    else
      warn "tmpfiles rule written to ${TMPFILES_CONF} but systemd-tmpfiles --create failed"
    fi
  else
    rm -f "${TMPFILES_CONF}.new"
    fail "could not write ${TMPFILES_CONF}"
  fi
fi

# --- 3. docker access ------------------------------------------------------
# Reported by default. Membership of the docker group is equivalent to root
# on this machine, so granting it is an explicit request, never a side
# effect of running a setup script.

if ! command -v docker >/dev/null 2>&1; then
  warn "docker is not installed"
elif sudo -u "$TARGET_USER" docker info >/dev/null 2>&1; then
  pass "${TARGET_USER} can reach the docker daemon"
elif [ "$ADD_DOCKER_GROUP" = 1 ]; then
  if usermod -aG docker "$TARGET_USER"; then
    pass "added ${TARGET_USER} to the docker group"
    info "this grants root-equivalent access to this machine"
    info "${TARGET_USER} must log out and back in for it to take effect"
  else
    fail "could not add ${TARGET_USER} to the docker group"
  fi
else
  warn "${TARGET_USER} cannot reach the docker daemon"
  info "docker group membership is root-equivalent, so it is not granted"
  info "automatically. If you want it: sudo $0 --add-docker-group"
fi

summarise \
  "incomplete; fix the FAIL lines above and re-run (it is safe to re-run)" \
  "done, with caveats -- read the WARN lines" \
  "done; now run ./scripts/bootstrap.sh as ${TARGET_USER} (no sudo)"
