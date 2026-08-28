"""HTTP translation for audio job use cases."""

from __future__ import annotations

from typing import BinaryIO, cast
from uuid import UUID

from fastapi import HTTPException, Request, Response, UploadFile, status

from glpi_ticketing_web.controllers.responses import ManagedStreamingResponse
from glpi_ticketing_web.schemas import (
    AudioFileExtension,
    JobAcceptedResponse,
    JobDetailResponse,
    JobErrorScope,
    JobEventActor,
    JobEventListResponse,
    JobEventName,
    JobEventResponse,
    JobListResponse,
    JobRetryAcceptedResponse,
    JobStage,
    JobState,
    JobSummaryResponse,
    RetentionState,
)
from glpi_ticketing_web.services import (
    AudioPurgedError,
    AudioStorageConflictError,
    AudioStorageInvariantError,
    AudioStorageUnavailableError,
    EmptyAudioError,
    GetAudioJob,
    GetDownloadableAudio,
    InvalidCursorError,
    JobEventView,
    JobPersistenceUnavailableError,
    JobQueryUnavailableError,
    JobRetryConflictError,
    JobView,
    ListAudioJobEvents,
    ListAudioJobs,
    RetryAudioJob,
    SubmitAudioJob,
    SubmittedJob,
    UnsupportedAudioUploadError,
    UploadTooLargeError,
)

_AUDIO_MEDIA_TYPES = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "m4a": "audio/mp4",
}


class JobController:
    """Translate HTTP requests and service outcomes for audio jobs."""

    def __init__(
        self,
        *,
        submit_audio_job: SubmitAudioJob,
        list_audio_jobs: ListAudioJobs,
        get_audio_job: GetAudioJob,
        list_audio_job_events: ListAudioJobEvents,
        retry_audio_job: RetryAudioJob,
        get_downloadable_audio: GetDownloadableAudio,
    ) -> None:
        self._submit_audio_job = submit_audio_job
        self._list_audio_jobs = list_audio_jobs
        self._get_audio_job = get_audio_job
        self._list_audio_job_events = list_audio_job_events
        self._retry_audio_job = retry_audio_job
        self._get_downloadable_audio = get_downloadable_audio

    def create_job(
        self,
        request: Request,
        response: Response,
        audio_file: UploadFile,
    ) -> JobAcceptedResponse:
        """Accept one API upload and return its durable job location."""
        submitted_job = self._submit_upload(audio_file)
        location = request.app.url_path_for(
            "get_job",
            audio_uuid=str(submitted_job.audio_uuid),
        )
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Location"] = str(location)
        return JobAcceptedResponse(
            audio_uuid=submitted_job.audio_uuid,
            state=JobState(submitted_job.state.value),
        )

    def list_jobs(
        self,
        *,
        state: JobState | None,
        audio_uuid: UUID | None,
        limit: int,
        cursor: str | None,
    ) -> JobListResponse:
        """Return one validated keyset page for the dashboard."""
        try:
            page = self._list_audio_jobs.execute(
                limit=limit,
                state=state.value if state is not None else None,
                audio_uuid=audio_uuid,
                cursor=cursor,
            )
        except InvalidCursorError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Invalid pagination cursor",
            ) from error
        except JobQueryUnavailableError as error:
            raise _query_unavailable() from error
        return JobListResponse(
            items=tuple(_summary_response(item) for item in page.items),
            next_cursor=page.next_cursor,
        )

    def get_job(self, audio_uuid: UUID) -> JobDetailResponse:
        """Return the persisted state and retention summary of one job."""
        try:
            job = self._get_audio_job.execute(audio_uuid)
        except JobQueryUnavailableError as error:
            raise _query_unavailable() from error
        if job is None:
            raise _job_not_found()
        return _detail_response(job)

    def list_job_events(
        self,
        audio_uuid: UUID,
        *,
        limit: int,
        cursor: str | None,
    ) -> JobEventListResponse:
        """Return one sanitized page from a known job's history."""
        try:
            page = self._list_audio_job_events.execute(
                audio_uuid,
                limit=limit,
                cursor=cursor,
            )
        except InvalidCursorError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Invalid pagination cursor",
            ) from error
        except JobQueryUnavailableError as error:
            raise _query_unavailable() from error
        if page is None:
            raise _job_not_found()
        return JobEventListResponse(
            items=tuple(_event_response(item) for item in page.items),
            next_cursor=page.next_cursor,
        )

    def retry_job(
        self,
        request: Request,
        response: Response,
        audio_uuid: UUID,
    ) -> JobRetryAcceptedResponse:
        """Atomically rearm one failed or blocked job."""
        try:
            retried = self._retry_audio_job.execute(audio_uuid)
        except JobRetryConflictError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Job cannot be retried",
            ) from error
        except (
            AudioStorageUnavailableError,
            JobQueryUnavailableError,
            JobPersistenceUnavailableError,
        ) as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Job retry is temporarily unavailable",
            ) from error
        if retried is None:
            raise _job_not_found()
        location = request.app.url_path_for(
            "get_job",
            audio_uuid=str(retried.audio_uuid),
        )
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Location"] = str(location)
        return JobRetryAcceptedResponse(
            audio_uuid=retried.audio_uuid,
            state=JobState(retried.state.value),
        )

    def download_audio(self, audio_uuid: UUID) -> ManagedStreamingResponse:
        """Stream one completed audio selected by its persisted job state."""
        try:
            downloadable = self._get_downloadable_audio.execute(audio_uuid)
        except JobQueryUnavailableError as error:
            raise _query_unavailable() from error
        except AudioPurgedError as error:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="Audio file has been purged",
            ) from error
        except AudioStorageInvariantError as error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Audio storage invariant violated",
            ) from error
        except AudioStorageUnavailableError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Audio storage is temporarily unavailable",
            ) from error
        if downloadable is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Audio file not found",
            )
        try:
            return ManagedStreamingResponse(
                downloadable.iter_bytes(),
                close_callback=downloadable.close,
                media_type=_AUDIO_MEDIA_TYPES[downloadable.extension.value],
                headers={
                    "Cache-Control": "private, no-store",
                    "Content-Disposition": (
                        "attachment; filename="
                        f'"{downloadable.uuid}.{downloadable.extension.value}"'
                    ),
                    "Content-Length": str(downloadable.size_bytes),
                    "X-Audio-UUID": str(downloadable.uuid),
                },
            )
        except BaseException:
            downloadable.close()
            raise

    def _submit_upload(self, audio_file: UploadFile) -> SubmittedJob:
        try:
            self._submit_audio_job.prevalidate(audio_file.filename)
            return self._submit_audio_job.execute(
                audio_file.filename,
                cast(BinaryIO, audio_file.file),
            )
        except UnsupportedAudioUploadError as error:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="Unsupported audio file extension",
            ) from error
        except UploadTooLargeError as error:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Audio file exceeds the upload size limit",
            ) from error
        except EmptyAudioError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Audio file must not be empty",
            ) from error
        except AudioStorageConflictError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Audio identifier conflict",
            ) from error
        except (AudioStorageUnavailableError, JobPersistenceUnavailableError) as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Audio job submission is temporarily unavailable",
            ) from error
        finally:
            audio_file.file.close()


