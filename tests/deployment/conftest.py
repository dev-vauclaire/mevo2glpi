"""Fixtures for the simplified disposable production Compose contract."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator

import pytest
from tests.support.mvp_compose import (
    ALL_SERVICES,
    BASE_COMPOSE_FILE,
    COMMON_COMPOSE_SERVICES_FILE,
    CUSTOM_SERVICES,
    DOCKER_ROOT,
    PROVIDERS_COMPOSE_FILE,
    PROVIDERS_ENV_FILE,
    REPOSITORY_ROOT,
    STAGING_COMPOSE_FILE,
    MvpComposeEnvironment,
    ProviderConfiguration,
    clean_environment,
    create_mvp_compose_environment,
    run_command,
)

DeploymentEnvironment = MvpComposeEnvironment


@pytest.fixture
def render_compose() -> Callable[[], dict[str, object]]:
    """Render the production manifest without contacting external services."""

    def render() -> dict[str, object]:
        providers = ProviderConfiguration.synthetic()
        environment = {
            **clean_environment(),
            "COMPOSE_PROJECT_NAME": "gta-contract",
            "DOKPLOY_NETWORK_NAME": "gta-contract-dokploy",
            "DATABASE_NETWORK_NAME": "gta-contract-database",
            "AUDIO_VOLUME_NAME": "gta-contract-audio",
            "DATABASE_URL": (
                "postgresql+psycopg://gta:synthetic@postgres:5432/"
                "glpi_ticketing_automation"
            ),
            "PUBLIC_BASE_URL": "https://tickets.example.invalid",
            "LEGACY_TRANSCRIPTION_BASE_URL": providers.transcription_base_url,
            "LEGACY_TRANSCRIPTION_API_KEY": providers.transcription_api_key,
            "GLPI_BASE_URL": providers.glpi_base_url,
            "GLPI_USERNAME": providers.glpi_username,
            "GLPI_PASSWORD": providers.glpi_password,
            "GLPI_CLIENT_ID": providers.glpi_client_id,
            "GLPI_CLIENT_SECRET": providers.glpi_client_secret,
            "PUBLISHER_ENTRA_TENANT_ID": "00000000-0000-4000-8000-000000000001",
            "PUBLISHER_ENTRA_CLIENT_ID": "00000000-0000-4000-8000-000000000002",
            "PUBLISHER_MAILBOX_ID": "00000000-0000-4000-8000-000000000003",
            "PUBLISHER_ENTRA_PRIVATE_KEY_PEM_HOST_PATH": (
                "/tmp/synthetic-publisher-private-key.pem"
            ),
            "PUBLISHER_ENTRA_CERTIFICATE_PEM_HOST_PATH": (
                "/tmp/synthetic-publisher-certificate.pem"
            ),
        }
        result = run_command(
            [
                "docker",
                "compose",
                "--env-file",
                str(DOCKER_ROOT / ".env.example"),
                "-f",
                str(BASE_COMPOSE_FILE),
                "config",
                "--format",
                "json",
            ],
            environment=environment,
            secrets_to_redact=providers.secrets,
        )
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise RuntimeError("Compose returned a non-object configuration")
        return payload

    return render


@pytest.fixture
def render_staging_compose() -> Callable[[], dict[str, object]]:
    """Render the isolated staging manifest without starting containers."""

    def render() -> dict[str, object]:
        result = run_command(
            [
                "docker",
                "compose",
                "--env-file",
                str(DOCKER_ROOT / "staging.env.example"),
                "-f",
                str(STAGING_COMPOSE_FILE),
                "config",
                "--format",
                "json",
            ],
            environment=clean_environment(),
        )
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise RuntimeError("Compose returned a non-object staging configuration")
        return payload

    return render


@pytest.fixture(scope="session")
def deployment_environment(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[DeploymentEnvironment]:
    """Build five apps and run the downstream stack with disposable PostgreSQL."""
    if os.environ.get("PYTEST_XDIST_WORKER") is not None:
        pytest.fail("Deployment tests must not be parallelized")

    runtime_root = tmp_path_factory.mktemp("gta-mvp-deployment")
    environment = create_mvp_compose_environment(runtime_root)
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
                "MVP deployment cleanup also failed; the primary error was preserved"
            )


__all__ = [
    "ALL_SERVICES",
    "BASE_COMPOSE_FILE",
    "COMMON_COMPOSE_SERVICES_FILE",
    "CUSTOM_SERVICES",
    "DOCKER_ROOT",
    "PROVIDERS_COMPOSE_FILE",
    "PROVIDERS_ENV_FILE",
    "DeploymentEnvironment",
    "REPOSITORY_ROOT",
    "STAGING_COMPOSE_FILE",
    "clean_environment",
    "run_command",
]
