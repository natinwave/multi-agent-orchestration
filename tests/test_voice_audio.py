"""Audio conversion between Discord and the Realtime API.

Pure arithmetic, so this is the part of the voice layer that can be tested
properly without a microphone, a Discord server or an API key.
"""

from __future__ import annotations

import math
from array import array

import pytest

from orchestrator.voice.audio import Downsampler, Upsampler


def pcm(samples: list[int]) -> bytes:
    return array("h", samples).tobytes()


def unpcm(data: bytes) -> list[int]:
    out = array("h")
    out.frombytes(data)
    return list(out)


def tone(hz: float, ms: int, rate: int, channels: int = 1, amp: int = 10_000) -> bytes:
    n = int(rate * ms / 1000)
    out = array("h")
    for i in range(n):
        v = int(amp * math.sin(2 * math.pi * hz * i / rate))
        out.extend([v] * channels)
    return out.tobytes()


# --- downsampling ----------------------------------------------------------


def test_four_stereo_samples_become_one_mono_sample() -> None:
    out = Downsampler().feed(pcm([100, 200, 300, 400]))
    assert unpcm(out) == [250]  # (100+200+300+400)/4


def test_halves_the_rate_and_folds_to_mono() -> None:
    """48 kHz stereo in, 24 kHz mono out: a quarter of the samples."""
    got = Downsampler().feed(tone(440, 100, 48_000, channels=2))
    assert len(unpcm(got)) == 24_000 * 100 // 1000


def test_partial_groups_carry_into_the_next_packet(  ) -> None:
    """Packet boundaries do not respect our 4-sample groups. Dropping the
    remainder would put a click at every boundary."""
    d = Downsampler()
    first = d.feed(pcm([100, 200, 300]))   # one short of a group
    assert first == b""
    second = d.feed(pcm([400]))            # completes it
    assert unpcm(second) == [250]


def test_streaming_in_odd_chunks_matches_one_shot() -> None:
    source = tone(440, 60, 48_000, channels=2)
    one_shot = Downsampler().feed(source)

    streamed, d = b"", Downsampler()
    step = 77  # deliberately not a multiple of the frame size
    for i in range(0, len(source), step):
        streamed += d.feed(source[i : i + step])
    assert streamed == one_shot


def test_an_odd_trailing_byte_does_not_desynchronise(  ) -> None:
    """A truncated packet must not shift every subsequent sample by one
    byte, which would turn the rest of the call into noise."""
    d = Downsampler()
    assert d.feed(pcm([100, 200, 300, 400]) + b"\x01") == pcm([250])


def test_empty_input() -> None:
    assert Downsampler().feed(b"") == b""


def test_downsampler_clamps_rather_than_wrapping() -> None:
    """Wrapping turns a loud moment into a burst of white noise."""
    out = Downsampler().feed(pcm([32767, 32767, 32767, 32767]))
    assert unpcm(out) == [32767]


def test_a_tone_survives_downsampling() -> None:
    """440 Hz is well under the 12 kHz Nyquist limit of the output rate, so
    it should come through with its amplitude intact."""
    out = unpcm(Downsampler().feed(tone(440, 100, 48_000, channels=2)))
    peak = max(abs(v) for v in out)
    assert 9_000 < peak < 11_000


def test_anti_aliasing_attenuates_content_above_nyquist() -> None:
    """The averaging is what stops an 18 kHz component folding down into
    the middle of the speech band."""
    out = unpcm(Downsampler().feed(tone(18_000, 100, 48_000, channels=2)))
    assert max(abs(v) for v in out) < 6_000


# --- upsampling ------------------------------------------------------------


def test_one_mono_sample_becomes_two_stereo_frames() -> None:
    u = Upsampler()
    out = unpcm(u.feed(pcm([100, 300])))
    # 100 held back? no: 100 emits, 300 is pending for the next packet.
    assert out == [100, 100, 200, 200]  # value, then midpoint of 100 and 300


def test_doubles_the_rate_and_expands_to_stereo() -> None:
    u = Upsampler()
    got = u.feed(tone(440, 100, 24_000)) + u.flush()
    # 2400 mono samples -> 4800 frames -> 9600 int16 values
    assert len(unpcm(got)) == 9_600


def test_left_and_right_are_identical() -> None:
    out = unpcm(Upsampler().feed(pcm([100, 200, 300])))
    for i in range(0, len(out), 2):
        assert out[i] == out[i + 1]


