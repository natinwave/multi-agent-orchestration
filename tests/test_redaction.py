"""The redaction filter is the last thing between agent output and a cloud
model's context, so it gets the most tests: it must catch real credentials,
must not eat the ordinary high-entropy strings agents emit constantly (git
SHAs, UUIDs, paths), and must be safe to apply twice.

Every credential in here is fabricated and exists to prove it gets scrubbed.
secret-scan: allow
"""

from pathlib import Path

import pytest

from orchestrator.redaction import Redactor, shannon_entropy


@pytest.fixture
def red() -> Redactor:
    return Redactor()


# --------------------------------------------------------------------------
# Layer 1: registered literal values
# --------------------------------------------------------------------------


def test_registered_value_is_replaced_by_name(red: Redactor) -> None:
    red.register_value("CLAUDE_CODE_OAUTH_TOKEN", "hunter2-hunter2-hunter2")
    out = red.scrub("exporting hunter2-hunter2-hunter2 into the child")
    assert "hunter2" not in out
    assert "[redacted:CLAUDE_CODE_OAUTH_TOKEN]" in out


def test_registered_value_found_mid_token(red: Redactor) -> None:
    """No word boundary assumption -- secrets get concatenated into URLs."""
    red.register_value("TOK", "s3cret-value-here")
    assert "s3cret" not in red.scrub("https://x/api?key=s3cret-value-here&z=1")


def test_registered_value_caught_when_base64_encoded(red: Redactor) -> None:
    red.register_value("TOK", "correct-horse-battery-staple")
    encoded = "Y29ycmVjdC1ob3JzZS1iYXR0ZXJ5LXN0YXBsZQ"
    assert "Y29ycmVjdC1o" not in red.scrub(f"payload={encoded}")


def test_registered_value_caught_when_url_encoded(red: Redactor) -> None:
    red.register_value("TOK", "a/secret+with/slashes")
    assert "a%2Fsecret" not in red.scrub("u=a%2Fsecret%2Bwith%2Fslashes")


def test_short_values_are_refused(red: Redactor) -> None:
    """Registering 'test' would redact half the log."""
    assert red.register_value("SHORT", "test") is False
    assert red.scrub("running the test suite") == "running the test suite"


def test_empty_and_none_values_refused(red: Redactor) -> None:
    assert red.register_value("A", None) is False
    assert red.register_value("B", "") is False


def test_overlapping_values_longest_wins(red: Redactor) -> None:
    red.register_value("SHORTER", "abcdefghij")
    red.register_value("LONGER", "abcdefghijklmnop")
    out = red.scrub("value abcdefghijklmnop here")
    assert "[redacted:LONGER]" in out
    assert "abcdefghij" not in out


def test_register_env(red: Redactor) -> None:
    names = red.register_env(["A_TOKEN", "MISSING"], {"A_TOKEN": "value-long-enough"})
    assert names == ["A_TOKEN"]
    assert "value-long-enough" not in red.scrub("leak: value-long-enough")


def test_register_file_and_dir(red: Redactor, tmp_path: Path) -> None:
    d = tmp_path / "claude-code"
    d.mkdir()
    (d / "oauth_token").write_text("sk-ant-oat01-registered-from-file\n")
    assert red.register_dir(d) == ["oauth_token"]
    # Trailing newline must not defeat the match.
    assert "registered-from-file" not in red.scrub("tok=sk-ant-oat01-registered-from-file")


def test_register_dir_missing_is_not_an_error(red: Redactor, tmp_path: Path) -> None:
    assert red.register_dir(tmp_path / "nope") == []


# --------------------------------------------------------------------------
# Layer 2: known credential shapes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "sk-ant-oat01-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "gho_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "github_pat_11ABCDEFG0abcdefghijkl_AbCdEfGhIjKlMnOpQrStUv",
        "AKIAIOSFODNN7EXAMPLE",
        "AIzaSyA1234567890abcdefghijklmnopqrstuvw",
        # These two are assembled rather than written out. They are
        # fabricated, but GitHub's push protection cannot tell a fixture from
        # a live credential and blocks the push; the regex under test is
        # handed the identical string either way.
        "xoxb-" + "123456789012-1234567890123-" + "AbCdEfGhIjKlMnOpQrStUvWx",
        "sk_" + "live_" + "AbCdEfGhIjKlMnOpQrStUvWx",
        "npm_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    ],
)
def test_known_credential_shapes_are_caught(red: Redactor, secret: str) -> None:
    out = red.scrub(f"the value is {secret} ok")
    assert secret not in out
    assert "[redacted" in out


