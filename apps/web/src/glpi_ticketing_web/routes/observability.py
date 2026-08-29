"""Route declaration for dashboard observability."""

from fastapi import APIRouter

from glpi_ticketing_web.controllers import ObservabilityController
from glpi_ticketing_web.schemas import ObservabilityResponse


def create_observability_router(controller: ObservabilityController) -> APIRouter:
    """Bind the internal observability endpoint to its controller."""
    router = APIRouter()

    @router.get("/api/observability", response_model=ObservabilityResponse)
    def get_observability() -> ObservabilityResponse:
        return controller.get_observability()

    return router


__all__ = ["create_observability_router"]
