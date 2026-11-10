"""Default-deny authentication boundary for the web application."""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mevo2glpi_web.auth.http import SessionCookiePolicy
from mevo2glpi_web.auth.models import ACCESS_ROLE
from mevo2glpi_web.auth.sessions import InvalidSessionError, SessionManager

LOGGER = logging.getLogger(__name__)

_PUBLIC_EXACT_PATHS = frozenset(
    {
        "/health",
        "/auth/login",
        "/auth/oidc/callback",
        "/favicon.ico",
    }
)
_PUBLIC_PREFIXES = ("/static/",)


class WebAuthenticationMiddleware:
    """Authenticate every non-public HTTP request before route dispatch."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        session_manager: SessionManager,
        cookie_policy: SessionCookiePolicy,
        required_role: str = ACCESS_ROLE,
    ) -> None:
        self._app = app
        self._session_manager = session_manager
        self._cookie_policy = cookie_policy
        self._required_role = required_role

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _is_public_path(scope.get("path", "")):
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        cookie_value = request.cookies.get(self._cookie_policy.name)
        if cookie_value is None:
            await self._send_unauthenticated(request, send, clear_cookie=False)
            return

        try:
            resolved = await run_in_threadpool(
                self._session_manager.resolve,
                cookie_value,
            )
        except InvalidSessionError:
            await self._send_unauthenticated(request, send, clear_cookie=True)
            return
        except Exception as error:  # pragma: no cover - defensive DB boundary
            LOGGER.error(
                "web authentication temporarily unavailable error_type=%s",
                type(error).__name__,
            )
            response = JSONResponse(
                {"detail": "Authentication is temporarily unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return

        if not resolved.principal.has_role(self._required_role):
            response = JSONResponse(
                {"detail": "Forbidden"},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state["auth_principal"] = resolved.principal
        state["auth_csrf_token"] = resolved.csrf_token
        state["auth_session_expires_at"] = resolved.expires_at

        async def send_protected_response(message: Message) -> None:
            if message.get("type") == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "cache-control" not in headers:
                    headers["Cache-Control"] = "private, no-store"
            await send(message)

        await self._app(scope, receive, send_protected_response)

    async def _send_unauthenticated(
        self,
        request: Request,
        send: Send,
        *,
        clear_cookie: bool,
    ) -> None:
        if _expects_json_authentication(request.url.path):
            response: Response = JSONResponse(
                {"detail": "Authentication required"},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        else:
            query = urlencode({"next": _safe_request_target(request)})
            response = RedirectResponse(
                url=f"/auth/login?{query}",
                status_code=303,
                headers={"Cache-Control": "no-store"},
            )
        if clear_cookie:
            self._cookie_policy.delete(response)
        await response(request.scope, request.receive, send)


def _is_public_path(path: str) -> bool:
    return path in _PUBLIC_EXACT_PATHS or any(
        path.startswith(prefix) for prefix in _PUBLIC_PREFIXES
    )


def _expects_json_authentication(path: str) -> bool:
    return path == "/api" or path.startswith("/api/") or path.startswith("/audios/")


def _safe_request_target(request: Request) -> str:
    path = request.url.path
    if request.url.query:
        target = f"{path}?{request.url.query}"
    else:
        target = path
    return target if len(target) <= 2048 else "/"


__all__ = [
    "WebAuthenticationMiddleware",
]
