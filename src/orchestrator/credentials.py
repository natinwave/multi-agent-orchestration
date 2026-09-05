"""Delegating credentials to agents, one grant at a time.

The 1Password "Agent" vault is the pool of credentials you are *willing* to
share. Nothing in it reaches an agent until you say so out loud, and a
grant is scoped to one agent identity -- optionally to one job.

Three properties make this cheap rather than elaborate:

- ``/run/orchestration/secrets/<agent>/`` is *already* bind-mounted into
  that agent's container read-only, so a granted file appears immediately.
  No container restart, no compose change.
- Revoking is ``unlink``. The mount is per-agent by construction, so a
  grant to one agent is invisible to every other.
- Values are never stored here, never logged, and never returned. This
  module deals in titles, filenames and environment variable names.

The honest limit: revoking stops *future* reads. A job that already read
the value still holds it in memory until it exits. If you need hard
expiry, end the job.

secret-scan: allow
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .registry import normalise

__all__ = [
    "CredentialStore",
    "Grant",
    "Available",
    "CredentialError",
    "UnknownCredential",
    "AmbiguousCredential",
    "OpError",
]

# Written by bootstrap and owned by the agent's identity, not by a grant.
# A grant may not overwrite these: doing so would replace the agent's own
# Claude credential with something else.
RESERVED = frozenset(
    {"oauth_token", "anthropic_api_key", "github_token", "openai_api_key", "discord_bot_token", "openai_webhook_secret"}
)

# A granted file becomes an environment variable, so its name has to be a
# valid identifier.
NAME_RE = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")

# Preferred field labels when an item has more than one concealed field.
FIELD_PREFERENCE = ("password", "credential", "token", "api_key", "apikey", "secret")


class CredentialError(RuntimeError):
    """Something the caller can act on. Safe to say out loud."""


class OpError(CredentialError):
    """The 1Password CLI failed. Never carries a value."""


class UnknownCredential(CredentialError):
    def __init__(self, query: str, known: list[str]) -> None:
        super().__init__(f"no credential matching {query!r}")
        self.query, self.known = query, known


class AmbiguousCredential(CredentialError):
    """Matched more than one item. Ask, do not guess."""

    def __init__(self, query: str, candidates: list[str]) -> None:
        super().__init__(f"{query!r} matches more than one credential")
        self.query, self.candidates = query, candidates


@dataclass(frozen=True)
class Available:
    """An item in the vault. Title only -- never a value."""

    title: str
    name: str  # the filename/env-var stem a grant would use

    @property
    def env_var(self) -> str:
        return self.name.upper()


@dataclass(frozen=True)
class Grant:
    """A credential currently exposed to one agent."""

    agent: str
    name: str
    title: str | None = None
    job_id: str | None = None
    granted_at: str | None = None

    @property
    def env_var(self) -> str:
        return self.name.upper()


def slugify(title: str) -> str:
    """Vault title to a filename stem: "Staging DB Password" -> staging_db_password."""
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    if not slug or not slug[0].isalpha():
        slug = f"c_{slug}" if slug else ""
    return slug[:64]


def _run_op(argv: list[str]) -> str:
    """Invoke the 1Password CLI.

    Errors deliberately drop stdout: an `op read` that partially succeeded
    could otherwise put a value into an exception message, which would
    travel straight into a log.
    """
    try:
        proc = subprocess.run(
            ["op", *argv], capture_output=True, text=True, timeout=30, check=False
        )
    except FileNotFoundError:
        raise OpError("the 1Password CLI (op) is not installed on this host") from None
    except subprocess.TimeoutExpired:
        raise OpError("the 1Password CLI timed out") from None
    if proc.returncode != 0:
        raise OpError(f"op {argv[0]} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


@dataclass
class CredentialStore:
    """Reads the vault and materialises grants into per-agent directories."""

    secrets_root: Path
    vault: str = "Agent"
    audit_log: Path | None = None
    # Injectable so the whole module is testable without a vault or a
    # signed-in CLI. Left as None rather than defaulting to _run_op, so the
    # function is looked up when it is called rather than bound at import
    # -- a default here would capture the original and could not be
    # substituted afterwards.
    op_runner: Callable[[list[str]], str] | None = None

    def _op(self, argv: list[str]) -> str:
        return (self.op_runner or _run_op)(argv)

    # -- reading the vault --------------------------------------------------

    def available(self) -> list[Available]:
        """Titles in the vault. This is the menu, not the contents."""
        raw = self._op(["item", "list", "--vault", self.vault, "--format", "json"])
        try:
            items = json.loads(raw or "[]")
        except json.JSONDecodeError:
            raise OpError(f"could not parse the item list for vault {self.vault!r}") from None

        out = []
        for item in items:
            title = (item or {}).get("title", "").strip()
            name = slugify(title)
            if title and NAME_RE.fullmatch(name):
                out.append(Available(title=title, name=name))
        return sorted(out, key=lambda a: a.title.lower())

    def resolve(self, query: str) -> Available:
        """Match spoken words against item titles.

        Exact title, then exact slug, then token overlap -- the same ladder
        repos use. A tie raises rather than guessing, because guessing here
        hands an agent the wrong credential.
        """
        items = self.available()
        q = query.strip().lower()

        for item in items:
            if q == item.title.lower() or q == item.name:
                return item

        wanted = set(normalise(query))
        if not wanted:
            raise UnknownCredential(query, [i.title for i in items])
        hits = [i for i in items if wanted <= set(normalise(i.title))]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise AmbiguousCredential(query, sorted(i.title for i in hits))
        raise UnknownCredential(query, [i.title for i in items])

    def _read_value(self, item: Available) -> str:
        """Fetch one item's secret. The only place a value exists here."""
        raw = self._op(
            ["item", "get", item.title, "--vault", self.vault, "--format", "json", "--reveal"]
        )
        try:
            fields = json.loads(raw).get("fields", []) or []
        except (json.JSONDecodeError, AttributeError):
            raise OpError(f"could not parse item {item.title!r}") from None

        concealed = [
            f for f in fields if (f.get("type") or "").upper() == "CONCEALED" and f.get("value")
        ]
        if not concealed:
            raise OpError(f"item {item.title!r} has no concealed field to grant")

        for preferred in FIELD_PREFERENCE:
            for field in concealed:
                label = (field.get("label") or field.get("id") or "").lower()
                if label == preferred:
                    return str(field["value"])
        return str(concealed[0]["value"])

    # -- granting -----------------------------------------------------------

    def grant(self, agent: str, query: str, job_id: str | None = None) -> Grant:
        """Expose one credential to one agent.

        The file lands in a directory already mounted read-only into that
        agent's container, so it is visible to the next job immediately.
        """
        item = self.resolve(query)
        if item.name in RESERVED:
            raise CredentialError(
                f"{item.title!r} would overwrite the agent's own {item.name} -- refused"
            )

        agent_dir = self.secrets_root / agent
        agent_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(agent_dir, 0o700)

        value = self._read_value(item)
        dest = agent_dir / item.name
        tmp = dest.with_suffix(".tmp")
        # umask-independent: create the file unreadable to anyone else from
        # the moment it exists, then swap it in atomically.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o400)
        try:
            os.write(fd, value.encode())
        finally:
            os.close(fd)
        os.replace(tmp, dest)
        del value

        granted = Grant(
            agent=agent,
            name=item.name,
            title=item.title,
            job_id=job_id,
            granted_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self._write_meta(granted)
        self._audit("grant", granted)
        return granted

    def revoke(self, agent: str, name: str) -> bool:
        """Remove a granted credential. Returns whether anything was there.

        This stops future reads. A job already holding the value keeps it
        until it exits -- end the job if you need that back.
        """
        path = self.secrets_root / agent / name
        existed = path.is_file()
        if existed:
            path.unlink()
        meta = self._read_meta()
        meta.pop(f"{agent}/{name}", None)
        self._write_meta_all(meta)
        if existed:
            self._audit("revoke", Grant(agent=agent, name=name))
        return existed

    def revoke_for_job(self, job_id: str) -> list[Grant]:
        """Drop every grant tied to a finished job."""
        dropped = []
        for grant in self.grants():
            if grant.job_id and grant.job_id == job_id:
                if self.revoke(grant.agent, grant.name):
                    dropped.append(grant)
        return dropped

    def grants(self) -> list[Grant]:
        """What is live right now.

        The filesystem is the source of truth -- a file present in an
        agent's directory *is* an exposed credential, whatever the metadata
        says. Metadata only enriches it.
        """
        meta = self._read_meta()
        live: list[Grant] = []
        if not self.secrets_root.is_dir():
            return live
        for agent_dir in sorted(p for p in self.secrets_root.iterdir() if p.is_dir()):
            for path in sorted(p for p in agent_dir.iterdir() if p.is_file()):
                if path.name in RESERVED or path.name.startswith("."):
                    continue
                record = meta.get(f"{agent_dir.name}/{path.name}", {})
                live.append(
                    Grant(
                        agent=agent_dir.name,
                        name=path.name,
                        title=record.get("title"),
                        job_id=record.get("job_id"),
                        granted_at=record.get("granted_at"),
                    )
                )
        return live

    # -- metadata and audit -------------------------------------------------
    # Both live outside the mounted directories, so a container cannot read
    # what else has been granted, to itself or to anyone.

    @property
    def _meta_path(self) -> Path:
        return self.secrets_root / ".grants.json"

    def _read_meta(self) -> dict:
        try:
            return json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_meta_all(self, meta: dict) -> None:
        self._meta_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._meta_path)

    def _write_meta(self, grant: Grant) -> None:
        meta = self._read_meta()
        meta[f"{grant.agent}/{grant.name}"] = {
            "title": grant.title,
            "job_id": grant.job_id,
            "granted_at": grant.granted_at,
        }
        self._write_meta_all(meta)

    def _audit(self, action: str, grant: Grant) -> None:
        """Append-only record of what was handed out and when.

        "What does that agent have access to, and since when" is a question
        you will eventually ask, usually in a hurry.
        """
        if self.audit_log is None:
            return
        line = "\t".join(
            [
                datetime.now(UTC).isoformat(timespec="seconds"),
                action,
                grant.agent,
                grant.name,
                grant.title or "-",
                grant.job_id or "-",
            ]
        )
        try:
            self.audit_log.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.audit_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (line + "\n").encode())
            finally:
                os.close(fd)
        except OSError:
            pass  # never let an audit write failure block a revoke
