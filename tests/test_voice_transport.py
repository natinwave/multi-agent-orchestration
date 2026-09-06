"""The transport boundary.

Discord's voice-receive support is unofficial and could break. The point of
this seam is that when it does, only one file changes -- so these tests
pin that the boundary is real and that everything above it can run without
Discord at all.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from orchestrator.voice import protocol
from orchestrator.voice.transport import Transport
from orchestrator.voice.transport.loopback import LoopbackTransport
from orchestrator.voice.realtime import RealtimeSession


class FakeServer:
    async def call_tool(self, name, args):
        class Result:
            content = [type("B", (), {"text": json.dumps({"job_id": "kestrel"})})()]

        return Result()


def test_loopback_satisfies_the_contract() -> None:
    assert isinstance(LoopbackTransport(), Transport)


def test_discord_satisfies_the_contract_without_pycord_installed() -> None:
    """The adapter must import and typecheck even where py-cord is absent,
    or no other transport can be developed on a machine without it."""
    from orchestrator.voice.transport.discord import DiscordTransport

    assert isinstance(DiscordTransport(token="t", channel_id=1), Transport)


def test_discord_only_needs_pycord_when_it_actually_connects() -> None:
    from orchestrator.voice.transport.discord import DiscordTransport

    t = DiscordTransport(token="t", channel_id=1)
    # Several samples: the upsampler holds the last one back to interpolate
    # against, so a single sample legitimately produces nothing.
    asyncio.run(t.play(b"\x01\x02" * 8))
    assert not t.playback.is_empty


def test_the_session_runs_over_a_transport_with_no_discord(  ) -> None:
    """The whole point of the boundary: everything above it is testable."""
    transport = LoopbackTransport()
    session = RealtimeSession(
        api_key="unused",
        server=FakeServer(),
        on_audio=transport.play,
        on_interrupt=transport.interrupt,
    )
    from orchestrator.voice.tools import ToolDispatcher

    session._dispatcher = ToolDispatcher(session.server)
    sent = []

    async def fake_send(event):
        sent.append(event)

    session._send = fake_send  # type: ignore[method-assign]

    pcm = b"\x10\x20\x30\x40"
    asyncio.run(
        session.handle_event(
            {"type": protocol.OUTPUT_AUDIO_DELTA, "delta": base64.b64encode(pcm).decode()}
        )
    )
    assert transport.audio == pcm


def test_an_interrupt_reaches_the_transport() -> None:
    transport = LoopbackTransport()
    session = RealtimeSession(
        api_key="unused",
        server=FakeServer(),
        on_audio=transport.play,
        on_interrupt=transport.interrupt,
    )

    async def fake_send(event):
        pass

    session._send = fake_send  # type: ignore[method-assign]
    asyncio.run(transport.play(b"\x01" * 100))
    asyncio.run(session.handle_event({"type": protocol.SPEECH_STARTED}))
    assert transport.interrupts == 1
    assert transport.audio == b"", "queued audio must be dropped, not just cancelled"


def test_transports_convert_to_the_api_format_themselves() -> None:
    """A transport hands the session 24 kHz mono PCM16 whatever its own
    wire format is, so a SIP adapter at 8 kHz needs no change above it."""
    from orchestrator.voice.transport.discord import DiscordTransport

    t = DiscordTransport(token="t", channel_id=1)
    assert hasattr(t, "_down") and hasattr(t, "_up")


def test_nothing_outside_the_transport_package_imports_discord() -> None:
    """If this fails, swapping transports stops being a one-file change."""
    from pathlib import Path

    voice = Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "voice"
    for path in voice.rglob("*.py"):
        if "transport" in path.parts:
            continue
        text = path.read_text()
        assert "import discord" not in text, path
        assert "DiscordTransport" not in text or path.name == "__main__.py", path


def test_a_missing_channel_id_is_reported_not_crashed() -> None:
    """discord_channel_id defaults to 0 in the template, which is falsy --
    it must read as 'you have not set this' rather than channel zero."""
    from orchestrator.voice.__main__ import build_transport
    from pathlib import Path

    transport, missing = build_transport("discord", {"discord_channel_id": 0}, Path("/nope"))
    assert transport is None
    assert any("discord_channel_id" in m for m in missing)


def test_an_unknown_transport_is_named() -> None:
    from orchestrator.voice.__main__ import build_transport
    from pathlib import Path

    transport, missing = build_transport("carrier-pigeon", {}, Path("/nope"))
    assert transport is None
    assert "carrier-pigeon" in missing[0]


def test_loopback_needs_no_discord_config() -> None:
    """So it can verify credentials with Discord entirely out of the way."""
    from orchestrator.voice.__main__ import build_transport
    from pathlib import Path

    transport, missing = build_transport("loopback", {}, Path("/nope"))
    assert transport is not None and missing == []


def test_loopback_lingers_so_server_events_can_arrive() -> None:
    """Returning immediately closes the session before a single event is
    read, so a rejected session.update looks exactly like a clean run."""
    import time

    from orchestrator.voice.transport.loopback import LoopbackTransport

    class Dummy:
        async def send_audio(self, pcm):
            pass

    t = LoopbackTransport(linger=0.05)
    started = time.monotonic()
    asyncio.run(t.run(Dummy()))
    assert time.monotonic() - started >= 0.05
    assert t.finished.is_set()


def test_the_session_records_when_configuration_is_accepted() -> None:
    """Connecting proves the key works. This proves the config does."""
    from orchestrator.voice import protocol

    session = RealtimeSession(api_key="k", server=FakeServer(), on_audio=LoopbackTransport().play)
    assert not session.configured.is_set()
    asyncio.run(session.handle_event({"type": protocol.SESSION_UPDATED}))
    assert session.configured.is_set()


def test_verbose_raises_our_logging_not_everything() -> None:
    """Root-level DEBUG makes websockets log every frame, which buries the
    handful of lines that explain a call and puts raw frame contents in the
    journal, outside the redaction chokepoint. --verbose has to be
    targeted or it cannot be left on in a service."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "src" / "orchestrator" / "voice" / "__main__.py"
    ).read_text()

    assert 'logging.getLogger("orchestrator").setLevel(logging.DEBUG)' in source
    assert "level=logging.DEBUG" not in source, "root logger must not go to DEBUG"
    for noisy in ("websockets", "asyncio"):
        assert noisy in source, f"{noisy} should be pinned down explicitly"
