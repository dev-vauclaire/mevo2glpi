"""FastAPI assembly for the layered web application."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from glpi_ticketing_common.release_identity import (
    DeploymentEnvironment,
    ReleaseIdentity,
)
from glpi_ticketing_web import __version__
from glpi_ticketing_web.controllers import (
    HealthController,
    JobController,
    ObservabilityController,
    PageController,
    VersionController,
)
from glpi_ticketing_web.routes import (
    create_health_router,
    create_jobs_router,
    create_observability_router,
    create_pages_router,
    create_version_router,
)
from glpi_ticketing_web.schemas import VersionResponse
from glpi_ticketing_web.services import (
    GetAudioJob,
    GetDownloadableAudio,
    GetObservability,
    ListAudioJobEvents,
    ListAudioJobs,
    RetryAudioJob,
)

_TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
_STATIC_DIRECTORY = Path(__file__).parent / "static"


@dataclass(frozen=True, slots=True)
class _WebConfig:
    audio_root: Path
    release_identity: ReleaseIdentity


def create_app(
    *,
    audio_root: Path,
    list_audio_jobs: ListAudioJobs,
    get_audio_job: GetAudioJob,
    list_audio_job_events: ListAudioJobEvents,
    retry_audio_job: RetryAudioJob,
    get_downloadable_audio: GetDownloadableAudio,
    get_observability: GetObservability,
    release_identity: ReleaseIdentity,
    shutdown_callback: Callable[[], None] | None = None,
) -> FastAPI:
    """Compose controllers and routers around injected application services."""
    config = _build_web_config(
        audio_root=audio_root,
        release_identity=release_identity,
    )

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        try:
            yield
        finally:
            if shutdown_callback is not None:
                shutdown_callback()

    documentation_enabled = (
        release_identity.environment is not DeploymentEnvironment.PRODUCTION
    )
    app = FastAPI(
        title="GLPI ticketing automation",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if documentation_enabled else None,
        redoc_url="/redoc" if documentation_enabled else None,
        openapi_url="/openapi.json" if documentation_enabled else None,
    )
    app.state.web_config = config
    app.mount("/static", StaticFiles(directory=_STATIC_DIRECTORY), name="static")

    job_controller = JobController(
        list_audio_jobs=list_audio_jobs,
        get_audio_job=get_audio_job,
        list_audio_job_events=list_audio_job_events,
        retry_audio_job=retry_audio_job,
        get_downloadable_audio=get_downloadable_audio,
    )
    version = VersionResponse(
        environment=config.release_identity.environment.value,
        release=config.release_identity.release,
        commit_sha=config.release_identity.commit_sha,
    )
    page_controller = PageController(
        templates=_TEMPLATES,
        version=version,
    )
    observability_controller = ObservabilityController(get_observability)
    version_controller = VersionController(version)

    app.include_router(create_health_router(HealthController()))
    app.include_router(create_pages_router(page_controller))
    app.include_router(create_jobs_router(job_controller))
    app.include_router(create_observability_router(observability_controller))
    app.include_router(create_version_router(version_controller))
    return app


def _build_web_config(
    *,
    audio_root: Path,
    release_identity: ReleaseIdentity,
) -> _WebConfig:
    resolved_audio_root = Path(audio_root).expanduser().resolve(strict=True)
    if not resolved_audio_root.is_dir():
        raise NotADirectoryError(resolved_audio_root)
    if not isinstance(release_identity, ReleaseIdentity):
        raise TypeError("release_identity must be a ReleaseIdentity")
    return _WebConfig(
        audio_root=resolved_audio_root,
        release_identity=release_identity,
    )


__all__ = ["create_app"]
