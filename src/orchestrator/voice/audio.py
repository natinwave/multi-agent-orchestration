"""Converting between Discord's audio and the Realtime API's.

Discord decodes Opus to 48 kHz 16-bit stereo. The Realtime API speaks
24 kHz 16-bit mono. Both directions are an exact 2:1 ratio, which is the
only reason this can be a hundred lines of arithmetic instead of a
dependency.

Written against ``array`` rather than ``audioop``: audioop is deprecated in
3.11 and removed in 3.13, so building on it would be building on something
already scheduled for demolition.

Both converters are stateful on purpose. Audio arrives in packets whose
boundaries respect nothing -- not the 4-sample groups the downsampler
consumes, and not even the 2-byte samples themselves. Everything left over
carries into the next call at the byte level. Dropping a remainder instead
would put a click at every packet boundary, and dropping a half-sample
would shift each following sample by one byte and turn the rest of the
call into noise.
"""

from __future__ import annotations

from array import array

__all__ = ["Downsampler", "Upsampler", "BYTES_PER_SAMPLE"]

BYTES_PER_SAMPLE = 2  # int16
_INT16_MIN, _INT16_MAX = -32_768, 32_767


def _clamp(value: int) -> int:
    return _INT16_MAX if value > _INT16_MAX else _INT16_MIN if value < _INT16_MIN else value


class Downsampler:
    """48 kHz stereo -> 24 kHz mono, for audio on its way to the model.

    Four input samples (two stereo frames) become one output sample: the
    channels are averaged, then the two frames are averaged. That second
    average is a two-tap box filter, which is crude but is genuine
    anti-aliasing -- decimating without it would fold everything above
    12 kHz back down into the speech band as a metallic ring.
    """

    #: input samples consumed per output sample (2 channels x 2 frames)
    GROUP = 4
    #: bytes consumed per output sample
    GROUP_BYTES = GROUP * BYTES_PER_SAMPLE

    def __init__(self) -> None:
        # Buffered as bytes, not samples: a packet can end mid-sample.
        self._buf = b""

    def feed(self, pcm: bytes) -> bytes:
        """Convert a packet. Returns however much is ready, possibly b""."""
        buf = self._buf + pcm
        groups = len(buf) // self.GROUP_BYTES
        if not groups:
            self._buf = buf
            return b""

        consumed = groups * self.GROUP_BYTES
        samples = array("h")
        samples.frombytes(buf[:consumed])
        self._buf = buf[consumed:]

        out = array("h", bytes(groups * BYTES_PER_SAMPLE))
        for i in range(groups):
            base = i * self.GROUP
            left_a, right_a, left_b, right_b = samples[base : base + self.GROUP]
            out[i] = _clamp((left_a + right_a + left_b + right_b) // 4)
        return out.tobytes()

    def flush(self) -> bytes:
        """Discard anything too short to convert. Ends an utterance."""
        self._buf = b""
        return b""


class Upsampler:
    """24 kHz mono -> 48 kHz stereo, for the model's voice on its way out.

    Each input sample becomes two stereo frames. The inserted sample is the
    midpoint between this sample and the next rather than a copy of it:
    linear interpolation instead of sample-and-hold, which costs one add
    and a shift and audibly softens the aliasing that duplication creates.

    Interpolating needs the *next* sample, so the last sample of every
    packet is held back until the following packet arrives.
    """

    def __init__(self) -> None:
        self._pending: int | None = None
        self._buf = b""

    def feed(self, pcm: bytes) -> bytes:
        buf = self._buf + pcm
        whole = len(buf) - (len(buf) % BYTES_PER_SAMPLE)
        self._buf = buf[whole:]
        if not whole:
            return b""
        samples = array("h")
        samples.frombytes(buf[:whole])

        if self._pending is not None:
            samples.insert(0, self._pending)
        # Hold the final sample back: its midpoint needs the next packet.
        self._pending = samples[-1]
        source = samples[:-1]
        if not source:
            return b""

        out = array("h", bytes(len(source) * 4 * BYTES_PER_SAMPLE))
        for i, value in enumerate(source):
            midpoint = _clamp((value + samples[i + 1]) // 2)
            base = i * 4
            out[base] = out[base + 1] = value       # frame 1, L and R
            out[base + 2] = out[base + 3] = midpoint  # frame 2, L and R
        return out.tobytes()

    def flush(self) -> bytes:
        """Emit the held-back sample. Call at the end of an utterance.

        Without this the last 20-odd microseconds of every reply are lost,
        which is inaudible on its own but accumulates into a clipped final
        consonant across a long conversation.
        """
        self._buf = b""
        if self._pending is None:
            return b""
        value, self._pending = self._pending, None
        out = array("h", [value, value, value, value])
        return out.tobytes()
