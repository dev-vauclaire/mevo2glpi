"""PostgreSQL adapter for the automation-worker container health probe."""

from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError

from mevo2glpi_common.runtime_healthcheck import (
    AudioAccessMode,
    HealthcheckCode,
    HealthcheckFailure,
    HealthcheckSettings,
    RuntimeHeartbeat,
    probe_worker_health,
    run_healthcheck,
)
from mevo2glpi_database import (
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
        session_factory = create_session_factory(engine)
        with session_factory() as session:
            snapshot = next(
                (
                    item
                    for item in WorkerRuntimeStatusRepository(session).list_all()
                    if item.service_name is WorkerServiceName.AUTOMATION
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
    """Apply the shared policy with automation's read-only storage contract."""
    probe_worker_health(
        settings,
        heartbeat_reader=_read_heartbeat,
        audio_access_mode=AudioAccessMode.READ_ONLY,
    )


def main() -> int:
    """Container ``HEALTHCHECK`` entry point."""
    return run_healthcheck(
        service_label="automation worker",
        health_probe=probe_health,
    )


if __name__ == "__main__":  # pragma: no cover - exercised by Docker
    raise SystemExit(main())


__all__ = ["main", "probe_health"]
