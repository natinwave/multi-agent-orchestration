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
  # 22.04 and 24.04 both work. What actually matters is checked below, one
  # capability at a time, rather than inferred from a version number.
  case "${VERSION_ID:-}" in
    24.04|22.04) pass "${PRETTY_NAME:-unknown distro}" ;;
    *)           warn "${PRETTY_NAME:-unknown distro} -- tested on Ubuntu 22.04 and 24.04" ;;
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

# Reported, not judged. A local model that is already loaded is SUPPOSED to
# be holding most of the VRAM -- treating that as a problem had this warning
# firing precisely when hermes was healthy. What matters is whether the
# endpoint answers, which is checked below.
if have nvidia-smi; then
  vram_free="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -dc '0-9')"
  vram_total="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -dc '0-9')"
  gpu_name="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | clip)"
  if [ -n "$vram_free" ]; then
    pass "gpu: ${gpu_name} (${vram_free}MiB of ${vram_total}MiB VRAM free)"
  else
    warn "nvidia-smi present but reported no GPU"
  fi
else
  info "no nvidia-smi -- fine unless you expect a local GPU model here"
fi

# The real question for a local-model agent: is anything answering?
if have python3; then
  endpoints="$(python3 - "$HERE/config/agents.toml" <<'PYEOF' 2>/dev/null
import sys, tomllib
try:
    with open(sys.argv[1], "rb") as fh:
        cfg = tomllib.load(fh)
except Exception:
    raise SystemExit(0)
for name, spec in cfg.get("agents", {}).items():
    if spec.get("type") == "http_openai" and spec.get("base_url"):
        print(f"{name}\t{spec['base_url']}")
PYEOF
)"
  while IFS="$(printf '\t')" read -r agent url; do
    [ -n "${agent:-}" ] || continue
    if python3 - "$url" <<'PYEOF' 2>/dev/null
import socket, sys, urllib.parse
u = urllib.parse.urlparse(sys.argv[1])
port = u.port or (443 if u.scheme == "https" else 80)
with socket.create_connection((u.hostname, port), timeout=3):
    pass
PYEOF
    then
      pass "agent ${agent}: something is listening at ${url}"
    else
      warn "agent ${agent}: nothing listening at ${url} -- jobs for it will fail"
    fi
  done <<EOF
${endpoints}
EOF
fi

# --- toolchain -------------------------------------------------------------

if have git; then
  git_v="$(git --version | awk '{print $3}')"
  git_major="${git_v%%.*}"; git_minor="$(printf '%s' "$git_v" | cut -d. -f2)"
  if [ "$git_major" -gt 2 ] 2>/dev/null || { [ "$git_major" = 2 ] && [ "$git_minor" -ge 20 ] 2>/dev/null; }; then
    pass "git ${git_v}"
    # --relative-paths worktrees arrived in 2.48. Below that the container
    # bind MUST use the identical path, which is what compose does.
    if [ "$git_minor" -lt 48 ] 2>/dev/null && [ "$git_major" = 2 ]; then
      info "git < 2.48: worktrees need the identical-path bind (compose already does this)"
    fi
  else
    fail "git ${git_v} -- 2.20 or newer required for worktree handling"
  fi
else
  fail "git not installed"
fi

if have python3; then
  py_v="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
  # 3.11 is the real floor: tomllib, StrEnum and datetime.UTC all arrive
  # there. Verified by running the suite under 3.11 and 3.10 -- 3.10 fails
  # on datetime.UTC and nothing else.
  if python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    pass "python ${py_v}"
  else
    fail "python ${py_v} -- 3.11 or newer required (tomllib, StrEnum, datetime.UTC)"
  fi

  # Ubuntu ships venv as a separate package, and without it bootstrap fails
  # several steps in with an error that does not name the cause.
  if python3 -c 'import venv, ensurepip' 2>/dev/null; then
    pass "python venv available"
  else
    fail "python venv missing -- run: sudo apt install python3-venv"
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
  info "install: https://github.com/cli/cli/blob/trunk/docs/install_linux.md"
fi

if have op; then
  if op whoami >/dev/null 2>&1; then
    pass "1Password CLI signed in"
  else
    fail "1Password CLI installed but not authenticated"
    info "headless: put a service account token at ~/.op-token (bootstrap finds it)"
    info "interactive: eval \$(op signin)"
  fi
else
  fail "1Password CLI (op) not installed -- secrets are op:// references only"
fi

# --- claude code, the one that cannot be fixed remotely --------------------

if have claude; then
  pass "claude code installed ($(claude --version 2>&1 | clip))"

  token_source=""
  [ -n "${ANTHROPIC_API_KEY:-}" ] && token_source="ANTHROPIC_API_KEY"
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
    fail "no Claude credential -- containers will hang on the onboarding wizard"
    info "Two ways to fix, both needing a person once:"
    info "  1. 'claude setup-token' at ANY terminal you are logged into -- your"
    info "     laptop is fine, it does not have to be this machine. It is"
    info "     interactive, so no agent and no chat relay can do it. Put the"
    info "     result in 1Password under the op:// ref in .env.example."
    info "  2. Or set ANTHROPIC_API_KEY instead: bills per token, needs nobody"
    info "     at a keyboard, and keeps a subscription token off an always-on box."
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
