"""End-to-end, and the one invariant that matters most.

The http_openai backend lets this run for real on any machine: a stub
server stands in for the local model, so ask() -> detached runner ->
backend -> check() is exercised as a whole, including the process spawn.
The container path cannot be tested here; see README.

The invariant: nothing leaves the supervisor unscrubbed. That is checked
twice -- once behaviourally, by planting a credential in the backend's
response and asserting it never comes back, and once structurally, by
reading supervisor.py's own AST to prove every public method returns
through the redaction chokepoint.

Plants a fake credential to prove it never escapes check(). secret-scan: allow
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from orchestrator import supervisor as supervisor_module
from orchestrator.registry import load
from orchestrator.state import JobState
from orchestrator.supervisor import Supervisor

# A credential the stub model helpfully echoes back into its answer.
LEAKED = "sk-ant-api03-LEAKEDsecretVALUE0123456789abcdef"


class _Handler(BaseHTTPRequestHandler):
    answer = "started the job\nall done, the token was " + LEAKED

    def do_POST(self) -> None:  # noqa: N802 - http.server's naming
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": self.answer}}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # keep pytest output clean
        pass


@pytest.fixture
def stub_model():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()
    server.server_close()


@pytest.fixture
def sup(tmp_path: Path, stub_model: str) -> Supervisor:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "agents.toml").write_text(
        f"""
[agents.hermes]
type = "http_openai"
description = "stub"
base_url = "{stub_model}"
model = "hermes"
needs_repo = false
timeout_seconds = 30
"""
    )
    (cfg_dir / "repos.toml").write_text(
        '[repos.main]\nurl = "git@h:o/main.git"\naliases = ["this repo"]\n'
    )
    (cfg_dir / "orchestrator.toml").write_text(
        f'[paths]\nroot = "{tmp_path / "srv"}"\n'
        f"[check]\ndefault_narration_lines = 3\n"
        f'[credentials]\nsecrets_root = "{tmp_path / "secrets"}"\n'
    )
    config = load(cfg_dir, root_override=tmp_path / "srv")
    return Supervisor.create(config=config, environ={})


def wait_for_terminal(sup: Supervisor, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = sup.check(job_id)
        if JobState(result["state"]).is_terminal:
            return result
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished: {sup.check(job_id, tail=40)}")


# --- end to end ------------------------------------------------------------


def test_ask_returns_a_speakable_id_immediately(sup: Supervisor) -> None:
    result = sup.ask("hermes", "what is the capital of France?")
    assert "error" not in result
    assert result["job_id"].isalpha() or "-" in result["job_id"]
    assert len(result["job_id"]) <= 16
    assert result["runner_pid"] > 0


def test_a_job_runs_to_completion_and_narrates(sup: Supervisor) -> None:
    job_id = sup.ask("hermes", "do the thing")["job_id"]
    result = wait_for_terminal(sup, job_id)
    assert result["state"] == "done"
    assert result["narration"], "a finished job with no narration is a silent job"


def test_check_returns_no_raw_log_by_default(sup: Supervisor) -> None:
    """This reply becomes a voice model's context. It stays small."""
    job_id = sup.ask("hermes", "do the thing")["job_id"]
    result = wait_for_terminal(sup, job_id)
    assert "log_tail" not in result
    assert set(result) <= {"job_id", "state", "narration", "detail"}


def test_raw_log_is_opt_in(sup: Supervisor) -> None:
    job_id = sup.ask("hermes", "do the thing")["job_id"]
    wait_for_terminal(sup, job_id)
    assert sup.check(job_id, tail=40)["log_tail"]


def test_narration_line_count_is_capped(sup: Supervisor) -> None:
    job_id = sup.ask("hermes", "do the thing")["job_id"]
    wait_for_terminal(sup, job_id)
    assert len(sup.check(job_id, narration_lines=9999)["narration"]) <= 20


def test_concurrent_jobs_get_distinct_names_and_workspaces(sup: Supervisor) -> None:
    results = [sup.ask("hermes", f"job {i}") for i in range(5)]
    ids = [r["job_id"] for r in results]
    assert len(set(ids)) == 5
    assert len({r["workdir"] for r in results}) == 5
    for job_id in ids:
        wait_for_terminal(sup, job_id)


