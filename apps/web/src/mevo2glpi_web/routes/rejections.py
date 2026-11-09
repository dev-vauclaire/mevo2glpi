"""Route declarations for rejected mail ingestions."""

from typing import Annotated

from fastapi import APIRouter, Query

from mevo2glpi_web.controllers import RejectionController
from mevo2glpi_web.schemas import RejectedIngestionListResponse

PageLimit = Annotated[int, Query(ge=1, le=100)]
PageCursor = Annotated[str | None, Query(min_length=1, max_length=512)]


def create_rejections_router(controller: RejectionController) -> APIRouter:
    """Bind rejected-mail URLs to their controller."""
    router = APIRouter()

    @router.get("/api/rejections", response_model=RejectedIngestionListResponse)
    def list_rejections(
        limit: PageLimit = 25,
        cursor: PageCursor = None,
    ) -> RejectedIngestionListResponse:
        return controller.list_rejections(limit=limit, cursor=cursor)

    return router


__all__ = ["create_rejections_router"]
