"""Validate the autonomous Keycloak laboratory used for local learning."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.deployment

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEVELOPMENT_ROOT = REPOSITORY_ROOT / "infra" / "docker" / "development"
COMPOSE_FILE = DEVELOPMENT_ROOT / "docker-compose.keycloak.yml"
ENV_EXAMPLE = DEVELOPMENT_ROOT / "keycloak.env.example"
REALM_FILE = DEVELOPMENT_ROOT / "keycloak" / "mevo2glpi-development-realm.json"
KEYCLOAK_IMAGE = (
    "quay.io/keycloak/keycloak:26.7.0@sha256:"
    "0f198be292568439d700cdbfb893e69a6009bb43a94a06a945b1d3d506c76b13"
)
POSTGRES_IMAGE = (
    "postgres:18.6-alpine3.24@sha256:"
    "10a5c69a83035ab5239f5620d708520e8b28f5aa1f2b20143e6024532edadfb5"
)


def _example_environment() -> dict[str, str]:
    source = ENV_EXAMPLE.read_text(encoding="utf-8")
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", source, re.MULTILINE))


def _clean_environment(*, published_port: str | None = None) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("COMPOSE_")
        and not name.startswith("KEYCLOAK_DEV_")
        and name != "MEVO2GLPI_DEV_KEYCLOAK_PORT"
    }
    environment.update(_example_environment())
    if published_port is not None:
        environment["MEVO2GLPI_DEV_KEYCLOAK_PORT"] = published_port
    return environment


def _render_compose(*, published_port: str | None = None) -> dict[str, object]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV_EXAMPLE),
            "--file",
            str(COMPOSE_FILE),
            "config",
            "--format",
            "json",
        ],
        cwd=REPOSITORY_ROOT,
        env=_clean_environment(published_port=published_port),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(
            "Docker Compose could not render the development Keycloak manifest "
            f"(exit_code={result.returncode}): {result.stderr.strip()}"
        )
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


@pytest.fixture(scope="module")
def keycloak_compose_config() -> dict[str, object]:
    """Render the manifest once without pulling or starting containers."""
    return _render_compose()


@pytest.fixture(scope="module")
def keycloak_realm() -> dict[str, Any]:
    """Load the versioned, synthetic realm imported by the local laboratory."""
    payload = json.loads(REALM_FILE.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _services(config: dict[str, object]) -> dict[str, dict[str, Any]]:
    services = config.get("services")
    assert isinstance(services, dict)
    return services


def test_keycloak_lab_is_autonomous_and_uses_pinned_images(
    keycloak_compose_config: dict[str, object],
) -> None:
    assert keycloak_compose_config["name"] == "mevo2glpi-development-keycloak"
    services = _services(keycloak_compose_config)
    assert set(services) == {"keycloak", "keycloak-db"}
    assert services["keycloak"]["image"] == KEYCLOAK_IMAGE
    assert services["keycloak-db"]["image"] == POSTGRES_IMAGE
    assert all(
        "latest" not in service["image"].casefold() for service in services.values()
    )


def test_keycloak_uses_development_mode_and_a_dedicated_database(
    keycloak_compose_config: dict[str, object],
) -> None:
    services = _services(keycloak_compose_config)
    keycloak = services["keycloak"]
    database = services["keycloak-db"]

    assert keycloak["command"] == ["start-dev", "--import-realm"]
    assert keycloak["environment"] == {
        "KC_BOOTSTRAP_ADMIN_PASSWORD": "development-only-admin",
        "KC_BOOTSTRAP_ADMIN_USERNAME": "admin",
        "KC_DB": "postgres",
        "KC_DB_PASSWORD": "development-only-database",
        "KC_DB_URL": "jdbc:postgresql://keycloak-db:5432/keycloak",
        "KC_DB_USERNAME": "keycloak",
        "KC_HEALTH_ENABLED": "true",
        "KC_HOSTNAME": "http://localhost:18084",
        "KEYCLOAK_DEV_AUTHORIZED_PASSWORD": "development-only-authorized",
        "KEYCLOAK_DEV_CLIENT_SECRET": "development-only-client-secret",
        "KEYCLOAK_DEV_FORBIDDEN_PASSWORD": "development-only-forbidden",
    }
    assert database["environment"] == {
        "POSTGRES_DB": "keycloak",
        "POSTGRES_PASSWORD": "development-only-database",
        "POSTGRES_USER": "keycloak",
    }
    assert keycloak["depends_on"] == {
        "keycloak-db": {
            "condition": "service_healthy",
            "required": True,
        }
    }


def test_keycloak_is_exposed_only_on_loopback_and_database_is_not_published(
    keycloak_compose_config: dict[str, object],
) -> None:
    services = _services(keycloak_compose_config)
    assert services["keycloak"]["ports"] == [
        {
            "host_ip": "127.0.0.1",
            "mode": "ingress",
            "protocol": "tcp",
            "published": "18084",
            "target": 8080,
        }
    ]
    assert "ports" not in services["keycloak-db"]
    assert keycloak_compose_config["networks"] == {
        "keycloak-database": {
            "ipam": {},
            "name": "mevo2glpi-development-keycloak-database-network",
        }
    }


def test_keycloak_imports_versioned_realm_read_only(
    keycloak_compose_config: dict[str, object],
) -> None:
    keycloak = _services(keycloak_compose_config)["keycloak"]

    assert REALM_FILE.name == "mevo2glpi-development-realm.json"
    assert keycloak["volumes"] == [
        {
            "type": "bind",
            "source": str(REALM_FILE),
            "target": ("/opt/keycloak/data/import/mevo2glpi-development-realm.json"),
            "read_only": True,
        }
    ]


def test_keycloak_port_override_updates_mapping_and_hostname() -> None:
    config = _render_compose(published_port="28084")
    keycloak = _services(config)["keycloak"]

    assert keycloak["ports"][0]["published"] == "28084"
    assert keycloak["environment"]["KC_HOSTNAME"] == "http://localhost:28084"


def test_keycloak_and_postgres_have_readiness_probes(
    keycloak_compose_config: dict[str, object],
) -> None:
    services = _services(keycloak_compose_config)
    keycloak_probe = services["keycloak"]["healthcheck"]
    database_probe = services["keycloak-db"]["healthcheck"]

    assert keycloak_probe["test"][:3] == ["CMD", "bash", "-ec"]
    assert "/health/ready" in keycloak_probe["test"][3]
    assert "127.0.0.1/9000" in keycloak_probe["test"][3]
    assert database_probe["test"] == [
        "CMD-SHELL",
        (
            "pg_isready --host=127.0.0.1 --username=$$POSTGRES_USER "
            "--dbname=$$POSTGRES_DB"
        ),
    ]


def test_keycloak_database_is_persistent_and_reset_is_isolated(
    keycloak_compose_config: dict[str, object],
) -> None:
    database = _services(keycloak_compose_config)["keycloak-db"]

    assert database["volumes"] == [
        {
            "source": "keycloak-database-data",
            "target": "/var/lib/postgresql",
            "type": "volume",
            "volume": {},
        }
    ]
    assert keycloak_compose_config["volumes"] == {
        "keycloak-database-data": {
            "name": "mevo2glpi-development-keycloak-database-data"
        }
    }


def test_keycloak_example_contains_only_explicit_local_values() -> None:
    environment = _example_environment()

    assert environment == {
        "KEYCLOAK_DEV_ADMIN_PASSWORD": "development-only-admin",
        "KEYCLOAK_DEV_ADMIN_USERNAME": "admin",
        "KEYCLOAK_DEV_AUTHORIZED_PASSWORD": "development-only-authorized",
        "KEYCLOAK_DEV_CLIENT_SECRET": "development-only-client-secret",
        "KEYCLOAK_DEV_DB_NAME": "keycloak",
        "KEYCLOAK_DEV_DB_PASSWORD": "development-only-database",
        "KEYCLOAK_DEV_DB_USERNAME": "keycloak",
        "KEYCLOAK_DEV_FORBIDDEN_PASSWORD": "development-only-forbidden",
        "MEVO2GLPI_DEV_KEYCLOAK_PORT": "18084",
    }
    assert all(
        "development" in value or value in {"admin", "keycloak", "18084"}
        for value in environment.values()
    )


def test_keycloak_realm_has_restrictive_authentication_defaults(
    keycloak_realm: dict[str, Any],
) -> None:
    assert keycloak_realm["realm"] == "mevo2glpi-development"
    assert keycloak_realm["enabled"] is True
    assert keycloak_realm["sslRequired"] == "external"
    assert keycloak_realm["ssoSessionIdleTimeout"] == 1800
    for setting in (
        "registrationAllowed",
        "registrationEmailAsUsername",
        "rememberMe",
        "verifyEmail",
        "loginWithEmailAllowed",
        "duplicateEmailsAllowed",
        "resetPasswordAllowed",
        "editUsernameAllowed",
    ):
        assert keycloak_realm[setting] is False


def test_keycloak_realm_configures_confidential_authorization_code_client(
    keycloak_realm: dict[str, Any],
) -> None:
    assert len(keycloak_realm["clients"]) == 1
    client = keycloak_realm["clients"][0]

    assert client["clientId"] == "mevo2glpi-web"
    assert client["protocol"] == "openid-connect"
    assert client["clientAuthenticatorType"] == "client-secret"
    assert client["secret"] == "${KEYCLOAK_DEV_CLIENT_SECRET}"
    assert client["enabled"] is True
    assert client["publicClient"] is False
    assert client["bearerOnly"] is False
    assert client["standardFlowEnabled"] is True
    assert client["implicitFlowEnabled"] is False
    assert client["directAccessGrantsEnabled"] is False
    assert client["serviceAccountsEnabled"] is False
    assert client["authorizationServicesEnabled"] is False
    assert client["fullScopeAllowed"] is False
    assert client["redirectUris"] == ["http://127.0.0.1:8000/auth/callback"]
    assert client["webOrigins"] == ["http://127.0.0.1:8000"]
    assert all("*" not in uri for uri in client["redirectUris"])
    assert client["attributes"] == {
        "pkce.code.challenge.method": "S256",
        "post.logout.redirect.uris": "http://127.0.0.1:8000/",
    }


def test_keycloak_realm_exposes_only_mevo2glpi_client_role(
    keycloak_realm: dict[str, Any],
) -> None:
    role = keycloak_realm["roles"]["client"]["mevo2glpi-web"]
    assert role == [
        {
            "name": "mevo2glpi-access",
            "description": "Allow access to the mevo2glpi dashboard.",
            "composite": False,
            "clientRole": True,
        }
    ]
    assert keycloak_realm["clientScopeMappings"] == {
        "mevo2glpi-web": [
            {
                "client": "mevo2glpi-web",
                "roles": ["mevo2glpi-access"],
            }
        ]
    }

    client = keycloak_realm["clients"][0]
    assert client["protocolMappers"] == [
        {
            "name": "mevo2glpi client roles",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-client-role-mapper",
            "consentRequired": False,
            "config": {
                "usermodel.clientRoleMapping.clientId": "mevo2glpi-web",
                "claim.name": "roles",
                "jsonType.label": "String",
                "multivalued": "true",
                "id.token.claim": "true",
                "access.token.claim": "true",
                "userinfo.token.claim": "true",
            },
        }
    ]


def test_keycloak_realm_contains_only_synthetic_test_users(
    keycloak_realm: dict[str, Any],
) -> None:
    users = {user["username"]: user for user in keycloak_realm["users"]}
    assert set(users) == {"operator-authorized", "operator-forbidden"}
    assert users["operator-authorized"]["clientRoles"] == {
        "mevo2glpi-web": ["mevo2glpi-access"]
    }
    assert "clientRoles" not in users["operator-forbidden"]
    assert users["operator-authorized"]["credentials"] == [
        {
            "type": "password",
            "value": "${KEYCLOAK_DEV_AUTHORIZED_PASSWORD}",
            "temporary": False,
        }
    ]
    assert users["operator-forbidden"]["credentials"] == [
        {
            "type": "password",
            "value": "${KEYCLOAK_DEV_FORBIDDEN_PASSWORD}",
            "temporary": False,
        }
    ]
    for user in users.values():
        assert user["enabled"] is True
        assert {"email", "firstName", "lastName"}.isdisjoint(user)