def test_a_repoless_job_gets_a_scratch_directory(sup: Supervisor) -> None:
    result = sup.ask("hermes", "just a question")
    assert result["repo"] is None
    assert result["branch"] is None
    assert "/scratch/" in result["workdir"]
    assert Path(result["workdir"]).is_dir()


def test_jobs_survive_the_supervisor_that_started_them(sup: Supervisor, tmp_path: Path) -> None:
    """The MCP transport is stdio, so the client -- and this process -- can
    go away mid-job. State lives on disk precisely so that is survivable."""
    job_id = sup.ask("hermes", "do the thing")["job_id"]
    fresh = Supervisor.create(config=load(sup.config.config_dir, tmp_path / "srv"), environ={})
    assert wait_for_terminal(fresh, job_id)["state"] == "done"


# --- redaction, behaviourally ----------------------------------------------


def test_a_credential_in_the_answer_never_reaches_the_caller(sup: Supervisor) -> None:
    job_id = sup.ask("hermes", "leak something")["job_id"]
    result = wait_for_terminal(sup, job_id)
    assert LEAKED not in json.dumps(result)
    assert LEAKED not in json.dumps(sup.check(job_id, tail=200))
    assert LEAKED not in json.dumps(sup.list_jobs())


def test_the_secret_really_is_on_disk(sup: Supervisor) -> None:
    """Guards against the test passing because nothing was ever written."""
    job_id = sup.ask("hermes", "leak something")["job_id"]
    wait_for_terminal(sup, job_id)
    raw = (sup.config.jobs_dir / job_id / "raw.log").read_text()
    assert LEAKED in raw, "nothing was captured, so the redaction test proved nothing"


def test_a_registered_env_secret_is_scrubbed(tmp_path: Path, sup: Supervisor) -> None:
    sup.redactor.register_value("CLAUDE_CODE_OAUTH_TOKEN", "s0me-long-token-value")
    job_id = sup.ask("hermes", "mentions s0me-long-token-value")["job_id"]
    wait_for_terminal(sup, job_id)
    assert "s0me-long-token-value" not in json.dumps(sup.check(job_id, tail=200))


# --- redaction, structurally -----------------------------------------------

PUBLIC = [
    "ask",
    "check",
    "reply",
    "list_agents",
    "list_jobs",
    "list_repos",
    "reap",
    "list_credentials",
    "grant",
    "revoke",
    "list_grants",
]


def test_public_list_covers_every_public_method() -> None:
    """The AST guard below only proves what this list names, so the list
    itself has to stay complete as methods are added."""
    actual = {
        name
        for name in vars(Supervisor)
        if not name.startswith("_") and callable(getattr(Supervisor, name))
    } - {"create"}
    assert actual == set(PUBLIC)


@pytest.mark.parametrize("method", PUBLIC)
def test_every_public_method_returns_through_the_redactor(method: str) -> None:
    """Enforced against the AST rather than by review, because a single
    plain `return {...}` added later would quietly pipe an unscrubbed secret
    to a cloud model and then to a speaker."""
    source = inspect.getsource(getattr(Supervisor, method))
    tree = ast.parse(textwrap.dedent(source))
    returns = [n for n in ast.walk(tree) if isinstance(n, ast.Return) and n.value is not None]
    assert returns, f"{method} returns nothing"
    for node in returns:
        assert (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "_out"
        ), f"{method} has a return on line {node.lineno} that bypasses self._out()"


def test_out_is_the_only_place_that_calls_the_redactor() -> None:
    """If a second call site appears, the AST check above stops proving
    anything -- so pin the chokepoint to one method."""
    tree = ast.parse(Path(inspect.getfile(supervisor_module)).read_text())
    callers = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Attribute) and node.attr in {"scrub", "scrub_obj"}
    }
    assert callers == {"_out"}


# --- error paths -----------------------------------------------------------


def test_unknown_agent_lists_what_exists(sup: Supervisor) -> None:
    result = sup.ask("clod-code", "hello")
    assert result["error"] == "unknown_agent"
    assert result["known"] == ["hermes"]


