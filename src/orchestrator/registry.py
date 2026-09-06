"""The agent and repo registry, loaded from TOML.

Adding a backend is a config change. Nothing in this module knows what
``claude-code`` is; it knows there is an agent whose ``type`` is
``container``, and the backend layer takes it from there.

The one piece of real logic here is repo resolution. ``ask()`` accepts a
free-text repo string because the caller is eventually a voice model saying
"the kiln one", so a name is matched against aliases and then loosely
against tokens -- and when that is ambiguous it raises rather than guessing.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .state import JobState  # noqa: F401  (re-exported for config consumers)

__all__ = [
    "Agent",
    "Repo",
    "Config",
    "load",
    "normalise",
    "default_config_dir",
    "CONFIG_DIR_ENV",
    "ROOT_ENV",
    "SECRETS_ENV",
    "AmbiguousRepo",
    "UnknownRepo",
    "UnknownAgent",
    "ConfigError",
]

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

# The runner is a separate process spawned by the supervisor, so it needs to
# be told which registry the supervisor was using. An environment variable
# keeps that out of meta.json, where it would go stale if the repo moved.
CONFIG_DIR_ENV = "ORCH_CONFIG_DIR"
ROOT_ENV = "ORCH_ROOT"
SECRETS_ENV = "ORCH_SECRETS"  # the name bootstrap.sh already uses


def default_config_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get(CONFIG_DIR_ENV) or DEFAULT_CONFIG_DIR)


class ConfigError(ValueError):
    """The registry on disk is not usable."""


class UnknownAgent(KeyError):
    def __init__(self, name: str, known: list[str]) -> None:
        super().__init__(name)
        self.name, self.known = name, known


class UnknownRepo(KeyError):
    def __init__(self, query: str, known: list[str]) -> None:
        super().__init__(query)
        self.query, self.known = query, known


class AmbiguousRepo(KeyError):
    """The spoken name matched more than one repo. Ask, do not guess."""

    def __init__(self, query: str, candidates: list[str]) -> None:
        super().__init__(query)
        self.query, self.candidates = query, candidates


@dataclass(frozen=True)
class Agent:
    name: str
    type: str
    description: str = ""
    # container backend
    container: str | None = None
    command: tuple[str, ...] = ()
    # How this particular CLI is told to start a session, resume one, and
    # take a system prompt. Config rather than code, because every agentic
    # CLI spells these differently and hardcoding one tool's flags in the
    # backend made "add an agent" a code change for anything but a second
    # Claude Code. Placeholders: {session_id}, {agent_prompt}.
    session_flags: tuple[str, ...] = ()
    resume_flags: tuple[str, ...] = ()
    system_prompt_flags: tuple[str, ...] = ()
    # http_openai backend
    base_url: str | None = None
    model: str | None = None
    api_key_file: str | None = None
    # shared
    secrets_dir: str | None = None
    default_repo: str | None = None
    needs_repo: bool = False
    timeout_seconds: int = 3600
    max_tokens: int = 2048


    @property
    def can_resume(self) -> bool:
        """Whether reply() can continue this agent's work.

        A CLI with no resume flag is not broken -- it simply has no
        sessions, and a parked job has to be restarted instead.
        """
        return bool(self.resume_flags)


@dataclass(frozen=True)
class Repo:
    name: str
    url: str
    base_ref: str = "main"
    aliases: tuple[str, ...] = ()


def normalise(s: str) -> list[str]:
    """Reduce spoken text to comparable tokens - lowercase, filler dropped."""
    words = re.findall(r"[a-z0-9]+", s.lower())
    return [w for w in words if w not in {"the", "a", "an", "one", "repo", "project"}]


@dataclass
class Config:
    agents: dict[str, Agent]
    repos: dict[str, Repo]
    root: Path
    config_dir: Path = DEFAULT_CONFIG_DIR
    default_narration_lines: int = 3
    max_narration_lines: int = 20
    max_tail_lines: int = 200
    entropy_threshold: float = 3.6
    entropy_fallback: bool = True
    scrub_env: tuple[str, ...] = ()
    retain_days: int = 14
    vault: str = "Agent"
    secrets_root: Path = Path("/run/orchestration/secrets")
    _repo_index: dict[str, set[str]] = field(default_factory=dict, repr=False)

    # -- paths --------------------------------------------------------------

    @property
    def jobs_dir(self) -> Path:
        return self.root / "jobs"

    @property
    def worktrees_dir(self) -> Path:
        return self.root / "worktrees"

    @property
    def scratch_dir(self) -> Path:
        return self.root / "scratch"

    @property
    def repos_dir(self) -> Path:
        return self.root / "repos"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    def repo_path(self, name: str) -> Path:
        return self.repos_dir / name

    @property
    def audit_log(self) -> Path:
        return self.logs_dir / "grants.log"

    def credential_store(self):
        """The delegation store, built from this config.

        Imported lazily: the runner needs it, but nothing in the hot path
        of check() does.
        """
        from .credentials import CredentialStore

        return CredentialStore(
            secrets_root=self.secrets_root,
            vault=self.vault,
            audit_log=self.audit_log,
        )

    # -- lookup -------------------------------------------------------------

    def agent(self, name: str) -> Agent:
        try:
            return self.agents[name]
        except KeyError:
            raise UnknownAgent(name, sorted(self.agents)) from None

    def resolve_repo(self, query: str | None, agent: Agent) -> Repo | None:
        """Turn a spoken repo name into a registered repo.

        ``None`` means the agent's default, and if it has none, no repo at
        all -- the job gets a scratch directory. Resolution runs exact name,
        then alias, then token overlap; only an unresolvable tie raises
        :class:`AmbiguousRepo`.
        """
        if query is None:
            if agent.default_repo:
                return self.repos[agent.default_repo]
            if agent.needs_repo:
                raise UnknownRepo("", sorted(self.repos))
            return None

        q = query.strip().lower()
        if q in self.repos:
            return self.repos[q]

        # Exact alias. Collected rather than short-circuited: two repos
        # sharing an alias is a config mistake, and silently picking the
        # first one means an agent committing to the wrong repository.
        aliased = [r.name for r in self.repos.values() if q in {a.lower() for a in r.aliases}]
        if len(aliased) == 1:
            return self.repos[aliased[0]]
        if len(aliased) > 1:
            raise AmbiguousRepo(query, sorted(aliased))

        # Loose match: does every word the caller said appear among this
        # repo's name and aliases? "the kiln one" -> {kiln} -> kiln.
        wanted = set(normalise(query))
        if not wanted:
            raise UnknownRepo(query, sorted(self.repos))
        hits = [name for name, toks in self._repo_index.items() if wanted <= toks]
        if len(hits) == 1:
            return self.repos[hits[0]]
        if len(hits) > 1:
            raise AmbiguousRepo(query, sorted(hits))
        raise UnknownRepo(query, sorted(self.repos))


def _require(table: dict, key: str, where: str) -> object:
    if key not in table:
        raise ConfigError(f"{where}: missing required key {key!r}")
    return table[key]


def load(config_dir: Path | None = None, root_override: Path | None = None) -> Config:
    """Read config/*.toml into a :class:`Config`.

    Validation is strict and happens here rather than at job-start time: a
    typo in the registry should fail on the host where a human can read the
    error, not four minutes into a container exec.
    """
    config_dir = config_dir or default_config_dir()
    if root_override is None and os.environ.get(ROOT_ENV):
        root_override = Path(os.environ[ROOT_ENV])

    agents_raw = _read_toml(config_dir / "agents.toml").get("agents", {})
    repos_raw = _read_toml(config_dir / "repos.toml").get("repos", {})
    orch = _read_toml(config_dir / "orchestrator.toml")

    if not agents_raw:
        raise ConfigError(f"{config_dir / 'agents.toml'}: no agents defined")

    known_fields = set(Agent.__dataclass_fields__) - {"name"}
    agents: dict[str, Agent] = {}
    for name, spec in agents_raw.items():
        where = f"agents.{name}"
        unknown = set(spec) - known_fields
        if unknown:
            raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
        kind = _require(spec, "type", where)
        if kind == "container":
            _require(spec, "container", where)
            _require(spec, "command", where)
        elif kind == "local":
            _require(spec, "command", where)
        elif kind == "http_openai":
            _require(spec, "base_url", where)
            _require(spec, "model", where)
        else:
            raise ConfigError(f"{where}: unknown type {kind!r}")
        spec = dict(spec)
        for list_field in ("command", "session_flags", "resume_flags", "system_prompt_flags"):
            if list_field in spec:
                spec[list_field] = tuple(spec[list_field])
        agents[name] = Agent(name=name, **spec)

    repos: dict[str, Repo] = {}
    for name, spec in repos_raw.items():
        where = f"repos.{name}"
        unknown = set(spec) - (set(Repo.__dataclass_fields__) - {"name"})
        if unknown:
            raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
        _require(spec, "url", where)
        repos[name] = Repo(
            name=name,
            url=spec["url"],
            base_ref=spec.get("base_ref", "main"),
            aliases=tuple(spec.get("aliases", ())),
        )

    for name, agent in agents.items():
        if agent.default_repo and agent.default_repo not in repos:
            raise ConfigError(
                f"agents.{name}: default_repo {agent.default_repo!r} is not in repos.toml"
            )

    check = orch.get("check", {})
    red = orch.get("redaction", {})
    creds = orch.get("credentials", {})
    root = root_override or Path(orch.get("paths", {}).get("root", "/srv/orchestration"))

    cfg = Config(
        agents=agents,
        repos=repos,
        root=Path(root),
        config_dir=config_dir,
        default_narration_lines=check.get("default_narration_lines", 3),
        max_narration_lines=check.get("max_narration_lines", 20),
        max_tail_lines=check.get("max_tail_lines", 200),
        entropy_threshold=red.get("entropy_threshold", 3.6),
        entropy_fallback=red.get("entropy_fallback", True),
        scrub_env=tuple(red.get("scrub_env", ())),
        retain_days=orch.get("jobs", {}).get("retain_days", 14),
        vault=creds.get("vault", "Agent"),
        secrets_root=Path(
            os.environ.get(SECRETS_ENV)
            or creds.get("secrets_root", "/run/orchestration/secrets")
        ),
    )
    cfg._repo_index = {
        name: set(normalise(name))
        | {t for a in repo.aliases for t in normalise(a)}
        | set(normalise(_slug(repo.url)))
        for name, repo in repos.items()
    }
    return cfg


def _slug(url: str) -> str:
    """The repo name out of a clone URL: ...:o/kiln-controller.git -> kiln-controller.

    Folded into the match index because people say the real repo name far
    more often than the short registry key -- "the provision ledger" should
    find `ledger` without anyone having to list that alias by hand.
    """
    tail = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail.removesuffix(".git")


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None
