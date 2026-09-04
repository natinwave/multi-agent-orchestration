"""A jitter buffer between the model's voice and Discord's clock.

Discord wants exactly 20 ms of 48 kHz stereo audio every 20 ms, forever.
The model sends audio in bursts of whatever size it likes, whenever it
likes. This sits between them: bursts go in, fixed frames come out, and
silence is returned when there is nothing to say rather than stalling the
voice client.

Kept apart from the Discord code because it is pure buffer arithmetic and
is worth testing properly, whereas the Discord glue is not testable here
at all.
"""

from __future__ import annotations

import threading

__all__ = ["PlaybackBuffer", "FRAME_BYTES", "FRAME_MS"]

FRAME_MS = 20
#: 20 ms of 48 kHz, 16-bit, stereo
FRAME_BYTES = 48_000 * 2 * 2 * FRAME_MS // 1000  # 3840

SILENCE = b"\x00" * FRAME_BYTES


class PlaybackBuffer:
    """Bytes in from any thread, fixed frames out on Discord's clock."""

    def __init__(self, max_bytes: int = FRAME_BYTES * 250) -> None:  # ~5 seconds
        # A cap matters: if Discord stops draining, an unbounded buffer
        # grows until the process dies, and a five-second-stale reply is
        # useless anyway.
        self._buf = bytearray()
        self._max = max_bytes
        self._lock = threading.Lock()

    def write(self, pcm: bytes) -> None:
        if not pcm:
            return
        with self._lock:
            self._buf.extend(pcm)
            if len(self._buf) > self._max:
                # Drop the oldest: it is already late.
                del self._buf[: len(self._buf) - self._max]

    def read_frame(self) -> bytes:
        """Exactly one frame. Silence when empty, padded when short.

        Returning a short frame would make the voice client treat the
        stream as finished and stop playing.
        """
        with self._lock:
            if not self._buf:
                return SILENCE
            frame = bytes(self._buf[:FRAME_BYTES])
            del self._buf[:FRAME_BYTES]
        if len(frame) < FRAME_BYTES:
            frame += b"\x00" * (FRAME_BYTES - len(frame))
        return frame

    def clear(self) -> None:
        """Drop everything queued. Used when the user interrupts."""
        with self._lock:
            self._buf.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)

    @property
    def is_empty(self) -> bool:
        return len(self) == 0
