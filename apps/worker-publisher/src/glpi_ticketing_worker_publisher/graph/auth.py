"""MSAL client-credentials authentication backed by separate PEM secrets."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol
from uuid import UUID

from msal.exceptions import MsalServiceError
from pydantic import SecretStr

from .contracts import GraphAuthenticationError
from .models import GraphAccessToken, GraphErrorCategory

LOGGER = logging.getLogger(__name__)

GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
ENTRA_AUTHORITY_ROOT = "https://login.microsoftonline.com"
SYNTHETIC_GRAPH_ACCESS_TOKEN = "synthetic-token"


class MsalConfidentialClientProtocol(Protocol):
    """Subset of MSAL used by the token provider."""

    def acquire_token_for_client(
        self,
        scopes: Sequence[str],
        **kwargs: object,
    ) -> Mapping[str, object]:
        """Obtain an application token, using MSAL's own cache when possible."""
        ...

    def remove_tokens_for_client(self) -> None:
        """Evict all cached tokens for the confidential client."""
        ...


MsalApplicationFactory = Callable[..., MsalConfidentialClientProtocol]


class SyntheticGraphTokenProvider:
    """Provide the known non-secret bearer accepted by the staging simulator."""

    def get_access_token(self) -> GraphAccessToken:
        return GraphAccessToken(
            value=SecretStr(SYNTHETIC_GRAPH_ACCESS_TOKEN),
        )

    def invalidate(self) -> None:
        """Keep the immutable simulator token after an HTTP 401 response."""


