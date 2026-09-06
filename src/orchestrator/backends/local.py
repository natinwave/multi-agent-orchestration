"""Run an agent directly on the host, outside any container.

For work that genuinely needs the machine: bringing a compose stack up,
touching files outside a worktree, using tools that are installed here and
not in the image. The container exists to bound what an agent can reach,
so choosing this is choosing to give that up, deliberately, for one agent.

**A local agent does not get ``--dangerously-skip-permissions``.** That
flag is acceptable where the blast radius is a container and nowhere else,
which was true when the container was the only option and is more true now
that there is an alternative. A local agent runs with
``--permission-mode acceptEdits`` and an explicit ``--allowedTools`` list
from the registry, so it edits freely within its worktree and has to have
been told, in advance and in writing, about anything else it may do. A
test enforces that the dangerous flag never reaches this backend.

The practical difference from the container backend is smaller than it
looks: same worktree, same narration, same per-agent credentials read at
exec time. What changes is that there is no boundary around it.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from ..registry import Agent, Config
from ..state import JobPaths, Meta
from .base import BackendError, Outcome
from .container import NONINTERACTIVE_ENV, SECRET_ENV

__all__ = ["LocalBackend", "DANGEROUS_FLAGS"]

#: Flags that make sense only inside a container. Refused here rather than
#: quietly honoured: an agent with no boundary around it and no permission
#: checks either is a different thing from what this project set out to
#: build, and it should not be reachable by editing one config line.
DANGEROUS_FLAGS = frozenset(
    {"--dangerously-skip-permissions", "--permission-mode=bypassPermissions"}
)


class LocalBackend:
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
        argv = self.command(agent, meta, resume=resume)
        env = self.environment(agent, paths, workdir)

        with paths.raw.open("ab") as log:
            log.write(f"$ (local) {shlex.join(argv)}\n".encode())
            log.flush()
            try:
                proc = subprocess.run(
                    argv,
                    input=prompt.encode(),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    cwd=str(workdir),
                    env=env,
                    timeout=agent.timeout_seconds,
                    # Its own session, so stopping the job can signal the
                    # whole group rather than orphaning what it spawned.
                    start_new_session=True,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                log.write(f"\n[supervisor] timed out after {agent.timeout_seconds}s\n".encode())
                return Outcome(exit_code=124, detail=f"timed out after {agent.timeout_seconds}s")
            except FileNotFoundError:
                raise BackendError(f"{argv[0]} is not installed on this host") from None

        detail = None if proc.returncode == 0 else f"exited {proc.returncode}"
        return Outcome(exit_code=proc.returncode, detail=detail)

    def command(self, agent: Agent, meta: Meta, resume: bool = False) -> list[str]:
        """The argv to run, refusing anything container-only."""
        from .container import AGENT_PROMPT_PATH, _expand

        for flag in agent.command:
            if flag in DANGEROUS_FLAGS:
                raise BackendError(
                    f"agent {agent.name!r} is local and asks for {flag}. That is "
                    "acceptable inside a container, where the blast radius is the "
                    "container. Here there is no boundary at all -- use "
                    "--permission-mode acceptEdits with an explicit --allowedTools."
                )

        substitutions = {
            "session_id": meta.session_id,
            # The host copy, not the image's: nothing is mounted here.
            "agent_prompt": str(self.agent_prompt_path() or AGENT_PROMPT_PATH),
        }
        argv = list(agent.command)
        argv += _expand(agent.resume_flags if resume else agent.session_flags, substitutions)
        argv += _expand(agent.system_prompt_flags, substitutions)
        return argv

    @staticmethod
    def agent_prompt_path() -> Path | None:
        """The narration contract, from the repo rather than the image."""
        path = Path(__file__).resolve().parents[3] / "docker" / "agent-prompt.md"
        return path if path.is_file() else None

    @staticmethod
    def helper_dir() -> Path:
        """Where ``narrate``, ``screenshot`` and ``page-text`` live.

        Baked into the image for container agents; for a local one they are
        put on PATH from the repo, so the same narration contract works
        without installing anything.
        """
        return Path(__file__).resolve().parents[3] / "docker" / "bin"

    def environment(self, agent: Agent, paths: JobPaths, workdir: Path) -> dict[str, str]:
        """The child's environment, including credentials read at exec time.

        Same discipline as the container: values are read from the
        per-agent secrets directory into this one process, never written
        anywhere and never inherited by anything else.
        """
        env = dict(os.environ)
        env.update(NONINTERACTIVE_ENV)
        env["ORCH_JOB_DIR"] = str(paths.root)
        env["ORCH_JOB_ID"] = paths.root.name

        helpers = self.helper_dir()
        if helpers.is_dir():
            env["PATH"] = f"{helpers}{os.pathsep}{env.get('PATH', '')}"

        if agent.secrets_dir:
            secrets = Path(agent.secrets_dir)
            for env_name, filename in SECRET_ENV.items():
                try:
                    value = (secrets / filename).read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                if value:
                    env[env_name] = value
            # Anything granted later, under its own uppercased name.
            try:
                granted = sorted(p for p in secrets.iterdir() if p.is_file())
            except OSError:
                granted = []
            for path in granted:
                if path.name in SECRET_ENV.values() or path.name.startswith("."):
                    continue
                try:
                    value = path.read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                if value:
                    env[path.name.upper().replace("-", "_")] = value

        return env

    def stop(self, *, agent: Agent, meta: Meta) -> None:
        """Nothing extra to do.

        Unlike a container exec, a local child dies with its process group
        when the runner is signalled, so the supervisor's kill is enough.
        """
        return None
