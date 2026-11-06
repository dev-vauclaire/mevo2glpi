"""Strict synchronous transcription subset compatible with the OpenAI API."""

from __future__ import annotations

import hmac

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException

from .settings import OpenAiTranscriptionSimulatorSettings

SYNTHETIC_TRANSCRIPTION = "Transcription synthétique de développement."
EXPECTED_MULTIPART_FIELDS = frozenset({"file", "model", "language", "response_format"})
MULTIPART_OVERHEAD_ALLOWANCE_BYTES = 64 * 1024


def create_app(settings: OpenAiTranscriptionSimulatorSettings) -> FastAPI:
    """Build the isolated, stateless HTTP application."""
    if not isinstance(settings, OpenAiTranscriptionSimulatorSettings):
        raise TypeError("settings must be an OpenAiTranscriptionSimulatorSettings")

    application = FastAPI(
        title="OpenAI-compatible transcription simulator",
        version="0.1.0",
    )

    @application.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/v1/audio/transcriptions")
    async def transcribe(request: Request) -> JSONResponse:
        _require_bearer(request, settings)
        _require_multipart(request)
        _require_bounded_content_length(request, settings)
        form = await _read_form(request, settings)
        try:
            upload = _require_contract(form)
            content = await upload.read(
                settings.openai_transcription_simulator_max_upload_bytes + 1
            )
        finally:
            for value in form.values():
                if isinstance(value, UploadFile):
                    await value.close()

        if not content:
            return _error("audio_empty", status_code=400)
        if len(content) > settings.openai_transcription_simulator_max_upload_bytes:
            return _error("audio_too_large", status_code=413)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"text": SYNTHETIC_TRANSCRIPTION},
        )

    return application


def _require_bearer(
    request: Request,
    settings: OpenAiTranscriptionSimulatorSettings,
) -> None:
    supplied = request.headers.get("authorization", "")
    expected = (
        f"Bearer {settings.openai_transcription_simulator_api_key.get_secret_value()}"
    )
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


def _require_multipart(request: Request) -> None:
    content_type = request.headers.get("content-type", "")
    if not content_type.casefold().startswith("multipart/form-data;"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_multipart_request",
        )


def _require_bounded_content_length(
    request: Request,
    settings: OpenAiTranscriptionSimulatorSettings,
) -> None:
    raw_value = request.headers.get("content-length")
    if raw_value is None:
        return
    try:
        value = int(raw_value)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from None
    if value < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    if value > (
        settings.openai_transcription_simulator_max_upload_bytes
        + MULTIPART_OVERHEAD_ALLOWANCE_BYTES
    ):
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE)


async def _read_form(
    request: Request,
    settings: OpenAiTranscriptionSimulatorSettings,
) -> FormData:
    maximum_body_bytes = (
        settings.openai_transcription_simulator_max_upload_bytes
        + MULTIPART_OVERHEAD_ALLOWANCE_BYTES
    )
    chunks: list[bytes] = []
    body_size = 0
    async for chunk in request.stream():
        body_size += len(chunk)
        if body_size > maximum_body_bytes:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE)
        chunks.append(chunk)
    body = b"".join(chunks)

    delivered = False

    async def receive() -> dict[str, object]:
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    bounded_request = Request(request.scope, receive)
    try:
        return await bounded_request.form(
            max_files=1,
            max_fields=3,
            max_part_size=settings.openai_transcription_simulator_max_upload_bytes,
        )
    except (MultiPartException, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_multipart_request",
        ) from None


def _require_contract(form: FormData) -> UploadFile:
    if set(form) != EXPECTED_MULTIPART_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_transcription_request",
        )

    upload = form["file"]
    model = form["model"]
    language = form["language"]
    response_format = form["response_format"]
    if not isinstance(upload, UploadFile) or not upload.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="audio_file_missing",
        )
    if not _valid_text(model, maximum_length=255):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="model_invalid",
        )
    if not _valid_text(language, maximum_length=32):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="language_invalid",
        )
    if response_format != "json":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="response_format_not_supported",
        )
    return upload


def _valid_text(value: object, *, maximum_length: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= maximum_length
        and all(character.isprintable() for character in value)
    )


def _error(code: str, *, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": "Synthetic transcription request rejected",
                "type": "invalid_request_error",
            }
        },
    )


__all__ = ["SYNTHETIC_TRANSCRIPTION", "create_app"]
