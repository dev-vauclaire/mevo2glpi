"""HTTP translation for audio job use cases."""

from __future__ import annotations

from typing import BinaryIO, cast
from uuid import UUID

from fastapi import HTTPException, Request, Response, UploadFile, status
from fastapi.templating import Jinja2Templates

from glpi_ticketing_web.controllers.responses import ManagedStreamingResponse
from glpi_ticketing_web.schemas import (
    JobAcceptedResponse,
    JobDetailResponse,
    JobStage,
    JobState,
)
from glpi_ticketing_web.services import (
    AudioStorageConflictError,
    AudioStorageInvariantError,
    AudioStorageUnavailableError,
    EmptyAudioError,
    GetAudioJob,
    GetDownloadableAudio,
    JobPersistenceUnavailableError,
    JobQueryUnavailableError,
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
        get_audio_job: GetAudioJob,
        get_downloadable_audio: GetDownloadableAudio,
        templates: Jinja2Templates,
    ) -> None:
        self._submit_audio_job = submit_audio_job
        self._get_audio_job = get_audio_job
        self._get_downloadable_audio = get_downloadable_audio
        self._templates = templates

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
        body = JobAcceptedResponse(
            audio_uuid=submitted_job.audio_uuid,
            state=JobState(submitted_job.state.value),
        )
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Location"] = str(location)
        return body

    def upload_audio(self, request: Request, audio_file: UploadFile) -> Response:
        """Accept one browser upload and render its acknowledgement page."""
        submitted_job = self._submit_upload(audio_file)
        audio_uuid = str(submitted_job.audio_uuid)
        location = request.app.url_path_for("get_job", audio_uuid=audio_uuid)
        return self._templates.TemplateResponse(
            request=request,
            name="uploaded.html",
            context={
                "audio_uuid": audio_uuid,
                "upload_again_path": request.app.url_path_for("index"),
            },
            status_code=status.HTTP_202_ACCEPTED,
            headers={"Location": str(location)},
        )

    def get_job(self, audio_uuid: UUID) -> JobDetailResponse:
        """Return the persisted state of one job."""
        try:
            snapshot = self._get_audio_job.execute(audio_uuid)
        except JobQueryUnavailableError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Job state is temporarily unavailable",
            ) from error

        if snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job not found",
            )

        return JobDetailResponse(
            audio_uuid=snapshot.uuid,
            state=JobState(snapshot.status.value),
            stage=JobStage(snapshot.stage.value)
            if snapshot.stage is not None
            else None,
        )

    def download_audio(self, audio_uuid: UUID) -> ManagedStreamingResponse:
        """Stream one completed audio selected by its persisted job state."""
        try:
            downloadable = self._get_downloadable_audio.execute(audio_uuid)
        except JobQueryUnavailableError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Job state is temporarily unavailable",
            ) from error
        except AudioStorageInvariantError as error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Audio storage invariant violated",
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


__all__ = ["JobController"]
