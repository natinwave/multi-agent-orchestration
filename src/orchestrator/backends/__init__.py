"""Backend selection, keyed by the ``type`` in the registry."""

from __future__ import annotations

from ..registry import Agent
from .base import Backend, BackendError, Outcome
from .bedrock_agentcore import BedrockAgentCoreBackend
from .container import ContainerBackend
from .http_openai import HttpOpenAIBackend
from .local import LocalBackend

__all__ = ["Backend", "BackendError", "Outcome", "for_agent", "BACKENDS"]

BACKENDS = {
    "container": ContainerBackend,
    "http_openai": HttpOpenAIBackend,
    # Runs on the host, outside any boundary. See local.py for what that
    # costs and what it refuses to do as a result.
    "local": LocalBackend,
    # An agent that already lives somewhere else, on AWS.
    "bedrock_agentcore": BedrockAgentCoreBackend,
}


def for_agent(agent: Agent) -> Backend:
    try:
        return BACKENDS[agent.type]()
    except KeyError:
        raise BackendError(f"agent {agent.name}: unknown backend type {agent.type!r}") from None
