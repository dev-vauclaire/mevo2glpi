"""Unit contracts for publisher environment configuration."""

from pathlib import Path

import pytest
from pydantic import ValidationError

import glpi_ticketing_worker_publisher.settings as settings_module
from glpi_ticketing_worker_publisher.settings import (
    MAX_ATTACHMENT_BYTES,
    OFFICIAL_GRAPH_BASE_URL,
    PublisherGraphAuthMode,
    PublisherLogLevel,
    PublisherSettings,
)

ENTRA_VARIABLES = (
    "PUBLISHER_ENTRA_TENANT_ID",
    "PUBLISHER_ENTRA_CLIENT_ID",
    "PUBLISHER_ENTRA_PRIVATE_KEY_PEM_PATH",
    "PUBLISHER_ENTRA_CERTIFICATE_PEM_PATH",
    "PUBLISHER_ENTRA_PRIVATE_KEY_PASSPHRASE",
)


@pytest.fixture
def publisher_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> dict[str, Path]:
    private_key = tmp_path / "private-key.pem"
    certificate = tmp_path / "certificate.pem"
    private_key.write_text(
        "-----BEGIN PRIVATE KEY-----\nsynthetic\n-----END PRIVATE KEY-----\n",
        encoding="ascii",
    )
    certificate.write_text(
        "-----BEGIN CERTIFICATE-----\nsynthetic\n-----END CERTIFICATE-----\n",
        encoding="ascii",
    )
    values = {
        "PUBLISHER_ENTRA_TENANT_ID": "00000000-0000-4000-8000-000000000001",
        "PUBLISHER_ENTRA_CLIENT_ID": "00000000-0000-4000-8000-000000000002",
        "PUBLISHER_MAILBOX_ID": "00000000-0000-4000-8000-000000000003",
        "PUBLISHER_ENTRA_PRIVATE_KEY_PEM_PATH": str(private_key),
        "PUBLISHER_ENTRA_CERTIFICATE_PEM_PATH": str(certificate),
        "DATABASE_URL": "postgresql+psycopg://gta:synthetic@db/gta",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return {"private_key": private_key, "certificate": certificate}


def test_settings_load_two_distinct_pem_paths_and_runtime_defaults(
    publisher_environment: dict[str, Path],
) -> None:
    settings = PublisherSettings()

    assert (
        settings.publisher_entra_private_key_pem_path
        == (publisher_environment["private_key"])
    )
    assert (
        settings.publisher_entra_certificate_pem_path
        == (publisher_environment["certificate"])
    )
    assert settings.publisher_entra_private_key_passphrase is None
    assert settings.publisher_graph_auth_mode is (
        PublisherGraphAuthMode.ENTRA_CERTIFICATE
    )
    assert str(settings.publisher_graph_base_url).rstrip("/") == (
        OFFICIAL_GRAPH_BASE_URL
    )
    assert settings.publisher_log_level is PublisherLogLevel.INFO
    assert settings.publisher_max_attachment_bytes == MAX_ATTACHMENT_BYTES


def test_private_key_passphrase_is_preserved_verbatim(
    publisher_environment: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUBLISHER_ENTRA_PRIVATE_KEY_PASSPHRASE", "  secret phrase  ")

    settings = PublisherSettings()

    assert settings.publisher_entra_private_key_passphrase is not None
    assert (
        settings.publisher_entra_private_key_passphrase.get_secret_value()
        == "  secret phrase  "
    )
    assert "secret phrase" not in repr(settings)


def test_empty_private_key_passphrase_is_treated_as_absent(
    publisher_environment: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUBLISHER_ENTRA_PRIVATE_KEY_PASSPHRASE", "")

    assert PublisherSettings().publisher_entra_private_key_passphrase is None


def test_pem_paths_must_be_distinct(
    publisher_environment: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "PUBLISHER_ENTRA_CERTIFICATE_PEM_PATH",
        str(publisher_environment["private_key"]),
    )

    with pytest.raises(ValidationError):
        PublisherSettings()


@pytest.mark.parametrize("database_url", ["invalid", "postgresql://db/gta"])
def test_database_url_requires_the_synchronous_psycopg_driver(
    publisher_environment: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)

    with pytest.raises(ValidationError):
        PublisherSettings()


def test_retry_initial_must_not_exceed_retry_maximum(
    publisher_environment: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUBLISHER_RETRY_INITIAL_SECONDS", "31")
    monkeypatch.setenv("PUBLISHER_RETRY_MAX_SECONDS", "30")

    with pytest.raises(ValidationError):
        PublisherSettings()


def test_synthetic_graph_mode_requires_no_entra_credentials(
    publisher_environment: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del publisher_environment
    for variable_name in ENTRA_VARIABLES:
        monkeypatch.delenv(variable_name, raising=False)
    monkeypatch.setenv("PUBLISHER_GRAPH_AUTH_MODE", "synthetic")
    monkeypatch.setenv(
        "PUBLISHER_GRAPH_BASE_URL",
        "http://provider-simulator:8080/v1.0",
    )

    settings = PublisherSettings()

    assert settings.publisher_graph_auth_mode is PublisherGraphAuthMode.SYNTHETIC
    assert str(settings.publisher_graph_base_url).rstrip("/") == (
        "http://provider-simulator:8080/v1.0"
    )
    assert settings.publisher_entra_tenant_id is None
    assert settings.publisher_entra_client_id is None
    assert settings.publisher_entra_private_key_pem_path is None
    assert settings.publisher_entra_certificate_pem_path is None


def test_synthetic_graph_mode_rejects_entra_settings_without_reading_pem_files(
    publisher_environment: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del publisher_environment
    monkeypatch.setenv("PUBLISHER_GRAPH_AUTH_MODE", "synthetic")
    monkeypatch.setenv(
        "PUBLISHER_GRAPH_BASE_URL",
        "http://provider-simulator:8080/v1.0",
    )
    monkeypatch.setenv(
        "PUBLISHER_ENTRA_PRIVATE_KEY_PEM_PATH",
        "/secrets/must-not-be-read/private.pem",
    )
    monkeypatch.setenv(
        "PUBLISHER_ENTRA_CERTIFICATE_PEM_PATH",
        "/secrets/must-not-be-read/certificate.pem",
    )

    def fail_if_read(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("synthetic mode must not inspect PEM files")

    monkeypatch.setattr(settings_module, "_validate_readable_file", fail_if_read)

    with pytest.raises(ValidationError, match="must not receive Entra credentials"):
        PublisherSettings()


def test_synthetic_graph_mode_refuses_the_official_graph_endpoint(
    publisher_environment: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del publisher_environment
    for variable_name in ENTRA_VARIABLES:
        monkeypatch.delenv(variable_name, raising=False)
    monkeypatch.setenv("PUBLISHER_GRAPH_AUTH_MODE", "synthetic")

    with pytest.raises(ValidationError, match="requires a non-Microsoft URL"):
        PublisherSettings()


def test_certificate_graph_mode_refuses_a_non_microsoft_endpoint(
    publisher_environment: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del publisher_environment
    monkeypatch.setenv(
        "PUBLISHER_GRAPH_BASE_URL",
        "https://graph-staging.example.test/v1.0",
    )

    with pytest.raises(ValidationError, match="official Microsoft Graph URL"):
        PublisherSettings()


def test_certificate_graph_mode_requires_all_entra_material(
    publisher_environment: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del publisher_environment
    monkeypatch.delenv("PUBLISHER_ENTRA_CLIENT_ID")

    with pytest.raises(ValidationError, match="requires tenant, client and PEM files"):
        PublisherSettings()
