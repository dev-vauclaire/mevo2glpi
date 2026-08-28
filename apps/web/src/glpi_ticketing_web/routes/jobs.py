"""Route declarations for audio jobs."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Request, Response, UploadFile, status
from fastapi.responses import HTMLResponse

from glpi_ticketing_web.controllers import JobController
from glpi_ticketing_web.schemas import JobAcceptedResponse, JobDetailResponse


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

    @router.post(
        "/audios",
        response_class=HTMLResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def upload_audio(
        request: Request,
        audio_file: Annotated[UploadFile, File(description="Audio file to process")],
    ) -> Response:
        return controller.upload_audio(request, audio_file)

    @router.get("/api/jobs/{audio_uuid}", response_model=JobDetailResponse)
    def get_job(audio_uuid: UUID) -> JobDetailResponse:
        return controller.get_job(audio_uuid)

    @router.get("/audios/{audio_uuid}")
    def download_audio(audio_uuid: UUID) -> Response:
        return controller.download_audio(audio_uuid)

    return router


__all__ = ["create_jobs_router"]
