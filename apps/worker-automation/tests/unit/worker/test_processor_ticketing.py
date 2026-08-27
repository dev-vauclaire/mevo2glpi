import json
import logging
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

import glpi_ticketing_worker_automation.worker.processor as processor_module
from glpi_ticketing_worker_automation.glpi import (
    GlpiClientProtocol,
    GlpiCreateOutcomeUnknownError,
    GlpiDuplicateExternalIdError,
    GlpiErrorCategory,
    GlpiHttpError,
    GlpiOperation,
    GlpiProtocolError,
    GlpiTicketInput,
    GlpiTicketRejectedError,
    GlpiTransportError,
    TicketId,
)
from glpi_ticketing_worker_automation.transcription import (
    TranscriberProtocol,
    TranscriptionResult,
)
from glpi_ticketing_worker_automation.worker import (
    AudioProcessor,
    BlockedOutcome,
    CompletedOutcome,
    FailedOutcome,
    ProcessingStage,
    RetryOutcome,
    WorkerDirectories,
)

AUDIO_UUID = UUID("123e4567-e89b-42d3-a456-426614174000")
AUDIO_FILENAME = f"{AUDIO_UUID}.m4a"
SYNTHETIC_AUDIO = b"synthetic-audio"
SYNTHETIC_TRANSCRIPTION = "Transcription sensible réservée au ticket"
SYNTHETIC_SECRET = "synthetic-client-secret"
TICKET_ID = TicketId(42)
FIXED_NOW = datetime(2026, 8, 27, 10, 15, 30, tzinfo=UTC)


class SingleResultTranscriber:
    def __init__(self, transcription: str = SYNTHETIC_TRANSCRIPTION) -> None:
        self.transcription = transcription
        self.audio_paths: list[Path] = []

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        self.audio_paths.append(audio_path)
        return TranscriptionResult(
            audio_path=audio_path,
            audio_uuid=AUDIO_UUID,
            transcription=self.transcription,
        )


class ScriptedGlpiClient:
    def __init__(self, events: list[TicketId | BaseException]) -> None:
        self._events = deque(events)
        self.tickets: list[GlpiTicketInput] = []

    def ensure_ticket(self, ticket: GlpiTicketInput) -> TicketId:
        self.tickets.append(ticket)
        if not self._events:
            raise AssertionError("unexpected GLPI call")
        event = self._events.popleft()
        if isinstance(event, BaseException):
            raise event
        return event


class ReconcilingGlpiClient:
    """Keep one logical ticket per audio across simulated worker restarts."""

    def __init__(
        self,
        *,
        first_error: BaseException | None = None,
        ticket_created_before_error: bool = False,
    ) -> None:
        self._first_error = first_error
        self._ticket_created_before_error = ticket_created_before_error
        self.requests: list[GlpiTicketInput] = []
        self.logical_tickets: dict[UUID, TicketId] = {}

    def ensure_ticket(self, ticket: GlpiTicketInput) -> TicketId:
        self.requests.append(ticket)
        if self._first_error is not None:
            error = self._first_error
            self._first_error = None
            if self._ticket_created_before_error:
                self.logical_tickets.setdefault(ticket.audio_uuid, TICKET_ID)
            raise error

        return self.logical_tickets.setdefault(ticket.audio_uuid, TICKET_ID)


@pytest.fixture
def audio_directories(tmp_path: Path) -> WorkerDirectories:
    directories = WorkerDirectories(
        pending=tmp_path / "pending",
        processing=tmp_path / "processing",
        completed=tmp_path / "completed",
        failed=tmp_path / "failed",
    )
    for directory in (
        directories.pending,
        directories.processing,
        directories.completed,
        directories.failed,
    ):
        directory.mkdir()
    return directories


