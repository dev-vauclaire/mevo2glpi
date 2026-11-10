from __future__ import annotations

from collections.abc import Mapping

import pytest

from mevo2glpi_web.auth.models import ACCESS_ROLE, AuthMode
from mevo2glpi_web.auth.oidc import (
    OidcAuthenticationError,
    OidcAuthenticator,
    OidcAuthorizationError,
    OidcClaimsError,
    OidcConfiguration,
    OidcConfigurationError,
    OidcUnavailableError,
    register_authlib_oidc_client,
)

ISSUER = "https://identity.example.test/realms/operators"
REDIRECT_URI = "https://mevo2glpi.example.test/auth/oidc/callback"
CLIENT_SECRET = "synthetic-client-secret"
PSEUDONYMIZATION_SECRET = b"pseudonymization-key-for-tests-only"


class FakeRequest:
    def __init__(self) -> None:
        self.session: dict[str, object] = {"preexisting": "preserved"}


class FakeOidcClient:
    def __init__(
        self,
        *,
        token: Mapping[str, object] | None = None,
        begin_error: Exception | None = None,
        callback_error: Exception | None = None,
    ) -> None:
        self.token = token or {}
        self.begin_error = begin_error
        self.callback_error = callback_error
        self.redirect_uri: str | None = None

    async def authorize_redirect(
        self,
        request: FakeRequest,
        redirect_uri: str,
        **_kwargs: object,
    ) -> object:
        if self.begin_error is not None:
            raise self.begin_error
        self.redirect_uri = redirect_uri
        request.session["_state_mevo2glpi_oidc_synthetic-state"] = {
            "data": {
                "state": "synthetic-state",
                "nonce": "synthetic-nonce",
                "code_verifier": "synthetic-pkce-verifier",
            }
        }
        return {"status_code": 302}

    async def authorize_access_token(
        self,
        request: FakeRequest,
        **_kwargs: object,
    ) -> Mapping[str, object]:
        request.session.pop("_state_mevo2glpi_oidc_synthetic-state", None)
        if self.callback_error is not None:
            raise self.callback_error
        return self.token


class FakeRegistry:
    def __init__(
        self,
        client: FakeOidcClient,
        *,
        register_returns_client: bool = True,
    ) -> None:
        self.client = client
        self.register_returns_client = register_returns_client
        self.name: str | None = None
        self.arguments: dict[str, object] = {}

    def register(self, name: str, **kwargs: object) -> FakeOidcClient | None:
        self.name = name
        self.arguments = kwargs
        return self.client if self.register_returns_client else None

    def create_client(self, name: str) -> FakeOidcClient | None:
        assert name == self.name
        return self.client


def _configuration(**overrides: object) -> OidcConfiguration:
    arguments: dict[str, object] = {
        "issuer": ISSUER,
        "client_id": "mevo2glpi-web",
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "pseudonymization_secret": PSEUDONYMIZATION_SECRET,
    }
    arguments.update(overrides)
    return OidcConfiguration(**arguments)  # type: ignore[arg-type]


def _token(
    *,
    subject: object = "provider-subject-never-persisted",
    roles: object = (ACCESS_ROLE, "unrelated-provider-role"),
    roles_claim: str = "roles",
) -> dict[str, object]:
    return {
        "access_token": "access-token-never-persisted",
        "refresh_token": "refresh-token-never-persisted",
        "id_token": "id-token-never-persisted",
        "userinfo": {"sub": subject, roles_claim: roles},
    }


def test_authlib_registration_uses_discovery_openid_and_pkce_s256() -> None:
    configuration = _configuration()
    client = FakeOidcClient()
    registry = FakeRegistry(client, register_returns_client=False)

    registered_client = register_authlib_oidc_client(registry, configuration)

    assert registered_client is client
    assert registry.name == "mevo2glpi_oidc"
    assert registry.arguments == {
        "client_id": "mevo2glpi-web",
        "client_secret": CLIENT_SECRET,
        "server_metadata_url": (
            "https://identity.example.test/realms/operators/"
            ".well-known/openid-configuration"
        ),
        "authorize_params": {"response_type": "code"},
        "client_kwargs": {
            "scope": "openid",
            "code_challenge_method": "S256",
        },
    }


@pytest.mark.asyncio
async def test_begin_uses_authlib_session_for_state_nonce_and_pkce() -> None:
    request = FakeRequest()
    client = FakeOidcClient()

    response = await OidcAuthenticator(client, _configuration()).begin(request)

    assert response == {"status_code": 302}
    assert client.redirect_uri == REDIRECT_URI
    transient = request.session["_state_mevo2glpi_oidc_synthetic-state"]
    assert isinstance(transient, dict)
    assert transient["data"] == {
        "state": "synthetic-state",
        "nonce": "synthetic-nonce",
        "code_verifier": "synthetic-pkce-verifier",
    }
    assert request.session["preexisting"] == "preserved"


@pytest.mark.asyncio
async def test_begin_sanitizes_provider_discovery_failures() -> None:
    sensitive_provider_error = "private provider discovery endpoint"
    client = FakeOidcClient(begin_error=RuntimeError(sensitive_provider_error))

    with pytest.raises(OidcUnavailableError) as captured_error:
        await OidcAuthenticator(client, _configuration()).begin(FakeRequest())

    assert sensitive_provider_error not in str(captured_error.value)
    assert captured_error.value.__cause__ is None


