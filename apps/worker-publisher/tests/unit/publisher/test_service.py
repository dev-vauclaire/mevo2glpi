from __future__ import annotations

import logging
import struct
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

import pytest

from glpi_ticketing_common.backoff import ExponentialBackoffPolicy
from glpi_ticketing_database import (
    AudioExtension,
    MailIngestionAcquisition,
    MailIngestionDisposition,
    MailIngestionErrorScope,
    MailIngestionSnapshot,
    MailIngestionStage,
    MailIngestionStatus,
)
from glpi_ticketing_worker_publisher.graph import (
    AttachmentDownload,
    AttachmentId,
    FolderId,
    GraphAttachment,
    GraphErrorCategory,
    GraphHttpError,
    GraphMailFolder,
    GraphMessage,
    GraphOperation,
    MessageId,
    MessageMoveResult,
    MoveOutcome,
)
from glpi_ticketing_worker_publisher.publisher import (
    CompletedOutcome,
    IdleOutcome,
    RejectedOutcome,
    RetryScheduledOutcome,
    SystemBlockedOutcome,
)
from glpi_ticketing_worker_publisher.publisher.service import PublisherWorker

NOW = datetime(2026, 9, 8, 8, tzinfo=UTC)
MAILBOX_ID = UUID("10000000-0000-4000-8000-000000000001")
OGG_CRC_POLYNOMIAL = 0x04C11DB7
UINT32_MASK = 0xFFFFFFFF


def _wav_bytes() -> bytes:
    audio = b"\xd5" * 80
    fmt = struct.pack("<HHIIHH", 6, 1, 8_000, 8_000, 1, 8)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"data" + struct.pack("<I", len(audio)) + audio
    return b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks


def _ogg_bytes() -> bytes:
    payload = b"Opus"
    header = b"OggS\x00\x06" + (b"\x00" * 20) + b"\x01"
    page = bytearray(header + bytes((len(payload),)) + payload)
    checksum = 0
    for octet in page:
        checksum ^= octet << 24
        for _ in range(8):
            if checksum & 0x80000000:
                checksum = (checksum << 1) ^ OGG_CRC_POLYNOMIAL
            else:
                checksum <<= 1
            checksum &= UINT32_MASK
    page[22:26] = checksum.to_bytes(4, byteorder="little")
    return bytes(page)


def _snapshot(
    *,
    ingestion_uuid: UUID | None = None,
    status: MailIngestionStatus = MailIngestionStatus.PROCESSING,
    stage: MailIngestionStage = MailIngestionStage.VALIDATION,
    disposition: MailIngestionDisposition | None = None,
    attachment_id: str | None = None,
    audio_extension: AudioExtension | None = None,
    attachment_size_bytes: int | None = None,
    job_uuid: UUID | None = None,
    message_id: str = "immutable-message-secret",
    rejection_category: str | None = None,
    rejection_code: str | None = None,
) -> MailIngestionSnapshot:
    identifier = ingestion_uuid or uuid4()
    return MailIngestionSnapshot(
        uuid=identifier,
        mailbox_id=MAILBOX_ID,
        message_id=message_id,
        attachment_id=attachment_id,
        job_uuid=job_uuid,
        status=status,
        stage=stage,
        disposition=disposition,
        audio_extension=audio_extension,
        attachment_size_bytes=attachment_size_bytes,
        attempt_count=1,
        consecutive_retry_count=0,
        next_attempt_at=None,
        rejection_category=rejection_category,
        rejection_code=rejection_code,
        last_error_scope=None,
        last_error_category=None,
        last_error_code=None,
        received_at=NOW,
        created_at=NOW,
        first_started_at=NOW,
        updated_at=NOW,
        finished_at=None,
    )


