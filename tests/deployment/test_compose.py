"""Validate the isolated dev, staging and production Compose contracts."""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from .conftest import (
    ALL_SERVICES,
    BASE_COMPOSE_FILE,
    CUSTOM_SERVICES,
    DOCKER_ROOT,
    ENVIRONMENTS_ROOT,
    DeploymentEnvironment,
)

pytestmark = [pytest.mark.deployment, pytest.mark.timeout(15 * 60)]

POSTGRES_REFERENCE = (
    "postgres:16.10-alpine@"
    "sha256:029660641a0cfc575b14f336ba448fb8a75fd595d42e1fa316b9fb4378742297"
)
ZERO_DIGEST = "0" * 64
SYNTHETIC_AUDIO = b"RIFF\x10\x00\x00\x00WAVEfmt \x00\x00\x00\x00data"


def _services(config: dict[str, object]) -> dict[str, dict[str, Any]]:
    services = config.get("services")
    assert isinstance(services, dict)
    return services


def _volume_for(service: dict[str, Any], target: str) -> dict[str, Any] | None:
    for volume in service.get("volumes", []):
        if volume["target"] == target:
            return volume
    return None


def _published_port_for(
    environment: DeploymentEnvironment,
    container_port: str,
) -> int:
    payload = environment.inspect_service("nginx")
    network = payload["NetworkSettings"]
    assert isinstance(network, dict)
    bindings = (network.get("Ports") or {}).get(container_port)
    assert isinstance(bindings, list) and len(bindings) == 1
    assert bindings[0]["HostIp"] == "127.0.0.1"
    return int(bindings[0]["HostPort"])


def test_runtime_layout_has_one_base_and_three_environment_overlays() -> None:
    assert BASE_COMPOSE_FILE.is_file()
    assert (DOCKER_ROOT / "compose.internal-ca.yaml").is_file()
    assert not (DOCKER_ROOT / "docker-compose.prod.yaml").exists()
    assert not (DOCKER_ROOT / "prepare-host.sh").exists()
    assert not (DOCKER_ROOT / "worker" / "start-runtime.sh").exists()

    dockerfiles = {
        "database-migrations": "database-migrations/Dockerfile",
        "web": "web/Dockerfile",
        "worker-automation": "worker-automation/Dockerfile",
        "worker-retention": "worker-retention/Dockerfile",
        "nginx": "nginx/Dockerfile",
    }
    for relative_path in dockerfiles.values():
        assert (DOCKER_ROOT / relative_path).is_file()

    for environment_name in ("dev", "staging", "prod"):
        root = ENVIRONMENTS_ROOT / environment_name
        assert (root / "compose.override.yaml").is_file()
        assert (root / "compose.env.example").is_file()
        for service_env in (
            "database",
            "migrations",
            "web",
            "worker-automation",
            "worker-retention",
        ):
            assert (root / "services" / f"{service_env}.env.example").is_file()
    assert (ENVIRONMENTS_ROOT / "staging" / "versions.env").is_file()
    assert (ENVIRONMENTS_ROOT / "prod" / "versions.env").is_file()


def test_nginx_published_ports_default_to_http_80_and_https_443() -> None:
    compose_source = BASE_COMPOSE_FILE.read_text(encoding="utf-8")
    assert 'published: "${GTA_NGINX_HTTP_PORT:-80}"' in compose_source
    assert 'published: "${GTA_NGINX_HTTPS_PORT:-443}"' in compose_source


def test_compose_cli_supports_gateway_priority() -> None:
    result = subprocess.run(
        ["docker", "compose", "version", "--short"],
        text=True,
        capture_output=True,
        check=True,
    )
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", result.stdout.strip())
    assert match is not None
    version = tuple(int(component) for component in match.groups())
    assert version >= (2, 33, 1)


