"""Running an agent on the host, outside any container.

Every credential here is fabricated. secret-scan: allow

The container exists to bound what an agent can reach, so this backend is
a deliberate decision to give that up for one agent. Most of these tests
are about what it refuses to do as a result.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from orchestrator.backends import BACKENDS, for_agent
from orchestrator.backends.base import BackendError
from orchestrator.backends.local import DANGEROUS_FLAGS, LocalBackend
from orchestrator.registry import Agent
from orchestrator.state import JobPaths, Meta


def agent(**kwargs) -> Agent:
    spec = {
        "name": "local",
        "type": "local",
        "command": (
            "claude", "-p",
            "--permission-mode", "acceptEdits",
            "--allowedTools", "Read Edit Bash(git *)",
        ),
        "session_flags": ("--session-id", "{session_id}"),
        "resume_flags": ("--resume", "{session_id}"),
        "system_prompt_flags": ("--append-system-prompt-file", "{agent_prompt}"),
    }
    spec.update(kwargs)
    return Agent(**spec)


def meta(**kwargs) -> Meta:
    spec = {
        "job_id": "kestrel",
        "agent": "local",
        "created_at": "2026-09-06T00:00:00+00:00",
        "workdir": "/srv/orchestration/worktrees/kestrel",
        "session_id": "3f7c1e2a-9b4d-4c8e-a1f6-2d5b8c0e4a91",
    }
    spec.update(kwargs)
    return Meta(**spec)


# --- what it refuses --------------------------------------------------------


@pytest.mark.parametrize("flag", sorted(DANGEROUS_FLAGS))
def test_it_refuses_container_only_flags(flag: str) -> None:
    """--dangerously-skip-permissions is acceptable where the blast radius
    is a container. Here there is no boundary at all, so an agent with
    neither should not be reachable by editing one config line."""
    with pytest.raises(BackendError, match="container"):
        LocalBackend().command(agent(command=("claude", "-p", flag)), meta())


def test_the_refusal_says_what_to_do_instead() -> None:
    with pytest.raises(BackendError) as exc:
        LocalBackend().command(
            agent(command=("claude", "-p", "--dangerously-skip-permissions")), meta()
        )
    assert "acceptEdits" in str(exc.value)
    assert "allowedTools" in str(exc.value)


def test_no_shipped_local_agent_carries_a_dangerous_flag() -> None:
    """The commented example in agents.toml is what people copy."""
    text = (Path(__file__).resolve().parents[1] / "config" / "agents.toml").read_text()
    local_block = text.split("[agents.local]")[-1] if "[agents.local]" in text else ""
    for flag in DANGEROUS_FLAGS:
        assert flag not in local_block


# --- the command it builds --------------------------------------------------


def test_the_command_carries_the_session_and_prompt() -> None:
    argv = LocalBackend().command(agent(), meta())
    line = shlex.join(argv)
    assert "--permission-mode acceptEdits" in line
    assert "--session-id 3f7c1e2a-9b4d-4c8e-a1f6-2d5b8c0e4a91" in line
    assert "agent-prompt.md" in line


def test_resuming_uses_the_resume_flags() -> None:
    argv = LocalBackend().command(agent(), meta(), resume=True)
    assert "--resume" in argv and "--session-id" not in argv


def test_the_prompt_comes_from_the_repo_not_the_image() -> None:
    """Nothing is mounted here, so the image's /etc path does not exist."""
    path = LocalBackend.agent_prompt_path()
    assert path is not None and path.is_file()
    assert "/etc/orchestration" not in str(path)


# --- the environment it builds ----------------------------------------------


def test_narrate_is_on_the_path(tmp_path: Path) -> None:
    """The narration contract has to work identically, or a local agent is
    silent and check() has nothing to report."""
    env = LocalBackend().environment(agent(), JobPaths(tmp_path), tmp_path)
    first = env["PATH"].split(":")[0]
    assert (Path(first) / "narrate").is_file()


def test_the_job_directory_is_passed_through(tmp_path: Path) -> None:
    env = LocalBackend().environment(agent(), JobPaths(tmp_path / "kestrel"), tmp_path)
    assert env["ORCH_JOB_DIR"].endswith("kestrel")
    assert env["ORCH_JOB_ID"] == "kestrel"


def test_credentials_are_read_at_exec_time(tmp_path: Path) -> None:
    """Same discipline as the container: into this one process, never
    written anywhere and never inherited by anything else."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "oauth_token").write_text("sk-ant-oat01-fabricated\n")
    (secrets / "staging_db_password").write_text("granted-value\n")

    env = LocalBackend().environment(
        agent(secrets_dir=str(secrets)), JobPaths(tmp_path / "job"), tmp_path
    )
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-fabricated"
    assert env["STAGING_DB_PASSWORD"] == "granted-value"


def test_a_missing_secrets_directory_is_survivable(tmp_path: Path) -> None:
    env = LocalBackend().environment(
        agent(secrets_dir=str(tmp_path / "nope")), JobPaths(tmp_path), tmp_path
    )
    assert "ORCH_JOB_DIR" in env


def test_the_onboarding_wizard_is_suppressed(tmp_path: Path) -> None:
    """It appears on a host too, and would hang the job waiting for a key."""
    env = LocalBackend().environment(agent(), JobPaths(tmp_path), tmp_path)
    assert env["CI"] == "1"


# --- selection --------------------------------------------------------------


def test_the_registry_selects_it() -> None:
    assert "local" in BACKENDS
    assert isinstance(for_agent(agent()), LocalBackend)


def test_stopping_needs_no_special_handling() -> None:
    """Unlike a docker exec, a local child dies with its process group."""
    assert LocalBackend().stop(agent=agent(), meta=meta()) is None
