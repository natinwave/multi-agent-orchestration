"""Guards on the container layer.

These are the constraints that would be quietly and catastrophically easy
to undo in a later edit -- mounting the docker socket "just to debug
something", or dropping a token into the compose file "temporarily". A test
is cheaper than remembering.

They read the files as text rather than running docker, so they hold on the
Mac where none of the rest of this layer can be exercised.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCKER = Path(__file__).resolve().parents[1] / "docker"
COMPOSE = (DOCKER / "docker-compose.yml").read_text()
DOCKERFILE = (DOCKER / "Dockerfile").read_text()


def uncommented(text: str) -> str:
    """Compose and Dockerfile comments explain these very invariants, so a
    naive substring search finds its own documentation."""
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )


COMPOSE_CODE = uncommented(COMPOSE)


# --- the two invariants ----------------------------------------------------


def test_no_agent_container_gets_the_docker_socket() -> None:
    """docker.sock is host root. An agent holding it could read every other
    agent's credentials, which would make the per-agent scoping decorative."""
    for name, text in (("compose", COMPOSE), ("Dockerfile", DOCKERFILE)):
        for line in text.splitlines():
            code = line.split("#", 1)[0]
            assert "docker.sock" not in code, f"{name}: {line.strip()}"


def test_no_secret_values_in_the_container_layer() -> None:
    from orchestrator.redaction import Redactor

    red = Redactor()
    for path in sorted(DOCKER.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        assert red.scrub(text) == text, f"{path} contains something secret-shaped"


def test_compose_does_not_load_an_env_file() -> None:
    """env_file is the easy way to put a credential into a container's
    environment, where docker inspect will show it to anyone in the docker
    group. Secrets arrive as read-only files instead."""
    assert not re.search(r"^\s*env_file:", COMPOSE_CODE, re.M)


def test_compose_environment_holds_no_credential_shaped_keys() -> None:
    block = COMPOSE_CODE.split("environment:", 1)[1].split("command:", 1)[0]
    for line in block.splitlines():
        key = line.split(":", 1)[0].strip()
        assert not re.search(r"(?i)token|secret|key|password|credential", key), line


# --- the mount layout the design depends on --------------------------------


def test_the_identical_path_bind_is_present() -> None:
    """Host path and container path must be the same string, or a git
    worktree's absolute pointer back to the main clone will not resolve
    inside the container -- and git 2.43 has no --relative-paths."""
    assert "${ORCH_ROOT:-/srv/orchestration}:${ORCH_ROOT:-/srv/orchestration}" in COMPOSE


def test_secrets_are_mounted_per_agent_and_read_only() -> None:
    mounts = re.findall(r"^\s*-\s*(\S*secrets\S*)\s*$", COMPOSE_CODE, re.M)
    assert mounts, "no secrets mount found"
    for mount in mounts:
        assert mount.endswith(":ro"), f"{mount} is writable"
        # Split from the right: the host side contains ${VAR:-default},
        # whose own colon would fool a left split.
        host = mount.rsplit(":", 2)[0]
        # A mount of the whole secrets root would hand one agent every
        # identity; each service takes only its own subdirectory.
        assert not host.rstrip("/").endswith("secrets"), f"{mount} mounts every identity"


def test_agent_uid_is_a_build_arg() -> None:
    """Bind mounts pass UIDs straight through on native Linux, so the
    container user has to match the host user that owns ORCH_ROOT."""
    assert "ARG AGENT_UID" in DOCKERFILE
    assert "AGENT_UID:" in COMPOSE


def test_the_stock_ubuntu_user_is_removed_before_creating_agent() -> None:
    """ubuntu:24.04 ships a `ubuntu` user already sitting on uid 1000."""
    assert "userdel" in DOCKERFILE
    assert DOCKERFILE.index("userdel") < DOCKERFILE.index("useradd")


# --- the image has to be self-sufficient -----------------------------------


@pytest.mark.parametrize("tool", ["git", "ripgrep", "jq", "tini"])
def test_toolchain_is_baked_in(tool: str) -> None:
    """A job costs a docker exec. Nothing installs dependencies per job."""
    assert tool in DOCKERFILE


def test_claude_code_is_installed_at_build_time() -> None:
    assert "@anthropic-ai/claude-code" in DOCKERFILE
    assert "npm install -g" in DOCKERFILE


def test_settings_are_baked_not_mounted() -> None:
    assert "/home/agent/.claude/settings.json" in DOCKERFILE
    assert "settings.json" not in COMPOSE_CODE


def test_baked_settings_deny_reading_the_secrets_mount() -> None:
    import json

    settings = json.loads((DOCKER / "claude-settings.json").read_text())
    deny = settings["permissions"]["deny"]
    assert any("/run/secrets" in rule for rule in deny)
    assert any("docker" in rule for rule in deny)


def test_the_container_runs_as_a_non_root_user() -> None:
    assert re.search(r"^USER agent\s*$", DOCKERFILE, re.M)
    # ...and nothing switches back afterwards.
    assert DOCKERFILE.index("USER agent") > DOCKERFILE.rindex("RUN ")


def test_workspace_volume_exists() -> None:
    assert 'VOLUME ["/workspace"]' in DOCKERFILE
