"""Route declarations for audio jobs."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Query, Request, Response, UploadFile, status

from glpi_ticketing_web.controllers import JobController
from glpi_ticketing_web.schemas import (
    JobAcceptedResponse,
    JobDetailResponse,
    JobEventListResponse,
    JobListResponse,
    JobRetryAcceptedResponse,
    JobState,
)

PageLimit = Annotated[int, Query(ge=1, le=100)]
PageCursor = Annotated[str | None, Query(min_length=1, max_length=512)]


def create_jobs_router(controller: JobController) -> APIRouter:
    """Bind audio-job URLs to their controller."""
    router = APIRouter()

    @router.post(
        "/api/jobs",
        response_model=JobAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_job(
        request: Request,
        response: Response,
        audio_file: Annotated[UploadFile, File(description="Audio file to process")],
    ) -> JobAcceptedResponse:
        return controller.create_job(request, response, audio_file)

    @router.get("/api/jobs", response_model=JobListResponse)
    def list_jobs(
        state: JobState | None = None,
        audio_uuid: UUID | None = None,
        limit: PageLimit = 25,
        cursor: PageCursor = None,
    ) -> JobListResponse:
        return controller.list_jobs(
            state=state,
            audio_uuid=audio_uuid,
            limit=limit,
            cursor=cursor,
        )

    @router.get("/api/jobs/{audio_uuid}", response_model=JobDetailResponse)
    def get_job(audio_uuid: UUID) -> JobDetailResponse:
        return controller.get_job(audio_uuid)

    @router.get(
        "/api/jobs/{audio_uuid}/events",
        response_model=JobEventListResponse,
    )
    def list_job_events(
        audio_uuid: UUID,
        limit: PageLimit = 25,
        cursor: PageCursor = None,
    ) -> JobEventListResponse:
        return controller.list_job_events(
            audio_uuid,
            limit=limit,
            cursor=cursor,
        )

    @router.post(
        "/api/jobs/{audio_uuid}/retry",
        response_model=JobRetryAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def retry_job(
        request: Request,
        response: Response,
        audio_uuid: UUID,
    ) -> JobRetryAcceptedResponse:
        return controller.retry_job(request, response, audio_uuid)

    @router.get("/audios/{audio_uuid}")
    def download_audio(audio_uuid: UUID) -> Response:
        return controller.download_audio(audio_uuid)

    return router


__all__ = ["create_jobs_router"]
