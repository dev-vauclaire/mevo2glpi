"""Route declarations for web authentication."""

from typing import Annotated

from fastapi import APIRouter, Form, Query, Request, Response

from mevo2glpi_web.controllers.authentication import AuthenticationController
from mevo2glpi_web.schemas.authentication import LocalLoginForm, LogoutForm

NextPath = Annotated[str | None, Query(alias="next", max_length=2048)]
LocalLoginData = Annotated[LocalLoginForm, Form()]
LogoutData = Annotated[LogoutForm, Form()]


def create_authentication_router(controller: AuthenticationController) -> APIRouter:
    """Bind login, OIDC callback and logout URLs to the controller."""
    router = APIRouter()

    @router.get("/auth/login", name="auth_login")
    async def login(request: Request, next_path: NextPath = None) -> Response:
        return await controller.login(request, next_path)

    @router.post("/auth/login", name="auth_local_login")
    async def local_login(request: Request, form: LocalLoginData) -> Response:
        return await controller.local_login(request, form)

    @router.get("/auth/oidc/callback", name="auth_oidc_callback")
    async def oidc_callback(request: Request) -> Response:
        return await controller.oidc_callback(request)

    @router.post("/auth/logout", name="auth_logout")
    async def logout(request: Request, form: LogoutData) -> Response:
        return await controller.logout(request, form)

    return router


__all__ = ["create_authentication_router"]
