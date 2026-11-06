"""FastAPI contracts for the staging-only Transcribe AI simulator."""

from __future__ import annotations

import hmac
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .settings import TranscribeAiSimulatorSettings
from .state import TranscribeAiSimulatorState

SYNTHETIC_TRANSCRIPTION = "Transcription synthétique de recette staging."
_MAX_MULTIPART_BYTES = 26 * 1024 * 1024


def create_app(
    settings: TranscribeAiSimulatorSettings,
    *,
    state_store: TranscribeAiSimulatorState | None = None,
) -> FastAPI:
    """Build the isolated staging HTTP application."""
    if not isinstance(settings, TranscribeAiSimulatorSettings):
        raise TypeError("settings must be a TranscribeAiSimulatorSettings")
    state_store = state_store or TranscribeAiSimulatorState()

    application = FastAPI(
        title="Transcribe AI staging simulator",
        version="0.1.0",
    )
    application.state.transcribe_ai_simulator_state = state_store

    @application.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/api/batchTranscription/createJob")
    async def create_transcription_job(request: Request) -> JSONResponse:
        _require_api_key(request, settings)
        content_type = request.headers.get("content-type", "")
        if not content_type.casefold().startswith("multipart/form-data;"):
            return _error("invalid_multipart_request", status_code=400)
        declared_length = _declared_content_length(request)
        if declared_length is not None and declared_length > _MAX_MULTIPART_BYTES:
            return _error("audio_too_large", status_code=413)
        body = await request.body()
        if len(body) > _MAX_MULTIPART_BYTES:
            return _error("audio_too_large", status_code=413)
        if b'name="audioFile"' not in body or b"filename=" not in body:
            return _error("audio_file_missing", status_code=400)
        job = state_store.create_job()
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
    async def get_transcription_result(
        request: Request,
        job_uuid: UUID,
    ) -> JSONResponse:
        _require_api_key(request, settings)
        polling = state_store.poll_job(job_uuid)
        if polling is None:
            return _error("job_not_found", status_code=404)
        job, completed = polling
        data: dict[str, object] = {
            # The pinned provider contract names this UUID ``job_id`` here.
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


def _require_api_key(
    request: Request,
    settings: TranscribeAiSimulatorSettings,
) -> None:
    supplied_key = request.headers.get("x-api-key", "")
    expected_key = settings.transcribe_ai_simulator_api_key.get_secret_value()
    if not hmac.compare_digest(supplied_key, expected_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


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


def _error(message: str, *, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"status": "error", "message": message},
    )


__all__ = ["SYNTHETIC_TRANSCRIPTION", "create_app"]
