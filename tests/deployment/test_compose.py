"""Validate the minimal Dokploy Compose contract and its disposable harness."""

from __future__ import annotations

import json
import re
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from .conftest import (
    ALL_SERVICES,
    APPS_DOCKER_ROOT,
    BASE_COMPOSE_FILE,
    COMMON_COMPOSE_SERVICES_FILE,
    CUSTOM_SERVICES,
    DOCKER_ROOT,
    SYNTHETIC_APP_COMMIT_SHA,
    SYNTHETIC_APP_RELEASE,
    DeploymentEnvironment,
    clean_environment,
    run_command,
)

pytestmark = [pytest.mark.deployment, pytest.mark.timeout(15 * 60)]


def _services(config: dict[str, object]) -> dict[str, dict[str, Any]]:
    services = config.get("services")
    assert isinstance(services, dict)
    return services


def _volume_for(service: dict[str, Any], target: str) -> dict[str, Any] | None:
    for volume in service.get("volumes", []):
        if volume["target"] == target:
            return volume
    return None


def test_production_manifest_contains_only_the_five_applications() -> None:
    assert BASE_COMPOSE_FILE == APPS_DOCKER_ROOT / "docker-compose.prod.yml"
    assert BASE_COMPOSE_FILE.is_file()
    assert COMMON_COMPOSE_SERVICES_FILE == (
        APPS_DOCKER_ROOT / "docker-compose.common.services.yml"
    )
    assert COMMON_COMPOSE_SERVICES_FILE.is_file()
    assert (APPS_DOCKER_ROOT / ".env.example").is_file()


def test_production_entrypoint_uses_portable_service_extends() -> None:
    compose_source = BASE_COMPOSE_FILE.read_text(encoding="utf-8")

    assert "include:" not in compose_source
    for service_name in CUSTOM_SERVICES:
        declaration = f"""  {service_name}:
    extends:
      file: ./docker-compose.common.services.yml
      service: {service_name}
"""
        assert declaration in compose_source


def test_dokploy_domain_patch_merges_with_web_configuration(tmp_path: Path) -> None:
    compose_source = BASE_COMPOSE_FILE.read_text(encoding="utf-8")
    web_declaration = """  web:
    extends:
      file: ./docker-compose.common.services.yml
      service: web
"""
    assert compose_source.count(web_declaration) == 1
    compose_source = compose_source.replace(
        web_declaration,
        """  web:
    extends:
      file: ./docker-compose.common.services.yml
      service: web
    labels:
      - traefik.docker.network=dokploy-network
      - traefik.enable=true
    networks:
      - dokploy-network
      - default
""",
        1,
    )
    patched_compose_file = tmp_path / "docker-compose.dokploy-domain.yml"
    patched_compose_file.write_text(compose_source, encoding="utf-8")

    result = run_command(
        [
            "docker",
            "compose",
            "--project-directory",
            str(APPS_DOCKER_ROOT),
            "--env-file",
            str(APPS_DOCKER_ROOT / ".env.example"),
            "--file",
            str(patched_compose_file),
            "config",
            "--format",
            "json",
        ],
        environment=clean_environment(),
    )
    config = json.loads(result.stdout)
    services = _services(config)
    web = services["web"]

    assert set(services) == set(CUSTOM_SERVICES)
    assert web["build"]["dockerfile"] == "infra/docker/apps/web/Dockerfile"
    assert web["read_only"] is True
    assert web["depends_on"]["database-migrations"]["condition"] == (
        "service_completed_successfully"
    )
    assert set(web["networks"]) == {"database", "default", "dokploy-network"}
    assert web["labels"]["traefik.enable"] == "true"
    assert config["networks"]["dokploy-network"]["name"] == "dokploy-network"


