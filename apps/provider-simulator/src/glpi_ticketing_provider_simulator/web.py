"""FastAPI contracts for the staging-only Graph and transcription simulators."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from urllib.parse import urlencode
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .audio import SyntheticAudioExtension
from .settings import ProviderSimulatorSettings
from .state import ProviderSimulatorState, SyntheticMessage

GRAPH_SYNTHETIC_ACCESS_TOKEN = "synthetic-token"
IMMUTABLE_ID_PREFERENCE = 'IdType="ImmutableId"'
SYNTHETIC_TRANSCRIPTION = "Transcription synthétique de recette staging."
_MAX_MULTIPART_BYTES = 26 * 1024 * 1024


class SeedMessageRequest(BaseModel):
    """Safe operator input for one synthetic voice message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    extension: SyntheticAudioExtension
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
    attachment_name: str
    received_at: datetime


def create_app(
    settings: ProviderSimulatorSettings,
    *,
    state_store: ProviderSimulatorState | None = None,
) -> FastAPI:
    """Build the isolated staging HTTP application."""
    if not isinstance(settings, ProviderSimulatorSettings):
        raise TypeError("settings must be a ProviderSimulatorSettings")
    state_store = state_store or ProviderSimulatorState(
        mailbox_id=settings.provider_simulator_mailbox_id,
    )
    if state_store.mailbox_id != settings.provider_simulator_mailbox_id:
        raise ValueError("state mailbox does not match configured mailbox")

    application = FastAPI(
        title="GLPI ticketing provider simulator",
        version="0.1.0",
    )
    application.state.provider_simulator_state = state_store

    @application.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post(
        "/__admin/messages",
        response_model=SeedMessageResponse,
        status_code=status.HTTP_201_CREATED,
        include_in_schema=False,
    )
    def seed_message(
        payload: SeedMessageRequest,
        request: Request,
    ) -> SeedMessageResponse:
        _require_admin(request, settings)
        message = state_store.seed_message(
            extension=payload.extension,
            received_at=payload.received_at,
        )
        return SeedMessageResponse(
            message_id=message.message_id,
            attachment_name=message.attachment.name,
            received_at=message.received_at,
        )

    @application.post(
        "/__admin/reset",
        status_code=status.HTTP_204_NO_CONTENT,
        include_in_schema=False,
    )
    def reset(request: Request) -> Response:
        _require_admin(request, settings)
        state_store.reset()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    graph_root = "/v1.0/users/{mailbox_id}"

    @application.get(f"{graph_root}/mailFolders/inbox/messages")
    def list_messages(mailbox_id: UUID, request: Request) -> dict[str, object]:
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
    def get_message(
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
    def list_attachments(
        mailbox_id: UUID,
        message_id: str,
        request: Request,
    ) -> dict[str, object]:
        _require_graph(request, state_store, mailbox_id)
        message = state_store.get_message(message_id)
        if message is None:
            raise _graph_not_found()
        attachment = message.attachment
        return {
            "value": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "id": attachment.attachment_id,
                    "name": attachment.name,
                    "contentType": attachment.media_type,
                    "size": len(attachment.content),
                    "isInline": False,
                }
            ]
        }

    @application.get(
        f"{graph_root}/messages/{{message_id}}/attachments/{{attachment_id}}/$value"
    )
    def download_attachment(
        mailbox_id: UUID,
        message_id: str,
        attachment_id: str,
        request: Request,
    ) -> Response:
        _require_graph(request, state_store, mailbox_id)
        message = state_store.get_message(message_id)
        if message is None or message.attachment.attachment_id != attachment_id:
            raise _graph_not_found()
        return Response(
            content=message.attachment.content,
            media_type=message.attachment.media_type,
        )

    @application.get(f"{graph_root}/mailFolders")
    def list_folders(mailbox_id: UUID, request: Request) -> dict[str, object]:
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

    @application.post("/api/batchTranscription/createJob")
    async def create_transcription_job(request: Request) -> JSONResponse:
        _require_transcription_api_key(request, settings)
        content_type = request.headers.get("content-type", "")
        if not content_type.casefold().startswith("multipart/form-data;"):
            return _transcription_error("invalid_multipart_request", status_code=400)
        declared_length = _declared_content_length(request)
        if declared_length is not None and declared_length > _MAX_MULTIPART_BYTES:
            return _transcription_error("audio_too_large", status_code=413)
        body = await request.body()
        if len(body) > _MAX_MULTIPART_BYTES:
            return _transcription_error("audio_too_large", status_code=413)
        if b'name="audioFile"' not in body or b"filename=" not in body:
            return _transcription_error("audio_file_missing", status_code=400)
        job = state_store.create_transcription_job()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "success",
                "data": {
                    "job_uuid": str(job.job_uuid),
                    "status": "Votre demande est dans la file d'attente",
                },
            },
        )

    @application.get("/api/batchTranscription/result")
    def get_transcription_result(
        request: Request,
        job_uuid: UUID,
    ) -> JSONResponse:
        _require_transcription_api_key(request, settings)
        polling = state_store.poll_transcription_job(job_uuid)
        if polling is None:
            return _transcription_error("job_not_found", status_code=404)
        job, completed = polling
        data: dict[str, object] = {
            "job_id": str(job.job_uuid),
            "status": "COMPLETED" if completed else "PENDING",
        }
        if completed:
            data["result"] = {
                "full_text": SYNTHETIC_TRANSCRIPTION,
                "language": "fr",
                "segments": [],
            }
            data["transcription_time"] = 0.01
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "success", "data": data},
        )

    return application


def _require_admin(
    request: Request,
    settings: ProviderSimulatorSettings,
) -> None:
    supplied_token = request.headers.get("x-admin-token", "")
    expected_token = settings.provider_simulator_admin_token.get_secret_value()
    if not hmac.compare_digest(supplied_token, expected_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


def _require_graph(
    request: Request,
    state_store: ProviderSimulatorState,
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


def _require_transcription_api_key(
    request: Request,
    settings: ProviderSimulatorSettings,
) -> None:
    supplied_key = request.headers.get("x-api-key", "")
    expected_key = settings.provider_simulator_transcription_api_key.get_secret_value()
    if not hmac.compare_digest(supplied_key, expected_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


def _message_payload(message: SyntheticMessage) -> dict[str, object]:
    return {
        "id": message.message_id,
        "parentFolderId": message.parent_folder_id,
        "receivedDateTime": (message.received_at.isoformat().replace("+00:00", "Z")),
        "hasAttachments": True,
    }


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


def _declared_content_length(request: Request) -> int | None:
    raw_value = request.headers.get("content-length")
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from None
    if value < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    return value


def _graph_bad_request(code: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": {"code": code}},
    )


def _graph_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": {"code": "NotFound"}},
    )


def _transcription_error(message: str, *, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"status": "error", "message": message},
    )


__all__ = [
    "GRAPH_SYNTHETIC_ACCESS_TOKEN",
    "IMMUTABLE_ID_PREFERENCE",
    "SYNTHETIC_TRANSCRIPTION",
    "SeedMessageRequest",
    "SeedMessageResponse",
    "create_app",
]
