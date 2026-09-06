"""Talk to an agent running on AWS Bedrock AgentCore.

For agents that already live somewhere else and know things this machine
does not -- a colleague's agent, one wired into a product's own data.
Nothing is checked out and nothing is run here; a message goes out and an
answer comes back.

Session handling falls out for free. Every job is assigned a UUID at
creation for ``--session-id``, and AgentCore takes a ``runtimeSessionId``,
so the same id makes ``reply()`` continue the same conversation rather
than starting a new one.

Credentials follow the same discipline as everything else: read from the
per-agent secrets directory at call time and handed to the client
directly, never exported into the environment where anything else could
inherit them.

NOT VERIFIED AGAINST A LIVE ACCOUNT. The client is injectable so the
response handling is tested, but the call itself is shaped from the
invocation snippet AgentCore's console produces. If it fails, the shape of
``invoke_harness`` is the first thing to check.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..narration import append as narrate
from ..registry import Agent, Config
from ..state import JobPaths, JobState, Meta
from .base import BackendError, Outcome

__all__ = ["BedrockAgentCoreBackend", "MAX_ANSWER_CHARS"]

log = logging.getLogger("orchestrator.backends.agentcore")

#: A remote agent can talk for a long time. The whole answer is kept on
#: disk; this only bounds what is held in memory while assembling it.
MAX_ANSWER_CHARS = 200_000


class BedrockAgentCoreBackend:
    def run(
        self,
        *,
        agent: Agent,
        meta: Meta,
        paths: JobPaths,
        workdir: Path,
        prompt: str,
        config: Config,
        resume: bool = False,
    ) -> Outcome:
        if not agent.harness_arn:
            raise BackendError(f"agent {agent.name}: harness_arn is required")

        client = self.client(agent)
        narrate(paths.narration, f"asking {agent.name}", JobState.RUNNING)

        try:
            response = client.invoke_harness(
                harnessArn=agent.harness_arn,
                # The job's own id, so a reply continues the conversation
                # rather than starting a new one.
                runtimeSessionId=meta.session_id,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
            )
        except Exception as exc:  # noqa: BLE001 - surfaced, not raised
            detail = f"could not reach {agent.name}: {type(exc).__name__}"
            self._log(paths, f"[supervisor] {detail}: {exc}")
            narrate(paths.narration, detail, JobState.BLOCKED)
            return Outcome(exit_code=1, detail=detail)

        try:
            answer = self.collect(response, paths)
        except Exception as exc:  # noqa: BLE001
            detail = f"{agent.name} sent something unreadable: {type(exc).__name__}"
            self._log(paths, f"[supervisor] {detail}: {exc}")
            return Outcome(exit_code=1, detail=detail)

        (paths.root / "answer.txt").write_text(answer, encoding="utf-8")
        first = next((ln for ln in answer.splitlines() if ln.strip()), "(no answer)")
        narrate(paths.narration, first)
        return Outcome(exit_code=0)

    # -- internals ----------------------------------------------------------

    def client(self, agent: Agent):
        """A boto3 client, credentialed from this agent's secrets.

        Passed explicitly rather than exported: an environment variable
        would be inherited by anything this process later spawns, and the
        point of per-agent directories is that one identity's credentials
        do not leak into another's.
        """
        try:
            import boto3
        except ImportError:
            raise BackendError(
                "boto3 is not installed. It is an optional extra: "
                "pip install -e '.[aws]'"
            ) from None

        keys = self._credentials(agent)
        return boto3.client(
            "bedrock-agentcore",
            region_name=agent.region or "us-east-1",
            **keys,
        )

    @staticmethod
    def _credentials(agent: Agent) -> dict:
        """Access keys from the per-agent secrets directory, if present.

        Absent is not an error: the host may have a role or a shared
        profile, and boto3 will find it. Explicit keys simply win.
        """
        if not agent.secrets_dir:
            return {}
        secrets = Path(agent.secrets_dir)
        wanted = {
            "aws_access_key_id": "aws_access_key_id",
            "aws_secret_access_key": "aws_secret_access_key",
            "aws_session_token": "aws_session_token",
        }
        found = {}
        for kwarg, filename in wanted.items():
            try:
                value = (secrets / filename).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if value:
                found[kwarg] = value
        # Half a credential is worse than none: boto3 would fall back to
        # ambient credentials and silently act as somebody else.
        if "aws_access_key_id" in found and "aws_secret_access_key" not in found:
            raise BackendError(
                f"agent {agent.name}: found aws_access_key_id but no "
                "aws_secret_access_key in its secrets directory"
            )
        return found

    def collect(self, response: dict, paths: JobPaths) -> str:
        """Assemble the streamed answer.

        AgentCore streams content-block deltas. Everything else in the
        stream is written to the raw log rather than dropped, so an error
        event or a tool call is visible when something goes wrong.
        """
        chunks: list[str] = []
        length = 0
        for event in response.get("stream", []):
            if "contentBlockDelta" in event:
                text = (event["contentBlockDelta"].get("delta") or {}).get("text")
                if text:
                    # Trim the chunk rather than appending it whole and
                    # checking afterwards: a delta can be any size, so a cap
                    # that a single chunk can overshoot is not a cap.
                    room = MAX_ANSWER_CHARS - length
                    if len(text) >= room:
                        chunks.append(text[:room])
                        chunks.append("\n[supervisor] answer truncated")
                        break
                    chunks.append(text)
                    length += len(text)
            else:
                self._log(paths, f"[event] {list(event)[:3]}")
        return "".join(chunks)

    def _log(self, paths: JobPaths, text: str) -> None:
        with paths.raw.open("a", encoding="utf-8") as fh:
            fh.write(text.rstrip() + "\n")

    def stop(self, *, agent: Agent, meta: Meta) -> None:
        """Nothing to stop: the request either returns or times out."""
        return None
