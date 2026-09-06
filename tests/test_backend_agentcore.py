"""Talking to an agent on AWS Bedrock AgentCore.

The boto3 client is injectable, so everything except the network call is
tested here. The call itself is shaped from the console's own snippet and
has not been made against a live account -- that is stated in the module
rather than implied by green tests.

Fabricated credentials throughout. secret-scan: allow
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.backends import BACKENDS, for_agent
from orchestrator.backends.base import BackendError
from orchestrator.backends.bedrock_agentcore import BedrockAgentCoreBackend
from orchestrator.registry import Agent, load
from orchestrator.state import JobPaths, Meta

ARN = "arn:aws:bedrock-agentcore:us-east-1:824683096545:harness/Soren-RSXzGGYQaQ"


def agent(**kwargs) -> Agent:
    spec = {
        "name": "soren",
        "type": "bedrock_agentcore",
        "harness_arn": ARN,
        "region": "us-east-1",
        "needs_repo": False,
    }
    spec.update(kwargs)
    return Agent(**spec)


def meta() -> Meta:
    return Meta(
        job_id="kestrel",
        agent="soren",
        created_at="2026-09-06T00:00:00+00:00",
        workdir="/tmp",
        session_id="3f7c1e2a-9b4d-4c8e-a1f6-2d5b8c0e4a91",
    )


class FakeClient:
    def __init__(self, stream=None, raises=None) -> None:
        self.stream = stream or []
        self.raises = raises
        self.calls: list[dict] = []

    def invoke_harness(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return {"stream": self.stream}


def deltas(*texts: str) -> list[dict]:
    return [{"contentBlockDelta": {"delta": {"text": t}}} for t in texts]


def run(backend, paths, client, prompt="hello"):
    backend.client = lambda a: client  # type: ignore[method-assign]
    return backend.run(
        agent=agent(), meta=meta(), paths=paths, workdir=Path("/tmp"),
        prompt=prompt, config=None,
    )


@pytest.fixture
def paths(tmp_path: Path) -> JobPaths:
    p = JobPaths(tmp_path / "kestrel")
    p.root.mkdir(parents=True)
    return p


# --- the call ---------------------------------------------------------------


def test_the_prompt_reaches_the_harness(paths: JobPaths) -> None:
    client = FakeClient(deltas("hi"))
    run(BedrockAgentCoreBackend(), paths, client, prompt="what is the CPC model?")
    call = client.calls[0]
    assert call["harnessArn"] == ARN
    assert call["messages"][0]["content"][0]["text"] == "what is the CPC model?"


def test_the_job_id_is_the_conversation(paths: JobPaths) -> None:
    """So replying continues the same conversation rather than starting a
    new one -- the session id is assigned at job creation for exactly this."""
    client = FakeClient(deltas("hi"))
    run(BedrockAgentCoreBackend(), paths, client)
    assert client.calls[0]["runtimeSessionId"] == meta().session_id


# --- the answer -------------------------------------------------------------


def test_streamed_deltas_are_assembled(paths: JobPaths) -> None:
    client = FakeClient(deltas("The CPC ", "model bills ", "per click."))
    outcome = run(BedrockAgentCoreBackend(), paths, client)
    assert outcome.exit_code == 0
    assert (paths.root / "answer.txt").read_text() == "The CPC model bills per click."


def test_the_first_line_becomes_the_narration(paths: JobPaths) -> None:
    from orchestrator.narration import read_tail

    run(BedrockAgentCoreBackend(), paths, FakeClient(deltas("Billed per click.\nMore detail.")))
    assert any("Billed per click." in ln.text for ln in read_tail(paths.narration, 10))


def test_other_stream_events_are_logged_not_dropped(paths: JobPaths) -> None:
    """An error event or a tool call in the stream should be visible when
    something goes wrong, not silently discarded."""
    stream = [{"internalServerException": {"message": "boom"}}, *deltas("partial")]
    run(BedrockAgentCoreBackend(), paths, FakeClient(stream))
    assert "internalServerException" in paths.raw.read_text()


def test_an_empty_answer_still_narrates(paths: JobPaths) -> None:
    from orchestrator.narration import read_tail

    run(BedrockAgentCoreBackend(), paths, FakeClient([]))
    assert read_tail(paths.narration, 10)


def test_an_enormous_answer_is_truncated(paths: JobPaths) -> None:
    from orchestrator.backends.bedrock_agentcore import MAX_ANSWER_CHARS

    client = FakeClient(deltas(*(["x" * 10_000] * 40)))
    run(BedrockAgentCoreBackend(), paths, client)
    assert len((paths.root / "answer.txt").read_text()) < MAX_ANSWER_CHARS + 1000


# --- failure ----------------------------------------------------------------


def test_an_unreachable_agent_is_reported_not_raised(paths: JobPaths) -> None:
    """A runner that dies without saying why is the failure this design
    exists to avoid."""
    outcome = run(BedrockAgentCoreBackend(), paths, FakeClient(raises=RuntimeError("no creds")))
    assert outcome.exit_code == 1
    assert "could not reach" in (outcome.detail or "")


def test_a_missing_arn_is_refused() -> None:
    with pytest.raises(BackendError, match="harness_arn"):
        BedrockAgentCoreBackend().run(
            agent=agent(harness_arn=None), meta=meta(),
            paths=JobPaths(Path("/tmp/x")), workdir=Path("/tmp"),
            prompt="hi", config=None,
        )


# --- credentials ------------------------------------------------------------


def test_keys_come_from_the_agents_own_secrets_directory(tmp_path: Path) -> None:
    secrets = tmp_path / "agentcore"
    secrets.mkdir()
    (secrets / "aws_access_key_id").write_text("AKIAFABRICATEDKEYID1\n")
    (secrets / "aws_secret_access_key").write_text("fabricated-secret-access-key\n")

    found = BedrockAgentCoreBackend()._credentials(agent(secrets_dir=str(secrets)))
    assert found["aws_access_key_id"] == "AKIAFABRICATEDKEYID1"
    assert found["aws_secret_access_key"] == "fabricated-secret-access-key"


def test_half_a_credential_is_refused(tmp_path: Path) -> None:
    """Otherwise boto3 falls back to ambient credentials and silently acts
    as somebody else."""
    secrets = tmp_path / "agentcore"
    secrets.mkdir()
    (secrets / "aws_access_key_id").write_text("AKIAFABRICATEDKEYID1")

    with pytest.raises(BackendError, match="aws_secret_access_key"):
        BedrockAgentCoreBackend()._credentials(agent(secrets_dir=str(secrets)))


def test_no_secrets_directory_defers_to_the_host(tmp_path: Path) -> None:
    """A role or shared profile is a legitimate way to be credentialed."""
    assert BedrockAgentCoreBackend()._credentials(agent()) == {}


def test_aws_keys_cannot_be_granted_over() -> None:
    from orchestrator.credentials import RESERVED

    assert {"aws_access_key_id", "aws_secret_access_key"} <= RESERVED


# --- configuration ----------------------------------------------------------


def test_the_backend_is_selectable() -> None:
    assert "bedrock_agentcore" in BACKENDS
    assert isinstance(for_agent(agent()), BedrockAgentCoreBackend)


def test_the_shipped_agents_are_configured() -> None:
    agents = load().agents
    for name in ("soren", "vesper"):
        assert agents[name].type == "bedrock_agentcore"
        assert agents[name].harness_arn.startswith("arn:aws:bedrock-agentcore:")
        assert not agents[name].needs_repo, "a remote agent has no checkout"


def test_the_two_remote_agents_are_distinguishable() -> None:
    """They share a backend and an account, so the descriptions are the
    only thing telling the voice model which to pick."""
    agents = load().agents
    assert agents["soren"].description != agents["vesper"].description
