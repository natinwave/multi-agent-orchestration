"""Narration is the only thing check() returns by default, so parsing has to
survive whatever an agent writes into the file."""

from pathlib import Path

import pytest

from orchestrator.narration import (
    MAX_TEXT,
    append,
    last_state,
    parse_line,
    read_tail,
)
from orchestrator.state import JobState


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / "kestrel" / "narration.log"


def test_append_creates_the_file_and_round_trips(log: Path) -> None:
    append(log, "cloned the repo")
    (line,) = read_tail(log, 5)
    assert line.text == "cloned the repo"
    assert line.state is None
    assert line.timestamp.startswith("20")


def test_append_records_a_state(log: Path) -> None:
    append(log, "which staging DB?", JobState.AWAITING_INPUT)
    assert read_tail(log, 1)[0].state is JobState.AWAITING_INPUT


def test_agents_may_not_declare_themselves_done(log: Path) -> None:
    """done/failed come from the process exit code, not from the agent's own
    account of itself."""
    for forbidden in (JobState.DONE, JobState.FAILED, JobState.QUEUED):
        with pytest.raises(ValueError):
            append(log, "all finished", forbidden)


def test_tabs_and_newlines_cannot_corrupt_the_record(log: Path) -> None:
    append(log, "line one\nline\ttwo\n\nline three")
    lines = read_tail(log, 10)
    assert len(lines) == 1
    assert lines[0].text == "line one line two line three"


def test_long_text_is_truncated_at_the_source(log: Path) -> None:
    append(log, "x" * 5000)
    assert len(read_tail(log, 1)[0].text) == MAX_TEXT
    # Every record must stay well under PIPE_BUF for the append to be atomic.
    assert len(log.read_bytes()) < 512


def test_tail_returns_oldest_first_and_only_n(log: Path) -> None:
    for i in range(6):
        append(log, f"step {i}")
    tail = read_tail(log, 3)
    assert [ln.text for ln in tail] == ["step 3", "step 4", "step 5"]


def test_tail_of_a_missing_log_is_empty(tmp_path: Path) -> None:
    assert read_tail(tmp_path / "nope.log", 3) == []


def test_tail_of_zero_is_empty(log: Path) -> None:
    append(log, "something")
    assert read_tail(log, 0) == []


def test_malformed_lines_are_surfaced_not_dropped(log: Path) -> None:
    """An agent that writes to the log with plain echo still gets heard.
    Silence is a worse failure than a slightly ragged line."""
    log.parent.mkdir(parents=True)
    log.write_text("just some text\n")
    (line,) = read_tail(log, 5)
    assert line.text == "just some text"
    assert line.state is None


def test_blank_lines_are_dropped(log: Path) -> None:
    log.parent.mkdir(parents=True)
    log.write_text("\n\n   \n")
    assert read_tail(log, 5) == []


def test_unknown_state_column_degrades_to_no_state(log: Path) -> None:
    assert parse_line("2026-01-01T00:00:00+00:00\tbanana\thello").state is None


def test_last_state_is_the_most_recent_declared_one(log: Path) -> None:
    append(log, "starting", JobState.RUNNING)
    append(log, "stuck", JobState.BLOCKED)
    append(log, "unstuck", JobState.RUNNING)
    assert last_state(log) is JobState.RUNNING


def test_last_state_ignores_plain_milestones(log: Path) -> None:
    """A parked job that keeps narrating progress without a state stays
    parked -- otherwise a chatty agent would silently un-park itself."""
    append(log, "which staging DB?", JobState.AWAITING_INPUT)
    append(log, "still waiting")
    assert last_state(log) is JobState.AWAITING_INPUT


def test_last_state_of_a_stateless_log_is_none(log: Path) -> None:
    append(log, "just working")
    assert last_state(log) is None


def test_last_state_of_a_missing_log_is_none(tmp_path: Path) -> None:
    assert last_state(tmp_path / "nope.log") is None


def test_appends_from_several_writers_do_not_interleave(log: Path) -> None:
    """Two processes in one container may narrate at once; O_APPEND under
    PIPE_BUF keeps each record whole."""
    import concurrent.futures as cf

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: append(log, f"milestone {i}"), range(64)))
    lines = read_tail(log, 200)
    assert len(lines) == 64
    assert {ln.text for ln in lines} == {f"milestone {i}" for i in range(64)}
