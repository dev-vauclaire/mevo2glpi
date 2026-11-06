"""Application-owned transactions for the durable mail-ingestion lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TypeVar
from uuid import UUID, uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from mevo2glpi_common import OriginatingPhoneNumberCipher
from mevo2glpi_database import (
    AudioExtension,
    DiscoveredMail,
    JobEventActor,
    JobEventType,
    MailIngestionAcquisition,
    MailIngestionDisposition,
    MailIngestionErrorScope,
    MailIngestionEventRepository,
    MailIngestionEventType,
    MailIngestionRepository,
    MailIngestionSnapshot,
    MailIngestionStage,
    MailIngestionStatus,
    PendingJob,
    TicketCreationJobEventRepository,
    TicketCreationJobRepository,
)

from ..graph import GraphAttachment, GraphMessage
from .originating_phone_number import extract_originating_phone_number
from .outcomes import PublisherFailure

_ResultT = TypeVar("_ResultT")


class PublisherPersistenceConflictError(RuntimeError):
    """Report persisted data that cannot belong to the current ingestion."""


class PublisherLifecycleService:
    """Coordinate model-specific repositories in short SQL transactions."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        originating_phone_number_cipher: OriginatingPhoneNumberCipher,
        ownership_guard: Callable[[], None] | None = None,
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        if not isinstance(
            originating_phone_number_cipher,
            OriginatingPhoneNumberCipher,
        ):
            raise TypeError(
                "originating_phone_number_cipher must be an "
                "OriginatingPhoneNumberCipher"
            )
        if ownership_guard is not None and not callable(ownership_guard):
            raise TypeError("ownership_guard must be callable")
        if not callable(uuid_factory):
            raise TypeError("uuid_factory must be callable")
        self._session_factory = session_factory
        self._originating_phone_number_cipher = originating_phone_number_cipher
        self._ownership_guard = ownership_guard or _no_op
        self._uuid_factory = uuid_factory

    def discover_messages(
        self,
        *,
        mailbox_id: UUID,
        messages: tuple[GraphMessage, ...],
        now: datetime,
    ) -> int:
        """Persist every unseen immutable message and its discovery event."""
        if not isinstance(mailbox_id, UUID):
            raise TypeError("mailbox_id must be a UUID")
        if not isinstance(messages, tuple) or any(
            not isinstance(message, GraphMessage) for message in messages
        ):
            raise TypeError("messages must be a tuple of GraphMessage values")

        def operation(session: Session) -> int:
            ingestion_repository = MailIngestionRepository(session)
            event_repository = MailIngestionEventRepository(session)
            discovered_count = 0
            for message in messages:
                if (
                    ingestion_repository.get_by_source(
                        mailbox_id,
                        str(message.message_id),
                    )
                    is not None
                ):
                    continue
                ingestion_uuid = self._uuid_factory()
                if not isinstance(ingestion_uuid, UUID):
                    raise TypeError("uuid_factory must return a UUID")
                originating_phone_number = extract_originating_phone_number(
                    message.subject
                )
                originating_phone_number_ciphertext = (
                    None
                    if originating_phone_number is None
                    else self._originating_phone_number_cipher.encrypt(
                        originating_phone_number,
                        ingestion_uuid,
                    )
                )
                snapshot = ingestion_repository.add_discovered(
                    DiscoveredMail(
                        uuid=ingestion_uuid,
                        mailbox_id=mailbox_id,
                        message_id=str(message.message_id),
                        received_at=message.received_at,
                        originating_phone_number_ciphertext=(
                            originating_phone_number_ciphertext
                        ),
                    ),
                    now=now,
                )
                self._append_ingestion_event(
                    event_repository,
                    snapshot,
                    event_type=MailIngestionEventType.MESSAGE_DISCOVERED,
                    occurred_at=now,
                )
                discovered_count += 1
            return discovered_count

        return self._transaction(operation)

    def resume_or_claim_next(
        self,
        now: datetime,
    ) -> MailIngestionAcquisition | None:
        """Claim or resume one ingestion with its audit event atomically."""

        def operation(session: Session) -> MailIngestionAcquisition | None:
            acquisition = MailIngestionRepository(session).resume_or_claim_next(now)
            if acquisition is None:
                return None
            self._append_ingestion_event(
                MailIngestionEventRepository(session),
                acquisition.ingestion,
                event_type=(
                    MailIngestionEventType.PROCESSING_RESUMED
                    if acquisition.resumed
                    else MailIngestionEventType.PROCESSING_STARTED
                ),
                occurred_at=now,
            )
            return acquisition

        return self._transaction(operation)

    def get_next_system_retry_at(self) -> datetime | None:
        """Read the durable dependency backoff that suspends Inbox discovery."""
        return self._transaction(
            lambda session: MailIngestionRepository(session).get_next_system_retry_at()
        )

    def record_attachment_validated(
        self,
        ingestion_uuid: UUID,
        attachment: GraphAttachment,
        audio_extension: AudioExtension,
        now: datetime,
    ) -> MailIngestionSnapshot:
        """Advance a validated audio attachment and append its event."""
        if not isinstance(attachment, GraphAttachment):
            raise TypeError("attachment must be a GraphAttachment")
        if not isinstance(audio_extension, AudioExtension):
            raise TypeError("audio_extension must be an AudioExtension")

        def operation(session: Session) -> MailIngestionSnapshot:
            snapshot = MailIngestionRepository(session).record_attachment_validated(
                ingestion_uuid,
                attachment_id=str(attachment.attachment_id),
                audio_extension=audio_extension,
                attachment_size_bytes=attachment.size_bytes,
                now=now,
            )
            self._append_ingestion_event(
                MailIngestionEventRepository(session),
                snapshot,
                event_type=MailIngestionEventType.ATTACHMENT_VALIDATED,
                occurred_at=now,
            )
            return snapshot

        return self._transaction(operation)

    def record_rejected(
        self,
        ingestion_uuid: UUID,
        *,
        failure: PublisherFailure,
        now: datetime,
        attachment: GraphAttachment | None = None,
        actual_size_bytes: int | None = None,
    ) -> MailIngestionSnapshot:
        """Persist a content rejection before any Exchange acknowledgement."""
        if not isinstance(failure, PublisherFailure):
            raise TypeError("failure must be a PublisherFailure")
        if failure.scope is not MailIngestionErrorScope.MESSAGE:
            raise ValueError("rejection failure must be scoped to one message")
        if attachment is not None and not isinstance(attachment, GraphAttachment):
            raise TypeError("attachment must be a GraphAttachment or None")

        def operation(session: Session) -> MailIngestionSnapshot:
            snapshot = MailIngestionRepository(session).record_rejected(
                ingestion_uuid,
                rejection_category=failure.category,
                rejection_code=failure.technical_code,
                attachment_id=(
                    str(attachment.attachment_id) if attachment is not None else None
                ),
                attachment_size_bytes=(
                    actual_size_bytes
                    if actual_size_bytes is not None
                    else (attachment.size_bytes if attachment is not None else None)
                ),
                now=now,
            )
            self._append_ingestion_event(
                MailIngestionEventRepository(session),
                snapshot,
                event_type=MailIngestionEventType.MESSAGE_REJECTED,
                occurred_at=now,
                failure=failure,
            )
            return snapshot

        return self._transaction(operation)

    def record_audio_published(
        self,
        ingestion_uuid: UUID,
        *,
        storage_key: str,
        size_bytes: int,
        now: datetime,
    ) -> MailIngestionSnapshot:
        """Atomically create the processing job and link its ingestion."""

        def operation(session: Session) -> MailIngestionSnapshot:
            ingestion_repository = MailIngestionRepository(session)
            job_repository = TicketCreationJobRepository(session)
            job_event_repository = TicketCreationJobEventRepository(session)
            current = ingestion_repository.get_by_uuid(ingestion_uuid)
            if current is None:
                raise PublisherPersistenceConflictError("ingestion no longer exists")
            if current.audio_extension is None:
                raise PublisherPersistenceConflictError(
                    "ingestion has no persisted audio extension"
                )
            if not (
                current.stage is MailIngestionStage.PUBLICATION
                or (
                    current.stage is MailIngestionStage.ACKNOWLEDGEMENT
                    and current.disposition is MailIngestionDisposition.ACCEPTED
                    and current.job_uuid == ingestion_uuid
                )
            ):
                raise PublisherPersistenceConflictError(
                    "ingestion does not match published job"
                )

            existing_job = job_repository.get_by_uuid(ingestion_uuid)
            if existing_job is None:
                job = job_repository.add_pending(
                    PendingJob(
                        uuid=ingestion_uuid,
                        storage_key=storage_key,
                        audio_extension=current.audio_extension,
                        audio_size_bytes=size_bytes,
                    )
                )
            else:
                if (
                    existing_job.storage_key != storage_key
                    or existing_job.audio_extension is not current.audio_extension
                    or existing_job.audio_size_bytes != size_bytes
                ):
                    raise PublisherPersistenceConflictError(
                        "existing job does not match published audio"
                    )
                job = existing_job

            if not job_event_repository.exists(
                ingestion_uuid,
                event_type=JobEventType.JOB_CREATED,
                actor=JobEventActor.WORKER_PUBLISHER,
            ):
                job_event_repository.append(
                    ingestion_uuid,
                    occurred_at=now,
                    status=job.status,
                    stage=job.stage,
                    event_type=JobEventType.JOB_CREATED,
                    actor=JobEventActor.WORKER_PUBLISHER,
                )

            if current.stage is MailIngestionStage.PUBLICATION:
                snapshot = ingestion_repository.record_accepted(
                    ingestion_uuid,
                    job_uuid=ingestion_uuid,
                    now=now,
                )
                self._append_ingestion_event(
                    MailIngestionEventRepository(session),
                    snapshot,
                    event_type=MailIngestionEventType.AUDIO_PUBLISHED,
                    occurred_at=now,
                )
                return snapshot
            if (
                current.stage is MailIngestionStage.ACKNOWLEDGEMENT
                and current.disposition is MailIngestionDisposition.ACCEPTED
                and current.job_uuid == ingestion_uuid
            ):
                return current
            raise PublisherPersistenceConflictError(
                "ingestion does not match published job"
            )

        try:
            return self._transaction(operation)
        except SQLAlchemyError:
            reconciled = self._read_published_ingestion(
                ingestion_uuid,
                storage_key=storage_key,
                size_bytes=size_bytes,
            )
            if reconciled is not None:
                return reconciled
            raise

    def schedule_retry(
        self,
        ingestion_uuid: UUID,
        *,
        stage: MailIngestionStage,
        failure: PublisherFailure,
        next_attempt_at: datetime,
        now: datetime,
    ) -> MailIngestionSnapshot:
        """Durably release an ingestion until its next retry deadline."""

        def operation(session: Session) -> MailIngestionSnapshot:
            snapshot = MailIngestionRepository(session).schedule_retry(
                ingestion_uuid,
                stage=stage,
                error_scope=failure.scope,
                error_category=failure.category,
                error_code=failure.technical_code,
                next_attempt_at=next_attempt_at,
                now=now,
            )
            self._append_ingestion_event(
                MailIngestionEventRepository(session),
                snapshot,
                event_type=MailIngestionEventType.RETRY_SCHEDULED,
                occurred_at=now,
                failure=failure,
            )
            return snapshot

        return self._transaction(operation)

    def record_blocked(
        self,
        ingestion_uuid: UUID,
        *,
        stage: MailIngestionStage,
        failure: PublisherFailure,
        now: datetime,
    ) -> MailIngestionSnapshot:
        """Persist an unsafe-to-retry message or systemic ambiguity."""

        def operation(session: Session) -> MailIngestionSnapshot:
            snapshot = MailIngestionRepository(session).record_blocked(
                ingestion_uuid,
                stage=stage,
                error_scope=failure.scope,
                error_category=failure.category,
                error_code=failure.technical_code,
                now=now,
            )
            self._append_ingestion_event(
                MailIngestionEventRepository(session),
                snapshot,
                event_type=MailIngestionEventType.INGESTION_BLOCKED,
                occurred_at=now,
                failure=failure,
            )
            return snapshot

        return self._transaction(operation)

    def record_message_moved(
        self,
        ingestion_uuid: UUID,
        *,
        now: datetime,
    ) -> MailIngestionSnapshot:
        """Finalize accepted/rejected ingestion only after Graph confirms move."""

        def operation(session: Session) -> MailIngestionSnapshot:
            repository = MailIngestionRepository(session)
            current = repository.get_by_uuid(ingestion_uuid)
            if current is None:
                raise PublisherPersistenceConflictError("ingestion no longer exists")
            if (
                current.status is not MailIngestionStatus.PROCESSING
                or current.stage is not MailIngestionStage.ACKNOWLEDGEMENT
                or current.disposition is None
            ):
                raise PublisherPersistenceConflictError(
                    "ingestion is not awaiting acknowledgement"
                )
            events = MailIngestionEventRepository(session)
            self._append_ingestion_event(
                events,
                current,
                event_type=MailIngestionEventType.MESSAGE_MOVED,
                occurred_at=now,
            )
            if current.disposition is MailIngestionDisposition.ACCEPTED:
                snapshot = repository.record_completed(ingestion_uuid, now=now)
                terminal_event = MailIngestionEventType.INGESTION_COMPLETED
            else:
                snapshot = repository.record_failed(ingestion_uuid, now=now)
                terminal_event = MailIngestionEventType.INGESTION_FAILED
            self._append_ingestion_event(
                events,
                snapshot,
                event_type=terminal_event,
                occurred_at=now,
            )
            return snapshot

        return self._transaction(operation)

    def _read_published_ingestion(
        self,
        ingestion_uuid: UUID,
        *,
        storage_key: str,
        size_bytes: int,
    ) -> MailIngestionSnapshot | None:
        self._ownership_guard()
        session = self._session_factory()
        try:
            ingestion = MailIngestionRepository(session).get_by_uuid(ingestion_uuid)
            job = TicketCreationJobRepository(session).get_by_uuid(ingestion_uuid)
            event_exists = TicketCreationJobEventRepository(session).exists(
                ingestion_uuid,
                event_type=JobEventType.JOB_CREATED,
                actor=JobEventActor.WORKER_PUBLISHER,
            )
            if (
                ingestion is None
                or job is None
                or not event_exists
                or ingestion.stage is not MailIngestionStage.ACKNOWLEDGEMENT
                or ingestion.disposition is not MailIngestionDisposition.ACCEPTED
                or ingestion.job_uuid != ingestion_uuid
                or ingestion.audio_extension is None
                or job.storage_key != storage_key
                or job.audio_extension is not ingestion.audio_extension
                or job.audio_size_bytes != size_bytes
            ):
                return None
            return ingestion
        finally:
            session.close()

    @staticmethod
    def _append_ingestion_event(
        repository: MailIngestionEventRepository,
        snapshot: MailIngestionSnapshot,
        *,
        event_type: MailIngestionEventType,
        occurred_at: datetime,
        failure: PublisherFailure | None = None,
    ) -> None:
        repository.append(
            snapshot.uuid,
            occurred_at=occurred_at,
            status=snapshot.status,
            stage=snapshot.stage,
            disposition=snapshot.disposition,
            event_type=event_type,
            error_scope=failure.scope if failure is not None else None,
            error_category=failure.category if failure is not None else None,
            technical_code=failure.technical_code if failure is not None else None,
        )

    def _transaction(self, operation: Callable[[Session], _ResultT]) -> _ResultT:
        self._ownership_guard()
        session = self._session_factory()
        try:
            with session.begin():
                result = operation(session)
        finally:
            session.close()
        self._ownership_guard()
        return result


def _no_op() -> None:
    return None


__all__ = [
    "PublisherLifecycleService",
    "PublisherPersistenceConflictError",
]
