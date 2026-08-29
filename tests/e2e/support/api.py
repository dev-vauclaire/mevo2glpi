"""Typed HTTPS helpers for the MVP end-to-end API contract."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

import httpx


class _MvpEnvironment(Protocol):
    def client(self) -> httpx.Client:
        """Return one authenticated HTTPS client."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioUpload:
    """One synthetic audio submitted through the public HTTPS boundary."""

    filename: str
    content: bytes = field(repr=False)
    media_type: str = "audio/wav"

    def __post_init__(self) -> None:
        if not isinstance(self.filename, str) or not self.filename:
            raise ValueError("filename must be a non-empty string")
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("content must be non-empty bytes")
        if not isinstance(self.media_type, str) or not self.media_type:
            raise ValueError("media_type must be a non-empty string")


@dataclass(frozen=True, slots=True)
class AcceptedJob:
    """Stable acknowledgement returned by upload and operator retry."""

    audio_uuid: UUID
    state: str
    location: str


@dataclass(frozen=True, slots=True, kw_only=True)
class JobDetail:
    """Sanitized fields used by lifecycle E2E assertions."""

    audio_uuid: UUID
    state: str
    stage: str | None
    audio_extension: str
    audio_size_bytes: int
    glpi_ticket_id: int | None
    attempt_count: int
    consecutive_retry_count: int
    last_error_scope: str | None
    last_error_category: str | None
    last_error_code: str | None
    next_attempt_at: str | None
    finished_at: str | None
    retention_status: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class JobSummary:
    """Bounded representation returned by the dashboard jobs endpoint."""

    audio_uuid: UUID
    state: str
    stage: str | None
    glpi_ticket_id: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class JobEvent:
    """One chronological, sanitized lifecycle event."""

    event_id: int
    event_type: str
    state: str
    stage: str | None
    actor: str
    error_scope: str | None
    error_category: str | None
    technical_code: str | None


@dataclass(frozen=True, slots=True)
class AudioDownload:
    """Exact completed audio returned through Nginx and FastAPI."""

    audio_uuid: UUID
    content: bytes = field(repr=False)
    content_length: int
    content_disposition: str
    media_type: str
    cache_control: str


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationalAlert:
    """One current dashboard condition without internal error contents."""

    key: str
    kind: str
    severity: str
    count: int


def upload_audio(client: httpx.Client, upload: AudioUpload) -> AcceptedJob:
    """Upload one complete audio and validate its durable acknowledgement."""
    if not isinstance(client, httpx.Client):
        raise TypeError("client must be an httpx.Client")
    if not isinstance(upload, AudioUpload):
        raise TypeError("upload must be an AudioUpload")

    response = client.post(
        "/api/jobs",
        files={
            "audio_file": (
                upload.filename,
                upload.content,
                upload.media_type,
            )
        },
    )
    _require_status(response, httpx.codes.ACCEPTED, "audio upload")
    return _accepted_job(response, expected_state="pending")


def upload_audios_concurrently(
    environment: _MvpEnvironment,
    uploads: tuple[AudioUpload, ...],
) -> tuple[AcceptedJob, ...]:
    """Upload at most three audios concurrently using independent clients."""
    if not uploads or len(uploads) > 3:
        raise ValueError("uploads must contain between one and three audios")
    if any(not isinstance(upload, AudioUpload) for upload in uploads):
        raise TypeError("uploads must contain only AudioUpload values")

    def submit(upload: AudioUpload) -> AcceptedJob:
        with environment.client() as client:
            return upload_audio(client, upload)

    with ThreadPoolExecutor(max_workers=len(uploads)) as executor:
        futures = tuple(executor.submit(submit, upload) for upload in uploads)
        return tuple(future.result() for future in futures)