@pytest.mark.parametrize("environment_name", ["dev", "staging", "prod"])
def test_every_environment_renders_an_isolated_complete_stack(
    environment_name: str,
    render_compose: Callable[..., dict[str, object]],
) -> None:
    config = render_compose(environment_name)
    services = _services(config)

    assert config["name"] == f"gta-{environment_name}"
    assert set(services) == set(ALL_SERVICES)
    assert services["postgres"]["image"] == POSTGRES_REFERENCE
    assert set(config["volumes"]) == {"audio-data", "postgres-data"}
    assert config["volumes"]["audio-data"]["name"] == (
        f"gta-{environment_name}_audio-data"
    )
    assert config["volumes"]["postgres-data"]["name"] == (
        f"gta-{environment_name}_postgres-data"
    )

    expected_nginx_ports = {
        "dev": {8080: "8080", 8443: "8443"},
        "staging": {8080: "8080", 8443: "8443"},
        "prod": {8080: "80", 8443: "443"},
    }
    expected_public_hosts = {
        "dev": "localhost",
        "staging": "gta.staging.example",
        "prod": "gta.example",
    }
    for service_name, service in services.items():
        port_bindings = service.get("ports", [])
        if service_name == "nginx":
            by_target = {binding["target"]: binding for binding in port_bindings}
            assert set(by_target) == {8080, 8443}
            for target, published in expected_nginx_ports[environment_name].items():
                assert str(by_target[target]["published"]) == published
                assert by_target[target]["host_ip"] == "127.0.0.1"
            assert (
                services["nginx"]["environment"]["NGINX_EXTERNAL_HTTPS_PORT"]
                == expected_nginx_ports[environment_name][8443]
            )
            assert (
                services["nginx"]["environment"]["NGINX_PUBLIC_HOST"]
                == (expected_public_hosts[environment_name])
            )
        else:
            assert not port_bindings

    expected_database_url = (
        "postgresql+psycopg://gta:CHANGE_ME@postgres:5432/glpi_ticketing_automation"
    )
    for service_name in (
        "database-migrations",
        "web",
        "worker-automation",
        "worker-retention",
    ):
        assert services[service_name]["environment"]["DATABASE_URL"] == (
            expected_database_url
        )


def test_dependencies_and_networks_gate_application_startup(
    render_compose: Callable[..., dict[str, object]],
) -> None:
    services = _services(render_compose("dev"))

    assert services["database-migrations"]["depends_on"] == {
        "postgres": {
            "condition": "service_healthy",
            "required": True,
        }
    }
    for service_name in ("web", "worker-automation", "worker-retention"):
        assert services[service_name]["depends_on"] == {
            "database-migrations": {
                "condition": "service_completed_successfully",
                "required": True,
            }
        }
    assert services["nginx"]["depends_on"] == {
        "web": {
            "condition": "service_healthy",
            "required": True,
        }
    }

    assert set(services["postgres"]["networks"]) == {"database-private"}
    assert set(services["database-migrations"]["networks"]) == {"database-private"}
    assert set(services["web"]["networks"]) == {
        "database-private",
        "web-private",
    }
    for worker_name in ("worker-automation", "worker-retention"):
        assert set(services[worker_name]["networks"]) == {
            "database-private",
            "worker-egress",
        }
        assert services[worker_name]["networks"]["worker-egress"]["gw_priority"] == 1
    assert set(services["nginx"]["networks"]) == {"proxy-edge", "web-private"}
    assert services["nginx"]["networks"]["proxy-edge"]["gw_priority"] == 1
    assert "database-private" not in services["nginx"]["networks"]


def test_audio_access_is_least_privilege_and_migration_has_no_audio(
    render_compose: Callable[..., dict[str, object]],
) -> None:
    services = _services(render_compose("dev"))

    assert _volume_for(services["database-migrations"], "/data/audios") is None
    web_audio = _volume_for(services["web"], "/data/audios")
    automation_audio = _volume_for(services["worker-automation"], "/data/audios")
    retention_audio = _volume_for(services["worker-retention"], "/data/audios")
    assert web_audio is not None and web_audio.get("read_only", False) is False
    assert automation_audio is not None and automation_audio["read_only"] is True
    assert (
        retention_audio is not None and retention_audio.get("read_only", False) is False
    )
    assert {
        web_audio["source"],
        automation_audio["source"],
        retention_audio["source"],
    } == {"audio-data"}


