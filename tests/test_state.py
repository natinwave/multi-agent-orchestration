"""State lives entirely on disk, so these tests cover the two things that
could lose a job: a torn status file, and the exit-code precedence rule."""

import json
import os
from pathlib import Path

import pytest

from orchestrator.state import (
    JobPaths,
    JobState,
    Meta,
    Status,
    final_state,
    pid_alive,
    read_json,
    write_json_atomic,
)


@pytest.fixture
def paths(tmp_path: Path) -> JobPaths:
    return JobPaths(tmp_path / "kestrel")


# --- the enum --------------------------------------------------------------


def test_states_serialise_as_the_documented_strings() -> None:
    """The brief named six. `stopped` is a deliberate seventh: telling
    someone their job "failed" when they asked to stop it is both wrong and
    alarming, and would teach them to discount the word failed."""
    assert [str(s) for s in JobState] == [
        "queued",
        "running",
        "blocked",
        "awaiting_input",
        "done",
        "failed",
        "stopped",
    ]


def test_a_stopped_job_is_finished_but_not_failed() -> None:
    assert JobState.STOPPED.is_terminal
    assert JobState.STOPPED is not JobState.FAILED
    assert not JobState.STOPPED.is_active


def test_state_categories() -> None:
    assert JobState.DONE.is_terminal and JobState.FAILED.is_terminal
    assert JobState.AWAITING_INPUT.is_parked and JobState.BLOCKED.is_parked
    assert JobState.RUNNING.is_active and JobState.QUEUED.is_active
    assert not JobState.RUNNING.is_terminal


# --- the precedence rule ---------------------------------------------------


def test_running_while_the_child_lives() -> None:
    assert final_state(None, None) is JobState.RUNNING


def test_clean_exit_is_done() -> None:
    assert final_state(0, None) is JobState.DONE
    assert final_state(0, JobState.RUNNING) is JobState.DONE


def test_parked_narration_survives_a_clean_exit() -> None:
    """claude -p exits 0 when it stops to ask a question. Reporting 'done'
    there would tell the voice model the work finished when it never began."""
    assert final_state(0, JobState.AWAITING_INPUT) is JobState.AWAITING_INPUT
    assert final_state(0, JobState.BLOCKED) is JobState.BLOCKED


def test_nonzero_exit_always_wins() -> None:
    assert final_state(1, JobState.AWAITING_INPUT) is JobState.FAILED
    assert final_state(137, JobState.BLOCKED) is JobState.FAILED


# --- atomic writes ---------------------------------------------------------


def test_atomic_write_round_trips(tmp_path: Path) -> None:
    p = tmp_path / "a" / "b" / "status.json"
    write_json_atomic(p, {"state": "running"})
    assert read_json(p) == {"state": "running"}


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    p = tmp_path / "status.json"
    write_json_atomic(p, {"x": 1})
    assert [q.name for q in tmp_path.iterdir()] == ["status.json"]


def test_atomic_write_replaces_rather_than_truncates(tmp_path: Path) -> None:
    """A reader holding the old inode must never see a zero-length file."""
    p = tmp_path / "status.json"
    write_json_atomic(p, {"n": 1})
    first = p.stat().st_ino
    write_json_atomic(p, {"n": 2})
    assert p.stat().st_ino != first
    assert read_json(p) == {"n": 2}


def test_failed_write_does_not_clobber_the_previous_file(tmp_path: Path) -> None:
    p = tmp_path / "status.json"
    write_json_atomic(p, {"good": True})
    with pytest.raises(TypeError):
        write_json_atomic(p, {"bad": object()})
    assert read_json(p) == {"good": True}
    assert [q.name for q in tmp_path.iterdir()] == ["status.json"]


# --- meta and status -------------------------------------------------------


def test_meta_round_trips(paths: JobPaths) -> None:
    meta = Meta(
        job_id="kestrel",
        agent="claude-code",
        created_at="2026-09-04T10:00:00+00:00",
        workdir="/srv/orchestration/worktrees/kestrel",
        session_id="3f7c1e2a-9b4d-4c8e-a1f6-2d5b8c0e4a91",
        repo="main",
        base_ref="main",
        branch="job/kestrel",
        worktree=True,
    )
    meta.write(paths)
    assert Meta.read(paths) == meta


def test_status_round_trips(paths: JobPaths) -> None:
    status = Status(state=JobState.RUNNING, runner_pid=4242)
    status.write(paths)
    back = Status.read(paths)
    assert back.state is JobState.RUNNING
    assert back.runner_pid == 4242
    assert back.updated_at


def test_status_of_a_job_with_no_file_yet_is_queued(paths: JobPaths) -> None:
    """ask() creates the directory before the runner writes anything."""
    paths.root.mkdir(parents=True)
    assert Status.read(paths).state is JobState.QUEUED


def test_status_read_ignores_unknown_fields(paths: JobPaths) -> None:
    """A status file written by a newer version must not crash an older
    reader mid-job."""
    paths.root.mkdir(parents=True)
    paths.status.write_text(json.dumps({"state": "running", "future_field": 1}))
    assert Status.read(paths).state is JobState.RUNNING


def test_status_write_refreshes_the_timestamp(paths: JobPaths) -> None:
    s = Status(state=JobState.QUEUED, updated_at="1999-01-01T00:00:00+00:00")
    s.write(paths)
    assert not Status.read(paths).updated_at.startswith("1999")


def test_job_paths_are_all_under_the_job_root(tmp_path: Path) -> None:
    p = JobPaths(tmp_path / "kestrel")
    for path in (p.meta, p.status, p.narration, p.raw, p.prompt, p.reply):
        assert path.parent == p.root


# --- liveness --------------------------------------------------------------


def test_pid_alive_for_this_process() -> None:
    assert pid_alive(os.getpid()) is True


def test_pid_alive_for_nothing() -> None:
    assert pid_alive(None) is False
    assert pid_alive(0) is False


def test_pid_alive_for_a_reaped_child() -> None:
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    assert pid_alive(pid) is False