class FakeGraph:
    def __init__(
        self,
        *,
        content: bytes | None = None,
        attachment_name: str = "voicemail-sensitive-name.wav",
        content_type: str = "audio/wav",
        parent_folder_id: str = "inbox-secret",
        declared_size_bytes: int | None = None,
    ) -> None:
        self.content = content if content is not None else _wav_bytes()
        self.message = GraphMessage(
            message_id=MessageId("immutable-message-secret"),
            parent_folder_id=FolderId(parent_folder_id),
            received_at=NOW,
            has_attachments=True,
        )
        self.attachment = GraphAttachment(
            attachment_id=AttachmentId("attachment-secret"),
            name=attachment_name,
            content_type=content_type,
            size_bytes=(
                len(self.content)
                if declared_size_bytes is None
                else declared_size_bytes
            ),
            is_inline=False,
        )
        self.list_messages_error: Exception | None = None
        self.list_attachments_error: Exception | None = None
        self.download_error: Exception | None = None
        self.destination_names: list[str] = []
        self.move_calls = 0
        self.list_messages_calls = 0

    def list_messages(self) -> tuple[GraphMessage, ...]:
        self.list_messages_calls += 1
        if self.list_messages_error is not None:
            raise self.list_messages_error
        return (self.message,)

    def list_attachments(self, _message_id: MessageId) -> tuple[GraphAttachment, ...]:
        if self.list_attachments_error is not None:
            raise self.list_attachments_error
        return (self.attachment,)

    def download_attachment(
        self,
        _message_id: MessageId,
        _attachment_id: AttachmentId,
        sink: BinaryIO,
    ) -> AttachmentDownload:
        if self.download_error is not None:
            raise self.download_error
        sink.write(self.content)
        return AttachmentDownload(size_bytes=len(self.content))

    def ensure_mail_folder(self, display_name: str) -> GraphMailFolder:
        self.destination_names.append(display_name)
        return GraphMailFolder(
            folder_id=FolderId(f"{display_name.casefold()}-folder"),
            display_name=display_name,
        )

    def ensure_message_in_folder(
        self,
        message_id: MessageId,
        destination_folder_id: FolderId,
    ) -> MessageMoveResult:
        self.move_calls += 1
        return MessageMoveResult(
            message_id=message_id,
            parent_folder_id=destination_folder_id,
            outcome=MoveOutcome.MOVED,
        )


