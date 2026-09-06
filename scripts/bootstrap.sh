#!/usr/bin/env bash
# Bring this host to a working state. IDEMPOTENT: re-running after a partial
# failure converges rather than duplicating or corrupting anything.
#
# One PASS/FAIL line per step to stdout (which is relayed to a person as
# chat text), full detail to a log file, one summary line at the end.
#
#   ./scripts/bootstrap.sh                 # converge
#   op run --env-file=.env -- ./scripts/bootstrap.sh   # ...with secrets
#   ./scripts/bootstrap.sh --selftest      # and prove ask/check work
#   ./scripts/bootstrap.sh --skip-build    # skip the slow image build
#
# Secrets: this script never writes a value into the repo, an image, or a
# container's environment. It copies them from 1Password into per-agent
# directories on a tmpfs, mode 0400, which containers mount read-only.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/common.sh
. "${HERE}/scripts/lib/common.sh"

ORCH_ROOT="${ORCH_ROOT:-/srv/orchestration}"
ORCH_SECRETS="${ORCH_SECRETS:-/run/orchestration/secrets}"
SELFTEST=0
SKIP_BUILD=0

for arg in "$@"; do
  case "$arg" in
    --selftest)   SELFTEST=1 ;;
    --skip-build) SKIP_BUILD=1 ;;
    --help|-h)    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)            echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# --- logging ---------------------------------------------------------------
# stdout stays terse; everything verbose goes to the log. The log path is
# printed first so it can be asked for by name if a step fails.

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_DIR="${ORCH_ROOT}/logs"
if ! mkdir -p "$LOG_DIR" 2>/dev/null; then
  LOG_DIR="${TMPDIR:-/tmp}"
fi
LOG="${LOG_DIR}/bootstrap-${STAMP}.log"
: > "$LOG" 2>/dev/null || LOG="/dev/null"

# Run a command with its output captured to the log, never to stdout.
log_run() { { echo "--- $* "; "$@" 2>&1; echo "--- exit=$?"; } >> "$LOG"; }
logged()  { { echo "--- $*"; "$@" 2>&1; } >> "$LOG" 2>&1; }

# --- single-instance lock --------------------------------------------------
# Two concurrent bootstraps would race on the image build and the worktrees.

LOCK="${LOG_DIR}/bootstrap.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "FAIL  another bootstrap is running (lock: ${LOCK})"
  echo "      if that is wrong, remove the lock directory and re-run"
  exit 1
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

echo "orchestration bootstrap -- $(date -u '+%Y-%m-%d %H:%M UTC') on $(hostname)"
echo "log: ${LOG}"
echo

# --- 1. runtime directories ------------------------------------------------
# mkdir -p is already idempotent; the ownership fix is what makes a re-run
# after an accidental sudo run converge instead of staying broken.

if mkdir -p "$ORCH_ROOT"/{jobs,worktrees,scratch,repos,logs} 2>/dev/null; then
  :
elif sudo -n mkdir -p "$ORCH_ROOT"/{jobs,worktrees,scratch,repos,logs} 2>/dev/null; then
  sudo -n chown -R "$(id -u):$(id -g)" "$ORCH_ROOT" 2>/dev/null
else
  fail "cannot create ${ORCH_ROOT} without sudo"
  info "run once: sudo ./scripts/root-setup.sh"
fi

if [ -w "$ORCH_ROOT" ]; then
  pass "runtime root ${ORCH_ROOT} ready"
else
  fail "runtime root ${ORCH_ROOT} is not writable by $(id -un)"
fi

# --- 2. python environment -------------------------------------------------
# The core is stdlib-only; the venv exists for the MCP server and pytest.

VENV="${HERE}/.venv"
if [ ! -x "${VENV}/bin/python" ]; then
  logged python3 -m venv "$VENV"
fi
if [ -x "${VENV}/bin/python" ]; then
  if "${VENV}/bin/python" -c "import mcp" 2>/dev/null; then
    pass "python environment ready (mcp already installed)"
  elif logged "${VENV}/bin/pip" install --quiet --upgrade "mcp>=2.0,<3" "pytest>=8.0"; then
    pass "python environment ready (installed mcp, pytest)"
  else
    fail "could not install python dependencies -- see ${LOG}"
  fi
