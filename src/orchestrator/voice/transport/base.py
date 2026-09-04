"""The contract every transport meets.

The boundary is deliberately narrow: a transport carries audio and knows
nothing about agents, jobs or tools. It converts whatever its wire format
is to and from 24 kHz mono PCM16 -- the Realtime API's format -- so the
resampling lives with the transport that needs it and the session never
learns that Discord is 48 kHz stereo or that a phone line is 8 kHz.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["Transport"]


@runtime_checkable
class Transport(Protocol):
    """A duplex audio channel to a person."""

    async def play(self, pcm24_mono: bytes) -> None:
        """Queue audio from the model for the listener."""
        ...

    async def interrupt(self) -> None:
        """Drop anything queued, because the user started talking.

        Cancelling generation is not enough on its own: audio already
        buffered would keep playing over them, which is the single most
        irritating failure a voice assistant has.
        """
        ...

    async def run(self, session) -> None:
        """Connect, pump audio into ``session.send_audio``, and block.

        Returns when the call ends.
        """
        ...
