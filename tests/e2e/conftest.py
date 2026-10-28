"""Compose the disposable MVP with the real provider integration stacks."""

from __future__ import annotations

import os
from collections.abc import Iterator
from importlib import import_module
from pathlib import Path

import pytest
from tests.support.mvp_compose import (
    MvpComposeEnvironment,
    ProviderConfiguration,
    create_mvp_compose_environment,
)

E2E_DIRECTORY = Path(__file__).resolve().parent
E2E_COMPOSE_FILE = E2E_DIRECTORY / "compose.override.yaml"

# Transcribe AI owns a subtree fixture while GLPI is registered by the root plugin.
_transcribe_ai_fixtures = import_module(
    "apps.worker-automation.tests.integration.transcribe-ai.conftest"
)
transcribe_ai_environment = _transcribe_ai_fixtures.transcribe_ai_environment


@pytest.fixture(scope="session", autouse=True)
def _prevent_parallel_e2e_suite() -> Iterator[None]:
    """Reject xdist because all provider stacks and the MVP are session resources."""
    if os.environ.get("PYTEST_XDIST_WORKER") is not None:
        pytest.fail("The end-to-end suite must not run with pytest-xdist")
    yield


@pytest.fixture(scope="session")
def e2e_environment(
    tmp_path_factory: pytest.TempPathFactory,
    glpi_environment,
    transcribe_ai_environment,
) -> Iterator[MvpComposeEnvironment]:
    """Start the applications with test-only PostgreSQL and provider networks."""
    credentials = glpi_environment.credentials
    providers = ProviderConfiguration(
        transcription_base_url=transcribe_ai_environment.internal_base_url,
        transcription_api_key=(transcribe_ai_environment.api_key.get_secret_value()),
        glpi_base_url=glpi_environment.internal_base_url,
        glpi_username=credentials.username,
        glpi_password=credentials.password,
        glpi_client_id=credentials.client_id,
        glpi_client_secret=credentials.client_secret,
        glpi_request_source_id=glpi_environment.request_source.id,
    )
    runtime_root = tmp_path_factory.mktemp("gta-mvp-e2e")
    environment = create_mvp_compose_environment(
        runtime_root,
        providers=providers,
        project_prefix="gtamvpe2e",
        extra_compose_files=(E2E_COMPOSE_FILE,),
        extra_compose_environment={
            "GTA_E2E_GLPI_NETWORK": glpi_environment.network_name,
            "GTA_E2E_TRANSCRIBE_AI_NETWORK": (transcribe_ai_environment.network_name),
        },
        # Avoid the `postgres` DNS alias owned by the Transcribe AI network.
        database_host="mvp-postgres",
    )
    primary_error: BaseException | None = None
    try:
        environment.build()
        environment.start()
        yield environment
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            environment.destroy()
        except Exception:
            if primary_error is None:
                raise
            primary_error.add_note(
                "MVP E2E cleanup also failed; the primary error was preserved"
            )