else
  fail "could not create a virtualenv at ${VENV}"
fi

# --- 3. secrets ------------------------------------------------------------
# From 1Password into per-agent tmpfs directories. Each agent's container
# mounts only its own, read-only, so identities stay separated.

# A machine nobody is sitting at wants a 1Password service account rather
# than an interactive sign-in, so pick the token up from a file if one is
# there and the environment does not already carry it. Read, never printed,
# never copied anywhere.
if [ -z "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]; then
  for candidate in "${OP_TOKEN_FILE:-}" "${HOME}/.op-token" "${HERE}/.op-token"; do
    if [ -n "$candidate" ] && [ -r "$candidate" ]; then
      OP_SERVICE_ACCOUNT_TOKEN="$(tr -d '[:space:]' < "$candidate")"
      export OP_SERVICE_ACCOUNT_TOKEN
      info "using the 1Password service account token from ${candidate}"
      break
    fi
  done
fi

# Make one directory exist, be owned by us, and be writable -- repairing it
# where we can. mkdir -p alone is not enough: it succeeds silently on a
# directory that already exists but belongs to root, and then every write
# into it fails one confusing layer later.
ensure_dir() {
  local dir="$1"
  mkdir -p "$dir" 2>/dev/null || sudo -n mkdir -p "$dir" 2>/dev/null || return 1
  # A foreign owner is the only case that actually needs root. If it is
  # ours already, a bad mode is ours to fix, so do not reach for sudo.
  if [ ! -O "$dir" ]; then
    sudo -n chown "$(id -u):$(id -g)" "$dir" 2>/dev/null || return 1
  fi
  chmod 0700 "$dir" 2>/dev/null || true
  [ -w "$dir" ]
}

# Printed when we could not repair ownership ourselves. /run is tmpfs, so
# the sudo fix would be needed again after every reboot -- the tmpfiles
# rule is the one that actually ends it.
secrets_permission_help() {
  info "run: sudo ./scripts/root-setup.sh"
  info "that repairs the ownership and installs a systemd rule so /run/orchestration"
  info "comes back correctly after a reboot -- it is the only root step there is."
}

secrets_step() {
  if [ ! -d "$ORCH_SECRETS" ]; then
    mkdir -p "$ORCH_SECRETS" 2>/dev/null \
      || sudo -n mkdir -p "$ORCH_SECRETS" 2>/dev/null \
      || {
        fail "cannot create ${ORCH_SECRETS} without sudo"
        info "run: sudo mkdir -p ${ORCH_SECRETS} && sudo chown -R $(id -u):$(id -g) ${ORCH_SECRETS}"
        return
      }
    sudo -n chown "$(id -u):$(id -g)" "$ORCH_SECRETS" 2>/dev/null
  fi
  if ! ensure_dir "$ORCH_SECRETS"; then
    fail "${ORCH_SECRETS} is not writable by $(id -un)"
    secrets_permission_help
    return
  fi

  # Written only if the value is actually present, so a re-run without
  # `op run` leaves an existing credential alone rather than truncating it.
  write_secret() {
    local agent="$1" name="$2" value="$3"
    [ -n "$value" ] || return 1
    local dir="${ORCH_SECRETS}/${agent}"
    if ! ensure_dir "$dir"; then
      SECRET_DIR_UNWRITABLE="$dir"
      return 1
    fi
    local dest="${dir}/${name}"
    ( umask 077; printf '%s' "$value" > "${dest}.tmp" ) 2>/dev/null \
      && mv "${dest}.tmp" "$dest" \
      && chmod 0400 "$dest" \
      && return 0
    rm -f "${dest}.tmp" 2>/dev/null
    SECRET_DIR_UNWRITABLE="$dir"
    return 1
  }

  # Resolve each reference in .env individually rather than handing the
  # whole file to `op run`. Two reasons, both learned the hard way: the
  # old guard only ran when CLAUDE_CODE_OAUTH_TOKEN was absent, so an
  # already-exported token meant every *other* reference was silently
  # never resolved; and `op run` fails wholesale on one bad reference, so
  # a missing optional item took the rest down with it.
  resolve_from_vault() {
    have op || return 0
    [ -r "${HERE}/.env" ] || return 0
    [ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ] || return 0

    local var ref value unresolved=""
    for var in CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY GH_TOKEN \
               HERMES_API_KEY OPENAI_API_KEY DISCORD_BOT_TOKEN \
               OPENAI_WEBHOOK_SECRET AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; do
      # Anything already in the environment wins: `op run` may have put it
      # there, or a human may be overriding deliberately.
      [ -n "${!var:-}" ] && continue

      ref="$(sed -n "s/^[[:space:]]*${var}=//p" "${HERE}/.env" | tr -d '"'"'"'' | head -1)"
      case "$ref" in
        op://*) ;;
        *) continue ;;   # absent or commented out: not an error
      esac

      if value="$(op read "$ref" 2>>"$LOG")"; then
        export "${var}=${value}"
      else
        unresolved="${unresolved}${var} "
      fi
      unset value
    done

    if [ -n "$unresolved" ]; then
      # Named individually, because "some secret is missing" is not
      # something anyone can act on from a chat relay.
      info "not in the vault yet: ${unresolved}"
      info "  create the items named in .env, or comment out the refs you do not want"
    fi
  }
  resolve_from_vault

  local written=0 kept=0
  if write_secret claude-code oauth_token "${CLAUDE_CODE_OAUTH_TOKEN:-}"; then
    written=$((written + 1))
  elif [ -r "${ORCH_SECRETS}/claude-code/oauth_token" ]; then
    kept=$((kept + 1))
  fi
  if write_secret claude-code anthropic_api_key "${ANTHROPIC_API_KEY:-}"; then
    written=$((written + 1))
  elif [ -r "${ORCH_SECRETS}/claude-code/anthropic_api_key" ]; then
    kept=$((kept + 1))
  fi
  if write_secret claude-code github_token "${GH_TOKEN:-}"; then
    written=$((written + 1))
  elif [ -r "${ORCH_SECRETS}/claude-code/github_token" ]; then
    kept=$((kept + 1))
  fi
  if write_secret hermes api_key "${HERMES_API_KEY:-}"; then
    written=$((written + 1))
  elif [ -r "${ORCH_SECRETS}/hermes/api_key" ]; then
    kept=$((kept + 1))
  fi
  # The voice bridge is its own identity: the coding agents never see these
  # and it never sees theirs.
  if write_secret voice openai_api_key "${OPENAI_API_KEY:-}"; then
    written=$((written + 1))
  elif [ -r "${ORCH_SECRETS}/voice/openai_api_key" ]; then
    kept=$((kept + 1))
  fi
  if write_secret voice discord_bot_token "${DISCORD_BOT_TOKEN:-}"; then
    written=$((written + 1))
  elif [ -r "${ORCH_SECRETS}/voice/discord_bot_token" ]; then
    kept=$((kept + 1))
  fi
  if write_secret agentcore aws_access_key_id "${AWS_ACCESS_KEY_ID:-}"; then
    written=$((written + 1))
  elif [ -r "${ORCH_SECRETS}/agentcore/aws_access_key_id" ]; then
    kept=$((kept + 1))
  fi
  if write_secret agentcore aws_secret_access_key "${AWS_SECRET_ACCESS_KEY:-}"; then
    written=$((written + 1))
  elif [ -r "${ORCH_SECRETS}/agentcore/aws_secret_access_key" ]; then
    kept=$((kept + 1))
  fi
  if write_secret voice openai_webhook_secret "${OPENAI_WEBHOOK_SECRET:-}"; then
    written=$((written + 1))
  elif [ -r "${ORCH_SECRETS}/voice/openai_webhook_secret" ]; then
    kept=$((kept + 1))
  fi

  if [ -n "${SECRET_DIR_UNWRITABLE:-}" ]; then
    # Distinguish "you have no credential" from "we could not write the
    # credential we already resolved" -- they look identical downstream and
    # have completely different fixes.
    fail "resolved the credentials but could not write them: ${SECRET_DIR_UNWRITABLE} is not writable"
    info "owned by $(stat -c '%U:%G' "$SECRET_DIR_UNWRITABLE" 2>/dev/null || echo 'someone else'), you are $(id -un)"
    secrets_permission_help
  elif [ -r "${ORCH_SECRETS}/claude-code/oauth_token" ] \
     || [ -r "${ORCH_SECRETS}/claude-code/anthropic_api_key" ]; then
    pass "secrets in place (${written} written, ${kept} already present)"
    # The voice bridge is optional, so its absence is a note rather than a
    # failure -- but a silent absence is what sends someone hunting.
    if [ ! -r "${ORCH_SECRETS}/voice/openai_api_key" ] \
       || [ ! -r "${ORCH_SECRETS}/voice/discord_bot_token" ]; then
      info "voice bridge not configured yet (needs openai_api_key and discord_bot_token)"
    fi
  else
    fail "no Claude credential in ${ORCH_SECRETS}/claude-code/"
    info "expected oauth_token (from 'claude setup-token', which a human must run"
    info "at a terminal) or anthropic_api_key. Put one in the Agent vault under"
    info "the reference in .env.example, then re-run."
  fi
}
secrets_step