class FakeLifecycle:
    def __init__(
        self,
        *,
        ingestion: MailIngestionSnapshot,
        discover_first: bool = False,
        fail_publication_once: bool = False,
    ) -> None:
        self.ingestion = ingestion
        self.discover_first = discover_first
        self.fail_publication_once = fail_publication_once
        self.claim_calls = 0
        self.discovered = False
        self.published: list[tuple[str, int]] = []
        self.retry_failure = None
        self.block_failure = None
        self.system_retry_at: datetime | None = None

    def resume_or_claim_next(
        self,
        _now: datetime,
    ) -> MailIngestionAcquisition | None:
        self.claim_calls += 1
        if self.discover_first and not self.discovered:
            return None
        if self.ingestion.status is not MailIngestionStatus.PROCESSING:
            return None
        return MailIngestionAcquisition(
            ingestion=self.ingestion,
            previous_status=MailIngestionStatus.PROCESSING,
            resumed=True,
        )

    def discover_messages(
        self,
        *,
        mailbox_id: UUID,
        messages: tuple[GraphMessage, ...],
        now: datetime,
    ) -> int:
        assert mailbox_id == MAILBOX_ID
        assert messages
        assert now == NOW
        self.discovered = True
        return 1

    def get_next_system_retry_at(self) -> datetime | None:
        return self.system_retry_at

    def record_attachment_validated(
        self,
        ingestion_uuid: UUID,
        attachment: GraphAttachment,
        audio_extension: AudioExtension,
        _now: datetime,
    ) -> MailIngestionSnapshot:
        assert ingestion_uuid == self.ingestion.uuid
        self.ingestion = replace(
            self.ingestion,
            stage=MailIngestionStage.PUBLICATION,
            attachment_id=str(attachment.attachment_id),
            audio_extension=audio_extension,
            attachment_size_bytes=attachment.size_bytes,
        )
        return self.ingestion

    def record_rejected(
        self,
        ingestion_uuid: UUID,
        *,
        failure: object,
        now: datetime,
        attachment: GraphAttachment | None = None,
        actual_size_bytes: int | None = None,
    ) -> MailIngestionSnapshot:
        del now, attachment, actual_size_bytes
        assert ingestion_uuid == self.ingestion.uuid
        self.ingestion = replace(
            self.ingestion,
            stage=MailIngestionStage.ACKNOWLEDGEMENT,
            disposition=MailIngestionDisposition.REJECTED,
            rejection_category=failure.category,
            rejection_code=failure.technical_code,
        )
        return self.ingestion

    def record_audio_published(
        self,
        ingestion_uuid: UUID,
        *,
        storage_key: str,
        size_bytes: int,
        now: datetime,
    ) -> MailIngestionSnapshot:
        del now
        self.published.append((storage_key, size_bytes))
        if self.fail_publication_once:
            self.fail_publication_once = False
            raise RuntimeError("synthetic database crash")
        self.ingestion = replace(
            self.ingestion,
            stage=MailIngestionStage.ACKNOWLEDGEMENT,
            disposition=MailIngestionDisposition.ACCEPTED,
            job_uuid=ingestion_uuid,
        )
        return self.ingestion

    def schedule_retry(self, ingestion_uuid: UUID, **kwargs: object) -> object:
        assert ingestion_uuid == self.ingestion.uuid
        self.retry_failure = kwargs["failure"]
        self.ingestion = replace(
            self.ingestion,
            status=MailIngestionStatus.RETRY_SCHEDULED,
        )
        return self.ingestion

    def record_blocked(self, ingestion_uuid: UUID, **kwargs: object) -> object:
        assert ingestion_uuid == self.ingestion.uuid
        self.block_failure = kwargs["failure"]
        self.ingestion = replace(self.ingestion, status=MailIngestionStatus.BLOCKED)
        return self.ingestion

    def record_message_moved(
        self,
        ingestion_uuid: UUID,
        *,
        now: datetime,
    ) -> MailIngestionSnapshot:
        del now
        assert ingestion_uuid == self.ingestion.uuid
        terminal_status = (
            MailIngestionStatus.COMPLETED
            if self.ingestion.disposition is MailIngestionDisposition.ACCEPTED
            else MailIngestionStatus.FAILED
        )
        self.ingestion = replace(self.ingestion, status=terminal_status)
        return self.ingestion


def _worker(
    tmp_path: Path,
    lifecycle: FakeLifecycle,
    graph: FakeGraph,
) -> PublisherWorker:
    return PublisherWorker(
        mailbox_id=MAILBOX_ID,
        graph=graph,
        lifecycle=lifecycle,
        audio_root=tmp_path,
        retry_policy=ExponentialBackoffPolicy(
            initial_seconds=5,
            maximum_seconds=60,
        ),
        poll_interval_seconds=30,
        system_probe_interval_seconds=60,
        maximum_attachment_bytes=25 * 1024 * 1024,
        now=lambda: NOW,
    )


def test_nominal_flow_discovers_publishes_job_then_moves_message(
    tmp_path: Path,
) -> None:
    ingestion = _snapshot()
    lifecycle = FakeLifecycle(ingestion=ingestion, discover_first=True)
    graph = FakeGraph()

    outcome = _worker(tmp_path, lifecycle, graph).run_once()

    assert isinstance(outcome, CompletedOutcome)
    assert outcome.job_uuid == ingestion.uuid
    assert lifecycle.published == [
        (f"{ingestion.uuid}.wav", len(graph.content)),
    ]
    assert (tmp_path / f"{ingestion.uuid}.wav").read_bytes() == graph.content
    assert graph.destination_names == ["Processed"]
    assert graph.move_calls == 1


def test_crash_recovery_is_prioritized_over_inbox_discovery(tmp_path: Path) -> None:
    ingestion_uuid = uuid4()
    ingestion = _snapshot(
        ingestion_uuid=ingestion_uuid,
        stage=MailIngestionStage.ACKNOWLEDGEMENT,
        disposition=MailIngestionDisposition.ACCEPTED,
        attachment_id="attachment-secret",
        audio_extension=AudioExtension.WAV,
        attachment_size_bytes=100,
        job_uuid=ingestion_uuid,
    )
    lifecycle = FakeLifecycle(ingestion=ingestion)
    graph = FakeGraph()
    graph.list_messages_error = AssertionError("Inbox must not be scanned")

    outcome = _worker(tmp_path, lifecycle, graph).run_once()

    assert isinstance(outcome, CompletedOutcome)
    assert graph.list_messages_calls == 0


