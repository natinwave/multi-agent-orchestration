#!/usr/bin/env bash
# Fail if anything in the working tree looks like a credential.
#
# "NO SECRET VALUES IN THIS REPO, ever -- 1Password references only." The
# patterns come from the supervisor's own redaction module, so the rule
# protecting the repo and the rule protecting the voice channel stay in step.
#
#   ./scripts/check-no-secrets.sh            # tracked files
#   ./scripts/check-no-secrets.sh --staged   # what you are about to commit
#
# To enforce it on every commit:
#   ln -s ../../scripts/check-no-secrets.sh .git/hooks/pre-commit
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE" || exit 2

python="${HERE}/.venv/bin/python"
[ -x "$python" ] || python="$(command -v python3)"

if [ "${1:-}" = "--staged" ]; then
  mapfile -t files < <(git diff --cached --name-only --diff-filter=ACM)
else
  mapfile -t files < <(git ls-files)
fi

if [ "${#files[@]}" -eq 0 ]; then
  echo "PASS  nothing to scan"
  exit 0
fi

PYTHONPATH="${HERE}/src" exec "$python" -m orchestrator.secret_scan "${files[@]}"
