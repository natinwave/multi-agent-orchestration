# The agent container. Long-lived: the toolchain is baked in, so a job costs
# a `docker exec`, never an npm install.
FROM ubuntu:24.04

# Matched to the host user that owns /srv/orchestration. On native Linux a
# bind mount passes UIDs straight through, so a mismatch means every file
# the agent writes lands wrong-owned and the host user cannot read its own
# worktrees. (Docker Desktop on macOS hides this; the Ubuntu host will not.)
ARG AGENT_UID=1000
ARG AGENT_GID=1000
ARG NODE_MAJOR=20
ARG CLAUDE_CODE_VERSION=latest

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    NODE_ENV=production \
    npm_config_update_notifier=false \
    npm_config_fund=false

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        jq \
        less \
        openssh-client \
        procps \
        ripgrep \
        tini \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Node 20 from NodeSource. The distro's nodejs is too old for Claude Code.
RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && npm --version

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force \
    && claude --version

# ubuntu:24.04 ships a stock `ubuntu` user already sitting on uid 1000, which
# is exactly the uid we usually want. Remove it before creating ours.
RUN if getent passwd "${AGENT_UID}" >/dev/null; then \
        userdel -r "$(getent passwd "${AGENT_UID}" | cut -d: -f1)" 2>/dev/null || true; \
    fi \
    && if getent group "${AGENT_GID}" >/dev/null; then \
        groupdel "$(getent group "${AGENT_GID}" | cut -d: -f1)" 2>/dev/null || true; \
    fi \
    && groupadd --gid "${AGENT_GID}" agent \
    && useradd --uid "${AGENT_UID}" --gid "${AGENT_GID}" --create-home --shell /bin/bash agent

# The narration contract: how an agent reports progress, and the fact that
# nobody is reading its stdout. Injected with --append-system-prompt-file.
COPY agent-prompt.md /etc/orchestration/agent-prompt.md

# `narrate` is the only way a job talks to the voice front-end.
COPY bin/narrate /usr/local/bin/narrate
RUN chmod 0755 /usr/local/bin/narrate /etc/orchestration/agent-prompt.md

# Settings are baked in, not mounted, so a job never waits on config that
# might not be there. Written to the image; the /home/agent volume in
# docker-compose.yml is seeded from it on first start.
COPY --chown=${AGENT_UID}:${AGENT_GID} claude-settings.json /home/agent/.claude/settings.json

# /workspace is a second view of /srv/orchestration/worktrees -- see
# docker-compose.yml for why the identical-path bind is the important one.
RUN mkdir -p /workspace /srv/orchestration \
    && chown -R "${AGENT_UID}:${AGENT_GID}" /home/agent /workspace
VOLUME ["/workspace"]

USER agent
WORKDIR /workspace

ENV CLAUDE_CONFIG_DIR=/home/agent/.claude \
    NPM_CONFIG_PREFIX=/home/agent/.npm-global \
    PATH=/home/agent/.npm-global/bin:/usr/local/bin:/usr/bin:/bin

# tini reaps the zombies that a long-lived exec target accumulates. The
# container does nothing on its own; work arrives as `docker exec`.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
