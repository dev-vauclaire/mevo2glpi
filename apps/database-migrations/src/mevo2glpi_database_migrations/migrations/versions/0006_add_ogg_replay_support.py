"""Persist ingestion audio formats and support audited operator replay.

Revision ID: 0006_add_ogg_replay_support
Revises: 0005_add_mail_ingestions
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_add_ogg_replay_support"
down_revision: str | None = "0005_add_mail_ingestions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INGESTIONS_TABLE = "mail_ingestions"
_EVENTS_TABLE = "mail_ingestion_events"
_ATTEMPT_STATE = "ck_mail_ingestions_attempt_state"
_ACCEPTED_DISPOSITION = "ck_mail_ingestions_accepted_disposition"
_EVENT_TYPE = "ck_mail_ingestion_events_event_type"

_OLD_ATTEMPT_STATE_SQL = (
    "(status = 'pending' AND stage = 'discovery' "
    "AND attempt_count = 0 AND consecutive_retry_count = 0 "
    "AND first_started_at IS NULL) OR "
    "(status <> 'pending' AND attempt_count > 0 "
    "AND first_started_at IS NOT NULL)"
)
_REPLAYABLE_ATTEMPT_STATE_SQL = (
    "(status = 'pending' AND stage = 'discovery' "
    "AND consecutive_retry_count = 0 "
    "AND ((attempt_count = 0 AND first_started_at IS NULL) "
    "OR (attempt_count > 0 AND first_started_at IS NOT NULL))) OR "
    "(status <> 'pending' AND attempt_count > 0 "
    "AND first_started_at IS NOT NULL)"
)
_OLD_ACCEPTED_DISPOSITION_SQL = (
    "disposition IS NULL OR disposition <> 'accepted' OR "
    "(job_uuid IS NOT NULL AND attachment_id IS NOT NULL "
    "AND attachment_size_bytes IS NOT NULL "
    "AND attachment_size_bytes > 0 "
    "AND rejection_category IS NULL AND rejection_code IS NULL)"
)
_FORMAT_AWARE_ACCEPTED_DISPOSITION_SQL = (
    "disposition IS NULL OR disposition <> 'accepted' OR "
    "(job_uuid IS NOT NULL AND attachment_id IS NOT NULL "
    "AND audio_extension IS NOT NULL "
    "AND attachment_size_bytes IS NOT NULL "
    "AND attachment_size_bytes > 0 "
    "AND rejection_category IS NULL AND rejection_code IS NULL)"
)
_OLD_EVENT_TYPES_SQL = (
    "event_type IN ('message_discovered', 'processing_started', "
    "'processing_resumed', 'attachment_validated', 'audio_published', "
    "'message_rejected', 'retry_scheduled', 'message_moved', "
    "'ingestion_completed', 'ingestion_failed', 'ingestion_blocked')"
)
_REPLAY_EVENT_TYPES_SQL = (
    "event_type IN ('message_discovered', 'processing_started', "
    "'processing_resumed', 'attachment_validated', 'audio_published', "
    "'message_rejected', 'retry_scheduled', 'message_moved', "
    "'ingestion_completed', 'ingestion_failed', 'ingestion_blocked', "
    "'operator_replay_requested')"
)


def upgrade() -> None:
    """Add crash-safe format state and replay-compatible constraints."""
    op.add_column(
        _INGESTIONS_TABLE,
        sa.Column("audio_extension", sa.String(length=4), nullable=True),
    )

    # Accepted rows already point at the source-of-truth job format.
    op.execute(
        sa.text(
            "UPDATE mail_ingestions AS ingestion "
            "SET audio_extension = job.audio_extension "
            "FROM ticket_creation_jobs AS job "
            "WHERE ingestion.job_uuid = job.uuid"
        )
    )
    # Before this revision, reaching publication was only possible for WAV.
    op.execute(
        sa.text(
            "UPDATE mail_ingestions "
            "SET audio_extension = 'wav' "
            "WHERE audio_extension IS NULL "
            "AND stage = 'publication' "
            "AND attachment_id IS NOT NULL "
            "AND attachment_size_bytes > 0"
        )
    )
    # Rejected invalid audio may have reached acknowledgement after a bounded
    # WAV download. Metadata-only attachment rejections intentionally stay NULL.
    op.execute(
        sa.text(
            "UPDATE mail_ingestions "
            "SET audio_extension = 'wav' "
            "WHERE audio_extension IS NULL "
            "AND stage = 'acknowledgement' "
            "AND disposition = 'rejected' "
            "AND rejection_category = 'invalid_audio' "
            "AND attachment_id IS NOT NULL "
            "AND attachment_size_bytes > 0"
        )
    )

    op.drop_constraint(op.f(_ATTEMPT_STATE), _INGESTIONS_TABLE, type_="check")
    op.create_check_constraint(
        op.f(_ATTEMPT_STATE),
        _INGESTIONS_TABLE,
        _REPLAYABLE_ATTEMPT_STATE_SQL,
    )
    op.drop_constraint(
        op.f(_ACCEPTED_DISPOSITION),
        _INGESTIONS_TABLE,
        type_="check",
    )
    op.create_check_constraint(
        op.f(_ACCEPTED_DISPOSITION),
        _INGESTIONS_TABLE,
        _FORMAT_AWARE_ACCEPTED_DISPOSITION_SQL,
    )
    op.create_check_constraint(
        op.f("ck_mail_ingestions_audio_extension"),
        _INGESTIONS_TABLE,
        "audio_extension IS NULL OR audio_extension IN ('wav', 'mp3', 'ogg', 'm4a')",
    )
    op.create_check_constraint(
        op.f("ck_mail_ingestions_pending_state"),
        _INGESTIONS_TABLE,
        "status <> 'pending' OR "
        "(attachment_id IS NULL AND audio_extension IS NULL "
        "AND attachment_size_bytes IS NULL AND job_uuid IS NULL "
        "AND disposition IS NULL AND rejection_category IS NULL "
        "AND rejection_code IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_mail_ingestions_audio_extension_has_attachment"),
        _INGESTIONS_TABLE,
        "audio_extension IS NULL OR "
        "(attachment_id IS NOT NULL AND attachment_size_bytes IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_mail_ingestions_publication_has_audio_extension"),
        _INGESTIONS_TABLE,
        "stage <> 'publication' OR "
        "(audio_extension IS NOT NULL AND attachment_size_bytes > 0)",
    )

    op.drop_constraint(op.f(_EVENT_TYPE), _EVENTS_TABLE, type_="check")
    op.create_check_constraint(
        op.f(_EVENT_TYPE),
        _EVENTS_TABLE,
        _REPLAY_EVENT_TYPES_SQL,
    )


def downgrade() -> None:
    """Restore the WAV-only ingestion contract when no replay audit blocks it."""
    active_non_wav_publications = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM mail_ingestions "
            "WHERE stage = 'publication' "
            "AND audio_extension IS NOT NULL "
            "AND audio_extension <> 'wav'"
        )
    )
    if active_non_wav_publications:
        raise RuntimeError(
            "cannot downgrade while a non-WAV ingestion is at publication"
        )

    op.drop_constraint(op.f(_EVENT_TYPE), _EVENTS_TABLE, type_="check")
    op.create_check_constraint(
        op.f(_EVENT_TYPE),
        _EVENTS_TABLE,
        _OLD_EVENT_TYPES_SQL,
    )

    op.drop_constraint(
        op.f("ck_mail_ingestions_publication_has_audio_extension"),
        _INGESTIONS_TABLE,
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_mail_ingestions_audio_extension_has_attachment"),
        _INGESTIONS_TABLE,
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_mail_ingestions_pending_state"),
        _INGESTIONS_TABLE,
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_mail_ingestions_audio_extension"),
        _INGESTIONS_TABLE,
        type_="check",
    )
    op.drop_constraint(
        op.f(_ACCEPTED_DISPOSITION),
        _INGESTIONS_TABLE,
        type_="check",
    )
    op.create_check_constraint(
        op.f(_ACCEPTED_DISPOSITION),
        _INGESTIONS_TABLE,
        _OLD_ACCEPTED_DISPOSITION_SQL,
    )
    op.drop_constraint(op.f(_ATTEMPT_STATE), _INGESTIONS_TABLE, type_="check")
    op.create_check_constraint(
        op.f(_ATTEMPT_STATE),
        _INGESTIONS_TABLE,
        _OLD_ATTEMPT_STATE_SQL,
    )
    op.drop_column(_INGESTIONS_TABLE, "audio_extension")
