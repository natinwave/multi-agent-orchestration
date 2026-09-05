"""Realtime API constants, in one place.

Every string the wire protocol depends on lives here rather than scattered
through the session code, because these are the things that change under
us and the things I cannot verify without a live key. When something stops
working after an API update, this is the file to read first.

Checked against the OpenAI realtime guides in September 2026:
  - the GA session object nests audio under session.audio.{input,output}
  - the OpenAI-Beta: realtime=v1 header is for the beta and is dropped at GA
  - audio deltas are response.output_audio.delta (not response.audio.delta)
"""

from __future__ import annotations

WS_URL = "wss://api.openai.com/v1/realtime"

# gpt-realtime-2 carries reasoning, and the guide suggests starting low --
# this is a voice loop, and a thinking pause reads as a dropped call.
DEFAULT_MODEL = "gpt-realtime-2.1"
DEFAULT_VOICE = "marin"
DEFAULT_REASONING_EFFORT = "low"

# The Realtime API speaks 24 kHz mono PCM16 in both directions. 24000 is
# the only rate accepted for PCM, and both the input and output format
# objects must carry it.
SAMPLE_RATE = 24_000
AUDIO_FORMAT = "audio/pcm"

# Discord decodes Opus to 48 kHz 16-bit stereo, so every frame crosses a
# 2:1 resample and a stereo/mono fold. See audio.py.
DISCORD_SAMPLE_RATE = 48_000
DISCORD_CHANNELS = 2

# -- client events ----------------------------------------------------------
SESSION_UPDATE = "session.update"
INPUT_AUDIO_APPEND = "input_audio_buffer.append"
CONVERSATION_ITEM_CREATE = "conversation.item.create"
RESPONSE_CREATE = "response.create"
RESPONSE_CANCEL = "response.cancel"

# -- server events ----------------------------------------------------------
SESSION_CREATED = "session.created"
SESSION_UPDATED = "session.updated"
OUTPUT_AUDIO_DELTA = "response.output_audio.delta"
OUTPUT_AUDIO_DONE = "response.output_audio.done"
OUTPUT_TRANSCRIPT_DELTA = "response.output_audio_transcript.delta"
SPEECH_STARTED = "input_audio_buffer.speech_started"
SPEECH_STOPPED = "input_audio_buffer.speech_stopped"
RESPONSE_DONE = "response.done"
ERROR = "error"


def session_config(
    instructions: str,
    tools: list[dict],
    voice: str = DEFAULT_VOICE,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> dict:
    """The session.update payload sent immediately after connecting."""
    return {
        "type": SESSION_UPDATE,
        "session": {
            "type": "realtime",
            "model": model,
            "instructions": instructions,
            "tools": tools,
            "tool_choice": "auto",
            "audio": {
                "input": {
                    "format": {"type": AUDIO_FORMAT, "rate": SAMPLE_RATE},
                    # Semantic VAD decides you have finished a thought rather
                    # than merely stopped making noise, which suits someone
                    # thinking out loud about what an agent should do.
                    "turn_detection": {"type": "semantic_vad"},
                },
                "output": {
                    # `rate` is required here, despite the API reference
                    # listing it as optional and the guide's own example
                    # omitting it. The live API rejects the session with
                    # "Missing required parameter:
                    # session.audio.output.format.rate". Where the docs and
                    # the server disagree, the server wins.
                    "format": {"type": AUDIO_FORMAT, "rate": SAMPLE_RATE},
                    "voice": voice,
                },
            },
            "reasoning": {"effort": reasoning_effort},
        },
    }


def function_output(call_id: str, payload: str) -> dict:
    """Hand a tool result back to the model."""
    return {
        "type": CONVERSATION_ITEM_CREATE,
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": payload,
        },
    }
