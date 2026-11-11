"""Compose the disposable MVP with its provider integration fixtures."""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.e2e.support.parakeet import (
    ParakeetComposeEnvironment,
    create_parakeet_compose_environment,
    load_synthetic_speech,
)
from tests.support.mvp_compose import (
    MvpComposeEnvironment,
    ProviderConfiguration,
    create_mvp_compose_environment,
)

E2E_DIRECTORY = Path(__file__).resolve().parent
E2E_COMPOSE_FILE = E2E_DIRECTORY / "compose.override.yaml"
PARAKEET_E2E_COMPOSE_FILE = E2E_DIRECTORY / "compose.parakeet.override.yaml"


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
) -> Iterator[MvpComposeEnvironment]:
    """Start the applications with PostgreSQL and the OpenAI contract double."""
    credentials = glpi_environment.credentials
    transcription_api_key = secrets.token_urlsafe(32)
    providers = ProviderConfiguration(
        transcription_base_url="http://openai-transcription:8080/v1",
        transcription_api_key=transcription_api_key,
        glpi_base_url=glpi_environment.internal_base_url,
        glpi_username=credentials.username,
        glpi_password=credentials.password,
        glpi_client_id=credentials.client_id,
        glpi_client_secret=credentials.client_secret,
        glpi_request_source_id=glpi_environment.request_source.id,
    )
    runtime_root = tmp_path_factory.mktemp("mevo2glpi-e2e")
    environment = create_mvp_compose_environment(
        runtime_root,
        providers=providers,
        project_prefix="mevo2glpie2e",
        extra_compose_files=(E2E_COMPOSE_FILE,),
        extra_compose_environment={
            "MEVO2GLPI_E2E_GLPI_NETWORK": glpi_environment.network_name,
            "OPENAI_TRANSCRIPTION_SIMULATOR_API_KEY": transcription_api_key,
        },
        extra_image_services=("openai-transcription-simulator",),
        database_host="mvp-postgres",
    )
    primary_error: BaseException | None = None
    try:
        environment.compose(
            "build",
            "openai-transcription-simulator",
            timeout_seconds=10 * 60,
        )
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


@pytest.fixture(scope="session")
def synthetic_audio_bytes() -> bytes:
    """Return the repository-owned, synthetic French speech fixture."""
    return load_synthetic_speech()


@pytest.fixture(scope="session")
def parakeet_environment(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[ParakeetComposeEnvironment]:
    """Build and start the pinned CPU provider used by the nominal release path."""
    runtime_root = tmp_path_factory.mktemp("mevo2glpi-parakeet-e2e")
    environment = create_parakeet_compose_environment(runtime_root)
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
                "Parakeet E2E cleanup also failed; the primary error was preserved"
            )


@pytest.fixture(scope="session")
def parakeet_e2e_environment(
    tmp_path_factory: pytest.TempPathFactory,
    glpi_environment,
    parakeet_environment: ParakeetComposeEnvironment,
) -> Iterator[MvpComposeEnvironment]:
    """Start the application stack on its nominal OpenAI-compatible provider."""
    credentials = glpi_environment.credentials
    providers = ProviderConfiguration(
        transcription_base_url=parakeet_environment.internal_base_url,
        transcription_api_key=parakeet_environment.api_key,
        glpi_base_url=glpi_environment.internal_base_url,
        glpi_username=credentials.username,
        glpi_password=credentials.password,
        glpi_client_id=credentials.client_id,
        glpi_client_secret=credentials.client_secret,
        glpi_request_source_id=glpi_environment.request_source.id,
    )
    runtime_root = tmp_path_factory.mktemp("mevo2glpi-parakeet-pipeline-e2e")
    environment = create_mvp_compose_environment(
        runtime_root,
        providers=providers,
        project_prefix="mevo2glpiparakeete2e",
        extra_compose_files=(PARAKEET_E2E_COMPOSE_FILE,),
        extra_compose_environment={
            "MEVO2GLPI_E2E_GLPI_NETWORK": glpi_environment.network_name,
            "TRANSCRIPTION_NETWORK_NAME": parakeet_environment.network_name,
            "TRANSCRIPTION_PROVIDER": "openai_compatible",
            "OPENAI_COMPATIBLE_TRANSCRIPTION_BASE_URL": (
                parakeet_environment.internal_base_url
            ),
            "OPENAI_COMPATIBLE_TRANSCRIPTION_API_KEY": (parakeet_environment.api_key),
            "OPENAI_COMPATIBLE_TRANSCRIPTION_MODEL": "parakeet-ultra",
            "OPENAI_COMPATIBLE_TRANSCRIPTION_LANGUAGE": "fr",
            "TRANSCRIPTION_HTTP_TIMEOUT_SECONDS": "180",
        },
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
                "Parakeet pipeline E2E cleanup also failed; the primary error was preserved"
            )