def test_private_key_block_is_caught_whole(red: Redactor) -> None:
    pem = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n"
        "AAAAMwAAAAtzc2gtZWQyNTUxOQAAACBaBcDeFgHiJkLmNoPqRsTu\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    out = red.scrub(f"cat id_ed25519\n{pem}\ndone")
    assert "b3BlbnNzaC1rZXktdjEA" not in out
    assert out.startswith("cat id_ed25519")
    assert out.endswith("done")


def test_authorization_header(red: Redactor) -> None:
    out = red.scrub("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345")
    assert "abcdefghijklmnop" not in out


def test_bearer_keeps_surrounding_context(red: Redactor) -> None:
    out = red.scrub('curl -H "bearer abcdefghijklmnopqrstuvwxyz" https://api.example.com')
    assert "abcdefghijklmnop" not in out
    assert "https://api.example.com" in out


def test_url_inline_credentials(red: Redactor) -> None:
    out = red.scrub("remote https://someone:p4ssw0rd-in-url@github.com/o/r.git")
    assert "p4ssw0rd-in-url" not in out
    assert "github.com/o/r.git" in out


@pytest.mark.parametrize(
    "line",
    [
        "API_KEY=abcdefghijklmnop",
        "api-key: abcdefghijklmnop",
        'password = "abcdefghijklmnop"',
        "client_secret: abcdefghijklmnop",
        "ACCESS_TOKEN=abcdefghijklmnop",
        "credentials=abcdefghijklmnop",
    ],
)
def test_assignment_shapes(red: Redactor, line: str) -> None:
    assert "abcdefghijklmnop" not in red.scrub(line)


def test_assignment_keeps_the_key_name_visible(red: Redactor) -> None:
    """The narration stays readable -- you learn *which* secret leaked."""
    out = red.scrub("API_KEY=abcdefghijklmnop")
    assert out.startswith("API_KEY=")


# --------------------------------------------------------------------------
# Layer 3: entropy fallback, and the false positives it must not create
# --------------------------------------------------------------------------


def test_unknown_credential_shape_still_caught(red: Redactor) -> None:
    """A provider we have no rule for. This is the fail-closed case."""
    out = red.scrub("token qX7vR2mZ9pL4wK8jH3nB6tY1sD5fG0aC2eU4iO7rP9xQ")
    assert "qX7vR2mZ9pL4" not in out


@pytest.mark.parametrize(
    "text",
    [
        "commit 9f2b1c4e8a7d3f6b0c5e9a1d4f7b2c8e6a3d0f5b",  # sha1
        "blob e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",  # sha256
        "id 3f7c1e2a-9b4d-4c8e-a1f6-2d5b8c0e4a91",  # uuid
        "at 2026-09-04T11:22:33.123456+00:00",
        "wrote /srv/orchestration/worktrees/kestrel/src/orchestrator/supervisor.py",
        "module orchestrator.backends.container",
        "bumped to v1.24.3-rc.1",
        "ran test_supervisor_returns_scrubbed_narration_lines",
        "branch feature/add-redaction-entropy-fallback",
        "package @anthropic-ai/claude-code@2.1.260",
        "counted 123456789012345678901234567890123456",
        "supercalifragilisticexpialidocious antidisestablishmentarianism",
    ],
)
def test_ordinary_agent_output_survives(red: Redactor, text: str) -> None:
    """Agents emit these constantly. Redacting them would make narration
    useless and would train the voice model to ignore [redacted]."""
    assert red.scrub(text) == text


def test_prose_is_untouched(red: Redactor) -> None:
    line = "Ran the test suite, 42 passed, opening a pull request against main."
    assert red.scrub(line) == line


def test_shannon_entropy_ranks_random_above_repetitive() -> None:
    assert shannon_entropy("aaaaaaaaaaaaaaaa") < 1.0
    assert shannon_entropy("qX7vR2mZ9pL4wK8jH3nB6tY1sD5fG0aC") > 4.5
    assert shannon_entropy("") == 0.0


