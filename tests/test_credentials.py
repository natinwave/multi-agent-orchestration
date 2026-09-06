"""Credential delegation.

The 1Password CLI is injected, so all of this runs without a vault or a
signed-in `op` -- which matters, because the real thing cannot be exercised
anywhere but the Ubuntu box.

Every credential here is fabricated. secret-scan: allow
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orchestrator.credentials import (
    RESERVED,
    AmbiguousCredential,
    CredentialError,
    CredentialStore,
    OpError,
    UnknownCredential,
    slugify,
)

VAULT = [
    {"title": "Staging DB Password"},
    {"title": "AXE RxAPI Key"},
    {"title": "Sentry DSN"},
]

ITEMS = {
    "Staging DB Password": {
        "fields": [
            {"label": "username", "type": "STRING", "value": "app"},
            {"label": "password", "type": "CONCEALED", "value": "staging-pw-value-here"},
        ]
    },
    "AXE RxAPI Key": {
        "fields": [{"label": "credential", "type": "CONCEALED", "value": "axe-key-value-here"}]
    },
    "Sentry DSN": {"fields": [{"label": "notes", "type": "STRING", "value": "not concealed"}]},
}


def fake_op(argv: list[str]) -> str:
    if argv[:2] == ["item", "list"]:
        return json.dumps(VAULT)
    if argv[:2] == ["item", "get"]:
        return json.dumps(ITEMS[argv[2]])
    raise AssertionError(f"unexpected op call: {argv}")


@pytest.fixture
def store(tmp_path: Path) -> CredentialStore:
    return CredentialStore(
        secrets_root=tmp_path / "secrets",
        vault="Agent",
        audit_log=tmp_path / "logs" / "grants.log",
        op_runner=fake_op,
    )


# --- naming ----------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Staging DB Password", "staging_db_password"),
        ("AXE RxAPI Key", "axe_rxapi_key"),
        ("  spaces  everywhere  ", "spaces_everywhere"),
        ("weird!!chars??here", "weird_chars_here"),
        ("2FA Backup", "c_2fa_backup"),  # must start with a letter to be an env var
    ],
)
def test_slugify(title: str, expected: str) -> None:
    assert slugify(title) == expected


def test_env_var_is_the_uppercased_name(store: CredentialStore) -> None:
    assert store.resolve("staging db password").env_var == "STAGING_DB_PASSWORD"


# --- the menu --------------------------------------------------------------


def test_available_returns_titles_only(store: CredentialStore) -> None:
    """This is the one call the voice agent makes constantly. It must not
    be able to leak a value even by accident."""
    items = store.available()
    assert [i.title for i in items] == ["AXE RxAPI Key", "Sentry DSN", "Staging DB Password"]
    blob = json.dumps([{"title": i.title, "name": i.name} for i in items])
    for secret in ("staging-pw-value-here", "axe-key-value-here"):
        assert secret not in blob


# --- colloquial resolution -------------------------------------------------


@pytest.mark.parametrize(
    "spoken,expected",
    [
        ("Staging DB Password", "Staging DB Password"),
        ("staging db password", "Staging DB Password"),
        ("staging_db_password", "Staging DB Password"),
        ("the staging password", "Staging DB Password"),
        ("staging", "Staging DB Password"),
        ("axe key", "AXE RxAPI Key"),
        ("the axe rxapi key", "AXE RxAPI Key"),
        ("sentry", "Sentry DSN"),
    ],
)
def test_spoken_names_resolve(store: CredentialStore, spoken: str, expected: str) -> None:
    assert store.resolve(spoken).title == expected


def test_unknown_credential_lists_the_menu(store: CredentialStore) -> None:
    with pytest.raises(UnknownCredential) as exc:
        store.resolve("the nuclear codes")
    assert "Sentry DSN" in exc.value.known


def test_ambiguity_refuses_rather_than_guessing(tmp_path: Path) -> None:
    """Guessing here hands an agent the wrong live credential."""

    def two(argv):
        if argv[:2] == ["item", "list"]:
            return json.dumps([{"title": "Prod API Key"}, {"title": "Prod API Secret"}])
        raise AssertionError

    store = CredentialStore(secrets_root=tmp_path, op_runner=two)
    with pytest.raises(AmbiguousCredential) as exc:
        store.resolve("prod api")
    assert exc.value.candidates == ["Prod API Key", "Prod API Secret"]


# --- granting --------------------------------------------------------------


def test_grant_writes_the_value_into_the_agents_own_directory(store: CredentialStore) -> None:
    grant = store.grant("claude-code", "staging db password")
    path = store.secrets_root / "claude-code" / "staging_db_password"
    assert path.read_text() == "staging-pw-value-here"
    assert grant.env_var == "STAGING_DB_PASSWORD"


def test_granted_files_are_unreadable_to_anyone_else(store: CredentialStore) -> None:
    store.grant("claude-code", "staging")
    path = store.secrets_root / "claude-code" / "staging_db_password"
    assert oct(path.stat().st_mode)[-3:] == "400"
    assert oct((store.secrets_root / "claude-code").stat().st_mode)[-3:] == "700"


def test_a_grant_is_invisible_to_other_agents(store: CredentialStore) -> None:
    """Per-agent directories are the whole mechanism: hermes's container
    mounts its own directory and cannot see this one."""
    store.grant("claude-code", "staging")
    assert not (store.secrets_root / "hermes").exists()


def test_grant_prefers_the_password_field(store: CredentialStore) -> None:
    """The item has a username too; picking the wrong field would hand the
    agent something useless and look like it worked."""
    store.grant("claude-code", "staging")
    assert (store.secrets_root / "claude-code" / "staging_db_password").read_text() != "app"


def test_grant_falls_back_to_the_only_concealed_field(store: CredentialStore) -> None:
    store.grant("claude-code", "axe key")
    assert (store.secrets_root / "claude-code" / "axe_rxapi_key").read_text() == "axe-key-value-here"


def test_an_item_with_nothing_concealed_is_refused(store: CredentialStore) -> None:
    with pytest.raises(OpError, match="no concealed field"):
        store.grant("claude-code", "sentry")


@pytest.mark.parametrize("reserved", sorted(RESERVED))
def test_a_grant_may_not_overwrite_the_agents_own_identity(tmp_path: Path, reserved: str) -> None:
    """Otherwise "grant the coding agent the oauth token" would replace its
    Claude credential with something else and break it mysteriously."""

    def vault(argv):
        if argv[:2] == ["item", "list"]:
            return json.dumps([{"title": reserved.replace("_", " ")}])
        raise AssertionError

    store = CredentialStore(secrets_root=tmp_path, op_runner=vault)
    with pytest.raises(CredentialError, match="refused"):
        store.grant("claude-code", reserved.replace("_", " "))


def test_grant_is_idempotent(store: CredentialStore) -> None:
    store.grant("claude-code", "staging")
    store.grant("claude-code", "staging")
    assert len(store.grants()) == 1


# --- listing what is live --------------------------------------------------


def test_grants_reflect_the_filesystem(store: CredentialStore) -> None:
    store.grant("claude-code", "staging")
    store.grant("hermes", "axe key")
    live = {(g.agent, g.name) for g in store.grants()}
    assert live == {("claude-code", "staging_db_password"), ("hermes", "axe_rxapi_key")}


def test_grants_never_include_values(store: CredentialStore) -> None:
    store.grant("claude-code", "staging")
    assert "staging-pw-value-here" not in json.dumps([g.__dict__ for g in store.grants()])


def test_the_agents_own_identity_is_not_listed_as_a_grant(store: CredentialStore) -> None:
    """bootstrap writes those; they are not something you delegated."""
    d = store.secrets_root / "claude-code"
    d.mkdir(parents=True)
    (d / "oauth_token").write_text("x")
    assert store.grants() == []


def test_a_file_appearing_by_hand_still_counts_as_live(store: CredentialStore) -> None:
    """The filesystem is the source of truth, not the metadata: a file in
    the directory IS an exposed credential however it got there."""
    d = store.secrets_root / "claude-code"
    d.mkdir(parents=True)
    (d / "planted").write_text("x")
    assert [g.name for g in store.grants()] == ["planted"]


def test_metadata_lives_outside_the_mounted_directories(store: CredentialStore) -> None:
    """A container mounts secrets_root/<its own name>. Metadata sitting in
    there would tell it what else exists."""
    store.grant("claude-code", "staging")
    assert store._meta_path.parent == store.secrets_root
    assert not (store.secrets_root / "claude-code" / ".grants.json").exists()


# --- revoking --------------------------------------------------------------


def test_revoke_removes_the_file(store: CredentialStore) -> None:
    store.grant("claude-code", "staging")
    assert store.revoke("claude-code", "staging_db_password") is True
    assert not (store.secrets_root / "claude-code" / "staging_db_password").exists()
    assert store.grants() == []


def test_revoking_something_never_granted(store: CredentialStore) -> None:
    assert store.revoke("claude-code", "nothing") is False


def test_job_scoped_grants_are_dropped_when_the_job_ends(store: CredentialStore) -> None:
    store.grant("claude-code", "staging", job_id="kestrel")
    store.grant("claude-code", "axe key")  # not scoped: survives
    dropped = store.revoke_for_job("kestrel")
    assert [g.name for g in dropped] == ["staging_db_password"]
    assert [g.name for g in store.grants()] == ["axe_rxapi_key"]


def test_revoking_for_an_unrelated_job_changes_nothing(store: CredentialStore) -> None:
    store.grant("claude-code", "staging", job_id="kestrel")
    assert store.revoke_for_job("otter") == []
    assert len(store.grants()) == 1


# --- audit -----------------------------------------------------------------


def test_grants_and_revokes_are_audited(store: CredentialStore) -> None:
    store.grant("claude-code", "staging", job_id="kestrel")
    store.revoke("claude-code", "staging_db_password")
    lines = store.audit_log.read_text().splitlines()
    assert len(lines) == 2
    assert "grant" in lines[0] and "claude-code" in lines[0] and "kestrel" in lines[0]
    assert "revoke" in lines[1]


def test_the_audit_log_never_contains_a_value(store: CredentialStore) -> None:
    store.grant("claude-code", "staging")
    assert "staging-pw-value-here" not in store.audit_log.read_text()


def test_the_audit_log_is_not_world_readable(store: CredentialStore) -> None:
    store.grant("claude-code", "staging")
    assert oct(store.audit_log.stat().st_mode)[-3:] == "600"


def test_an_unwritable_audit_log_does_not_block_a_revoke(store: CredentialStore) -> None:
    """Losing the audit trail is bad; being unable to withdraw a live
    credential is worse."""
    store.grant("claude-code", "staging")
    store.audit_log.parent.chmod(0o500)
    try:
        assert store.revoke("claude-code", "staging_db_password") is True
    finally:
        store.audit_log.parent.chmod(0o700)


# --- op failures -----------------------------------------------------------


def test_a_missing_op_cli_is_a_clear_message(tmp_path: Path) -> None:
    def boom(argv):
        raise OpError("the 1Password CLI (op) is not installed on this host")

    with pytest.raises(OpError, match="not installed"):
        CredentialStore(secrets_root=tmp_path, op_runner=boom).available()


def test_unparseable_vault_output(tmp_path: Path) -> None:
    store = CredentialStore(secrets_root=tmp_path, op_runner=lambda a: "not json")
    with pytest.raises(OpError, match="parse"):
        store.available()


def test_op_errors_do_not_carry_stdout(tmp_path: Path) -> None:
    """An `op read` that half-succeeded must not put a value into an
    exception message, which would travel straight into a log."""
    import subprocess
    from unittest.mock import patch

    with patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="leaked-value-here", stderr="auth required"
        )
        from orchestrator.credentials import _run_op

        with pytest.raises(OpError) as exc:
            _run_op(["item", "list"])
    assert "leaked-value-here" not in str(exc.value)
    assert "auth required" in str(exc.value)


# --- finding the 1Password token -------------------------------------------


def test_the_environment_wins(tmp_path: Path) -> None:
    from orchestrator.credentials import service_account_token

    (tmp_path / ".op-token").write_text("from-the-file")
    env = {"OP_SERVICE_ACCOUNT_TOKEN": "from-the-environment", "HOME": str(tmp_path)}
    assert service_account_token(env) == "from-the-environment"


def test_a_token_file_is_found_when_the_environment_is_clean(tmp_path: Path) -> None:
    """The case that broke credential sharing: a systemd service starts
    with a clean environment, so only bootstrap.sh knew where the token
    lived and the voice agent reported it could not find the vault."""
    from orchestrator.credentials import service_account_token

    (tmp_path / ".op-token").write_text("token-from-home\n")
    assert service_account_token({"HOME": str(tmp_path)}) == "token-from-home"


def test_an_explicit_token_file_is_preferred(tmp_path: Path) -> None:
    from orchestrator.credentials import service_account_token

    (tmp_path / ".op-token").write_text("from-home")
    explicit = tmp_path / "elsewhere"
    explicit.write_text("from-op-token-file")
    env = {"OP_TOKEN_FILE": str(explicit), "HOME": str(tmp_path)}
    assert service_account_token(env) == "from-op-token-file"


def test_surrounding_whitespace_is_stripped(tmp_path: Path) -> None:
    """A file written by echo has a trailing newline, and a token with one
    is not a token."""
    from orchestrator.credentials import service_account_token

    (tmp_path / ".op-token").write_text("  token-with-space  \n")
    assert service_account_token({"HOME": str(tmp_path)}) == "token-with-space"


def test_an_empty_token_file_is_ignored(tmp_path: Path) -> None:
    from orchestrator.credentials import service_account_token

    (tmp_path / ".op-token").write_text("\n")
    assert service_account_token({"HOME": str(tmp_path)}) is None


def test_the_search_order_matches_bootstrap(tmp_path: Path) -> None:
    """One answer to "where does the token live", or the shell and the
    Python disagree and only one of them works."""
    from orchestrator.credentials import token_paths

    bootstrap = (Path(__file__).resolve().parents[1] / "scripts" / "bootstrap.sh").read_text()
    assert "OP_TOKEN_FILE" in bootstrap
    assert "${HOME}/.op-token" in bootstrap

    paths = [str(p) for p in token_paths({"OP_TOKEN_FILE": "/x/tok", "HOME": "/home/agent"})]
    assert paths[0] == "/x/tok"
    assert paths[1] == "/home/agent/.op-token"


def test_a_missing_token_produces_an_actionable_error(monkeypatch) -> None:
    """"not signed in" from a background service is misleading advice: the
    problem is almost always that the token was never found."""
    import subprocess

    from orchestrator.credentials import OpError, _run_op

    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.setattr(
        "orchestrator.credentials.service_account_token", lambda *a, **k: None
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="You are not currently signed in."
        ),
    )
    with pytest.raises(OpError) as exc:
        _run_op(["item", "list"])
    assert "no service account token was found" in str(exc.value)
    assert ".op-token" in str(exc.value)
