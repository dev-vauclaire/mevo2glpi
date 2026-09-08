"""Unit contracts for publisher environment configuration."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from glpi_ticketing_worker_publisher.settings import (
    MAX_ATTACHMENT_BYTES,
    PublisherLogLevel,
    PublisherSettings,
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
