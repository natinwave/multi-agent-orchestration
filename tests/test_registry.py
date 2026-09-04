"""The registry is config, so the tests are mostly about rejecting bad
config loudly on the host -- and about the colloquial repo matching, which
is the only real logic in here."""

import re
from pathlib import Path

import pytest

from orchestrator.registry import (
    AmbiguousRepo,
    ConfigError,
    UnknownAgent,
    UnknownRepo,
    load,
)

AGENTS = """
[agents.claude-code]
type = "container"
container = "orch-claude-code"
command = ["claude", "-p"]
default_repo = "kiln"
needs_repo = true

[agents.hermes]
type = "http_openai"
base_url = "http://127.0.0.1:8080/v1"
model = "hermes"
needs_repo = false
"""

REPOS = """
[repos.kiln]
url = "git@github.com:o/kiln-controller.git"
aliases = ["the kiln one", "pottery"]

[repos.ledger]
url = "git@github.com:o/provision-ledger.git"
base_ref = "develop"
aliases = ["provisioning", "the ledger"]
"""

ORCH = """
[paths]
root = "/srv/orchestration"

[check]
default_narration_lines = 3

[redaction]
scrub_env = ["GH_TOKEN"]
"""


def write_config(tmp_path: Path, agents=AGENTS, repos=REPOS, orch=ORCH) -> Path:
    d = tmp_path / "config"
    d.mkdir(exist_ok=True)
    (d / "agents.toml").write_text(agents)
    (d / "repos.toml").write_text(repos)
    (d / "orchestrator.toml").write_text(orch)
    return d


# --- the shipped config has to be valid ------------------------------------


def test_shipped_config_loads() -> None:
    cfg = load()
    assert "claude-code" in cfg.agents
    assert "hermes" in cfg.agents
    assert cfg.agents["claude-code"].type == "container"
    assert cfg.agents["hermes"].type == "http_openai"


def test_shipped_config_keeps_dangerous_flag_inside_the_container_backend() -> None:
    """--dangerously-skip-permissions is acceptable in a container and
    nowhere else, so no http agent may carry it."""
    cfg = load()
    for agent in cfg.agents.values():
        if agent.type != "container":
            assert "--dangerously-skip-permissions" not in agent.command


def test_shipped_config_has_no_secret_values() -> None:
    """The registry names secret *locations*, never values."""
    from orchestrator.redaction import Redactor

    red = Redactor()
    for name in ("agents.toml", "repos.toml", "orchestrator.toml"):
        text = (Path(__file__).resolve().parents[1] / "config" / name).read_text()
        assert red.scrub(text) == text, f"config/{name} contains something secret-shaped"


# --- loading and validation ------------------------------------------------


def test_paths_derive_from_root(tmp_path: Path) -> None:
    cfg = load(write_config(tmp_path))
    assert cfg.jobs_dir == Path("/srv/orchestration/jobs")
    assert cfg.worktrees_dir == Path("/srv/orchestration/worktrees")
    assert cfg.repo_path("kiln") == Path("/srv/orchestration/repos/kiln")


def test_root_can_be_overridden_for_tests(tmp_path: Path) -> None:
    cfg = load(write_config(tmp_path), root_override=tmp_path / "srv")
    assert cfg.jobs_dir == tmp_path / "srv" / "jobs"


def test_missing_file_is_a_clear_error(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "config")


def test_malformed_toml_names_the_file(tmp_path: Path) -> None:
    d = write_config(tmp_path, agents="[agents.x\n")
    with pytest.raises(ConfigError, match="agents.toml"):
        load(d)


def test_no_agents_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no agents"):
        load(write_config(tmp_path, agents="# nothing\n"))


def test_unknown_backend_type_is_rejected(tmp_path: Path) -> None:
    bad = '[agents.x]\ntype = "carrier-pigeon"\n'
    with pytest.raises(ConfigError, match="carrier-pigeon"):
        load(write_config(tmp_path, agents=bad))


def test_container_agent_must_name_a_container(tmp_path: Path) -> None:
    bad = '[agents.x]\ntype = "container"\ncommand = ["claude"]\n'
    with pytest.raises(ConfigError, match="container"):
        load(write_config(tmp_path, agents=bad))


def test_http_agent_must_name_a_model(tmp_path: Path) -> None:
    bad = '[agents.x]\ntype = "http_openai"\nbase_url = "http://x/v1"\n'
    with pytest.raises(ConfigError, match="model"):
        load(write_config(tmp_path, agents=bad))


