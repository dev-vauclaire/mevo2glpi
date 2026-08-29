"""Fixtures for the disposable multi-environment Compose deployment."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from tests.support.mvp_compose import (
    ALL_SERVICES,
    BASE_COMPOSE_FILE,
    CUSTOM_SERVICES,
    DOCKER_ROOT,
    ENVIRONMENTS_ROOT,
    INTERNAL_CA_COMPOSE_FILE,
    MvpComposeEnvironment,
    clean_environment,
    create_mvp_compose_environment,
    run_command,
)

DeploymentEnvironment = MvpComposeEnvironment


def _parse_environment_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator:
            raise RuntimeError(f"Invalid environment example: {path.name}")
        values[name] = value
    return values


@pytest.fixture
def render_compose(tmp_path: Path) -> Callable[..., dict[str, object]]:
    """Render one environment without contacting the Docker daemon."""

    def render(
        environment_name: str,
        *,
        release_confirmed: bool = True,
        include_internal_ca: bool = False,
        check: bool = True,
    ) -> dict[str, object] | subprocess.CompletedProcess[str]:
        environment_root = ENVIRONMENTS_ROOT / environment_name
        compose_environment = _parse_environment_file(
            environment_root / "compose.env.example"
        )
        versions_file = environment_root / "versions.env"
        if versions_file.exists():
            compose_environment.update(_parse_environment_file(versions_file))

        service_env_names = (
            "database",
            "migrations",
            "web",
            "worker-automation",
            "worker-retention",
        )
        variable_names = (
            "GTA_DATABASE_ENV_FILE",
            "GTA_MIGRATIONS_ENV_FILE",
            "GTA_WEB_ENV_FILE",
            "GTA_AUTOMATION_ENV_FILE",
            "GTA_RETENTION_ENV_FILE",
        )
        for service_env_name, variable_name in zip(
            service_env_names,
            variable_names,
            strict=True,
        ):
            compose_environment[variable_name] = str(
                environment_root / "services" / f"{service_env_name}.env.example"
            )

        harmless_file = tmp_path / "compose-secret-placeholder"
        harmless_file.write_text("not-a-secret\n", encoding="utf-8")
        for variable_name in (
            "GTA_POSTGRES_PASSWORD_FILE",
            "GTA_NGINX_TLS_CERTIFICATE_FILE",
            "GTA_NGINX_TLS_KEY_FILE",
            "GTA_NGINX_HTPASSWD_FILE",
        ):
            compose_environment[variable_name] = str(harmless_file)
        compose_environment["GTA_NGINX_SECRET_GID"] = str(os.getgid())
        compose_environment["GTA_POSTGRES_SECRET_GID"] = str(os.getgid())
        if release_confirmed:
            compose_environment["GTA_RELEASE_DIGESTS_CONFIRMED"] = "1"

        compose_files = [
            BASE_COMPOSE_FILE,
            environment_root / "compose.override.yaml",
        ]
        if include_internal_ca:
            compose_files.append(INTERNAL_CA_COMPOSE_FILE)
            compose_environment["GTA_CA_BUNDLE"] = str(harmless_file)

        command = ["docker", "compose"]
        for compose_file in compose_files:
            command.extend(("-f", str(compose_file)))
        command.extend(("config", "--format", "json"))
        result = run_command(
            command,
            environment={**clean_environment(), **compose_environment},
            check=check,
        )
        if not check:
            return result
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise RuntimeError("Compose returned a non-object configuration")
        return payload

    return render


@pytest.fixture(scope="session")
def deployment_environment(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[DeploymentEnvironment]:
    """Build, start and finally destroy one complete disposable MVP stack."""
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
    "ENVIRONMENTS_ROOT",
    "INTERNAL_CA_COMPOSE_FILE",
    "DeploymentEnvironment",
]
