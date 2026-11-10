"""HTTP security contracts shared by local and OIDC authentication."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from mevo2glpi_common.release_identity import (
    DeploymentEnvironment,
    ReleaseIdentity,
)
from mevo2glpi_web.auth.audit import AuthAuditEvent
from mevo2glpi_web.auth.http import (
    LOGIN_CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SessionCookiePolicy,
)
from mevo2glpi_web.auth.local import LocalAccount, LocalAuthenticator
from mevo2glpi_web.auth.models import ACCESS_ROLE, AuthMode, Principal
from mevo2glpi_web.auth.oidc import (
    OidcAuthenticationError,
    OidcAuthorizationError,
    OidcUnavailableError,
)
from mevo2glpi_web.auth.sessions import (
    ItsDangerousSessionSigner,
    LocalSessionAccountState,
    SessionCookieCodec,
    SessionManager,
)
from mevo2glpi_web.controllers.authentication import AuthenticationServices
from mevo2glpi_web.web import create_app

_ACCOUNT_ID = UUID("7281cdff-a51b-4be2-9445-e72f627dc3e8")
_SESSION_SECRET = "synthetic-auth-http-session-secret-longer-than-32-bytes"
_VALID_PASSWORD = "synthetic-valid-password"
_RELEASE_IDENTITY = ReleaseIdentity(
    environment=DeploymentEnvironment.DEVELOPMENT,
    release="main",
    commit_sha="a" * 40,
)


@dataclass(slots=True)
class _AuditRecorder:
    events: list[tuple[AuthAuditEvent, dict[str, object]]] = field(default_factory=list)

    def record(self, event_type: AuthAuditEvent, **kwargs: object) -> None:
        self.events.append((event_type, kwargs))


@dataclass(slots=True)
class _LocalAccountStore:
    account: LocalAccount
    availability_error: Exception | None = None
    successful_authentications: int = 0
    failed_authentications: int = 0

    def get_by_username(self, username: str) -> LocalAccount | None:
        if self.availability_error is not None:
            raise self.availability_error
        return self.account if username == self.account.username else None

    def get_session_state(
        self,
        account_id: UUID,
    ) -> LocalSessionAccountState | None:
        if account_id != self.account.account_id:
            return None
        return LocalSessionAccountState(
            is_active=self.account.is_active,
            session_version=self.account.session_version,
        )

    def record_failed_authentication(self, *_args: object, **_kwargs: object) -> None:
        self.failed_authentications += 1

    def record_successful_authentication(
        self,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        self.successful_authentications += 1


class _SyntheticPasswordHasher:
    def hash(self, _password: str) -> str:
        return "synthetic-password-hash"

    def verify(self, password: str, password_hash: str) -> bool:
        return (
            password == _VALID_PASSWORD and password_hash == "synthetic-password-hash"
        )


@dataclass(slots=True)
class _OidcAuthenticator:
    principal: Principal
    begin_error: Exception | None = None
    callback_error: Exception | None = None
    begin_calls: int = 0
    complete_calls: int = 0

    async def begin(self, request: Any) -> RedirectResponse:
        self.begin_calls += 1
        if self.begin_error is not None:
            raise self.begin_error
        request.session["synthetic_state"] = "state-value"
        return RedirectResponse("https://identity.example.test/authorize")

    async def complete(self, request: Any) -> Principal:
        self.complete_calls += 1
        if self.callback_error is not None:
            raise self.callback_error
        # Simulate sensitive Authlib protocol state. The application controller
        # must clear it and must never copy it to its own signed cookie.
        request.session["access_token"] = "never-copy-access-token"
        request.session["refresh_token"] = "never-copy-refresh-token"
        return self.principal


@dataclass(frozen=True, slots=True)
class _AuthContext:
    client: TestClient
    sessions: SessionManager
    audit: _AuditRecorder
    oidc: _OidcAuthenticator | None = None


def test_authentication_is_default_deny_but_health_and_static_are_public(
    tmp_path: Path,
) -> None:
    with _local_context(tmp_path) as context:
        page = context.client.get("/?filter=pending", follow_redirects=False)
        api = context.client.get("/api/version", follow_redirects=False)

        assert page.status_code == 303
        assert page.headers["location"] == "/auth/login?next=%2F%3Ffilter%3Dpending"
        assert page.headers["cache-control"] == "no-store"
        assert api.status_code == 401
        assert api.json() == {"detail": "Authentication required"}
        assert api.headers["cache-control"] == "no-store"
        assert context.client.get("/health").status_code == 200
        assert context.client.get("/static/css/app.css").status_code == 200


def test_authenticated_responses_are_not_cacheable_by_shared_proxies(
    tmp_path: Path,
) -> None:
    with _local_context(tmp_path) as context:
        _login_local(context.client)

        response = context.client.get("/openapi.json")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"


def test_local_login_issues_minimal_secure_cookie_and_audits_success(
    tmp_path: Path,
) -> None:
    with _local_context(tmp_path) as context:
        context.client.get("/auth/login?next=/api/version")
        csrf_token = context.client.cookies.get(LOGIN_CSRF_COOKIE_NAME)

        response = context.client.post(
            "/auth/login",
            data={
                "username": "operator-authorized",
                "password": _VALID_PASSWORD,
                "csrf_token": csrf_token,
                "next_path": "/api/version",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/api/version"
        session_cookie = context.client.cookies.get(SESSION_COOKIE_NAME)
        assert isinstance(session_cookie, str)
        resolved = context.sessions.resolve(session_cookie)
        assert resolved.principal == Principal(
            auth_mode=AuthMode.LOCAL,
            subject=str(_ACCOUNT_ID),
            roles=frozenset({ACCESS_ROLE}),
            session_version=0,
        )
        set_cookie_headers = response.headers.get_list("set-cookie")
        application_cookie = next(
            header
            for header in set_cookie_headers
            if header.startswith(f"{SESSION_COOKIE_NAME}=")
        )
        assert "HttpOnly" in application_cookie
        assert "SameSite=lax" in application_cookie
        assert "Path=/" in application_cookie
        assert "operator-authorized" not in session_cookie
        assert _VALID_PASSWORD not in session_cookie
        assert context.client.get("/api/version").status_code == 200
        assert [event for event, _details in context.audit.events] == [
            AuthAuditEvent.LOGIN_SUCCEEDED
        ]
        assert context.audit.events[0][1] == {
            "auth_mode": AuthMode.LOCAL,
            "succeeded": True,
            "category": None,
            "technical_code": None,
        }


def test_favicon_request_never_invalidates_login_csrf_token(tmp_path: Path) -> None:
    with _local_context(tmp_path) as context:
        login_page = context.client.get("/auth/login")
        csrf_token = context.client.cookies.get(LOGIN_CSRF_COOKIE_NAME)

        assert login_page.status_code == 200
        assert isinstance(csrf_token, str)
        assert csrf_token in login_page.text
        assert 'rel="icon"' in login_page.text

        favicon = context.client.get("/favicon.ico", follow_redirects=False)

        assert favicon.status_code == 404
        assert "location" not in favicon.headers
        assert favicon.headers.get_list("set-cookie") == []
        assert context.client.cookies.get(LOGIN_CSRF_COOKIE_NAME) == csrf_token

        login = context.client.post(
            "/auth/login",
            data={
                "username": "operator-authorized",
                "password": _VALID_PASSWORD,
                "csrf_token": csrf_token,
                "next_path": "/",
            },
            follow_redirects=False,
        )

        assert login.status_code == 303
        assert context.client.cookies.get(SESSION_COOKIE_NAME) is not None


def test_local_login_failure_is_generic_and_does_not_issue_session(
    tmp_path: Path,
) -> None:
    with _local_context(tmp_path) as context:
        context.client.get("/auth/login")
        csrf_token = context.client.cookies.get(LOGIN_CSRF_COOKIE_NAME)
        secret_input = "synthetic-unknown-operator"
        secret_password = "synthetic-wrong-password"

        response = context.client.post(
            "/auth/login",
            data={
                "username": secret_input,
                "password": secret_password,
                "csrf_token": csrf_token,
                "next_path": "/",
            },
        )

        assert response.status_code == 401
        assert "Identifiant ou mot de passe incorrect." in response.text
        assert secret_input not in response.text
        assert secret_password not in response.text
        assert context.client.cookies.get(SESSION_COOKIE_NAME) is None
        assert context.audit.events == [
            (
                AuthAuditEvent.LOGIN_FAILED,
                {
                    "auth_mode": AuthMode.LOCAL,
                    "succeeded": False,
                    "category": "authentication",
                    "technical_code": "credentials_invalid",
                },
            )
        ]


def test_local_account_store_unavailability_is_sanitized_and_reported_as_503(
    tmp_path: Path,
) -> None:
    private_error = RuntimeError("synthetic private database endpoint")
    with _local_context(tmp_path, availability_error=private_error) as context:
        context.client.get("/auth/login")
        response = context.client.post(
            "/auth/login",
            data={
                "username": "operator-authorized",
                "password": _VALID_PASSWORD,
                "csrf_token": context.client.cookies.get(LOGIN_CSRF_COOKIE_NAME),
                "next_path": "/",
            },
        )

        assert response.status_code == 503
        assert "temporairement indisponible" in response.text
        assert str(private_error) not in response.text
        assert context.client.cookies.get(SESSION_COOKIE_NAME) is None
        assert context.audit.events == [
            (
                AuthAuditEvent.LOGIN_FAILED,
                {
                    "auth_mode": AuthMode.LOCAL,
                    "succeeded": False,
                    "category": "availability",
                    "technical_code": "local_account_store_unavailable",
                },
            )
        ]


def test_local_login_never_reflects_an_oversized_password(tmp_path: Path) -> None:
    with _local_context(tmp_path) as context:
        context.client.get("/auth/login")
        oversized_password = "private-password-value-" * 16

        response = context.client.post(
            "/auth/login",
            data={
                "username": "operator-authorized",
                "password": oversized_password,
                "csrf_token": context.client.cookies.get(LOGIN_CSRF_COOKIE_NAME),
                "next_path": "/",
            },
        )

        assert response.status_code == 401
        assert "Identifiant ou mot de passe incorrect." in response.text
        assert oversized_password not in response.text


def test_locked_local_account_is_audited_as_throttled(tmp_path: Path) -> None:
    locked_until = datetime(2026, 11, 10, 12, 5, tzinfo=UTC)
    with _local_context(tmp_path, locked_until=locked_until) as context:
        context.client.get("/auth/login")

        response = context.client.post(
            "/auth/login",
            data={
                "username": "operator-authorized",
                "password": _VALID_PASSWORD,
                "csrf_token": context.client.cookies.get(LOGIN_CSRF_COOKIE_NAME),
                "next_path": "/",
            },
        )

        assert response.status_code == 401
        assert context.audit.events == [
            (
                AuthAuditEvent.LOGIN_THROTTLED,
                {
                    "auth_mode": AuthMode.LOCAL,
                    "succeeded": False,
                    "category": "throttling",
                    "technical_code": "login_temporarily_locked",
                },
            )
        ]


def test_logout_requires_session_csrf_then_clears_cookie(tmp_path: Path) -> None:
    with _local_context(tmp_path) as context:
        _login_local(context.client)
        session_cookie = context.client.cookies.get(SESSION_COOKIE_NAME)
        assert isinstance(session_cookie, str)
        csrf_token = context.sessions.resolve(session_cookie).csrf_token

        rejected = context.client.post(
            "/auth/logout",
            data={"csrf_token": "x" * 32},
            follow_redirects=False,
        )
        assert rejected.status_code == 403
        assert context.client.cookies.get(SESSION_COOKIE_NAME) == session_cookie

        response = context.client.post(
            "/auth/logout",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"
        assert context.client.cookies.get(SESSION_COOKIE_NAME) is None
        assert [event for event, _details in context.audit.events] == [
            AuthAuditEvent.LOGIN_SUCCEEDED,
            AuthAuditEvent.LOGOUT,
        ]


def test_oidc_callback_replaces_temporary_authlib_state_with_application_cookie(
    tmp_path: Path,
) -> None:
    with _oidc_context(tmp_path) as context:
        redirect = context.client.get(
            "/auth/login?next=/api/version",
            follow_redirects=False,
        )

        assert redirect.status_code in {302, 307}
        assert redirect.headers["location"] == "https://identity.example.test/authorize"
        assert context.client.cookies.get("mevo2glpi_oidc_transaction") is not None

        callback = context.client.get(
            "/auth/oidc/callback",
            follow_redirects=False,
        )

        assert callback.status_code == 303
        assert callback.headers["location"] == "/api/version"
        session_cookie = context.client.cookies.get(SESSION_COOKIE_NAME)
        assert isinstance(session_cookie, str)
        resolved = context.sessions.resolve(session_cookie)
        assert resolved.principal.auth_mode is AuthMode.OIDC
        assert context.client.cookies.get("mevo2glpi_oidc_transaction") is None
        assert "never-copy-access-token" not in session_cookie
        assert "never-copy-refresh-token" not in session_cookie
        assert context.client.get("/api/version").status_code == 200
        assert context.oidc is not None
        assert context.oidc.begin_calls == 1
        assert context.oidc.complete_calls == 1
        assert [event for event, _details in context.audit.events] == [
            AuthAuditEvent.LOGIN_SUCCEEDED
        ]


def test_oidc_provider_unavailability_is_sanitized_and_reported_as_503(
    tmp_path: Path,
) -> None:
    private_error = OidcUnavailableError("synthetic private provider endpoint")
    with _oidc_context(tmp_path, begin_error=private_error) as context:
        response = context.client.get("/auth/login", follow_redirects=False)

        assert response.status_code == 503
        assert "temporairement indisponible" in response.text
        assert str(private_error) not in response.text
        assert context.client.cookies.get(SESSION_COOKIE_NAME) is None
        assert context.client.cookies.get("mevo2glpi_oidc_transaction") is None
        assert context.audit.events == [
            (
                AuthAuditEvent.LOGIN_FAILED,
                {
                    "auth_mode": AuthMode.OIDC,
                    "succeeded": False,
                    "category": "availability",
                    "technical_code": "oidc_provider_unavailable",
                },
            )
        ]


@pytest.mark.parametrize(
    ("callback_error", "expected_status", "expected_code", "public_message"),
    [
        (
            OidcAuthenticationError("synthetic private provider response"),
            401,
            "oidc_response_invalid",
            "La connexion n’a pas pu être validée.",
        ),
        (
            OidcAuthorizationError("synthetic private role detail"),
            403,
            "required_role_missing",
            "Votre compte n’est pas autorisé à accéder à mevo2glpi.",
        ),
    ],
)
def test_oidc_callback_errors_are_sanitized_and_audited(
    tmp_path: Path,
    callback_error: Exception,
    expected_status: int,
    expected_code: str,
    public_message: str,
) -> None:
    with _oidc_context(tmp_path, callback_error=callback_error) as context:
        context.client.get("/auth/login", follow_redirects=False)

        response = context.client.get(
            "/auth/oidc/callback",
            follow_redirects=False,
        )

        assert response.status_code == expected_status
        assert public_message in response.text
        assert str(callback_error) not in response.text
        assert context.client.cookies.get(SESSION_COOKIE_NAME) is None
        assert context.client.cookies.get("mevo2glpi_oidc_transaction") is None
        assert context.audit.events[0][0] is AuthAuditEvent.LOGIN_FAILED
        assert context.audit.events[0][1]["technical_code"] == expected_code


@pytest.mark.parametrize(
    "unsafe_target",
    [
        "https://attacker.example.test/steal",
        "//attacker.example.test/steal",
        "/\\attacker.example.test/steal",
    ],
)
def test_local_login_never_redirects_to_an_external_target(
    tmp_path: Path,
    unsafe_target: str,
) -> None:
    with _local_context(tmp_path) as context:
        context.client.get("/auth/login")
        response = context.client.post(
            "/auth/login",
            data={
                "username": "operator-authorized",
                "password": _VALID_PASSWORD,
                "csrf_token": context.client.cookies.get(LOGIN_CSRF_COOKIE_NAME),
                "next_path": unsafe_target,
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/"


def test_invalid_session_cookie_is_deleted_without_leaking_its_value(
    tmp_path: Path,
) -> None:
    with _local_context(tmp_path) as context:
        invalid_cookie = "synthetic-private-invalid-cookie"
        context.client.cookies.set(SESSION_COOKIE_NAME, invalid_cookie)

        response = context.client.get("/api/version", follow_redirects=False)

        assert response.status_code == 401
        assert response.json() == {"detail": "Authentication required"}
        assert invalid_cookie not in response.text
        deletion_cookie = next(
            header
            for header in response.headers.get_list("set-cookie")
            if header.startswith(f"{SESSION_COOKIE_NAME}=")
        )
        assert "Max-Age=0" in deletion_cookie
        assert "Path=/" in deletion_cookie


class _ContextManager:
    def __init__(self, client: TestClient, context: _AuthContext) -> None:
        self._test_client = client
        self._context = context

    def __enter__(self) -> _AuthContext:
        self._test_client.__enter__()
        return self._context

    def __exit__(self, *args: object) -> None:
        self._test_client.__exit__(*args)


def _local_context(
    tmp_path: Path,
    *,
    locked_until: datetime | None = None,
    availability_error: Exception | None = None,
) -> _ContextManager:
    store = _LocalAccountStore(
        LocalAccount(
            account_id=_ACCOUNT_ID,
            username="operator-authorized",
            password_hash="synthetic-password-hash",
            is_active=True,
            session_version=0,
            locked_until=locked_until,
        ),
        availability_error=availability_error,
    )
    sessions = _session_manager(AuthMode.LOCAL, local_accounts=store)
    audit = _AuditRecorder()
    services = AuthenticationServices(
        mode=AuthMode.LOCAL,
        sessions=sessions,
        audit=audit,
        local=LocalAuthenticator(
            store,
            _SyntheticPasswordHasher(),
            clock=lambda: datetime(2026, 11, 10, 12, tzinfo=UTC),
        ),
    )
    client = TestClient(
        _app(tmp_path, services=services, cookie_policy=_cookie_policy())
    )
    return _ContextManager(client, _AuthContext(client, sessions, audit))


def _oidc_context(
    tmp_path: Path,
    *,
    begin_error: Exception | None = None,
    callback_error: Exception | None = None,
) -> _ContextManager:
    sessions = _session_manager(AuthMode.OIDC)
    audit = _AuditRecorder()
    oidc = _OidcAuthenticator(
        Principal(
            auth_mode=AuthMode.OIDC,
            subject="oidc:" + "b" * 64,
            roles=frozenset({ACCESS_ROLE}),
        ),
        begin_error=begin_error,
        callback_error=callback_error,
    )
    services = AuthenticationServices(
        mode=AuthMode.OIDC,
        sessions=sessions,
        audit=audit,
        oidc=oidc,  # type: ignore[arg-type]
    )
    client = TestClient(
        _app(
            tmp_path,
            services=services,
            cookie_policy=_cookie_policy(),
            oidc_transaction_secret=_SESSION_SECRET,
        )
    )
    return _ContextManager(client, _AuthContext(client, sessions, audit, oidc))


def _app(
    tmp_path: Path,
    *,
    services: AuthenticationServices,
    cookie_policy: SessionCookiePolicy,
    oidc_transaction_secret: str | None = None,
):
    audio_root = tmp_path / "audios"
    audio_root.mkdir(exist_ok=True)
    unused_service = object()
    return create_app(
        audio_root=audio_root,
        list_audio_jobs=unused_service,  # type: ignore[arg-type]
        get_audio_job=unused_service,  # type: ignore[arg-type]
        list_audio_job_events=unused_service,  # type: ignore[arg-type]
        list_rejected_ingestions=unused_service,  # type: ignore[arg-type]
        retry_audio_job=unused_service,  # type: ignore[arg-type]
        get_downloadable_audio=unused_service,  # type: ignore[arg-type]
        get_observability=unused_service,  # type: ignore[arg-type]
        release_identity=_RELEASE_IDENTITY,
        authentication=services,
        session_cookie_policy=cookie_policy,
        required_auth_role=ACCESS_ROLE,
        oidc_transaction_secret=oidc_transaction_secret,
    )


def _cookie_policy() -> SessionCookiePolicy:
    return SessionCookiePolicy(secure=False, max_age_seconds=1800)


def _session_manager(
    mode: AuthMode,
    *,
    local_accounts: _LocalAccountStore | None = None,
) -> SessionManager:
    return SessionManager(
        SessionCookieCodec(ItsDangerousSessionSigner(_SESSION_SECRET)),
        auth_mode=mode,
        local_accounts=local_accounts,
        clock=lambda: datetime(2026, 11, 10, 12, tzinfo=UTC),
    )


def _login_local(client: TestClient) -> None:
    client.get("/auth/login")
    client.post(
        "/auth/login",
        data={
            "username": "operator-authorized",
            "password": _VALID_PASSWORD,
            "csrf_token": client.cookies.get(LOGIN_CSRF_COOKIE_NAME),
            "next_path": "/",
        },
        follow_redirects=False,
    )
