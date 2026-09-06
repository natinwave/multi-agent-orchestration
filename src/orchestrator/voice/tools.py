"""Exposing the supervisor's MCP tools to the realtime model.

The MCP server is used *in process* rather than over stdio: the voice
process and the supervisor are the same program, so a subprocess and a
protocol hop would buy nothing. What it does buy, by going through
``build_server`` rather than calling the supervisor directly, is that the
tool descriptions -- which are carefully written prompt surface -- and the
redaction chokepoint are shared with the MCP path instead of duplicated.

The one thing this module adds is :class:`GrantGuard`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["realtime_tools", "ToolDispatcher", "GrantGuard", "CONFIRM_WINDOW_SECONDS"]

# How long a spoken "yes" stays good for. Long enough to answer a question
# read aloud, short enough that an approval cannot be reused later in the
# conversation for a different credential.
CONFIRM_WINDOW_SECONDS = 120.0

#: Tools that hand a live secret to a process running model-authored code.
#: These are gated in code, not merely discouraged in the prompt.
CONFIRMED_TOOLS = frozenset({"grant"})


async def realtime_tools(server: Any) -> list[dict]:
    """MCP tool definitions in the shape the Realtime API expects."""
    tools = []
    for tool in await server.list_tools():
        tools.append(
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.input_schema,
            }
        )
    return tools


@dataclass
class _Pending:
    args: dict
    at: float


@dataclass
class GrantGuard:
    """Optionally requires a credential grant to be asked for twice.

    Off by default, at the owner's request after living with it: the
    read-back-and-confirm exchange is rote when you trust the model to
    interpret "give the coding agent the staging password" correctly, and
    the useful half is being *told* what happened, not being asked first.
    That half is not optional -- the tool description requires the model to
    state exactly what it granted, to whom, and for how long, so a
    misinterpretation is caught immediately rather than never.

    Turning it on restores the two-step: the first call to ``grant`` never
    grants anything, it returns the sentence to say, and only an identical
    call inside :data:`CONFIRM_WINDOW_SECONDS` is allowed through.
    "Identical" is deliberate -- confirming the staging password for hermes
    would not release the production key, or the same key to another agent.
    """

    #: Whether to require the two-step. Off means a grant happens at once.
    enabled: bool = False
    window: float = CONFIRM_WINDOW_SECONDS
    _pending: dict[str, _Pending] = field(default_factory=dict, repr=False)
    _clock: Any = time.monotonic

    @staticmethod
    def _key(name: str, args: dict) -> str:
        # job_id is excluded: scoping a grant to a job is a narrowing, and
        # re-asking because the model added it would only teach the user
        # that confirmations are noise.
        return json.dumps(
            [name, str(args.get("agent", "")).lower(), str(args.get("credential", "")).lower()]
        )

    def check(self, name: str, args: dict) -> dict | None:
        """Return a response to send instead of calling, or None to allow."""
        if not self.enabled or name not in CONFIRMED_TOOLS:
            return None

        key = self._key(name, args)
        now = self._clock()
        pending = self._pending.get(key)

        if pending is not None and now - pending.at <= self.window:
            del self._pending[key]
            return None  # confirmed: let it through

        self._pending[key] = _Pending(args=dict(args), at=now)
        agent = args.get("agent", "that agent")
        credential = args.get("credential", "that credential")
        scope = "for this job only" if args.get("job_id") else "until it is revoked"
        return {
            "status": "needs_confirmation",
            "say": (
                f"This will give {agent} the {credential}, {scope}. "
                f"Say yes and I'll do it."
            ),
            "instructions": (
                "Nothing has been granted. Say the sentence in 'say' out loud, "
                "wait for the user to agree, then call grant again with exactly "
                "the same agent and credential. If they decline or change the "
                "subject, do not call it again."
            ),
        }

    def forget(self) -> None:
        """Drop every pending confirmation. Call when the call ends."""
        self._pending.clear()


@dataclass
class ToolDispatcher:
    """Runs a tool call from the model against the supervisor."""

    server: Any
    guard: GrantGuard = field(default_factory=GrantGuard)

    async def call(self, name: str, raw_arguments: str) -> str:
        """Execute one call and return the JSON string to send back.

        Never raises: a tool error the model can read and explain is worth
        far more than a dropped session.
        """
        try:
            args = json.loads(raw_arguments) if raw_arguments else {}
        except json.JSONDecodeError:
            return json.dumps({"error": "arguments were not valid JSON"})
        if not isinstance(args, dict):
            return json.dumps({"error": "arguments must be an object"})

        held = self.guard.check(name, args)
        if held is not None:
            return json.dumps(held)

        try:
            result = await self.server.call_tool(name, args)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model, not raised
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})

        return self._flatten(result)

    @staticmethod
    def _flatten(result: Any) -> str:
        """MCP returns content blocks; the model wants one JSON string.

        The supervisor has already redacted everything inside.
        """
        content = getattr(result, "content", None)
        if not content:
            return json.dumps({"ok": True})
        texts = [getattr(block, "text", "") for block in content]
        joined = "\n".join(t for t in texts if t)
        return joined or json.dumps({"ok": True})
