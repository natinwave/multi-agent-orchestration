"""Run a job inside a persistent, named container via ``docker exec``.

The container is long-lived and already carries the toolchain, so a job
costs an exec, not an image pull and an npm install. The supervisor stays on
the host and is the only thing that can talk to the docker daemon; no agent
container ever gets /var/run/docker.sock, because that socket is host root
and would hand any agent every other agent's credentials.

Secrets never appear here. The exec runs a small shell wrapper that reads
the token out of the read-only per-agent mount at /run/secrets and puts it
in the environment of the single claude process. It is not in the image, not
in the container's environment, and not in `docker inspect`.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from ..registry import Agent, Config
from ..state import JobPaths, Meta
from .base import BackendError, Outcome

__all__ = ["ContainerBackend"]

# Read inside the container, exported for one process, never persisted.
SECRET_ENV = {
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth_token",
    "GH_TOKEN": "github_token",
}

# Claude Code's onboarding wizard appears in containers even with valid
# credentials and will sit there forever waiting for a keypress. These make
# a non-interactive exec fail fast and loudly instead of hanging.
NONINTERACTIVE_ENV = {
    "CI": "1",
    "TERM": "dumb",
    "NO_COLOR": "1",
    "CLAUDE_CODE_NONINTERACTIVE": "1",
}


class ContainerBackend:
    def run(
        self,
        *,
        agent: Agent,
        meta: Meta,
        paths: JobPaths,
        workdir: Path,
        prompt: str,
        config: Config,
        resume: bool = False,
    ) -> Outcome:
        if not agent.container:
            raise BackendError(f"agent {agent.name}: no container configured")

        cmd = self.docker_command(agent, meta, paths, workdir, resume=resume)

        # The prompt goes in on stdin, never on the command line: a command
        # line is visible to every process on the host via ps, and lands in
        # shell history and in the docker daemon's logs.
        with paths.raw.open("ab") as log:
            log.write(f"$ {shlex.join(cmd)}\n".encode())
            log.flush()
            try:
                proc = subprocess.run(
                    cmd,
                    input=prompt.encode(),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=agent.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                log.write(f"\n[supervisor] timed out after {agent.timeout_seconds}s\n".encode())
                return Outcome(exit_code=124, detail=f"timed out after {agent.timeout_seconds}s")
            except FileNotFoundError:
                raise BackendError("docker not found on PATH") from None

        detail = None if proc.returncode == 0 else f"exited {proc.returncode}"
        return Outcome(exit_code=proc.returncode, detail=detail)

    # Split out from run() so the command can be asserted in tests without
    # a docker daemon anywhere near them.
    def docker_command(
        self,
        agent: Agent,
        meta: Meta,
        paths: JobPaths,
        workdir: Path,
        resume: bool = False,
    ) -> list[str]:
        inner = self.inner_script(agent, meta, workdir, resume=resume)
        env_flags: list[str] = []
        for key, value in NONINTERACTIVE_ENV.items():
            env_flags += ["--env", f"{key}={value}"]
        # ORCH_JOB_DIR is how `narrate` finds the log to append to. It is a
        # path, not a secret.
        env_flags += ["--env", f"ORCH_JOB_DIR={paths.root}"]
        env_flags += ["--env", f"ORCH_JOB_ID={meta.job_id}"]
        return [
            "docker",
            "exec",
            "--interactive",
            "--workdir",
            str(workdir),
            *env_flags,
            agent.container or "",
            "bash",
            "-lc",
            inner,
        ]

    def inner_script(
        self, agent: Agent, meta: Meta, workdir: Path, resume: bool = False
    ) -> str:
        """The shell run inside the container.

        Secrets are sourced from files at exec time and exported into this
        one process. `exec` replaces the shell with claude, so the token
        lives exactly as long as the agent does.
        """
        lines = ["set -euo pipefail"]
        for env_name, filename in SECRET_ENV.items():
            path = f"/run/secrets/{filename}"
            # Optional: a missing GH_TOKEN should not stop a job that only
            # needs the Anthropic credential.
            lines.append(
                f'if [ -r {path} ]; then export {env_name}="$(cat {path})"; fi'
            )
        lines.append(
            'if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then'
        )
        lines.append(
            '  echo "[supervisor] no credential at /run/secrets/oauth_token -- '
            'run claude setup-token on the host, see README" >&2; exit 78; fi'
        )

        argv = list(agent.command)
        argv += ["--session-id", meta.session_id] if not resume else ["--resume", meta.session_id]
        argv += ["--append-system-prompt-file", "/etc/orchestration/agent-prompt.md"]
        lines.append(f"cd {shlex.quote(str(workdir))}")
        lines.append(f"exec {shlex.join(argv)}")
        return "\n".join(lines)
