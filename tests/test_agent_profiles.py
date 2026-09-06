"""Profiles: one kind of agent, any number of instances.

The shape borrowed from AgentCore -- define an image, a model and a prompt
once, then run as many as you like against it. Most of that already
existed: a job gets its own worktree and its own process, so concurrent
jobs of the same kind do not collide. What was missing was defining a
second kind without copying the first, keeping concurrent instances out of
each other's way, and a ceiling on how many can run at once.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from orchestrator.backends.base import BackendError
from orchestrator.backends.container import ContainerBackend
from orchestrator.registry import Agent, ConfigError, load
from orchestrator.state import JobPaths, Meta

BASE = '''
[agents.base]
type = "container"
container = "orch-base"
image = "orchestration/base:latest"
command = ["claude", "-p"]
secrets_dir = "/run/orchestration/secrets/base"
timeout_seconds = 900
max_concurrent = 4
'''


def write(tmp_path: Path, agents: str) -> Path:
    d = tmp_path / "config"
    d.mkdir(exist_ok=True)
    (d / "agents.toml").write_text(agents)
    (d / "repos.toml").write_text('[repos.main]\nurl = "https://h/o/m.git"\n')
    (d / "orchestrator.toml").write_text('[paths]\nroot = "/srv/orchestration"\n')
    return d


def meta(job_id: str = "kestrel") -> Meta:
    return Meta(
        job_id=job_id,
        agent="base",
        created_at="2026-09-06T00:00:00+00:00",
        workdir=f"/srv/orchestration/worktrees/{job_id}",
        session_id="3f7c1e2a-9b4d-4c8e-a1f6-2d5b8c0e4a91",
    )


# --- defining a kind --------------------------------------------------------


def test_a_second_kind_is_a_few_lines(tmp_path: Path) -> None:
    cfg = load(write(tmp_path, BASE + '''
[agents.reviewer]
extends = "base"
description = "reads, never edits"
max_concurrent = 2
'''))
    reviewer = cfg.agents["reviewer"]
    assert reviewer.container == "orch-base"          # inherited
    assert reviewer.timeout_seconds == 900            # inherited
    assert reviewer.description == "reads, never edits"  # its own
    assert reviewer.max_concurrent == 2               # overridden


def test_inheritance_chains(tmp_path: Path) -> None:
    cfg = load(write(tmp_path, BASE + '''
[agents.middle]
extends = "base"
timeout_seconds = 60

[agents.leaf]
extends = "middle"
description = "two hops from base"
'''))
    assert cfg.agents["leaf"].container == "orch-base"
    assert cfg.agents["leaf"].timeout_seconds == 60


def test_a_cycle_is_refused_rather_than_followed(tmp_path: Path) -> None:
    """The alternative is a hang at startup with nothing to read."""
    agents = '''
[agents.a]
extends = "b"
type = "container"
container = "c"
command = ["x"]

[agents.b]
extends = "a"
'''
    with pytest.raises(ConfigError, match="cycle"):
        load(write(tmp_path, agents))


def test_extending_something_that_is_not_an_agent(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not an agent"):
        load(write(tmp_path, BASE + '[agents.x]\nextends = "ghost"\n'))


def test_a_child_does_not_inherit_a_name(tmp_path: Path) -> None:
    cfg = load(write(tmp_path, BASE + '[agents.reviewer]\nextends = "base"\n'))
    assert cfg.agents["reviewer"].name == "reviewer"


# --- keeping instances apart ------------------------------------------------


def test_shared_isolation_execs_into_the_long_lived_container() -> None:
    agent = Agent(name="base", type="container", container="orch-base", command=("claude",))
    cmd = ContainerBackend().docker_command(agent, meta(), JobPaths(Path("/j")), Path("/w"))
    assert cmd[:2] == ["docker", "exec"]


def test_per_job_isolation_starts_a_container_of_its_own() -> None:
    agent = Agent(
        name="isolated", type="container", isolation="per_job",
        image="orchestration/claude-code:latest", command=("claude",),
        secrets_dir="/run/orchestration/secrets/claude-code",
    )
    cmd = ContainerBackend().docker_command(agent, meta(), JobPaths(Path("/j")), Path("/w"))
    line = shlex.join(cmd)
    assert cmd[:2] == ["docker", "run"]
    assert "--rm" in cmd, "a per-job container must not outlive its job"
    assert "orchestration/claude-code:latest" in cmd


def test_a_per_job_container_is_named_after_the_job() -> None:
    """So `docker ps` on a busy afternoon says which agent is doing what."""
    agent = Agent(
        name="isolated", type="container", isolation="per_job",
        image="img", command=("claude",),
    )
    cmd = ContainerBackend().docker_command(agent, meta("otter"), JobPaths(Path("/j")), Path("/w"))
    assert "orch-isolated-otter" in cmd


def test_a_per_job_container_keeps_the_same_security_posture() -> None:
    """It runs model-authored commands like any other."""
    agent = Agent(name="i", type="container", isolation="per_job", image="img", command=("c",))
    line = shlex.join(
        ContainerBackend().docker_command(agent, meta(), JobPaths(Path("/j")), Path("/w"))
    )
    assert "--cap-drop ALL" in line
    assert "no-new-privileges" in line
    assert "docker.sock" not in line


def test_a_per_job_container_gets_the_identical_path_bind() -> None:
    """Worktrees resolve only because host and container agree on the path,
    and this path builds its own mounts rather than reading compose."""
    from orchestrator.registry import Config

    agent = Agent(name="i", type="container", isolation="per_job", image="img", command=("c",))
    cfg = Config(agents={}, repos={}, root=Path("/srv/orchestration"))
    line = shlex.join(
        ContainerBackend().docker_command(
            agent, meta(), JobPaths(Path("/j")), Path("/w"), config=cfg
        )
    )
    assert "/srv/orchestration:/srv/orchestration" in line
    assert "/srv/orchestration/worktrees:/workspace" in line


def test_a_per_job_container_mounts_only_its_own_secrets() -> None:
    agent = Agent(
        name="i", type="container", isolation="per_job", image="img", command=("c",),
        secrets_dir="/run/orchestration/secrets/claude-code",
    )
    line = shlex.join(
        ContainerBackend().docker_command(agent, meta(), JobPaths(Path("/j")), Path("/w"))
    )
    assert "/run/orchestration/secrets/claude-code:/run/secrets:ro" in line


def test_per_job_isolation_needs_an_image(tmp_path: Path) -> None:
    agents = (
        '[agents.x]\ntype = "container"\ncontainer = "c"\n'
        'command = ["claude"]\nisolation = "per_job"\n'
    )
    with pytest.raises(ConfigError, match="image"):
        load(write(tmp_path, agents))


def test_an_unknown_isolation_is_refused(tmp_path: Path) -> None:
    agents = (
        '[agents.x]\ntype = "container"\ncontainer = "c"\n'
        'command = ["claude"]\nisolation = "sometimes"\n'
    )
    with pytest.raises(ConfigError, match="isolation"):
        load(write(tmp_path, agents))


def test_stopping_a_per_job_container_removes_it() -> None:
    """The container is the job, so there is nothing to pkill inside it."""
    import subprocess
    from unittest.mock import patch

    agent = Agent(name="i", type="container", isolation="per_job", image="img", command=("c",))
    with patch.object(subprocess, "run") as run:
        ContainerBackend().stop(agent=agent, meta=meta("cedar"))
    argv = run.call_args[0][0]
    assert argv[:3] == ["docker", "rm", "--force"]
    assert "orch-i-cedar" in argv


# --- how many at once -------------------------------------------------------


def test_a_limit_can_be_set(tmp_path: Path) -> None:
    cfg = load(write(tmp_path, BASE))
    assert cfg.agents["base"].max_concurrent == 4


def test_no_limit_by_default(tmp_path: Path) -> None:
    agents = '[agents.x]\ntype = "container"\ncontainer = "c"\ncommand = ["claude"]\n'
    assert load(write(tmp_path, agents)).agents["x"].max_concurrent == 0


def test_the_shipped_coding_agent_has_a_ceiling() -> None:
    """An enthusiastic afternoon should not put twenty agents on one
    desktop by accident."""
    assert load().agents["claude-code"].max_concurrent > 0


# --- profiles from outside the repository -----------------------------------


def test_a_profile_file_becomes_an_agent(tmp_path: Path) -> None:
    """Profiles get personal fast -- what an agent is for, what it holds,
    which projects it touches -- and none of that belongs in a git
    history."""
    root = tmp_path / "srv"
    (root / "profiles").mkdir(parents=True)
    (root / "profiles" / "ledger.toml").write_text(
        'description = "works on the ledger"\n'
        'extends = "base"\n'
        'isolation = "per_job"\n'
        'image = "orchestration/ledger:latest"\n'
    )
    cfg = load(write(tmp_path, BASE), root_override=root)
    ledger = cfg.agents["ledger"]
    assert ledger.description == "works on the ledger"
    assert ledger.container == "orch-base"          # inherited from the repo
    assert ledger.isolation == "per_job"


def test_the_filename_is_the_name(tmp_path: Path) -> None:
    root = tmp_path / "srv"
    (root / "profiles").mkdir(parents=True)
    (root / "profiles" / "vesper-local.toml").write_text('extends = "base"\n')
    assert "vesper-local" in load(write(tmp_path, BASE), root_override=root).agents


def test_a_profile_repeating_its_own_name_is_refused(tmp_path: Path) -> None:
    """Two places to say who it is, is one place for them to disagree."""
    root = tmp_path / "srv"
    (root / "profiles").mkdir(parents=True)
    (root / "profiles" / "ledger.toml").write_text('[agents.ledger]\nextends = "base"\n')
    with pytest.raises(ConfigError, match="filename names it"):
        load(write(tmp_path, BASE), root_override=root)


def test_an_unspeakable_filename_is_refused(tmp_path: Path) -> None:
    """A profile name is said out loud by the voice agent."""
    root = tmp_path / "srv"
    (root / "profiles").mkdir(parents=True)
    (root / "profiles" / "Ledger V2!.toml").write_text('extends = "base"\n')
    with pytest.raises(ConfigError, match="lowercase"):
        load(write(tmp_path, BASE), root_override=root)


def test_a_profile_overrides_a_shipped_agent_of_the_same_name(tmp_path: Path) -> None:
    """Overriding one is a legitimate thing to want, and ignoring the
    override silently would not be."""
    root = tmp_path / "srv"
    (root / "profiles").mkdir(parents=True)
    (root / "profiles" / "base.toml").write_text(
        'type = "container"\ncontainer = "mine"\ncommand = ["claude"]\n'
    )
    cfg = load(write(tmp_path, BASE), root_override=root)
    assert cfg.agents["base"].container == "mine"


def test_no_profiles_directory_is_fine(tmp_path: Path) -> None:
    assert "base" in load(write(tmp_path, BASE), root_override=tmp_path / "empty").agents


def test_the_shipped_example_profile_is_valid(tmp_path: Path) -> None:
    """It is what people copy, so it has to load."""
    import shutil

    root = tmp_path / "srv"
    (root / "profiles").mkdir(parents=True)
    example = Path(__file__).resolve().parents[1] / "examples" / "profiles" / "ledger.toml"
    shutil.copy(example, root / "profiles" / "ledger.toml")

    ledger = load(root_override=root).agents["ledger"]
    assert ledger.isolation == "per_job", "each instance should get its own container"
    assert ledger.image and ledger.secrets_dir
    assert len(ledger.description) > 60


# --- standing credentials ---------------------------------------------------


def test_a_profile_declares_what_it_always_holds(tmp_path: Path) -> None:
    cfg = load(write(tmp_path, BASE + '''
[agents.ledger]
extends = "base"
credentials = ["Ledger DB Password", "GitHub Agent Token"]
'''))
    assert cfg.agents["ledger"].credentials == ("Ledger DB Password", "GitHub Agent Token")


def test_credentials_are_inherited_like_anything_else(tmp_path: Path) -> None:
    cfg = load(write(tmp_path, BASE.replace(
        "max_concurrent = 4", 'max_concurrent = 4\ncredentials = ["Shared Token"]'
    ) + '[agents.child]\nextends = "base"\n'))
    assert cfg.agents["child"].credentials == ("Shared Token",)