class MsalCertificateTokenProvider:
    """Adapt MSAL's application cache to the publisher token-provider port."""

    def __init__(self, application: MsalConfidentialClientProtocol) -> None:
        if not callable(getattr(application, "acquire_token_for_client", None)):
            raise TypeError("application must expose acquire_token_for_client")
        if not callable(getattr(application, "remove_tokens_for_client", None)):
            raise TypeError("application must expose remove_tokens_for_client")
        self._application = application

    @classmethod
    def from_pem(
        cls,
        *,
        tenant_id: UUID,
        client_id: UUID,
        private_key_pem_path: Path,
        certificate_pem_path: Path,
        passphrase: SecretStr | None = None,
        timeout_seconds: float = 10.0,
        application_factory: MsalApplicationFactory | None = None,
    ) -> MsalCertificateTokenProvider:
        """Build MSAL from a private key and its public X.509 certificate."""
        tenant_id = _validate_uuid(tenant_id, "tenant_id")
        client_id = _validate_uuid(client_id, "client_id")
        private_key_pem_path = _validate_pem_path(
            private_key_pem_path,
            technical_code="private_key_read_error",
        )
        certificate_pem_path = _validate_pem_path(
            certificate_pem_path,
            technical_code="certificate_read_error",
        )
        if private_key_pem_path.resolve() == certificate_pem_path.resolve():
            raise GraphAuthenticationError(
                category=GraphErrorCategory.AUTHENTICATION,
                technical_code="pem_files_must_be_distinct",
                retryable=False,
            )
        passphrase = _validate_passphrase(passphrase)
        timeout_seconds = _validate_timeout(timeout_seconds)
        factory = application_factory or _default_application_factory

        try:
            private_key = _read_private_key_pem(private_key_pem_path)
            public_certificate = _read_x509_certificate_pem(certificate_pem_path)
            client_credential: dict[str, str] = {
                "private_key": private_key,
                "public_certificate": public_certificate,
            }
            if passphrase is not None:
                client_credential["passphrase"] = passphrase.get_secret_value()
            application = factory(
                str(client_id),
                authority=f"{ENTRA_AUTHORITY_ROOT}/{tenant_id}",
                client_credential=client_credential,
                enable_pii_log=False,
                timeout=timeout_seconds,
            )
        except OSError as error:
            LOGGER.debug(
                "Entra client initialization diagnostic error_type=%s",
                type(error).__name__,
            )
            raise GraphAuthenticationError(
                category=GraphErrorCategory.UNAVAILABLE,
                technical_code="msal_transport_error",
                retryable=True,
            ) from None
        except MsalServiceError as error:
            LOGGER.debug(
                "Entra client initialization diagnostic error_type=%s",
                type(error).__name__,
            )
            raise GraphAuthenticationError(
                category=GraphErrorCategory.UNAVAILABLE,
                technical_code="identity_service_error",
                retryable=True,
            ) from None
        except ValueError:
            raise GraphAuthenticationError(
                category=GraphErrorCategory.AUTHENTICATION,
                technical_code="invalid_client_certificate",
                retryable=False,
            ) from None
        return cls(application)

    def get_access_token(self) -> GraphAccessToken:
        """Acquire a cached or fresh Graph ``.default`` application token."""
        try:
            result = self._application.acquire_token_for_client(
                scopes=[GRAPH_DEFAULT_SCOPE]
            )
        except OSError as error:
            LOGGER.debug(
                "Entra token acquisition diagnostic error_type=%s",
                type(error).__name__,
            )
            raise GraphAuthenticationError(
                category=GraphErrorCategory.UNAVAILABLE,
                technical_code="msal_transport_error",
                retryable=True,
            ) from None
        except MsalServiceError as error:
            LOGGER.debug(
                "Entra token acquisition diagnostic error_type=%s",
                type(error).__name__,
            )
            raise GraphAuthenticationError(
                category=GraphErrorCategory.UNAVAILABLE,
                technical_code="identity_service_error",
                retryable=True,
            ) from None
        except ValueError:
            raise GraphAuthenticationError(
                category=GraphErrorCategory.AUTHENTICATION,
                technical_code="invalid_client_certificate",
                retryable=False,
            ) from None

        if not isinstance(result, Mapping):
            raise GraphAuthenticationError(
                category=GraphErrorCategory.PROTOCOL,
                technical_code="invalid_token_response",
                retryable=False,
            )

        access_token = result.get("access_token")
        if isinstance(access_token, str) and access_token:
            try:
                return GraphAccessToken(
                    value=SecretStr(access_token),
                    expires_in_seconds=_read_expiry(result),
                )
            except ValueError:
                raise GraphAuthenticationError(
                    category=GraphErrorCategory.PROTOCOL,
                    technical_code="invalid_token_response",
                    retryable=False,
                ) from None

        category, technical_code, retryable = _classify_msal_failure(
            result.get("error")
        )
        raise GraphAuthenticationError(
            category=category,
            technical_code=technical_code,
            retryable=retryable,
        )

    def invalidate(self) -> None:
        """Evict MSAL's cached application token after Graph rejects it."""
        try:
            self._application.remove_tokens_for_client()
        except (MsalServiceError, OSError) as error:
            LOGGER.debug(
                "Entra token invalidation diagnostic error_type=%s",
                type(error).__name__,
            )
            raise GraphAuthenticationError(
                category=GraphErrorCategory.UNAVAILABLE,
                technical_code="token_invalidation_failed",
                retryable=True,
            ) from None


def _default_application_factory(
    client_id: str,
    **kwargs: object,
) -> MsalConfidentialClientProtocol:
    from msal import ConfidentialClientApplication

    return ConfidentialClientApplication(client_id, **kwargs)


