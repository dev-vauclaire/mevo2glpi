"""HTTP translation for rejected mail ingestions."""

from fastapi import HTTPException, status

from mevo2glpi_web.schemas import (
    RejectedIngestionListResponse,
    RejectedIngestionResponse,
)
from mevo2glpi_web.services import (
    InvalidCursorError,
    ListRejectedIngestions,
    RejectedIngestionQueryUnavailableError,
)


class RejectionController:
    """Translate HTTP requests and query outcomes for rejected messages."""

    def __init__(self, list_rejected_ingestions: ListRejectedIngestions) -> None:
        self._list_rejected_ingestions = list_rejected_ingestions

    def list_rejections(
        self,
        *,
        limit: int,
        cursor: str | None,
    ) -> RejectedIngestionListResponse:
        """Return one sanitized keyset page for the dashboard."""
        try:
            page = self._list_rejected_ingestions.execute(
                limit=limit,
                cursor=cursor,
            )
        except InvalidCursorError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Invalid pagination cursor",
            ) from error
        except RejectedIngestionQueryUnavailableError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Rejected messages are temporarily unavailable",
            ) from error
        return RejectedIngestionListResponse(
            items=tuple(
                RejectedIngestionResponse(
                    ingestion_uuid=item.ingestion_uuid,
                    received_at=item.received_at,
                    rejected_at=item.rejected_at,
                    rejection_category=item.rejection_category,
                    rejection_code=item.rejection_code,
                )
                for item in page.items
            ),
            next_cursor=page.next_cursor,
        )


__all__ = ["RejectionController"]
