from unittest.mock import create_autospec

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from glpi_ticketing_database import (
    AdvisoryLockConnectionLostError,
    AdvisoryLockReleasedError,
    WorkerServiceName,
    try_acquire_advisory_lock,
)


def test_try_acquire_owns_a_dedicated_connection_until_release() -> None:
    engine, connection = _engine_and_connection()
    connection.scalar.side_effect = [True, 4123]

    lock = try_acquire_advisory_lock(engine, WorkerServiceName.AUTOMATION)

    assert lock is not None
    assert lock.service_name is WorkerServiceName.AUTOMATION
    assert lock.is_released is False
    connection.close.assert_not_called()

    connection.scalar.side_effect = [4123, True]
    lock.release()

    assert lock.is_released is True
    connection.close.assert_called_once_with()
    assert connection.commit.call_count == 2


def test_unavailable_lock_closes_its_candidate_connection() -> None:
    engine, connection = _engine_and_connection()
    connection.scalar.side_effect = [False, 4123]

    assert try_acquire_advisory_lock(engine, WorkerServiceName.AUTOMATION) is None

    connection.close.assert_called_once_with()


def test_worker_services_use_distinct_stable_lock_keys() -> None:
    first_engine, first_connection = _engine_and_connection()
    second_engine, second_connection = _engine_and_connection()
    first_connection.scalar.side_effect = [False, 10]
    second_connection.scalar.side_effect = [False, 11]

    try_acquire_advisory_lock(first_engine, WorkerServiceName.AUTOMATION)
    try_acquire_advisory_lock(second_engine, WorkerServiceName.RETENTION)

    first_parameters = first_connection.scalar.call_args_list[0].args[1]
    second_parameters = second_connection.scalar.call_args_list[0].args[1]
    assert first_parameters["lock_key"] != second_parameters["lock_key"]


def test_replaced_backend_connection_is_observable_and_fails_closed() -> None:
    engine, connection = _engine_and_connection()
    connection.scalar.side_effect = [True, 4123]
    lock = try_acquire_advisory_lock(engine, WorkerServiceName.AUTOMATION)
    assert lock is not None
    connection.scalar.side_effect = [9999]

    with pytest.raises(AdvisoryLockConnectionLostError):
        lock.ensure_held()

    assert lock.is_released is True
    connection.invalidate.assert_called_once_with()
    connection.close.assert_called_once_with()


def test_database_error_after_acquisition_is_normalized_without_details() -> None:
    engine, connection = _engine_and_connection()
    connection.scalar.side_effect = [True, 4123]
    lock = try_acquire_advisory_lock(engine, WorkerServiceName.AUTOMATION)
    assert lock is not None
    connection.scalar.side_effect = SQLAlchemyError("database-secret")

    with pytest.raises(AdvisoryLockConnectionLostError) as captured_error:
        lock.ensure_held()

    assert "database-secret" not in str(captured_error.value)
    assert captured_error.value.__cause__ is None
    connection.invalidate.assert_called_once_with()


def test_error_after_lock_grant_discards_session_instead_of_leaking_lock() -> None:
    engine, connection = _engine_and_connection()
    connection.scalar.side_effect = [True, SQLAlchemyError("identity failed")]

    with pytest.raises(SQLAlchemyError):
        try_acquire_advisory_lock(engine, WorkerServiceName.AUTOMATION)

    connection.invalidate.assert_called_once_with()
    connection.close.assert_called_once_with()


def test_released_lock_cannot_be_asserted_but_release_is_idempotent() -> None:
    engine, connection = _engine_and_connection()
    connection.scalar.side_effect = [True, 4123]
    lock = try_acquire_advisory_lock(engine, WorkerServiceName.AUTOMATION)
    assert lock is not None
    connection.scalar.side_effect = [4123, True]
    lock.release()

    lock.release()
    with pytest.raises(AdvisoryLockReleasedError):
        lock.ensure_held()
    with pytest.raises(AdvisoryLockReleasedError):
        lock.open_session()

    connection.close.assert_called_once_with()


def _engine_and_connection() -> tuple[Engine, Connection]:
    engine = create_autospec(Engine, instance=True)
    connection = create_autospec(Connection, instance=True)
    engine.connect.return_value = connection
    return engine, connection
