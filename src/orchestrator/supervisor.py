"""The supervisor: ask, check, reply, and the three list calls.

Runs on the host and is the only privileged component. It talks to the
docker daemon; nothing it launches can. It holds no job state in memory --
every method reads the job directory -- so a second CLI invocation, the MCP
server, and a client that reconnects all see the same thing.

The contract that shapes this file: **everything returned goes through the
redactor.** check() output lands in a cloud model's context and is then
spoken out loud, so :meth:`_out` is the single exit through which every
public method returns, and ``tests/test_supervisor_contract.py`` asserts
there is no way around it.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import backends, ids
from .credentials import (
    AmbiguousCredential,
    CredentialError,
    UnknownCredential,
)
from .narration import append as narrate
from .narration import read_tail
from .redaction import Redactor
from .registry import (
    CONFIG_DIR_ENV,
    ROOT_ENV,
    SECRETS_ENV,
    AmbiguousRepo,
    Config,
    UnknownAgent,
    UnknownRepo,
    load,
)
from .state import JobPaths, JobState, Meta, Status, pid_alive
from .worktree import GitError, Workspace, prepare, remove

__all__ = ["Supervisor", "SupervisorError"]

log = logging.getLogger("orchestrator.supervisor")


class SupervisorError(RuntimeError):
    """Something the caller can act on. The message is safe to speak."""


@dataclass
class Supervisor:
    config: Config
    redactor: Redactor

    @classmethod
    def create(cls, config: Config | None = None, environ=None) -> "Supervisor":
        config = config or load()
        redactor = Redactor(
            entropy_threshold=config.entropy_threshold,
            entropy_fallback=config.entropy_fallback,
        )
        # Two sources of literal secrets: the environment this process was
        # started with (likely under `op run`), and the secret files
        # bootstrap materialised. Registering both means a token that leaks
        # into a log is scrubbed by value, not merely by shape.
        redactor.register_env(config.scrub_env, os.environ if environ is None else environ)

        # Every identity under the secrets root, not just the ones listed as
        # agents. The voice bridge is an identity without being an agent,
        # and iterating agents left its OpenAI key and Discord bot token
        # unregistered -- protected only by pattern, on the one path that
        # ends in something being said out loud.
        redactor.register_dir(config.secrets_root)
        try:
            identities = sorted(p for p in config.secrets_root.iterdir() if p.is_dir())
        except OSError:
            identities = []
        for identity in identities:
            redactor.register_dir(identity)

        # The 1Password service account token is itself a credential, and
        # it is read from a file rather than the environment -- so without
        # this it would be the one secret in the system that scrub_env
        # could not catch.
        from .credentials import service_account_token

        redactor.register_value("OP_SERVICE_ACCOUNT_TOKEN", service_account_token())

        # Anything configured explicitly elsewhere still counts.
        for agent in config.agents.values():
            if agent.secrets_dir:
                redactor.register_dir(Path(agent.secrets_dir))
        return cls(config=config, redactor=redactor)

    # -- the two operations the voice front-end needs -----------------------

    def ask(self, agent: str, message: str, repo: str | None = None) -> dict:
        """Start a job. Returns as soon as the job exists, not when it ends."""
        if not message or not message.strip():
            return self._out({"error": "empty_message", "message": "nothing to ask"})

        try:
            agent_spec = self.config.agent(agent)
        except UnknownAgent as exc:
            return self._out(
                {
                    "error": "unknown_agent",
                    "message": f"no agent called {exc.name!r}",
                    "known": exc.known,
                }
            )

        try:
            repo_spec = self.config.resolve_repo(repo, agent_spec)
        except AmbiguousRepo as exc:
            # Refuse rather than guess: the wrong guess means an agent
            # committing to the wrong repository.
            return self._out(
                {
                    "error": "ambiguous_repo",
                    "message": f"{exc.query!r} matches more than one repo",
                    "candidates": exc.candidates,
                }
            )
        except UnknownRepo as exc:
            return self._out(
                {
                    "error": "unknown_repo",
                    "message": f"no repo matching {exc.query!r}",
                    "known": exc.known,
                }
            )

        job_id, job_root = ids.allocate(self.config.jobs_dir, ids.load_wordlist())
        paths = JobPaths(job_root)

        try:
            workspace = prepare(
                job_id=job_id,
                repo=repo_spec,
                repo_path=self.config.repo_path(repo_spec.name) if repo_spec else None,
                worktrees_dir=self.config.worktrees_dir,
                scratch_dir=self.config.scratch_dir,
            )
        except GitError as exc:
            Status(state=JobState.FAILED, detail=str(exc)).write(paths)
            narrate(paths.narration, f"could not create a workspace: {exc}")
            return self._out({"error": "workspace_failed", "message": str(exc), "job_id": job_id})

        Meta(
            job_id=job_id,
            agent=agent,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            workdir=str(workspace.path),
            # Assigned here, passed to claude as --session-id, so reply()
            # can --resume it without scraping the id out of the log.
            session_id=str(uuid.uuid4()),
            repo=repo_spec.name if repo_spec else None,
            base_ref=repo_spec.base_ref if repo_spec else None,
            branch=workspace.branch,
            worktree=workspace.is_worktree,
        ).write(paths)

        # On disk, not on the command line: argv is world-readable via ps.
        paths.prompt.write_text(message, encoding="utf-8")
        Status(state=JobState.QUEUED).write(paths)

        pid = self._spawn_runner(paths)
        return self._out(
            {
                "job_id": job_id,
                "agent": agent,
                "repo": repo_spec.name if repo_spec else None,
                "workdir": str(workspace.path),
                "branch": workspace.branch,
                "runner_pid": pid,
            }
        )

    def check(self, job_id: str, tail: int = 0, narration_lines: int | None = None) -> dict:
        """Report on a job.

        Returns a state and a few narration lines. Raw log output is only
        ever included when *tail* is passed explicitly, because this reply
        is about to become a voice model's context.
        """
        paths = self._paths(job_id)
        if paths is None:
            return self._out({"error": "unknown_job", "message": f"no job called {job_id!r}"})

        status = self._reconciled(paths)
        n = narration_lines if narration_lines is not None else self.config.default_narration_lines
        n = max(0, min(n, self.config.max_narration_lines))

        result: dict = {
            "job_id": job_id,
            "state": str(status.state),
            "narration": [line.text for line in read_tail(paths.narration, n)],
        }
        if status.state is JobState.FAILED and status.detail:
            result["detail"] = status.detail

        if tail:
            result["log_tail"] = self._log_tail(paths, min(tail, self.config.max_tail_lines))

        return self._out(result)

    def reply(self, job_id: str, message: str) -> dict:
        """Answer a job that is waiting on you, and let it carry on.

        Optional for the front-end: a voice model that never calls this
        still sees the question through check(), and you can start a fresh
        ask() instead.
        """
        paths = self._paths(job_id)
        if paths is None:
            return self._out({"error": "unknown_job", "message": f"no job called {job_id!r}"})

        meta = Meta.read(paths)
        try:
            agent_spec = self.config.agent(meta.agent)
        except UnknownAgent as exc:
            return self._out({"error": "unknown_agent", "message": str(exc), "known": exc.known})
        if agent_spec.type in {"container", "local"} and not agent_spec.can_resume:
            return self._out(
                {
                    "error": "cannot_resume",
                    "message": (
                        f"{meta.agent} has no way to resume a session -- "
                        f"start a new job with the answer included"
                    ),
                }
            )

        status = self._reconciled(paths)
        if not status.state.is_parked:
            return self._out(
                {
                    "error": "not_waiting",
                    "message": f"{job_id} is {status.state}, not waiting for input",
                    "state": str(status.state),
                }
            )

        paths.reply.write_text(message, encoding="utf-8")
        narrate(paths.narration, "got a reply, resuming")
        Status(state=JobState.QUEUED).write(paths)
        pid = self._spawn_runner(paths, resume=True)
        return self._out({"job_id": job_id, "state": str(JobState.QUEUED), "runner_pid": pid})

    def stop(self, job_id: str) -> dict:
        """Stop a running job.

        Two kills, because one is not enough. The runner is its own process
        group leader, so signalling the group ends it and the `docker exec`
        client with it -- but a docker exec leaves the process inside the
        container running when its client dies, so the backend also reaches
        in and ends the agent by its session id.
        """
        paths = self._paths(job_id)
        if paths is None:
            return self._out({"error": "unknown_job", "message": f"no job called {job_id!r}"})

        status = self._reconciled(paths)
        if status.state.is_terminal:
            return self._out(
                {
                    "error": "already_finished",
                    "message": f"{job_id} already {status.state}",
                    "state": str(status.state),
                }
            )

        meta = Meta.read(paths)
        killed = self._kill_runner(status.runner_pid)

        try:
            agent_spec = self.config.agent(meta.agent)
            backends.for_agent(agent_spec).stop(agent=agent_spec, meta=meta)
        except Exception as exc:  # noqa: BLE001 - the job still stops
            log.debug("backend stop failed for %s: %s", job_id, exc)

        # Anything lent to this job goes back now rather than outliving it.
        released = 0
        try:
            released = len(self.config.credential_store().revoke_for_job(job_id))
        except Exception:  # noqa: BLE001
            log.debug("could not release grants for %s", job_id, exc_info=True)

        narrate(paths.narration, "stopped at your request")
        Status(
            state=JobState.STOPPED,
            runner_pid=status.runner_pid,
            exit_code=status.exit_code,
            detail="stopped on request",
        ).write(paths)

        return self._out(
            {
                "job_id": job_id,
                "state": str(JobState.STOPPED),
                "runner_stopped": killed,
                "credentials_released": released,
            }
        )

    def _kill_runner(self, pid: int | None) -> bool:
        """Signal the runner's process group, then insist.

        The group, not the process: ask() starts the runner with
        start_new_session, so it leads a group containing whatever it
        spawned. Signalling only the runner would orphan its children.
        """
        if not pid or not pid_alive(pid):
            return False
        try:
            group = os.getpgid(pid)
        except ProcessLookupError:
            return False

        for sig in (signal.SIGTERM, signal.SIGKILL):
            if not self._signal(pid, group, sig):
                return not pid_alive(pid)
            # A moment to exit cleanly before insisting.
            for _ in range(20):
                self._reap(pid)
                if not pid_alive(pid):
                    return True
                time.sleep(0.05)
        return not pid_alive(pid)

    @staticmethod
    def _signal(pid: int, group: int, sig: int) -> bool:
        """Signal the group, or failing that the process. True if delivered.

        The group first, because the runner leads one containing whatever
        it spawned and signalling only the runner would orphan its
        children. But group signalling is not uniformly permitted across
        platforms even for one's own child, so a refusal there falls back
        to the process itself rather than giving up -- a stopped job that
        keeps running is the worst outcome available.
        """
        for target, deliver in ((group, os.killpg), (pid, os.kill)):
            try:
                deliver(target, sig)
                return True
            except ProcessLookupError:
                return False  # already gone
            except PermissionError:
                continue
        log.warning("not permitted to stop pid %s", pid)
        return False

    @staticmethod
    def _reap(pid: int) -> None:
        """Collect the runner if it was our child and has exited.

        Nothing waits on the runner otherwise, so it would linger as a
        zombie and keep answering "yes" to being alive.
        """
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass

    # -- listings -----------------------------------------------------------

    def list_agents(self) -> dict:
        return self._out(
            {
                "agents": [
                    {
                        "name": a.name,
                        "type": a.type,
                        "description": a.description,
                        "default_repo": a.default_repo,
                        "needs_repo": a.needs_repo,
                    }
                    for a in self.config.agents.values()
                ]
            }
        )

    def list_repos(self) -> dict:
        """The vocabulary the voice model can use when naming a repo."""
        return self._out(
            {
                "repos": [
                    {"name": r.name, "base_ref": r.base_ref, "aliases": list(r.aliases)}
                    for r in self.config.repos.values()
                ]
            }
        )

    def list_jobs(self, active_only: bool = False, limit: int = 20) -> dict:
        """Jobs, newest first. Deliberately terse -- this gets read aloud."""
        jobs = []
        if self.config.jobs_dir.is_dir():
            for root in sorted(self.config.jobs_dir.iterdir()):
                if not root.is_dir():
                    continue
                paths = JobPaths(root)
                status = self._reconciled(paths)
                if active_only and status.state.is_terminal:
                    continue
                try:
                    meta = Meta.read(paths)
                    agent, created, repo = meta.agent, meta.created_at, meta.repo
                except (OSError, ValueError, TypeError):
                    agent = created = repo = None
                jobs.append(
                    {
                        "job_id": root.name,
                        "state": str(status.state),
                        "agent": agent,
                        "repo": repo,
                        "created_at": created,
                        "updated_at": status.updated_at,
                    }
                )
        jobs.sort(key=lambda j: j["created_at"] or "", reverse=True)
        return self._out({"jobs": jobs[:limit]})

    # -- credential delegation ----------------------------------------------
    # The vault is the pool you are willing to share; a grant is what is
    # actually live. Values never pass through here -- only titles, the
    # filename a grant uses, and the environment variable it becomes.

    def list_credentials(self) -> dict:
        """Titles in the vault. The menu, never the contents."""
        try:
            items = self.config.credential_store().available()
        except CredentialError as exc:
            return self._out({"error": "vault_unavailable", "message": str(exc)})
        return self._out(
            {
                "vault": self.config.vault,
                "credentials": [
                    {"title": i.title, "env_var": i.env_var} for i in items
                ],
            }
        )

    def grant(self, agent: str, credential: str, job_id: str | None = None) -> dict:
        """Expose one credential to one agent.

        Takes effect immediately: the per-agent secrets directory is
        already bind-mounted into that container, so the next job sees it
        without a restart.
        """
        try:
            self.config.agent(agent)
        except UnknownAgent as exc:
            return self._out(
                {
                    "error": "unknown_agent",
                    "message": f"no agent called {exc.name!r}",
                    "known": exc.known,
                }
            )
        if job_id is not None and self._paths(job_id) is None:
            return self._out({"error": "unknown_job", "message": f"no job called {job_id!r}"})

        try:
            granted = self.config.credential_store().grant(agent, credential, job_id=job_id)
        except AmbiguousCredential as exc:
            return self._out(
                {
                    "error": "ambiguous_credential",
                    "message": str(exc),
                    "candidates": exc.candidates,
                }
            )
        except UnknownCredential as exc:
            return self._out(
                {"error": "unknown_credential", "message": str(exc), "known": exc.known}
            )
        except CredentialError as exc:
            return self._out({"error": "grant_failed", "message": str(exc)})

        return self._out(
            {
                "granted": granted.title,
                "agent": granted.agent,
                "env_var": granted.env_var,
                "job_id": granted.job_id,
                "scope": "this job only" if granted.job_id else "until revoked",
            }
        )

    def revoke(self, agent: str, credential: str) -> dict:
        """Withdraw a credential from an agent.

        Stops future reads. A job already holding the value keeps it until
        it exits -- end the job if you need that back.
        """
        store = self.config.credential_store()
        name = credential.strip().lower()
        # Accept the spoken title as well as the filename.
        if not any(g.name == name for g in store.grants()):
            for g in store.grants():
                if g.title and g.title.lower() == credential.strip().lower():
                    name = g.name
                    break
        try:
            removed = store.revoke(agent, name)
        except CredentialError as exc:
            return self._out({"error": "revoke_failed", "message": str(exc)})
        if not removed:
            return self._out(
                {
                    "error": "not_granted",
                    "message": f"{agent} does not have {credential!r}",
                }
            )
        return self._out({"revoked": name, "agent": agent})

    def list_grants(self) -> dict:
        """What each agent can currently reach."""
        try:
            grants = self.config.credential_store().grants()
        except CredentialError as exc:
            return self._out({"error": "vault_unavailable", "message": str(exc)})
        return self._out(
            {
                "grants": [
                    {
                        "agent": g.agent,
                        "credential": g.title or g.name,
                        "env_var": g.env_var,
                        "job_id": g.job_id,
                        "granted_at": g.granted_at,
                    }
                    for g in grants
                ]
            }
        )

    # -- internals ----------------------------------------------------------

    def _out(self, payload: dict) -> dict:
        """The single exit from this class. Everything is scrubbed here.

        Public methods return ``self._out(...)`` and nothing else -- see
        tests/test_supervisor_contract.py, which enforces that by reading
        this module's source.
        """
        return self.redactor.scrub_obj(payload)  # type: ignore[return-value]

    def _paths(self, job_id: str) -> JobPaths | None:
        # job_id arrives from an MCP client and is about to be joined onto a
        # path, so it is validated against the id grammar, not sanitised.
        if not ids.is_valid(job_id):
            return None
        root = self.config.jobs_dir / job_id
        return JobPaths(root) if root.is_dir() else None

    def _reconciled(self, paths: JobPaths) -> Status:
        """Read status, catching runners that died without writing.

        A runner killed by the OOM killer or a reboot leaves 'running'
        behind forever. check() is the only thing that would notice, so it
        is the thing that repairs it.
        """
        status = Status.read(paths)
        if status.state.is_active and status.runner_pid and not pid_alive(status.runner_pid):
            status = Status(
                state=JobState.FAILED,
                runner_pid=status.runner_pid,
                exit_code=status.exit_code,
                detail="runner process disappeared",
            )
            status.write(paths)
        return status

    def _log_tail(self, paths: JobPaths, n: int) -> list[str]:
        try:
            raw = paths.raw.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return []
        return raw.splitlines()[-n:]

    def _spawn_runner(self, paths: JobPaths, resume: bool = False) -> int:
        """Start the runner detached, so it outlives this process.

        start_new_session puts it in its own session: the runner does not
        die when an MCP client disconnects or a terminal closes.
        """
        cmd = [sys.executable, "-m", "orchestrator.runner"]
        if resume:
            cmd.append("--resume")
        cmd.append(str(paths.root))

        env = dict(os.environ)
        src = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if "PYTHONPATH" in env else "")
        # The runner is a fresh process: tell it which registry and which
        # runtime root this supervisor is working from, so the two cannot
        # drift apart mid-job.
        env[CONFIG_DIR_ENV] = str(self.config.config_dir)
        env[ROOT_ENV] = str(self.config.root)
        # The runner revokes this job's grants when it ends, so it has to
        # look in the same secrets directory the supervisor granted into.
        env[SECRETS_ENV] = str(self.config.secrets_root)

        with open(os.devnull, "rb") as devnull, paths.raw.open("ab") as log:
            proc = subprocess.Popen(
                cmd,
                stdin=devnull,
                stdout=log,
                stderr=log,
                start_new_session=True,
                env=env,
                cwd=src,
            )
        return proc.pid

    # -- housekeeping -------------------------------------------------------

    def reap(self, job_id: str, keep_branch: bool = True) -> dict:
        """Delete a finished job's workspace and free its name."""
        paths = self._paths(job_id)
        if paths is None:
            return self._out({"error": "unknown_job", "message": f"no job called {job_id!r}"})
        status = self._reconciled(paths)
        if not status.state.is_terminal:
            return self._out(
                {"error": "still_running", "message": f"{job_id} is {status.state}"}
            )
        meta = Meta.read(paths)
        remove(
            job_id,
            self.config.repo_path(meta.repo) if meta.repo else None,
            Path(meta.workdir),
            keep_branch=keep_branch,
        )
        shutil.rmtree(paths.root, ignore_errors=True)
        return self._out({"job_id": job_id, "reaped": True, "branch_kept": keep_branch})
