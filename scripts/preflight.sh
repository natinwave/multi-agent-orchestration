#!/usr/bin/env bash
# Read-only check of this host. CHANGES NOTHING -- no installs, no writes,
# no containers started. Safe to run repeatedly.
#
# This is the first thing run on the remote box, and its entire output gets
# relayed to a person as chat text, so it is terse on purpose: one line per
# check, one summary line, no colour, no spinners.
#
#   ./scripts/preflight.sh
#
# Exit 0 if nothing FAILed. WARN means "works, but you will want to fix it".
set -uo pipefail   # deliberately not -e: a failing check must not stop the run

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/common.sh
. "${HERE}/scripts/lib/common.sh"

ORCH_ROOT="${ORCH_ROOT:-/srv/orchestration}"
MIN_DISK_GB="${MIN_DISK_GB:-40}"
MIN_VRAM_MB="${MIN_VRAM_MB:-8000}"

echo "orchestration preflight -- $(date -u '+%Y-%m-%d %H:%M UTC') on $(hostname)"
echo

# --- the host itself -------------------------------------------------------

kernel="$(uname -s)"
if [ "$kernel" = "Linux" ]; then
  pass "host is Linux ($(uname -r))"
else
  fail "host is $kernel, not Linux -- this system targets the Ubuntu box"
  info "on macOS, Docker Desktop hides the bind-mount and UID behaviour this relies on"
fi

if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091  # provided by the distro, not this repo
  . /etc/os-release
  case "${VERSION_ID:-}" in
    24.04) pass "${PRETTY_NAME:-unknown distro}" ;;
    *)     warn "${PRETTY_NAME:-unknown distro} -- built for Ubuntu 24.04" ;;
  esac
fi

# --- docker ----------------------------------------------------------------

if have docker; then
  pass "docker installed ($(docker --version 2>&1 | clip))"

  if docker info >/dev/null 2>&1; then
    pass "docker daemon reachable"

    # Docker Desktop's daemon behaves differently enough from native Docker
    # that a pass here on a Mac means very little.
    if docker info --format '{{.OperatingSystem}}' 2>/dev/null | grep -qi "docker desktop"; then
      warn "this is Docker Desktop, not native Docker -- results will not transfer"
    fi
  else
    err="$(docker info 2>&1 | clip)"
    if printf '%s' "$err" | grep -qi "permission denied"; then
      fail "docker daemon unreachable: $(id -un) is not in the docker group"
      info "fix: sudo usermod -aG docker $(id -un), then log out and back in"
    else
      fail "docker daemon unreachable: ${err}"
    fi
  fi

  if docker compose version >/dev/null 2>&1; then
    pass "docker compose v2 ($(docker compose version --short 2>&1 | clip))"
  else
    fail "docker compose v2 plugin missing (the standalone docker-compose is not enough)"
  fi
else
  fail "docker not installed"
fi

# --- disk ------------------------------------------------------------------

disk_target="$ORCH_ROOT"
[ -d "$disk_target" ] || disk_target="$(dirname "$ORCH_ROOT")"
[ -d "$disk_target" ] || disk_target="/"
free_gb="$(df -BG --output=avail "$disk_target" 2>/dev/null | tail -1 | tr -dc '0-9')"
if [ -z "$free_gb" ]; then
  # BSD df, i.e. someone is running this on the Mac.
  free_gb="$(df -g "$disk_target" 2>/dev/null | tail -1 | awk '{print $4}')"
fi
if [ -n "$free_gb" ] && [ "$free_gb" -ge "$MIN_DISK_GB" ] 2>/dev/null; then
  pass "disk: ${free_gb}G free on ${disk_target} (want ${MIN_DISK_GB}G)"
elif [ -n "$free_gb" ]; then
  warn "disk: only ${free_gb}G free on ${disk_target} (want ${MIN_DISK_GB}G)"
else
  warn "disk: could not measure free space on ${disk_target}"
fi

# --- gpu -------------------------------------------------------------------

