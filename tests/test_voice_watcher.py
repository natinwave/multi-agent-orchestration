"""Volunteering updates during a call.

The restraint is the feature. Being interrupted by a machine is worse than
not being told, so most of these tests are about what it stays quiet for.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from orchestrator.voice.watcher import NOTIFY_STATES, JobWatcher, describe


class FakeServer:
    """Serves scripted list_jobs and check results."""

    def __init__(self) -> None:
        self.jobs: dict[str, str] = {}
        self.narration: dict[str, list[str]] = {}

    async def call_tool(self, name, args):
        if name == "list_jobs":
            payload = {
                "jobs": [
                    {"job_id": jid, "state": state} for jid, state in self.jobs.items()
                ]
            }
        elif name == "check":
            jid = args["job_id"]
            payload = {
                "job_id": jid,
                "state": self.jobs.get(jid, "unknown"),
                "narration": self.narration.get(jid, []),
            }
        else:
            payload = {}

        class Result:
            content = [type("B", (), {"text": json.dumps(payload)})()]

        return Result()


class Heard:
    def __init__(self) -> None:
        self.said: list[str] = []
        self.busy = False

    async def announce(self, text: str) -> None:
        self.said.append(text)

    def is_busy(self) -> bool:
        return self.busy


@pytest.fixture
def setup():
    server, heard = FakeServer(), Heard()
    watcher = JobWatcher(server=server, announce=heard.announce, is_busy=heard.is_busy)
    return server, heard, watcher


def seed(watcher: JobWatcher, server: FakeServer) -> None:
    """Take the first reading, which is never announced."""
    asyncio.run(watcher.poll())
    watcher._seen = dict(server.jobs)
    asyncio.run(watcher._flush())


# --- what it speaks up about ------------------------------------------------


@pytest.mark.parametrize("state", sorted(NOTIFY_STATES))
def test_a_job_reaching_a_notable_state_is_announced(setup, state: str) -> None:
    server, heard, watcher = setup
    server.jobs = {"kestrel": "running"}
    seed(watcher, server)

    server.jobs["kestrel"] = state
    asyncio.run(watcher.poll())
    assert heard.said and "kestrel" in heard.said[0]


def test_a_parked_job_reports_the_question(setup) -> None:
    """The question is the whole point of being told."""
    server, heard, watcher = setup
    server.jobs = {"kestrel": "running"}
    seed(watcher, server)

    server.jobs["kestrel"] = "awaiting_input"
    server.narration["kestrel"] = ["reading the tests", "which staging database?"]
    asyncio.run(watcher.poll())
    assert "which staging database?" in heard.said[0]


def test_a_failure_reports_the_last_thing_that_worked(setup) -> None:
    server, heard, watcher = setup
    server.jobs = {"kestrel": "running"}
    seed(watcher, server)

    server.jobs["kestrel"] = "failed"
    server.narration["kestrel"] = ["cloned the repo", "the test suite will not start"]
    asyncio.run(watcher.poll())
    assert "will not start" in heard.said[0]


def test_several_changes_become_one_sentence(setup) -> None:
    """Three separate interruptions would be intolerable."""
    server, heard, watcher = setup
    server.jobs = {"kestrel": "running", "otter": "running", "cedar": "running"}
    seed(watcher, server)

    server.jobs = {"kestrel": "done", "otter": "done", "cedar": "failed"}
    asyncio.run(watcher.poll())
    assert len(heard.said) == 1
    for name in ("kestrel", "otter", "cedar"):
        assert name in heard.said[0]


# --- what it stays quiet about ---------------------------------------------


def test_the_first_reading_announces_nothing(setup) -> None:
    """Otherwise every call opens with a recital of everything that ever
    ran."""
    server, heard, watcher = setup
    server.jobs = {"kestrel": "done", "otter": "failed"}
    asyncio.run(watcher.poll())
    assert heard.said == []


def test_progress_is_not_news(setup) -> None:
    """queued to running changes nothing you would do differently."""
    server, heard, watcher = setup
    server.jobs = {"kestrel": "queued"}
    seed(watcher, server)

    server.jobs["kestrel"] = "running"
    asyncio.run(watcher.poll())
    assert heard.said == []


def test_a_state_that_has_not_changed_is_not_repeated(setup) -> None:
    server, heard, watcher = setup
    server.jobs = {"kestrel": "running"}
    seed(watcher, server)

    server.jobs["kestrel"] = "done"
    asyncio.run(watcher.poll())
    assert len(heard.said) == 1
    asyncio.run(watcher.poll())
    assert len(heard.said) == 1, "announced the same thing twice"


def test_a_job_started_during_the_call_is_not_echoed(setup) -> None:
    """The model just said it started that job. Repeating it is noise."""
    server, heard, watcher = setup
    server.jobs = {"kestrel": "running"}
    seed(watcher, server)

    server.jobs["otter"] = "running"
    asyncio.run(watcher.poll())
    assert heard.said == []


def test_it_waits_rather_than_talking_over_you(setup) -> None:
    """Interrupting is worse than being a moment late."""
    server, heard, watcher = setup
    server.jobs = {"kestrel": "running"}
    seed(watcher, server)

    heard.busy = True
    server.jobs["kestrel"] = "done"
    asyncio.run(watcher.poll())
    assert heard.said == [], "spoke while the model was mid-sentence"

    heard.busy = False
    asyncio.run(watcher._flush())
    assert "kestrel" in heard.said[0], "never said it once the gap arrived"


def test_nothing_held_back_is_lost(setup) -> None:
    """A change that arrives during a sentence is queued, not dropped."""
    server, heard, watcher = setup
    server.jobs = {"kestrel": "running", "otter": "running"}
    seed(watcher, server)

    heard.busy = True
    server.jobs["kestrel"] = "done"
    asyncio.run(watcher.poll())
    server.jobs["otter"] = "failed"
    asyncio.run(watcher.poll())

    heard.busy = False
    asyncio.run(watcher._flush())
    assert "kestrel" in heard.said[0] and "otter" in heard.said[0]


# --- robustness -------------------------------------------------------------


def test_a_failing_poll_does_not_end_the_call(setup) -> None:
    """The watcher runs for the length of a phone call. It may not raise."""
    server, heard, watcher = setup

    class Broken:
        async def call_tool(self, name, args):
            raise RuntimeError("supervisor unavailable")

    watcher.server = Broken()
    watcher.interval = 0.01

    async def scenario():
        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.1)
        assert not task.done(), "the watcher died"
        task.cancel()

    asyncio.run(scenario())


def test_a_state_change_is_still_reported_when_check_fails(setup) -> None:
    """Losing the narration should cost the detail, not the update."""
    server, heard, watcher = setup
    server.jobs = {"kestrel": "running"}
    seed(watcher, server)

    original = server.call_tool

    async def flaky(name, args):
        if name == "check":
            raise RuntimeError("nope")
        return await original(name, args)

    server.call_tool = flaky
    server.jobs["kestrel"] = "done"
    asyncio.run(watcher.poll())
    assert "kestrel" in heard.said[0]


# --- wording ----------------------------------------------------------------


def test_sentences_are_written_for_the_ear() -> None:
    assert describe("kestrel", "done", ["opened a pull request"]) == (
        "kestrel finished. opened a pull request"
    )
    assert describe("otter", "awaiting_input", ["which database?"]) == (
        "otter needs an answer: which database?"
    )
    assert describe("cedar", "done", []) == "cedar finished."


def test_blank_narration_lines_are_skipped() -> None:
    assert describe("kestrel", "done", ["did the thing", "  "]) == (
        "kestrel finished. did the thing"
    )
