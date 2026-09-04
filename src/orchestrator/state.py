"""Job state on disk.

The supervisor keeps nothing in memory: ``ask()`` creates a job directory and
walks away, and every reader -- the MCP server, the CLI, a second client --
reconstructs state by reading files. That is what lets a job outlive the
stdio MCP client that started it.

Two files per job. ``meta.json`` is written once at creation. ``status.json``
is rewritten as the job progresses and has exactly one writer, the runner,
so no locking is needed; readers get a consistent view because every write
goes through :func:`write_json_atomic`.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

__all__ = [
    "JobState",
    "JobPaths",
    "Meta",
    "Status",
    "write_json_atomic",
    "read_json",
    "final_state",
    "pid_alive",
    "utcnow",
]


class JobState(StrEnum):
    """The six states ``check()`` may report. Nothing else is ever returned."""

    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    AWAITING_INPUT = "awaiting_input"
    DONE = "done"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (JobState.DONE, JobState.FAILED)

    @property
    def is_parked(self) -> bool:
        """Stopped, but resumable: the agent is waiting on something."""
        return self in (JobState.BLOCKED, JobState.AWAITING_INPUT)

    @property
    def is_active(self) -> bool:
        return self in (JobState.QUEUED, JobState.RUNNING)


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class JobPaths:
    """Everything a job owns, derived from its directory."""

    root: Path

    @property
    def meta(self) -> Path:
        return self.root / "meta.json"

    @property
    def status(self) -> Path:
        return self.root / "status.json"

    @property
    def narration(self) -> Path:
        return self.root / "narration.log"

    @property
    def raw(self) -> Path:
        return self.root / "raw.log"

    @property
    def prompt(self) -> Path:
        """The message, written to a file so it never rides on a command
        line where it would show up in ``ps`` and in shell history."""
        return self.root / "prompt.txt"

    @property
    def reply(self) -> Path:
        return self.root / "reply.txt"


@dataclass
class Meta:
    """Immutable facts about a job, written once at creation."""

    job_id: str
    agent: str
    created_at: str
    workdir: str
    # A UUID assigned up front and passed as --session-id, so --resume is
    # deterministic later without having to scrape it out of the log.
    session_id: str
    repo: str | None = None
    base_ref: str | None = None
    branch: str | None = None
    worktree: bool = False

    def write(self, paths: JobPaths) -> None:
        write_json_atomic(paths.meta, asdict(self))

    @classmethod
    def read(cls, paths: JobPaths) -> "Meta":
        return cls(**read_json(paths.meta))


@dataclass
class Status:
    """Mutable job progress. Sole writer: the runner process."""

    state: JobState = JobState.QUEUED
    updated_at: str = field(default_factory=utcnow)
    runner_pid: int | None = None
    exit_code: int | None = None
    # A short operator-facing note, e.g. why a job failed to start. Scrubbed
    # like everything else before it leaves the supervisor.
    detail: str | None = None

    def write(self, paths: JobPaths) -> None:
        self.updated_at = utcnow()
        write_json_atomic(paths.status, {**asdict(self), "state": str(self.state)})

    @classmethod
    def read(cls, paths: JobPaths) -> "Status":
        try:
            raw = read_json(paths.status)
        except FileNotFoundError:
            # The job directory exists but the runner has not written yet.
            return cls(state=JobState.QUEUED)
        raw["state"] = JobState(raw.get("state", "queued"))
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


def write_json_atomic(path: Path, obj: object) -> None:
    """Write JSON so a concurrent reader never sees a half-written file.

    Same-directory temp file plus ``os.replace``, which is atomic within a
    filesystem. Readers therefore see either the old file or the new one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def pid_alive(pid: int | None) -> bool:
    """Whether *pid* still exists.

    Signal 0 checks for existence without delivering anything. This can be
    fooled by PID reuse, which would make a dead job look alive until the
    next check; the window is small and the failure mode is a stale
    "running", never a lost job.
    """
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, owned by someone else.
        return True
    return True


def final_state(exit_code: int | None, last_narrated: JobState | None) -> JobState:
    """Resolve the state of a job whose child process has been waited on.

    The precedence rule that matters: a job that narrated ``awaiting_input``
    and then exited 0 is *parked*, not done. ``claude -p`` exits cleanly when
    it stops to ask a question, so trusting the exit code alone would report
    "done" for a job that has not started the work. A non-zero exit always
    wins -- a crash is a crash, whatever the agent last claimed.
    """
    if exit_code is None:
        return JobState.RUNNING
    if exit_code != 0:
        return JobState.FAILED
    if last_narrated is not None and last_narrated.is_parked:
        return last_narrated
    return JobState.DONE
