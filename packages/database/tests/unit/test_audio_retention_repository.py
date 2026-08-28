from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from glpi_ticketing_database import (
    AudioRetention,
    AudioRetentionRepository,
    RetentionPolicy,
    RetentionStatus,
)

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)


def test_activate_creates_only_an_audio_retention() -> None:
    session = create_autospec(Session, instance=True)
    session.get.return_value = None
    job_uuid = uuid4()

    AudioRetentionRepository(session).activate(
        job_uuid,
        policy=RetentionPolicy.TICKET_OR_DEADLINE,
        hard_delete_at=NOW + timedelta(days=90),
        next_check_at=NOW + timedelta(hours=6),
        now=NOW,
    )

    retention = session.add.call_args.args[0]
    assert isinstance(retention, AudioRetention)
    assert retention.job_uuid == job_uuid
    assert retention.policy == RetentionPolicy.TICKET_OR_DEADLINE.value
    assert retention.status == RetentionStatus.SCHEDULED.value
    assert retention.hard_delete_at == NOW + timedelta(days=90)
    assert retention.next_check_at == NOW + timedelta(hours=6)
    session.flush.assert_called_once_with()


def test_activate_resets_only_an_existing_audio_retention() -> None:
    session = create_autospec(Session, instance=True)
    job_uuid = uuid4()
    retention = AudioRetention(
        job_uuid=job_uuid,
        policy=RetentionPolicy.TICKET_OR_DEADLINE.value,
        status=RetentionStatus.BLOCKED.value,
        hard_delete_at=NOW + timedelta(days=1),
        next_check_at=None,
        check_attempt_count=3,
        consecutive_retry_count=2,
        last_error_category="unavailable",
        last_error_code="http_503",
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(hours=1),
    )
    session.get.return_value = retention

    AudioRetentionRepository(session).activate(
        job_uuid,
        policy=RetentionPolicy.DEADLINE_ONLY,
        hard_delete_at=NOW + timedelta(days=30),
        next_check_at=None,
        now=NOW,
    )

    assert retention.policy == RetentionPolicy.DEADLINE_ONLY.value
    assert retention.status == RetentionStatus.SCHEDULED.value
    assert retention.hard_delete_at == NOW + timedelta(days=30)
    assert retention.next_check_at is None
    assert retention.consecutive_retry_count == 0
    assert retention.last_error_category is None
    assert retention.last_error_code is None
    session.add.assert_not_called()
    session.flush.assert_called_once_with()


@pytest.mark.parametrize(
    ("policy", "next_check_at"),
    [
        (RetentionPolicy.TICKET_OR_DEADLINE, None),
        (RetentionPolicy.DEADLINE_ONLY, NOW + timedelta(hours=6)),
    ],
)
def test_activate_rejects_a_schedule_incompatible_with_its_policy(
    policy: RetentionPolicy,
    next_check_at: datetime | None,
) -> None:
    session = create_autospec(Session, instance=True)

    with pytest.raises(ValueError):
        AudioRetentionRepository(session).activate(
            uuid4(),
            policy=policy,
            hard_delete_at=NOW + timedelta(days=30),
            next_check_at=next_check_at,
            now=NOW,
        )

    session.get.assert_not_called()
    session.add.assert_not_called()
    session.flush.assert_not_called()


def test_retention_repository_never_owns_transaction_or_session() -> None:
    session = create_autospec(Session, instance=True)
    session.get.return_value = None

    AudioRetentionRepository(session).activate(
        uuid4(),
        policy=RetentionPolicy.DEADLINE_ONLY,
        hard_delete_at=NOW + timedelta(days=30),
        next_check_at=None,
        now=NOW,
    )

    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    session.close.assert_not_called()
