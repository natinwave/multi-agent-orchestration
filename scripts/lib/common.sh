# shellcheck shell=bash
# Shared output helpers. Sourced, not executed.
#
# Both scripts' stdout is relayed to a person as chat text, so it is fixed
# width, one line per check, and free of progress spinners and colour codes
# that turn into noise once pasted.

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

pass() { PASS_COUNT=$((PASS_COUNT + 1)); printf 'PASS  %s\n' "$*"; }
warn() { WARN_COUNT=$((WARN_COUNT + 1)); printf 'WARN  %s\n' "$*"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); printf 'FAIL  %s\n' "$*"; }
info() { printf '      %s\n' "$*"; }

# A summary line that can be read on its own, because it is often the only
# line that gets quoted back.
# summarise <fail-verdict> <warn-verdict> <pass-verdict>
summarise() {
  local verdict
  if [ "$FAIL_COUNT" -gt 0 ]; then
    verdict="$1"
  elif [ "$WARN_COUNT" -gt 0 ]; then
    verdict="$2"
  else
    verdict="$3"
  fi
  printf '\nSUMMARY: %d pass, %d warn, %d fail -- %s\n' \
    "$PASS_COUNT" "$WARN_COUNT" "$FAIL_COUNT" "$verdict"
  [ "$FAIL_COUNT" -eq 0 ]
}

# Truncate anything captured from a subcommand: an unbounded error message
# would swamp the chat relay.
clip() { tr '\n' ' ' | tr -s ' ' | cut -c1-140 | sed 's/[[:space:]]*$//'; }

have() { command -v "$1" >/dev/null 2>&1; }