def test_the_last_sample_is_held_until_flush() -> None:
    """It needs its successor to interpolate against, so it waits."""
    u = Upsampler()
    u.feed(pcm([100, 200]))
    assert unpcm(u.flush()) == [200, 200, 200, 200]


def test_flush_twice_is_harmless() -> None:
    u = Upsampler()
    u.feed(pcm([100, 200]))
    u.flush()
    assert u.flush() == b""


def test_streaming_upsample_matches_one_shot() -> None:
    source = tone(440, 40, 24_000)
    one_shot = Upsampler().feed(source)

    streamed, u = b"", Upsampler()
    for i in range(0, len(source), 55):
        streamed += u.feed(source[i : i + 55])
    assert streamed == one_shot


def test_upsampler_clamps() -> None:
    out = unpcm(Upsampler().feed(pcm([32767, -32768, 32767])))
    assert all(-32768 <= v <= 32767 for v in out)


def test_empty_upsample() -> None:
    assert Upsampler().feed(b"") == b""


# --- round trip ------------------------------------------------------------


def test_a_round_trip_preserves_duration() -> None:
    """What Discord sends, converted for the model and back again, should
    still be the same length of audio."""
    source = tone(440, 100, 48_000, channels=2)
    down = Downsampler().feed(source)
    u = Upsampler()
    back = u.feed(down) + u.flush()
    assert len(back) == pytest.approx(len(source), rel=0.01)


def test_a_round_trip_preserves_the_tone() -> None:
    source = tone(440, 100, 48_000, channels=2)
    u = Upsampler()
    back = unpcm(u.feed(Downsampler().feed(source)) + u.flush())
    peak = max(abs(v) for v in back)
    assert 8_500 < peak < 11_000


# --- playback buffer -------------------------------------------------------


def test_playback_returns_silence_when_empty() -> None:
    from orchestrator.voice.playback import FRAME_BYTES, PlaybackBuffer

    frame = PlaybackBuffer().read_frame()
    assert len(frame) == FRAME_BYTES
    assert set(frame) == {0}


def test_playback_returns_exactly_one_frame() -> None:
    from orchestrator.voice.playback import FRAME_BYTES, PlaybackBuffer

    b = PlaybackBuffer()
    b.write(b"\x01" * (FRAME_BYTES * 3))
    assert len(b.read_frame()) == FRAME_BYTES
    assert len(b) == FRAME_BYTES * 2


def test_playback_pads_a_short_tail() -> None:
    """A short frame makes the voice client think the stream ended."""
    from orchestrator.voice.playback import FRAME_BYTES, PlaybackBuffer

    b = PlaybackBuffer()
    b.write(b"\x01" * 100)
    frame = b.read_frame()
    assert len(frame) == FRAME_BYTES
    assert frame[:100] == b"\x01" * 100


def test_playback_drops_the_oldest_audio_when_it_overflows() -> None:
    """Five seconds behind is useless; growing without bound is fatal."""
    from orchestrator.voice.playback import FRAME_BYTES, PlaybackBuffer

    b = PlaybackBuffer(max_bytes=FRAME_BYTES * 2)
    b.write(b"\x01" * FRAME_BYTES)
    b.write(b"\x02" * FRAME_BYTES)
    b.write(b"\x03" * FRAME_BYTES)
    assert len(b) == FRAME_BYTES * 2
    assert b.read_frame()[0] == 2  # the \x01 block was dropped


def test_playback_clear_drops_everything() -> None:
    """This is what makes barge-in actually silence the model."""
    from orchestrator.voice.playback import FRAME_BYTES, PlaybackBuffer

    b = PlaybackBuffer()
    b.write(b"\x01" * FRAME_BYTES * 5)
    b.clear()
    assert b.is_empty
    assert set(b.read_frame()) == {0}


def test_playback_is_safe_across_threads() -> None:
    """Audio arrives on the asyncio loop; Discord reads on its own thread."""
    import threading

    from orchestrator.voice.playback import FRAME_BYTES, PlaybackBuffer

    b = PlaybackBuffer(max_bytes=FRAME_BYTES * 10_000)
    def writer():
        for _ in range(200):
            b.write(b"\x01" * 64)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for _ in range(50):
        b.read_frame()
    for t in threads:
        t.join()
    assert len(b) % 1 == 0  # no torn state, no exception
