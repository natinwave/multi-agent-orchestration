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

__all__ = ["ContainerBackend", "AGENT_PROMPT_PATH"]

#: The narration contract, baked into the image.
AGENT_PROMPT_PATH = "/etc/orchestration/agent-prompt.md"


def _expand(flags, substitutions: dict[str, str]) -> list[str]:
    """Fill {session_id} / {agent_prompt} in a configured flag list.

    Unknown placeholders are left alone rather than raising: a stray brace
    in someone's flag is not worth failing a job over.
    """
    out = []
    for flag in flags:
        try:
            out.append(flag.format(**substitutions))
        except (KeyError, IndexError, ValueError):
            out.append(flag)
    return out

# The agent's own identity, read inside the container and exported for one
# process. These two have fixed environment names; anything else granted
# later is exported under its own uppercased filename.
SECRET_ENV = {
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth_token",
    # Either credential satisfies Claude Code. An API key bills per token
    # and needs nobody at a keyboard, which is the easier story for a box
    # you are not sitting at.
    "ANTHROPIC_API_KEY": "anthropic_api_key",
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

        # Delegated credentials. Each one you grant appears here as a file
        # and is exported as its uppercased name, so the agent never reads
        # /run/secrets itself -- the value is simply already in the
        # environment of the tools that need it.
        reserved = " ".join(sorted(SECRET_ENV.values()))
        lines.append(
            "for _f in /run/secrets/*; do\n"
            '  [ -r "$_f" ] || continue\n'
            '  _n="$(basename "$_f")"\n'
            f'  case " {reserved} " in *" $_n "*) continue ;; esac\n'
            '  case "$_n" in .*) continue ;; esac\n'
            "  export \"$(printf '%s' \"$_n\" | tr '[:lower:]-' '[:upper:]_')=$(cat \"$_f\")\"\n"
            "done"
        )
        lines.append(
            'if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then'
        )
        lines.append(
            '  echo "[supervisor] no Claude credential in /run/secrets -- expected '
            'oauth_token or anthropic_api_key; see README" >&2; exit 78; fi'
        )

        # Every flag shape comes from the registry, so adding a different
        # agentic CLI is a config change rather than an edit here.
        substitutions = {
            "session_id": meta.session_id,
            "agent_prompt": AGENT_PROMPT_PATH,
        }
        argv = list(agent.command)
        argv += _expand(agent.resume_flags if resume else agent.session_flags, substitutions)
        argv += _expand(agent.system_prompt_flags, substitutions)
        lines.append(f"cd {shlex.quote(str(workdir))}")
        lines.append(f"exec {shlex.join(argv)}")
        return "\n".join(lines)
