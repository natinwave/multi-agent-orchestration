"""Job ids are allocated by mkdir, so the tests care mostly about the race
and about exhaustion -- the two places a name could be handed out twice."""

import random
from pathlib import Path

import pytest

from orchestrator.ids import (
    JOB_ID_RE,
    NoNamesAvailable,
    allocate,
    is_valid,
    load_wordlist,
)


def test_shipped_wordlist_is_usable() -> None:
    words = load_wordlist()
    assert len(words) > 100
    assert len(set(words)) == len(words), "duplicate names would collide silently"
    for w in words:
        assert JOB_ID_RE.fullmatch(w)
        assert len(w) <= 12, f"{w} is a mouthful to say aloud"


def test_allocate_creates_the_directory(tmp_path: Path) -> None:
    job_id, path = allocate(tmp_path, ["kestrel"])
    assert job_id == "kestrel"
    assert path == tmp_path / "kestrel"
    assert path.is_dir()


def test_allocate_creates_the_jobs_root(tmp_path: Path) -> None:
    root = tmp_path / "does" / "not" / "exist"
    _, path = allocate(root, ["kestrel"])
    assert path.is_dir()


def test_allocate_never_reuses_a_name(tmp_path: Path) -> None:
    words = ["kestrel", "otter", "cedar"]
    got = {allocate(tmp_path, words)[0] for _ in range(3)}
    assert got == set(words)


def test_allocate_suffixes_when_pool_is_exhausted(tmp_path: Path) -> None:
    allocate(tmp_path, ["kestrel"])
    job_id, _ = allocate(tmp_path, ["kestrel"])
    assert job_id == "kestrel-2"
    assert allocate(tmp_path, ["kestrel"])[0] == "kestrel-3"


def test_suffixed_names_are_still_valid_ids(tmp_path: Path) -> None:
    allocate(tmp_path, ["kestrel"])
    job_id, _ = allocate(tmp_path, ["kestrel"])
    assert is_valid(job_id)


def test_allocate_skips_a_name_taken_by_another_process(tmp_path: Path) -> None:
    """Simulates the race: the directory appears between our choosing the
    name and our claiming it."""
    (tmp_path / "kestrel").mkdir(parents=True)
    job_id, _ = allocate(tmp_path, ["kestrel", "otter"])
    assert job_id == "otter"


def test_allocation_is_deterministic_under_a_seeded_rng(tmp_path: Path) -> None:
    words = load_wordlist()
    a = allocate(tmp_path / "a", words, random.Random(7))[0]
    b = allocate(tmp_path / "b", words, random.Random(7))[0]
    assert a == b


def test_exhaustion_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("orchestrator.ids._MAX_SUFFIX", 2)
    for _ in range(2):
        allocate(tmp_path, ["kestrel"])
    with pytest.raises(NoNamesAvailable):
        allocate(tmp_path, ["kestrel"])


@pytest.mark.parametrize("bad", ["", "a", "Kestrel", "kestrel_2", "../etc", "kestrel/x", "9lives"])
def test_is_valid_rejects_unsafe_ids(bad: str) -> None:
    """Ids arrive from an MCP client and get joined onto a path."""
    assert is_valid(bad) is False


def test_load_wordlist_rejects_an_unspeakable_entry(tmp_path: Path) -> None:
    p = tmp_path / "w.txt"
    p.write_text("# comment\n\nkestrel\nNOT VALID\n")
    with pytest.raises(ValueError):
        load_wordlist(p)


def test_load_wordlist_rejects_an_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "w.txt"
    p.write_text("# only a comment\n")
    with pytest.raises(ValueError):
        load_wordlist(p)
