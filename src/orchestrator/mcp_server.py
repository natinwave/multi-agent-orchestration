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
            "tell the user its name, and check back when they ask."
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
