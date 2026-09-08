"""Readable orchestration from one Exchange message to one pending audio job."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryFile
from typing import TypeVar
from uuid import UUID

from glpi_ticketing_common.audio_files import build_audio_storage_key
from glpi_ticketing_common.backoff import ExponentialBackoffPolicy
from glpi_ticketing_database import (
    MailIngestionDisposition,
    MailIngestionErrorScope,
    MailIngestionSnapshot,
    MailIngestionStage,
)

from ..graph import (
    AttachmentId,
    FolderId,
    GraphClientError,
    GraphErrorCategory,
    GraphMailboxProtocol,
    GraphPayloadTooLargeError,
    MessageId,
)
from .audio import (
    AudioPublicationError,
    classify_audio_io_error,
    inspect_wav_structure,
    publish_or_confirm_audio,
    select_wav_attachment,
)
from .lifecycle import PublisherPersistenceConflictError
from .outcomes import (
    BlockedOutcome,
    CompletedOutcome,
    DiscoveryRetryOutcome,
    IdleOutcome,
    PublisherFailure,
    PublisherRunOutcome,
    RejectedOutcome,
    RetryScheduledOutcome,
    SystemBlockedOutcome,
)
from .ports import PublisherLifecycleProtocol

LOGGER = logging.getLogger(__name__)

PROCESSED_FOLDER_NAME = "Processed"
REJECTED_FOLDER_NAME = "Rejected"

_CallResultT = TypeVar("_CallResultT")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _no_op() -> None:
    return None


def _no_op_ingestion_started(_ingestion_uuid: UUID) -> None:
    return None


class PublisherWorker:
    """Advance one durable ingestion through validation, publication and ack."""

    def __init__(
        self,
        *,
        mailbox_id: UUID,
        graph: GraphMailboxProtocol,
        lifecycle: PublisherLifecycleProtocol,
        audio_root: Path,
        retry_policy: ExponentialBackoffPolicy,
        poll_interval_seconds: float,
        system_probe_interval_seconds: float,
        maximum_attachment_bytes: int,
        ownership_guard: Callable[[], None] = _no_op,
        on_ingestion_started: Callable[[UUID], None] = _no_op_ingestion_started,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not isinstance(mailbox_id, UUID):
            raise TypeError("mailbox_id must be a UUID")
        for method_name in (
            "list_messages",
            "list_attachments",
            "download_attachment",
            "ensure_mail_folder",
            "ensure_message_in_folder",
        ):
            if not callable(getattr(graph, method_name, None)):
                raise TypeError(f"graph must expose {method_name}()")
        for method_name in (
            "resume_or_claim_next",
            "get_next_system_retry_at",
        ):
            if not callable(getattr(lifecycle, method_name, None)):
                raise TypeError(f"lifecycle must expose {method_name}()")
        if not isinstance(audio_root, Path):
            raise TypeError("audio_root must be a Path")
        if not isinstance(retry_policy, ExponentialBackoffPolicy):
            raise TypeError("retry_policy must be an ExponentialBackoffPolicy")
        if (
            isinstance(maximum_attachment_bytes, bool)
            or not isinstance(maximum_attachment_bytes, int)
            or maximum_attachment_bytes <= 0
        ):
            raise ValueError("maximum_attachment_bytes must be a positive integer")
        if not callable(ownership_guard):
            raise TypeError("ownership_guard must be callable")
        if not callable(on_ingestion_started):
            raise TypeError("on_ingestion_started must be callable")
        if not callable(now):
            raise TypeError("now must be callable")

        self._mailbox_id = mailbox_id
        self._graph = graph
        self._lifecycle = lifecycle
        self._audio_root = audio_root
        self._retry_policy = retry_policy
        self._poll_interval_seconds = _positive_seconds(
            poll_interval_seconds,
            "poll_interval_seconds",
        )
        self._system_probe_interval_seconds = _positive_seconds(
            system_probe_interval_seconds,
            "system_probe_interval_seconds",
        )
        self._maximum_attachment_bytes = maximum_attachment_bytes
        self._ownership_guard = ownership_guard
        self._on_ingestion_started = on_ingestion_started
        self._now = now
        self._discovery_retry_count = 0

    def run_once(self) -> PublisherRunOutcome:
        """Resume durable work first, discovering Inbox only when the queue is empty."""
        selection_time = self._current_time()
        acquisition = self._lifecycle.resume_or_claim_next(selection_time)
        if acquisition is None:
            system_retry_at = self._lifecycle.get_next_system_retry_at()
            if system_retry_at is not None:
                wait_seconds = (system_retry_at - selection_time).total_seconds()
                if wait_seconds <= 0:
                    # A deadline cannot be due here with the singleton lock held,
                    # but fail closed instead of scanning Graph if persisted state
                    # and the selection result ever disagree.
                    wait_seconds = self._poll_interval_seconds
                return IdleOutcome(wait_seconds=wait_seconds)
            discovery_outcome = self._discover_inbox()
            if discovery_outcome is not None:
                return discovery_outcome
            acquisition = self._lifecycle.resume_or_claim_next(self._current_time())
        if acquisition is None:
            return IdleOutcome(wait_seconds=self._poll_interval_seconds)

        ingestion = acquisition.ingestion
        self._on_ingestion_started(ingestion.uuid)
        LOGGER.info(
            "mail ingestion started ingestion_uuid=%s stage=%s",
            ingestion.uuid,
            ingestion.stage.value,
        )
        return self._process(ingestion)

    def _discover_inbox(self) -> DiscoveryRetryOutcome | SystemBlockedOutcome | None:
        try:
            messages = self._graph_call(self._graph.list_messages)
        except GraphClientError as error:
            failure = self._graph_failure(error, scope=MailIngestionErrorScope.SYSTEM)
            self._log_failure("mail discovery failed", None, None, failure)
            if error.retryable:
                delay = self._bounded_retry_delay(
                    consecutive_retry_count=self._discovery_retry_count,
                    retry_after_seconds=error.retry_after_seconds,
                )
                self._discovery_retry_count += 1
                return DiscoveryRetryOutcome(failure=failure, wait_seconds=delay)
            return SystemBlockedOutcome(
                failure=failure,
                probe_after_seconds=self._system_probe_interval_seconds,
            )

        self._discovery_retry_count = 0
        discovered_count = self._lifecycle.discover_messages(
            mailbox_id=self._mailbox_id,
            messages=messages,
            now=self._current_time(),
        )
        if discovered_count:
            LOGGER.info(
                "mail discovery completed discovered_count=%s", discovered_count
            )
        return None

    def _process(self, ingestion: MailIngestionSnapshot) -> PublisherRunOutcome:
        if ingestion.stage is MailIngestionStage.VALIDATION:
            validation_outcome = self._validate_attachment(ingestion)
            if not isinstance(validation_outcome, MailIngestionSnapshot):
                return validation_outcome
            ingestion = validation_outcome

        if ingestion.stage is MailIngestionStage.PUBLICATION:
            publication_outcome = self._publish_audio(ingestion)
            if not isinstance(publication_outcome, MailIngestionSnapshot):
                return publication_outcome
            ingestion = publication_outcome

        if ingestion.stage is MailIngestionStage.ACKNOWLEDGEMENT:
            return self._acknowledge(ingestion)
        raise RuntimeError("processing ingestion has an unsupported stage")

    def _validate_attachment(
        self,
        ingestion: MailIngestionSnapshot,
    ) -> MailIngestionSnapshot | PublisherRunOutcome:
        try:
            attachments = self._graph_call(
                lambda: self._graph.list_attachments(MessageId(ingestion.message_id))
            )
        except GraphClientError as error:
            return self._handle_graph_failure(ingestion, error)

        validation = select_wav_attachment(
            attachments,
            maximum_bytes=self._maximum_attachment_bytes,
        )
        if validation.failure is not None:
            rejected = self._lifecycle.record_rejected(
                ingestion.uuid,
                failure=validation.failure,
                attachment=validation.attachment,
                now=self._current_time(),
            )
            return self._acknowledge(rejected)
        assert validation.attachment is not None
        return self._lifecycle.record_attachment_validated(
            ingestion.uuid,
            validation.attachment,
            self._current_time(),
        )

    def _publish_audio(
        self,
        ingestion: MailIngestionSnapshot,
    ) -> MailIngestionSnapshot | PublisherRunOutcome:
        if ingestion.attachment_id is None:
            return self._block_invariant(ingestion, "attachment_state_invalid")

        try:
            with TemporaryFile(mode="w+b") as temporary_audio:
                try:
                    download = self._graph_call(
                        lambda: self._graph.download_attachment(
                            MessageId(ingestion.message_id),
                            AttachmentId(ingestion.attachment_id),
                            temporary_audio,
                        )
                    )
                except GraphPayloadTooLargeError:
                    return self._reject_oversized_audio(ingestion)
                except GraphClientError as error:
                    if error.category is GraphErrorCategory.PAYLOAD_TOO_LARGE:
                        return self._reject_oversized_audio(ingestion)
                    return self._handle_graph_failure(ingestion, error)

                wav_failure = inspect_wav_structure(
                    temporary_audio,
                    size_bytes=download.size_bytes,
                )
                if wav_failure is not None:
                    return self._reject_downloaded_audio(
                        ingestion,
                        wav_failure,
                        actual_size_bytes=download.size_bytes,
                    )

                storage_key = build_audio_storage_key(ingestion.uuid, "wav")
                self._ownership_guard()
                published = publish_or_confirm_audio(
                    self._audio_root,
                    storage_key,
                    temporary_audio,
                    size_bytes=download.size_bytes,
                )
                self._ownership_guard()
        except OSError as error:
            return self._handle_audio_io_failure(
                ingestion,
                classify_audio_io_error(error),
            )
        except AudioPublicationError as error:
            return self._handle_audio_io_failure(ingestion, error)

        try:
            return self._lifecycle.record_audio_published(
                ingestion.uuid,
                storage_key=published.storage_key,
                size_bytes=published.size_bytes,
                now=self._current_time(),
            )
        except PublisherPersistenceConflictError:
            return self._block_invariant(ingestion, "publication_state_conflict")

    def _reject_downloaded_audio(
        self,
        ingestion: MailIngestionSnapshot,
        failure: PublisherFailure,
        *,
        actual_size_bytes: int | None = None,
    ) -> PublisherRunOutcome:
        rejected = self._lifecycle.record_rejected(
            ingestion.uuid,
            failure=failure,
            actual_size_bytes=actual_size_bytes,
            now=self._current_time(),
        )
        return self._acknowledge(rejected)

    def _reject_oversized_audio(
        self,
        ingestion: MailIngestionSnapshot,
    ) -> PublisherRunOutcome:
        return self._reject_downloaded_audio(
            ingestion,
            PublisherFailure(
                scope=MailIngestionErrorScope.MESSAGE,
                category="invalid_audio",
                technical_code="audio_too_large",
            ),
        )

    def _handle_audio_io_failure(
        self,
        ingestion: MailIngestionSnapshot,
        error: AudioPublicationError,
    ) -> PublisherRunOutcome:
        failure = PublisherFailure(
            scope=MailIngestionErrorScope.SYSTEM,
            category="storage",
            technical_code=error.technical_code,
        )
        if error.retryable:
            return self._schedule_retry(ingestion, failure)
        return self._block(ingestion, failure, system=True)

    def _acknowledge(self, ingestion: MailIngestionSnapshot) -> PublisherRunOutcome:
        if ingestion.disposition is None:
            return self._block_invariant(ingestion, "disposition_state_invalid")
        destination_name = (
            PROCESSED_FOLDER_NAME
            if ingestion.disposition is MailIngestionDisposition.ACCEPTED
            else REJECTED_FOLDER_NAME
        )
        try:
            folder = self._graph_call(
                lambda: self._graph.ensure_mail_folder(destination_name)
            )
            self._graph_call(
                lambda: self._graph.ensure_message_in_folder(
                    MessageId(ingestion.message_id),
                    FolderId(str(folder.folder_id)),
                )
            )
        except GraphClientError as error:
            return self._handle_graph_failure(ingestion, error)

        terminal = self._lifecycle.record_message_moved(
            ingestion.uuid,
            now=self._current_time(),
        )
        if terminal.disposition is MailIngestionDisposition.ACCEPTED:
            if terminal.job_uuid is None:
                return self._block_invariant(terminal, "job_state_invalid")
            LOGGER.info(
                "mail ingestion completed ingestion_uuid=%s job_uuid=%s",
                terminal.uuid,
                terminal.job_uuid,
            )
            return CompletedOutcome(
                ingestion_uuid=terminal.uuid,
                job_uuid=terminal.job_uuid,
            )

        failure = PublisherFailure(
            scope=MailIngestionErrorScope.MESSAGE,
            category=terminal.rejection_category or "invalid_message",
            technical_code=terminal.rejection_code or "rejection_state_invalid",
        )
        self._log_failure(
            "mail ingestion rejected",
            terminal.uuid,
            terminal.stage,
            failure,
        )
        return RejectedOutcome(ingestion_uuid=terminal.uuid, failure=failure)

    def _handle_graph_failure(
        self,
        ingestion: MailIngestionSnapshot,
        error: GraphClientError,
    ) -> PublisherRunOutcome:
        if error.retryable:
            return self._schedule_retry(
                ingestion,
                self._graph_failure(error, scope=MailIngestionErrorScope.SYSTEM),
                retry_after_seconds=error.retry_after_seconds,
            )

        scope = (
            MailIngestionErrorScope.MESSAGE
            if error.category
            in {GraphErrorCategory.NOT_FOUND, GraphErrorCategory.CONFLICT}
            else MailIngestionErrorScope.SYSTEM
        )
        failure = self._graph_failure(error, scope=scope)
        return self._block(
            ingestion,
            failure,
            system=scope is MailIngestionErrorScope.SYSTEM,
        )

    def _schedule_retry(
        self,
        ingestion: MailIngestionSnapshot,
        failure: PublisherFailure,
        *,
        retry_after_seconds: float | None = None,
    ) -> RetryScheduledOutcome:
        now = self._current_time()
        delay = self._bounded_retry_delay(
            consecutive_retry_count=ingestion.consecutive_retry_count,
            retry_after_seconds=retry_after_seconds,
        )
        next_attempt_at = now + timedelta(seconds=delay)
        self._lifecycle.schedule_retry(
            ingestion.uuid,
            stage=ingestion.stage,
            failure=failure,
            next_attempt_at=next_attempt_at,
            now=now,
        )
        self._log_failure(
            "mail ingestion retry scheduled",
            ingestion.uuid,
            ingestion.stage,
            failure,
        )
        return RetryScheduledOutcome(
            ingestion_uuid=ingestion.uuid,
            stage=ingestion.stage,
            failure=failure,
            next_attempt_at=next_attempt_at,
        )

    def _block(
        self,
        ingestion: MailIngestionSnapshot,
        failure: PublisherFailure,
        *,
        system: bool,
    ) -> BlockedOutcome | SystemBlockedOutcome:
        self._lifecycle.record_blocked(
            ingestion.uuid,
            stage=ingestion.stage,
            failure=failure,
            now=self._current_time(),
        )
        self._log_failure(
            "mail ingestion blocked",
            ingestion.uuid,
            ingestion.stage,
            failure,
        )
        if system:
            return SystemBlockedOutcome(
                ingestion_uuid=ingestion.uuid,
                stage=ingestion.stage,
                failure=failure,
                probe_after_seconds=self._system_probe_interval_seconds,
            )
        return BlockedOutcome(
            ingestion_uuid=ingestion.uuid,
            stage=ingestion.stage,
            failure=failure,
        )

    def _block_invariant(
        self,
        ingestion: MailIngestionSnapshot,
        code: str,
    ) -> SystemBlockedOutcome:
        failure = PublisherFailure(
            scope=MailIngestionErrorScope.SYSTEM,
            category="invariant",
            technical_code=code,
        )
        return self._block(ingestion, failure, system=True)

    def _graph_call(self, operation: Callable[[], _CallResultT]) -> _CallResultT:
        self._ownership_guard()
        result = operation()
        self._ownership_guard()
        return result

    @staticmethod
    def _graph_failure(
        error: GraphClientError,
        *,
        scope: MailIngestionErrorScope,
    ) -> PublisherFailure:
        return PublisherFailure(
            scope=scope,
            category=f"graph_{error.category.value}",
            technical_code=error.technical_code,
        )

    def _bounded_retry_delay(
        self,
        *,
        consecutive_retry_count: int,
        retry_after_seconds: float | None,
    ) -> float:
        delay = self._retry_policy.delay_seconds(
            consecutive_retry_count=consecutive_retry_count
        )
        if retry_after_seconds is not None and math.isfinite(retry_after_seconds):
            delay = max(delay, retry_after_seconds)
        return min(delay, self._retry_policy.maximum_seconds)

    def _current_time(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime):
            raise TypeError("now must return a datetime")
        if value.utcoffset() is None:
            raise ValueError("now must return a timezone-aware datetime")
        return value

    @staticmethod
    def _log_failure(
        message: str,
        ingestion_uuid: UUID | None,
        stage: MailIngestionStage | None,
        failure: PublisherFailure,
    ) -> None:
        LOGGER.warning(
            "%s ingestion_uuid=%s stage=%s category=%s code=%s",
            message,
            ingestion_uuid,
            stage.value if stage is not None else None,
            failure.category,
            failure.technical_code,
        )


def _positive_seconds(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field_name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{field_name} must be finite and greater than zero")
    return normalized


__all__ = [
    "PROCESSED_FOLDER_NAME",
    "PublisherWorker",
    "REJECTED_FOLDER_NAME",
]
