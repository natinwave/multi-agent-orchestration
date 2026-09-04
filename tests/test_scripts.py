"""Guards on the two shell entry points.

Their stdout is the entire remote interface -- it gets pasted into chat --
and preflight's promise is that it changes nothing. Both are easy to break
by accident while adding "just one more check".
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PREFLIGHT = (SCRIPTS / "preflight.sh").read_text()
BOOTSTRAP = (SCRIPTS / "bootstrap.sh").read_text()


def code_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


@pytest.mark.parametrize(
    "name", ["preflight.sh", "bootstrap.sh", "root-setup.sh", "lib/common.sh"]
)
def test_scripts_are_syntactically_valid(name: str) -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPTS / name)]).returncode == 0


@pytest.mark.parametrize("name", ["preflight.sh", "bootstrap.sh", "root-setup.sh"])
def test_scripts_are_executable(name: str) -> None:
    assert (SCRIPTS / name).stat().st_mode & 0o111


# --- preflight changes nothing ---------------------------------------------

MUTATING = [
    r"\bmkdir\b",
    r"\brm\b",
    r"\bmv\b",
    r"\bcp\b",
    r"\bchown\b",
    r"\bchmod\b",
    r"\bapt-get\b",
    r"\bnpm (install|i)\b",
    r"\bpip install\b",
    r"\bdocker (build|run|pull|start|stop|rm|create|exec)\b",
    r"\bdocker compose (up|down|build|start|restart|pull)\b",
    r"\bgit (clone|fetch|checkout|worktree|init)\b",
    r">\s*/(?!dev/null)",  # redirecting into a real path
]


def is_message(line: str) -> bool:
    """pass/warn/fail/info lines are advice printed to a person -- telling
    someone to run `npm install` is not the same as running it."""
    return bool(re.match(r"\s*(pass|warn|fail|info)\s", line))


@pytest.mark.parametrize("pattern", MUTATING)
def test_preflight_changes_nothing(pattern: str) -> None:
    """It is the first thing run on a machine nobody is sitting at, and it
    is advertised as safe to run repeatedly."""
    for line in code_lines(PREFLIGHT):
        if is_message(line):
            continue
        assert not re.search(pattern, line), f"preflight mutates state: {line.strip()}"


def test_preflight_does_not_use_errexit() -> None:
    """set -e would stop at the first failing check and hide the rest of
    the report -- the opposite of what a diagnostic should do."""
    assert re.search(r"^set -uo pipefail", PREFLIGHT, re.M)
    assert not re.search(r"^set -e", PREFLIGHT, re.M)


def test_preflight_checks_everything_the_brief_asked_for() -> None:
    for probe in ("docker info", "df ", "nvidia-smi", "gh auth status", "claude -p"):
        assert probe in PREFLIGHT, f"preflight does not check {probe}"


def test_preflight_says_setup_token_cannot_be_done_remotely() -> None:
    """The one failure that no amount of retrying from a chat relay can
    fix, so it has to be stated rather than implied -- and it has to name
    the alternative that needs nobody at a keyboard."""
    assert "setup-token" in PREFLIGHT
    assert re.search(r"(?i)interactive", PREFLIGHT)
    assert re.search(r"(?i)no agent and no chat relay", PREFLIGHT)
    assert "ANTHROPIC_API_KEY" in PREFLIGHT


def test_preflight_emits_one_summary_line() -> None:
    assert PREFLIGHT.count("summarise") == 1


# --- bootstrap converges ---------------------------------------------------


def test_bootstrap_takes_a_lock() -> None:
    """Two concurrent runs would race on the image build and the worktrees.
    mkdir is the lock because it is atomic."""
    assert 'mkdir "$LOCK"' in BOOTSTRAP
    assert "trap" in BOOTSTRAP


def test_bootstrap_creates_directories_idempotently() -> None:
    for line in code_lines(BOOTSTRAP):
        if re.search(r"\bmkdir\b", line) and "$LOCK" not in line:
            assert "-p" in line, f"non-idempotent mkdir: {line.strip()}"


def test_bootstrap_never_resets_a_repository() -> None:
    """A worktree may hold hours of uncommitted agent work; a re-run of
    bootstrap must not be able to destroy it."""
    for forbidden in ("git reset", "git clean", "checkout --force", "-f origin"):
        assert forbidden not in BOOTSTRAP


def test_bootstrap_clones_only_when_absent() -> None:
    assert re.search(r'if \[ -d "\$\{dest\}/\.git" \]', BOOTSTRAP)


def test_bootstrap_writes_detail_to_a_log_not_to_stdout() -> None:
    assert 'LOG="' in BOOTSTRAP
    assert "logged" in BOOTSTRAP


def test_bootstrap_prints_mcp_config_rather_than_writing_it() -> None:
    """Editing someone's client config behind their back is not a thing a
    bootstrap script gets to do."""
    assert "mcpServers" in BOOTSTRAP
    idx = BOOTSTRAP.index("mcpServers")
    context = BOOTSTRAP[idx - 400 : idx]
    assert "cat <<" in context or "echo" in context


def test_bootstrap_emits_one_summary_line() -> None:
    assert BOOTSTRAP.count("summarise") == 1


# --- the permission rule ---------------------------------------------------


def test_dangerous_flag_appears_only_in_the_container_command() -> None:
    """--dangerously-skip-permissions is acceptable inside a container and
    nowhere else. Nothing that runs on the bare host may use it."""
    for path in [*SCRIPTS.rglob("*.sh"), ROOT / "bin" / "orchestrate", ROOT / "bin" / "orchestrate-mcp"]:
        text = path.read_text()
        assert "--dangerously-skip-permissions" not in text, path


def test_host_side_claude_invocations_are_scoped() -> None:
    """The bootstrap/preflight path runs claude on the bare host, so it
    gets acceptEdits and an explicit tool list instead."""
    for match in re.finditer(r"claude -p\b[^\n]*", PREFLIGHT):
        assert "--permission-mode" in PREFLIGHT[match.start() : match.start() + 400]


def test_no_secret_values_in_any_script() -> None:
    from orchestrator.redaction import Redactor

    red = Redactor()
    for path in [*SCRIPTS.rglob("*.sh"), *(ROOT / "bin").iterdir()]:
        text = path.read_text()
        assert red.scrub(text) == text, f"{path} contains something secret-shaped"


def test_secrets_are_written_with_restrictive_permissions() -> None:
    assert "umask 077" in BOOTSTRAP
    assert "chmod 0400" in BOOTSTRAP
    assert "chmod 0700" in BOOTSTRAP


# --- the one script that needs root ---------------------------------------

ROOT_SETUP = (SCRIPTS / "root-setup.sh").read_text()


def test_root_setup_refuses_to_run_unprivileged() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPTS / "root-setup.sh")], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "must run as root" in result.stdout


def test_root_setup_refuses_dangerous_paths() -> None:
    """It runs chown -R as root, so the paths it accepts are worth pinning
    even though they come from our own defaults."""
    for guarded in ("/usr", "/etc", "/home", "/root"):
        assert guarded in ROOT_SETUP


def test_root_setup_will_not_grant_docker_group_silently() -> None:
    """Membership of the docker group is root-equivalent on this machine,
    so it must be asked for, never a side effect of running setup."""
    assert "--add-docker-group" in ROOT_SETUP
    usermod = [ln for ln in ROOT_SETUP.splitlines() if "usermod" in ln and not ln.strip().startswith("#")]
    assert usermod, "expected a usermod call"
    assert "ADD_DOCKER_GROUP" in ROOT_SETUP


def test_root_setup_is_the_only_script_that_needs_root() -> None:
    """Everything else runs as the ordinary user; if that stops being true
    the README's trust story is wrong."""
    for path in SCRIPTS.glob("*.sh"):
        if path.name == "root-setup.sh":
            continue
        assert "must run as root" not in path.read_text(), path


def test_preflight_never_captures_a_resolved_secret_into_a_variable() -> None:
    """preflight resolves the credential reference to prove it is reachable.
    The exit code is the answer; the value must go to /dev/null, never into
    a variable that some later line could print."""
    for line in code_lines(PREFLIGHT):
        if "op read" in line:
            assert ">/dev/null" in line, f"op read must discard its output: {line.strip()}"
            assert "$(" not in line.split("op read")[0][-3:], line


def test_preflight_still_changes_nothing_when_resolving() -> None:
    """`op read` is a read. If this ever becomes `op run` or `op inject`
    writing a file, preflight's read-only promise is broken."""
    for bad in ("op run", "op inject", "op item create", "op vault create"):
        assert bad not in PREFLIGHT
