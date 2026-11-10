"""FastAPI assembly for the layered web application."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from mevo2glpi_common.release_identity import (
    DeploymentEnvironment,
    ReleaseIdentity,
)
from mevo2glpi_web import __version__
from mevo2glpi_web.auth.http import (
    AuthenticatedCsrfProtection,
    LoginCsrfProtection,
    SessionCookiePolicy,
)
from mevo2glpi_web.auth.models import AuthMode
from mevo2glpi_web.controllers import (
    AuthenticationController,
    AuthenticationServices,
    HealthController,
    JobController,
    ObservabilityController,
    PageController,
    RejectionController,
    VersionController,
)
from mevo2glpi_web.middlewares import WebAuthenticationMiddleware
from mevo2glpi_web.routes import (
    create_authentication_router,
    create_health_router,
    create_jobs_router,
    create_observability_router,
    create_pages_router,
    create_rejections_router,
    create_version_router,
)
from mevo2glpi_web.schemas import VersionResponse
from mevo2glpi_web.services import (
    GetAudioJob,
    GetDownloadableAudio,
    GetObservability,
    ListAudioJobEvents,
    ListAudioJobs,
    ListRejectedIngestions,
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
    list_rejected_ingestions: ListRejectedIngestions,
    retry_audio_job: RetryAudioJob,
    get_downloadable_audio: GetDownloadableAudio,
    get_observability: GetObservability,
    release_identity: ReleaseIdentity,
    authentication: AuthenticationServices,
    session_cookie_policy: SessionCookiePolicy,
    required_auth_role: str,
    oidc_transaction_secret: str | None = None,
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
        title="mevo2glpi",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if documentation_enabled else None,
        redoc_url="/redoc" if documentation_enabled else None,
        openapi_url="/openapi.json" if documentation_enabled else None,
    )
    app.state.web_config = config
    app.mount("/static", StaticFiles(directory=_STATIC_DIRECTORY), name="static")

    csrf_protection = AuthenticatedCsrfProtection()
    job_controller = JobController(
        list_audio_jobs=list_audio_jobs,
        get_audio_job=get_audio_job,
        list_audio_job_events=list_audio_job_events,
        retry_audio_job=retry_audio_job,
        get_downloadable_audio=get_downloadable_audio,
        csrf_protection=csrf_protection,
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
    rejection_controller = RejectionController(list_rejected_ingestions)
    version_controller = VersionController(version)
    authentication_controller = AuthenticationController(
        templates=_TEMPLATES,
        services=authentication,
        cookie_policy=session_cookie_policy,
        login_csrf=LoginCsrfProtection(secure=session_cookie_policy.secure),
    )

    app.include_router(create_health_router(HealthController()))
    app.include_router(create_authentication_router(authentication_controller))
    app.include_router(create_pages_router(page_controller))
    app.include_router(create_jobs_router(job_controller))
    app.include_router(create_rejections_router(rejection_controller))
    app.include_router(create_observability_router(observability_controller))
    app.include_router(create_version_router(version_controller))

    app.add_middleware(
        WebAuthenticationMiddleware,
        session_manager=authentication.sessions,
        cookie_policy=session_cookie_policy,
        required_role=required_auth_role,
    )
    if authentication.mode is AuthMode.OIDC:
        if oidc_transaction_secret is None:
            raise ValueError("OIDC mode requires a transaction-session secret")
        app.add_middleware(
            SessionMiddleware,
            secret_key=oidc_transaction_secret,
            session_cookie="mevo2glpi_oidc_transaction",
            max_age=600,
            path="/auth",
            same_site="lax",
            https_only=session_cookie_policy.secure,
        )
    elif oidc_transaction_secret is not None:
        raise ValueError("local mode cannot configure an OIDC transaction session")
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
