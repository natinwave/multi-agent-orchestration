"""Per-job working directories.

Concurrent jobs must not collide on branches or files, so each one gets its
own ``git worktree`` cut from the main clone. Jobs that need no repository
get a plain scratch directory instead -- a question for the local model does
not need a checkout.

The path matters more than it looks. A worktree's ``.git`` file stores an
*absolute* path back to the main clone, and the container sees the same
directory at the same path only because ``/srv/orchestration`` is bind
mounted at ``/srv/orchestration``. Git grew ``worktree --relative-paths`` in
2.48; Ubuntu 24.04 ships 2.43, so the identical-path bind is not a style
choice, it is the thing that makes worktrees resolve inside the container.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .registry import Repo

__all__ = ["Workspace", "prepare", "remove", "GitError", "branch_name"]


class GitError(RuntimeError):
    """A git command failed. Carries stderr, which the runner narrates."""


@dataclass(frozen=True)
class Workspace:
    path: Path
    is_worktree: bool
    branch: str | None = None


def branch_name(job_id: str) -> str:
    """One branch per job, named after the job so it is speakable too."""
    return f"job/{job_id}"


def _git(args: list[str], cwd: Path | None = None, timeout: int = 120) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def prepare(
    job_id: str,
    repo: Repo | None,
    repo_path: Path | None,
    worktrees_dir: Path,
    scratch_dir: Path,
) -> Workspace:
    """Create the directory this job will run in.

    No repo means a scratch directory. With a repo, a fresh worktree on a
    new branch cut from the repo's base ref.
    """
    if repo is None or repo_path is None:
        path = scratch_dir / job_id
        path.mkdir(parents=True, exist_ok=True)
        return Workspace(path=path, is_worktree=False)

    if not (repo_path / ".git").exists():
        raise GitError(
            f"{repo_path} is not a clone; run bootstrap.sh to fetch repo {repo.name!r}"
        )

    path = worktrees_dir / job_id
    branch = branch_name(job_id)
    worktrees_dir.mkdir(parents=True, exist_ok=True)

    # Fetch before branching so a job starts from what the remote has now,
    # not from whenever bootstrap last ran. Offline is survivable: fall back
    # to the local ref rather than refusing to start the job.
    try:
        _git(["fetch", "--quiet", "origin", repo.base_ref], cwd=repo_path)
        base = f"origin/{repo.base_ref}"
    except GitError:
        base = repo.base_ref

    # A stale worktree registration from a crashed run would block the add.
    _git(["worktree", "prune"], cwd=repo_path)
    _git(["worktree", "add", "--quiet", "-b", branch, str(path), base], cwd=repo_path)
    return Workspace(path=path, is_worktree=True, branch=branch)


def remove(job_id: str, repo_path: Path | None, path: Path, keep_branch: bool = True) -> None:
    """Tear a workspace down.

    The branch is kept by default: a job's output is the reason it ran, and
    reaping a job should not silently delete work that was never pushed.
    """
    if repo_path is not None and (repo_path / ".git").exists():
        try:
            _git(["worktree", "remove", "--force", str(path)], cwd=repo_path)
        except GitError:
            # Already gone, or never registered. Fall through to rmtree so
            # reaping is never blocked by a half-created workspace.
            pass
        _git(["worktree", "prune"], cwd=repo_path)
        if not keep_branch:
            try:
                _git(["branch", "-D", branch_name(job_id)], cwd=repo_path)
            except GitError:
                pass
    shutil.rmtree(path, ignore_errors=True)
