"""Repository dedicated to the ``MailIngestion`` model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ORIGINATING_PHONE_NUMBER_CIPHERTEXT_MAX_BYTES,
    AudioExtension,
    MailIngestion,
    MailIngestionDisposition,
    MailIngestionErrorScope,
    MailIngestionStage,
    MailIngestionStatus,
)
from ._validation import (
    validate_positive_integer,
    validate_safe_identifier,
    validate_utc_datetime,
    validate_uuid,
)

_MAX_GRAPH_IDENTIFIER_LENGTH = 512


class MailIngestionNotFoundError(LookupError):
    """Report a transition requested for an unknown mail ingestion."""


class InvalidMailIngestionTransitionError(RuntimeError):
    """Report a transition incompatible with the persisted ingestion state."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DiscoveredMail:
    """Validated source identity needed to persist a discovered Graph message."""

    uuid: UUID
    mailbox_id: UUID
    message_id: str
    received_at: datetime
    originating_phone_number_ciphertext: bytes | None = field(
        default=None,
        repr=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class MailIngestionSnapshot:
    """Immutable typed view of one persisted mail ingestion."""

    uuid: UUID
    mailbox_id: UUID
    message_id: str
    attachment_id: str | None
    job_uuid: UUID | None
    status: MailIngestionStatus
    stage: MailIngestionStage
    disposition: MailIngestionDisposition | None
    audio_extension: AudioExtension | None
    attachment_size_bytes: int | None
    attempt_count: int
    consecutive_retry_count: int
    next_attempt_at: datetime | None
    rejection_category: str | None
    rejection_code: str | None
    last_error_scope: MailIngestionErrorScope | None
    last_error_category: str | None
    last_error_code: str | None
    received_at: datetime
    created_at: datetime
    first_started_at: datetime | None
    updated_at: datetime
    finished_at: datetime | None
    originating_phone_number_ciphertext: bytes | None = field(
        default=None,
        repr=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class MailIngestionAcquisition:
    """Describe one deterministic publisher claim or crash recovery."""

    ingestion: MailIngestionSnapshot
    previous_status: MailIngestionStatus
    resumed: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class MailIngestionReplayFilter:
    """Typed, bounded selector for one operator-requested replay batch."""

    status: MailIngestionStatus
    disposition: MailIngestionDisposition
    rejection_category: str
    rejection_code: str
    received_after: datetime
    received_before: datetime
    limit: int
    job_uuid_is_null: bool = True
    attachment_id_is_not_null: bool = False

    def __post_init__(self) -> None:
        _validate_replay_filter(self)


class MailIngestionRepository:
    """Read and mutate only mail ingestions in a caller-owned transaction."""

    def __init__(self, session: Session) -> None:
        if not isinstance(session, Session):
            raise TypeError("session must be a SQLAlchemy Session")
        self._session = session

    def add_discovered(
        self,
        discovered_mail: DiscoveredMail,
        *,
        now: datetime,
    ) -> MailIngestionSnapshot:
        """Add one source message without creating a job or an event."""
        _validate_discovered_mail(discovered_mail)
        now = validate_utc_datetime(now, "now")
        ingestion = MailIngestion(
            uuid=discovered_mail.uuid,
            mailbox_id=discovered_mail.mailbox_id,
            message_id=discovered_mail.message_id,
            originating_phone_number_ciphertext=(
                discovered_mail.originating_phone_number_ciphertext
            ),
            attachment_id=None,
            job_uuid=None,
            status=MailIngestionStatus.PENDING.value,
            stage=MailIngestionStage.DISCOVERY.value,
            disposition=None,
            audio_extension=None,
            attachment_size_bytes=None,
            attempt_count=0,
            consecutive_retry_count=0,
            next_attempt_at=None,
            rejection_category=None,
            rejection_code=None,
            last_error_scope=None,
            last_error_category=None,
            last_error_code=None,
            received_at=discovered_mail.received_at,
            created_at=now,
            first_started_at=None,
            updated_at=now,
            finished_at=None,
        )
        self._session.add(ingestion)
        self._session.flush()
        return _to_snapshot(ingestion)

    def get_by_uuid(self, ingestion_uuid: UUID) -> MailIngestionSnapshot | None:
        """Return an ingestion by its internal primary key."""
        validate_uuid(ingestion_uuid, "ingestion_uuid")
        ingestion = self._session.get(MailIngestion, ingestion_uuid)
        return _to_snapshot(ingestion) if ingestion is not None else None

    def get_by_source(
        self,
        mailbox_id: UUID,
        message_id: str,
    ) -> MailIngestionSnapshot | None:
        """Return the unique ingestion for one immutable Graph message."""
        validate_uuid(mailbox_id, "mailbox_id")
        message_id = _validate_graph_identifier(message_id, "message_id")
        ingestion = self._session.scalars(
            select(MailIngestion)
            .where(
                MailIngestion.mailbox_id == mailbox_id,
                MailIngestion.message_id == message_id,
            )
            .limit(1)
        ).first()
        return _to_snapshot(ingestion) if ingestion is not None else None

    def get_by_job_uuid(self, job_uuid: UUID) -> MailIngestionSnapshot | None:
        """Return the optional ingestion linked to one application job."""
        validate_uuid(job_uuid, "job_uuid")
        ingestion = self._session.scalars(
            select(MailIngestion).where(MailIngestion.job_uuid == job_uuid).limit(1)
        ).first()
        return _to_snapshot(ingestion) if ingestion is not None else None

    def delete_by_uuid(self, ingestion_uuid: UUID) -> None:
        """Delete one locked ingestion without touching related models."""
        validate_uuid(ingestion_uuid, "ingestion_uuid")
        ingestion = self._session.get(
            MailIngestion,
            ingestion_uuid,
            with_for_update=True,
        )
        if ingestion is None:
            raise MailIngestionNotFoundError(
                f"mail ingestion {ingestion_uuid} does not exist"
            )
        self._session.delete(ingestion)
        self._session.flush()

    def clear_originating_phone_number_ciphertext(
        self,
        ingestion_uuid: UUID,
        *,
        now: datetime,
    ) -> MailIngestionSnapshot:
        """Erase protected caller metadata after confirming its GLPI ticket."""
        validate_uuid(ingestion_uuid, "ingestion_uuid")
        now = validate_utc_datetime(now, "now")
        ingestion = self._session.get(
            MailIngestion,
            ingestion_uuid,
            with_for_update=True,
        )
        if ingestion is None:
            raise MailIngestionNotFoundError(
                f"mail ingestion {ingestion_uuid} does not exist"
            )
        if (
            ingestion.job_uuid != ingestion_uuid
            or ingestion.disposition != MailIngestionDisposition.ACCEPTED.value
        ):
            raise InvalidMailIngestionTransitionError(
                "protected caller metadata requires an accepted linked job"
            )
        if ingestion.originating_phone_number_ciphertext is not None:
            ingestion.originating_phone_number_ciphertext = None
            ingestion.updated_at = now
            self._session.flush()
        return _to_snapshot(ingestion)

    def resume_or_claim_next(
        self,
        now: datetime,
    ) -> MailIngestionAcquisition | None:
        """Resume processing, otherwise claim a due retry then pending mail."""
        now = validate_utc_datetime(now, "now")
        ingestion = self._select_processing()
        if ingestion is not None:
            previous_status = MailIngestionStatus.PROCESSING
            resumed = True
        else:
            ingestion = self._select_due_retry(now)
            if ingestion is None:
                if self._has_scheduled_system_retry():
                    return None
                ingestion = self._select_pending()
            if ingestion is None:
                return None
            previous_status = MailIngestionStatus(ingestion.status)
            resumed = False

        if previous_status is MailIngestionStatus.PENDING:
            ingestion.stage = MailIngestionStage.VALIDATION.value
        ingestion.status = MailIngestionStatus.PROCESSING.value
        ingestion.attempt_count += 1
        if ingestion.first_started_at is None:
            ingestion.first_started_at = now
        ingestion.next_attempt_at = None
        _clear_last_error(ingestion)
        ingestion.finished_at = None
        ingestion.updated_at = now
        self._session.flush()
        return MailIngestionAcquisition(
            ingestion=_to_snapshot(ingestion),
            previous_status=previous_status,
            resumed=resumed,
        )

    def get_next_system_retry_at(self) -> datetime | None:
        """Return the earliest systemic retry deadline that gates new work."""
        statement = (
            select(MailIngestion.next_attempt_at)
            .where(
                MailIngestion.status == MailIngestionStatus.RETRY_SCHEDULED.value,
                MailIngestion.last_error_scope == MailIngestionErrorScope.SYSTEM.value,
                MailIngestion.next_attempt_at.is_not(None),
            )
            .order_by(MailIngestion.next_attempt_at)
            .limit(1)
        )
        return self._session.scalar(statement)

    def list_replay_candidates(
        self,
        replay_filter: MailIngestionReplayFilter,
    ) -> tuple[MailIngestionSnapshot, ...]:
        """Return one deterministic, read-only batch matching operator filters."""
        replay_filter = _validate_replay_filter(replay_filter)
        statement = select(MailIngestion).where(
            MailIngestion.status == replay_filter.status.value,
            MailIngestion.disposition == replay_filter.disposition.value,
            MailIngestion.rejection_category == replay_filter.rejection_category,
            MailIngestion.rejection_code == replay_filter.rejection_code,
            MailIngestion.received_at >= replay_filter.received_after,
            MailIngestion.received_at < replay_filter.received_before,
            MailIngestion.job_uuid.is_(None),
        )
        if replay_filter.attachment_id_is_not_null:
            statement = statement.where(MailIngestion.attachment_id.is_not(None))
        statement = statement.order_by(
            MailIngestion.received_at,
            MailIngestion.uuid,
        ).limit(replay_filter.limit)
        return tuple(
            _to_snapshot(ingestion)
            for ingestion in self._session.scalars(statement).all()
        )

    def rearm_for_operator_replay(
        self,
        ingestion_uuid: UUID,
        *,
        expected: MailIngestionReplayFilter,
        now: datetime,
    ) -> MailIngestionSnapshot:
        """Return one rejected ingestion to pending after locked revalidation."""
        validate_uuid(ingestion_uuid, "ingestion_uuid")
        expected = _validate_replay_filter(expected)
        now = validate_utc_datetime(now, "now")
        ingestion = self._session.get(
            MailIngestion,
            ingestion_uuid,
            with_for_update=True,
        )
        if ingestion is None:
            raise MailIngestionNotFoundError(
                f"mail ingestion {ingestion_uuid} does not exist"
            )
        if not _matches_replay_filter(ingestion, expected):
            raise InvalidMailIngestionTransitionError(
                "mail ingestion no longer matches the replay filter"
            )

        ingestion.attachment_id = None
        ingestion.job_uuid = None
        ingestion.status = MailIngestionStatus.PENDING.value
        ingestion.stage = MailIngestionStage.DISCOVERY.value
        ingestion.disposition = None
        ingestion.audio_extension = None
        ingestion.attachment_size_bytes = None
        ingestion.consecutive_retry_count = 0
        ingestion.next_attempt_at = None
        ingestion.rejection_category = None
        ingestion.rejection_code = None
        _clear_last_error(ingestion)
        ingestion.updated_at = now
        ingestion.finished_at = None
        self._session.flush()
        return _to_snapshot(ingestion)

    def record_attachment_validated(
        self,
        ingestion_uuid: UUID,
        *,
        attachment_id: str,
        audio_extension: AudioExtension,
        attachment_size_bytes: int,
        now: datetime,
    ) -> MailIngestionSnapshot:
        """Persist validated attachment metadata and advance to publication."""
        attachment_id = _validate_graph_identifier(attachment_id, "attachment_id")
        if not isinstance(audio_extension, AudioExtension):
            raise TypeError("audio_extension must be an AudioExtension")
        attachment_size_bytes = validate_positive_integer(
            attachment_size_bytes,
            "attachment_size_bytes",
        )
        now = validate_utc_datetime(now, "now")
        ingestion = self._lock_processing_ingestion(
            ingestion_uuid,
            MailIngestionStage.VALIDATION,
        )
        ingestion.attachment_id = attachment_id
        ingestion.audio_extension = audio_extension.value
        ingestion.attachment_size_bytes = attachment_size_bytes
        ingestion.stage = MailIngestionStage.PUBLICATION.value
        ingestion.consecutive_retry_count = 0
        ingestion.updated_at = now
        self._session.flush()
        return _to_snapshot(ingestion)

    def record_accepted(
        self,
        ingestion_uuid: UUID,
        *,
        job_uuid: UUID,
        now: datetime,
    ) -> MailIngestionSnapshot:
        """Link an application-created job and prepare Exchange acknowledgement."""
        validate_uuid(job_uuid, "job_uuid")
        now = validate_utc_datetime(now, "now")
        ingestion = self._lock_processing_ingestion(
            ingestion_uuid,
            MailIngestionStage.PUBLICATION,
        )
        if (
            ingestion.attachment_id is None
            or ingestion.audio_extension is None
            or ingestion.attachment_size_bytes is None
            or ingestion.attachment_size_bytes <= 0
        ):
            raise InvalidMailIngestionTransitionError(
                "acceptance requires a validated attachment"
            )
        ingestion.job_uuid = job_uuid
        ingestion.disposition = MailIngestionDisposition.ACCEPTED.value
        ingestion.stage = MailIngestionStage.ACKNOWLEDGEMENT.value
        ingestion.consecutive_retry_count = 0
        ingestion.updated_at = now
        self._session.flush()
        return _to_snapshot(ingestion)

    def record_rejected(
        self,
        ingestion_uuid: UUID,
        *,
        rejection_category: str,
        rejection_code: str,
        now: datetime,
        attachment_id: str | None = None,
        attachment_size_bytes: int | None = None,
    ) -> MailIngestionSnapshot:
        """Persist a local rejection before moving the message to Rejected."""
        rejection_category = validate_safe_identifier(
            rejection_category,
            "rejection_category",
        )
        rejection_code = validate_safe_identifier(rejection_code, "rejection_code")
        if attachment_id is not None:
            attachment_id = _validate_graph_identifier(
                attachment_id,
                "attachment_id",
            )
        if attachment_size_bytes is not None:
            attachment_size_bytes = _validate_non_negative_integer(
                attachment_size_bytes,
                "attachment_size_bytes",
            )
        now = validate_utc_datetime(now, "now")
        ingestion = self._lock_processing_ingestion_at_stages(
            ingestion_uuid,
            (
                MailIngestionStage.VALIDATION,
                MailIngestionStage.PUBLICATION,
            ),
        )
        if attachment_id is not None:
            ingestion.attachment_id = attachment_id
        if attachment_size_bytes is not None:
            ingestion.attachment_size_bytes = attachment_size_bytes
        ingestion.job_uuid = None
        ingestion.disposition = MailIngestionDisposition.REJECTED.value
        ingestion.rejection_category = rejection_category
        ingestion.rejection_code = rejection_code
        ingestion.originating_phone_number_ciphertext = None
        ingestion.stage = MailIngestionStage.ACKNOWLEDGEMENT.value
        ingestion.consecutive_retry_count = 0
        ingestion.updated_at = now
        self._session.flush()
        return _to_snapshot(ingestion)

    def schedule_retry(
        self,
        ingestion_uuid: UUID,
        *,
        stage: MailIngestionStage,
        error_scope: MailIngestionErrorScope,
        error_category: str,
        error_code: str,
        next_attempt_at: datetime,
        now: datetime,
    ) -> MailIngestionSnapshot:
        """Release a processing ingestion into a persisted retry schedule."""
        stage = _validate_stage(stage)
        error_scope = _validate_error_scope(error_scope)
        error_category = validate_safe_identifier(error_category, "error_category")
        error_code = validate_safe_identifier(error_code, "error_code")
        now = validate_utc_datetime(now, "now")
        next_attempt_at = validate_utc_datetime(next_attempt_at, "next_attempt_at")
        if next_attempt_at <= now:
            raise ValueError("next_attempt_at must be later than now")

        ingestion = self._lock_processing_ingestion(ingestion_uuid, stage)
        ingestion.status = MailIngestionStatus.RETRY_SCHEDULED.value
        ingestion.next_attempt_at = next_attempt_at
        ingestion.consecutive_retry_count += 1
        ingestion.last_error_scope = error_scope.value
        ingestion.last_error_category = error_category
        ingestion.last_error_code = error_code
        ingestion.updated_at = now
        self._session.flush()
        return _to_snapshot(ingestion)

    def record_completed(
        self,
        ingestion_uuid: UUID,
        *,
        now: datetime,
    ) -> MailIngestionSnapshot:
        """Finish an accepted ingestion after its message has been moved."""
        now = validate_utc_datetime(now, "now")
        ingestion = self._lock_processing_ingestion(
            ingestion_uuid,
            MailIngestionStage.ACKNOWLEDGEMENT,
        )
        if ingestion.disposition != MailIngestionDisposition.ACCEPTED.value:
            raise InvalidMailIngestionTransitionError(
                "completion requires an accepted ingestion"
            )
        _apply_terminal_state(
            ingestion,
            status=MailIngestionStatus.COMPLETED,
            now=now,
        )
        self._session.flush()
        return _to_snapshot(ingestion)

    def record_failed(
        self,
        ingestion_uuid: UUID,
        *,
        now: datetime,
    ) -> MailIngestionSnapshot:
        """Finish a rejected ingestion after its message has been moved."""
        now = validate_utc_datetime(now, "now")
        ingestion = self._lock_processing_ingestion(
            ingestion_uuid,
            MailIngestionStage.ACKNOWLEDGEMENT,
        )
        if ingestion.disposition != MailIngestionDisposition.REJECTED.value:
            raise InvalidMailIngestionTransitionError(
                "failure requires a rejected ingestion"
            )
        _apply_terminal_state(
            ingestion,
            status=MailIngestionStatus.FAILED,
            now=now,
        )
        self._session.flush()
        return _to_snapshot(ingestion)

    def record_blocked(
        self,
        ingestion_uuid: UUID,
        *,
        stage: MailIngestionStage,
        error_scope: MailIngestionErrorScope,
        error_category: str,
        error_code: str,
        now: datetime,
    ) -> MailIngestionSnapshot:
        """Persist an ingestion whose safe automatic continuation is ambiguous."""
        stage = _validate_stage(stage)
        error_scope = _validate_error_scope(error_scope)
        error_category = validate_safe_identifier(error_category, "error_category")
        error_code = validate_safe_identifier(error_code, "error_code")
        now = validate_utc_datetime(now, "now")
        ingestion = self._lock_processing_ingestion(ingestion_uuid, stage)
        _apply_terminal_state(
            ingestion,
            status=MailIngestionStatus.BLOCKED,
            now=now,
            error_scope=error_scope,
            error_category=error_category,
            error_code=error_code,
        )
        self._session.flush()
        return _to_snapshot(ingestion)

    def _select_processing(self) -> MailIngestion | None:
        statement = (
            select(MailIngestion)
            .where(MailIngestion.status == MailIngestionStatus.PROCESSING.value)
            .order_by(MailIngestion.created_at, MailIngestion.uuid)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return self._session.scalars(statement).first()

    def _select_due_retry(self, now: datetime) -> MailIngestion | None:
        statement = (
            select(MailIngestion)
            .where(
                MailIngestion.status == MailIngestionStatus.RETRY_SCHEDULED.value,
                MailIngestion.next_attempt_at <= now,
            )
            .order_by(
                MailIngestion.next_attempt_at,
                MailIngestion.received_at,
                MailIngestion.uuid,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return self._session.scalars(statement).first()

    def _select_pending(self) -> MailIngestion | None:
        statement = (
            select(MailIngestion)
            .where(MailIngestion.status == MailIngestionStatus.PENDING.value)
            .order_by(MailIngestion.received_at, MailIngestion.uuid)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return self._session.scalars(statement).first()

    def _has_scheduled_system_retry(self) -> bool:
        statement = (
            select(MailIngestion.uuid)
            .where(
                MailIngestion.status == MailIngestionStatus.RETRY_SCHEDULED.value,
                MailIngestion.last_error_scope == MailIngestionErrorScope.SYSTEM.value,
            )
            .limit(1)
        )
        return self._session.scalar(statement) is not None

    def _lock_processing_ingestion(
        self,
        ingestion_uuid: UUID,
        expected_stage: MailIngestionStage,
    ) -> MailIngestion:
        return self._lock_processing_ingestion_at_stages(
            ingestion_uuid,
            (expected_stage,),
        )

    def _lock_processing_ingestion_at_stages(
        self,
        ingestion_uuid: UUID,
        expected_stages: tuple[MailIngestionStage, ...],
    ) -> MailIngestion:
        validate_uuid(ingestion_uuid, "ingestion_uuid")
        if not expected_stages or any(
            not isinstance(stage, MailIngestionStage) for stage in expected_stages
        ):
            raise TypeError("expected_stages must contain MailIngestionStage values")
        ingestion = self._session.get(
            MailIngestion,
            ingestion_uuid,
            with_for_update=True,
        )
        if ingestion is None:
            raise MailIngestionNotFoundError(
                f"mail ingestion {ingestion_uuid} does not exist"
            )
        if (
            ingestion.status != MailIngestionStatus.PROCESSING.value
            or ingestion.stage not in {stage.value for stage in expected_stages}
        ):
            raise InvalidMailIngestionTransitionError(
                "mail ingestion is not processing at the expected stage"
            )
        return ingestion


def _validate_discovered_mail(discovered_mail: DiscoveredMail) -> None:
    if not isinstance(discovered_mail, DiscoveredMail):
        raise TypeError("discovered_mail must be a DiscoveredMail")
    validate_uuid(discovered_mail.uuid, "discovered_mail.uuid")
    validate_uuid(discovered_mail.mailbox_id, "discovered_mail.mailbox_id")
    _validate_graph_identifier(discovered_mail.message_id, "message_id")
    _validate_originating_phone_number_ciphertext(
        discovered_mail.originating_phone_number_ciphertext
    )
    validate_utc_datetime(discovered_mail.received_at, "received_at")


def _validate_originating_phone_number_ciphertext(
    ciphertext: bytes | None,
) -> bytes | None:
    if ciphertext is None:
        return None
    if not isinstance(ciphertext, bytes):
        raise TypeError("originating_phone_number_ciphertext must be bytes or None")
    if not 1 <= len(ciphertext) <= ORIGINATING_PHONE_NUMBER_CIPHERTEXT_MAX_BYTES:
        raise ValueError(
            "originating_phone_number_ciphertext must be non-empty and bounded"
        )
    return ciphertext


def _validate_replay_filter(
    replay_filter: MailIngestionReplayFilter,
) -> MailIngestionReplayFilter:
    if not isinstance(replay_filter, MailIngestionReplayFilter):
        raise TypeError("replay_filter must be a MailIngestionReplayFilter")
    if replay_filter.status is not MailIngestionStatus.FAILED:
        raise ValueError("replay status must be failed")
    if replay_filter.disposition is not MailIngestionDisposition.REJECTED:
        raise ValueError("replay disposition must be rejected")
    validate_safe_identifier(
        replay_filter.rejection_category,
        "rejection_category",
    )
    validate_safe_identifier(replay_filter.rejection_code, "rejection_code")
    received_after = validate_utc_datetime(
        replay_filter.received_after,
        "received_after",
    )
    received_before = validate_utc_datetime(
        replay_filter.received_before,
        "received_before",
    )
    if received_before <= received_after:
        raise ValueError("received_before must be later than received_after")
    if isinstance(replay_filter.limit, bool) or not isinstance(
        replay_filter.limit, int
    ):
        raise TypeError("limit must be an integer")
    if not 1 <= replay_filter.limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    if replay_filter.job_uuid_is_null is not True:
        raise ValueError("job_uuid_is_null must be true for a safe replay")
    if not isinstance(replay_filter.attachment_id_is_not_null, bool):
        raise TypeError("attachment_id_is_not_null must be a boolean")
    return replay_filter


def _matches_replay_filter(
    ingestion: MailIngestion,
    replay_filter: MailIngestionReplayFilter,
) -> bool:
    return bool(
        ingestion.status == replay_filter.status.value
        and ingestion.disposition == replay_filter.disposition.value
        and ingestion.rejection_category == replay_filter.rejection_category
        and ingestion.rejection_code == replay_filter.rejection_code
        and ingestion.job_uuid is None
        and ingestion.received_at >= replay_filter.received_after
        and ingestion.received_at < replay_filter.received_before
        and (
            not replay_filter.attachment_id_is_not_null
            or ingestion.attachment_id is not None
        )
    )


def _validate_graph_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if (
        not value
        or value != value.strip()
        or len(value) > _MAX_GRAPH_IDENTIFIER_LENGTH
        or any(not character.isprintable() for character in value)
    ):
        raise ValueError(f"{field_name} must be a bounded printable identifier")
    return value


def _validate_non_negative_integer(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must not be negative")
    return value


def _validate_stage(stage: MailIngestionStage) -> MailIngestionStage:
    if not isinstance(stage, MailIngestionStage):
        raise TypeError("stage must be a MailIngestionStage")
    return stage


def _validate_error_scope(
    error_scope: MailIngestionErrorScope,
) -> MailIngestionErrorScope:
    if not isinstance(error_scope, MailIngestionErrorScope):
        raise TypeError("error_scope must be a MailIngestionErrorScope")
    return error_scope


def _clear_last_error(ingestion: MailIngestion) -> None:
    ingestion.last_error_scope = None
    ingestion.last_error_category = None
    ingestion.last_error_code = None


def _apply_terminal_state(
    ingestion: MailIngestion,
    *,
    status: MailIngestionStatus,
    now: datetime,
    error_scope: MailIngestionErrorScope | None = None,
    error_category: str | None = None,
    error_code: str | None = None,
) -> None:
    ingestion.status = status.value
    ingestion.next_attempt_at = None
    ingestion.last_error_scope = error_scope.value if error_scope is not None else None
    ingestion.last_error_category = error_category
    ingestion.last_error_code = error_code
    ingestion.finished_at = now
    ingestion.updated_at = now


def _to_snapshot(ingestion: MailIngestion) -> MailIngestionSnapshot:
    return MailIngestionSnapshot(
        uuid=ingestion.uuid,
        mailbox_id=ingestion.mailbox_id,
        message_id=ingestion.message_id,
        attachment_id=ingestion.attachment_id,
        job_uuid=ingestion.job_uuid,
        status=MailIngestionStatus(ingestion.status),
        stage=MailIngestionStage(ingestion.stage),
        disposition=(
            MailIngestionDisposition(ingestion.disposition)
            if ingestion.disposition is not None
            else None
        ),
        audio_extension=(
            AudioExtension(ingestion.audio_extension)
            if ingestion.audio_extension is not None
            else None
        ),
        attachment_size_bytes=ingestion.attachment_size_bytes,
        attempt_count=ingestion.attempt_count,
        consecutive_retry_count=ingestion.consecutive_retry_count,
        next_attempt_at=ingestion.next_attempt_at,
        rejection_category=ingestion.rejection_category,
        rejection_code=ingestion.rejection_code,
        last_error_scope=(
            MailIngestionErrorScope(ingestion.last_error_scope)
            if ingestion.last_error_scope is not None
            else None
        ),
        last_error_category=ingestion.last_error_category,
        last_error_code=ingestion.last_error_code,
        received_at=ingestion.received_at,
        created_at=ingestion.created_at,
        first_started_at=ingestion.first_started_at,
        updated_at=ingestion.updated_at,
        finished_at=ingestion.finished_at,
        originating_phone_number_ciphertext=(
            bytes(ingestion.originating_phone_number_ciphertext)
            if ingestion.originating_phone_number_ciphertext is not None
            else None
        ),
    )


__all__ = [
    "DiscoveredMail",
    "InvalidMailIngestionTransitionError",
    "MailIngestionAcquisition",
    "MailIngestionNotFoundError",
    "MailIngestionRepository",
    "MailIngestionReplayFilter",
    "MailIngestionSnapshot",
]
