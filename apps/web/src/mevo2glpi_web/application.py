"""Composition root and executable lifecycle for the web application."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import IntEnum
from typing import Protocol

import uvicorn
from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from mevo2glpi_database import (
    DatabaseConfigurationError,
    create_database_engine,
    create_session_factory,
)

from .auth.audit import LoggingAuthAuditRecorder
from .auth.http import SessionCookiePolicy
from .auth.local import LocalAuthenticator
from .auth.models import AuthMode
from .auth.oidc import (
    OidcAuthenticator,
    OidcConfiguration,
    register_authlib_oidc_client,
)
from .auth.passwords import Argon2PasswordHasher
from .auth.persistence import (
    SqlAlchemyLocalAccountStore,
)
from .auth.sessions import (
    ItsDangerousSessionSigner,
    SessionCookieCodec,
    SessionManager,
)
from .controllers import AuthenticationServices
from .services import (
    GetAudioJob,
    GetDownloadableAudio,
    GetObservability,
    ListAudioJobEvents,
    ListAudioJobs,
    ListRejectedIngestions,
    ObservabilityThresholds,
    RetryAudioJob,
)
from .settings import WebAuthMode, WebSettings
from .web import create_app

LOGGER = logging.getLogger(__name__)


class WebExitCode(IntEnum):
    """Stable process exit codes exposed to the container runtime."""

    SUCCESS = 0
    UNEXPECTED_ERROR = 1
    CONFIGURATION_ERROR = 2
    INTERRUPTED = 130


class WebConfigurationError(ValueError):
    """Report invalid runtime configuration without exposing its values."""


class _Server(Protocol):
    def run(self) -> None:
        """Serve until Uvicorn handles a standard shutdown signal."""
        ...


class _ServerFactory(Protocol):
    def __call__(self, settings: WebSettings) -> _Server:
        """Return a configured web server."""


@dataclass(frozen=True, slots=True)
class WebAuthenticationComposition:
    """Authentication dependencies safe to inject into the HTTP assembly."""

    services: AuthenticationServices
    cookie_policy: SessionCookiePolicy
    oidc_transaction_secret: str | None


class _AuthenticationFactory(Protocol):
    def __call__(
        self,
        settings: WebSettings,
        session_factory: sessionmaker[Session],
    ) -> WebAuthenticationComposition:
        """Build the selected fail-closed authentication mode."""
        ...


def build_application(
    settings: WebSettings,
    *,
    authentication_factory: _AuthenticationFactory | None = None,
) -> FastAPI:
    """Compose FastAPI, canonical storage and lazy PostgreSQL connections."""
    if not isinstance(settings, WebSettings):
        raise TypeError("settings must be WebSettings")

    engine = None
    try:
        audio_root = settings.audio_root.expanduser().resolve(strict=True)
        if not audio_root.is_dir():
            raise NotADirectoryError(audio_root)
        engine = create_database_engine(
            settings.database_url.get_secret_value(),
            connect_timeout_seconds=5,
        )
        session_factory = create_session_factory(engine)
        authentication = (authentication_factory or _build_authentication)(
            settings,
            session_factory,
        )
        get_audio_job = GetAudioJob(session_factory)
        observability_thresholds = ObservabilityThresholds.from_seconds(
            worker_stale_after=settings.dashboard_worker_stale_after_seconds,
            pending_alert_after=settings.dashboard_pending_alert_after_seconds,
            retry_overdue_grace=settings.dashboard_retry_overdue_grace_seconds,
            retention_deadline_grace=(
                settings.dashboard_retention_deadline_grace_seconds
            ),
        )
        return create_app(
            audio_root=audio_root,
            list_audio_jobs=ListAudioJobs(session_factory),
            get_audio_job=get_audio_job,
            list_audio_job_events=ListAudioJobEvents(session_factory),
            list_rejected_ingestions=ListRejectedIngestions(session_factory),
            retry_audio_job=RetryAudioJob(
                audio_root=audio_root,
                session_factory=session_factory,
                get_audio_job=get_audio_job,
            ),
            get_downloadable_audio=GetDownloadableAudio(
                audio_root,
                get_audio_job,
            ),
            get_observability=GetObservability(
                session_factory,
                thresholds=observability_thresholds,
            ),
            release_identity=settings.release_identity,
            authentication=authentication.services,
            session_cookie_policy=authentication.cookie_policy,
            required_auth_role=settings.web_auth_required_role,
            oidc_transaction_secret=authentication.oidc_transaction_secret,
            shutdown_callback=engine.dispose,
        )
    except (DatabaseConfigurationError, OSError, RuntimeError, ValueError):
        if engine is not None:
            engine.dispose()
        raise WebConfigurationError(
            "web database or audio storage configuration is invalid"
        ) from None


def _build_authentication(
    settings: WebSettings,
    session_factory: sessionmaker[Session],
) -> WebAuthenticationComposition:
    secret = settings.web_auth_session_secret.get_secret_value()
    mode = AuthMode(settings.web_auth_mode.value)
    account_store = (
        SqlAlchemyLocalAccountStore(session_factory) if mode is AuthMode.LOCAL else None
    )
    session_manager = SessionManager(
        SessionCookieCodec(ItsDangerousSessionSigner(secret)),
        auth_mode=mode,
        local_accounts=account_store,
        lifetime=timedelta(seconds=settings.web_auth_session_max_age_seconds),
    )
    local_authenticator: LocalAuthenticator | None = None
    oidc_authenticator: OidcAuthenticator | None = None
    oidc_transaction_secret: str | None = None

    if settings.web_auth_mode is WebAuthMode.LOCAL:
        assert account_store is not None
        local_authenticator = LocalAuthenticator(
            account_store,
            Argon2PasswordHasher(),
            access_role=settings.web_auth_required_role,
        )
    else:
        assert settings.web_auth_issuer_url is not None
        assert settings.web_auth_client_id is not None
        assert settings.web_auth_client_secret is not None
        assert settings.web_auth_redirect_uri is not None
        oidc_configuration = OidcConfiguration(
            issuer=str(settings.web_auth_issuer_url),
            client_id=settings.web_auth_client_id,
            client_secret=settings.web_auth_client_secret.get_secret_value(),
            redirect_uri=str(settings.web_auth_redirect_uri),
            pseudonymization_secret=secret,
            roles_claim=settings.web_auth_roles_claim,
            required_role=settings.web_auth_required_role,
        )
        registry = OAuth()
        oidc_authenticator = OidcAuthenticator(
            register_authlib_oidc_client(registry, oidc_configuration),
            oidc_configuration,
        )
        oidc_transaction_secret = secret

    return WebAuthenticationComposition(
        services=AuthenticationServices(
            mode=mode,
            sessions=session_manager,
            audit=LoggingAuthAuditRecorder(),
            local=local_authenticator,
            oidc=oidc_authenticator,
        ),
        cookie_policy=SessionCookiePolicy(
            secure=settings.web_auth_cookie_secure,
            max_age_seconds=settings.web_auth_session_max_age_seconds,
        ),
        oidc_transaction_secret=oidc_transaction_secret,
    )


def build_server(settings: WebSettings) -> uvicorn.Server:
    """Create the single-process Uvicorn server used by the MVP."""
    application = build_application(settings)
    config = uvicorn.Config(
        application,
        host=settings.web_host,
        port=settings.web_port,
        log_level="info",
        # Uvicorn logs the complete query string. An OIDC callback carries a
        # short-lived authorization code and state, so only the sanitized
        # authentication logger must record this HTTP exchange.
        access_log=False,
        proxy_headers=settings.web_trust_proxy_headers,
        forwarded_allow_ips="*" if settings.web_trust_proxy_headers else [],
    )
    return uvicorn.Server(config)


def run_application(
    settings: WebSettings,
    *,
    server_factory: _ServerFactory = build_server,
) -> None:
    """Run Uvicorn until its standard SIGINT or SIGTERM shutdown."""
    server_factory(settings).run()


def run_cli(
    *,
    settings_factory: Callable[[], WebSettings] = WebSettings,
    application_runner: Callable[[WebSettings], None] = run_application,
) -> WebExitCode:
    """Load configuration and run the web server with sanitized diagnostics."""
    try:
        settings = settings_factory()
    except ValidationError:
        LOGGER.error("web configuration is invalid")
        return WebExitCode.CONFIGURATION_ERROR
    except Exception as error:  # pragma: no cover - defensive startup boundary
        LOGGER.error(
            "web configuration loading failed error_type=%s",
            type(error).__name__,
        )
        return WebExitCode.UNEXPECTED_ERROR

    release_identity = settings.release_identity
    LOGGER.info(
        "web server starting app_environment=%s app_release=%s app_commit_sha=%s",
        release_identity.environment.value,
        release_identity.release,
        release_identity.commit_sha,
    )
    try:
        application_runner(settings)
    except WebConfigurationError:
        LOGGER.error("web runtime configuration is invalid")
        return WebExitCode.CONFIGURATION_ERROR
    except KeyboardInterrupt:
        LOGGER.info("web server interrupted")
        return WebExitCode.INTERRUPTED
    except Exception as error:  # pragma: no cover - exercised through a fake runner
        LOGGER.error(
            "web server stopped unexpectedly error_type=%s",
            type(error).__name__,
        )
        return WebExitCode.UNEXPECTED_ERROR

    LOGGER.info("web server stopped")
    return WebExitCode.SUCCESS


def main() -> int:
    """Console-script entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return int(run_cli())


__all__ = [
    "WebConfigurationError",
    "WebAuthenticationComposition",
    "WebExitCode",
    "build_application",
    "build_server",
    "main",
    "run_application",
    "run_cli",
]
