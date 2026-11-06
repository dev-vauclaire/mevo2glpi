"""Route declaration for deployment identity metadata."""

from fastapi import APIRouter, Response

from mevo2glpi_web.controllers import VersionController
from mevo2glpi_web.schemas import VersionResponse


def create_version_router(controller: VersionController) -> APIRouter:
    """Bind the deployment identity endpoint to its controller."""
    router = APIRouter()

    @router.get("/api/version", response_model=VersionResponse)
    def get_version(response: Response) -> VersionResponse:
        return controller.get_version(response)

    return router


__all__ = ["create_version_router"]