if have nvidia-smi; then
  vram_free="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -dc '0-9')"
  gpu_name="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | clip)"
  if [ -z "$vram_free" ]; then
    warn "nvidia-smi present but reported no GPU"
  elif [ "$vram_free" -ge "$MIN_VRAM_MB" ]; then
    pass "gpu: ${gpu_name}, ${vram_free}MiB VRAM free (want ${MIN_VRAM_MB})"
  else
    warn "gpu: ${gpu_name}, only ${vram_free}MiB VRAM free (want ${MIN_VRAM_MB}) -- hermes may not load"
  fi
else
  warn "nvidia-smi not found -- no local GPU model, hermes will be unavailable"
fi

# --- toolchain -------------------------------------------------------------

if have git; then
  git_v="$(git --version | awk '{print $3}')"
  pass "git ${git_v}"
else
  fail "git not installed"
fi

if have python3; then
  py_v="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
  if python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    pass "python ${py_v}"
  else
    fail "python ${py_v} -- 3.12 or newer required (tomllib, StrEnum)"
  fi
else
  fail "python3 not installed"
fi

if have gh; then
  if gh auth status >/dev/null 2>&1; then
    acct="$(gh auth status 2>&1 | grep -o 'account [^ ]*' | head -1 | cut -d' ' -f2)"
    pass "gh authenticated${acct:+ as ${acct}}"
  else
    warn "gh installed but not authenticated -- run: gh auth login"
  fi
else
  warn "gh not installed -- agents will not be able to open pull requests"
fi

if have op; then
  if op whoami >/dev/null 2>&1; then
    pass "1Password CLI signed in"
  else
    fail "1Password CLI installed but not signed in -- run: eval \$(op signin)"
    info "bootstrap needs it to materialise credentials; it holds no secrets itself"
  fi
else
  fail "1Password CLI (op) not installed -- secrets are op:// references only"
fi

# --- claude code, the one that cannot be fixed remotely --------------------

if have claude; then
  pass "claude code installed ($(claude --version 2>&1 | clip))"

  token_source=""
  [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && token_source="environment"
  [ -z "$token_source" ] && [ -r "${ORCH_SECRETS:-/run/orchestration/secrets}/claude-code/oauth_token" ] \
    && token_source="secrets file"

  if [ -n "$token_source" ]; then
    pass "claude oauth token present (${token_source})"
    if out="$(timeout 90 claude -p \
                --permission-mode acceptEdits \
                --allowedTools "" \
                "say ok" 2>&1)"; then
      pass "claude authenticated and answering ($(printf '%s' "$out" | clip))"
    else
      fail "claude did not answer: $(printf '%s' "$out" | clip)"
    fi
  else
    fail "no CLAUDE_CODE_OAUTH_TOKEN -- containers will hang on the onboarding wizard"
    info "A HUMAN MUST RUN 'claude setup-token' WHILE SITTING AT THIS MACHINE."
    info "It is interactive: it cannot be done over chat, by an agent, or over ssh"
    info "without a terminal. Then store the token in 1Password as the op:// ref"
    info "in .env.example, and re-run this script."
  fi
else
  fail "claude code not installed (npm install -g @anthropic-ai/claude-code)"
fi

# --- runtime root ----------------------------------------------------------

if [ -d "$ORCH_ROOT" ]; then
  if [ -w "$ORCH_ROOT" ]; then
    pass "runtime root ${ORCH_ROOT} exists and is writable"
  else
    fail "runtime root ${ORCH_ROOT} exists but is not writable by $(id -un)"
  fi
else
  parent="$(dirname "$ORCH_ROOT")"
  if [ -w "$parent" ] || [ "$(id -u)" = 0 ]; then
    info "runtime root ${ORCH_ROOT} does not exist yet -- bootstrap.sh will create it"
  else
    warn "runtime root ${ORCH_ROOT} missing and ${parent} is not writable -- bootstrap needs sudo"
  fi
fi

summarise \
  "not ready; fix the FAIL lines above, then re-run" \
  "usable, but read the WARN lines before relying on it" \
  "ready; run ./scripts/bootstrap.sh next"
