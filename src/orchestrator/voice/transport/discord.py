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

from ..audio import Downsampler, Upsampler
from ..playback import PlaybackBuffer

__all__ = ["DiscordTransport"]

log = logging.getLogger("orchestrator.voice.discord")


class DiscordTransport:
    """A Discord voice connection, presented as 24 kHz mono PCM16.

    Owns the resamplers and the playback buffer, so nothing above this
    class knows Discord speaks 48 kHz stereo.
    """

    def __init__(self, token: str, channel_id: int) -> None:
        self.token = token
        self.channel_id = channel_id
        self.playback = PlaybackBuffer()
        self._down = Downsampler()
        self._up = Upsampler()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: Any = None

    # -- the Transport contract ---------------------------------------------

    async def play(self, pcm24_mono: bytes) -> None:
        self.playback.write(self._up.feed(pcm24_mono))

    async def interrupt(self) -> None:
        self.playback.clear()
        self._up.flush()
        log.debug("interrupted: playback dropped")

    async def run(self, session: Any) -> None:
        """Connect and stay connected until the process is stopped."""
        self._session = session
        self._loop = asyncio.get_running_loop()
        bot = self._build_bot()
        await bot.start(self.token)

    # -- Discord internals --------------------------------------------------

    def _on_discord_audio(self, pcm48_stereo: bytes) -> None:
        """Called from Discord's decoder thread, not the event loop.

        Hence run_coroutine_threadsafe: touching the session from this
        thread would corrupt the WebSocket's write state.
        """
        pcm24_mono = self._down.feed(pcm48_stereo)
        if not pcm24_mono or self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._session.send_audio(pcm24_mono), self._loop)

    def _build_bot(self) -> Any:
        """Construct the client.

        Imported lazily so the rest of the voice package -- and every test
        -- works without py-cord installed.
        """
        import discord  # py-cord

        if not hasattr(discord, "sinks"):
            raise RuntimeError(
                "this needs py-cord, which supports receiving voice; discord.py "
                "does not. Both install as `discord`, so uninstall discord.py first."
            )

        # py-cord imports perfectly well without its voice dependencies and
        # only fails when you try to join a channel -- by which point the
        # bot is connected and looks healthy. Check up front instead.
        try:
            import discord.voice  # noqa: F401
        except Exception as exc:  # noqa: BLE001 - py-cord raises its own type
            raise RuntimeError(
                "py-cord is installed without its voice dependencies "
                f"({exc}). Install the extra: pip install 'py-cord[voice]' -- "
                "PyNaCl handles voice encryption and davey handles DAVE, "
                "Discord's end-to-end encrypted voice protocol."
            ) from exc

        # Nothing here is a privileged intent: the bot joins a voice channel
        # and streams. It never reads message content.
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.message_content = False
        bot = discord.Bot(intents=intents)
        transport = self

        transport._sink = lambda: _Sink()
        transport._source = lambda: _Source()

        class _Sink(discord.sinks.Sink):
            """Forwards each decoded packet instead of recording a file.

            py-cord's sinks are built to record a whole call and hand back
            a file at the end. write() is called per packet, so it is the
            streaming hook; the accumulated audio is simply never used.

            This is the unsupported surface. If a Discord change breaks
            receiving voice, it breaks here and nowhere else.
            """

            def __init__(self) -> None:
                super().__init__()

            def write(self, data, user):  # noqa: D102 - py-cord's interface
                transport._on_discord_audio(bytes(data))

        class _Source(discord.AudioSource):
            """Pulls 20 ms frames from the buffer on Discord's clock."""

            def read(self) -> bytes:
                return transport.playback.read_frame()

            def is_opus(self) -> bool:
                return False

        @bot.event
        async def on_ready() -> None:  # noqa: D103
            # discord.py swallows exceptions raised in event handlers and
            # logs "Ignoring exception in on_ready", which leaves the
            # process connected, idle and looking healthy forever. Anything
            # that goes wrong here means the bot cannot do its job, so it
            # ends the run instead of being ignored.
            try:
                await transport._join(bot)
            except Exception:
                log.exception("could not start listening; shutting down")
                await bot.close()

        return bot

    async def _join(self, bot: Any) -> None:
        log.info("connected to discord as %s", bot.user)
        channel = bot.get_channel(self.channel_id)
        if channel is None:
            raise RuntimeError(
                f"cannot see channel {self.channel_id} -- is the bot invited to "
                "that server, and does it have View Channel on that specific "
                "channel? A category override can deny it server-wide grants."
            )
        if not hasattr(channel, "connect"):
            raise RuntimeError(
                f"channel {self.channel_id} ({channel.name!r}) is not a voice "
                "channel. Both channels are often named the same; copy the id "
                "from the one under Voice Channels."
            )

        voice_client = await channel.connect()
        voice_client.play(self._source())
        voice_client.start_recording(self._sink(), lambda *a: None)
        log.info("listening in %s", channel.name)
