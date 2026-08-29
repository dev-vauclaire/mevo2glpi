"""Reuse the disposable service stacks in the end-to-end suite."""

from __future__ import annotations

import os
from collections.abc import Iterator
from importlib import import_module

import pytest

# Transcribe AI still owns a subtree fixture. GLPI is registered once through
# the workspace-level lazy plugin, so the end-to-end suite requests it by name.
_transcribe_ai_fixtures = import_module(
    "apps.worker-automation.tests.integration.transcribe-ai.conftest"
)

transcribe_ai_environment = _transcribe_ai_fixtures.transcribe_ai_environment


@pytest.fixture(scope="session", autouse=True)
def _prevent_parallel_e2e_suite() -> Iterator[None]:
    """Reject xdist because the two Compose stacks are session resources."""
    if os.environ.get("PYTEST_XDIST_WORKER") is not None:
        pytest.fail("The end-to-end suite must not run with pytest-xdist")
    yield
