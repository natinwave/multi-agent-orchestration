"""The py-cord API surface the Discord transport depends on.

Skipped where py-cord is not installed, which is most development
machines. It runs on the orchestration host, where bootstrap runs the
suite -- so a py-cord upgrade that moves this ground is reported by the
test run rather than discovered halfway through a phone call.

Discord's voice receive is unofficial and unversioned, so this is the
surface most likely to move under us.
"""

from __future__ import annotations

import inspect

import pytest

discord = pytest.importorskip("discord", reason="py-cord is part of the [voice] extra")


def test_this_is_pycord_not_discordpy() -> None:
    """Both install as `discord`. discord.py has no sinks and cannot
    receive voice at all."""
    assert hasattr(discord, "sinks"), "discord.py is installed instead of py-cord"


def test_the_voice_dependencies_are_present() -> None:
    """py-cord imports happily without them and only fails when joining a
    channel, by which point the bot looks healthy."""
    import discord.voice  # noqa: F401


def test_sink_write_takes_data_and_user() -> None:
    """The streaming hook. If this signature changes, audio stops reaching
    the model and the bot goes quiet without erroring."""
    params = list(inspect.signature(discord.sinks.Sink.write).parameters)
    assert params == ["self", "data", "user"]


def test_a_bare_sink_constructs() -> None:
    """Our sink subclass calls super().__init__() with no arguments."""
    assert discord.sinks.Sink() is not None


def test_audio_source_read_returns_bytes_with_no_arguments() -> None:
    """Called on Discord's clock every 20 ms; anything else in the
    signature means our playback source is not a valid source."""
    assert list(inspect.signature(discord.AudioSource.read).parameters) == ["self"]
    assert list(inspect.signature(discord.AudioSource.is_opus).parameters) == ["self"]


def test_voice_client_accepts_the_calls_the_transport_makes() -> None:
    from discord.voice import VoiceClient

    play = inspect.signature(VoiceClient.play).parameters
    assert "source" in play

    recording = inspect.signature(VoiceClient.start_recording).parameters
    assert "sink" in recording and "callback" in recording


def test_our_transport_builds_a_bot_against_the_installed_pycord() -> None:
    """The whole assembly, short of connecting."""
    from orchestrator.voice.transport.discord import DiscordTransport

    transport = DiscordTransport(token="not-a-real-token", channel_id=1)
    bot = transport._build_bot()
    assert bot is not None
    assert callable(transport._sink) and callable(transport._source)
