"""Talk to a local model over an OpenAI-compatible HTTP endpoint.

This is the short-horizon path: one request, one answer, no container and no
checkout. The model runs on the same host as the supervisor, so the call
goes over loopback and no credential normally leaves the machine at all.

STUB: `base_url` in config/agents.toml points at a placeholder port. Set the
real one and re-run bootstrap; nothing in this file changes.

Written against urllib rather than requests to keep the core dependency-free
-- the Ubuntu host should be able to run the supervisor with stock Python.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from ..narration import append as narrate
from ..registry import Agent, Config
from ..state import JobPaths, JobState, Meta
from .base import BackendError, Outcome

__all__ = ["HttpOpenAIBackend"]


class HttpOpenAIBackend:
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
        if not agent.base_url or not agent.model:
            raise BackendError(f"agent {agent.name}: base_url and model are required")

        messages = self._conversation(paths, prompt, resume=resume)
        body = json.dumps(
            {
                "model": agent.model,
                "messages": messages,
                "max_tokens": agent.max_tokens,
                "stream": False,
            }
        ).encode()

        request = urllib.request.Request(
            agent.base_url.rstrip("/") + "/chat/completions",
            data=body,
            headers={"Content-Type": "application/json", **self._auth(agent)},
            method="POST",
        )

        narrate(paths.narration, f"asking {agent.model}", JobState.RUNNING)
        try:
            with urllib.request.urlopen(request, timeout=agent.timeout_seconds) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            detail = f"{agent.model} returned HTTP {exc.code}"
            self._log(paths, f"[supervisor] {detail}\n{exc.read().decode(errors='replace')}")
            narrate(paths.narration, detail, JobState.BLOCKED)
            return Outcome(exit_code=1, detail=detail)
        except (urllib.error.URLError, TimeoutError) as exc:
            # The usual cause is the model server not being up. Say so
            # plainly: this is the message that gets read out loud.
            detail = f"cannot reach {agent.name} at {agent.base_url} ({exc})"
            self._log(paths, f"[supervisor] {detail}")
            narrate(paths.narration, detail, JobState.BLOCKED)
            return Outcome(exit_code=1, detail=detail)

        text = self._answer(payload)
        self._log(paths, json.dumps(payload, indent=2))
        (paths.root / "answer.txt").write_text(text, encoding="utf-8")

        # A short-horizon backend has no way to call narrate for itself, so
        # the answer's first line stands in as the milestone.
        first_line = next((ln for ln in text.splitlines() if ln.strip()), "(empty answer)")
        narrate(paths.narration, first_line)
        return Outcome(exit_code=0)

    # -- helpers ------------------------------------------------------------

    def _auth(self, agent: Agent) -> dict[str, str]:
        """Read the bearer token, if this server wants one.

        Same discipline as the container path: the value is read from the
        per-agent secrets directory at call time and never stored.
        """
        if not (agent.secrets_dir and agent.api_key_file):
            return {}
        path = Path(agent.secrets_dir) / agent.api_key_file
        try:
            key = path.read_text(encoding="utf-8").strip()
        except OSError:
            return {}
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _conversation(self, paths: JobPaths, prompt: str, resume: bool) -> list[dict]:
        """Rebuild the exchange from disk so reply() can continue it.

        This backend is stateless between calls -- there is no session to
        resume, so the previous turn is replayed from the job directory.
        """
        answer = paths.root / "answer.txt"
        if resume and answer.exists() and paths.prompt.exists():
            return [
                {"role": "user", "content": paths.prompt.read_text(encoding="utf-8")},
                {"role": "assistant", "content": answer.read_text(encoding="utf-8")},
                {"role": "user", "content": prompt},
            ]
        return [{"role": "user", "content": prompt}]

    def _answer(self, payload: dict) -> str:
        try:
            return payload["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise BackendError(f"unexpected response shape: {json.dumps(payload)[:400]}") from None

    def _log(self, paths: JobPaths, text: str) -> None:
        with paths.raw.open("a", encoding="utf-8") as fh:
            fh.write(text.rstrip() + "\n")