def test_environment_example_documents_every_compose_variable_without_secrets() -> None:
    compose_source = "\n".join(
        (
            BASE_COMPOSE_FILE.read_text(encoding="utf-8"),
            COMMON_COMPOSE_SERVICES_FILE.read_text(encoding="utf-8"),
        )
    )
    example_source = (APPS_DOCKER_ROOT / ".env.example").read_text(encoding="utf-8")
    interpolated_names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose_source))
    documented_values = dict(
        re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", example_source, re.MULTILINE)
    )

    assert set(documented_values) == interpolated_names
    assert "# Docker Compose et Dokploy" in example_source
    assert "# PostgreSQL Database Dokploy" in example_source
    assert "# Application web" in example_source
    assert "# Worker d'automatisation" in example_source
    assert "# Worker de rétention" in example_source
    assert documented_values["APP_ENVIRONMENT"] == "production"
    assert documented_values["APP_RELEASE"] == "CHANGE_ME"
    assert documented_values["APP_COMMIT_SHA"] == "CHANGE_ME"
    assert documented_values["DATABASE_NETWORK_NAME"] == (
        "mevo2glpi-prod-database-network"
    )
    assert documented_values["TRANSCRIPTION_NETWORK_NAME"] == (
        "mevo2glpi-prod-transcription-network"
    )
    assert documented_values["AUDIO_VOLUME_NAME"] == "mevo2glpi-prod-audio-data"
    assert documented_values["FAILED_RETENTION_DAYS"] == "10"
    assert documented_values["TRANSCRIPTION_HTTP_TIMEOUT_SECONDS"] == "180"
    assert documented_values["TRANSCRIPTION_PROVIDER"] == "openai_compatible"
    assert documented_values["OPENAI_COMPATIBLE_TRANSCRIPTION_BASE_URL"] == (
        "http://mevo2glpi-prod-transcription:5092/v1"
    )
    assert documented_values["OPENAI_COMPATIBLE_TRANSCRIPTION_MODEL"] == (
        "parakeet-ultra"
    )
    assert documented_values["ORIGINATING_PHONE_NUMBER_KEY_HOST_PATH"] == (
        "/etc/mevo2glpi/secrets/originating-phone-number.key"
    )
    assert documented_values["GLPI_TICKET_TYPE"] == "1"
    assert documented_values["GLPI_TICKET_REQUEST_SOURCE_ID"] == (
        "CHANGE_ME_WITH_THE_PRODUCTION_MEVO_REQUEST_SOURCE_ID"
    )

    secret_names = {
        "LEGACY_TRANSCRIPTION_API_KEY",
        "OPENAI_COMPATIBLE_TRANSCRIPTION_API_KEY",
        "GLPI_USERNAME",
        "GLPI_PASSWORD",
        "GLPI_CLIENT_ID",
        "GLPI_CLIENT_SECRET",
    }
    assert {documented_values[name] for name in secret_names} == {"CHANGE_ME"}
    assert "mevo2glpi:CHANGE_ME@" in documented_values["DATABASE_URL"]
    assert not re.search(
        r"(?i)(?:api[_-]?key|secret|password)=[0-9a-f]{32,}", example_source
    )


def test_production_manifest_builds_the_five_expected_images(
    render_compose: Callable[[], dict[str, object]],
) -> None:
    services = _services(render_compose())
    assert set(services) == set(CUSTOM_SERVICES)
    assert "postgres" not in services
    assert "nginx" not in services

    expected_dockerfiles = {
        "database-migrations": "infra/docker/apps/database-migrations/Dockerfile",
        "web": "infra/docker/apps/web/Dockerfile",
        "worker-automation": "infra/docker/apps/worker-automation/Dockerfile",
        "worker-publisher": "infra/docker/apps/worker-publisher/Dockerfile",
        "worker-retention": "infra/docker/apps/worker-retention/Dockerfile",
    }
    for service_name, dockerfile in expected_dockerfiles.items():
        build = services[service_name]["build"]
        assert Path(build["context"]).resolve() == DOCKER_ROOT.parent.parent.resolve()
        assert build["dockerfile"] == dockerfile
        assert build["args"] == {
            "IMAGE_VERSION": SYNTHETIC_APP_RELEASE,
            "VCS_REF": SYNTHETIC_APP_COMMIT_SHA,
        }


def test_production_services_share_the_release_environment(
    render_compose: Callable[[], dict[str, object]],
) -> None:
    services = _services(render_compose())

    for service in services.values():
        assert service["environment"]["APP_ENVIRONMENT"] == "production"


