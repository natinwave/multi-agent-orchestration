"""The repo-side half of the no-secrets rule.

Every credential here is fabricated and exists to prove the scanner catches
its shape. secret-scan: allow
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.secret_scan import MARKER, main, scan


def test_a_planted_credential_is_found(tmp_path: Path) -> None:
    f = tmp_path / "config.sh"
    f.write_text("GITHUB_TOKEN=ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789\n")
    (finding,) = scan([f])
    assert finding.line == 1
    assert "ghp_" in finding.snippet


def test_op_references_are_fine(tmp_path: Path) -> None:
    """The whole point: references are allowed, values are not. Recognised
    by shape rather than by exempting .env.example, so a real value that
    lands in that file is still caught."""
    f = tmp_path / ".env"
    f.write_text('CLAUDE_CODE_OAUTH_TOKEN="op://Orchestration/claude-code/oauth_token"\n')
    assert scan([f]) == []


def test_a_real_value_beside_a_reference_is_still_caught(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text(
        'A="op://Vault/item/field"\n'
        'B="ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"\n'
    )
    assert [f.line for f in scan([f])] == [2]


def test_the_marker_opts_a_file_out(tmp_path: Path) -> None:
    f = tmp_path / "test_thing.py"
    f.write_text(f'"""Fake creds on purpose. {MARKER}"""\nT = "ghp_AbCdEfGhIjKlMnOpQrStUvWx01"\n')
    assert scan([f]) == []


def test_binary_files_are_skipped(tmp_path: Path) -> None:
    f = tmp_path / "blob.bin"
    f.write_bytes(b"\x00\x01\x02\xff")
    assert scan([f]) == []


def test_missing_paths_are_skipped(tmp_path: Path) -> None:
    assert scan([tmp_path / "gone.txt"]) == []


def test_line_numbers_point_at_the_offending_line(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("fine\nalso fine\nAKIAIOSFODNN7EXAMPLE\n")
    assert scan([f])[0].line == 3


def test_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    clean = tmp_path / "clean.txt"
    clean.write_text("nothing to see\n")
    assert main([str(clean)]) == 0

    dirty = tmp_path / "dirty.txt"
    dirty.write_text("AKIAIOSFODNN7EXAMPLE\n")
    assert main([str(dirty)]) == 1
    assert "look like credentials" in capsys.readouterr().out


def test_the_repo_itself_is_clean() -> None:
    """The rule, applied to this repo, every test run."""
    import subprocess

    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.split()
    findings = scan([root / p for p in tracked])
    assert findings == [], f"secret-shaped content: {findings}"


def test_python_keyword_arguments_are_not_findings(tmp_path: Path) -> None:
    """`api_key=openai_key` is a kwarg. Flagging those makes the scanner
    noisy enough that people stop running it."""
    f = tmp_path / "call.py"
    f.write_text("session = RealtimeSession(api_key=openai_key, token=discord_token)\n")
    assert scan([f]) == []


def test_a_quoted_secret_in_python_is_still_caught(tmp_path: Path) -> None:
    """The exemption is for UNQUOTED right-hand sides only -- a literal in
    Python has to be quoted, so this must still fire."""
    f = tmp_path / "bad.py"
    f.write_text('TOKEN = "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"\n')
    assert [x.line for x in scan([f])] == [1]


def test_the_python_exemption_does_not_apply_to_env_files(tmp_path: Path) -> None:
    """In a .env an unquoted right-hand side IS the value."""
    f = tmp_path / ".env"
    f.write_text("GITHUB_TOKEN=ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789\n")
    assert [x.line for x in scan([f])] == [1]


def test_the_python_exemption_does_not_apply_to_shell(tmp_path: Path) -> None:
    f = tmp_path / "x.sh"
    f.write_text("export API_KEY=abcdefghijklmnopqrstuvwx\n")
    assert scan([f]) != []


def test_python_call_expressions_are_not_findings(tmp_path: Path) -> None:
    """`token = read_secret(root, "discord_bot_token")` names a file, not a
    value."""
    f = tmp_path / "m.py"
    f.write_text('token = read_secret(secrets_root, "discord_bot_token")\n')
    assert scan([f]) == []


def test_a_secret_inside_a_python_call_is_still_caught(tmp_path: Path) -> None:
    """Scanning only the string literals must not mean skipping them."""
    f = tmp_path / "m.py"
    f.write_text('client = Thing(key="ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789")\n')
    assert [x.line for x in scan([f])] == [1]


def test_a_secret_in_a_python_dict_is_still_caught(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text('CONFIG = {"token": "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"}\n')
    assert [x.line for x in scan([f])] == [1]