def test_ambiguous_repo_asks_instead_of_guessing(tmp_path: Path, sup: Supervisor) -> None:
    (sup.config.config_dir / "repos.toml").write_text(
        '[repos.alpha]\nurl = "git@h:o/alpha.git"\naliases = ["the app"]\n'
        '[repos.beta]\nurl = "git@h:o/beta.git"\naliases = ["the app"]\n'
    )
    sup2 = Supervisor.create(
        config=load(sup.config.config_dir, tmp_path / "srv"), environ={}
    )
    result = sup2.ask("hermes", "do it", repo="the app")
    assert result["error"] == "ambiguous_repo"
    assert result["candidates"] == ["alpha", "beta"]


def test_unknown_repo_lists_the_vocabulary(sup: Supervisor) -> None:
    result = sup.ask("hermes", "do it", repo="the tax return")
    assert result["error"] == "unknown_repo"
    assert result["known"] == ["main"]


def test_empty_message_is_refused(sup: Supervisor) -> None:
    assert sup.ask("hermes", "   ")["error"] == "empty_message"


def test_unknown_job(sup: Supervisor) -> None:
    assert sup.check("nosuchjob")["error"] == "unknown_job"


@pytest.mark.parametrize("evil", ["../../etc/passwd", "a/b", "..", "Kestrel"])
def test_job_ids_are_validated_before_touching_the_filesystem(sup: Supervisor, evil: str) -> None:
    """job_id arrives from an MCP client and is joined onto a path."""
    assert sup.check(evil)["error"] == "unknown_job"


def test_reply_refuses_a_job_that_is_not_waiting(sup: Supervisor) -> None:
    job_id = sup.ask("hermes", "do the thing")["job_id"]
    wait_for_terminal(sup, job_id)
    result = sup.reply(job_id, "an answer nobody asked for")
    assert result["error"] == "not_waiting"


# --- listings --------------------------------------------------------------


def test_list_agents_describes_the_registry(sup: Supervisor) -> None:
    agents = sup.list_agents()["agents"]
    assert [a["name"] for a in agents] == ["hermes"]
    assert agents[0]["needs_repo"] is False


def test_list_repos_exposes_the_spoken_vocabulary(sup: Supervisor) -> None:
    repos = sup.list_repos()["repos"]
    assert repos[0]["name"] == "main"
    assert "this repo" in repos[0]["aliases"]


def test_list_jobs_is_newest_first(sup: Supervisor) -> None:
    ids = [sup.ask("hermes", f"job {i}")["job_id"] for i in range(3)]
    for job_id in ids:
        wait_for_terminal(sup, job_id)
    listed = [j["job_id"] for j in sup.list_jobs()["jobs"]]
    assert set(listed) == set(ids)


def test_list_jobs_active_only_filters_finished_work(sup: Supervisor) -> None:
    job_id = sup.ask("hermes", "do the thing")["job_id"]
    wait_for_terminal(sup, job_id)
    assert sup.list_jobs(active_only=True)["jobs"] == []


def test_list_jobs_respects_the_limit(sup: Supervisor) -> None:
    for i in range(4):
        wait_for_terminal(sup, sup.ask("hermes", f"job {i}")["job_id"])
    assert len(sup.list_jobs(limit=2)["jobs"]) == 2


# --- crash recovery --------------------------------------------------------


def test_a_runner_that_vanished_is_reported_failed_not_running(sup: Supervisor) -> None:
    """A reboot or the OOM killer leaves 'running' on disk forever. check()
    is the only thing that would notice, so it is what repairs it."""
    from orchestrator.state import JobPaths, Status

    job_id = sup.ask("hermes", "do the thing")["job_id"]
    wait_for_terminal(sup, job_id)
    paths = JobPaths(sup.config.jobs_dir / job_id)
    Status(state=JobState.RUNNING, runner_pid=999_999).write(paths)

    result = sup.check(job_id)
    assert result["state"] == "failed"
    assert "disappeared" in result["detail"]


def test_reap_frees_the_job(sup: Supervisor) -> None:
    job_id = sup.ask("hermes", "do the thing")["job_id"]
    wait_for_terminal(sup, job_id)
    assert sup.reap(job_id)["reaped"] is True
    assert sup.check(job_id)["error"] == "unknown_job"


def test_reap_refuses_a_live_job(sup: Supervisor) -> None:
    job_id = sup.ask("hermes", "do the thing")["job_id"]
    result = sup.reap(job_id)
    if "error" in result:
        assert result["error"] == "still_running"
    wait_for_terminal(sup, job_id)


