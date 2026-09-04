"""Backend selection, keyed by the ``type`` in the registry."""

from __future__ import annotations

from ..registry import Agent
from .base import Backend, BackendError, Outcome
from .container import ContainerBackend
from .http_openai import HttpOpenAIBackend

__all__ = ["Backend", "BackendError", "Outcome", "for_agent", "BACKENDS"]

BACKENDS = {
    "container": ContainerBackend,
    "http_openai": HttpOpenAIBackend,
}


def for_agent(agent: Agent) -> Backend:
    try:
        return BACKENDS[agent.type]()
    except KeyError:
        raise BackendError(f"agent {agent.name}: unknown backend type {agent.type!r}") from None
