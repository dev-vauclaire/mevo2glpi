"""Provider-neutral OpenID Connect authentication.

Authlib owns the protocol details of the authorization-code flow: discovery,
state, nonce, PKCE and ID-token validation. This module configures an
Authlib-compatible client and turns validated claims into a minimal principal.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from mevo2glpi_web.auth.models import ACCESS_ROLE, AuthMode, Principal

DEFAULT_OIDC_CLIENT_NAME = "mevo2glpi_oidc"
DEFAULT_ROLES_CLAIM = "roles"
_MIN_PSEUDONYMIZATION_SECRET_BYTES = 32


class OidcConfigurationError(ValueError):
    """Raised when the local OIDC configuration is unsafe or incomplete."""


class OidcAuthenticationError(RuntimeError):
    """Raised when the provider response cannot establish an identity."""


class OidcUnavailableError(OidcAuthenticationError):
    """Raised when a new OIDC authorization cannot reach the provider."""


class OidcClaimsError(OidcAuthenticationError):
    """Raised when the validated ID-token claims do not match the contract."""


class OidcAuthorizationError(RuntimeError):
    """Raised when an authenticated identity lacks application access."""


class OidcRequestProtocol(Protocol):
    """Subset of a Starlette request used by Authlib."""

    @property
    def session(self) -> MutableMapping[str, object]: ...


class AuthlibOidcClientProtocol(Protocol):
    """Structural boundary implemented by Authlib's Starlette remote app."""

    async def authorize_redirect(
        self,
        request: OidcRequestProtocol,
        redirect_uri: str,
        **kwargs: object,
    ) -> object: ...

    async def authorize_access_token(
        self,
        request: OidcRequestProtocol,
        **kwargs: object,
    ) -> Mapping[str, object]: ...


class AuthlibOAuthRegistryProtocol(Protocol):
    """Subset of ``authlib.integrations.starlette_client.OAuth`` in use."""

    def register(
        self,
        name: str,
        **kwargs: object,
    ) -> AuthlibOidcClientProtocol | None: ...

    def create_client(self, name: str) -> AuthlibOidcClientProtocol | None: ...


@dataclass(frozen=True, slots=True)
class OidcConfiguration:
    """Provider-neutral inputs needed by the OIDC adapter."""

    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    pseudonymization_secret: bytes | str = field(repr=False)
    roles_claim: str = DEFAULT_ROLES_CLAIM
    required_role: str = ACCESS_ROLE
    client_name: str = DEFAULT_OIDC_CLIENT_NAME

    def __post_init__(self) -> None:
        normalized_issuer = _validate_absolute_url(self.issuer, field_name="issuer")
        normalized_redirect = _validate_absolute_url(
            self.redirect_uri,
            field_name="redirect_uri",
        )
        _validate_clean_text(self.client_id, field_name="client_id")
        _validate_secret(self.client_secret, field_name="client_secret")
        _validate_clean_text(self.roles_claim, field_name="roles_claim")
        _validate_clean_text(self.required_role, field_name="required_role")
        _validate_clean_text(self.client_name, field_name="client_name")

        secret = self.pseudonymization_secret
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if (
            not isinstance(secret, bytes)
            or len(secret) < _MIN_PSEUDONYMIZATION_SECRET_BYTES
        ):
            raise OidcConfigurationError(
                "The OIDC pseudonymization secret must contain at least 32 bytes."
            )

        object.__setattr__(self, "issuer", normalized_issuer.rstrip("/"))
        object.__setattr__(self, "redirect_uri", normalized_redirect)
        object.__setattr__(self, "pseudonymization_secret", secret)

    @property
    def discovery_url(self) -> str:
        """Return the OpenID Provider Configuration endpoint for the issuer."""

        return f"{self.issuer}/.well-known/openid-configuration"


def register_authlib_oidc_client(
    registry: AuthlibOAuthRegistryProtocol,
    configuration: OidcConfiguration,
) -> AuthlibOidcClientProtocol:
    """Register a generic OIDC client configured for code flow and PKCE S256."""

    client = registry.register(
        configuration.client_name,
        client_id=configuration.client_id,
        client_secret=configuration.client_secret,
        server_metadata_url=configuration.discovery_url,
        authorize_params={"response_type": "code"},
        client_kwargs={
            "scope": "openid",
            "code_challenge_method": "S256",
        },
    )
    if client is None:
        client = registry.create_client(configuration.client_name)
    if client is None:
        raise OidcConfigurationError("The OIDC client could not be registered.")
    return client


