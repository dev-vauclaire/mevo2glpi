"""Smoke-test the complete MVP topology without creating a business job."""

from __future__ import annotations

import time
from typing import Any

import psycopg
import pytest
from tests.support.mvp_compose import (
    ALL_SERVICES,
    MvpComposeEnvironment,
)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(45 * 60)]


def test_mvp_stack_is_healthy_and_connected_to_real_provider_networks(
    e2e_environment: MvpComposeEnvironment,
    glpi_environment,
    transcribe_ai_environment,
) -> None:
    """Validate direct HTTP readiness, worker heartbeats and provider topology."""
    environment = e2e_environment

    observed: dict[str, Any] | None = None
    with environment.client() as client:
        health = client.get("/health")
        health.raise_for_status()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            response = client.get("/api/observability")
            response.raise_for_status()
            candidate = response.json()
            workers_by_service = {
                worker["service_name"]: worker for worker in candidate["workers"]
            }
            downstream_workers_are_ready = all(
                workers_by_service.get(service_name, {}).get("heartbeat_status")
                == "fresh"
                for service_name in ("worker_automation", "worker_retention")
            )
            publisher_is_intentionally_absent = (
                workers_by_service.get("worker_publisher", {}).get("heartbeat_status")
                == "absent"
            )
            if downstream_workers_are_ready and publisher_is_intentionally_absent:
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
    publisher = next(
        worker
        for worker in observed["workers"]
        if worker["service_name"] == "worker_publisher"
    )
    assert publisher["heartbeat_status"] == "absent"
    assert publisher["runtime_state"] is None
    assert publisher["current_ingestion_uuid"] is None
    assert any(
        alert["key"] == "workers:worker_publisher"
        and alert["kind"] == "worker_absent"
        and alert["severity"] == "critical"
        for alert in observed["alerts"]
    )

    migration_state = environment.inspect_service("database-migrations")["State"]
    assert isinstance(migration_state, dict)
    assert migration_state["Status"] == "exited"
    assert migration_state["ExitCode"] == 0

    service_networks = {
        service_name: _network_names(environment, service_name)
        for service_name in ALL_SERVICES
    }
    control_network = f"{environment.project_name}_e2e-control"
    glpi_network = glpi_environment.network_name
    transcribe_network = transcribe_ai_environment.network_name
    assert control_network in service_networks["postgres"]
    for service_name in ALL_SERVICES[1:]:
        assert control_network not in service_networks[service_name]
    assert {glpi_network, transcribe_network} <= service_networks["worker-automation"]
    assert glpi_network in service_networks["worker-retention"]
    assert transcribe_network not in service_networks["worker-retention"]
    for service_name in ("postgres", "database-migrations", "web"):
        assert glpi_network not in service_networks[service_name]
        assert transcribe_network not in service_networks[service_name]

    _assert_tcp_reachable(environment, "worker-automation", "api", 5000)
    _assert_tcp_reachable(environment, "worker-automation", "glpi", 80)
    _assert_tcp_reachable(environment, "worker-retention", "glpi", 80)

    _assert_test_only_ports_are_bound_to_loopback(environment)
    with psycopg.connect(
        environment.psycopg_database_url,
        connect_timeout=3,
    ) as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)


def _network_names(
    environment: MvpComposeEnvironment,
    service_name: str,
) -> set[str]:
    network_settings = environment.inspect_service(service_name)["NetworkSettings"]
    assert isinstance(network_settings, dict)
    networks = network_settings["Networks"]
    assert isinstance(networks, dict)
    return set(networks)


def _assert_tcp_reachable(
    environment: MvpComposeEnvironment,
    service_name: str,
    host: str,
    port: int,
) -> None:
    source = (
        "import socket; "
        f"connection=socket.create_connection(({host!r}, {port}), timeout=3); "
        "connection.close()"
    )
    environment.compose(
        "exec",
        "-T",
        service_name,
        "python",
        "-c",
        source,
    )


def _assert_test_only_ports_are_bound_to_loopback(
    environment: MvpComposeEnvironment,
) -> None:
    for service_name in ALL_SERVICES:
        network_settings = environment.inspect_service(service_name)["NetworkSettings"]
        assert isinstance(network_settings, dict)
        ports = network_settings.get("Ports") or {}
        published = {
            container_port: bindings
            for container_port, bindings in ports.items()
            if bindings is not None
        }
        if service_name == "web":
            assert set(published) == {"8000/tcp"}
        elif service_name == "postgres":
            assert set(published) == {"5432/tcp"}
        else:
            assert published == {}
        for bindings in published.values():
            assert len(bindings) == 1
            assert bindings[0]["HostIp"] == "127.0.0.1"
