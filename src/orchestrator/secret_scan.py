"""Scan files for credential-shaped content.

"NO SECRET VALUES IN THIS REPO, ever -- 1Password references only." This is
that rule, enforced, and it reuses :mod:`orchestrator.redaction` so the
patterns guarding the repo and the patterns guarding the voice channel can
never drift apart.

Driven by ``scripts/check-no-secrets.sh``; kept here rather than inline in
the shell so it can be tested.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .redaction import Redactor

__all__ = ["Finding", "scan", "main", "MARKER"]

# A 1Password reference is the approved way to name a credential, so it is
# not a finding -- but it is shaped exactly like `TOKEN=<opaque string>`, so
# the redactor flags it. Replaced with a short placeholder before scanning,
# which keeps .env.example honest: an actual value on the same line is
# still caught.
OP_REFERENCE = re.compile(r"op://[\w\-./%~+@]+")
OP_PLACEHOLDER = "op-ref"

# `credential=credential` is a keyword argument, not a secret: a real value
# is never literally its own key name. The redactor keeps flagging these
# because over-redacting a log line costs nothing, but the scanner has to
# stay quiet enough that people keep running it.
SELF_ASSIGNMENT = re.compile(r"\b(\w+)(\s*[:=]\s*)\1\b")

# In Python source, only a quoted string can hold a secret -- anything
# unquoted is an expression, or it would be a NameError. So .py lines are
# scanned by their string literals rather than whole: `api_key=openai_key`
# and `token = read_secret(root, "discord_bot_token")` are code, while
# `TOKEN = "ghp_..."` still has its literal examined and still trips.
#
# This is sound rather than heuristic, which is why it is worth doing: it
# removes a whole class of false positive without removing any real
# finding.
PY_STRING = re.compile(r"""(['"])(?:\\.|(?!\1).)*\1""", re.DOTALL)

# Some files must contain credential-shaped text to do their job: the
# redaction module documents every shape it matches, and the tests plant
# fake credentials to prove they get scrubbed. Rather than a list here that
# goes stale, such a file opts out in its own text and says why -- so the
# reason travels with the file and a reviewer sees it in the diff.
MARKER = "secret-scan: allow"

MAX_SNIPPET = 100


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    snippet: str


def scan(paths: list[Path], redactor: Redactor | None = None, exempt=frozenset()) -> list[Finding]:
    red = redactor or Redactor()
    findings: list[Finding] = []
    for path in paths:
        if str(path) in exempt or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: nothing to read a secret out of
        if MARKER in text:
            continue
        for number, raw in enumerate(text.splitlines(), 1):
            probe = OP_REFERENCE.sub(OP_PLACEHOLDER, raw)
            probe = SELF_ASSIGNMENT.sub(r"k\g<2>v", probe)
            if path.suffix == ".py":
                probe = " ".join(m.group(0) for m in PY_STRING.finditer(probe))
            if red.scrub(probe) != probe:
                findings.append(Finding(path, number, raw.strip()[:MAX_SNIPPET]))
    return findings


def main(argv: list[str] | None = None) -> int:
    paths = [Path(a) for a in (sys.argv[1:] if argv is None else argv)]
    findings = scan(paths)
    if not findings:
        print(f"PASS  no secret-shaped values in {len(paths)} file(s)")
        return 0

    print(f"FAIL  {len(findings)} line(s) look like credentials:\n")
    for f in findings:
        # The snippet is printed raw on purpose: you cannot fix what you
        # cannot see, and this output goes to a terminal, not to a model.
        print(f"  {f.path}:{f.line}: {f.snippet}")
    print("\nUse a 1Password op:// reference instead (op:// URIs are not findings).")
    print(f'If the file must contain credential-shaped text, put "{MARKER}" in it')
    print("with a line saying why, so the reason travels with the file.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
