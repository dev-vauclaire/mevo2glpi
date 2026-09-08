"""PostgreSQL and storage health probe for the publisher container."""

from __future__ import annotations

import os

from sqlalchemy.exc import SQLAlchemyError

from glpi_ticketing_common.runtime_healthcheck import (
    AudioAccessMode,
    HealthcheckCode,
    HealthcheckFailure,
    HealthcheckSettings,
    RuntimeHeartbeat,
    probe_worker_health,
    run_healthcheck,
)
from glpi_ticketing_database import (
    DatabaseConfigurationError,
    WorkerRuntimeStatusRepository,
    WorkerServiceName,
    create_database_engine,
    create_session_factory,
)


def _read_heartbeat(settings: HealthcheckSettings) -> RuntimeHeartbeat | None:
    engine = None
    try:
        engine = create_database_engine(
            settings.database_url,
            connect_timeout_seconds=settings.database_connect_timeout_seconds,
        )
        with create_session_factory(engine)() as session:
            snapshot = next(
                (
                    item
                    for item in WorkerRuntimeStatusRepository(session).list_all()
                    if item.service_name is WorkerServiceName.PUBLISHER
                ),
                None,
            )
    except DatabaseConfigurationError:
        raise HealthcheckFailure(HealthcheckCode.INVALID_CONFIGURATION) from None
    except SQLAlchemyError:
        raise HealthcheckFailure(HealthcheckCode.DATABASE_UNAVAILABLE) from None
    finally:
        if engine is not None:
            engine.dispose()

    if snapshot is None:
        return None
    return RuntimeHeartbeat(
        state=snapshot.state.value,
        last_heartbeat_at=snapshot.last_heartbeat_at,
    )


def probe_health(settings: HealthcheckSettings) -> None:
    """Require publisher liveness and read/write access to the audio volume."""
    probe_worker_health(
        settings,
        heartbeat_reader=_read_heartbeat,
        audio_access_mode=AudioAccessMode.READ_WRITE,
    )


def _settings_from_environment() -> HealthcheckSettings:
    values = dict(os.environ)
    publisher_interval = values.get("PUBLISHER_HEARTBEAT_INTERVAL_SECONDS")
    if (
        publisher_interval is not None
        and "WORKER_HEARTBEAT_INTERVAL_SECONDS" not in values
    ):
        values["WORKER_HEARTBEAT_INTERVAL_SECONDS"] = publisher_interval
    return HealthcheckSettings.from_environment(values)


def main() -> int:
    """Container ``HEALTHCHECK`` entry point."""
    return run_healthcheck(
        service_label="publisher worker",
        health_probe=probe_health,
        settings_factory=_settings_from_environment,
    )


if __name__ == "__main__":  # pragma: no cover - exercised by Docker
    raise SystemExit(main())


__all__ = ["main", "probe_health"]
