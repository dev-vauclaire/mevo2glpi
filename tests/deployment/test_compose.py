"""Smoke the deployable images and their least-privilege Compose contract."""

from __future__ import annotations

import ssl
import time
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import httpx
import pytest

if TYPE_CHECKING:
    from .conftest import DeploymentEnvironment

pytestmark = [pytest.mark.deployment, pytest.mark.timeout(10 * 60)]


def test_runtime_layout_separates_service_images() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    docker_root = repository_root / "infra" / "docker"
    compose_file = docker_root / "docker-compose.prod.yaml"
    nginx_dockerfile = docker_root / "nginx" / "Dockerfile"
    web_dockerfile = docker_root / "web" / "Dockerfile"
    worker_dockerfile = docker_root / "worker" / "Dockerfile"

    assert compose_file.is_file()
    assert nginx_dockerfile.is_file()
    assert web_dockerfile.is_file()
    assert worker_dockerfile.is_file()
    assert (docker_root / ".env.example").is_file()
    assert (docker_root / "web" / "web.env.example").is_file()
    assert (docker_root / "worker" / "worker.env.example").is_file()
    assert (docker_root / "nginx" / "nginx.conf").is_file()
    assert not (repository_root / "deploy").exists()

    compose_contract = compose_file.read_text(encoding="utf-8")
    assert "context: ./nginx" in compose_contract
    assert "dockerfile: infra/docker/web/Dockerfile" in compose_contract
    assert "dockerfile: infra/docker/worker/Dockerfile" in compose_contract
    assert "target: web-runtime" not in compose_contract
    assert "target: worker-runtime" not in compose_contract

    assert 'ENTRYPOINT ["/usr/sbin/nginx"]' in nginx_dockerfile.read_text(
        encoding="utf-8"
    )
    assert 'CMD ["glpi-ticketing-web"]' in web_dockerfile.read_text(encoding="utf-8")
    assert 'CMD ["glpi-ticketing-worker"]' in worker_dockerfile.read_text(
        encoding="utf-8"
    )


def _assert_runtime_hardening(
    inspect_payload: dict[str, object],
    *,
    restart_policy: str,
    runtime_user: str,
    added_capabilities: frozenset[str] = frozenset(),
) -> None:
    config = inspect_payload["Config"]
    host_config = inspect_payload["HostConfig"]
    assert isinstance(config, dict)
    assert isinstance(host_config, dict)
    healthcheck = config.get("Healthcheck")
    assert isinstance(healthcheck, dict)
    assert healthcheck["Test"][0] == "CMD"
    assert config["User"] == runtime_user
    assert host_config["ReadonlyRootfs"] is True
    assert host_config["CapDrop"] == ["ALL"]
    actual_capabilities = {
        str(capability).removeprefix("CAP_")
        for capability in (host_config["CapAdd"] or [])
    }
    assert actual_capabilities == added_capabilities
    assert "no-new-privileges:true" in host_config["SecurityOpt"]
    assert host_config["RestartPolicy"]["Name"] == restart_policy


def _wait_for_running_worker(environment: DeploymentEnvironment) -> None:
    deadline = time.monotonic() + 15.0
    last_status = "missing"
    while time.monotonic() < deadline:
        payload = environment.inspect_service("worker")
        state = payload["State"]
        assert isinstance(state, dict)
        last_status = str(state["Status"])
        if last_status == "running":
            return
        if last_status in {"dead", "exited"}:
            break
        time.sleep(0.25)
    raise AssertionError(f"worker did not remain running (last status: {last_status})")


def _wait_for_worker_exit_code(
    environment: DeploymentEnvironment,
    expected_exit_code: int,
) -> dict[str, object]:
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        payload = environment.inspect_service("worker")
        state = payload["State"]
        assert isinstance(state, dict)
        if state["Status"] == "exited":
            assert state["ExitCode"] == expected_exit_code
            return payload
        time.sleep(0.25)
    raise AssertionError("worker did not reach its expected terminal state")


def _wait_for_worker_health(
    environment: DeploymentEnvironment,
    expected_health: str,
) -> dict[str, object]:
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        payload = environment.inspect_service("worker")
        state = payload["State"]
        assert isinstance(state, dict)
        health = state.get("Health")
        assert isinstance(health, dict)
        if health["Status"] == expected_health:
            return payload
        time.sleep(0.25)
    raise AssertionError(f"worker did not become {expected_health}")