def test_success_returns_completed_outcome_and_public_audio_url(
    audio_directories: WorkerDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    transcriber = SingleResultTranscriber()
    glpi_client = ScriptedGlpiClient([TICKET_ID])
    processor = _processor(audio_directories, transcriber, glpi_client)

    outcome = processor.run_once()

    assert isinstance(outcome, CompletedOutcome)
    assert outcome.audio_uuid == AUDIO_UUID
    assert outcome.ticket_id == TICKET_ID
    assert outcome.completed_path == audio_directories.completed / AUDIO_FILENAME
    assert outcome.completed_path.read_bytes() == SYNTHETIC_AUDIO
    assert not audio_path.exists()
    assert transcriber.audio_paths == [audio_path]
    assert len(glpi_client.tickets) == 1
    ticket = glpi_client.tickets[0]
    assert ticket.audio_uuid == AUDIO_UUID
    assert ticket.transcription == SYNTHETIC_TRANSCRIPTION
    assert ticket.audio_url == f"https://web.example.test/base/audios/{AUDIO_UUID}"


def test_ticket_is_ensured_before_audio_is_published(
    audio_directories: WorkerDirectories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_processing_audio(audio_directories)
    glpi_client = ScriptedGlpiClient([TICKET_ID])
    real_move_audio = processor_module.audio_files.move_audio

    def assert_ticket_before_move(source: Path, destination: Path, **kwargs) -> Path:
        assert len(glpi_client.tickets) == 1
        return real_move_audio(source, destination, **kwargs)

    monkeypatch.setattr(
        processor_module.audio_files,
        "move_audio",
        assert_ticket_before_move,
    )
    processor = _processor(
        audio_directories,
        SingleResultTranscriber(),
        glpi_client,
    )

    assert isinstance(processor.run_once(), CompletedOutcome)


def test_glpi_retry_reuses_transcription_in_memory(
    audio_directories: WorkerDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    transcriber = SingleResultTranscriber()
    glpi_client = ScriptedGlpiClient(
        [
            GlpiTransportError(GlpiOperation.FIND_TICKET),
            TICKET_ID,
        ]
    )
    processor = _processor(audio_directories, transcriber, glpi_client)

    first_outcome = processor.run_once()

    assert isinstance(first_outcome, RetryOutcome)
    assert first_outcome.stage is ProcessingStage.TICKETING
    assert first_outcome.processing_path == audio_path
    assert first_outcome.retry_after_seconds == 0.5
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert not list(audio_directories.failed.iterdir())

    second_outcome = processor.run_once()

    assert not audio_path.exists()
    assert isinstance(second_outcome, CompletedOutcome)
    assert transcriber.audio_paths == [audio_path]
    assert len(glpi_client.tickets) == 2
    assert all(
        ticket.transcription == SYNTHETIC_TRANSCRIPTION
        for ticket in glpi_client.tickets
    )


def test_unknown_creation_outcome_retries_without_moving_to_failed(
    audio_directories: WorkerDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    glpi_client = ScriptedGlpiClient(
        [
            GlpiCreateOutcomeUnknownError(
                category=GlpiErrorCategory.UNAVAILABLE,
                technical_code="transport_error",
            ),
            TICKET_ID,
        ]
    )
    processor = _processor(
        audio_directories,
        SingleResultTranscriber(),
        glpi_client,
    )

    first_outcome = processor.run_once()

    assert isinstance(first_outcome, RetryOutcome)
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert not list(audio_directories.failed.iterdir())
    assert isinstance(processor.run_once(), CompletedOutcome)


def test_explicit_ticket_rejection_returns_failed_with_sanitized_sidecar(
    audio_directories: WorkerDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    terminal_error = GlpiTicketRejectedError()
    processor = _processor(
        audio_directories,
        SingleResultTranscriber(),
        ScriptedGlpiClient([terminal_error]),
    )

    outcome = processor.run_once()

    assert isinstance(outcome, FailedOutcome)
    assert outcome.stage is ProcessingStage.TICKETING
    assert not audio_path.exists()
    serialized_sidecar = outcome.error_sidecar_path.read_text(encoding="utf-8")
    assert json.loads(serialized_sidecar) == {
        "uuid": str(AUDIO_UUID),
        "step": "ticketing",
        "category": "invalid_request",
        "code": "ticket_input_rejected",
        "timestamp": "2026-08-27T10:15:30Z",
    }
    assert SYNTHETIC_TRANSCRIPTION not in serialized_sidecar
    assert SYNTHETIC_SECRET not in serialized_sidecar


@pytest.mark.parametrize(
    ("operation", "status_code"),
    [
        (GlpiOperation.FIND_TICKET, 400),
        (GlpiOperation.CREATE_TICKET, 404),
        (GlpiOperation.CREATE_TICKET, 405),
    ],
)
def test_contract_or_route_http_errors_block_and_keep_processing(
    audio_directories: WorkerDirectories,
    operation: GlpiOperation,
    status_code: int,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    processor = _processor(
        audio_directories,
        SingleResultTranscriber(),
        ScriptedGlpiClient(
            [
                GlpiHttpError(
                    operation=operation,
                    category=GlpiErrorCategory.INVALID_REQUEST,
                    status_code=status_code,
                    retryable=False,
                )
            ]
        ),
    )

    outcome = processor.run_once()

    assert isinstance(outcome, BlockedOutcome)
    assert outcome.stage is ProcessingStage.TICKETING
    assert outcome.failure.technical_code == f"http_{status_code}"
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert not list(audio_directories.failed.iterdir())


@pytest.mark.parametrize(
    ("status_code", "category"),
    [
        (400, GlpiErrorCategory.INVALID_REQUEST),
        (409, GlpiErrorCategory.CONFLICT),
        (422, GlpiErrorCategory.INVALID_REQUEST),
    ],
)
def test_generic_create_http_errors_block_and_keep_processing(
    audio_directories: WorkerDirectories,
    status_code: int,
    category: GlpiErrorCategory,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    processor = _processor(
        audio_directories,
        SingleResultTranscriber(),
        ScriptedGlpiClient(
            [
                GlpiHttpError(
                    operation=GlpiOperation.CREATE_TICKET,
                    category=category,
                    status_code=status_code,
                    retryable=False,
                )
            ]
        ),
    )

    outcome = processor.run_once()

    assert isinstance(outcome, BlockedOutcome)
    assert outcome.stage is ProcessingStage.TICKETING
    assert outcome.failure.category == category.value
    assert outcome.failure.technical_code == f"http_{status_code}"
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert not list(audio_directories.failed.iterdir())


def test_duplicate_external_id_moves_audio_to_failed(
    audio_directories: WorkerDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    processor = _processor(
        audio_directories,
        SingleResultTranscriber(),
        ScriptedGlpiClient([GlpiDuplicateExternalIdError()]),
    )

    outcome = processor.run_once()

    assert isinstance(outcome, FailedOutcome)
    assert outcome.stage is ProcessingStage.TICKETING
    assert outcome.failure.category == GlpiErrorCategory.CONFLICT.value
    assert outcome.failure.technical_code == "duplicate_external_id"
    assert not audio_path.exists()
    assert outcome.failed_path.read_bytes() == SYNTHETIC_AUDIO


@pytest.mark.parametrize(
    ("status_code", "category"),
    [
        (401, GlpiErrorCategory.AUTHENTICATION),
        (403, GlpiErrorCategory.AUTHORIZATION),
    ],
)
def test_systemic_glpi_error_blocks_and_keeps_processing(
    audio_directories: WorkerDirectories,
    status_code: int,
    category: GlpiErrorCategory,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    processor = _processor(
        audio_directories,
        SingleResultTranscriber(),
        ScriptedGlpiClient(
            [
                GlpiHttpError(
                    operation=GlpiOperation.AUTHENTICATE,
                    category=category,
                    status_code=status_code,
                    retryable=False,
                )
            ]
        ),
    )

    outcome = processor.run_once()

    assert isinstance(outcome, BlockedOutcome)
    assert outcome.stage is ProcessingStage.TICKETING
    assert outcome.failure.category == category.value
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert not list(audio_directories.failed.iterdir())


def test_glpi_protocol_error_blocks_and_keeps_processing(
    audio_directories: WorkerDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    processor = _processor(
        audio_directories,
        SingleResultTranscriber(),
        ScriptedGlpiClient(
            [GlpiProtocolError(GlpiOperation.FIND_TICKET, "expected_json_array")]
        ),
    )

    outcome = processor.run_once()

    assert isinstance(outcome, BlockedOutcome)
    assert outcome.stage is ProcessingStage.TICKETING
    assert outcome.failure.category == GlpiErrorCategory.PROTOCOL.value
    assert outcome.failure.technical_code == "expected_json_array"
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert not list(audio_directories.failed.iterdir())


def test_invalid_audio_url_blocks_without_contacting_glpi(
    audio_directories: WorkerDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    transcriber = SingleResultTranscriber()
    glpi_client = ScriptedGlpiClient([TICKET_ID])
    processor = AudioProcessor(
        directories=audio_directories,
        transcriber=transcriber,
        glpi_client=glpi_client,
        audio_url_for=lambda _audio_uuid: "not-an-http-url",
        now=lambda: FIXED_NOW,
    )

    outcome = processor.run_once()

    assert isinstance(outcome, BlockedOutcome)
    assert outcome.failure.category == "configuration"
    assert outcome.failure.technical_code == "invalid_audio_url"
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert glpi_client.tickets == []


def test_invalid_ticket_id_blocks_before_completed_transition(
    audio_directories: WorkerDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    glpi_client = ScriptedGlpiClient([0])  # type: ignore[list-item]
    processor = _processor(
        audio_directories,
        SingleResultTranscriber(),
        glpi_client,
    )

    outcome = processor.run_once()

    assert isinstance(outcome, BlockedOutcome)
    assert outcome.failure.category == "protocol"
    assert outcome.failure.technical_code == "invalid_ticket_id"
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert not list(audio_directories.completed.iterdir())


def test_completed_move_failure_can_resume_manually_on_same_processor_without_external_calls(
    audio_directories: WorkerDirectories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    transcriber = SingleResultTranscriber()
    glpi_client = ScriptedGlpiClient([TICKET_ID])
    real_move_audio = processor_module.audio_files.move_audio
    move_calls = 0

    def fail_once(source: Path, destination: Path, **kwargs) -> Path:
        nonlocal move_calls
        move_calls += 1
        if move_calls == 1:
            raise OSError("synthetic completed volume failure")
        return real_move_audio(source, destination, **kwargs)

    monkeypatch.setattr(processor_module.audio_files, "move_audio", fail_once)
    processor = _processor(audio_directories, transcriber, glpi_client)

    first_outcome = processor.run_once()

    assert isinstance(first_outcome, BlockedOutcome)
    assert first_outcome.stage is ProcessingStage.FINALIZATION
    assert first_outcome.ticket_id == TICKET_ID
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO

    second_outcome = processor.run_once()

    assert not audio_path.exists()
    assert isinstance(second_outcome, CompletedOutcome)
    assert transcriber.audio_paths == [audio_path]
    assert len(glpi_client.tickets) == 1
    assert move_calls == 2


@pytest.mark.parametrize(
    ("first_error", "ticket_created_before_error"),
    [
        (
            GlpiTransportError(GlpiOperation.FIND_TICKET),
            False,
        ),
        (
            GlpiCreateOutcomeUnknownError(
                category=GlpiErrorCategory.UNAVAILABLE,
                technical_code="transport_error",
            ),
            True,
        ),
    ],
    ids=("find_retry", "create_outcome_unknown"),
)
def test_restart_after_glpi_retry_retranscribes_and_reconciles_one_logical_ticket(
    audio_directories: WorkerDirectories,
    first_error: BaseException,
    ticket_created_before_error: bool,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    glpi_client = ReconcilingGlpiClient(
        first_error=first_error,
        ticket_created_before_error=ticket_created_before_error,
    )
    first_transcriber = SingleResultTranscriber()
    first_processor = _processor(
        audio_directories,
        first_transcriber,
        glpi_client,
    )

    first_outcome = first_processor.run_once()

    assert isinstance(first_outcome, RetryOutcome)
    assert first_outcome.stage is ProcessingStage.TICKETING
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO

    restarted_transcriber = SingleResultTranscriber()
    restarted_processor = _processor(
        audio_directories,
        restarted_transcriber,
        glpi_client,
    )
    restarted_outcome = restarted_processor.run_once()

    assert isinstance(restarted_outcome, CompletedOutcome)
    assert first_transcriber.audio_paths == [audio_path]
    assert restarted_transcriber.audio_paths == [audio_path]
    assert len(glpi_client.requests) == 2
    assert {request.audio_uuid for request in glpi_client.requests} == {AUDIO_UUID}
    assert glpi_client.logical_tickets == {AUDIO_UUID: TICKET_ID}
    assert restarted_outcome.ticket_id == TICKET_ID


def test_restart_after_finalization_failure_retranscribes_reconciles_and_completes(
    audio_directories: WorkerDirectories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    glpi_client = ReconcilingGlpiClient()
    first_transcriber = SingleResultTranscriber()
    real_move_audio = processor_module.audio_files.move_audio
    move_calls = 0

    def fail_first_finalization(
        source: Path,
        destination: Path,
        **kwargs,
    ) -> Path:
        nonlocal move_calls
        move_calls += 1
        if move_calls == 1:
            raise OSError("synthetic completed volume failure")
        return real_move_audio(source, destination, **kwargs)

    monkeypatch.setattr(
        processor_module.audio_files,
        "move_audio",
        fail_first_finalization,
    )
    first_processor = _processor(
        audio_directories,
        first_transcriber,
        glpi_client,
    )

    first_outcome = first_processor.run_once()

    assert isinstance(first_outcome, BlockedOutcome)
    assert first_outcome.stage is ProcessingStage.FINALIZATION
    assert first_outcome.ticket_id == TICKET_ID
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert glpi_client.logical_tickets == {AUDIO_UUID: TICKET_ID}

    restarted_transcriber = SingleResultTranscriber()
    restarted_processor = _processor(
        audio_directories,
        restarted_transcriber,
        glpi_client,
    )
    restarted_outcome = restarted_processor.run_once()

    assert isinstance(restarted_outcome, CompletedOutcome)
    assert first_transcriber.audio_paths == [audio_path]
    assert restarted_transcriber.audio_paths == [audio_path]
    assert len(glpi_client.requests) == 2
    assert glpi_client.logical_tickets == {AUDIO_UUID: TICKET_ID}
    assert move_calls == 2


def test_existing_processing_and_completed_audio_block_finalization_and_remain(
    audio_directories: WorkerDirectories,
) -> None:
    processing_audio = _write_processing_audio(audio_directories)
    completed_audio = audio_directories.completed / AUDIO_FILENAME
    completed_audio.write_bytes(SYNTHETIC_AUDIO)
    processor = _processor(
        audio_directories,
        SingleResultTranscriber(),
        ScriptedGlpiClient([TICKET_ID]),
    )

    outcome = processor.run_once()

    assert isinstance(outcome, BlockedOutcome)
    assert outcome.stage is ProcessingStage.FINALIZATION
    assert outcome.failure.technical_code == "completed_publish_failed"
    assert outcome.ticket_id == TICKET_ID
    assert processing_audio.read_bytes() == SYNTHETIC_AUDIO
    assert completed_audio.read_bytes() == SYNTHETIC_AUDIO


def test_oserror_after_source_unlink_reconciles_published_completed_audio(
    audio_directories: WorkerDirectories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processing_audio = _write_processing_audio(audio_directories)
    completed_audio = audio_directories.completed / AUDIO_FILENAME
    real_unlink_source = processor_module.audio_files._unlink_source

    def unlink_then_raise(source: Path) -> None:
        real_unlink_source(source)
        raise OSError("synthetic error after source unlink")

    monkeypatch.setattr(
        processor_module.audio_files,
        "_unlink_source",
        unlink_then_raise,
    )
    processor = _processor(
        audio_directories,
        SingleResultTranscriber(),
        ScriptedGlpiClient([TICKET_ID]),
    )

    outcome = processor.run_once()

    assert isinstance(outcome, CompletedOutcome)
    assert outcome.ticket_id == TICKET_ID
    assert outcome.completed_path == completed_audio
    assert completed_audio.read_bytes() == SYNTHETIC_AUDIO
    assert not processing_audio.exists()


def test_ticketing_logs_identifiers_but_not_sensitive_content(
    audio_directories: WorkerDirectories,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_processing_audio(audio_directories)
    processor = _processor(
        audio_directories,
        SingleResultTranscriber(),
        ScriptedGlpiClient([TICKET_ID]),
    )

    with caplog.at_level(
        logging.INFO,
        logger="glpi_ticketing_worker_automation.worker.processor",
    ):
        outcome = processor.run_once()

    assert isinstance(outcome, CompletedOutcome)
    assert str(AUDIO_UUID) in caplog.text
    assert str(TICKET_ID) in caplog.text
    assert SYNTHETIC_TRANSCRIPTION not in caplog.text
    assert SYNTHETIC_SECRET not in caplog.text


def _processor(
    directories: WorkerDirectories,
    transcriber: TranscriberProtocol,
    glpi_client: GlpiClientProtocol,
) -> AudioProcessor:
    return AudioProcessor(
        directories=directories,
        transcriber=transcriber,
        glpi_client=glpi_client,
        audio_url_for=lambda audio_uuid: (
            f"https://web.example.test/base/audios/{audio_uuid}"
        ),
        idle_wait_seconds=0.25,
        transcription_retry_interval_seconds=0.25,
        glpi_retry_interval_seconds=0.5,
        now=lambda: FIXED_NOW,
    )


def _write_processing_audio(directories: WorkerDirectories) -> Path:
    audio_path = directories.processing / AUDIO_FILENAME
    audio_path.write_bytes(SYNTHETIC_AUDIO)
    return audio_path
