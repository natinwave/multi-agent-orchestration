"""Test isolation.

bootstrap.sh runs the suite with ORCH_ROOT, ORCH_SECRETS and
ORCH_CONFIG_DIR exported, because the supervisor passes them to the
runners it spawns. Those variables take priority over config files, so
without this the tests read the *live* runtime directories instead of
their own tmp_path -- and a test that passes on a developer machine fails
on the host, or worse, passes there for the wrong reason.

That is exactly what happened: a redaction test passed locally and failed
under bootstrap on the target machine, because ORCH_SECRETS pointed it at
/run/orchestration/secrets rather than its own fixture.
"""

from __future__ import annotations

import pytest

from orchestrator.registry import CONFIG_DIR_ENV, ROOT_ENV, SECRETS_ENV

#: Ambient configuration the suite must never inherit.
ORCHESTRATION_ENV = (ROOT_ENV, SECRETS_ENV, CONFIG_DIR_ENV)


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test as if no orchestration environment were set.

    A test that genuinely wants one of these sets it itself.
    """
    for name in ORCHESTRATION_ENV:
        monkeypatch.delenv(name, raising=False)