# --- 4. repositories -------------------------------------------------------
# Clone if absent, fetch if present. Never a reset: a fetch cannot destroy
# work sitting in a worktree.

repos_step() {
  local names
  names="$("${VENV}/bin/python" - <<'PY' 2>>"$LOG"
import sys
sys.path.insert(0, "src")
from orchestrator.registry import load
for name, repo in load().repos.items():
    print(f"{name}\t{repo.url}")
PY
)"
  if [ -z "$names" ]; then
    warn "no repos configured in config/repos.toml -- nothing to clone"
    return
  fi

  local ok=0 bad=0
  while IFS=$'\t' read -r name url; do
    [ -n "$name" ] || continue
    local dest="${ORCH_ROOT}/repos/${name}"
    if [ -d "${dest}/.git" ]; then
      if logged git -C "$dest" fetch --quiet --prune origin; then
        ok=$((ok + 1))
      else
        bad=$((bad + 1))
        info "could not fetch ${name} (offline? credentials?) -- see ${LOG}"
      fi
    elif logged git clone --quiet "$url" "$dest"; then
      ok=$((ok + 1))
    else
      bad=$((bad + 1))
      info "could not clone ${name} from ${url} -- see ${LOG}"
    fi
  done <<< "$names"

  if [ "$bad" -eq 0 ]; then
    pass "repositories ready (${ok} cloned or fetched)"
  else
    warn "repositories: ${ok} ready, ${bad} unavailable -- jobs needing those will fail"
  fi
}
cd "$HERE" && repos_step

