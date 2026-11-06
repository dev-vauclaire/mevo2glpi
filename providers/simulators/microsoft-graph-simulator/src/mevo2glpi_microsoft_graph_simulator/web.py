"""FastAPI contracts for the staging-only Microsoft Graph simulator."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from urllib.parse import urlencode
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .audio import SyntheticAudioExtension
from .scenarios import (
    MAX_SEED_AUDIO_BYTES,
    SeedScenario,
    SyntheticAttachmentKind,
)
from .settings import MicrosoftGraphSimulatorSettings
from .state import (
    MicrosoftGraphSimulatorState,
    SyntheticAttachment,
    SyntheticMessage,
)

GRAPH_SYNTHETIC_ACCESS_TOKEN = "synthetic-token"
IMMUTABLE_ID_PREFERENCE = 'IdType="ImmutableId"'


class _GraphContractError(Exception):
    """Carry one sanitized Microsoft Graph error response."""

    def __init__(self, *, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


class SeedMessageRequest(BaseModel):
    """Safe operator input for one synthetic voice message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario: SeedScenario
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("received_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        """Keep Graph dates unambiguous and normalize them to UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        return value.astimezone(UTC)


class SeedMessageResponse(BaseModel):
    """Identifiers safe to expose to a staging operator."""

    model_config = ConfigDict(frozen=True)

    message_id: str
    scenario: SeedScenario | None
    attachment_names: tuple[str, ...]
    attachment_count: int
    received_at: datetime


def create_app(
    settings: MicrosoftGraphSimulatorSettings,
    *,
    state_store: MicrosoftGraphSimulatorState | None = None,
) -> FastAPI:
    """Build the isolated staging HTTP application."""
    if not isinstance(settings, MicrosoftGraphSimulatorSettings):
        raise TypeError("settings must be a MicrosoftGraphSimulatorSettings")
    state_store = state_store or MicrosoftGraphSimulatorState(
        mailbox_id=settings.microsoft_graph_simulator_mailbox_id,
    )
    if state_store.mailbox_id != settings.microsoft_graph_simulator_mailbox_id:
        raise ValueError("state mailbox does not match configured mailbox")

    application = FastAPI(
        title="Microsoft Graph staging simulator",
        version="0.1.0",
    )
    application.state.microsoft_graph_simulator_state = state_store

    @application.exception_handler(_GraphContractError)
    async def graph_contract_error(
        _request: Request,
        error: _GraphContractError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"error": {"code": error.code}},
        )

    @application.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post(
        "/__admin/messages",
        response_model=SeedMessageResponse,
        status_code=status.HTTP_201_CREATED,
        include_in_schema=False,
    )
    async def seed_message(
        payload: SeedMessageRequest,
        request: Request,
    ) -> SeedMessageResponse:
        _require_admin(request, settings)
        message = state_store.seed_scenario(
            scenario=payload.scenario,
            received_at=payload.received_at,
        )
        return _seed_message_response(message)

    @application.post(
        "/__admin/messages/audio",
        response_model=SeedMessageResponse,
        status_code=status.HTTP_201_CREATED,
        include_in_schema=False,
    )
    async def seed_uploaded_audio(request: Request) -> SeedMessageResponse:
        _require_admin(request, settings)
        extension = _uploaded_audio_extension(request)
        received_at = _uploaded_audio_received_at(request)
        content = await _read_bounded_audio(request)
        message = state_store.seed_uploaded_audio(
            extension=extension,
            content=content,
            received_at=received_at,
        )
        return _seed_message_response(message)

    @application.post(
        "/__admin/reset",
        status_code=status.HTTP_204_NO_CONTENT,
        include_in_schema=False,
    )
    async def reset(request: Request) -> Response:
        _require_admin(request, settings)
        state_store.reset()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    graph_root = "/v1.0/users/{mailbox_id}"

    @application.get(f"{graph_root}/mailFolders/inbox/messages")
    async def list_messages(
        mailbox_id: UUID,
        request: Request,
    ) -> dict[str, object]:
        _require_graph(request, state_store, mailbox_id)
        messages = state_store.list_inbox_messages()
        page, next_offset = _page(messages, request)
        payload: dict[str, object] = {
            "value": [_message_payload(message) for message in page]
        }
        if next_offset is not None:
            payload["@odata.nextLink"] = _next_link(request, next_offset)
        return payload

    @application.get(f"{graph_root}/messages/{{message_id}}")
    async def get_message(
        mailbox_id: UUID,
        message_id: str,
        request: Request,
    ) -> dict[str, str]:
        _require_graph(request, state_store, mailbox_id)
        message = state_store.get_message(message_id)
        if message is None:
            raise _graph_not_found()
        return {
            "id": message.message_id,
            "parentFolderId": message.parent_folder_id,
        }

    @application.get(f"{graph_root}/messages/{{message_id}}/attachments")
    async def list_attachments(
        mailbox_id: UUID,
        message_id: str,
        request: Request,
    ) -> dict[str, object]:
        _require_graph(request, state_store, mailbox_id)
        message = state_store.get_message(message_id)
        if message is None:
            raise _graph_not_found()
        return {
            "value": [
                _attachment_payload(attachment) for attachment in message.attachments
            ]
        }

    @application.get(
        f"{graph_root}/messages/{{message_id}}/attachments/{{attachment_id}}/$value"
    )
    async def download_attachment(
        mailbox_id: UUID,
        message_id: str,
        attachment_id: str,
        request: Request,
    ) -> Response:
        _require_graph(request, state_store, mailbox_id)
        message = state_store.get_message(message_id)
        if message is None:
            raise _graph_not_found()
        attachment = next(
            (
                candidate
                for candidate in message.attachments
                if candidate.attachment_id == attachment_id
            ),
            None,
        )
        if attachment is None or attachment.kind is not SyntheticAttachmentKind.FILE:
            raise _graph_not_found()
        return Response(
            content=attachment.content,
            media_type=attachment.media_type,
        )

    @application.get(f"{graph_root}/mailFolders")
    async def list_folders(
        mailbox_id: UUID,
        request: Request,
    ) -> dict[str, object]:
        _require_graph(request, state_store, mailbox_id)
        folders = state_store.list_folders()
        page, next_offset = _page(folders, request)
        payload: dict[str, object] = {
            "value": [
                {"id": folder_id, "displayName": display_name}
                for folder_id, display_name in page
            ]
        }
        if next_offset is not None:
            payload["@odata.nextLink"] = _next_link(request, next_offset)
        return payload

    @application.post(
        f"{graph_root}/mailFolders",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_folder(
        mailbox_id: UUID,
        request: Request,
    ) -> dict[str, str]:
        _require_graph(request, state_store, mailbox_id)
        payload = await _read_json_object(request)
        display_name = payload.get("displayName")
        if not isinstance(display_name, str) or not display_name.strip():
            raise _graph_bad_request("InvalidFolderName")
        folder_id, normalized_name = state_store.ensure_folder(display_name)
        return {"id": folder_id, "displayName": normalized_name}

    @application.post(
        f"{graph_root}/messages/{{message_id}}/move",
        status_code=status.HTTP_201_CREATED,
    )
    async def move_message(
        mailbox_id: UUID,
        message_id: str,
        request: Request,
    ) -> dict[str, str]:
        _require_graph(request, state_store, mailbox_id)
        payload = await _read_json_object(request)
        destination_id = payload.get("destinationId")
        if not isinstance(destination_id, str) or not destination_id:
            raise _graph_bad_request("InvalidDestinationId")
        message = state_store.move_message(message_id, destination_id)
        if message is None:
            raise _graph_not_found()
        return {
            "id": message.message_id,
            "parentFolderId": message.parent_folder_id,
        }

    return application


def _require_admin(
    request: Request,
    settings: MicrosoftGraphSimulatorSettings,
) -> None:
    supplied_token = request.headers.get("x-admin-token", "")
    expected_token = settings.microsoft_graph_simulator_admin_token.get_secret_value()
    if not hmac.compare_digest(supplied_token, expected_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


def _require_graph(
    request: Request,
    state_store: MicrosoftGraphSimulatorState,
    mailbox_id: UUID,
) -> None:
    if mailbox_id != state_store.mailbox_id:
        raise _graph_not_found()
    if request.headers.get("authorization") != (
        f"Bearer {GRAPH_SYNTHETIC_ACCESS_TOKEN}"
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    if request.headers.get("prefer") != IMMUTABLE_ID_PREFERENCE:
        raise _graph_bad_request("MissingImmutableIdPreference")
    if request.headers.get("return-client-request-id") != "true":
        raise _graph_bad_request("InvalidClientRequestId")
    try:
        request_id = UUID(request.headers.get("client-request-id", ""))
    except ValueError:
        raise _graph_bad_request("InvalidClientRequestId") from None
    if not state_store.register_client_request_id(request_id):
        raise _graph_bad_request("DuplicateClientRequestId")


def _message_payload(message: SyntheticMessage) -> dict[str, object]:
    return {
        "id": message.message_id,
        "parentFolderId": message.parent_folder_id,
        "receivedDateTime": message.received_at.isoformat().replace("+00:00", "Z"),
        "hasAttachments": bool(message.attachments),
        "subject": message.subject,
    }


def _attachment_payload(attachment: SyntheticAttachment) -> dict[str, object]:
    return {
        "@odata.type": attachment.kind.odata_type,
        "id": attachment.attachment_id,
        "name": attachment.name,
        "contentType": attachment.media_type,
        "size": attachment.size_bytes,
        "isInline": attachment.is_inline,
    }


def _seed_message_response(message: SyntheticMessage) -> SeedMessageResponse:
    return SeedMessageResponse(
        message_id=message.message_id,
        scenario=message.scenario,
        attachment_names=tuple(item.name for item in message.attachments),
        attachment_count=len(message.attachments),
        received_at=message.received_at,
    )


def _uploaded_audio_extension(request: Request) -> SyntheticAudioExtension:
    raw_extension = request.headers.get("x-audio-extension", "")
    try:
        return SyntheticAudioExtension(raw_extension.casefold())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="audio_extension_invalid",
        ) from None


def _uploaded_audio_received_at(request: Request) -> datetime:
    raw_value = request.headers.get("x-received-at")
    if raw_value is None:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="received_at_invalid",
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="received_at_invalid",
        )
    return parsed.astimezone(UTC)


async def _read_bounded_audio(request: Request) -> bytes:
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="content_length_invalid",
            ) from None
        if content_length > MAX_SEED_AUDIO_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="audio_too_large",
            )

    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_SEED_AUDIO_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="audio_too_large",
            )
    if not content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="audio_empty",
        )
    return bytes(content)


