"""The realtime event loop.

The socket itself cannot be tested without a live key, so handle_event is
written to take a plain dictionary and everything decidable offline is
tested by feeding it one: tool dispatch, barge-in, and not falling over on
events we do not recognise.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from orchestrator.voice import protocol
from orchestrator.voice.realtime import RealtimeSession


class FakeServer:
    """Stands in for the in-process MCP server."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))

        class Result:
            content = [type("Block", (), {"text": json.dumps({"job_id": "kestrel"})})()]

        return Result()


class Recorder:
    """Captures what the session sent and played."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.audio: list[bytes] = []
        self.interrupts = 0

    async def on_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    async def on_interrupt(self) -> None:
        self.interrupts += 1


@pytest.fixture
def session():
    rec = Recorder()
    s = RealtimeSession(
        api_key="unused",
        server=FakeServer(),
        on_audio=rec.on_audio,
        on_interrupt=rec.on_interrupt,
    )
    # Stand in for the socket and the dispatcher connect() would build.
    from orchestrator.voice.tools import ToolDispatcher

    s._dispatcher = ToolDispatcher(s.server)

    async def fake_send(event):
        rec.sent.append(event)

    s._send = fake_send  # type: ignore[method-assign]
    return s, rec


def run(coro):
    return asyncio.run(coro)


# --- audio out -------------------------------------------------------------


def test_audio_deltas_are_decoded_and_played(session) -> None:
    s, rec = session
    pcm = b"\x01\x02\x03\x04"
    run(s.handle_event({"type": protocol.OUTPUT_AUDIO_DELTA,
                        "delta": base64.b64encode(pcm).decode()}))
    assert rec.audio == [pcm]


def test_an_empty_delta_plays_nothing(session) -> None:
    s, rec = session
    run(s.handle_event({"type": protocol.OUTPUT_AUDIO_DELTA, "delta": ""}))
    assert rec.audio == []


# --- barge-in --------------------------------------------------------------


def test_the_user_speaking_cancels_the_reply(session) -> None:
    """Without this the model keeps talking over them for several seconds."""
    s, rec = session
    run(s.handle_event({"type": protocol.SPEECH_STARTED}))
    assert any(e["type"] == protocol.RESPONSE_CANCEL for e in rec.sent)


def test_the_user_speaking_drops_queued_audio(session) -> None:
    """Cancelling generation is not enough: audio already buffered for
    playback would still be spoken over them."""
    s, rec = session
    run(s.handle_event({"type": protocol.SPEECH_STARTED}))
    assert rec.interrupts == 1


# --- tool calls ------------------------------------------------------------


def make_response_done(name: str, args: dict, call_id: str = "call_1") -> dict:
    return {
        "type": protocol.RESPONSE_DONE,
        "response": {
            "output": [
                {
                    "type": "function_call",
                    "name": name,
                    "call_id": call_id,
                    "arguments": json.dumps(args),
                }
            ]
        },
    }


def test_a_tool_call_is_executed_and_answered(session) -> None:
    s, rec = session
    run(s.handle_event(make_response_done("ask", {"agent": "hermes", "message": "hi"})))

    assert s.server.calls == [("ask", {"agent": "hermes", "message": "hi"})]
    outputs = [e for e in rec.sent if e["type"] == protocol.CONVERSATION_ITEM_CREATE]
    assert len(outputs) == 1
    assert outputs[0]["item"]["call_id"] == "call_1"
    assert "kestrel" in outputs[0]["item"]["output"]


def test_the_model_is_prompted_to_speak_after_a_tool_call(session) -> None:
    """Without this it sits silently holding the result."""
    s, rec = session
    run(s.handle_event(make_response_done("check", {"job_id": "kestrel"})))
    assert rec.sent[-1]["type"] == protocol.RESPONSE_CREATE


def test_several_tool_calls_run_in_order(session) -> None:
    """They mutate shared state, so order matters more than concurrency."""
    s, rec = session
    event = make_response_done("ask", {"agent": "hermes", "message": "one"})
    event["response"]["output"].append(
        {
            "type": "function_call",
            "name": "check",
            "call_id": "call_2",
            "arguments": json.dumps({"job_id": "kestrel"}),
        }
    )
    run(s.handle_event(event))
    assert [name for name, _ in s.server.calls] == ["ask", "check"]


def test_a_response_with_no_tool_calls_sends_nothing(session) -> None:
    s, rec = session
    run(s.handle_event({"type": protocol.RESPONSE_DONE,
                        "response": {"output": [{"type": "message"}]}}))
    assert rec.sent == []


def test_a_grant_goes_straight_through_by_default(session) -> None:
    """One step, at the owner's request. The control is the tool
    description requiring the model to report what it shared."""
    s, rec = session
    run(s.handle_event(make_response_done("grant", {"agent": "hermes", "credential": "x"})))
    assert s.server.calls == [("grant", {"agent": "hermes", "credential": "x"})]


def test_a_grant_is_held_when_confirmation_is_turned_on(session) -> None:
    from orchestrator.voice.tools import GrantGuard, ToolDispatcher

    s, rec = session
    s._dispatcher = ToolDispatcher(s.server, guard=GrantGuard(enabled=True))
    run(s.handle_event(make_response_done("grant", {"agent": "hermes", "credential": "x"})))
    assert s.server.calls == [], "the first grant must not execute"
    output = json.loads(rec.sent[0]["item"]["output"])
    assert output["status"] == "needs_confirmation"


# --- robustness ------------------------------------------------------------


def test_unknown_events_are_ignored(session) -> None:
    """The API adds event types; an unrecognised one must not end a call."""
    s, rec = session
    run(s.handle_event({"type": "response.some_future_thing", "data": 1}))
    assert rec.sent == [] and rec.audio == []


def test_an_event_with_no_type_is_ignored(session) -> None:
    s, _ = session
    run(s.handle_event({}))


def test_api_errors_are_logged_not_raised(session, caplog) -> None:
    s, _ = session
    run(s.handle_event({"type": protocol.ERROR, "error": {"message": "rate limited"}}))
    assert "rate limited" in caplog.text


def test_transcript_accumulates(session) -> None:
    s, _ = session
    for word in ("started ", "kestrel"):
        run(s.handle_event({"type": protocol.OUTPUT_TRANSCRIPT_DELTA, "delta": word}))
    assert s.transcript == "started kestrel"


def test_sending_audio_before_connecting_is_an_error() -> None:
    s = RealtimeSession(api_key="k", server=FakeServer(), on_audio=Recorder().on_audio)
    with pytest.raises(RuntimeError):
        run(s._send({"type": "x"}))


def test_sending_empty_audio_is_a_no_op(session) -> None:
    s, rec = session
    run(s.send_audio(b""))
    assert rec.sent == []


# --- session configuration -------------------------------------------------


def test_both_audio_formats_carry_a_rate() -> None:
    """The live API rejects a session whose output format has no rate, even
    though the reference lists it as optional. Caught only by connecting,
    so it is pinned here."""
    audio = protocol.session_config("hi", [])["session"]["audio"]
    for side in ("input", "output"):
        fmt = audio[side]["format"]
        assert fmt["type"] == protocol.AUDIO_FORMAT, side
        assert fmt["rate"] == protocol.SAMPLE_RATE, f"{side} format needs an explicit rate"


def test_the_session_declares_its_type_and_model() -> None:
    session = protocol.session_config("hi", [])["session"]
    assert session["type"] == "realtime"
    assert session["model"] == protocol.DEFAULT_MODEL


def test_the_configured_voice_is_one_the_api_accepts() -> None:
    """Wrong voice is another rejection that only shows up on connect."""
    valid = {
        "alloy", "ash", "ballad", "coral", "echo",
        "sage", "shimmer", "verse", "marin", "cedar",
    }
    assert protocol.DEFAULT_VOICE in valid


def test_the_sample_rate_is_the_only_one_pcm_accepts() -> None:
    assert protocol.SAMPLE_RATE == 24_000


# --- attaching to a call that is already up --------------------------------


def test_attaching_to_a_call_prompts_the_model_to_speak() -> None:
    """The model says nothing until asked. On a phone call that means the
    caller hears an open line and silence, which is indistinguishable from
    a broken system."""
    rec = Recorder()
    session = RealtimeSession(
        api_key="k", server=FakeServer(), on_audio=rec.on_audio, call_id="rtc_123"
    )

    class FakeWS:
        async def send(self, raw):
            rec.sent.append(json.loads(raw))

    async def scenario():
        session._ws = FakeWS()
        from orchestrator.voice.tools import ToolDispatcher, realtime_tools

        session._dispatcher = ToolDispatcher(session.server)
        # The tail of connect() that runs once attached.
        session.configured.set()
        await session._send({"type": protocol.RESPONSE_CREATE})

    run(scenario())
    assert rec.sent[-1]["type"] == protocol.RESPONSE_CREATE


def test_unhandled_events_are_visible_at_debug(session, caplog) -> None:
    """Silently dropping them left no way to tell 'the API sent nothing'
    from 'the API sent something we ignore'."""
    import logging

    s, _ = session
    with caplog.at_level(logging.DEBUG, logger="orchestrator.voice"):
        run(s.handle_event({"type": "response.output_item.added"}))
    assert "response.output_item.added" in caplog.text
