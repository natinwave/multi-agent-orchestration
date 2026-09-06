"""The bridge from the realtime model to the supervisor.

The grant guard is the point of this file: the user chose to let the voice
agent start jobs freely but confirm every credential, and a prompt is not
a control. These tests are what make it one.

Fabricated credentials only. secret-scan: allow
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from orchestrator.registry import load
from orchestrator.supervisor import Supervisor
from orchestrator.voice.tools import (
    CONFIRM_WINDOW_SECONDS,
    GrantGuard,
    ToolDispatcher,
    realtime_tools,
)

pytest.importorskip("mcp", reason="the MCP extra is optional: pip install '.[mcp]'")

from orchestrator.mcp_server import build_server  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def server(tmp_path: Path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "agents.toml").write_text(
        '[agents.hermes]\ntype = "http_openai"\n'
        'base_url = "http://127.0.0.1:1/v1"\nmodel = "hermes"\n'
    )
    (cfg / "repos.toml").write_text('[repos.main]\nurl = "https://h/o/main.git"\n')
    (cfg / "orchestrator.toml").write_text(
        f'[paths]\nroot = "{tmp_path / "srv"}"\n'
        f'[credentials]\nsecrets_root = "{tmp_path / "secrets"}"\n'
    )
    sup = Supervisor.create(config=load(cfg, tmp_path / "srv"), environ={})
    return build_server(sup)


# --- tool definitions ------------------------------------------------------


def test_every_supervisor_tool_is_offered_to_the_model(server) -> None:
    tools = asyncio.run(realtime_tools(server))
    names = {t["name"] for t in tools}
    assert {"ask", "check", "grant", "revoke", "list_jobs"} <= names


def test_tool_definitions_have_the_shape_the_api_wants(server) -> None:
    for tool in asyncio.run(realtime_tools(server)):
        assert tool["type"] == "function"
        assert tool["name"] and tool["description"]
        assert tool["parameters"]["type"] == "object"


def test_descriptions_are_reused_not_rewritten(server) -> None:
    """They are prompt surface written once in mcp_server.py; a second copy
    here would drift."""
    tools = {t["name"]: t["description"] for t in asyncio.run(realtime_tools(server))}
    assert "ALWAYS SAY WHAT YOU GRANTED" in tools["grant"]


def test_the_grant_description_demands_a_report(server) -> None:
    """With the confirmation step gone, this sentence is the whole control:
    it is how a misheard request gets caught."""
    tools = {t["name"]: t["description"] for t in asyncio.run(realtime_tools(server))}
    grant = tools["grant"]
    assert "ALWAYS SAY WHAT YOU GRANTED" in grant
    assert "not optional" in grant
    assert "never speculatively" in grant.lower()


# --- the grant guard -------------------------------------------------------


def test_by_default_a_grant_happens_at_once() -> None:
    """The owner's call, made after living with the two-step: the rote
    read-back is friction when you trust the model to interpret the
    request, and being *told* what happened is the half that catches a
    mistake. That half is required by the tool description instead."""
    assert GrantGuard().check("grant", {"agent": "hermes", "credential": "x"}) is None


def test_the_two_step_can_be_turned_back_on() -> None:
    held = GrantGuard(enabled=True).check("grant", {"agent": "hermes", "credential": "x"})
    assert held is not None and held["status"] == "needs_confirmation"


def test_the_first_grant_never_grants() -> None:
    guard = GrantGuard(enabled=True)
    held = guard.check("grant", {"agent": "hermes", "credential": "staging password"})
    assert held is not None
    assert held["status"] == "needs_confirmation"
    assert "hermes" in held["say"] and "staging password" in held["say"]


def test_an_identical_second_call_is_allowed() -> None:
    guard = GrantGuard(enabled=True)
    args = {"agent": "hermes", "credential": "staging password"}
    guard.check("grant", args)
    assert guard.check("grant", args) is None


def test_confirmation_is_consumed_not_reusable() -> None:
    """A single spoken yes authorises exactly one grant."""
    guard = GrantGuard(enabled=True)
    args = {"agent": "hermes", "credential": "staging password"}
    guard.check("grant", args)
    assert guard.check("grant", args) is None
    assert guard.check("grant", args) is not None  # asks again


def test_confirming_one_credential_does_not_authorise_another() -> None:
    guard = GrantGuard(enabled=True)
    guard.check("grant", {"agent": "hermes", "credential": "staging password"})
    held = guard.check("grant", {"agent": "hermes", "credential": "production key"})
    assert held is not None, "a yes for staging must not release production"


def test_confirming_for_one_agent_does_not_authorise_another() -> None:
    guard = GrantGuard(enabled=True)
    guard.check("grant", {"agent": "hermes", "credential": "staging password"})
    held = guard.check("grant", {"agent": "claude-code", "credential": "staging password"})
    assert held is not None


def test_confirmation_expires() -> None:
    """An approval must not sit around waiting to be used ten minutes into
    a different conversation."""
    clock = FakeClock()
    guard = GrantGuard(enabled=True, _clock=clock)
    args = {"agent": "hermes", "credential": "staging password"}
    guard.check("grant", args)
    clock.advance(CONFIRM_WINDOW_SECONDS + 1)
    assert guard.check("grant", args) is not None


def test_confirmation_survives_a_normal_pause() -> None:
    clock = FakeClock()
    guard = GrantGuard(enabled=True, _clock=clock)
    args = {"agent": "hermes", "credential": "staging password"}
    guard.check("grant", args)
    clock.advance(20)
    assert guard.check("grant", args) is None


def test_matching_ignores_case_and_job_scope() -> None:
    """Re-asking because the model capitalised differently, or added the
    job scope on the second call, would teach the user that confirmations
    are noise to be talked through."""
    guard = GrantGuard(enabled=True)
    guard.check("grant", {"agent": "Hermes", "credential": "Staging Password"})
    allowed = guard.check(
        "grant", {"agent": "hermes", "credential": "staging password", "job_id": "kestrel"}
    )
    assert allowed is None


def test_reading_tools_are_never_gated() -> None:
    guard = GrantGuard(enabled=True)
    for name in ("ask", "check", "list_jobs", "list_credentials", "revoke"):
        assert guard.check(name, {"agent": "hermes"}) is None


def test_revoke_is_deliberately_not_gated() -> None:
    """Withdrawing access is the safe direction. Making someone confirm it
    twice would slow down the one action you might need in a hurry."""
    assert GrantGuard(enabled=True).check("revoke", {"agent": "hermes"}) is None


def test_forget_clears_pending_confirmations() -> None:
    guard = GrantGuard(enabled=True)
    args = {"agent": "hermes", "credential": "staging password"}
    guard.check("grant", args)
    guard.forget()
    assert guard.check("grant", args) is not None


# --- dispatch --------------------------------------------------------------


def call(dispatcher: ToolDispatcher, name: str, args: dict) -> dict:
    raw = asyncio.run(dispatcher.call(name, json.dumps(args)))
    return json.loads(raw)


def test_a_read_tool_round_trips(server) -> None:
    result = call(ToolDispatcher(server), "list_agents", {})
    assert [a["name"] for a in result["agents"]] == ["hermes"]


def test_a_held_grant_does_not_reach_the_supervisor(server, tmp_path: Path) -> None:
    """When the two-step is on, the guard has to run before the call."""
    dispatcher = ToolDispatcher(server, guard=GrantGuard(enabled=True))
    result = call(dispatcher, "grant", {"agent": "hermes", "credential": "anything"})
    assert result["status"] == "needs_confirmation"
    assert not (tmp_path / "secrets").exists()


def test_bad_json_arguments_come_back_as_an_error(server) -> None:
    """Something the model can read and explain beats a dropped session."""
    raw = asyncio.run(ToolDispatcher(server).call("check", "{not json"))
    assert "error" in json.loads(raw)


def test_non_object_arguments_are_rejected(server) -> None:
    raw = asyncio.run(ToolDispatcher(server).call("check", '["kestrel"]'))
    assert "error" in json.loads(raw)


def test_an_unknown_tool_is_an_error_not_a_crash(server) -> None:
    raw = asyncio.run(ToolDispatcher(server).call("launch_missiles", "{}"))
    assert "error" in json.loads(raw)


def test_errors_from_the_supervisor_reach_the_model(server) -> None:
    result = call(ToolDispatcher(server), "check", {"job_id": "nosuchjob"})
    assert result["error"] == "unknown_job"