def test_corrupt_wav_is_failed_without_creating_an_audio_job(tmp_path: Path) -> None:
    lifecycle = FakeLifecycle(ingestion=_snapshot())
    graph = FakeGraph(content=b"not a wav")

    outcome = _worker(tmp_path, lifecycle, graph).run_once()

    assert isinstance(outcome, RejectedOutcome)
    assert outcome.failure.technical_code == "wav_header_invalid"
    assert not lifecycle.published
    assert graph.destination_names == ["Rejected"]
    assert not tuple(tmp_path.iterdir())


def test_empty_download_is_rejected_when_graph_metadata_was_nonempty(
    tmp_path: Path,
) -> None:
    lifecycle = FakeLifecycle(ingestion=_snapshot())
    graph = FakeGraph(content=b"", declared_size_bytes=128)

    outcome = _worker(tmp_path, lifecycle, graph).run_once()

    assert isinstance(outcome, RejectedOutcome)
    assert outcome.failure.technical_code == "audio_empty"
    assert lifecycle.ingestion.audio_extension is AudioExtension.WAV
    assert not lifecycle.published


def test_rearmed_ogg_in_rejected_is_processed_without_scanning_inbox(
    tmp_path: Path,
) -> None:
    ingestion = _snapshot()
    lifecycle = FakeLifecycle(ingestion=ingestion)
    graph = FakeGraph(
        content=_ogg_bytes(),
        attachment_name="message.OGG",
        content_type="application/octet-stream",
        parent_folder_id="rejected-folder",
    )
    graph.list_messages_error = AssertionError("Inbox must not be scanned")

    outcome = _worker(tmp_path, lifecycle, graph).run_once()

    assert isinstance(outcome, CompletedOutcome)
    assert graph.list_messages_calls == 0
    assert lifecycle.published == [(f"{ingestion.uuid}.ogg", len(graph.content))]
    assert (tmp_path / f"{ingestion.uuid}.ogg").read_bytes() == graph.content
    assert graph.destination_names == ["Processed"]


def test_retryable_graph_failure_is_scheduled_and_never_failed(
    tmp_path: Path,
) -> None:
    lifecycle = FakeLifecycle(ingestion=_snapshot())
    graph = FakeGraph()
    graph.list_attachments_error = GraphHttpError(
        operation=GraphOperation.LIST_ATTACHMENTS,
        category=GraphErrorCategory.UNAVAILABLE,
        status_code=503,
        retryable=True,
    )

    outcome = _worker(tmp_path, lifecycle, graph).run_once()

    assert isinstance(outcome, RetryScheduledOutcome)
    assert outcome.next_attempt_at == NOW.replace(second=5)
    assert outcome.failure.scope is MailIngestionErrorScope.SYSTEM
    assert lifecycle.ingestion.status is MailIngestionStatus.RETRY_SCHEDULED
    assert not graph.destination_names


def test_retry_after_cannot_exceed_the_configured_backoff_cap(
    tmp_path: Path,
) -> None:
    lifecycle = FakeLifecycle(ingestion=_snapshot())
    graph = FakeGraph()
    graph.list_attachments_error = GraphHttpError(
        operation=GraphOperation.LIST_ATTACHMENTS,
        category=GraphErrorCategory.THROTTLED,
        status_code=429,
        retryable=True,
        retry_after_seconds=3_600,
    )

    outcome = _worker(tmp_path, lifecycle, graph).run_once()

    assert isinstance(outcome, RetryScheduledOutcome)
    assert outcome.next_attempt_at == NOW + timedelta(seconds=60)


