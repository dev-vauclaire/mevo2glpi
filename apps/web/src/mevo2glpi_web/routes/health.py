"""Route declarations for process health."""

from fastapi import APIRouter

from mevo2glpi_web.controllers import HealthController


def create_health_router(controller: HealthController) -> APIRouter:
    """Bind the health URL to its controller."""
    router = APIRouter()

    @router.get("/health")
    def health() -> dict[str, str]:
        return controller.health()

    return router


__all__ = ["create_health_router"]
