"""Fixtures for the simplified disposable production Compose contract."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator

import pytest
from tests.support.mvp_compose import (
    ALL_SERVICES,
    BASE_COMPOSE_FILE,
    CUSTOM_SERVICES,
    DOCKER_ROOT,
    PRODUCTION_COMPOSE_SERVICES_FILE,
    REPOSITORY_ROOT,
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
            "GTA_DOKPLOY_ENV_FILE": str(DOCKER_ROOT / ".env.example"),
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
        }
        result = run_command(
            [
                "docker",
                "compose",
                "--project-directory",
                str(REPOSITORY_ROOT),
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


@pytest.fixture(scope="session")
def deployment_environment(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[DeploymentEnvironment]:
    """Build and run the four apps with test-only PostgreSQL and direct web HTTP."""
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
    "CUSTOM_SERVICES",
    "DOCKER_ROOT",
    "DeploymentEnvironment",
    "PRODUCTION_COMPOSE_SERVICES_FILE",
    "REPOSITORY_ROOT",
    "clean_environment",
    "run_command",
]