def _page[ItemT](
    items: tuple[ItemT, ...],
    request: Request,
) -> tuple[tuple[ItemT, ...], int | None]:
    try:
        page_size = int(request.query_params.get("$top", "50"))
        offset = int(request.query_params.get("$skiptoken", "0"))
    except ValueError:
        raise _graph_bad_request("InvalidPagination") from None
    if not 1 <= page_size <= 1_000 or offset < 0:
        raise _graph_bad_request("InvalidPagination")
    page = items[offset : offset + page_size]
    next_offset = offset + len(page)
    return page, next_offset if next_offset < len(items) else None


def _next_link(request: Request, next_offset: int) -> str:
    query = dict(request.query_params)
    query["$skiptoken"] = str(next_offset)
    return str(request.url.replace(query=urlencode(query)))


async def _read_json_object(request: Request) -> dict[str, object]:
    try:
        payload = await request.json()
    except ValueError:
        raise _graph_bad_request("InvalidJson") from None
    if not isinstance(payload, dict):
        raise _graph_bad_request("InvalidJson")
    return payload


def _graph_bad_request(code: str) -> _GraphContractError:
    return _GraphContractError(
        status_code=status.HTTP_400_BAD_REQUEST,
        code=code,
    )


def _graph_not_found() -> _GraphContractError:
    return _GraphContractError(
        status_code=status.HTTP_404_NOT_FOUND,
        code="NotFound",
    )


__all__ = [
    "GRAPH_SYNTHETIC_ACCESS_TOKEN",
    "IMMUTABLE_ID_PREFERENCE",
    "SeedMessageRequest",
    "SeedMessageResponse",
    "create_app",
]