def test_entropy_fallback_can_be_disabled() -> None:
    """Escape hatch for a caller that would rather tune patterns."""
    r = Redactor(entropy_fallback=False)
    text = "qX7vR2mZ9pL4wK8jH3nB6tY1sD5fG0aC2eU4iO7rP9xQ"
    assert r.scrub(text) == text


# --------------------------------------------------------------------------
# Structural guarantees
# --------------------------------------------------------------------------


def test_scrub_is_idempotent(red: Redactor) -> None:
    red.register_value("TOK", "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv")
    text = "tok sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv and ghp_AbCdEfGhIjKlMnOpQrStUvWxYz01"
    once = red.scrub(text)
    assert red.scrub(once) == once


def test_scrub_obj_walks_nested_structures(red: Redactor) -> None:
    red.register_value("TOK", "sk-ant-secret-value-here")
    obj = {
        "state": "running",
        "narration": ["started", "used sk-ant-secret-value-here"],
        "nested": {"env": {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-secret-value-here"}},
        "exit_code": 0,
        "ok": True,
        "nothing": None,
    }
    out = red.scrub_obj(obj)
    assert "sk-ant-secret-value-here" not in repr(out)
    # Non-string leaves must survive with their types intact.
    assert out["exit_code"] == 0
    assert out["ok"] is True
    assert out["nothing"] is None


def test_scrub_obj_scrubs_dictionary_keys(red: Redactor) -> None:
    red.register_value("TOK", "sk-ant-secret-value-here")
    out = red.scrub_obj({"sk-ant-secret-value-here": "value"})
    assert "sk-ant-secret-value-here" not in repr(out)


def test_scrub_obj_preserves_tuple_type(red: Redactor) -> None:
    assert isinstance(red.scrub_obj(("a", "b")), tuple)


def test_empty_input(red: Redactor) -> None:
    assert red.scrub("") == ""


def test_multiline_log_tail(red: Redactor) -> None:
    red.register_value("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-abcdefghijklmnop")
    log = "\n".join(
        [
            "+ docker exec orch-claude-code claude -p",
            "env: CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-abcdefghijklmnop",
            "commit 9f2b1c4e8a7d3f6b0c5e9a1d4f7b2c8e6a3d0f5b",
            "done",
        ]
    )
    out = red.scrub(log)
    assert "sk-ant-oat01" not in out
    assert "9f2b1c4e8a7d3f6b0c5e9a1d4f7b2c8e6a3d0f5b" in out  # SHA survives
    assert out.count("\n") == 3  # line structure preserved


# --------------------------------------------------------------------------
# Documented limits -- these are known and accepted, not bugs
# --------------------------------------------------------------------------


def test_known_limit_secret_split_across_lines_is_not_matched(red: Redactor) -> None:
    """A secret broken by a newline mid-value escapes layer 1. Accepted:
    matching across arbitrary wrapping would produce constant false
    positives. Layers 2 and 3 still see each fragment."""
    red.register_value("TOK", "sk-ant-averylongsecretvalue")
    out = red.scrub("sk-ant-avery\nlongsecretvalue")
    assert "[redacted:TOK]" not in out


def test_known_limit_single_case_letter_runs_are_allowlisted(red: Redactor) -> None:
    """A 32+ char run of one-case letters with no digits is treated as a word
    or identifier, not a credential. This is the price of not redacting long
    English words and test names. Layers 1 and 2 still cover it: any such
    secret we actually know is registered, and any that appears in a
    `token = ...` assignment is caught by pattern."""
    weird = "abcdefghijklmnopqrstuvwxyzabcdefgh"
    assert red.scrub(weird) == weird
    # ...but the moment it is registered or labelled, it is caught.
    assert weird not in red.scrub(f"api_key={weird}")


def test_path_allowlist_does_not_swallow_base64(red: Redactor) -> None:
    """Base64 uses '/', so the path allowlist has to be narrow enough that a
    blob with padding or '+' cannot pass itself off as a directory tree."""
    blob = "qX7v/R2mZ9pL4wK8+jH3nB6tY1sD5fG0aC2eU4iO7rP9xQ="
    assert blob not in red.scrub(f"payload {blob}")
