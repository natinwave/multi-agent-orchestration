"""Scrub secret values out of anything leaving the supervisor.

Everything ``check()`` and its siblings return passes through
:meth:`Redactor.scrub_obj`. That output goes into a cloud model's context and
is then spoken out loud, so this module is the last line of defence and is
built to fail closed: when in doubt, redact.

Three layers, applied in order:

1. Registered literal values -- secrets the supervisor actually knows (a
   secret file's contents, a named environment variable, an ``op read``
   result). Exact matches, plus base64 and percent-encoded forms.
2. Pattern rules -- known credential shapes (``sk-ant-``, ``ghp_``, JWTs,
   PEM blocks, ``Authorization: Bearer``, ``key = value`` assignments).
3. Entropy fallback -- any long opaque run whose Shannon entropy is high
   enough to look like a credential we have no pattern for.

Layer 3 is where false positives live, so it carries an allowlist for the
things that legitimately look random in agent output: git SHAs, UUIDs, ISO
timestamps, filesystem paths. See ``tests/test_redaction.py``.

This module documents every credential shape it matches, so it trips its own
scanner by construction. secret-scan: allow
"""

from __future__ import annotations

import base64
import math
import re
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Redactor", "Rule", "DEFAULT_RULES", "shannon_entropy"]

# A registered value shorter than this is too likely to be a common word to
# search for blindly. Real credentials are comfortably longer.
MIN_REGISTERED_LEN = 8


@dataclass(frozen=True)
class Rule:
    """A named regex whose match (or one capture group) is a secret."""

    name: str
    pattern: re.Pattern[str]
    # When set, redact only this group rather than the whole match, so the
    # surrounding context ("Authorization: Bearer") stays readable.
    group: int | None = None


def _r(name: str, pattern: str, group: int | None = None, flags: int = 0) -> Rule:
    return Rule(name, re.compile(pattern, flags), group)