def test_runtime_compose_smoke(
    deployment_environment: DeploymentEnvironment,
) -> None:
    environment = deployment_environment
    tls_context = ssl.create_default_context(cafile=str(environment.nginx_certificate))

    with httpx.Client(
        base_url=environment.base_url,
        timeout=3.0,
        verify=tls_context,
    ) as anonymous_client:
        unauthorized_response = anonymous_client.get("/")

    with httpx.Client(
        base_url=environment.base_url,
        timeout=3.0,
        verify=tls_context,
        auth=environment.nginx_basic_auth,
    ) as client:
        health_response = client.get("/health")
        redirect_response = client.get("/health/", follow_redirects=False)
        index_response = client.get("/")
        upload_response = client.post(
            "/audios",
            files={"audio_file": ("smoke.wav", b"RIFFsynthetic", "audio/wav")},
        )
        nginx_id_before_web_recreate = environment.inspect_service("nginx")["Id"]
        environment.compose(
            "up",
            "--detach",
            "--no-deps",
            "--force-recreate",
            "web",
        )
        environment.wait_for_healthy("web")
        deadline = time.monotonic() + 15.0
        recovered_health_response: httpx.Response | None = None
        while time.monotonic() < deadline:
            try:
                candidate = client.get("/health")
                if candidate.status_code == httpx.codes.OK:
                    recovered_health_response = candidate
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)

    assert unauthorized_response.status_code == httpx.codes.UNAUTHORIZED
    assert unauthorized_response.headers["www-authenticate"].startswith("Basic")
    assert health_response.json() == {"status": "ok"}
    assert redirect_response.status_code == httpx.codes.TEMPORARY_REDIRECT
    assert redirect_response.headers["location"] == f"{environment.base_url}/health"
    assert recovered_health_response is not None
    assert recovered_health_response.json() == {"status": "ok"}
    assert environment.inspect_service("nginx")["Id"] == nginx_id_before_web_recreate
    assert "Déposer un audio" in index_response.text
    assert 'action="/audios"' in index_response.text
    assert upload_response.status_code == httpx.codes.ACCEPTED

    location = upload_response.headers["location"]
    audio_uuid = UUID(location.removeprefix("/audios/"))
    pending_audio = environment.data_root / "pending" / f"{audio_uuid}.wav"
    assert pending_audio.read_bytes() == b"RIFFsynthetic"
    pending_audio.unlink()

    web_inspect = environment.inspect_service("web")
    _assert_runtime_hardening(
        web_inspect,
        restart_policy="unless-stopped",
        runtime_user="10001:10001",
    )
    network_settings = web_inspect["NetworkSettings"]
    assert isinstance(network_settings, dict)
    port_bindings = network_settings["Ports"]["8000/tcp"]
    assert port_bindings is None
    assert set(network_settings["Networks"]) == {
        f"{environment.project_name}_web-private"
    }

    nginx_inspect = environment.inspect_service("nginx")
    _assert_runtime_hardening(
        nginx_inspect,
        restart_policy="unless-stopped",
        runtime_user="101:101",
    )
    nginx_network_settings = nginx_inspect["NetworkSettings"]
    assert isinstance(nginx_network_settings, dict)
    nginx_ports = nginx_network_settings["Ports"]
    assert nginx_ports["8443/tcp"][0]["HostIp"] == "127.0.0.1"
    assert set(nginx_network_settings["Networks"]) == {
        f"{environment.project_name}_proxy-edge",
        f"{environment.project_name}_web-private",
    }
    nginx_secret_targets = {
        mount["Destination"]: mount["RW"]
        for mount in nginx_inspect["Mounts"]
        if str(mount["Destination"]).startswith("/run/secrets/")
    }
    assert nginx_secret_targets == {
        "/run/secrets/nginx_htpasswd": False,
        "/run/secrets/nginx_tls_certificate": False,
        "/run/secrets/nginx_tls_key": False,
    }
    assert environment.nginx_basic_auth[1] not in str(nginx_inspect)
    environment.compose(
        "exec",
        "-T",
        "nginx",
        "/bin/sh",
        "-c",
        'test "$(id -u)" -eq 101; '
        "test -r /run/secrets/nginx_tls_certificate; "
        "test -r /run/secrets/nginx_tls_key; "
        "test -r /run/secrets/nginx_htpasswd; "
        'probe=$(mktemp /tmp/nginx-smoke.XXXXXX); rm -f "$probe"',
    )
    nginx_rootfs_write = environment.compose(
        "exec",
        "-T",
        "nginx",
        "/bin/sh",
        "-c",
        "touch /etc/nginx/forbidden",
        check=False,
    )
    assert nginx_rootfs_write.returncode != 0
    nginx_logs = environment.compose("logs", "--no-color", "nginx")
    assert environment.nginx_basic_auth[1] not in nginx_logs.stdout

    completed_write = environment.compose(
        "exec",
        "-T",
        "web",
        "python",
        "-c",
        "from pathlib import Path; "
        "Path('/data/audio-completed/forbidden').write_bytes(b'x')",
        check=False,
    )
    assert completed_write.returncode != 0
    environment.compose(
        "exec",
        "-T",
        "web",
        "python",
        "-c",
        "from pathlib import Path; "
        "assert not Path('/data/audio-processing').exists(); "
        "assert not Path('/data/audio-failed').exists()",
    )

    environment.compose("up", "--detach", "--no-deps", "worker")
    _wait_for_running_worker(environment)
    environment.wait_for_healthy("worker")
    environment.assert_worker_received_runtime_secrets()
    worker_inspect = environment.inspect_service("worker")
    _assert_runtime_hardening(
        worker_inspect,
        restart_policy="no",
        runtime_user="10001:10001",
    )
    worker_networks = worker_inspect["NetworkSettings"]
    assert isinstance(worker_networks, dict)
    assert set(worker_networks["Networks"]) == {
        f"{environment.project_name}_worker-egress"
    }

    environment.compose(
        "exec",
        "-T",
        "worker",
        "python",
        "-c",
        "from pathlib import Path; "
        "paths = [Path('/data') / f'audio-{name}' "
        "for name in ('pending', 'processing', 'completed', 'failed')]; "
        "[(path / '.smoke').write_bytes(b'ok') for path in paths]; "
        "[(path / '.smoke').unlink() for path in paths]",
    )

    for service_name in ("web", "worker"):
        environment.compose(
            "exec",
            "-T",
            service_name,
            "python",
            "-c",
            "import importlib.util, shutil; from pathlib import Path; "
            "assert importlib.util.find_spec('pytest') is None; "
            "assert importlib.util.find_spec('httpx2') is None; "
            "assert shutil.which('uv') is None; "
            "assert not Path('/app/.env').exists(); "
            "assert not Path('/app/pyproject.toml').exists()",
        )

    for image_reference in (
        environment.nginx_image,
        environment.web_image,
        environment.worker_image,
    ):
        image_metadata = environment.inspect_image(image_reference)
        image_config = image_metadata["Config"]
        assert isinstance(image_config, dict)
        image_labels = image_config["Labels"]
        assert isinstance(image_labels, dict)
        assert image_labels["org.opencontainers.image.version"] == "smoke"
        assert image_labels["org.opencontainers.image.revision"] == "0" * 40
        environment.assert_image_contains_no_runtime_secret(image_reference)

    environment.compose("stop", "--timeout", "15", "worker")
    stopped_worker = environment.inspect_service("worker")
    stopped_state = stopped_worker["State"]
    assert isinstance(stopped_state, dict)
    assert stopped_state["Status"] == "exited"
    assert stopped_state["ExitCode"] == 0
    assert stopped_worker["RestartCount"] == 0

    frontend_ids_before_worker_guards = {
        service_name: environment.inspect_service(service_name)["Id"]
        for service_name in ("web", "nginx")
    }

    environment.replace_worker_with_failing_healthcheck()
    unhealthy_worker = _wait_for_worker_health(environment, "unhealthy")
    unhealthy_start = environment.start_runtime(check=False)
    assert unhealthy_start.returncode == 1
    assert "refusing running worker with health=unhealthy" in unhealthy_start.stderr
    unhealthy_worker_after_guard = environment.inspect_service("worker")
    assert unhealthy_worker_after_guard["Id"] == unhealthy_worker["Id"]
    unhealthy_state_after_guard = unhealthy_worker_after_guard["State"]
    assert isinstance(unhealthy_state_after_guard, dict)
    assert unhealthy_state_after_guard["Status"] == "running"
    assert unhealthy_state_after_guard["Health"]["Status"] == "unhealthy"
    assert {
        service_name: environment.inspect_service(service_name)["Id"]
        for service_name in ("web", "nginx")
    } == frontend_ids_before_worker_guards

    environment.replace_worker_with_exit_code(3)
    blocked_worker = _wait_for_worker_exit_code(environment, 3)
    guarded_start = environment.start_runtime(check=False)
    assert guarded_start.returncode == 3
    assert "refusing to restart worker" in guarded_start.stderr
    blocked_worker_after_guard = environment.inspect_service("worker")
    assert blocked_worker_after_guard["Id"] == blocked_worker["Id"]
    blocked_state_after_guard = blocked_worker_after_guard["State"]
    assert isinstance(blocked_state_after_guard, dict)
    assert blocked_state_after_guard["Status"] == "exited"
    assert blocked_state_after_guard["ExitCode"] == 3
    assert blocked_worker_after_guard["RestartCount"] == 0
    assert {
        service_name: environment.inspect_service(service_name)["Id"]
        for service_name in ("web", "nginx")
    } == frontend_ids_before_worker_guards
    assert not (environment.data_root / "completed" / "forbidden").exists()
    assert all(
        not (environment.data_root / name / ".smoke").exists()
        for name in ("pending", "processing", "completed", "failed")
    )
    assert all(
        not tuple((environment.data_root / name).glob(".gta-health.*"))
        for name in ("pending", "processing", "completed", "failed")
    )
    assert environment.data_root.is_dir()
