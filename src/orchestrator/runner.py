"""The detached process that actually runs one job.

``ask()`` spawns this with ``setsid`` and returns immediately, which is why
a job survives the MCP client that started it -- and why the supervisor can
stay stateless. This process is the **sole writer of status.json**: no other
component writes it, so there is nothing to lock.

Invoked as ``python -m orchestrator.runner <job_dir>``, never imported by
the supervisor. The two only meet through the job directory.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

from . import backends
from .narration import append as narrate
from .narration import last_state
from .registry import Config, load
from .state import JobPaths, JobState, Meta, Status, final_state

__all__ = ["run_job", "main"]


def run_job(paths: JobPaths, config: Config, resume: bool = False) -> JobState:
    meta = Meta.read(paths)
    status = Status(state=JobState.RUNNING, runner_pid=os.getpid())
    status.write(paths)

    prompt_file = paths.reply if resume else paths.prompt
    prompt = prompt_file.read_text(encoding="utf-8")

    try:
        agent = config.agent(meta.agent)
        backend = backends.for_agent(agent)
        outcome = backend.run(
            agent=agent,
            meta=meta,
            paths=paths,
            workdir=Path(meta.workdir),
            prompt=prompt,
            config=config,
            resume=resume,
        )
        exit_code, detail = outcome.exit_code, outcome.detail
    except Exception as exc:  # noqa: BLE001 - a runner must never die silently
        # Nothing above us is watching, so the traceback goes to raw.log and
        # a human-readable line goes to narration. A job that dies without
        # saying why is the failure mode this whole design exists to avoid.
        with paths.raw.open("a", encoding="utf-8") as fh:
            fh.write("\n[supervisor] runner failed\n")
            traceback.print_exc(file=fh)
        detail = f"{type(exc).__name__}: {exc}"
        narrate(paths.narration, f"could not start: {detail}")
        exit_code = 70

    # A grant tied to this job dies with it. Best-effort: a job must still
    # be marked finished even if the secrets directory has gone away.
    try:
        revoked = config.credential_store().revoke_for_job(meta.job_id)
        if revoked:
            narrate(
                paths.narration,
                f"released {len(revoked)} delegated credential(s)",
            )
    except Exception:  # noqa: BLE001 - never let cleanup mask the result
        pass

    state = final_state(exit_code, last_state(paths.narration))
    Status(
        state=state,
        runner_pid=os.getpid(),
        exit_code=exit_code,
        detail=detail,
    ).write(paths)
    return state


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    resume = "--resume" in argv
    argv = [a for a in argv if a != "--resume"]
    if len(argv) != 1:
        print("usage: python -m orchestrator.runner [--resume] <job_dir>", file=sys.stderr)
        return 2

    paths = JobPaths(Path(argv[0]))
    # The job directory tells us the runtime root; ORCH_CONFIG_DIR, set by
    # the supervisor when it spawned us, tells us which registry to read.
    config = load(root_override=paths.root.parent.parent)
    state = run_job(paths, config, resume=resume)
    return 0 if state is not JobState.FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
