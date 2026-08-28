"""FastAPI assembly for the layered web application."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from glpi_ticketing_web import __version__
from glpi_ticketing_web.controllers import (
    HealthController,
    JobController,
    PageController,
)
from glpi_ticketing_web.routes import (
    create_health_router,
    create_jobs_router,
    create_pages_router,
)
from glpi_ticketing_web.services import (
    GetAudioJob,
    GetDownloadableAudio,
    SubmitAudioJob,
)
from glpi_ticketing_web.settings import DEFAULT_MAX_UPLOAD_BYTES

_TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
_STATIC_DIRECTORY = Path(__file__).parent / "static"


@dataclass(frozen=True, slots=True)
class _WebConfig:
    audio_root: Path
    max_upload_bytes: int


def create_app(
    *,
    audio_root: Path,
    submit_audio_job: SubmitAudioJob,
    get_audio_job: GetAudioJob,
    get_downloadable_audio: GetDownloadableAudio,
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    shutdown_callback: Callable[[], None] | None = None,
) -> FastAPI:
    """Compose controllers and routers around injected application services."""
    config = _build_web_config(
        audio_root=audio_root,
        max_upload_bytes=max_upload_bytes,
    )

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        try:
            yield
        finally:
            if shutdown_callback is not None:
                shutdown_callback()

    app = FastAPI(
        title="GLPI ticketing automation",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.web_config = config
    app.mount("/static", StaticFiles(directory=_STATIC_DIRECTORY), name="static")

    job_controller = JobController(
        submit_audio_job=submit_audio_job,
        get_audio_job=get_audio_job,
        get_downloadable_audio=get_downloadable_audio,
        templates=_TEMPLATES,
    )
    page_controller = PageController(
        templates=_TEMPLATES,
        max_upload_bytes=config.max_upload_bytes,
    )

    app.include_router(create_health_router(HealthController()))
    app.include_router(create_pages_router(page_controller))
    app.include_router(create_jobs_router(job_controller))
    return app


def _build_web_config(*, audio_root: Path, max_upload_bytes: int) -> _WebConfig:
    if isinstance(max_upload_bytes, bool) or not isinstance(max_upload_bytes, int):
        raise TypeError("max_upload_bytes must be an integer")
    if max_upload_bytes <= 0:
        raise ValueError("max_upload_bytes must be greater than zero")

    resolved_audio_root = Path(audio_root).expanduser().resolve(strict=True)
    if not resolved_audio_root.is_dir():
        raise NotADirectoryError(resolved_audio_root)
    return _WebConfig(
        audio_root=resolved_audio_root,
        max_upload_bytes=max_upload_bytes,
    )


__all__ = ["create_app"]