class OidcAuthenticator:
    """Execute a generic OIDC login and build a pseudonymous principal."""

    def __init__(
        self,
        client: AuthlibOidcClientProtocol,
        configuration: OidcConfiguration,
    ) -> None:
        self._client = client
        self._configuration = configuration

    async def begin(self, request: OidcRequestProtocol) -> object:
        """Start Authorization Code + PKCE through Authlib.

        With ``openid`` and PKCE configured at registration, Authlib generates
        state, nonce and the verifier and stores them temporarily in
        ``request.session``.
        """

        try:
            return await self._client.authorize_redirect(
                request,
                self._configuration.redirect_uri,
            )
        except Exception:
            raise OidcUnavailableError(
                "The OIDC provider is unavailable for a new authorization."
            ) from None

    async def complete(self, request: OidcRequestProtocol) -> Principal:
        """Validate the callback and return the application's minimal identity."""

        try:
            token = await self._client.authorize_access_token(request)
        except Exception:
            raise OidcAuthenticationError(
                "The OIDC authorization response could not be validated."
            ) from None

        try:
            subject, roles = _extract_validated_identity(
                token,
                roles_claim=self._configuration.roles_claim,
            )
            if self._configuration.required_role not in roles:
                raise OidcAuthorizationError(
                    "The authenticated identity is not authorized for mevo2glpi."
                )

            pseudonymous_subject = _pseudonymize_subject(
                issuer=self._configuration.issuer,
                subject=subject,
                secret=self._configuration.pseudonymization_secret,
            )
            return Principal(
                auth_mode=AuthMode.OIDC,
                subject=pseudonymous_subject,
                roles=frozenset({self._configuration.required_role}),
                session_version=None,
            )
        finally:
            # Never pass Authlib's access, refresh or ID token to the signed
            # application-session serializer.
            token = None


def _extract_validated_identity(
    token: Mapping[str, object],
    *,
    roles_claim: str,
) -> tuple[str, frozenset[str]]:
    """Read only claims produced by Authlib's validated ID-token parser."""

    if not isinstance(token, Mapping):
        raise OidcClaimsError("The OIDC provider returned an invalid token response.")
    userinfo = token.get("userinfo")
    if not isinstance(userinfo, Mapping):
        raise OidcClaimsError("The OIDC response has no validated identity claims.")

    subject = userinfo.get("sub")
    if not _is_clean_claim(subject, maximum_length=512):
        raise OidcClaimsError("The validated OIDC subject claim is invalid.")

    raw_roles = userinfo.get(roles_claim)
    if raw_roles is None:
        return subject, frozenset()
    if not isinstance(raw_roles, (list, tuple)):
        raise OidcClaimsError("The validated OIDC roles claim is invalid.")
    if len(raw_roles) > 64:
        raise OidcClaimsError("The validated OIDC roles claim is invalid.")

    roles: set[str] = set()
    for role in raw_roles:
        if not _is_clean_claim(role, maximum_length=128):
            raise OidcClaimsError("The validated OIDC roles claim is invalid.")
        roles.add(role)
    return subject, frozenset(roles)


def _pseudonymize_subject(*, issuer: str, subject: str, secret: bytes | str) -> str:
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    material = b"mevo2glpi.oidc.subject.v1\x00" + f"{issuer}\x00{subject}".encode()
    digest = hmac.new(secret, material, hashlib.sha256).hexdigest()
    return f"oidc:{digest}"


def _validate_absolute_url(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OidcConfigurationError(f"The OIDC {field_name} must be an absolute URL.")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise OidcConfigurationError(f"The OIDC {field_name} must be an absolute URL.")
    return value


def _validate_clean_text(value: object, *, field_name: str) -> None:
    if not _is_clean_claim(value, maximum_length=256):
        raise OidcConfigurationError(f"The OIDC {field_name} is invalid.")


def _validate_secret(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise OidcConfigurationError(f"The OIDC {field_name} is invalid.")


def _is_clean_claim(value: object, *, maximum_length: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum_length
        and value == value.strip()
        and all(character.isprintable() for character in value)
    )