def get_job(client: httpx.Client, audio_uuid: UUID) -> JobDetail:
    """Read one persisted job through the public API."""
    _require_uuid(audio_uuid)
    response = client.get(f"/api/jobs/{audio_uuid}")
    _require_status(response, httpx.codes.OK, "job detail")
    payload = _json_object(response, "job detail")
    return JobDetail(
        audio_uuid=_uuid_field(payload, "audio_uuid"),
        state=_string_field(payload, "state"),
        stage=_optional_string_field(payload, "stage"),
        audio_extension=_string_field(payload, "audio_extension"),
        audio_size_bytes=_non_negative_integer_field(payload, "audio_size_bytes"),
        glpi_ticket_id=_optional_positive_integer_field(payload, "glpi_ticket_id"),
        attempt_count=_non_negative_integer_field(payload, "attempt_count"),
        consecutive_retry_count=_non_negative_integer_field(
            payload,
            "consecutive_retry_count",
        ),
        last_error_scope=_optional_string_field(payload, "last_error_scope"),
        last_error_category=_optional_string_field(payload, "last_error_category"),
        last_error_code=_optional_string_field(payload, "last_error_code"),
        next_attempt_at=_optional_string_field(payload, "next_attempt_at"),
        finished_at=_optional_string_field(payload, "finished_at"),
        retention_status=_optional_string_field(payload, "retention_status"),
    )


def list_jobs(client: httpx.Client, *, limit: int = 100) -> tuple[JobSummary, ...]:
    """Read one bounded dashboard page, sufficient for isolated E2E jobs."""
    response = client.get("/api/jobs", params={"limit": str(limit)})
    _require_status(response, httpx.codes.OK, "job list")
    payload = _json_object(response, "job list")
    items = payload.get("items")
    if not isinstance(items, list):
        raise AssertionError("job list returned an invalid items collection")
    summaries: list[JobSummary] = []
    for item in items:
        if not isinstance(item, dict):
            raise AssertionError("job list returned an invalid item")
        summaries.append(
            JobSummary(
                audio_uuid=_uuid_field(item, "audio_uuid"),
                state=_string_field(item, "state"),
                stage=_optional_string_field(item, "stage"),
                glpi_ticket_id=_optional_positive_integer_field(
                    item,
                    "glpi_ticket_id",
                ),
            )
        )
    return tuple(summaries)


