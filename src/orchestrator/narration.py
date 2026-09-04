"""The narration log: one short line per milestone.

This is the only channel by which an agent talks to the voice front-end.
Raw logs are never returned by default, so if the agent does not narrate,
``check()`` has nothing to say beyond a state.

Line format, tab-separated so it stays greppable and trivially parsable by
the shell helper baked into the image:

    2026-09-04T11:22:33+00:00 <TAB> - <TAB> ran the test suite, 42 passed
    2026-09-04T11:24:01+00:00 <TAB> awaiting_input <TAB> which staging DB?

The state column is ``-`` for a plain milestone. When it names a state, the
runner picks it up and moves the job there.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .state import JobState, utcnow

__all__ = ["NarrationLine", "append", "read_tail", "last_state", "MAX_TEXT"]

# Milestones are read aloud. Anything longer than this is a log line
# masquerading as narration, so it is truncated at the source.
MAX_TEXT = 200

# States an agent is allowed to declare. It may not mark itself done or
# failed -- that is the runner's call, from the process exit code.
AGENT_SETTABLE = (JobState.RUNNING, JobState.BLOCKED, JobState.AWAITING_INPUT)

_SEP = "\t"


@dataclass(frozen=True)
class NarrationLine:
    timestamp: str
    state: JobState | None
    text: str

    def format(self) -> str:
        return _SEP.join([self.timestamp, str(self.state) if self.state else "-", self.text])


def _sanitise(text: str) -> str:
    """Collapse to a single line and cap the length.

    Tabs and newlines would break the record format, and an agent that pipes
    a stack trace into narrate must not be able to corrupt the log.
    """
    flat = " ".join(text.replace(_SEP, " ").split())
    return flat[:MAX_TEXT]


def append(path: Path, text: str, state: JobState | None = None) -> NarrationLine:
    """Append one milestone.

    Opened ``O_APPEND`` and written in a single ``write`` call: below
    ``PIPE_BUF`` that is atomic, so an agent narrating from several
    processes cannot interleave two half-lines. ``MAX_TEXT`` keeps every
    record well under that limit.
    """
    if state is not None and state not in AGENT_SETTABLE:
        raise ValueError(f"{state} is not an agent-settable state")
    line = NarrationLine(utcnow(), state, _sanitise(text))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, (line.format() + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return line


def parse_line(raw: str) -> NarrationLine | None:
    """Parse one record, tolerating anything an agent may have mangled."""
    parts = raw.rstrip("\n").split(_SEP)
    if len(parts) < 3:
        # Not our format. Surface it as text rather than dropping it: a
        # stray line is still a signal, and silence is the worse failure.
        text = raw.strip()
        return NarrationLine("", None, text[:MAX_TEXT]) if text else None
    timestamp, state_field, text = parts[0], parts[1], _SEP.join(parts[2:])
    try:
        state = JobState(state_field) if state_field != "-" else None
    except ValueError:
        state = None
    return NarrationLine(timestamp, state, text[:MAX_TEXT])


def read_tail(path: Path, n: int) -> list[NarrationLine]:
    """The last *n* milestones, oldest first. Missing file means none yet."""
    if n <= 0:
        return []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    lines = [parse_line(ln) for ln in raw.splitlines()]
    return [ln for ln in lines if ln is not None][-n:]


def last_state(path: Path) -> JobState | None:
    """The most recent state an agent declared, if any.

    Read in full rather than tailed: a job that narrates ``awaiting_input``
    and then reports further progress without a state must not look parked.
    Narration logs are a handful of lines, so this is cheap.
    """
    for line in reversed(read_tail(path, n=10_000)):
        if line.state is not None:
            return line.state
    return None
