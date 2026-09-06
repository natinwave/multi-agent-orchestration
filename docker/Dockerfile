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
        build-essential \
        ca-certificates \
        curl \
        git \
        gnupg \
        jq \
        less \
        openssh-client \
        procps \
        python3 \
        python3-dev \
        python3-pip \
        python3-venv \
        ripgrep \
        tini \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Ubuntu 24.04 marks the system Python externally-managed (PEP 668), so a
# plain `pip install` fails with an error that sends an agent hunting.
# Inside a disposable container there is nothing to protect, so allow it --
# the agent prompt still asks for a venv on real project work, where the
# project's own pinned versions matter.
ENV PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# Node 20 from NodeSource. The distro's nodejs is too old for Claude Code.
RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && npm --version

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force \
    && claude --version

# A real browser, for agents that need to look at a page rather than curl
# it. Installed to a shared path rather than a user cache so the Python
# playwright package finds the same binaries as the Node one -- otherwise
# a Python project would download a second copy into the agent's home.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN npm install -g playwright@latest \
    && playwright install --with-deps chromium \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

# The Python package too, not only the Node one. An agent reaching for a
# browser in a Python project will `pip install playwright`, and without
# this that pulls a package whose expected browser build is not here --
# which it then tries to download mid-task, slowly, and often fails.
RUN pip install --no-cache-dir playwright \
    && python3 -m playwright install chromium \
    && chmod -R a+rX /opt/playwright

# Put a browser on PATH. Playwright hides its chromium several directories
# deep, so an agent checking whether a browser exists finds nothing and
# concludes, reasonably, that there is not one.
RUN ln -sf "$(find /opt/playwright -name chrome -type f -path '*chrome-linux*' | head -1)" \
        /usr/local/bin/chromium \
    && ln -sf /usr/local/bin/chromium /usr/local/bin/google-chrome \
    && chromium --version

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
# `screenshot` and `page-text` make looking at a web page one command
# rather than a research project.
COPY bin/narrate bin/screenshot bin/page-text /usr/local/bin/
RUN chmod 0755 /usr/local/bin/narrate /usr/local/bin/screenshot \
        /usr/local/bin/page-text /etc/orchestration/agent-prompt.md

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
