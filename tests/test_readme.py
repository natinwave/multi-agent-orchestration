"""The README is the handover document.

It is what a new session, or a relay agent with a cleared context, reads
to pick this up. Claims in it drift out of true silently, so the ones that
can be checked against the code are checked here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text()


def test_it_does_not_still_call_itself_unbuilt() -> None:
    """It described a voice front-end that "does not exist yet" for some
    time after the phone was answering."""
    for stale in ("does not exist yet", "no audio here", "Phase one is"):
        assert stale not in README, f"stale claim: {stale!r}"


def test_every_cli_command_is_documented() -> None:
    """The command table is the first thing anyone looks for."""
    from orchestrator.cli import build_parser

    actions = build_parser()._subparsers._group_actions[0]  # type: ignore[attr-defined]
    for name in actions.choices:
        assert name in README, f"{name} is not mentioned in the README"


def test_every_mcp_tool_is_documented() -> None:
    import asyncio

    from orchestrator.mcp_server import build_server
    from orchestrator.registry import load
    from orchestrator.supervisor import Supervisor

    pytest.importorskip("mcp")
    server = build_server(Supervisor.create(load()))
    for tool in asyncio.run(server.list_tools()):
        assert tool.name in README, f"MCP tool {tool.name} is undocumented"


def test_every_job_state_is_in_the_states_table() -> None:
    from orchestrator.state import JobState

    table = README[README.index("### States") : README.index("### States") + 1500]
    for state in JobState:
        assert f"`{state}`" in table, f"{state} is missing from the states table"


def test_every_backend_type_is_explained() -> None:
    from orchestrator.backends import BACKENDS

    for name in BACKENDS:
        assert name in README, f"backend {name} is undocumented"


def test_the_profile_walkthrough_is_present_and_ordered() -> None:
    """Someone picking this up cold needs the whole sequence, not a
    scattering of references to profiles."""
    assert "### Making a profile" in README
    section = README[README.index("### Making a profile") :]
    section = section[: section.index("Three mechanics underneath")]
    for step in ("repos.toml", "profiles/ledger.toml", "docker build", "sync-credentials"):
        assert step in section, f"the walkthrough never mentions {step}"


def test_it_does_not_promise_a_default_repository() -> None:
    """No agent has one, and a job without a repo gets an empty directory."""
    from orchestrator.registry import load

    assert not any(a.default_repo for a in load().agents.values())
    assert "no agent defaults to one" in README.lower() or "No agent has a `default_repo`" in README


def test_the_test_count_is_roughly_right() -> None:
    """A stale number reads as a stale document."""
    import subprocess

    match = re.search(r"pytest -q\s+# (\d+) tests", README)
    assert match, "the README should say how many tests there are"
    claimed = int(match.group(1))

    # This project's pytest config prints one "path: count" line per file
    # rather than a total, so sum them. A skip here would make the check
    # worthless -- it would pass forever without ever looking.
    result = subprocess.run(
        [".venv/bin/python", "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True,
    )
    per_file = re.findall(r"^\S+\.py: (\d+)$", result.stdout, re.M)
    assert per_file, f"could not count tests; pytest said:\n{result.stdout[-500:]}"
    actual = sum(int(n) for n in per_file)
    assert abs(actual - claimed) <= 25, f"README says {claimed}, suite has {actual}"
