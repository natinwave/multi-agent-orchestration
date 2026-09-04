"""The Discord side: a bot that joins a voice channel and carries audio.

Discord was chosen over a web page because the phone should be able to be
in a pocket. A browser tab needs the screen on and the page in front; a
native app has background-audio privileges and push notifications, which
is what "like a phone call" actually requires.

NONE OF THIS FILE CAN BE TESTED FROM A DEVELOPMENT MACHINE. It needs a bot
token, a server, and a voice connection. Everything decidable without one
lives in audio.py, playback.py, tools.py and realtime.py, which are tested
properly; this file is deliberately thin glue and nothing more.

Requires py-cord rather than discord.py: receiving voice is not part of
discord.py's supported API. Note that both install as `discord`, so having
them both in one environment breaks in confusing ways.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .audio import Downsampler, Upsampler
from .playback import FRAME_BYTES, PlaybackBuffer

__all__ = ["VoiceBridge", "build_bot"]

log = logging.getLogger("orchestrator.voice.discord")


class VoiceBridge:
    """Carries audio between one Discord voice connection and one session.

    Owns the two resamplers and the playback buffer, so the session and
    the bot never have to know each other's audio format.
    """

    def __init__(self, session: Any, loop: asyncio.AbstractEventLoop) -> None:
        self.session = session
        self.loop = loop
        self.playback = PlaybackBuffer()
        self._down = Downsampler()
        self._up = Upsampler()

    # -- microphone -> model ------------------------------------------------

    def on_discord_audio(self, pcm48_stereo: bytes) -> None:
        """Called from Discord's decoder thread, not the event loop.

        Hence run_coroutine_threadsafe: touching the session directly from
        this thread would corrupt the WebSocket's write state.
        """
        pcm24_mono = self._down.feed(pcm48_stereo)
        if not pcm24_mono:
            return
        asyncio.run_coroutine_threadsafe(self.session.send_audio(pcm24_mono), self.loop)

    # -- model -> speaker ---------------------------------------------------

    async def on_model_audio(self, pcm24_mono: bytes) -> None:
        self.playback.write(self._up.feed(pcm24_mono))

    async def on_interrupt(self) -> None:
        """The user started talking. Stop playing immediately.

        Cancelling generation is not enough on its own -- whatever is
        already buffered would keep playing over them for a second or two,
        which is exactly the thing that makes a voice assistant infuriating.
        """
        self.playback.clear()
        self._up.flush()
        log.debug("interrupted: playback dropped")


def build_bot(session_factory, channel_id: int, token: str) -> Any:
    """Construct the Discord client.

    Imported lazily so the rest of the voice package -- and every test --
    works without py-cord installed.
    """
    import discord  # py-cord

    if not hasattr(discord, "sinks"):
        raise RuntimeError(
            "this needs py-cord (which supports receiving voice), not discord.py. "
            "Both install as `discord`; uninstall discord.py first."
        )

    intents = discord.Intents.default()
    intents.voice_states = True
    intents.message_content = False
    bot = discord.Bot(intents=intents)

    class _Sink(discord.sinks.Sink):
        """Forwards each decoded packet instead of recording to a file.

        py-cord's sinks are built for recording a whole call and handing
        back a file at the end. write() is called per packet, so it is the
        streaming hook -- the accumulated audio is simply never used.
        """

        def __init__(self, bridge: VoiceBridge) -> None:
            super().__init__()
            self.bridge = bridge

        def write(self, data, user):  # noqa: D102 - py-cord's interface
            self.bridge.on_discord_audio(bytes(data))

    class _Source(discord.AudioSource):
        """Pulls 20 ms frames from the buffer on Discord's clock."""

        def __init__(self, bridge: VoiceBridge) -> None:
            self.bridge = bridge

        def read(self) -> bytes:
            return self.bridge.playback.read_frame()

        def is_opus(self) -> bool:
            return False

    @bot.event
    async def on_ready() -> None:  # noqa: D103
        log.info("connected to discord as %s", bot.user)
        channel = bot.get_channel(channel_id)
        if channel is None:
            log.error("cannot see channel %s -- is the bot invited?", channel_id)
            return

        voice_client = await channel.connect()
        bridge, session = await session_factory(bot.loop)

        voice_client.play(_Source(bridge))
        voice_client.start_recording(_Sink(bridge), lambda *a: None)
        log.info("listening in %s", channel.name)

        await session.run()

    bot._orchestration_token = token  # kept off the module surface
    return bot
