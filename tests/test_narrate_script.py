"""The narrate helper is shell (it runs in the container) and the reader is
Python (it runs on the host). They are the two halves of one wire format, so
they are tested against each other rather than separately."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from orchestrator.narration import MAX_TEXT, last_state, read_tail
from orchestrator.state import JobState

NARRATE = Path(__file__).resolve().parents[1] / "docker" / "bin" / "narrate"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def run(job_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(NARRATE), *args],
        env={"ORCH_JOB_DIR": str(job_dir), "PATH": "/usr/bin:/bin:/usr/local/bin"},
        capture_output=True,
        text=True,
    )


def test_script_is_syntactically_valid() -> None:
    assert subprocess.run(["bash", "-n", str(NARRATE)]).returncode == 0


def test_a_milestone_round_trips_to_the_python_reader(tmp_path: Path) -> None:
    assert run(tmp_path, "cloned the repo").returncode == 0
    (line,) = read_tail(tmp_path / "narration.log", 5)
    assert line.text == "cloned the repo"
    assert line.state is None


def test_a_state_round_trips(tmp_path: Path) -> None:
    assert run(tmp_path, "--state", "awaiting_input", "which staging DB?").returncode == 0
    assert last_state(tmp_path / "narration.log") is JobState.AWAITING_INPUT


@pytest.mark.parametrize("state", ["running", "blocked", "awaiting_input"])
def test_agent_settable_states_are_accepted(tmp_path: Path, state: str) -> None:
    assert run(tmp_path, "--state", state, "x").returncode == 0
    assert last_state(tmp_path / "narration.log") is JobState(state)


@pytest.mark.parametrize("state", ["done", "failed", "queued", "nonsense"])
def test_the_script_refuses_states_an_agent_may_not_set(tmp_path: Path, state: str) -> None:
    """Same rule the Python side enforces: done and failed come from the
    exit code, not from the agent's own account of itself."""
    result = run(tmp_path, "--state", state, "all finished")
    assert result.returncode == 2
    assert not (tmp_path / "narration.log").exists()


def test_ragged_input_cannot_corrupt_the_record(tmp_path: Path) -> None:
    run(tmp_path, "line\twith\ttabs and\nnewlines   and    spaces")
    lines = read_tail(tmp_path / "narration.log", 10)
    assert len(lines) == 1
    assert "\t" not in lines[0].text
    assert lines[0].text == "line with tabs and newlines and spaces"


def test_long_text_is_capped_so_appends_stay_atomic(tmp_path: Path) -> None:
    run(tmp_path, "x" * 5000)
    assert len(read_tail(tmp_path / "narration.log", 1)[0].text) == MAX_TEXT
    assert len((tmp_path / "narration.log").read_bytes()) < 512


def test_appends_accumulate(tmp_path: Path) -> None:
    for i in range(4):
        run(tmp_path, f"step {i}")
    assert [ln.text for ln in read_tail(tmp_path / "narration.log", 10)] == [
        f"step {i}" for i in range(4)
    ]


def test_saying_nothing_is_an_error(tmp_path: Path) -> None:
    assert run(tmp_path).returncode == 2


def test_an_unsupervised_invocation_fails_loudly(tmp_path: Path) -> None:
    """Outside a job there is no log to append to, and silently succeeding
    would make an agent think it had reported progress."""
    result = subprocess.run(
        ["bash", str(NARRATE), "hello"],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "ORCH_JOB_DIR" in result.stderr
