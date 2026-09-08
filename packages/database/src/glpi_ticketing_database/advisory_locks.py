"""PostgreSQL session advisory locks for singleton worker processes."""

from __future__ import annotations

from contextlib import suppress
from types import TracebackType

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .models import WorkerServiceName

# Human-readable ASCII prefixes encoded as signed BIGINT-compatible values.
# These constants are part of the deployment contract and must remain stable.
_LOCK_KEYS: dict[WorkerServiceName, int] = {
    WorkerServiceName.AUTOMATION: 0x4754415F4155544F,  # ``GTA_AUTO``
    WorkerServiceName.RETENTION: 0x4754415F5245544E,  # ``GTA_RETN``
    WorkerServiceName.PUBLISHER: 0x4754415F5055424C,  # ``GTA_PUBL``
}

_TRY_LOCK = text("SELECT pg_try_advisory_lock(:lock_key)")
_UNLOCK = text("SELECT pg_advisory_unlock(:lock_key)")
_BACKEND_PID = text("SELECT pg_backend_pid()")


class AdvisoryLockConnectionLostError(RuntimeError):
    """Report that a previously acquired singleton lock can no longer be trusted."""


class AdvisoryLockReleasedError(RuntimeError):
    """Report an operation attempted on an explicitly released lock handle."""


class AdvisoryLock:
    """Own one PostgreSQL session lock and its dedicated connection.

    A session advisory lock disappears as soon as its physical PostgreSQL
    connection disappears. ``ensure_held`` verifies that SQLAlchemy has not
    transparently replaced that connection with a new backend session.
    """

    def __init__(
        self,
        *,
        connection: Connection,
        service_name: WorkerServiceName,
        lock_key: int,
        backend_pid: int,
    ) -> None:
        self._connection = connection
        self._service_name = service_name
        self._lock_key = lock_key
        self._backend_pid = backend_pid
        self._released = False

    @property
    def service_name(self) -> WorkerServiceName:
        """Return the singleton service protected by this handle."""

        return self._service_name

    @property
    def is_released(self) -> bool:
        """Return whether this handle has completed its release lifecycle."""

        return self._released

    def ensure_held(self) -> None:
        """Fail closed when the dedicated PostgreSQL session was lost."""

        if self._released:
            raise AdvisoryLockReleasedError("advisory lock has already been released")

        try:
            backend_pid = self._connection.scalar(_BACKEND_PID)
            self._connection.commit()
        except SQLAlchemyError:
            self._mark_lost()
            raise AdvisoryLockConnectionLostError(
                "advisory lock connection was lost"
            ) from None

        if backend_pid != self._backend_pid:
            self._mark_lost()
            raise AdvisoryLockConnectionLostError(
                "advisory lock connection was replaced"
            )

    def open_session(self) -> Session:
        """Open an ORM session bound to the lock-owning connection.

        A transaction committed through this session cannot outlive ownership
        of the PostgreSQL session lock: losing the physical connection also
        makes its commit fail. Closing the returned session does not close the
        externally owned connection.
        """

        self.ensure_held()
        return Session(bind=self._connection, expire_on_commit=False)

    def release(self) -> None:
        """Release the PostgreSQL lock and close its dedicated connection."""

        if self._released:
            return

        try:
            backend_pid = self._connection.scalar(_BACKEND_PID)
            if backend_pid != self._backend_pid:
                raise AdvisoryLockConnectionLostError(
                    "advisory lock connection was replaced"
                )
            was_unlocked = self._connection.scalar(
                _UNLOCK,
                {"lock_key": self._lock_key},
            )
            self._connection.commit()
            if was_unlocked is not True:
                raise AdvisoryLockConnectionLostError(
                    "advisory lock was not held by its dedicated connection"
                )
        except AdvisoryLockConnectionLostError:
            self._mark_lost()
            raise
        except SQLAlchemyError:
            self._mark_lost()
            raise AdvisoryLockConnectionLostError(
                "advisory lock connection was lost during release"
            ) from None

        self._released = True
        self._connection.close()

    def __enter__(self) -> AdvisoryLock:
        self.ensure_held()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    def _mark_lost(self) -> None:
        self._released = True
        _discard_connection(self._connection)


def try_acquire_advisory_lock(
    engine: Engine,
    service_name: WorkerServiceName,
) -> AdvisoryLock | None:
    """Try to acquire one worker lock on a newly dedicated connection.

    ``None`` means that another live PostgreSQL session already owns the lock.
    Initial database connectivity errors are deliberately propagated to the
    composition root; only loss *after* acquisition is normalized by the lock
    handle.
    """

    if not isinstance(engine, Engine):
        raise TypeError("engine must be a SQLAlchemy Engine")
    if not isinstance(service_name, WorkerServiceName):
        raise TypeError("service_name must be a WorkerServiceName")

    connection = engine.connect()
    lock_key = _LOCK_KEYS[service_name]
    acquired: object = False
    try:
        acquired = connection.scalar(_TRY_LOCK, {"lock_key": lock_key})
        backend_pid = connection.scalar(_BACKEND_PID)
        connection.commit()
    except BaseException:
        if acquired is True:
            # Returning this physical session to the pool would leak its
            # session-level lock. Invalidation forces PostgreSQL to release it.
            _discard_connection(connection)
        else:
            connection.close()
        raise

    if acquired is not True:
        connection.close()
        return None
    if isinstance(backend_pid, bool) or not isinstance(backend_pid, int):
        # We cannot safely prove which PostgreSQL session owns the lock.
        _discard_connection(connection)
        raise AdvisoryLockConnectionLostError(
            "advisory lock backend identity is invalid"
        )

    return AdvisoryLock(
        connection=connection,
        service_name=service_name,
        lock_key=lock_key,
        backend_pid=backend_pid,
    )


def _discard_connection(connection: Connection) -> None:
    """Force-close a session that may still own an advisory lock."""

    with suppress(Exception):
        connection.invalidate()
    with suppress(Exception):
        connection.close()


__all__ = [
    "AdvisoryLock",
    "AdvisoryLockConnectionLostError",
    "AdvisoryLockReleasedError",
    "try_acquire_advisory_lock",
]