def _summary_response(job: JobView) -> JobSummaryResponse:
    return JobSummaryResponse(
        audio_uuid=job.audio_uuid,
        state=JobState(job.state.value),
        stage=JobStage(job.stage.value) if job.stage is not None else None,
        updated_at=job.updated_at,
        attempt_count=job.attempt_count,
        consecutive_retry_count=job.consecutive_retry_count,
        glpi_ticket_id=job.glpi_ticket_id,
    )


def _detail_response(job: JobView) -> JobDetailResponse:
    return JobDetailResponse(
        audio_uuid=job.audio_uuid,
        state=JobState(job.state.value),
        stage=JobStage(job.stage.value) if job.stage is not None else None,
        audio_extension=AudioFileExtension(job.audio_extension.value),
        audio_size_bytes=job.audio_size_bytes,
        glpi_ticket_id=job.glpi_ticket_id,
        attempt_count=job.attempt_count,
        consecutive_retry_count=job.consecutive_retry_count,
        next_attempt_at=job.next_attempt_at,
        last_error_scope=(
            JobErrorScope(job.last_error_scope.value)
            if job.last_error_scope is not None
            else None
        ),
        last_error_category=job.last_error_category,
        last_error_code=job.last_error_code,
        created_at=job.created_at,
        first_started_at=job.first_started_at,
        updated_at=job.updated_at,
        finished_at=job.finished_at,
        retention_status=(
            RetentionState(job.retention_status.value)
            if job.retention_status is not None
            else None
        ),
        audio_deleted_at=job.audio_deleted_at,
    )


def _event_response(event: JobEventView) -> JobEventResponse:
    return JobEventResponse(
        event_id=event.event_id,
        event_type=JobEventName(event.event_type.value),
        occurred_at=event.occurred_at,
        state=JobState(event.state.value),
        stage=JobStage(event.stage.value) if event.stage is not None else None,
        actor=JobEventActor(event.actor.value),
        error_scope=(
            JobErrorScope(event.error_scope.value)
            if event.error_scope is not None
            else None
        ),
        error_category=event.error_category,
        technical_code=event.technical_code,
    )


def _job_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")


def _query_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Job state is temporarily unavailable",
    )


__all__ = ["JobController"]