# --- 5. image --------------------------------------------------------------
# Tagged with a hash of docker/, so an unchanged tree is a no-op rather than
# a rebuild. AGENT_UID must match the host user or bind-mounted files land
# wrong-owned; on native Linux there is no translation layer to save us.

IMAGE_TAG="$(find docker -type f -exec sha256sum {} + 2>/dev/null \
  | sort | sha256sum | cut -c1-12)"
[ -n "$IMAGE_TAG" ] || IMAGE_TAG="latest"
AGENT_UID="$(id -u)"
AGENT_GID="$(id -g)"
IMAGE="orchestration/claude-code:${IMAGE_TAG}"
# compose reads all four out of the environment.
export IMAGE_TAG AGENT_UID AGENT_GID ORCH_ROOT ORCH_SECRETS

if [ "$SKIP_BUILD" = 1 ]; then
  info "image build skipped (--skip-build)"
elif ! have docker || ! docker info >/dev/null 2>&1; then
  fail "docker daemon unreachable -- run ./scripts/preflight.sh"
elif docker image inspect "$IMAGE" >/dev/null 2>&1; then
  pass "image ${IMAGE} already built (docker/ unchanged)"
elif logged docker compose -f docker/docker-compose.yml build; then
  pass "image ${IMAGE} built"
else
  fail "image build failed -- see ${LOG}"
fi

# --- 6. containers ---------------------------------------------------------
# `up -d` converges: an already-running container with the right config is
# left alone, a stale one is recreated.

