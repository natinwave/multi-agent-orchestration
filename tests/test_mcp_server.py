"""The MCP layer is a thin shell over the supervisor, so these tests check
the shape of the surface a client model sees -- and that going through it
does not route around the redaction chokepoint.

Plants a fake credential to prove the MCP layer does not route around the
redaction chokepoint. secret-scan: allow
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from orchestrator.registry import load
from orchestrator.supervisor import Supervisor

pytest.importorskip("mcp", reason="MCP server is an optional extra: pip install '.[mcp]'")

from orchestrator.mcp_server import build_server  # noqa: E402

BRIEF_TOOLS = {"ask", "check", "list_agents", "list_jobs"}
EXTRA_TOOLS = {"reply", "list_repos"}


@pytest.fixture
def server(tmp_path: Path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "agents.toml").write_text(
        '[agents.hermes]\ntype = "http_openai"\n'
        'base_url = "http://127.0.0.1:1/v1"\nmodel = "hermes"\n'
    )
    (cfg / "repos.toml").write_text('[repos.main]\nurl = "git@h:o/main.git"\n')
    (cfg / "orchestrator.toml").write_text(f'[paths]\nroot = "{tmp_path / "srv"}"\n')
    sup = Supervisor.create(config=load(cfg, tmp_path / "srv"), environ={})
    return build_server(sup), sup


def call(server, name: str, args: dict) -> dict:
    result = asyncio.run(server.call_tool(name, args))
    return json.loads(result.content[0].text)


def test_exposes_the_four_tools_the_brief_asked_for(server) -> None:
    names = {t.name for t in asyncio.run(server[0].list_tools())}
    assert BRIEF_TOOLS <= names
    assert names == BRIEF_TOOLS | EXTRA_TOOLS


def test_every_tool_is_described_for_the_client_model(server) -> None:
    """Docstrings here are prompt surface -- the model picks tools by them."""
    for tool in asyncio.run(server[0].list_tools()):
        assert tool.description and len(tool.description) > 40, tool.name


def test_ask_takes_an_optional_spoken_repo(server) -> None:
    (ask,) = [t for t in asyncio.run(server[0].list_tools()) if t.name == "ask"]
    schema = ask.input_schema
    assert set(schema["required"]) == {"agent", "message"}
    assert "repo" in schema["properties"]


def test_check_defaults_to_no_raw_log(server) -> None:
    """The default has to be the cheap one: this lands in a voice context."""
    (check,) = [t for t in asyncio.run(server[0].list_tools()) if t.name == "check"]
    assert check.input_schema["properties"]["tail"].get("default") == 0
    assert "tail" not in check.input_schema.get("required", [])


def test_list_agents_round_trips(server) -> None:
    assert [a["name"] for a in call(server[0], "list_agents", {})["agents"]] == ["hermes"]


def test_errors_come_back_as_data_not_exceptions(server) -> None:
    """A raised exception would reach the client as a protocol error and
    tell the voice model nothing useful."""
    result = call(server[0], "ask", {"agent": "nope", "message": "hi"})
    assert result["error"] == "unknown_agent"
    assert result["known"] == ["hermes"]


def test_responses_are_scrubbed_through_the_supervisor(server) -> None:
    srv, sup = server
    sup.redactor.register_value("TOKEN", "sk-ant-planted-secret-value")
    result = call(srv, "check", {"job_id": "sk-ant-planted-secret-value"})
    assert "sk-ant-planted-secret-value" not in json.dumps(result)
