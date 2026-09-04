"""What a backend has to provide.

A backend turns a job into a running process and reports how it ended. It
does not touch status.json -- the runner owns that -- and it does not
redact, because everything it writes lands in raw.log, which is only ever
returned through the supervisor's scrubber.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..registry import Agent, Config
from ..state import JobPaths, Meta

__all__ = ["Backend", "Outcome", "BackendError"]


class BackendError(RuntimeError):
    """The backend could not start, or died in a way worth narrating."""


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    # A short operator-facing note. Ends up in status.detail, scrubbed.
    detail: str | None = None


class Backend(Protocol):
    def run(
        self,
        *,
        agent: Agent,
        meta: Meta,
        paths: JobPaths,
        workdir: Path,
        prompt: str,
        config: Config,
        resume: bool = False,
    ) -> Outcome:
        """Run the job to completion, appending output to ``paths.raw``.

        Blocks. The runner process that calls this is already detached, so
        blocking here is what keeps the supervisor stateless.
        """
        ...