if have docker && docker info >/dev/null 2>&1; then
  if logged docker compose -f docker/docker-compose.yml up -d --remove-orphans; then
    running="$(docker compose -f docker/docker-compose.yml ps --services --filter status=running 2>/dev/null | wc -l | tr -d ' ')"
    pass "containers up (${running} running)"
  else
    fail "docker compose up failed -- see ${LOG}"
  fi

  # The container is useless if the toolchain is not actually in it.
  if out="$(docker exec orch-claude-code claude --version 2>&1)"; then
    pass "claude code in container ($(printf '%s' "$out" | clip))"
  else
    fail "claude not runnable in orch-claude-code: $(printf '%s' "$out" | clip)"
  fi

  if docker exec orch-claude-code test -r /home/agent/.claude/settings.json 2>/dev/null; then
    pass "settings.json baked into the image"
  else
    warn "settings.json missing in container -- the /home/agent volume may predate it"
    info "fix: docker compose -f docker/docker-compose.yml down -v && re-run"
  fi

  # The identical-path bind is what makes git worktrees resolve inside the
  # container. Verified rather than assumed, because it fails silently.
  if docker exec orch-claude-code test -d "${ORCH_ROOT}/worktrees" 2>/dev/null; then
    pass "identical-path bind ${ORCH_ROOT} visible in container"
  else
    fail "${ORCH_ROOT} not visible inside the container -- worktrees will not resolve"
  fi

  # A file written by the container must be owned by the host user.
  probe="${ORCH_ROOT}/scratch/.uid-probe"
  if docker exec orch-claude-code sh -c "touch '${probe}'" 2>/dev/null; then
    owner="$(stat -c '%u' "$probe" 2>/dev/null || stat -f '%u' "$probe" 2>/dev/null)"
    rm -f "$probe"
    if [ "$owner" = "$(id -u)" ]; then
      pass "container writes files as uid $(id -u) (bind-mount ownership correct)"
    else
      fail "container writes as uid ${owner}, host user is $(id -u) -- rebuild with AGENT_UID=$(id -u)"
    fi
  else
    warn "could not test bind-mount ownership"
  fi
fi

# --- 7. tests --------------------------------------------------------------

if [ -x "${VENV}/bin/python" ]; then
  if logged "${VENV}/bin/python" -m pytest -q; then
    pass "test suite passed"
  else
    fail "test suite failed -- see ${LOG}"
  fi
fi

# --- 8. self-test: prove ask/check actually work ---------------------------
# The whole point of phase one. Uses the container agent, since the http
# agent's endpoint is still a stub.

if [ "$SELFTEST" = 1 ]; then
  job="$("${HERE}/bin/orchestrate" --json ask claude-code \
        "Reply with the single word: ready. Then call: narrate \"self-test complete\"" \
        2>>"$LOG" | "${VENV}/bin/python" -c 'import json,sys; print(json.load(sys.stdin).get("job_id",""))' 2>/dev/null)"
  if [ -z "$job" ]; then
    fail "self-test: ask() did not return a job id -- see ${LOG}"
  else
    info "self-test job: ${job}"
    state=""
    for _ in $(seq 1 120); do
      state="$("${HERE}/bin/orchestrate" --json check "$job" \
        | "${VENV}/bin/python" -c 'import json,sys; print(json.load(sys.stdin).get("state",""))' 2>/dev/null)"
      case "$state" in done|failed|blocked|awaiting_input) break ;; esac
      sleep 5
    done
    if [ "$state" = "done" ]; then
      pass "self-test: ask/check worked end to end (job ${job})"
      "${HERE}/bin/orchestrate" check "$job" | sed 's/^/      /'
    else
      fail "self-test: job ${job} ended ${state:-unknown}"
      "${HERE}/bin/orchestrate" check "$job" --tail 20 | sed 's/^/      /'
    fi
  fi
fi

# --- 9. how to wire the MCP client -----------------------------------------
# Printed, never written: this script does not edit your client's config.

echo
echo "MCP client config (stdio; add this to your OpenAI Agents SDK client):"
cat <<JSON
      {
        "mcpServers": {
          "orchestrator": {
            "command": "${HERE}/bin/orchestrate-mcp",
            "args": []
          }
        }
      }
JSON

echo
summarise \
  "incomplete; fix the FAIL lines above and re-run (it is safe to re-run)" \
  "up, with caveats -- read the WARN lines" \
  "ready; try: ./bin/orchestrate ask claude-code \"...\""