def test_future_system_retry_never_scans_graph_before_its_deadline(
    tmp_path: Path,
) -> None:
    lifecycle = FakeLifecycle(
        ingestion=_snapshot(status=MailIngestionStatus.RETRY_SCHEDULED),
    )
    lifecycle.system_retry_at = NOW + timedelta(seconds=45)
    graph = FakeGraph()
    graph.list_messages_error = AssertionError("Inbox must not be scanned")

    outcome = _worker(tmp_path, lifecycle, graph).run_once()

    assert isinstance(outcome, IdleOutcome)
    assert outcome.wait_seconds == 45
    assert graph.list_messages_calls == 0


def test_download_http_413_rejects_only_the_message(tmp_path: Path) -> None:
    lifecycle = FakeLifecycle(ingestion=_snapshot())
    graph = FakeGraph()
    graph.download_error = GraphHttpError(
        operation=GraphOperation.DOWNLOAD_ATTACHMENT,
        category=GraphErrorCategory.PAYLOAD_TOO_LARGE,
        status_code=413,
        retryable=False,
    )

    outcome = _worker(tmp_path, lifecycle, graph).run_once()

    assert isinstance(outcome, RejectedOutcome)
    assert outcome.failure.technical_code == "audio_too_large"
    assert graph.destination_names == ["Rejected"]
    assert lifecycle.block_failure is None


def test_local_scratch_io_failure_is_retried_and_never_rejected(
    tmp_path: Path,
) -> None:
    lifecycle = FakeLifecycle(ingestion=_snapshot())
    graph = FakeGraph()
    graph.download_error = OSError("synthetic scratch-volume failure")

    outcome = _worker(tmp_path, lifecycle, graph).run_once()

    assert isinstance(outcome, RetryScheduledOutcome)
    assert outcome.failure.scope is MailIngestionErrorScope.SYSTEM
    assert outcome.failure.technical_code == "storage_unavailable"
    assert lifecycle.ingestion.disposition is None
    assert not graph.destination_names


def test_terminal_authentication_failure_blocks_the_system(tmp_path: Path) -> None:
    lifecycle = FakeLifecycle(ingestion=_snapshot())
    graph = FakeGraph()
    graph.list_attachments_error = GraphHttpError(
        operation=GraphOperation.LIST_ATTACHMENTS,
        category=GraphErrorCategory.AUTHENTICATION,
        status_code=401,
        retryable=False,
    )

    outcome = _worker(tmp_path, lifecycle, graph).run_once()

    assert isinstance(outcome, SystemBlockedOutcome)
    assert outcome.automatic_probe is False
    assert lifecycle.ingestion.status is MailIngestionStatus.BLOCKED


def test_existing_audio_from_crash_is_reconciled_without_overwrite(
    tmp_path: Path,
) -> None:
    ingestion = _snapshot(
        stage=MailIngestionStage.PUBLICATION,
        attachment_id="attachment-secret",
        audio_extension=AudioExtension.WAV,
        attachment_size_bytes=len(_wav_bytes()),
    )
    graph = FakeGraph()
    first_lifecycle = FakeLifecycle(
        ingestion=ingestion,
        fail_publication_once=True,
    )

    with pytest.raises(RuntimeError, match="synthetic database crash"):
        _worker(tmp_path, first_lifecycle, graph).run_once()

    path = tmp_path / f"{ingestion.uuid}.wav"
    assert path.read_bytes() == graph.content
    resumed_lifecycle = FakeLifecycle(ingestion=ingestion)

    outcome = _worker(tmp_path, resumed_lifecycle, graph).run_once()

    assert isinstance(outcome, CompletedOutcome)
    assert path.read_bytes() == graph.content
    assert len(tuple(tmp_path.iterdir())) == 1


def test_logs_never_include_graph_identifiers_or_attachment_names(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    lifecycle = FakeLifecycle(ingestion=_snapshot())
    graph = FakeGraph(content=b"corrupt")
    caplog.set_level(
        logging.INFO,
        logger="glpi_ticketing_worker_publisher.publisher.service",
    )

    _worker(tmp_path, lifecycle, graph).run_once()

    assert "immutable-message-secret" not in caplog.text
    assert "attachment-secret" not in caplog.text
    assert "voicemail-sensitive-name" not in caplog.text