def test_restart_and_hardening_contracts_are_explicit(
    render_compose: Callable[..., dict[str, object]],
) -> None:
    services = _services(render_compose("dev"))

    assert services["postgres"]["user"] == "70:70"
    assert services["postgres"]["read_only"] is True
    assert services["postgres"]["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in services["postgres"]["security_opt"]
    assert services["postgres"]["restart"] == "unless-stopped"
    assert services["database-migrations"]["restart"] == "no"
    assert services["web"]["restart"] == "unless-stopped"
    assert services["nginx"]["restart"] == "unless-stopped"
    for worker_name in ("worker-automation", "worker-retention"):
        assert services[worker_name]["restart"] == "on-failure:5"
    for service_name in CUSTOM_SERVICES:
        service = services[service_name]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]


def test_dev_builds_locally_while_promoted_environments_are_image_only(
    render_compose: Callable[..., dict[str, object]],
) -> None:
    dev_services = _services(render_compose("dev"))
    for service_name in CUSTOM_SERVICES:
        assert "build" in dev_services[service_name]

    digest_pattern = re.compile(r"@sha256:[0-9a-f]{64}\Z")
    for environment_name in ("staging", "prod"):
        promoted_services = _services(render_compose(environment_name))
        for service_name in CUSTOM_SERVICES:
            service = promoted_services[service_name]
            assert "build" not in service
            assert digest_pattern.search(service["image"])
            assert service["pull_policy"] == "missing"