def _validate_uuid(value: object, field_name: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be a UUID")
    return value


def _validate_pem_path(value: object, *, technical_code: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError("PEM path must be a Path")
    try:
        if not value.is_file():
            raise OSError
        with value.open("rb"):
            pass
    except OSError:
        raise GraphAuthenticationError(
            category=GraphErrorCategory.UNAVAILABLE,
            technical_code=technical_code,
            retryable=True,
        ) from None
    return value


def _read_private_key_pem(path: Path) -> str:
    content = _read_pem_text(
        path,
        read_error_code="private_key_read_error",
        invalid_pem_code="invalid_private_key_pem",
    )
    private_key_labels = (
        "PRIVATE KEY",
        "ENCRYPTED PRIVATE KEY",
        "RSA PRIVATE KEY",
        "EC PRIVATE KEY",
    )
    if not any(_contains_pem_block(content, label) for label in private_key_labels):
        raise GraphAuthenticationError(
            category=GraphErrorCategory.AUTHENTICATION,
            technical_code="invalid_private_key_pem",
            retryable=False,
        )
    return content


def _read_x509_certificate_pem(path: Path) -> str:
    content = _read_pem_text(
        path,
        read_error_code="certificate_read_error",
        invalid_pem_code="invalid_x509_certificate_pem",
    )
    if not _contains_pem_block(content, "CERTIFICATE"):
        raise GraphAuthenticationError(
            category=GraphErrorCategory.AUTHENTICATION,
            technical_code="invalid_x509_certificate_pem",
            retryable=False,
        )
    return content


def _read_pem_text(
    path: Path,
    *,
    read_error_code: str,
    invalid_pem_code: str,
) -> str:
    try:
        return path.read_text(encoding="ascii")
    except UnicodeError:
        raise GraphAuthenticationError(
            category=GraphErrorCategory.AUTHENTICATION,
            technical_code=invalid_pem_code,
            retryable=False,
        ) from None
    except OSError:
        raise GraphAuthenticationError(
            category=GraphErrorCategory.UNAVAILABLE,
            technical_code=read_error_code,
            retryable=True,
        ) from None


def _contains_pem_block(content: str, label: str) -> bool:
    return f"-----BEGIN {label}-----" in content and f"-----END {label}-----" in content


def _validate_passphrase(value: object) -> SecretStr | None:
    if value is None:
        return None
    if not isinstance(value, SecretStr):
        raise TypeError("passphrase must be a SecretStr or None")
    raw_value = value.get_secret_value()
    if not raw_value or any(not character.isprintable() for character in raw_value):
        raise ValueError("passphrase must be non-empty and printable")
    return value


def _validate_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("timeout_seconds must be a positive number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout_seconds must be finite and greater than zero")
    return float(value)


def _read_expiry(result: Mapping[str, object]) -> int | None:
    expires_in = result.get("expires_in")
    if expires_in is None:
        return None
    if (
        isinstance(expires_in, bool)
        or not isinstance(expires_in, int)
        or expires_in <= 0
    ):
        raise GraphAuthenticationError(
            category=GraphErrorCategory.PROTOCOL,
            technical_code="invalid_token_expiry",
            retryable=False,
        )
    return expires_in


def _classify_msal_failure(
    error: object,
) -> tuple[GraphErrorCategory, str, bool]:
    classifications = {
        "temporarily_unavailable": (
            GraphErrorCategory.UNAVAILABLE,
            "identity_service_unavailable",
            True,
        ),
        "server_error": (
            GraphErrorCategory.UNAVAILABLE,
            "identity_service_error",
            True,
        ),
        "invalid_scope": (
            GraphErrorCategory.AUTHORIZATION,
            "invalid_graph_scope",
            False,
        ),
        "consent_required": (
            GraphErrorCategory.AUTHORIZATION,
            "admin_consent_required",
            False,
        ),
        "interaction_required": (
            GraphErrorCategory.AUTHORIZATION,
            "application_authorization_required",
            False,
        ),
        "invalid_client": (
            GraphErrorCategory.AUTHENTICATION,
            "invalid_client_certificate",
            False,
        ),
        "unauthorized_client": (
            GraphErrorCategory.AUTHENTICATION,
            "unauthorized_client",
            False,
        ),
    }
    if isinstance(error, str):
        classification = classifications.get(error)
        if classification is not None:
            return classification
    return (
        GraphErrorCategory.AUTHENTICATION,
        "token_acquisition_failed",
        False,
    )


__all__ = [
    "ENTRA_AUTHORITY_ROOT",
    "GRAPH_DEFAULT_SCOPE",
    "MsalApplicationFactory",
    "MsalCertificateTokenProvider",
    "MsalConfidentialClientProtocol",
]