DEFAULT_RULES: tuple[Rule, ...] = (
    # -- provider-specific shapes, most distinctive first ------------------
    _r("anthropic-key", r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    _r("anthropic-oauth", r"sk-ant-oat[0-9]{2}-[A-Za-z0-9_\-]{16,}"),
    _r("openai-key", r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    _r("github-token", r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    _r("github-pat", r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    _r("aws-access-key", r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{12,}"),
    _r("google-api-key", r"\bAIza[0-9A-Za-z_\-]{35}"),
    _r("slack-token", r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    _r("stripe-key", r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    _r("npm-token", r"\bnpm_[A-Za-z0-9]{30,}"),
    _r("1password-token", r"\bops_[A-Za-z0-9_\-\.]{30,}"),
    # A JWT: three base64url segments. Also catches session cookies.
    _r("jwt", r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    # -- structural shapes --------------------------------------------------
    _r(
        "private-key",
        r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----",
        flags=re.DOTALL,
    ),
    _r("authorization-header", r"(?i)\b(?:authorization|proxy-authorization)\s*[:=]\s*\S+", None),
    _r("bearer", r"(?i)\bbearer\s+([A-Za-z0-9_\-\.=+/]{12,})", 1),
    # A URL carrying inline credentials: https://user:pw@host
    _r("url-credentials", r"(?i)\b([a-z][a-z0-9+.\-]*)://[^/\s:@]+:([^/\s@]+)@", 2),
    # Generic assignment. Deliberately broad -- this is the one that catches
    # an agent echoing its own environment.
    _r(
        "assignment",
        r"(?i)\b(?:api[_\-]?key|secret[_\-]?key|access[_\-]?token|auth[_\-]?token"
        r"|client[_\-]?secret|password|passwd|pwd|token|secret|credential)s?"
        r"\s*[:=]\s*[\"']?([^\s\"',;]{8,})",
        1,
    ),
)

# Things that are long and look random but are not secrets. Checked before the
# entropy fallback fires. Anchored: the whole candidate must match.
#
# Every entry here is a hole in layer 3, so each is deliberately narrow and
# earns its place by appearing constantly in real agent output. Redacting
# these would both destroy the narration and teach the voice model that
# "[redacted]" is noise to be ignored.
_ALLOWLIST: tuple[re.Pattern[str], ...] = (
    # Git object ids and md5/sha1/sha256 digests. Single-case hex only.
    re.compile(r"[0-9a-f]{7,64}\Z"),
    re.compile(r"[0-9A-F]{7,64}\Z"),
    re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"),
    re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*\Z"),  # ISO timestamps
    # Filesystem paths, module names, repo slugs. The leading slash is
    # optional because the opaque-run scanner includes "/" (base64 uses it),
    # so an absolute path arrives here with its slash attached. "+" and "="
    # are excluded from the segments, so a base64 blob cannot pass as a path.
    re.compile(r"/?[\w.\-]+(?:/[\w.\-]+)+/?\Z"),
    re.compile(r"v?\d+(?:\.\d+){1,3}(?:[-+][\w.]+)?\Z"),  # semver
    re.compile(r"\d+\Z"),
    # Words joined by _ or - : test names, branch names, container names.
    re.compile(r"(?:[A-Za-z0-9]+[_\-]){2,}[A-Za-z0-9]+\Z"),
    # A single unbroken run of one-case letters, no digits: a long English
    # word or an identifier. Credentials essentially always mix case or
    # include digits, so requiring that diversity costs us very little.
    re.compile(r"[a-z]+\Z"),
    re.compile(r"[A-Z]+\Z"),
)

# Candidate for the entropy check: an unbroken opaque run. "=" is allowed
# only as trailing base64 padding -- letting it sit mid-string made every
# `SOME_LONG_CONFIG_NAME=value` in a Dockerfile look like a credential.
# A real assignment whose value is secret is caught by the assignment rule
# in layer 2, which runs first.
_OPAQUE = re.compile(r"[A-Za-z0-9+/_\-]{32,}={0,2}")

DEFAULT_ENTROPY_THRESHOLD = 3.6
PLACEHOLDER = "[redacted]"


def shannon_entropy(s: str) -> float:
    """Bits of entropy per character. A random base64 run scores ~5.5-6.0."""
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _variants(value: str) -> list[str]:
    """A secret's plausible encodings, longest first so nesting is safe."""
    out = {value}
    try:
        out.add(base64.b64encode(value.encode()).decode().rstrip("="))
    except Exception:  # pragma: no cover - encode of str never fails
        pass
    out.add(urllib.parse.quote(value, safe=""))
    out.add(urllib.parse.quote_plus(value))
    # A value carried through JSON gains escaped slashes.
    out.add(value.replace("/", r"\/"))
    return sorted((v for v in out if len(v) >= MIN_REGISTERED_LEN), key=len, reverse=True)


@dataclass
class Redactor:
    """Scrubs known and suspected secrets out of text.

    Registered values are matched literally; everything else is caught by
    pattern or by entropy. Construct one per supervisor process and register
    whatever secrets that process can see.
    """

    rules: Sequence[Rule] = DEFAULT_RULES
    entropy_threshold: float = DEFAULT_ENTROPY_THRESHOLD
    entropy_fallback: bool = True
    _values: dict[str, str] = field(default_factory=dict, repr=False)

    # -- registration -------------------------------------------------------

    def register_value(self, name: str, value: str | None) -> bool:
        """Register a literal secret. Returns whether it was accepted."""
        if not value:
            return False
        value = value.strip()
        if len(value) < MIN_REGISTERED_LEN:
            return False
        self._values[name] = value
        return True

    def register_env(self, names: Iterable[str], environ: Mapping[str, str]) -> list[str]:
        """Register the values of the named environment variables."""
        return [n for n in names if self.register_value(n, environ.get(n))]

    def register_file(self, name: str, path: Path) -> bool:
        """Register a secret file's contents (as written by ``op read``)."""
        try:
            return self.register_value(name, path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return False

    def register_dir(self, path: Path) -> list[str]:
        """Register every regular file in a per-agent secrets directory."""
        registered = []
        try:
            entries = sorted(p for p in path.iterdir() if p.is_file())
        except OSError:
            return registered
        for p in entries:
            if self.register_file(p.name, p):
                registered.append(p.name)
        return registered

    @property
    def registered(self) -> tuple[str, ...]:
        return tuple(self._values)

    # -- scrubbing ----------------------------------------------------------

    def scrub(self, text: str) -> str:
        """Return *text* with every known or suspected secret replaced."""
        if not text:
            return text

        # Layer 1: literal values. Longest first, so a secret that contains
        # another registered secret is replaced as a whole.
        for name, value in sorted(self._values.items(), key=lambda kv: -len(kv[1])):
            marker = f"[redacted:{name}]"
            for variant in _variants(value):
                if variant in text:
                    text = text.replace(variant, marker)

        # Layer 2: known credential shapes.
        for rule in self.rules:
            text = self._apply_rule(text, rule)

        # Layer 3: anything else that looks like a credential.
        if self.entropy_fallback:
            text = _OPAQUE.sub(self._maybe_entropy, text)

        return text

    def scrub_obj(self, obj: object) -> object:
        """Deep-scrub a JSON-shaped structure. Dict keys are scrubbed too."""
        if isinstance(obj, str):
            return self.scrub(obj)
        if isinstance(obj, Mapping):
            return {self.scrub(str(k)): self.scrub_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            scrubbed = [self.scrub_obj(v) for v in obj]
            return type(obj)(scrubbed) if isinstance(obj, tuple) else scrubbed
        return obj

    # -- internals ----------------------------------------------------------

    def _apply_rule(self, text: str, rule: Rule) -> str:
        marker = f"[redacted:{rule.name}]"

        def repl(m: re.Match[str]) -> str:
            if rule.group is None:
                return marker
            secret = m.group(rule.group)
            if not secret:
                return m.group(0)
            # Keep the readable context, replace only the credential.
            start, end = m.span(rule.group)
            return m.group(0)[: start - m.start()] + marker + m.group(0)[end - m.start() :]

        return rule.pattern.sub(repl, text)

    def _maybe_entropy(self, m: re.Match[str]) -> str:
        candidate = m.group(0)
        if any(p.fullmatch(candidate) for p in _ALLOWLIST):
            return candidate
        if shannon_entropy(candidate) < self.entropy_threshold:
            return candidate
        return "[redacted:high-entropy]"
