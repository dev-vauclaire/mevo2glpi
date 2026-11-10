"""HTTP translation for local and generic OIDC authentication."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from mevo2glpi_web.auth.audit import AuthAuditEvent, AuthAuditRecorder
from mevo2glpi_web.auth.http import (
    LOGIN_CSRF_COOKIE_NAME,
    LoginCsrfProtection,
    SessionCookiePolicy,
    safe_next_path,
)
from mevo2glpi_web.auth.local import (
    LocalAuthenticationError,
    LocalAuthenticationFailureReason,
    LocalAuthenticationUnavailableError,
    LocalAuthenticator,
)
from mevo2glpi_web.auth.models import AuthMode, Principal
from mevo2glpi_web.auth.oidc import (
    OidcAuthenticationError,
    OidcAuthenticator,
    OidcAuthorizationError,
    OidcUnavailableError,
)
from mevo2glpi_web.auth.sessions import InvalidSessionError, SessionManager
from mevo2glpi_web.schemas.authentication import LocalLoginForm, LogoutForm

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuthenticationServices:
    """Mode-specific authenticators around one common application session."""

    mode: AuthMode
    sessions: SessionManager
    audit: AuthAuditRecorder
    local: LocalAuthenticator | None = None
    oidc: OidcAuthenticator | None = None

    def __post_init__(self) -> None:
        if self.mode is AuthMode.LOCAL and (
            self.local is None or self.oidc is not None
        ):
            raise ValueError("local mode requires only the local authenticator")
        if self.mode is AuthMode.OIDC and (self.oidc is None or self.local is not None):
            raise ValueError("OIDC mode requires only the OIDC authenticator")


class AuthenticationController:
    """Build sanitized authentication responses for the selected mode."""

    def __init__(
        self,
        *,
        templates: Jinja2Templates,
        services: AuthenticationServices,
        cookie_policy: SessionCookiePolicy,
        login_csrf: LoginCsrfProtection,
    ) -> None:
        self._templates = templates
        self._services = services
        self._cookie_policy = cookie_policy
        self._login_csrf = login_csrf

    async def login(self, request: Request, next_path: str | None) -> Response:
        """Render local login or begin the configured OIDC code flow."""
        target = safe_next_path(next_path)
        if self._services.mode is AuthMode.OIDC:
            request.session["mevo2glpi_post_auth_redirect"] = target
            assert self._services.oidc is not None
            try:
                response = await self._services.oidc.begin(request)
            except OidcUnavailableError:
                request.session.clear()
                await self._record(
                    AuthAuditEvent.LOGIN_FAILED,
                    auth_mode=AuthMode.OIDC,
                    succeeded=False,
                    category="availability",
                    technical_code="oidc_provider_unavailable",
                )
                return self._render_oidc_error(
                    request,
                    message="Le fournisseur d’identité est temporairement indisponible.",
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            if not isinstance(response, Response):
                raise RuntimeError("OIDC client returned an invalid redirect")
            return response
        return self._render_local_login(request, target=target)

    async def local_login(
        self,
        request: Request,
        form: LocalLoginForm,
    ) -> Response:
        """Authenticate a local account with a generic client-facing failure."""
        if self._services.mode is not AuthMode.LOCAL:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if not self._login_csrf.validate(
            request.cookies.get(LOGIN_CSRF_COOKIE_NAME),
            form.csrf_token,
        ):
            return self._render_local_login(
                request,
                target=safe_next_path(form.next_path),
                error="La demande de connexion a expiré. Veuillez réessayer.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        assert self._services.local is not None
        try:
            principal = await run_in_threadpool(
                self._services.local.authenticate,
                form.username,
                form.password.get_secret_value(),
            )
        except LocalAuthenticationUnavailableError:
            await self._record(
                AuthAuditEvent.LOGIN_FAILED,
                auth_mode=AuthMode.LOCAL,
                succeeded=False,
                category="availability",
                technical_code="local_account_store_unavailable",
            )
            return self._render_local_login(
                request,
                target=safe_next_path(form.next_path),
                error="Le service de connexion est temporairement indisponible.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except LocalAuthenticationError as error:
            throttled = error.reason is LocalAuthenticationFailureReason.THROTTLED
            await self._record(
                (
                    AuthAuditEvent.LOGIN_THROTTLED
                    if throttled
                    else AuthAuditEvent.LOGIN_FAILED
                ),
                auth_mode=AuthMode.LOCAL,
                succeeded=False,
                category="throttling" if throttled else "authentication",
                technical_code=(
                    "login_temporarily_locked" if throttled else "credentials_invalid"
                ),
            )
            return self._render_local_login(
                request,
                target=safe_next_path(form.next_path),
                error="Identifiant ou mot de passe incorrect.",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        issued = self._services.sessions.issue(principal)
        await self._record_success(principal)
        response = RedirectResponse(
            safe_next_path(form.next_path),
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Cache-Control": "no-store"},
        )
        self._cookie_policy.set(response, issued.cookie_value)
        self._login_csrf.delete_cookie(response)
        return response

    async def oidc_callback(self, request: Request) -> Response:
        """Complete Authlib validation and create the minimal application cookie."""
        if self._services.mode is not AuthMode.OIDC:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        assert self._services.oidc is not None
        target = safe_next_path(
            request.session.pop("mevo2glpi_post_auth_redirect", "/")
        )
        try:
            principal = await self._services.oidc.complete(request)
        except OidcAuthorizationError:
            request.session.clear()
            await self._record(
                AuthAuditEvent.LOGIN_FAILED,
                auth_mode=AuthMode.OIDC,
                succeeded=False,
                category="authorization",
                technical_code="required_role_missing",
            )
            return self._render_oidc_error(
                request,
                message="Votre compte n’est pas autorisé à accéder à mevo2glpi.",
                status_code=status.HTTP_403_FORBIDDEN,
            )
        except OidcAuthenticationError:
            request.session.clear()
            await self._record(
                AuthAuditEvent.LOGIN_FAILED,
                auth_mode=AuthMode.OIDC,
                succeeded=False,
                category="authentication",
                technical_code="oidc_response_invalid",
            )
            return self._render_oidc_error(
                request,
                message="La connexion n’a pas pu être validée.",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        request.session.clear()
        issued = self._services.sessions.issue(principal)
        await self._record_success(principal)
        response = RedirectResponse(
            target,
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Cache-Control": "no-store"},
        )
        self._cookie_policy.set(response, issued.cookie_value)
        return response

    async def logout(self, request: Request, form: LogoutForm) -> Response:
        """Validate CSRF, clear the application cookie and audit the logout."""
        cookie_value = request.cookies.get(self._cookie_policy.name)
        if cookie_value is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        try:
            principal = await run_in_threadpool(
                self._services.sessions.validate_csrf,
                cookie_value,
                form.csrf_token,
            )
        except InvalidSessionError as error:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from error

        try:
            await self._record(
                AuthAuditEvent.LOGOUT,
                auth_mode=principal.auth_mode,
                succeeded=True,
            )
        except Exception as error:  # logout must remain possible during audit failure
            LOGGER.error(
                "authentication logout audit failed error_type=%s",
                type(error).__name__,
            )
        if self._services.mode is AuthMode.OIDC:
            request.session.clear()
        response = RedirectResponse(
            "/auth/login",
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Cache-Control": "no-store"},
        )
        self._cookie_policy.delete(response)
        return response

    def _render_local_login(
        self,
        request: Request,
        *,
        target: str,
        error: str | None = None,
        status_code: int = status.HTTP_200_OK,
    ) -> Response:
        csrf_token = self._login_csrf.issue()
        response = self._templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "csrf_token": csrf_token,
                "next_path": target,
                "error": error,
            },
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )
        self._login_csrf.set_cookie(response, csrf_token)
        return response

    def _render_oidc_error(
        self,
        request: Request,
        *,
        message: str,
        status_code: int,
    ) -> Response:
        return self._templates.TemplateResponse(
            request=request,
            name="authentication-error.html",
            context={"message": message},
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    async def _record_success(self, principal: Principal) -> None:
        await self._record(
            AuthAuditEvent.LOGIN_SUCCEEDED,
            auth_mode=principal.auth_mode,
            succeeded=True,
        )

    async def _record(
        self,
        event_type: AuthAuditEvent,
        *,
        auth_mode: AuthMode,
        succeeded: bool,
        category: str | None = None,
        technical_code: str | None = None,
    ) -> None:
        await run_in_threadpool(
            self._services.audit.record,
            event_type,
            auth_mode=auth_mode,
            succeeded=succeeded,
            category=category,
            technical_code=technical_code,
        )


__all__ = ["AuthenticationController", "AuthenticationServices"]
