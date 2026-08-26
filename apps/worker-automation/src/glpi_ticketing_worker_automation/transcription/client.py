"""HTTP client for the batch-transcription API."""

import math
from collections.abc import Mapping
from pathlib import Path

import httpx
from pydantic import SecretStr

from .contracts import (
    TranscriptionHttpError,
    TranscriptionProtocolError,
    TranscriptionSubmissionRejectedError,
    TranscriptionTransportError,
)
from .models import (
    JobSnapshot,
    JobStatus,
    JobUuid,
    ResponseLogEntry,
    Submission,
    TranscriptionOperation,
    TranscriptionProvider,
)

CREATE_JOB_PATH = "/api/batchTranscription/createJob"
GET_RESULT_PATH = "/api/batchTranscription/result"
MAX_JOB_UUID_LENGTH = 256
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 502, 503, 504})
AUDIO_MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
}


class TranscriptionClient:
    """Translate the transcription HTTP API into the stable domain contract."""

    def __init__(
        self,
        *,
        base_url: str | httpx.URL,
        api_key: SecretStr,
        http_client: httpx.Client,
        timeout: float | httpx.Timeout,
    ) -> None:
        self._base_url = _validate_base_url(base_url)
        self._api_key = _validate_api_key(api_key)
        self._http_client = http_client
        self._timeout = _validate_timeout(timeout)

    @property
    def provider(self) -> TranscriptionProvider:
        """Return the transcription provider identifier."""
        return TranscriptionProvider.LEGACY

    def submit(self, audio_path: Path) -> Submission:
        """Submit one audio file to the batch endpoint."""
        audio_path = _validate_audio_path(audio_path)
        media_type = _require_audio_media_type(audio_path)
        operation = TranscriptionOperation.SUBMIT

        try:
            with audio_path.open("rb") as audio_stream:
                response = self._http_client.post(
                    self._url(CREATE_JOB_PATH),
                    headers=self._headers(),
                    files={
                        "audioFile": (
                            audio_path.name,
                            audio_stream,
                            media_type,
                        )
                    },
                    timeout=self._timeout,
                    follow_redirects=False,
                )
        except httpx.RequestError:
            raise TranscriptionTransportError(self.provider, operation) from None

        rejected_log = _non_success_error_log(response, operation)
        if rejected_log is not None:
            raise TranscriptionSubmissionRejectedError(
                self.provider,
                response_log=rejected_log,
            )
        _require_successful_http_status(response, self.provider, operation)
        payload = _decode_object(response, self.provider, operation)
        provider_status = _require_string_field(
            payload,
            "status",
            self.provider,
            operation,
        )
        normalized_provider_status = provider_status.strip().casefold()

        if normalized_provider_status == "error":
            response_log = ResponseLogEntry(
                operation=operation,
                http_status=response.status_code,
                provider_status="error",
                job_status=None,
            )
            raise TranscriptionSubmissionRejectedError(
                self.provider,
                response_log=response_log,
            )

        if normalized_provider_status != "success":
            raise TranscriptionProtocolError(
                self.provider,
                operation,
                "unsupported top-level status",
            )

        data = _require_object_field(payload, "data", self.provider, operation)
        job_uuid = JobUuid(
            _validate_job_uuid(
                _require_string_field(data, "job_uuid", self.provider, operation),
                self.provider,
                operation,
            )
        )
        response_log = ResponseLogEntry(
            operation=operation,
            http_status=response.status_code,
            provider_status="success",
            job_status=None,
        )
        return Submission(job_uuid=job_uuid, response_log=response_log)

    def get_job(self, job_uuid: JobUuid) -> JobSnapshot:
        """Fetch and normalize one transcription job response."""
        operation = TranscriptionOperation.GET_JOB
        job_uuid = JobUuid(_validate_job_uuid(job_uuid, self.provider, operation))

        try:
            response = self._http_client.get(
                self._url(GET_RESULT_PATH),
                headers=self._headers(),
                params={"job_uuid": job_uuid},
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.RequestError:
            raise TranscriptionTransportError(self.provider, operation) from None

        rejected_log = _non_success_error_log(response, operation)
        if rejected_log is not None:
            return JobSnapshot(
                job_uuid=job_uuid,
                status=JobStatus.FAILED,
                transcription=None,
                response_log=rejected_log,
            )
        _require_successful_http_status(response, self.provider, operation)
        payload = _decode_object(response, self.provider, operation)
        provider_status = _require_string_field(
            payload,
            "status",
            self.provider,
            operation,
        )
        normalized_provider_status = provider_status.strip().casefold()
        persisted_provider_status = (
            normalized_provider_status
            if normalized_provider_status in {"success", "error"}
            else "other"
        )

        if normalized_provider_status != "success":
            response_log = ResponseLogEntry(
                operation=operation,
                http_status=response.status_code,
                provider_status=persisted_provider_status,
                job_status=None,
            )
            return JobSnapshot(
                job_uuid=job_uuid,
                status=JobStatus.FAILED,
                transcription=None,
                response_log=response_log,
            )

        data = _require_object_field(payload, "data", self.provider, operation)
        # The historical API calls this UUID ``job_id`` in result payloads.
        response_job_uuid = data.get("job_id")
        if response_job_uuid is not None:
            validated_response_job_uuid = _validate_job_uuid(
                response_job_uuid,
                self.provider,
                operation,
            )
            if validated_response_job_uuid != job_uuid:
                raise TranscriptionProtocolError(
                    self.provider,
                    operation,
                    "response job identifier does not match the requested job",
                )

        raw_job_status = _require_string_field(
            data,
            "status",
            self.provider,
            operation,
        )
        normalized_job_status = raw_job_status.strip().upper()
        status = {
            "PENDING": JobStatus.PENDING,
            "PROCESSING": JobStatus.PROCESSING,
            "COMPLETED": JobStatus.COMPLETED,
        }.get(normalized_job_status, JobStatus.FAILED)
        persisted_job_status = (
            normalized_job_status
            if normalized_job_status in {"PENDING", "PROCESSING", "COMPLETED", "FAILED"}
            else "OTHER"
        )

        transcription: str | None = None
        if status is JobStatus.COMPLETED:
            result = _require_object_field(
                data,
                "result",
                self.provider,
                operation,
            )
            transcription = _require_string_field(
                result,
                "full_text",
                self.provider,
                operation,
            )

        response_log = ResponseLogEntry(
            operation=operation,
            http_status=response.status_code,
            provider_status=persisted_provider_status,
            job_status=persisted_job_status,
        )
        return JobSnapshot(
            job_uuid=job_uuid,
            status=status,
            transcription=transcription,
            response_log=response_log,
        )

    def _headers(self) -> dict[str, str]:
        return {"X-API-KEY": self._api_key.get_secret_value()}

    def _url(self, path: str) -> httpx.URL:
        return httpx.URL(f"{str(self._base_url).rstrip('/')}{path}")


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
    secret_value = api_key.get_secret_value()
    if not secret_value or secret_value != secret_value.strip():
        raise ValueError("api_key must be non-empty without surrounding whitespace")
    if any(not character.isprintable() for character in secret_value):
        raise ValueError("api_key must not contain control characters")
    return api_key


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


def _validate_audio_path(audio_path: Path) -> Path:
    audio_path = Path(audio_path)
    if audio_path.is_symlink():
        raise ValueError("audio_path must not be a symbolic link")
    if not audio_path.exists():
        raise FileNotFoundError(audio_path)
    if not audio_path.is_file():
        raise ValueError("audio_path must be a regular file")
    return audio_path


def _require_audio_media_type(audio_path: Path) -> str:
    extension = audio_path.suffix.casefold()
    try:
        return AUDIO_MEDIA_TYPES[extension]
    except KeyError:
        supported_extensions = ", ".join(sorted(AUDIO_MEDIA_TYPES))
        raise ValueError(
            f"unsupported audio file extension; expected one of: {supported_extensions}"
        ) from None


def _validate_job_uuid(
    value: object,
    provider: TranscriptionProvider,
    operation: TranscriptionOperation,
) -> str:
    if not isinstance(value, str):
        raise TranscriptionProtocolError(
            provider,
            operation,
            "job identifier must be a string",
        )
    if not value or value != value.strip():
        raise TranscriptionProtocolError(
            provider,
            operation,
            "job identifier must be a non-empty string without surrounding whitespace",
        )
    if len(value) > MAX_JOB_UUID_LENGTH or any(
        not character.isprintable() for character in value
    ):
        raise TranscriptionProtocolError(
            provider,
            operation,
            "job identifier contains unsupported characters or is too long",
        )
    return value


def _require_successful_http_status(
    response: httpx.Response,
    provider: TranscriptionProvider,
    operation: TranscriptionOperation,
) -> None:
    if response.status_code == 200:
        return
    status_code = response.status_code
    retryable = status_code in RETRYABLE_HTTP_STATUSES
    raise TranscriptionHttpError(
        provider,
        operation,
        status_code,
        retryable=retryable,
    )


def _non_success_error_log(
    response: httpx.Response,
    operation: TranscriptionOperation,
) -> ResponseLogEntry | None:
    if (
        not 400 <= response.status_code < 500
        or response.status_code in RETRYABLE_HTTP_STATUSES
    ):
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, Mapping):
        return None
    provider_status = payload.get("status")
    if not isinstance(provider_status, str):
        return None
    if provider_status.strip().casefold() != "error":
        return None
    return ResponseLogEntry(
        operation=operation,
        http_status=response.status_code,
        provider_status="error",
        job_status=None,
    )


def _decode_object(
    response: httpx.Response,
    provider: TranscriptionProvider,
    operation: TranscriptionOperation,
) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError:
        raise TranscriptionProtocolError(
            provider,
            operation,
            "response body is not valid JSON",
        ) from None
    if not isinstance(payload, Mapping):
        raise TranscriptionProtocolError(
            provider,
            operation,
            "response body must be a JSON object",
        )
    return payload


def _require_object_field(
    payload: Mapping[str, object],
    field: str,
    provider: TranscriptionProvider,
    operation: TranscriptionOperation,
) -> Mapping[str, object]:
    value = payload.get(field)
    if not isinstance(value, Mapping):
        raise TranscriptionProtocolError(
            provider,
            operation,
            f"{field} must be a JSON object",
        )
    return value


def _require_string_field(
    payload: Mapping[str, object],
    field: str,
    provider: TranscriptionProvider,
    operation: TranscriptionOperation,
) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise TranscriptionProtocolError(
            provider,
            operation,
            f"{field} must be a string",
        )
    return value
