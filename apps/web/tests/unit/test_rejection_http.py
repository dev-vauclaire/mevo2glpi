"""HTTP contracts for the rejected-ingestion dashboard view."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException

from mevo2glpi_web.controllers import RejectionController
from mevo2glpi_web.routes import create_rejections_router
from mevo2glpi_web.services import (
    InvalidCursorError,
    RejectedIngestionListPage,
    RejectedIngestionQueryUnavailableError,
    RejectedIngestionView,
)

INGESTION_UUID = UUID("d952968c-36af-51d9-989c-c136bd2f13d2")
NOW = datetime(2026, 10, 30, 9, 17, tzinfo=UTC)


class FakeListRejectedIngestions:
    def __init__(self, result: RejectedIngestionListPage | Exception) -> None:
        self.result = result

    def execute(self, **_kwargs: object) -> RejectedIngestionListPage:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_controller_exposes_only_sanitized_rejection_metadata() -> None:
    page = RejectedIngestionListPage(
        items=(
            RejectedIngestionView(
                ingestion_uuid=INGESTION_UUID,
                rejection_category="invalid_attachment",
                rejection_code="audio_format_not_supported",
                received_at=NOW,
                rejected_at=NOW,
            ),
        ),
        next_cursor=None,
    )
    controller = RejectionController(
        FakeListRejectedIngestions(page)  # type: ignore[arg-type]
    )

    response = controller.list_rejections(limit=25, cursor=None)
    serialized = response.model_dump(mode="json")

    assert serialized == {
        "items": [
            {
                "ingestion_uuid": str(INGESTION_UUID),
                "received_at": "2026-10-30T09:17:00Z",
                "rejected_at": "2026-10-30T09:17:00Z",
                "rejection_category": "invalid_attachment",
                "rejection_code": "audio_format_not_supported",
            }
        ],
        "next_cursor": None,
    }
    assert "message_id" not in serialized["items"][0]
    assert "mailbox_id" not in serialized["items"][0]
    assert "attachment_id" not in serialized["items"][0]


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (InvalidCursorError("private cursor"), 422),
        (RejectedIngestionQueryUnavailableError("private database URL"), 503),
    ],
)
def test_controller_sanitizes_query_failures(
    error: Exception,
    expected_status: int,
) -> None:
    controller = RejectionController(
        FakeListRejectedIngestions(error)  # type: ignore[arg-type]
    )

    with pytest.raises(HTTPException) as captured:
        controller.list_rejections(limit=25, cursor=None)

    assert captured.value.status_code == expected_status
    assert "private" not in str(captured.value.detail)


def test_rejection_route_is_read_only_and_has_bounded_pagination() -> None:
    app = FastAPI()
    app.include_router(create_rejections_router(object()))  # type: ignore[arg-type]
    operation = app.openapi()["paths"]["/api/rejections"]

    assert set(operation) == {"get"}
    parameters = {
        parameter["name"]: parameter for parameter in operation["get"]["parameters"]
    }
    assert parameters["limit"]["schema"] == {
        "type": "integer",
        "maximum": 100,
        "minimum": 1,
        "default": 25,
        "title": "Limit",
    }
    cursor_schema = parameters["cursor"]["schema"]["anyOf"][0]
    assert cursor_schema["type"] == "string"
    assert cursor_schema["minLength"] == 1
    assert cursor_schema["maxLength"] == 512