@pytest.mark.parametrize("environment_name", ["staging", "prod"])
def test_promoted_environment_refuses_unconfirmed_sentinel_digests(
    environment_name: str,
    render_compose: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    rejected = render_compose(
        environment_name,
        release_confirmed=False,
        check=False,
    )
    assert rejected.returncode != 0
    assert "replace zero digests" in rejected.stderr

    versions = (ENVIRONMENTS_ROOT / environment_name / "versions.env").read_text(
        encoding="utf-8"
    )
    assert versions.count(f"@sha256:{ZERO_DIGEST}") == len(CUSTOM_SERVICES)
    assert not any(
        line.startswith("GTA_RELEASE_DIGESTS_CONFIRMED=")
        for line in versions.splitlines()
    )


def test_database_url_is_declared_once_per_environment() -> None:
    for environment_name in ("dev", "staging", "prod"):
        service_root = ENVIRONMENTS_ROOT / environment_name / "services"
        database_example = service_root / "database.env.example"
        assert database_example.read_text(encoding="utf-8").count("DATABASE_URL=") == 1
        for service_name in (
            "migrations",
            "web",
            "worker-automation",
            "worker-retention",
        ):
            assert "DATABASE_URL=" not in (
                service_root / f"{service_name}.env.example"
            ).read_text(encoding="utf-8")


def test_internal_ca_overlay_targets_both_workers_only(
    render_compose: Callable[..., dict[str, object]],
) -> None:
    services = _services(render_compose("dev", include_internal_ca=True))
    target = "/etc/ssl/certs/gta-trust-bundle.pem"
    for worker_name in ("worker-automation", "worker-retention"):
        assert services[worker_name]["environment"]["SSL_CERT_FILE"] == target
        ca_volume = _volume_for(services[worker_name], target)
        assert ca_volume is not None and ca_volume["read_only"] is True
    for service_name in ("postgres", "database-migrations", "web", "nginx"):
        assert "SSL_CERT_FILE" not in services[service_name].get("environment", {})


def test_runtime_environment_examples_contain_no_real_secret() -> None:
    for path in ENVIRONMENTS_ROOT.rglob("*.env*"):
        content = path.read_text(encoding="utf-8")
        if path.name.endswith(".example"):
            assert re.search(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", content) is None
        if path.name.endswith(".example") and "worker" in path.name:
            assert "WORKER_HEALTHCHECK_STALE_AFTER_SECONDS=45" in content


def test_runtime_stack_reports_fresh_workers_through_nginx(
    deployment_environment: DeploymentEnvironment,
) -> None:
    environment = deployment_environment
    with httpx.Client(
        base_url=environment.base_url,
        verify=environment.tls_context(),
        timeout=5,
    ) as anonymous:
        unauthorized = anonymous.get("/api/observability")
    assert unauthorized.status_code == httpx.codes.UNAUTHORIZED

    with environment.client() as client:
        health = client.get("/health")
        deadline = time.monotonic() + 10
        observed: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            response = client.get("/api/observability")
            response.raise_for_status()
            candidate = response.json()
            if all(
                worker["heartbeat_status"] == "fresh" for worker in candidate["workers"]
            ):
                observed = candidate
                break
            time.sleep(0.25)

    assert health.json() == {"status": "ok"}
    assert observed is not None
    assert {worker["service_name"] for worker in observed["workers"]} == {
        "worker_automation",
        "worker_retention",
    }

    migration_state = environment.inspect_service("database-migrations")["State"]
    assert isinstance(migration_state, dict)
    assert migration_state["Status"] == "exited"
    assert migration_state["ExitCode"] == 0


def test_nginx_http_listener_redirects_to_the_external_https_port(
    deployment_environment: DeploymentEnvironment,
) -> None:
    http_port = _published_port_for(deployment_environment, "8080/tcp")
    https_port = _published_port_for(deployment_environment, "8443/tcp")

    response = httpx.get(
        f"http://127.0.0.1:{http_port}/api/jobs?state=pending",
        headers={"Host": "untrusted.invalid"},
        follow_redirects=False,
        timeout=5,
    )

    assert response.status_code == httpx.codes.PERMANENT_REDIRECT
    assert response.headers["location"] == (
        f"https://127.0.0.1:{https_port}/api/jobs?state=pending"
    )
    assert "www-authenticate" not in response.headers


def test_nginx_rejects_an_invalid_external_https_port(
    deployment_environment: DeploymentEnvironment,
) -> None:
    result = deployment_environment.compose(
        "run",
        "--rm",
        "--no-deps",
        "--env",
        "NGINX_EXTERNAL_HTTPS_PORT=0",
        "nginx",
        check=False,
    )

    assert result.returncode != 0
    assert "must be an integer between 1 and 65535" in result.stderr


def test_nginx_rejects_an_unsafe_public_host(
    deployment_environment: DeploymentEnvironment,
) -> None:
    result = deployment_environment.compose(
        "run",
        "--rm",
        "--no-deps",
        "--env",
        "NGINX_PUBLIC_HOST=unsafe.invalid/path",
        "nginx",
        check=False,
    )

    assert result.returncode != 0
    assert "must be a DNS name or IPv4 address" in result.stderr


def test_only_nginx_is_published_and_runtime_restart_is_bounded(
    deployment_environment: DeploymentEnvironment,
) -> None:
    environment = deployment_environment
    for service_name in ALL_SERVICES:
        payload = environment.inspect_service(service_name)
        network = payload["NetworkSettings"]
        assert isinstance(network, dict)
        published = {
            container_port: bindings
            for container_port, bindings in (network.get("Ports") or {}).items()
            if bindings is not None
        }
        if service_name == "nginx":
            assert set(published) == {"8080/tcp", "8443/tcp"}
            assert published["8080/tcp"][0]["HostIp"] == "127.0.0.1"
            assert published["8443/tcp"][0]["HostIp"] == "127.0.0.1"
        else:
            assert published == {}

    expected_policies = {
        "postgres": ("unless-stopped", 0),
        "database-migrations": ("no", 0),
        "web": ("unless-stopped", 0),
        "worker-automation": ("on-failure", 5),
        "worker-retention": ("on-failure", 5),
        "nginx": ("unless-stopped", 0),
    }
    for service_name, (policy_name, maximum_retries) in expected_policies.items():
        host_config = environment.inspect_service(service_name)["HostConfig"]
        assert isinstance(host_config, dict)
        restart_policy = host_config["RestartPolicy"]
        assert restart_policy["Name"] == policy_name
        assert restart_policy["MaximumRetryCount"] == maximum_retries


def test_runtime_images_are_minimal_non_root_and_contain_no_secret(
    deployment_environment: DeploymentEnvironment,
) -> None:
    environment = deployment_environment
    postgres = environment.inspect_service("postgres")
    postgres_config = postgres["Config"]
    postgres_host_config = postgres["HostConfig"]
    assert isinstance(postgres_config, dict)
    assert isinstance(postgres_host_config, dict)
    assert postgres_config["User"] == "70:70"
    assert postgres_host_config["ReadonlyRootfs"] is True
    assert postgres_host_config["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in postgres_host_config["SecurityOpt"]

    expected_users = {
        "database-migrations": "10001:10001",
        "web": "10001:10001",
        "worker-automation": "10001:10001",
        "worker-retention": "10001:10001",
        "nginx": "101:101",
    }
    python_probe = (
        "import importlib.util, os, shutil; from pathlib import Path; "
        "assert os.getuid() == 10001; "
        "assert importlib.util.find_spec('pytest') is None; "
        "assert shutil.which('uv') is None; "
        "assert not Path('/app/.env').exists(); "
        "assert not Path('/app/pyproject.toml').exists(); "
        "assert not Path('/app/apps').exists(); "
        "assert not Path('/app/packages').exists(); "
        "assert not Path('/app/src').exists()"
    )

    for service_name, expected_user in expected_users.items():
        container = environment.inspect_service(service_name)
        host_config = container["HostConfig"]
        assert isinstance(host_config, dict)
        assert host_config["ReadonlyRootfs"] is True
        assert host_config["CapDrop"] == ["ALL"]
        assert "no-new-privileges:true" in host_config["SecurityOpt"]
        image = environment.inspect_image_for_service(service_name)
        image_config = image["Config"]
        assert isinstance(image_config, dict)
        assert image_config["User"] == expected_user
        labels = image_config["Labels"]
        assert labels["org.opencontainers.image.version"] == "smoke"
        assert labels["org.opencontainers.image.revision"] == "0" * 40
        environment.assert_image_contains_no_runtime_secret(service_name)
        if service_name != "nginx":
            environment.run_image_python(service_name, python_probe)


def test_named_audio_permissions_and_secret_mounts_are_effective(
    deployment_environment: DeploymentEnvironment,
) -> None:
    environment = deployment_environment
    write_probe = (
        "from pathlib import Path; "
        "path=Path('/data/audios/.deployment-probe'); "
        "path.write_text('ok'); path.unlink()"
    )
    environment.compose("exec", "-T", "web", "python", "-c", write_probe)
    automation_write = environment.compose(
        "exec",
        "-T",
        "worker-automation",
        "python",
        "-c",
        write_probe,
        check=False,
    )
    assert automation_write.returncode != 0
    environment.compose("exec", "-T", "worker-retention", "python", "-c", write_probe)

    migration_mounts = environment.inspect_service("database-migrations")["Mounts"]
    assert all(mount["Destination"] != "/data/audios" for mount in migration_mounts)
    postgres = environment.inspect_service("postgres")
    postgres_mounts = {mount["Destination"]: mount for mount in postgres["Mounts"]}
    assert postgres_mounts["/run/secrets/postgres_password"]["RW"] is False
    environment.compose(
        "exec",
        "-T",
        "postgres",
        "sh",
        "-ec",
        "test -r /run/secrets/postgres_password",
    )
    assert all(
        secret not in json.dumps(postgres) for secret in environment._runtime_secrets
    )


def test_migration_is_idempotent(
    deployment_environment: DeploymentEnvironment,
) -> None:
    first = deployment_environment.compose("run", "--rm", "database-migrations")
    second = deployment_environment.compose("run", "--rm", "database-migrations")
    assert first.returncode == 0
    assert second.returncode == 0


def test_cooperative_worker_stop_does_not_trigger_restart(
    deployment_environment: DeploymentEnvironment,
) -> None:
    environment = deployment_environment
    environment.stop_service("worker-automation")
    stopped = environment.inspect_service("worker-automation")
    state = stopped["State"]
    assert isinstance(state, dict)
    assert state["Status"] == "exited"
    assert state["ExitCode"] == 0
    assert stopped["RestartCount"] == 0

    environment.start_service("worker-automation")


def test_worker_service_controls_reject_every_non_worker_service(
    deployment_environment: DeploymentEnvironment,
) -> None:
    environment = deployment_environment
    for service_name in ("", "postgres", "web", "nginx", "unknown"):
        with pytest.raises(ValueError, match="not controllable"):
            environment.stop_service(service_name)
        with pytest.raises(ValueError, match="not controllable"):
            environment.start_service(service_name)
        with pytest.raises(ValueError, match="not controllable"):
            environment.crash_service(service_name)


def test_unexpected_worker_failure_restarts_five_times_then_stops(
    deployment_environment: DeploymentEnvironment,
    tmp_path: Path,
) -> None:
    environment = deployment_environment
    project_name = f"{environment.project_name}restart"
    overlay = tmp_path / "worker-failure.yaml"
    overlay.write_text(
        "services:\n"
        "  worker-automation:\n"
        '    entrypoint: ["python", "-c"]\n'
        '    command: ["raise SystemExit(1)"]\n',
        encoding="utf-8",
    )
    try:
        environment.compose(
            "up",
            "--detach",
            "worker-automation",
            project_name=project_name,
            extra_files=(overlay,),
        )
        deadline = time.monotonic() + 30
        terminal: dict[str, object] | None = None
        while time.monotonic() < deadline:
            candidate = environment.inspect_service(
                "worker-automation",
                project_name=project_name,
            )
            state = candidate["State"]
            assert isinstance(state, dict)
            if state["Status"] == "exited" and candidate["RestartCount"] == 5:
                terminal = candidate
                break
            time.sleep(0.25)
        assert terminal is not None
        terminal_state = terminal["State"]
        assert isinstance(terminal_state, dict)
        assert terminal_state["ExitCode"] == 1
    finally:
        environment.compose(
            "down",
            "--volumes",
            "--remove-orphans",
            project_name=project_name,
            extra_files=(overlay,),
            check=False,
        )


def test_failed_migration_gates_every_application_and_cleanup_removes_resources(
    deployment_environment: DeploymentEnvironment,
    tmp_path: Path,
) -> None:
    environment = deployment_environment
    project_name = f"{environment.project_name}fail"
    overlay = tmp_path / "migration-failure.yaml"
    overlay.write_text(
        "services:\n"
        "  database-migrations:\n"
        '    entrypoint: ["python", "-c"]\n'
        '    command: ["raise SystemExit(4)"]\n'
        "  nginx:\n"
        "    environment:\n"
        '      NGINX_EXTERNAL_HTTPS_PORT: "443"\n'
        "    ports: !override\n"
        "      - target: 8080\n"
        '        published: "0"\n'
        '        host_ip: "127.0.0.1"\n'
        "        protocol: tcp\n"
        "      - target: 8443\n"
        '        published: "0"\n'
        '        host_ip: "127.0.0.1"\n'
        "        protocol: tcp\n",
        encoding="utf-8",
    )
    try:
        result = environment.compose(
            "up",
            "--detach",
            project_name=project_name,
            extra_files=(overlay,),
            check=False,
        )
        assert result.returncode != 0
        for service_name in (
            "web",
            "worker-automation",
            "worker-retention",
            "nginx",
        ):
            running_service = environment.compose(
                "ps",
                "--quiet",
                "--status",
                "running",
                service_name,
                project_name=project_name,
                extra_files=(overlay,),
            )
            assert running_service.stdout.strip() == ""
    finally:
        environment.compose(
            "down",
            "--volumes",
            "--remove-orphans",
            project_name=project_name,
            extra_files=(overlay,),
            check=False,
        )
    leftovers = subprocess.run(
        [
            "docker",
            "volume",
            "ls",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project_name}",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    assert leftovers.stdout.strip() == ""


def test_postgres_and_audio_survive_down_without_volume_removal(
    deployment_environment: DeploymentEnvironment,
) -> None:
    environment = deployment_environment
    environment.compose("stop", "worker-automation", "worker-retention")
    with environment.client() as client:
        upload = client.post(
            "/api/jobs",
            files={"audio_file": ("persistent.wav", SYNTHETIC_AUDIO, "audio/wav")},
        )
        upload.raise_for_status()
        audio_uuid = UUID(upload.json()["audio_uuid"])
        location = upload.headers["location"]
        assert client.get(location).status_code == httpx.codes.OK

    audio_probe = (
        "from pathlib import Path; "
        f"raise SystemExit(0 if Path('/data/audios/{audio_uuid}.wav').is_file() else 1)"
    )
    environment.compose("exec", "-T", "web", "python", "-c", audio_probe)
    environment.compose("down", "--remove-orphans", "--timeout", "10")
    environment.start()

    with environment.client() as client:
        persisted = client.get(location)
    assert persisted.status_code == httpx.codes.OK
    environment.compose("exec", "-T", "web", "python", "-c", audio_probe)
