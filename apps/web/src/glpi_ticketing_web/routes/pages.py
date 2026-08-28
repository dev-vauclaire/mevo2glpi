"""Route declarations for server-rendered pages."""

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from glpi_ticketing_web.controllers import PageController


def create_pages_router(controller: PageController) -> APIRouter:
    """Bind page URLs to their controller."""
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        return controller.index(request)

    return router


__all__ = ["create_pages_router"]
