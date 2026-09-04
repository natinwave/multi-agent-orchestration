"""Speakable job ids.

A voice model reads these aloud and keeps several in its head at once, so
they are short words rather than UUIDs. Names are drawn from
``data/wordlist.txt`` and allocated by creating the job directory: ``mkdir``
is atomic on every filesystem we care about, so two supervisors racing for
the same name cannot both win.

Names are recycled only once every word is used. That keeps them short, at
the cost of an old job's name being reusable after it is reaped -- which is
why :func:`allocate` prefers names belonging to no job at all, and only then
falls back to a numeric suffix.
"""

from __future__ import annotations

import random
import re
from pathlib import Path

__all__ = ["load_wordlist", "allocate", "is_valid", "JOB_ID_RE", "NoNamesAvailable"]

JOB_ID_RE = re.compile(r"\A[a-z]{3,16}(?:-[0-9]{1,3})?\Z")

_DEFAULT_WORDLIST = Path(__file__).resolve().parents[2] / "data" / "wordlist.txt"

# Hard ceiling on the numeric suffix search, so a pathological state
# directory cannot spin here forever.
_MAX_SUFFIX = 999


class NoNamesAvailable(RuntimeError):
    """Every name, and every suffixed variant, is already taken."""


def is_valid(job_id: str) -> bool:
    """Whether *job_id* is a name this module could have produced.

    Used to validate ids arriving from an MCP client before they are joined
    onto a filesystem path.
    """
    return bool(JOB_ID_RE.fullmatch(job_id))


def load_wordlist(path: Path | None = None) -> list[str]:
    """Read the name pool, ignoring comments and blank lines."""
    path = path or _DEFAULT_WORDLIST
    words = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        word = raw.strip()
        if not word or word.startswith("#"):
            continue
        if not is_valid(word):
            raise ValueError(f"{path}: {word!r} is not a usable job name")
        words.append(word)
    if not words:
        raise ValueError(f"{path}: no usable names")
    return words


def allocate(jobs_dir: Path, words: list[str], rng: random.Random | None = None) -> tuple[str, Path]:
    """Claim an unused name and create its job directory.

    Returns ``(job_id, job_path)``. The directory's creation *is* the claim:
    :func:`os.mkdir` fails if it already exists, so a name is never handed to
    two jobs even across separate supervisor processes.
    """
    rng = rng or random.Random()
    jobs_dir.mkdir(parents=True, exist_ok=True)

    # Shuffle rather than sample, so exhausting the pool is a clean walk
    # instead of an unbounded retry loop.
    candidates = list(words)
    rng.shuffle(candidates)

    for word in candidates:
        path = jobs_dir / word
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return word, path

    # Every bare name is taken. Fall back to a spoken-friendly suffix:
    # "kestrel two" is still easier to say back than a UUID.
    for suffix in range(2, _MAX_SUFFIX + 1):
        for word in candidates:
            path = jobs_dir / f"{word}-{suffix}"
            try:
                path.mkdir()
            except FileExistsError:
                continue
            return path.name, path

    raise NoNamesAvailable(
        f"all {len(words)} names and suffixes up to {_MAX_SUFFIX} are in use under {jobs_dir}"
    )