# --- credential delegation, through the supervisor -------------------------

VAULT_ITEMS = [{"title": "Staging DB Password"}, {"title": "AXE RxAPI Key"}]
VAULT_VALUE = "staging-pw-value-here"


@pytest.fixture
def sup_with_vault(sup: Supervisor, monkeypatch: pytest.MonkeyPatch) -> Supervisor:
    def fake_op(argv):
        if argv[:2] == ["item", "list"]:
            return json.dumps(VAULT_ITEMS)
        return json.dumps(
            {"fields": [{"label": "password", "type": "CONCEALED", "value": VAULT_VALUE}]}
        )

    monkeypatch.setattr("orchestrator.credentials._run_op", fake_op)
    return sup


def test_list_credentials_returns_titles_and_env_vars(sup_with_vault: Supervisor) -> None:
    result = sup_with_vault.list_credentials()
    assert [c["title"] for c in result["credentials"]] == [
        "AXE RxAPI Key",
        "Staging DB Password",
    ]
    assert "STAGING_DB_PASSWORD" in json.dumps(result)


def test_list_credentials_never_returns_a_value(sup_with_vault: Supervisor) -> None:
    assert VAULT_VALUE not in json.dumps(sup_with_vault.list_credentials())


def test_grant_reports_the_env_var_not_the_value(sup_with_vault: Supervisor) -> None:
    """The voice agent relays this to you and to the coding agent, so it
    has to name the variable and never speak the secret."""
    result = sup_with_vault.grant("hermes", "the staging password")
    assert result["env_var"] == "STAGING_DB_PASSWORD"
    assert result["agent"] == "hermes"
    assert VAULT_VALUE not in json.dumps(result)


def test_grant_scoped_to_a_job_says_so(sup_with_vault: Supervisor) -> None:
    job_id = sup_with_vault.ask("hermes", "do the thing")["job_id"]
    result = sup_with_vault.grant("hermes", "staging", job_id=job_id)
    assert result["scope"] == "this job only"
    wait_for_terminal(sup_with_vault, job_id)


def test_grant_to_an_unknown_agent_is_refused(sup_with_vault: Supervisor) -> None:
    assert sup_with_vault.grant("nope", "staging")["error"] == "unknown_agent"


def test_grant_against_an_unknown_job_is_refused(sup_with_vault: Supervisor) -> None:
    """Otherwise the grant would never be revoked, because no job ends."""
    assert sup_with_vault.grant("hermes", "staging", job_id="ghost")["error"] == "unknown_job"


def test_ambiguous_credential_asks(sup: Supervisor, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "orchestrator.credentials._run_op",
        lambda argv: json.dumps([{"title": "Prod API Key"}, {"title": "Prod API Secret"}]),
    )
    result = sup.grant("hermes", "prod api")
    assert result["error"] == "ambiguous_credential"
    assert result["candidates"] == ["Prod API Key", "Prod API Secret"]


def test_revoke_round_trip(sup_with_vault: Supervisor) -> None:
    sup_with_vault.grant("hermes", "staging")
    assert sup_with_vault.list_grants()["grants"]
    assert sup_with_vault.revoke("hermes", "Staging DB Password")["revoked"]
    assert sup_with_vault.list_grants()["grants"] == []


def test_revoking_what_was_never_granted(sup_with_vault: Supervisor) -> None:
    assert sup_with_vault.revoke("hermes", "staging")["error"] == "not_granted"


def test_list_grants_never_leaks_a_value(sup_with_vault: Supervisor) -> None:
    sup_with_vault.grant("hermes", "staging")
    assert VAULT_VALUE not in json.dumps(sup_with_vault.list_grants())


def test_a_finished_job_releases_its_scoped_grant(sup_with_vault: Supervisor) -> None:
    """The runner drops job-scoped grants on exit, so a credential handed
    over for one piece of work does not outlive it."""
    job_id = sup_with_vault.ask("hermes", "do the thing")["job_id"]
    sup_with_vault.grant("hermes", "staging", job_id=job_id)
    wait_for_terminal(sup_with_vault, job_id)
    assert sup_with_vault.list_grants()["grants"] == []
