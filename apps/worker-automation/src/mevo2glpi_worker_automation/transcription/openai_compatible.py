"""Synchronous adapter for the strict OpenAI transcription subset."""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import SecretStr

from mevo2glpi_common import audio_files

from .contracts import (
    TranscriptionBlockedError,
    TranscriptionRetryRequired,
    TranscriptionTerminalError,
)
from .models import (
    TranscriptionErrorCategory,
    TranscriptionResult,
)

TRANSCRIPTIONS_PATH = "/audio/transcriptions"
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429})
TERMINAL_HTTP_STATUSES = frozenset({400, 413, 415, 422, 500})
AUDIO_MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
}


class OpenAICompatibleTranscriber:
    """Transcribe one audio through a synchronous OpenAI-compatible API.

    Only the common ``/audio/transcriptions`` JSON contract is consumed. The
    adapter deliberately ignores provider-specific response extensions so that
    changing the backing server does not affect the worker domain contract.
    """

    def __init__(
        self,
        *,
        base_url: str | httpx.URL,
        api_key: SecretStr,
        model: str,
        language: str,
        http_client: httpx.Client,
        timeout: float | httpx.Timeout,
    ) -> None:
        self._base_url = _validate_base_url(base_url)
        self._api_key = _validate_api_key(api_key)
        self._model = _validate_text(model, "model", maximum_length=255)
        self._language = _validate_text(language, "language", maximum_length=32)
        if not isinstance(http_client, httpx.Client):
            raise TypeError("http_client must be an httpx.Client")
        self._http_client = http_client
        self._timeout = _validate_timeout(timeout)

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        """Return normalized text or a sanitized retry, terminal or block error."""
        audio_path, audio_uuid = _validate_audio_path(audio_path)
        media_type = AUDIO_MEDIA_TYPES[audio_path.suffix.casefold()]

        try:
            with audio_path.open("rb") as audio_stream:
                response = self._http_client.post(
                    self._url(),
                    headers={
                        "Authorization": (f"Bearer {self._api_key.get_secret_value()}")
                    },
                    data={
                        "model": self._model,
                        "language": self._language,
                        "response_format": "json",
                    },
                    files={
                        "file": (
                            audio_path.name,
                            audio_stream,
                            media_type,
                        )
                    },
                    timeout=self._timeout,
                    follow_redirects=False,
                )
        except httpx.RequestError:
            raise TranscriptionRetryRequired(
                "transcription_unavailable",
                audio_path,
            ) from None

        self._require_success(response, audio_path)
        payload = _decode_object(response, audio_path)
        text = payload.get("text")
        if not isinstance(text, str):
            raise TranscriptionBlockedError(
                category=TranscriptionErrorCategory.PROTOCOL,
                code="invalid_transcription_response",
                audio_path=audio_path,
                automatic_probe=False,
            )
        if not _valid_transcription(text):
            raise TranscriptionTerminalError("invalid_transcription", audio_path)

        return TranscriptionResult(
            audio_path=audio_path,
            audio_uuid=audio_uuid,
            transcription=text,
        )

    def _url(self) -> httpx.URL:
        return httpx.URL(f"{str(self._base_url).rstrip('/')}{TRANSCRIPTIONS_PATH}")

    @staticmethod
    def _require_success(response: httpx.Response, audio_path: Path) -> None:
        status_code = response.status_code
        if status_code == 200:
            return
        if status_code in TERMINAL_HTTP_STATUSES:
            raise TranscriptionTerminalError(f"http_{status_code}", audio_path)
        if status_code in RETRYABLE_HTTP_STATUSES or 500 <= status_code < 600:
            raise TranscriptionRetryRequired(
                "transcription_unavailable",
                audio_path,
            )
        if status_code in {401, 403}:
            category = (
                TranscriptionErrorCategory.AUTHENTICATION
                if status_code == 401
                else TranscriptionErrorCategory.AUTHORIZATION
            )
            raise TranscriptionBlockedError(
                category=category,
                code=f"http_{status_code}",
                audio_path=audio_path,
                automatic_probe=True,
            )
        category = (
            TranscriptionErrorCategory.PROTOCOL
            if 200 <= status_code < 400
            else TranscriptionErrorCategory.INVALID_REQUEST
        )
        raise TranscriptionBlockedError(
            category=category,
            code=f"http_{status_code}",
            audio_path=audio_path,
            automatic_probe=False,
        )


def _validate_base_url(base_url: str | httpx.URL) -> httpx.URL:
    try:
        url = httpx.URL(base_url)
    except (TypeError, ValueError) as error:
        raise ValueError("base_url must be a valid HTTP URL") from error
    if url.scheme not in {"http", "https"} or url.host is None:
        raise ValueError("base_url must be an absolute HTTP URL")
    if url.username or url.password or url.query or url.fragment:
        raise ValueError(
            "base_url must not contain credentials, a query, or a fragment"
        )
    return url


def _validate_api_key(api_key: SecretStr) -> SecretStr:
    if not isinstance(api_key, SecretStr):
        raise TypeError("api_key must be a SecretStr")
    _validate_text(api_key.get_secret_value(), "api_key", maximum_length=4096)
    return api_key


def _validate_text(value: object, field_name: str, *, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if (
        not value
        or value != value.strip()
        or len(value) > maximum_length
        or any(not character.isprintable() for character in value)
    ):
        raise ValueError(
            f"{field_name} must be non-empty, trimmed, printable and no longer "
            f"than {maximum_length} characters"
        )
    return value


def _validate_timeout(timeout: float | httpx.Timeout) -> float | httpx.Timeout:
    if isinstance(timeout, bool):
        raise TypeError("timeout must be a positive number or httpx.Timeout")
    if isinstance(timeout, int | float):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and greater than zero")
        return float(timeout)
    if isinstance(timeout, httpx.Timeout):
        values = (timeout.connect, timeout.read, timeout.write, timeout.pool)
        if any(
            value is None or not math.isfinite(value) or value <= 0 for value in values
        ):
            raise ValueError("all timeout phases must be finite and greater than zero")
        return timeout
    raise TypeError("timeout must be a positive number or httpx.Timeout")


def _validate_audio_path(audio_path: Path) -> tuple[Path, UUID]:
    audio_path = Path(audio_path)
    parsed_name = audio_files.parse_audio_filename(audio_path.name)
    if audio_path.is_symlink():
        raise ValueError("audio_path must not be a symbolic link")
    if not audio_path.exists():
        raise FileNotFoundError(audio_path)
    if not audio_path.is_file():
        raise ValueError("audio_path must be a regular file")
    return audio_path, parsed_name.audio_uuid


def _decode_object(
    response: httpx.Response,
    audio_path: Path,
) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, Mapping):
        raise TranscriptionBlockedError(
            category=TranscriptionErrorCategory.PROTOCOL,
            code="invalid_transcription_response",
            audio_path=audio_path,
            automatic_probe=False,
        )
    return payload


def _valid_transcription(value: str) -> bool:
    return bool(value.strip()) and not any(
        (ord(character) < 32 and character not in "\r\n\t") or ord(character) == 127
        for character in value
    )


__all__ = ["OpenAICompatibleTranscriber"]
