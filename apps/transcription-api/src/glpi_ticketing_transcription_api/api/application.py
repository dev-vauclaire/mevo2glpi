"""FastAPI assembly for the strict OpenAI-compatible transcription subset."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile, status
from pydantic import SecretStr

from glpi_ticketing_transcription_api import __version__
from glpi_ticketing_transcription_api.audio import (
    AudioDurationExceeded,
    AudioNormalizationError,
    FFmpegNormalizer,
)
from glpi_ticketing_transcription_api.engines import (
    EngineInferenceError,
    EngineLoadError,
    TranscriptionEngine,
)

from .schemas import HealthResponse, TranscriptionResponse

LOGGER = logging.getLogger(__name__)
_READ_CHUNK_BYTES = 1024 * 1024
_SUPPORTED_MEDIA_TYPES = frozenset(
    {
        "application/octet-stream",
        "audio/mp4",
        "audio/mpeg",
        "audio/ogg",
        "audio/wav",
        "audio/wave",
        "audio/x-m4a",
        "audio/x-wav",
    }
)


def create_app(
    *,
    engine: TranscriptionEngine,
    normalizer: FFmpegNormalizer,
    api_key: SecretStr,
    model_name: str,
    max_upload_bytes: int,
) -> FastAPI:
    """Compose the HTTP boundary around injected audio and inference services."""
    if not isinstance(api_key, SecretStr):
        raise TypeError("api_key must be a SecretStr")
    expected_api_key = api_key.get_secret_value()
    if not expected_api_key:
        raise ValueError("api_key cannot be empty")

    inference_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            await asyncio.to_thread(engine.load)
        except EngineLoadError as error:
            LOGGER.error(
                "transcription engine unavailable error_type=%s",
                type(error).__name__,
            )
            raise
        try:
            yield
        finally:
            await asyncio.to_thread(engine.close)

    app = FastAPI(
        title="mevo2glpi transcription API",
        version=__version__,
        lifespan=lifespan,
    )

    @app.get("/health/live", response_model=HealthResponse)
    async def live() -> HealthResponse:
        return HealthResponse(status="live")

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"description": "Inference engine is unavailable"}},
    )
    async def ready() -> HealthResponse:
        if not engine.is_ready:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="transcription_engine_unavailable",
            )
        return HealthResponse(status="ready")

    @app.post(
        "/v1/audio/transcriptions",
        response_model=TranscriptionResponse,
        responses={
            401: {"description": "Missing or invalid bearer token"},
            413: {"description": "Audio exceeds a configured limit"},
            415: {"description": "Unsupported media type"},
            422: {"description": "Invalid audio"},
            429: {"description": "The single CPU inference slot is busy"},
            503: {"description": "Inference engine is unavailable"},
        },
    )
    async def transcribe(
        file: Annotated[UploadFile, File()],
        model: Annotated[str, Form(min_length=1, max_length=255)],
        language: Annotated[
            str | None,
            Form(min_length=1, max_length=32),
        ] = None,
        response_format: Annotated[
            str,
            Form(min_length=1, max_length=32),
        ] = "json",
        authorization: Annotated[str | None, Header()] = None,
    ) -> TranscriptionResponse:
        _require_bearer_token(authorization, expected_api_key)
        if not engine.is_ready:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="transcription_engine_unavailable",
            )
        if model != model_name or response_format != "json":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="unsupported_transcription_request",
            )
        if file.content_type not in _SUPPORTED_MEDIA_TYPES:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="unsupported_audio_media_type",
            )
        if inference_lock.locked():
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="transcription_busy",
                headers={"Retry-After": "1"},
            )

        async with inference_lock:
            audio = await _read_upload(file, max_upload_bytes)
            try:
                normalized = await normalizer.normalize(audio)
            except AudioDurationExceeded:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="audio_duration_exceeded",
                ) from None
            except AudioNormalizationError:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="invalid_audio",
                ) from None
            try:
                output = await asyncio.to_thread(
                    engine.transcribe,
                    normalized.pcm,
                    language=language,
                )
            except EngineInferenceError:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="transcription_engine_unavailable",
                ) from None

        LOGGER.info(
            "transcription completed duration_seconds=%.3f detected_language=%s",
            normalized.duration_seconds,
            output.language or "unknown",
        )
        return TranscriptionResponse(text=output.text)

    return app


async def _read_upload(upload: UploadFile, max_upload_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    try:
        while chunk := await upload.read(_READ_CHUNK_BYTES):
            size += len(chunk)
            if size > max_upload_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="audio_upload_too_large",
                )
            chunks.append(chunk)
    finally:
        await upload.close()
    return b"".join(chunks)


def _require_bearer_token(authorization: str | None, expected: str) -> None:
    scheme, separator, token = (authorization or "").partition(" ")
    valid = (
        separator == " "
        and scheme.casefold() == "bearer"
        and bool(token)
        and secrets.compare_digest(token, expected)
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_api_key",
            headers={"WWW-Authenticate": "Bearer"},
        )


__all__ = ["create_app"]