@pytest.mark.asyncio
async def test_callback_returns_only_a_pseudonymous_minimal_principal() -> None:
    raw_subject = "provider-subject-never-persisted"
    request = FakeRequest()
    client = FakeOidcClient(token=_token(subject=raw_subject))

    principal = await OidcAuthenticator(client, _configuration()).complete(request)

    assert principal.auth_mode is AuthMode.OIDC
    assert principal.subject.startswith("oidc:")
    assert raw_subject not in principal.subject
    assert principal.roles == frozenset({ACCESS_ROLE})
    assert principal.session_version is None
    assert request.session == {"preexisting": "preserved"}
    assert "access-token-never-persisted" not in repr(request.session)
    assert "refresh-token-never-persisted" not in repr(request.session)
    assert "id-token-never-persisted" not in repr(request.session)


@pytest.mark.asyncio
async def test_callback_accepts_a_configurable_flat_roles_claim() -> None:
    client = FakeOidcClient(
        token=_token(roles_claim="application_roles", roles=(ACCESS_ROLE,))
    )
    authenticator = OidcAuthenticator(
        client,
        _configuration(roles_claim="application_roles"),
    )

    principal = await authenticator.complete(FakeRequest())

    assert principal.roles == frozenset({ACCESS_ROLE})


@pytest.mark.asyncio
async def test_callback_rejects_an_identity_without_the_required_role() -> None:
    client = FakeOidcClient(token=_token(roles=("another-role",)))

    with pytest.raises(OidcAuthorizationError, match="not authorized"):
        await OidcAuthenticator(client, _configuration()).complete(FakeRequest())


@pytest.mark.asyncio
async def test_callback_treats_an_omitted_roles_claim_as_unauthorized() -> None:
    token = _token()
    userinfo = token["userinfo"]
    assert isinstance(userinfo, dict)
    userinfo.pop("roles")
    client = FakeOidcClient(token=token)

    with pytest.raises(OidcAuthorizationError, match="not authorized"):
        await OidcAuthenticator(client, _configuration()).complete(FakeRequest())


@pytest.mark.parametrize(
    ("token", "expected_message"),
    [
        ({"sub": "unvalidated-top-level-subject"}, "no validated identity"),
        (_token(subject=""), "subject claim is invalid"),
        (_token(roles=ACCESS_ROLE), "roles claim is invalid"),
        (_token(roles=(ACCESS_ROLE, 42)), "roles claim is invalid"),
    ],
)
@pytest.mark.asyncio
async def test_callback_rejects_missing_or_malformed_validated_claims(
    token: Mapping[str, object],
    expected_message: str,
) -> None:
    client = FakeOidcClient(token=token)

    with pytest.raises(OidcClaimsError, match=expected_message):
        await OidcAuthenticator(client, _configuration()).complete(FakeRequest())


@pytest.mark.asyncio
async def test_authlib_callback_failures_are_sanitized() -> None:
    sensitive_provider_error = (
        "provider response contained a private authorization code"
    )
    client = FakeOidcClient(callback_error=RuntimeError(sensitive_provider_error))

    with pytest.raises(OidcAuthenticationError) as captured_error:
        await OidcAuthenticator(client, _configuration()).complete(FakeRequest())

    assert sensitive_provider_error not in str(captured_error.value)
    assert captured_error.value.__cause__ is None


@pytest.mark.asyncio
async def test_pseudonym_is_stable_per_issuer_and_distinct_between_issuers() -> None:
    token = _token(subject="same-provider-subject")
    first = await OidcAuthenticator(
        FakeOidcClient(token=token),
        _configuration(),
    ).complete(FakeRequest())
    repeated = await OidcAuthenticator(
        FakeOidcClient(token=token),
        _configuration(),
    ).complete(FakeRequest())
    other_issuer = await OidcAuthenticator(
        FakeOidcClient(token=token),
        _configuration(issuer="https://another-idp.example.test"),
    ).complete(FakeRequest())

    assert first.subject == repeated.subject
    assert first.subject != other_issuer.subject


def test_configuration_repr_and_errors_never_echo_secrets() -> None:
    configuration = _configuration()

    assert CLIENT_SECRET not in repr(configuration)
    assert PSEUDONYMIZATION_SECRET.decode() not in repr(configuration)

    with pytest.raises(OidcConfigurationError) as captured_error:
        _configuration(pseudonymization_secret="too-short")

    assert "too-short" not in str(captured_error.value)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("issuer", "identity.example.test/realms/operators"),
        ("issuer", "https://identity.example.test/realm?secret=value"),
        ("redirect_uri", "javascript:alert(1)"),
        ("roles_claim", "roles\nforged"),
        ("required_role", ""),
    ],
)
def test_configuration_rejects_unsafe_public_inputs(
    field_name: str,
    invalid_value: str,
) -> None:
    with pytest.raises(OidcConfigurationError):
        _configuration(**{field_name: invalid_value})