def get_job_events(
    client: httpx.Client,
    audio_uuid: UUID,
) -> tuple[JobEvent, ...]:
    """Read every bounded event page and return events chronologically."""
    _require_uuid(audio_uuid)
    events: list[JobEvent] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _ in range(100):
        params = {"limit": "100"}
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get(f"/api/jobs/{audio_uuid}/events", params=params)
        _require_status(response, httpx.codes.OK, "job events")
        payload = _json_object(response, "job events")
        items = payload.get("items")
        if not isinstance(items, list):
            raise AssertionError("job events returned an invalid items collection")
        for item in items:
            if not isinstance(item, dict):
                raise AssertionError("job events returned an invalid item")
            events.append(
                JobEvent(
                    event_id=_positive_integer_field(item, "event_id"),
                    event_type=_string_field(item, "event_type"),
                    state=_string_field(item, "state"),
                    stage=_optional_string_field(item, "stage"),
                    actor=_string_field(item, "actor"),
                    error_scope=_optional_string_field(item, "error_scope"),
                    error_category=_optional_string_field(item, "error_category"),
                    technical_code=_optional_string_field(item, "technical_code"),
                )
            )
        next_cursor = payload.get("next_cursor")
        if next_cursor is None:
            break
        if not isinstance(next_cursor, str) or not next_cursor:
            raise AssertionError("job events returned an invalid cursor")
        if next_cursor in seen_cursors:
            raise AssertionError("job events repeated a pagination cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise AssertionError("job events exceeded the bounded pagination limit")

    event_ids = [event.event_id for event in events]
    if len(event_ids) != len(set(event_ids)):
        raise AssertionError("job events returned duplicate identifiers")
    return tuple(sorted(events, key=lambda event: event.event_id))


def retry_job(client: httpx.Client, audio_uuid: UUID) -> AcceptedJob:
    """Request an operator retry through the public application transition."""
    _require_uuid(audio_uuid)
    response = client.post(f"/api/jobs/{audio_uuid}/retry")
    _require_status(response, httpx.codes.ACCEPTED, "operator retry")
    accepted = _accepted_job(response, expected_state="pending")
    if accepted.audio_uuid != audio_uuid:
        raise AssertionError("operator retry returned a mismatched UUID")
    return accepted


def download_audio(client: httpx.Client, audio_uuid: UUID) -> AudioDownload:
    """Download a completed audio and validate its bounded response headers."""
    _require_uuid(audio_uuid)
    response = client.get(f"/audios/{audio_uuid}")
    _require_status(response, httpx.codes.OK, "audio download")
    returned_uuid = _parse_uuid(response.headers.get("x-audio-uuid"), "audio header")
    try:
        content_length = int(response.headers["content-length"])
    except (KeyError, TypeError, ValueError):
        raise AssertionError(
            "audio download returned an invalid content length"
        ) from None
    disposition = response.headers.get("content-disposition")
    if not isinstance(disposition, str) or not disposition:
        raise AssertionError("audio download returned no content disposition")
    if content_length != len(response.content):
        raise AssertionError("audio download content length is inconsistent")
    media_type = response.headers.get("content-type")
    if not isinstance(media_type, str) or not media_type:
        raise AssertionError("audio download returned no media type")
    cache_control = response.headers.get("cache-control")
    if not isinstance(cache_control, str) or not cache_control:
        raise AssertionError("audio download returned no cache policy")
    return AudioDownload(
        audio_uuid=returned_uuid,
        content=response.content,
        content_length=content_length,
        content_disposition=disposition,
        media_type=media_type,
        cache_control=cache_control,
    )


def get_observability_alerts(client: httpx.Client) -> tuple[OperationalAlert, ...]:
    """Read current sanitized dashboard alerts."""
    response = client.get("/api/observability")
    _require_status(response, httpx.codes.OK, "observability")
    payload = _json_object(response, "observability")
    items = payload.get("alerts")
    if not isinstance(items, list):
        raise AssertionError("observability returned an invalid alerts collection")
    alerts: list[OperationalAlert] = []
    for item in items:
        if not isinstance(item, dict):
            raise AssertionError("observability returned an invalid alert")
        alerts.append(
            OperationalAlert(
                key=_string_field(item, "key"),
                kind=_string_field(item, "kind"),
                severity=_string_field(item, "severity"),
                count=_positive_integer_field(item, "count"),
            )
        )
    return tuple(alerts)


def _accepted_job(response: httpx.Response, *, expected_state: str) -> AcceptedJob:
    payload = _json_object(response, "accepted job")
    audio_uuid = _uuid_field(payload, "audio_uuid")
    state = _string_field(payload, "state")
    if state != expected_state:
        raise AssertionError("accepted job returned an unexpected state")
    location = response.headers.get("location")
    expected_location = f"/api/jobs/{audio_uuid}"
    if location != expected_location:
        raise AssertionError("accepted job returned an invalid Location header")
    return AcceptedJob(audio_uuid=audio_uuid, state=state, location=location)


def _require_status(
    response: httpx.Response,
    expected_status: int,
    operation: str,
) -> None:
    if response.status_code != expected_status:
        raise AssertionError(
            f"{operation} returned unexpected HTTP status {response.status_code}"
        )


def _json_object(response: httpx.Response, operation: str) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError:
        raise AssertionError(f"{operation} returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise AssertionError(f"{operation} returned a non-object JSON payload")
    return payload


def _uuid_field(payload: dict[str, object], field_name: str) -> UUID:
    return _parse_uuid(payload.get(field_name), field_name)


def _parse_uuid(value: object, field_name: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise AssertionError(f"{field_name} is not a UUID") from None


def _string_field(payload: dict[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise AssertionError(f"{field_name} is not a non-empty string")
    return value


def _optional_string_field(
    payload: dict[str, object],
    field_name: str,
) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise AssertionError(f"{field_name} is not a nullable non-empty string")
    return value


def _non_negative_integer_field(payload: dict[str, object], field_name: str) -> int:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AssertionError(f"{field_name} is not a non-negative integer")
    return value


def _positive_integer_field(payload: dict[str, object], field_name: str) -> int:
    value = _non_negative_integer_field(payload, field_name)
    if value == 0:
        raise AssertionError(f"{field_name} is not a positive integer")
    return value


def _optional_positive_integer_field(
    payload: dict[str, object],
    field_name: str,
) -> int | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AssertionError(f"{field_name} is not a nullable positive integer")
    return value


def _require_uuid(value: object) -> None:
    if not isinstance(value, UUID):
        raise TypeError("audio_uuid must be a UUID")


__all__ = [
    "AcceptedJob",
    "AudioDownload",
    "AudioUpload",
    "JobDetail",
    "JobEvent",
    "JobSummary",
    "OperationalAlert",
    "download_audio",
    "get_job",
    "get_job_events",
    "get_observability_alerts",
    "list_jobs",
    "retry_job",
    "upload_audio",
    "upload_audios_concurrently",
]