def test_typo_in_a_key_is_caught_not_ignored(tmp_path: Path) -> None:
    """A silently ignored key would mean an agent quietly running with the
    wrong timeout for weeks."""
    bad = (
        '[agents.x]\ntype = "container"\ncontainer = "c"\n'
        'command = ["claude"]\ntimeout_second = 60\n'
    )
    with pytest.raises(ConfigError, match="timeout_second"):
        load(write_config(tmp_path, agents=bad))


def test_default_repo_must_exist(tmp_path: Path) -> None:
    bad = (
        '[agents.x]\ntype = "container"\ncontainer = "c"\n'
        'command = ["claude"]\ndefault_repo = "nope"\n'
    )
    with pytest.raises(ConfigError, match="nope"):
        load(write_config(tmp_path, agents=bad))


def test_repo_must_have_a_url(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="url"):
        load(write_config(tmp_path, repos='[repos.x]\naliases = ["y"]\n'))


def test_unknown_agent_lists_what_is_available(tmp_path: Path) -> None:
    cfg = load(write_config(tmp_path))
    with pytest.raises(UnknownAgent) as exc:
        cfg.agent("clod-code")
    assert exc.value.known == ["claude-code", "hermes"]


# --- colloquial repo resolution --------------------------------------------


@pytest.fixture
def cfg(tmp_path: Path):
    return load(write_config(tmp_path))


@pytest.mark.parametrize(
    "spoken,expected",
    [
        ("kiln", "kiln"),
        ("KILN", "kiln"),
        ("  kiln  ", "kiln"),
        ("the kiln one", "kiln"),
        ("pottery", "kiln"),
        ("the pottery project", "kiln"),
        ("kiln controller", "kiln"),
        ("ledger", "ledger"),
        ("the ledger repo", "ledger"),
        ("provisioning", "ledger"),
        ("provision ledger", "ledger"),
    ],
)
def test_spoken_names_resolve(cfg, spoken: str, expected: str) -> None:
    assert cfg.resolve_repo(spoken, cfg.agents["hermes"]).name == expected


def test_none_uses_the_agents_default(cfg) -> None:
    assert cfg.resolve_repo(None, cfg.agents["claude-code"]).name == "kiln"


def test_none_for_a_repoless_agent_is_no_repo(cfg) -> None:
    """hermes answers questions; not every job needs a checkout."""
    assert cfg.resolve_repo(None, cfg.agents["hermes"]) is None


def test_unknown_repo_lists_the_vocabulary(cfg) -> None:
    with pytest.raises(UnknownRepo) as exc:
        cfg.resolve_repo("the tax return", cfg.agents["hermes"])
    assert exc.value.known == ["kiln", "ledger"]


def test_ambiguity_refuses_rather_than_guessing(tmp_path: Path) -> None:
    """Guessing here means an agent committing to the wrong repository."""
    agents = '[agents.hermes]\ntype = "http_openai"\nbase_url = "http://x/v1"\nmodel = "m"\n'
    repos = (
        '[repos.alpha]\nurl = "git@h:o/alpha.git"\naliases = ["the app"]\n'
        '[repos.beta]\nurl = "git@h:o/beta.git"\naliases = ["the app"]\n'
    )
    cfg = load(write_config(tmp_path, agents=agents, repos=repos))
    with pytest.raises(AmbiguousRepo) as exc:
        cfg.resolve_repo("the app", cfg.agents["hermes"])
    assert exc.value.candidates == ["alpha", "beta"]


def test_filler_only_query_is_unknown_not_ambiguous(cfg) -> None:
    with pytest.raises(UnknownRepo):
        cfg.resolve_repo("the the the", cfg.agents["hermes"])


def test_base_ref_defaults_and_overrides(cfg) -> None:
    assert cfg.repos["kiln"].base_ref == "main"
    assert cfg.repos["ledger"].base_ref == "develop"


def test_repo_slug_from_the_clone_url_is_matchable(cfg) -> None:
    """People say the real repository name, not the short registry key."""
    assert cfg.resolve_repo("kiln controller", cfg.agents["hermes"]).name == "kiln"
    assert cfg.resolve_repo("provision ledger", cfg.agents["hermes"]).name == "ledger"


def test_shipped_repos_use_https_not_ssh() -> None:
    """The orchestration host has no SSH key, and giving it one would be a
    credential to manage outside 1Password. Private repos use GH_TOKEN."""
    for repo in load().repos.values():
        assert repo.url.startswith("https://"), f"{repo.name} would need an SSH key"


def test_env_example_points_at_a_vault_that_exists() -> None:
    """The refs must name the Agent vault -- pointing at a vault the
    account does not have kills bootstrap before it starts."""
    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    refs = re.findall(r"op://([^/]+)/", text)
    assert refs, "no op:// references found"
    assert set(refs) == {"Agent"}, f"unexpected vaults: {sorted(set(refs))}"