def test_application_dockerfiles_copy_every_workspace_member_manifest() -> None:
    repository_root = DOCKER_ROOT.parent.parent
    dockerignore = (repository_root / ".dockerignore").read_text(encoding="utf-8")
    workspace = tomllib.loads(
        (repository_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    members = workspace["tool"]["uv"]["workspace"]["members"]

    assert workspace["project"]["license"] == "EUPL-1.2"
    assert "!LICENSE" in dockerignore
    assert "!NOTICE" in dockerignore
    assert (repository_root / "LICENSE").is_file()
    assert (repository_root / "NOTICE").is_file()
    for member in members:
        manifest = repository_root / member / "pyproject.toml"
        member_metadata = tomllib.loads(manifest.read_text(encoding="utf-8"))
        assert member_metadata["project"]["license"] == "EUPL-1.2"

    dockerfiles = [
        APPS_DOCKER_ROOT / application_name / "Dockerfile"
        for application_name in (
            "database-migrations",
            "web",
            "worker-automation",
            "worker-publisher",
            "worker-retention",
        )
    ]
    dockerfiles.append(repository_root / "apps" / "transcription-api" / "Dockerfile")

    for dockerfile_path in dockerfiles:
        dockerfile = dockerfile_path.read_text(encoding="utf-8")
        assert "COPY pyproject.toml uv.lock README.md ./" in dockerfile
        assert (
            "COPY --chown=10001:10001 --chmod=0444 LICENSE NOTICE "
            "/usr/share/licenses/mevo2glpi/"
        ) in dockerfile
        for member in members:
            manifest = f"{member}/pyproject.toml"
            assert f"COPY {manifest} {manifest}" in dockerfile


def test_application_dockerfiles_embed_the_release_identity() -> None:
    repository_root = DOCKER_ROOT.parent.parent
    dockerfiles = [
        APPS_DOCKER_ROOT / application_name / "Dockerfile"
        for application_name in (
            "database-migrations",
            "web",
            "worker-automation",
            "worker-publisher",
            "worker-retention",
        )
    ]
    dockerfiles.append(repository_root / "apps" / "transcription-api" / "Dockerfile")

    for dockerfile_path in dockerfiles:
        dockerfile = dockerfile_path.read_text(encoding="utf-8")
        assert "ARG IMAGE_VERSION\n" in dockerfile
        assert "ARG VCS_REF\n" in dockerfile
        assert "ARG IMAGE_VERSION=" not in dockerfile
        assert "ARG VCS_REF=" not in dockerfile
        assert 'org.opencontainers.image.version="${IMAGE_VERSION}"' in dockerfile
        assert 'org.opencontainers.image.revision="${VCS_REF}"' in dockerfile
        assert (
            'org.opencontainers.image.source="https://github.com/dev-vauclaire/mevo2glpi"'
            in dockerfile
        )
        assert 'org.opencontainers.image.title="mevo2glpi ' in dockerfile
        assert 'org.opencontainers.image.licenses="EUPL-1.2' in dockerfile
        assert "ENV APP_RELEASE=${IMAGE_VERSION}" in dockerfile
        assert "APP_COMMIT_SHA=${VCS_REF}" in dockerfile


def test_production_manifest_separates_application_networks(
    render_compose: Callable[[], dict[str, object]],
) -> None:
    config = render_compose()
    assert set(config["networks"]) == {
        "database",
        "dokploy-network",
        "transcription",
    }
    assert config["networks"]["database"]["name"] == ("mevo2glpi-contract-database")
    assert config["networks"]["database"]["external"] is True
    assert config["networks"]["dokploy-network"]["name"] == (
        "mevo2glpi-contract-dokploy"
    )
    assert config["networks"]["dokploy-network"]["external"] is True
    assert config["networks"]["transcription"]["name"] == (
        "mevo2glpi-contract-transcription"
    )
    assert config["networks"]["transcription"]["external"] is True
    assert config["volumes"] == {"audio-data": {"name": "mevo2glpi-contract-audio"}}
    services = _services(config)
    assert set(services["web"]["networks"]) == {"database", "dokploy-network"}
    assert set(services["worker-automation"]["networks"]) == {
        "database",
        "transcription",
    }
    for service_name in (
        "database-migrations",
        "worker-publisher",
        "worker-retention",
    ):
        assert set(services[service_name]["networks"]) == {"database"}


def test_production_uses_openai_compatible_provider_as_nominal_default(
    render_compose: Callable[[], dict[str, object]],
) -> None:
    config = render_compose()
    automation = _services(config)["worker-automation"]

    assert automation["environment"]["TRANSCRIPTION_PROVIDER"] == ("openai_compatible")
    assert automation["environment"]["OPENAI_COMPATIBLE_TRANSCRIPTION_BASE_URL"] == (
        "http://mevo2glpi-prod-transcription:5092/v1"
    )
    assert automation["environment"]["OPENAI_COMPATIBLE_TRANSCRIPTION_MODEL"] == (
        "parakeet-ultra"
    )
    assert set(automation["networks"]) == {"database", "transcription"}
    assert config["networks"]["transcription"]["external"] is True


def test_migration_gates_all_long_lived_applications(
    render_compose: Callable[[], dict[str, object]],
) -> None:
    services = _services(render_compose())
    assert "depends_on" not in services["database-migrations"]
    for service_name in (
        "web",
        "worker-automation",
        "worker-publisher",
        "worker-retention",
    ):
        assert services[service_name]["depends_on"] == {
            "database-migrations": {
                "condition": "service_completed_successfully",
                "required": True,
            }
        }


def test_audio_access_follows_least_privilege(
    render_compose: Callable[[], dict[str, object]],
) -> None:
    services = _services(render_compose())
    assert _volume_for(services["database-migrations"], "/data/audios") is None
    web_audio = _volume_for(services["web"], "/data/audios")
    automation_audio = _volume_for(services["worker-automation"], "/data/audios")
    publisher_audio = _volume_for(services["worker-publisher"], "/data/audios")
    retention_audio = _volume_for(services["worker-retention"], "/data/audios")
    assert web_audio is not None and web_audio["read_only"] is True
    assert automation_audio is not None and automation_audio["read_only"] is True
    assert (
        publisher_audio is not None and publisher_audio.get("read_only", False) is False
    )
    assert (
        retention_audio is not None and retention_audio.get("read_only", False) is False
    )
    assert {
        web_audio["source"],
        automation_audio["source"],
        publisher_audio["source"],
        retention_audio["source"],
    } == {"audio-data"}


def test_runtime_hardening_and_restart_policies_are_explicit(
    render_compose: Callable[[], dict[str, object]],
) -> None:
    services = _services(render_compose())
    expected_restarts = {
        "database-migrations": "no",
        "web": "unless-stopped",
        "worker-automation": "on-failure:5",
        "worker-publisher": "on-failure:5",
        "worker-retention": "on-failure:5",
    }
    for service_name, service in services.items():
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert service["restart"] == expected_restarts[service_name]
        assert service["pids_limit"] == 128


def test_web_receives_no_provider_credentials(
    render_compose: Callable[[], dict[str, object]],
) -> None:
    services = _services(render_compose())
    web_environment = services["web"]["environment"]
    assert "DATABASE_URL" in web_environment
    assert not any(
        name.startswith(
            ("GLPI_", "LEGACY_TRANSCRIPTION_", "OPENAI_COMPATIBLE_TRANSCRIPTION_")
        )
        for name in web_environment
    )
    assert (
        "LEGACY_TRANSCRIPTION_API_KEY" in services["worker-automation"]["environment"]
    )
    assert (
        "OPENAI_COMPATIBLE_TRANSCRIPTION_API_KEY"
        in services["worker-automation"]["environment"]
    )
    automation_environment = services["worker-automation"]["environment"]
    assert automation_environment["WORKER_LOG_LEVEL"] == "INFO"
    assert automation_environment["GLPI_TICKET_TYPE"] == "1"
    assert automation_environment["GLPI_TICKET_REQUEST_SOURCE_ID"] == "42"
    assert (
        "LEGACY_TRANSCRIPTION_API_KEY"
        not in services["worker-retention"]["environment"]
    )
    assert web_environment["WEB_TRUST_PROXY_HEADERS"] == "true"
    publisher_environment = services["worker-publisher"]["environment"]
    assert "PUBLISHER_ENTRA_PRIVATE_KEY_PEM_PATH" in publisher_environment
    assert "PUBLISHER_ENTRA_CERTIFICATE_PEM_PATH" in publisher_environment
    assert not any(
        name.startswith(("GLPI_", "LEGACY_TRANSCRIPTION_"))
        or name.startswith("OPENAI_COMPATIBLE_TRANSCRIPTION_")
        for name in publisher_environment
    )
    for service_name in ("database-migrations", "web", "worker-retention"):
        assert (
            "GLPI_TICKET_REQUEST_SOURCE_ID" not in services[service_name]["environment"]
        )


def test_production_web_authentication_is_explicit_and_fail_closed(
    render_compose: Callable[[], dict[str, object]],
) -> None:
    services = _services(render_compose())
    web_environment = services["web"]["environment"]

    assert web_environment["WEB_AUTH_MODE"] == "oidc"
    assert web_environment["WEB_AUTH_COOKIE_SECURE"] == "true"
    assert web_environment["WEB_AUTH_REQUIRED_ROLE"] == "mevo2glpi-access"
    assert web_environment["WEB_AUTH_ROLES_CLAIM"] == "roles"
    for name in (
        "WEB_AUTH_SESSION_SECRET",
        "WEB_AUTH_ISSUER_URL",
        "WEB_AUTH_CLIENT_ID",
        "WEB_AUTH_CLIENT_SECRET",
        "WEB_AUTH_REDIRECT_URI",
    ):
        assert web_environment[name]
    for service_name, service in services.items():
        if service_name != "web":
            assert not any(
                name.startswith("WEB_AUTH_") for name in service["environment"]
            )


def test_ca_bundle_is_read_only_and_used_without_disabling_tls(
    render_compose: Callable[[], dict[str, object]],
) -> None:
    services = _services(render_compose())
    target = "/etc/ssl/certs/mevo2glpi-ca-bundle.pem"
    for service in services.values():
        bundle = _volume_for(service, target)
        assert bundle is not None
        assert bundle["type"] == "bind"
        assert bundle["read_only"] is True
        assert service["environment"]["PGSSLROOTCERT"] == target

    for worker_name in (
        "worker-automation",
        "worker-publisher",
        "worker-retention",
    ):
        assert services[worker_name]["environment"]["SSL_CERT_FILE"] == target
    assert services["web"]["environment"]["SSL_CERT_FILE"] == target
    assert services["worker-publisher"]["environment"]["REQUESTS_CA_BUNDLE"] == target


def test_rejected_ingestion_retention_uses_shared_interval(
    render_compose: Callable[[], dict[str, object]],
) -> None:
    environment = _services(render_compose())["worker-retention"]["environment"]

    assert environment["REJECTED_INGESTION_RETENTION_DAYS"] == "90"
    assert environment["RETENTION_CHECK_INTERVAL_HOURS"] == "6"
    assert "REJECTED_INGESTION_RETENTION_MODE" not in environment
    assert "REJECTED_INGESTION_RETENTION_BATCH_SIZE" not in environment
    assert "REJECTED_INGESTION_RETENTION_SCAN_INTERVAL_HOURS" not in environment


def test_publisher_pem_credentials_are_separate_read_only_bind_mounts(
    render_compose: Callable[[], dict[str, object]],
) -> None:
    publisher = _services(render_compose())["worker-publisher"]
    private_key = _volume_for(
        publisher,
        "/run/secrets/publisher-entra-private-key.pem",
    )
    certificate = _volume_for(
        publisher,
        "/run/secrets/publisher-entra-certificate.pem",
    )

    assert private_key is not None and private_key["read_only"] is True
    assert certificate is not None and certificate["read_only"] is True
    assert private_key["type"] == certificate["type"] == "bind"
    assert private_key["source"] != certificate["source"]
    assert (
        publisher["environment"]["PUBLISHER_ENTRA_PRIVATE_KEY_PEM_PATH"]
        == (private_key["target"])
    )
    assert (
        publisher["environment"]["PUBLISHER_ENTRA_CERTIFICATE_PEM_PATH"]
        == (certificate["target"])
    )


def test_originating_number_key_is_shared_only_by_its_two_consumers(
    render_compose: Callable[[], dict[str, object]],
) -> None:
    services = _services(render_compose())
    target = "/run/secrets/originating-phone-number.key"

    for service_name in ("worker-publisher", "worker-automation"):
        service = services[service_name]
        key = _volume_for(service, target)
        assert key is not None
        assert key["type"] == "bind"
        assert key["read_only"] is True
        assert service["environment"]["ORIGINATING_PHONE_NUMBER_KEY_PATH"] == target

    for service_name in ("database-migrations", "web", "worker-retention"):
        service = services[service_name]
        assert _volume_for(service, target) is None
        assert "ORIGINATING_PHONE_NUMBER_KEY_PATH" not in service["environment"]


def test_disposable_stack_reports_fresh_workers_over_direct_http(
    deployment_environment: DeploymentEnvironment,
) -> None:
    with deployment_environment.client() as client:
        health = client.get("/health")
        health.raise_for_status()
        observed: dict[str, Any] | None = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            response = client.get("/api/observability")
            response.raise_for_status()
            candidate = response.json()
            workers_by_service = {
                worker["service_name"]: worker for worker in candidate["workers"]
            }
            if all(
                workers_by_service.get(service_name, {}).get("heartbeat_status")
                == "fresh"
                for service_name in ("worker_automation", "worker_retention")
            ) and (
                workers_by_service.get("worker_publisher", {}).get("heartbeat_status")
                == "absent"
            ):
                observed = candidate
                break
            time.sleep(0.25)

    assert health.json() == {"status": "ok"}
    assert observed is not None
    assert {worker["service_name"] for worker in observed["workers"]} == {
        "worker_publisher",
        "worker_automation",
        "worker_retention",
    }


def test_test_harness_adds_only_postgres_and_loopback_http(
    deployment_environment: DeploymentEnvironment,
) -> None:
    assert {service for service in ALL_SERVICES} == {
        "postgres",
        "database-migrations",
        "web",
        "worker-automation",
        "worker-retention",
    }
    assert deployment_environment.base_url.startswith("http://127.0.0.1:")
    for service_name in ALL_SERVICES:
        settings = deployment_environment.inspect_service(service_name)[
            "NetworkSettings"
        ]
        assert isinstance(settings, dict)
        ports = {
            port: bindings
            for port, bindings in (settings.get("Ports") or {}).items()
            if bindings is not None
        }
        if service_name == "web":
            assert set(ports) == {"8000/tcp"}
        elif service_name == "postgres":
            assert set(ports) == {"5432/tcp"}
        else:
            assert ports == {}
        for bindings in ports.values():
            assert bindings[0]["HostIp"] == "127.0.0.1"


def test_migration_is_idempotent(
    deployment_environment: DeploymentEnvironment,
) -> None:
    first = deployment_environment.compose("run", "--rm", "database-migrations")
    second = deployment_environment.compose("run", "--rm", "database-migrations")
    assert first.returncode == 0
    assert second.returncode == 0


@pytest.mark.parametrize("service_name", CUSTOM_SERVICES)
def test_images_contain_no_ephemeral_runtime_secret(
    deployment_environment: DeploymentEnvironment,
    service_name: str,
) -> None:
    deployment_environment.assert_image_contains_no_runtime_secret(service_name)


@pytest.mark.parametrize("service_name", CUSTOM_SERVICES)
def test_images_report_the_synthetic_release_identity(
    deployment_environment: DeploymentEnvironment,
    service_name: str,
) -> None:
    image = deployment_environment.inspect_built_image_for_service(service_name)
    config = image["Config"]
    labels = config["Labels"]
    environment = config["Env"]

    assert labels["org.opencontainers.image.version"] == SYNTHETIC_APP_RELEASE
    assert labels["org.opencontainers.image.revision"] == SYNTHETIC_APP_COMMIT_SHA
    assert f"APP_RELEASE={SYNTHETIC_APP_RELEASE}" in environment
    assert f"APP_COMMIT_SHA={SYNTHETIC_APP_COMMIT_SHA}" in environment
