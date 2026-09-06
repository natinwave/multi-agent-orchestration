"""Telling you something happened, without being asked.

A voice agent that only reports when questioned is a worse tool than a
text log: you have to remember to ask, and the whole point of delegating
work is not having to hold it in your head.

The realtime session is a socket we keep open for the length of the call,
so it can be pushed as well as pulled. This watches the jobs while you
talk and, when one finishes or gets stuck, hands the model a sentence and
asks it to speak.

Restraint is the design constraint. Being interrupted by a machine is
worse than not being told, so this announces only transitions that change
what you might do next, never progress for its own sake, and it waits for
a gap in the conversation rather than talking over you.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["JobWatcher", "NOTIFY_STATES", "describe"]

log = logging.getLogger("orchestrator.voice.watcher")

#: Transitions worth interrupting a conversation for. A job going from
#: queued to running is progress, not news -- it changes nothing you would
#: do differently, so it is not worth a sentence.
NOTIFY_STATES = frozenset({"done", "failed", "awaiting_input", "blocked"})
# "stopped" is deliberately absent: you only get there by asking, and being
# told about a thing you just did is noise.

#: How long a job must hold a state before it is announced. Without this a
#: job that fails instantly is announced before the model has finished
#: saying it started.
SETTLE_SECONDS = 2.0


def describe(job_id: str, state: str, narration: list[str]) -> str:
    """One sentence, written to be read aloud.

    The last narration line carries the substance: for a parked job it is
    the question, and for a failure it is the last thing that worked.
    """
    last = next((line for line in reversed(narration) if line.strip()), "")
    match state:
        case "done":
            return f"{job_id} finished. {last}".strip()
        case "failed":
            return f"{job_id} failed. {last}".strip()
        case "awaiting_input":
            return f"{job_id} needs an answer: {last}".strip()
        case "blocked":
            return f"{job_id} is stuck: {last}".strip()
        case _:
            return f"{job_id} is now {state}."


@dataclass
class JobWatcher:
    """Polls the supervisor during a call and reports what changed.

    Goes through the MCP server rather than the supervisor directly, so
    everything it reads has been through the same redaction chokepoint as
    every other answer -- these sentences are spoken aloud.
    """

    server: Any
    #: Called with a sentence to say. Should not block for long.
    announce: Callable[[str], Awaitable[None]]
    #: Whether the model is mid-sentence. Announcements wait for a gap.
    is_busy: Callable[[], bool] = lambda: False
    interval: float = 8.0
    _seen: dict[str, str] = field(default_factory=dict, repr=False)
    _pending: list[str] = field(default_factory=list, repr=False)

    async def run(self) -> None:
        """Watch until cancelled. Never raises: a call outlives this."""
        try:
            self._seen = await self._states()
            log.debug("watching %d job(s)", len(self._seen))
        except Exception:  # noqa: BLE001
            log.debug("could not read jobs at start of call", exc_info=True)

        while True:
            try:
                await asyncio.sleep(self.interval)
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad poll must not end the call
                log.debug("poll failed", exc_info=True)

    async def poll(self) -> None:
        """One pass. Separated out so tests need no clock."""
        current = await self._states()

        for job_id, state in current.items():
            was = self._seen.get(job_id)
            if was is None:
                # A job that appeared during the call is one the model just
                # started; it already said so. Announcing it would be an
                # echo.
                continue
            if state != was and state in NOTIFY_STATES:
                self._pending.append(await self._sentence(job_id, state))

        self._seen = current
        await self._flush()

    async def _flush(self) -> None:
        """Say what is queued, once there is a gap to say it in."""
        if not self._pending or self.is_busy():
            return
        # Several at once become one sentence rather than a monologue.
        message = " ".join(self._pending)
        self._pending.clear()
        log.info("announcing: %s", message)
        await self.announce(message)

    async def _states(self) -> dict[str, str]:
        result = await self._call("list_jobs", {})
        return {job["job_id"]: job["state"] for job in result.get("jobs", [])}

    async def _sentence(self, job_id: str, state: str) -> str:
        try:
            detail = await self._call("check", {"job_id": job_id})
            narration = detail.get("narration", [])
        except Exception:  # noqa: BLE001 - the state alone is still worth saying
            narration = []
        return describe(job_id, state, narration)

    async def _call(self, name: str, args: dict) -> dict:
        result = await self.server.call_tool(name, args)
        content = getattr(result, "content", None) or []
        for block in content:
            text = getattr(block, "text", "")
            if text:
                return json.loads(text)
        return {}
