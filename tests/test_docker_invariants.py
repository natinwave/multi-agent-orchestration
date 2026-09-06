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


def test_either_claude_credential_is_accepted() -> None:
    """A subscription token needs a human at a terminal; an API key does
    not. The container must start from either."""
    from orchestrator.backends.container import SECRET_ENV

    assert SECRET_ENV["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth_token"
    assert SECRET_ENV["ANTHROPIC_API_KEY"] == "anthropic_api_key"


def test_no_agent_identity_can_be_overwritten_by_a_grant() -> None:
    """Every credential the container maps to a fixed environment name is
    the agent's own identity, so a grant must not be able to replace it."""
    from orchestrator.backends.container import SECRET_ENV
    from orchestrator.credentials import RESERVED

    assert set(SECRET_ENV.values()) <= RESERVED


def test_the_voice_identity_cannot_be_granted_over() -> None:
    """The voice bridge holds the OpenAI and Discord credentials. A grant
    that overwrote them would break the thing doing the granting."""
    from orchestrator.credentials import RESERVED

    assert {"openai_api_key", "discord_bot_token"} <= RESERVED


def test_voice_secrets_are_a_separate_identity_from_the_agents() -> None:
    """The coding agents must not see the OpenAI key, and the voice bridge
    must not see their Claude token. Separate directories, as ever."""
    bootstrap = (Path(__file__).resolve().parents[1] / "scripts" / "bootstrap.sh").read_text()
    assert "write_secret voice openai_api_key" in bootstrap
    assert "write_secret voice discord_bot_token" in bootstrap
    assert "write_secret claude-code openai_api_key" not in bootstrap


# --- what agents can actually do ------------------------------------------


@pytest.mark.parametrize("tool", ["python3", "python3-venv", "python3-pip", "build-essential"])
def test_python_is_available_to_agents(tool: str) -> None:
    """Most of the projects these agents work on are Python. Without it the
    first command an agent runs fails."""
    assert tool in DOCKERFILE


def test_pep668_will_not_block_a_pip_install() -> None:
    """Ubuntu marks the system Python externally-managed, and the resulting
    error sends an agent hunting for a problem that is not there."""
    assert "PIP_BREAK_SYSTEM_PACKAGES=1" in DOCKERFILE


def test_a_browser_is_available() -> None:
    assert "playwright" in DOCKERFILE
    assert "chromium" in DOCKERFILE


def test_browsers_live_on_a_shared_path() -> None:
    """So the Python playwright package finds the same binaries as the Node
    one, instead of downloading a second copy per language."""
    assert "PLAYWRIGHT_BROWSERS_PATH=/opt/playwright" in DOCKERFILE


def test_agents_still_cannot_run_containers() -> None:
    """Every way to give a container Docker -- the socket, privileged
    docker-in-docker, relaxing cap_drop -- hands back the isolation this
    design rests on. Agents reach services instead of running them."""
    for name, text in (("compose", COMPOSE_CODE), ("Dockerfile", DOCKERFILE)):
        assert "privileged" not in text, name
        assert "docker.sock" not in text, name
    assert "cap_drop" in COMPOSE_CODE
    assert "no-new-privileges" in COMPOSE_CODE


def test_agents_can_reach_services_on_the_host() -> None:
    """The replacement for running containers: an agent can talk to a
    database or dev server the host is running, by name."""
    assert "host.docker.internal:host-gateway" in COMPOSE_CODE


# --- looking at web pages ---------------------------------------------------


def test_a_browser_is_on_the_path() -> None:
    """An agent checking whether a browser exists looks for a binary. With
    Playwright's chromium buried several directories deep it finds nothing
    and concludes, reasonably, that there is not one -- which is exactly
    what happened."""
    assert "/usr/local/bin/chromium" in DOCKERFILE
    assert "chromium --version" in DOCKERFILE, "verify the symlink at build time"


def test_playwright_is_installed_for_both_languages() -> None:
    """A Python project reaching for a browser will pip install playwright,
    and without the package here that pulls one whose expected browser
    build is missing -- which it then downloads mid-task."""
    assert "npm install -g playwright" in DOCKERFILE
    assert "pip install --no-cache-dir playwright" in DOCKERFILE
    assert "python3 -m playwright install chromium" in DOCKERFILE


@pytest.mark.parametrize("helper", ["screenshot", "page-text"])
def test_the_browser_helpers_ship_and_are_executable(helper: str) -> None:
    path = DOCKER / "bin" / helper
    assert path.is_file()
    assert "/usr/local/bin/" in DOCKERFILE
    import ast

    ast.parse(path.read_text())


@pytest.mark.parametrize("helper", ["screenshot", "page-text"])
def test_the_helpers_disable_the_chromium_sandbox(helper: str) -> None:
    """Chromium's sandbox needs privileges this container drops, and the
    failure without it names everything except sandboxes."""
    assert "--no-sandbox" in (DOCKER / "bin" / helper).read_text()


def test_the_prompt_tells_agents_the_browser_exists_and_how_to_use_it() -> None:
    """It said 'a real browser via Playwright' and gave no command, so an
    agent had to discover the tooling. One of them concluded there was no
    browser at all."""
    prompt = (DOCKER / "agent-prompt.md").read_text()
    assert "page-text" in prompt and "screenshot" in prompt
    assert "--no-sandbox" in prompt
    assert "Do not install a browser" in prompt
