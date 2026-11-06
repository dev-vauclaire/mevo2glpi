"""Unit contracts for certificate-backed Entra ID authentication."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr

from mevo2glpi_worker_publisher.graph.auth import (
    ENTRA_AUTHORITY_ROOT,
    GRAPH_DEFAULT_SCOPE,
    SYNTHETIC_GRAPH_ACCESS_TOKEN,
    MsalCertificateTokenProvider,
    SyntheticGraphTokenProvider,
)
from mevo2glpi_worker_publisher.graph.contracts import GraphAuthenticationError
from mevo2glpi_worker_publisher.graph.models import GraphErrorCategory

TENANT_ID = UUID("6b16a39f-72ca-4f20-9eb2-55e984eaab0f")
CLIENT_ID = UUID("550ff65d-cec5-45d8-af03-9a51af8feb17")
PEM_PASSPHRASE = "  synthetic pem passphrase  "
PRIVATE_KEY_PEM = """-----BEGIN ENCRYPTED PRIVATE KEY-----
synthetic-private-key
-----END ENCRYPTED PRIVATE KEY-----
"""
CERTIFICATE_PEM = """-----BEGIN CERTIFICATE-----
synthetic-x509-certificate
-----END CERTIFICATE-----
"""
ACCESS_TOKEN = "synthetic-graph-access-token"
SENSITIVE_DESCRIPTION = "sensitive identity-provider description"


class FakeMsalApplication:
    def __init__(self, result: object) -> None:
        self.result = result
        self.scopes: list[list[str]] = []
        self.invalidations = 0

    def acquire_token_for_client(self, scopes: list[str]) -> Any:
        self.scopes.append(scopes)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def remove_tokens_for_client(self) -> None:
        self.invalidations += 1


def test_synthetic_provider_returns_the_known_non_secret_staging_token() -> None:
    provider = SyntheticGraphTokenProvider()

    token = provider.get_access_token()
    provider.invalidate()

    assert token.value.get_secret_value() == SYNTHETIC_GRAPH_ACCESS_TOKEN
    assert token.expires_in_seconds is None
    assert SYNTHETIC_GRAPH_ACCESS_TOKEN not in repr(token)


def test_from_pem_configures_msal_with_separate_files_and_verbatim_passphrase(
    tmp_path: Path,
) -> None:
    private_key_path = tmp_path / "private-key.pem"
    certificate_path = tmp_path / "certificate.pem"
    private_key_path.write_text(PRIVATE_KEY_PEM, encoding="ascii")
    certificate_path.write_text(CERTIFICATE_PEM, encoding="ascii")
    application = FakeMsalApplication({"access_token": ACCESS_TOKEN})
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def factory(*args: object, **kwargs: object) -> FakeMsalApplication:
        calls.append((args, kwargs))
        return application

    provider = MsalCertificateTokenProvider.from_pem(
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        private_key_pem_path=private_key_path,
        certificate_pem_path=certificate_path,
        passphrase=SecretStr(PEM_PASSPHRASE),
        application_factory=factory,
    )

    assert isinstance(provider, MsalCertificateTokenProvider)
    assert calls == [
        (
            (str(CLIENT_ID),),
            {
                "authority": f"{ENTRA_AUTHORITY_ROOT}/{TENANT_ID}",
                "client_credential": {
                    "private_key": PRIVATE_KEY_PEM,
                    "public_certificate": CERTIFICATE_PEM,
                    "passphrase": PEM_PASSPHRASE,
                },
                "enable_pii_log": False,
                "timeout": 10.0,
            },
        )
    ]
    assert private_key_path.as_posix() not in repr(calls)


def test_from_pem_omits_passphrase_for_an_unencrypted_key(tmp_path: Path) -> None:
    private_key_path = tmp_path / "private-key.pem"
    certificate_path = tmp_path / "certificate.pem"
    private_key_path.write_text(PRIVATE_KEY_PEM, encoding="ascii")
    certificate_path.write_text(CERTIFICATE_PEM, encoding="ascii")
    calls: list[dict[str, object]] = []

    def factory(*_args: object, **kwargs: object) -> FakeMsalApplication:
        calls.append(kwargs)
        return FakeMsalApplication({"access_token": ACCESS_TOKEN})

    MsalCertificateTokenProvider.from_pem(
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        private_key_pem_path=private_key_path,
        certificate_pem_path=certificate_path,
        application_factory=factory,
    )

    assert "passphrase" not in calls[0]["client_credential"]


def test_from_pem_rejects_a_raw_public_key_instead_of_x509_certificate(
    tmp_path: Path,
) -> None:
    private_key_path = tmp_path / "private-key.pem"
    certificate_path = tmp_path / "public-key.pem"
    private_key_path.write_text(PRIVATE_KEY_PEM, encoding="ascii")
    certificate_path.write_text(
        "-----BEGIN PUBLIC KEY-----\nsynthetic\n-----END PUBLIC KEY-----\n",
        encoding="ascii",
    )

    with pytest.raises(GraphAuthenticationError) as captured_error:
        MsalCertificateTokenProvider.from_pem(
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            private_key_pem_path=private_key_path,
            certificate_pem_path=certificate_path,
            application_factory=lambda *_args, **_kwargs: FakeMsalApplication({}),
        )

    assert captured_error.value.technical_code == "invalid_x509_certificate_pem"
    assert captured_error.value.retryable is False


def test_from_pem_treats_non_ascii_certificate_as_terminal_configuration(
    tmp_path: Path,
) -> None:
    private_key_path = tmp_path / "private-key.pem"
    certificate_path = tmp_path / "certificate.pem"
    private_key_path.write_text(PRIVATE_KEY_PEM, encoding="ascii")
    certificate_path.write_bytes(b"\xff")

    with pytest.raises(GraphAuthenticationError) as captured_error:
        MsalCertificateTokenProvider.from_pem(
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            private_key_pem_path=private_key_path,
            certificate_pem_path=certificate_path,
            application_factory=lambda *_args, **_kwargs: FakeMsalApplication({}),
        )

    assert captured_error.value.technical_code == "invalid_x509_certificate_pem"
    assert captured_error.value.retryable is False


def test_get_access_token_uses_application_default_scope_and_preserves_expiry() -> None:
    application = FakeMsalApplication(
        {"access_token": ACCESS_TOKEN, "expires_in": 3600}
    )
    provider = MsalCertificateTokenProvider(application)

    token = provider.get_access_token()

    assert token.value.get_secret_value() == ACCESS_TOKEN
    assert token.expires_in_seconds == 3600
    assert application.scopes == [[GRAPH_DEFAULT_SCOPE]]
    assert ACCESS_TOKEN not in repr(token)


def test_invalidate_delegates_to_msal_cache() -> None:
    application = FakeMsalApplication({"access_token": ACCESS_TOKEN})
    provider = MsalCertificateTokenProvider(application)

    provider.invalidate()

    assert application.invalidations == 1


@pytest.mark.parametrize(
    ("provider_error", "expected_category", "technical_code", "retryable"),
    [
        (
            "temporarily_unavailable",
            GraphErrorCategory.UNAVAILABLE,
            "identity_service_unavailable",
            True,
        ),
        (
            "invalid_scope",
            GraphErrorCategory.AUTHORIZATION,
            "invalid_graph_scope",
            False,
        ),
        (
            "consent_required",
            GraphErrorCategory.AUTHORIZATION,
            "admin_consent_required",
            False,
        ),
        (
            "invalid_client",
            GraphErrorCategory.AUTHENTICATION,
            "invalid_client_certificate",
            False,
        ),
        (
            "unexpected_error",
            GraphErrorCategory.AUTHENTICATION,
            "token_acquisition_failed",
            False,
        ),
    ],
)
def test_identity_failures_are_classified_without_remote_description(
    provider_error: str,
    expected_category: GraphErrorCategory,
    technical_code: str,
    retryable: bool,
) -> None:
    provider = MsalCertificateTokenProvider(
        FakeMsalApplication(
            {
                "error": provider_error,
                "error_description": SENSITIVE_DESCRIPTION,
            }
        )
    )

    with pytest.raises(GraphAuthenticationError) as captured_error:
        provider.get_access_token()

    error = captured_error.value
    assert error.category is expected_category
    assert error.technical_code == technical_code
    assert error.retryable is retryable
    assert error.outcome_unknown is False
    assert SENSITIVE_DESCRIPTION not in str(error)
    assert SENSITIVE_DESCRIPTION not in repr(error)


def test_msal_transport_failure_logs_only_exception_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_error = f"network unavailable with {PEM_PASSPHRASE}"
    provider = MsalCertificateTokenProvider(
        FakeMsalApplication(OSError(sensitive_error))
    )

    with (
        caplog.at_level(
            logging.DEBUG,
            logger="mevo2glpi_worker_publisher.graph.auth",
        ),
        pytest.raises(GraphAuthenticationError) as captured_error,
    ):
        provider.get_access_token()

    error = captured_error.value
    assert error.category is GraphErrorCategory.UNAVAILABLE
    assert error.technical_code == "msal_transport_error"
    assert error.retryable is True
    assert "OSError" in caplog.text
    assert sensitive_error not in caplog.text
    assert PEM_PASSPHRASE not in str(error)


@pytest.mark.parametrize(
    "expires_in",
    [0, -1, True, "3600"],
)
def test_invalid_token_expiry_is_a_terminal_protocol_error(
    expires_in: object,
) -> None:
    provider = MsalCertificateTokenProvider(
        FakeMsalApplication({"access_token": ACCESS_TOKEN, "expires_in": expires_in})
    )

    with pytest.raises(GraphAuthenticationError) as captured_error:
        provider.get_access_token()

    error = captured_error.value
    assert error.category is GraphErrorCategory.PROTOCOL
    assert error.technical_code == "invalid_token_expiry"
    assert error.retryable is False


def test_from_pem_accepts_readable_secret_mount_symlinks(tmp_path: Path) -> None:
    real_key = tmp_path / "real-key.pem"
    real_certificate = tmp_path / "real-certificate.pem"
    real_key.write_text(PRIVATE_KEY_PEM, encoding="ascii")
    real_certificate.write_text(CERTIFICATE_PEM, encoding="ascii")
    key_symlink = tmp_path / "private-key.pem"
    certificate_symlink = tmp_path / "certificate.pem"
    key_symlink.symlink_to(real_key)
    certificate_symlink.symlink_to(real_certificate)
    application = FakeMsalApplication({"access_token": ACCESS_TOKEN})

    provider = MsalCertificateTokenProvider.from_pem(
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        private_key_pem_path=key_symlink,
        certificate_pem_path=certificate_symlink,
        passphrase=SecretStr(PEM_PASSPHRASE),
        application_factory=lambda *_args, **_kwargs: application,
    )

    assert provider.get_access_token().value.get_secret_value() == ACCESS_TOKEN


def test_unreadable_private_key_path_is_reported_without_exposing_its_location(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "sensitive-deployment-layout" / "private-key.pem"
    certificate_path = tmp_path / "certificate.pem"
    certificate_path.write_text(CERTIFICATE_PEM, encoding="ascii")

    with pytest.raises(GraphAuthenticationError) as captured_error:
        MsalCertificateTokenProvider.from_pem(
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            private_key_pem_path=missing_path,
            certificate_pem_path=certificate_path,
            passphrase=SecretStr(PEM_PASSPHRASE),
            application_factory=lambda *_args, **_kwargs: FakeMsalApplication({}),
        )

    error = captured_error.value
    assert error.category is GraphErrorCategory.UNAVAILABLE
    assert error.technical_code == "private_key_read_error"
    assert str(missing_path) not in str(error)


def test_msal_initialization_transport_failure_is_sanitized(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_key_path = tmp_path / "private-key.pem"
    certificate_path = tmp_path / "certificate.pem"
    private_key_path.write_text(PRIVATE_KEY_PEM, encoding="ascii")
    certificate_path.write_text(CERTIFICATE_PEM, encoding="ascii")
    sensitive_error = f"identity endpoint unavailable: {PEM_PASSPHRASE}"

    def failing_factory(*_args: object, **_kwargs: object) -> FakeMsalApplication:
        raise OSError(sensitive_error)

    with (
        caplog.at_level(
            logging.DEBUG,
            logger="mevo2glpi_worker_publisher.graph.auth",
        ),
        pytest.raises(GraphAuthenticationError) as captured_error,
    ):
        MsalCertificateTokenProvider.from_pem(
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            private_key_pem_path=private_key_path,
            certificate_pem_path=certificate_path,
            passphrase=SecretStr(PEM_PASSPHRASE),
            application_factory=failing_factory,
        )

    error = captured_error.value
    assert error.category is GraphErrorCategory.UNAVAILABLE
    assert error.technical_code == "msal_transport_error"
    assert sensitive_error not in caplog.text
    assert PEM_PASSPHRASE not in str(error)


@pytest.mark.parametrize("timeout_seconds", [True, 0, -1, float("inf")])
def test_from_pem_requires_a_finite_positive_identity_timeout(
    tmp_path: Path,
    timeout_seconds: object,
) -> None:
    private_key_path = tmp_path / "private-key.pem"
    certificate_path = tmp_path / "certificate.pem"
    private_key_path.write_text(PRIVATE_KEY_PEM, encoding="ascii")
    certificate_path.write_text(CERTIFICATE_PEM, encoding="ascii")

    with pytest.raises((TypeError, ValueError)):
        MsalCertificateTokenProvider.from_pem(
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            private_key_pem_path=private_key_path,
            certificate_pem_path=certificate_path,
            passphrase=SecretStr(PEM_PASSPHRASE),
            timeout_seconds=timeout_seconds,  # type: ignore[arg-type]
            application_factory=lambda *_args, **_kwargs: FakeMsalApplication({}),
        )
