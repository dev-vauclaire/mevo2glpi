"""Unit contracts for bounded, model-specific alert aggregates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import create_autospec

import pytest
from sqlalchemy.orm import Session

import glpi_ticketing_database as database_api
from glpi_ticketing_database import (
    AlertAggregate,
    AudioRetentionRepository,
    JobAlertSummary,
    RetentionAlertSummary,
    TicketCreationJobRepository,
)

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)


def test_job_alert_summary_maps_counts_and_oldest_timestamps_without_commit() -> None:
    session = create_autospec(Session, instance=True)
    pending_since = NOW - timedelta(minutes=20)
    retry_since = NOW - timedelta(minutes=2)
    failed_since = NOW - timedelta(days=1)
    session.execute.return_value.one.return_value = (
        2,
        pending_since,
        1,
        retry_since,
        3,
        failed_since,
        0,
        None,
    )

    summary = TicketCreationJobRepository(session).get_alert_summary(
        pending_before=NOW - timedelta(minutes=15),
        retry_before=NOW - timedelta(seconds=60),
    )

    assert summary == JobAlertSummary(
        pending_overdue=AlertAggregate(count=2, condition_since=pending_since),
        retry_overdue=AlertAggregate(count=1, condition_since=retry_since),
        failed=AlertAggregate(count=3, condition_since=failed_since),
        blocked=AlertAggregate(count=0, condition_since=None),
    )
    session.execute.assert_called_once()
    statement = session.execute.call_args.args[0]
    rendered_sql = str(statement).upper()
    assert rendered_sql.count("SELECT COUNT(*)") == 4
    assert "FILTER (WHERE" not in rendered_sql
    session.flush.assert_not_called()
    session.commit.assert_not_called()


def test_retention_alert_summary_maps_exclusive_categories_without_commit() -> None:
    session = create_autospec(Session, instance=True)
    deadline_since = NOW - timedelta(minutes=3)
    blocked_since = NOW - timedelta(minutes=2)
    retry_since = NOW - timedelta(minutes=1)
    session.execute.return_value.one.return_value = (
        1,
        deadline_since,
        2,
        blocked_since,
        0,
        None,
        4,
        retry_since,
        0,
        None,
    )

    summary = AudioRetentionRepository(session).get_alert_summary(
        retry_before=NOW - timedelta(seconds=60),
        deadline_before=NOW - timedelta(seconds=120),
    )

    assert summary == RetentionAlertSummary(
        deadline_overdue=AlertAggregate(count=1, condition_since=deadline_since),
        glpi_check_blocked=AlertAggregate(count=2, condition_since=blocked_since),
        deletion_blocked=AlertAggregate(count=0, condition_since=None),
        glpi_retry_overdue=AlertAggregate(count=4, condition_since=retry_since),
        deletion_retry_overdue=AlertAggregate(count=0, condition_since=None),
    )
    session.execute.assert_called_once()
    statement = session.execute.call_args.args[0]
    rendered_sql = str(statement).upper()
    assert rendered_sql.count("SELECT COUNT(*)") == 5
    assert "FILTER (WHERE" not in rendered_sql
    session.flush.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.parametrize(
    ("repository_type", "arguments"),
    [
        (
            TicketCreationJobRepository,
            {
                "pending_before": NOW.replace(tzinfo=None),
                "retry_before": NOW,
            },
        ),
        (
            TicketCreationJobRepository,
            {
                "pending_before": NOW,
                "retry_before": NOW.astimezone(timezone(timedelta(hours=1))),
            },
        ),
        (
            AudioRetentionRepository,
            {
                "retry_before": NOW,
                "deadline_before": NOW.replace(tzinfo=None),
            },
        ),
    ],
)
def test_alert_summaries_reject_non_utc_cutoffs_before_query(
    repository_type: type[TicketCreationJobRepository] | type[AudioRetentionRepository],
    arguments: dict[str, datetime],
) -> None:
    session = create_autospec(Session, instance=True)

    with pytest.raises(ValueError, match="timezone-aware|UTC"):
        repository_type(session).get_alert_summary(**arguments)

    session.execute.assert_not_called()


def test_alert_summary_contracts_are_exported_by_package() -> None:
    assert database_api.AlertAggregate is AlertAggregate
    assert database_api.JobAlertSummary is JobAlertSummary
    assert database_api.RetentionAlertSummary is RetentionAlertSummary
