"""The realtime session: a WebSocket to the model, and the audio both ways.

Written directly against the API rather than through an SDK, so the wire
protocol is visible and every constant it depends on lives in
``protocol.py`` where it can be corrected in one place.

The transport is unverifiable without a live key, so everything decidable
without one -- what to do when a tool is called, what to do when the user
interrupts -- is a separate method that the tests drive directly.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import protocol
from .prompt import build_instructions
from .tools import ToolDispatcher, realtime_tools

__all__ = ["RealtimeSession", "AudioSink"]

log = logging.getLogger("orchestrator.voice")

#: Called with 24 kHz mono PCM16 as the model speaks.
AudioSink = Callable[[bytes], Awaitable[None]]


@dataclass
class RealtimeSession:
    api_key: str
    server: Any
    on_audio: AudioSink
    #: Called with no arguments when the user interrupts; drop buffered audio.
    on_interrupt: Callable[[], Awaitable[None]] | None = None
    model: str = protocol.DEFAULT_MODEL
    voice: str = protocol.DEFAULT_VOICE
    extra_instructions: str | None = None

    #: Set once the API acknowledges our session.update.
    configured: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _ws: Any = field(default=None, repr=False)
    _dispatcher: ToolDispatcher | None = field(default=None, repr=False)
    _transcript: list[str] = field(default_factory=list, repr=False)

    # -- connection ---------------------------------------------------------

    async def connect(self) -> None:
        """Open the socket and configure the session."""
        import websockets  # imported here so the core stays dependency-free

        url = f"{protocol.WS_URL}?model={self.model}"
        # No OpenAI-Beta header: that was the beta, and sending it at GA is
        # at best ignored and at worst rejected.
        headers = {"Authorization": f"Bearer {self.api_key}"}

        self._ws = await websockets.connect(url, additional_headers=headers)
        self._dispatcher = ToolDispatcher(self.server)
        tools = await realtime_tools(self.server)
        await self._send(
            protocol.session_config(
                instructions=build_instructions(self.extra_instructions),
                tools=tools,
                voice=self.voice,
                model=self.model,
            )
        )
        log.info("realtime session open: %s, %d tools", self.model, len(tools))

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    # -- sending ------------------------------------------------------------

    async def send_audio(self, pcm24_mono: bytes) -> None:
        """Append microphone audio. Already 24 kHz mono PCM16."""
        if not pcm24_mono or self._ws is None:
            return
        await self._send(
            {
                "type": protocol.INPUT_AUDIO_APPEND,
                "audio": base64.b64encode(pcm24_mono).decode(),
            }
        )

    async def _send(self, event: dict) -> None:
        if self._ws is None:
            raise RuntimeError("session is not connected")
        await self._ws.send(json.dumps(event))

    # -- receiving ----------------------------------------------------------

    async def run(self) -> None:
        """Pump server events until the socket closes."""
        if self._ws is None:
            raise RuntimeError("session is not connected")
        async for raw in self._ws:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("unparseable event from the API")
                continue
            try:
                await self.handle_event(event)
            except Exception:  # noqa: BLE001 - one bad event must not end the call
                log.exception("error handling %s", event.get("type"))

    async def handle_event(self, event: dict) -> None:
        """React to one server event.

        Split out from run() so the interesting behaviour can be tested by
        feeding it dictionaries, with no socket involved.
        """
        kind = event.get("type")

        if kind == protocol.OUTPUT_AUDIO_DELTA:
            delta = event.get("delta")
            if delta:
                await self.on_audio(base64.b64decode(delta))

        elif kind == protocol.SPEECH_STARTED:
            # Barge-in. The user talking over the model means they already
            # have what they needed, so stop generating and drop whatever
            # is queued -- otherwise the reply keeps playing over them.
            await self._cancel_response()

        elif kind == protocol.RESPONSE_DONE:
            await self._handle_response_done(event)

        elif kind == protocol.OUTPUT_TRANSCRIPT_DELTA:
            self._transcript.append(event.get("delta", ""))

        elif kind == protocol.SESSION_CREATED:
            log.info("session created by the API")

        elif kind == protocol.SESSION_UPDATED:
            # The API accepted our configuration -- model, voice, tools,
            # audio format. Worth saying out loud: connecting proves the
            # key works, but this is what proves the config is right.
            self.configured.set()
            log.info("session configuration accepted")

        elif kind == protocol.ERROR:
            log.error("realtime API error: %s", json.dumps(event.get("error", {}))[:400])

    async def _cancel_response(self) -> None:
        try:
            await self._send({"type": protocol.RESPONSE_CANCEL})
        except Exception:  # noqa: BLE001 - cancelling when idle is not an error
            log.debug("nothing to cancel")
        if self.on_interrupt is not None:
            await self.on_interrupt()

    async def _handle_response_done(self, event: dict) -> None:
        """Run any tools the model asked for, then let it speak again."""
        outputs = (event.get("response") or {}).get("output") or []
        calls = [item for item in outputs if item.get("type") == "function_call"]
        if not calls or self._dispatcher is None:
            return

        # Sequential rather than gathered: these mutate shared state (a
        # grant, a job) and a predictable order is worth more here than the
        # few milliseconds concurrency would save.
        for call in calls:
            name = call.get("name", "")
            call_id = call.get("call_id") or call.get("id") or ""
            log.info("tool call: %s", name)
            result = await self._dispatcher.call(name, call.get("arguments") or "{}")
            await self._send(protocol.function_output(call_id, result))

        # The model is waiting on these results to say anything at all.
        await self._send({"type": protocol.RESPONSE_CREATE})

    @property
    def transcript(self) -> str:
        return "".join(self._transcript)
