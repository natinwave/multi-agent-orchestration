"""MCP server wrapping the supervisor.

Speaks stdio: the OpenAI Agents SDK client runs on this same host and spawns
this as a subprocess, so there is no port to bind, nothing to authenticate,
and nothing publicly reachable. Jobs still outlive the client, because the
runners are detached and all state is on disk.

This is the only module that needs the `mcp` package. It is a thin shell --
every tool is one call into the supervisor, which has already redacted its
output. Deliberately no logic here: logic that lives here would not be
covered by the supervisor's redaction chokepoint.

Tool docstrings are prompt surface. The client model reads them to decide
what to call, so they say what the tool is for and what it costs.
"""

from __future__ import annotations

import argparse
import sys

from mcp.server.mcpserver import MCPServer

from . import __version__
from .registry import ConfigError, load
from .supervisor import Supervisor

__all__ = ["build_server", "main"]


def build_server(sup: Supervisor) -> MCPServer:
    server = MCPServer(
        name="orchestrator",
        version=__version__,
        instructions=(
            "Delegates work to background agents on this host.\n\n"
            "ask() starts a job and returns immediately with a short spoken "
            "name like 'kestrel'; the work continues in the background. Use "
            "check() to see how it is going -- it returns a state and the "
            "agent's own progress notes, and is cheap to call repeatedly. "
            "Raw logs are only returned if you pass tail=N, which is large "
            "and rarely what you want.\n\n"
            "Jobs run for minutes to hours. Do not wait on one: start it, "
            "tell the user its name, and check back when they ask.\n\n"
            "Agents hold no credentials by default. grant() hands one over "
            "and needs the user's explicit say-so first, every time -- read "
            "back what you are about to grant and to whom, then wait."
        ),
    )

    @server.tool()
    def ask(agent: str, message: str, repo: str | None = None) -> dict:
        """Start a background job and return its short spoken name.

        Returns immediately -- the job is still running when this returns.

        agent: which backend to use. Call list_agents() if unsure.
        message: what to do. Full task description; the agent cannot ask you
            for clarification without parking the job.
        repo: which repository, in whatever words the user used ("the kiln
            one"). Omit for the agent's default, or when no repo is needed.
            If the name is ambiguous this returns candidates instead of
            guessing -- ask the user which they meant and call again.
        """
        return sup.ask(agent=agent, message=message, repo=repo)

    @server.tool()
    def check(job_id: str, tail: int = 0) -> dict:
        """Report on a job: its state and the agent's recent progress notes.

        state is one of queued, running, blocked, awaiting_input, done,
        failed. 'awaiting_input' means the agent asked something and is
        parked until reply() answers it; the question is in the narration.

        tail: number of raw log lines to include as well. Defaults to 0.
            These are long and full of tool output -- only pass it when
            something failed and the narration did not explain why.
        """
        return sup.check(job_id=job_id, tail=tail)

    @server.tool()
    def reply(job_id: str, message: str) -> dict:
        """Answer a job that is parked on awaiting_input, so it can carry on.

        Only valid while the job's state is awaiting_input or blocked.
        """
        return sup.reply(job_id=job_id, message=message)

    @server.tool()
    def stop(job_id: str) -> dict:
        """Stop a running job now.

        Ends the agent's work and hands back any credential lent to that
        job. Whatever it already wrote to its branch stays -- stopping is
        not undoing, so say so if the user might expect otherwise.

        The job's state becomes 'stopped', which is distinct from 'failed'
        on purpose: it did what you asked, it did not go wrong.
        """
        return sup.stop(job_id=job_id)

    @server.tool()
    def list_agents() -> dict:
        """The agents ask() will accept, and what each is good for."""
        return sup.list_agents()

    @server.tool()
    def list_jobs(active_only: bool = False, limit: int = 20) -> dict:
        """Recent jobs and their states, newest first.

        active_only: hide finished jobs. Useful for "what's still running?".
        """
        return sup.list_jobs(active_only=active_only, limit=limit)

    @server.tool()
    def list_credentials() -> dict:
        """Credentials the user is willing to share with agents.

        Titles only -- this never returns a value. Nothing here reaches an
        agent until grant() is called for it.
        """
        return sup.list_credentials()

    @server.tool()
    def grant(agent: str, credential: str, job_id: str | None = None) -> dict:
        """Give one agent access to one credential, when asked to.

        ALWAYS SAY WHAT YOU GRANTED, TO WHOM, AND FOR HOW LONG. Name the
        credential and the agent in your reply, every time, without being
        asked. That sentence is the only thing standing between a
        misheard request and a secret going somewhere it should not, so it
        is not optional and it is not a summary -- "gave claude-code the
        staging database password, just for this job".

        Grant only what was actually asked for. Never speculatively, never
        "in case it needs it", never in the same breath as ask(). If you
        are unsure which credential was meant, ask before calling rather
        than guessing: ambiguity returns candidates, and reading those back
        is the right move.

        credential: what the user called it, in their words.
        job_id: scope it to one job so it is withdrawn when that job ends.
            Prefer this whenever the credential is for a specific piece of
            work, which is nearly always.

        Returns the environment variable the agent will find it in; tell
        the agent that name, never the value, which you never see.
        """
        return sup.grant(agent=agent, credential=credential, job_id=job_id)

    @server.tool()
    def revoke(agent: str, credential: str) -> dict:
        """Withdraw a credential from an agent.

        Stops future reads. A job already holding the value keeps it until
        it exits, so say so if the user is revoking something urgently --
        ending the job is what actually takes it back.
        """
        return sup.revoke(agent=agent, credential=credential)

    @server.tool()
    def list_grants() -> dict:
        """What each agent can currently reach, and since when."""
        return sup.list_grants()

    @server.tool()
    def list_repos() -> dict:
        """Repositories ask() can target, with the names each answers to.

        Use this to map what the user said onto a repo before calling ask().
        """
        return sup.list_repos()

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orchestrator-mcp", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "streamable-http"],
        help="stdio (default) for a client on this host; http only if you "
        "tunnel it -- never bind it to a public interface",
    )
    args = parser.parse_args(argv)

    try:
        sup = Supervisor.create(load())
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    build_server(sup).run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
