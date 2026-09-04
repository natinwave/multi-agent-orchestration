"""An in-memory transport, for testing the session without a phone.

Everything above the transport boundary -- the session, tool dispatch, the
grant guard, barge-in -- can be exercised through this with no Discord, no
bot token and no microphone. That is most of the value of having a
boundary at all.
"""

from __future__ import annotations

import asyncio

__all__ = ["LoopbackTransport"]


class LoopbackTransport:
    """Feeds scripted audio in and records everything played back."""

    def __init__(self, incoming: list[bytes] | None = None) -> None:
        self.incoming = list(incoming or [])
        self.played: list[bytes] = []
        self.interrupts = 0
        self.finished = asyncio.Event()

    async def play(self, pcm24_mono: bytes) -> None:
        self.played.append(pcm24_mono)

    async def interrupt(self) -> None:
        self.interrupts += 1
        self.played.clear()

    async def run(self, session) -> None:
        for chunk in self.incoming:
            await session.send_audio(chunk)
        self.finished.set()

    @property
    def audio(self) -> bytes:
        return b"".join(self.played)
