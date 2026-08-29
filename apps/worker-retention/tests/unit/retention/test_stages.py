"""Unit tests for GLPI inspection and crash-safe purge adapters."""

from pathlib import Path

import pytest

from glpi_ticketing_common.audio_purge import (
    AudioPurgeDisposition,
    AudioPurgeErrorCode,
    AudioPurgeFilesystemError,
    AudioPurgeResult,
)
from glpi_ticketing_database import ErrorScope
from glpi_ticketing_glpi import (
    GlpiErrorCategory,
    GlpiHttpError,
    GlpiOperation,
    GlpiTicketNotFoundError,
    GlpiTransportError,
    TicketStatus,
)
from glpi_ticketing_worker_retention.retention.stages import (
    CrashSafeAudioPurger,
    GlpiCheckFailure,
    GlpiCheckFailureDisposition,
    GlpiCheckStage,
    GlpiCheckSuccess,
)

from ._fakes import (
    JOB_UUID,
    STORAGE_KEY,
    TICKET_ID,
    FakeTicketReader,
    glpi_work,
    ticket_snapshot,
)


@pytest.mark.parametrize(
    "status",
    [
        TicketStatus.INCOMING,
        TicketStatus.ASSIGNED,
        TicketStatus.SOLVED,
        TicketStatus.CLOSED,
    ],
)
def test_glpi_stage_reads_by_ticket_id_and_keeps_a_correlated_snapshot(
    status: TicketStatus,
) -> None:
    reader = FakeTicketReader(ticket_snapshot(status=status))

    result = GlpiCheckStage(reader).execute(glpi_work())

    assert isinstance(result, GlpiCheckSuccess)
    assert result.snapshot.status is status
    assert reader.ticket_ids == [TICKET_ID]


def test_glpi_stage_blocks_a_missing_ticket_without_retry() -> None:
    result = GlpiCheckStage(FakeTicketReader(GlpiTicketNotFoundError())).execute(
        glpi_work()
    )

    assert isinstance(result, GlpiCheckFailure)
    assert result.disposition is GlpiCheckFailureDisposition.JOB_BLOCKED
    assert result.failure.scope is ErrorScope.JOB
    assert result.failure.category == "not_found"
    assert result.failure.technical_code == "ticket_not_found"


def test_glpi_stage_blocks_an_external_id_mismatch() -> None:
    result = GlpiCheckStage(
        FakeTicketReader(
            ticket_snapshot(external_id="323e4567-e89b-42d3-a456-426614174000")
        )
    ).execute(glpi_work())

    assert isinstance(result, GlpiCheckFailure)
    assert result.disposition is GlpiCheckFailureDisposition.JOB_BLOCKED
    assert result.failure.scope is ErrorScope.JOB
    assert result.failure.category == "conflict"
    assert result.failure.technical_code == "external_id_mismatch"


def test_glpi_stage_classifies_transport_failure_as_retryable_system_failure() -> None:
    result = GlpiCheckStage(
        FakeTicketReader(GlpiTransportError(GlpiOperation.GET_TICKET))
    ).execute(glpi_work())

    assert isinstance(result, GlpiCheckFailure)
    assert result.disposition is GlpiCheckFailureDisposition.RETRYABLE
    assert result.failure.scope is ErrorScope.SYSTEM
    assert result.failure.category == "unavailable"
    assert result.failure.technical_code == "transport_error"


def test_glpi_stage_classifies_authentication_failure_as_system_blocked() -> None:
    result = GlpiCheckStage(
        FakeTicketReader(
            GlpiHttpError(
                operation=GlpiOperation.GET_TICKET,
                category=GlpiErrorCategory.AUTHENTICATION,
                status_code=401,
                retryable=False,
            )
        )
    ).execute(glpi_work())

    assert isinstance(result, GlpiCheckFailure)
    assert result.disposition is GlpiCheckFailureDisposition.SYSTEM_BLOCKED
    assert result.failure.scope is ErrorScope.SYSTEM
    assert result.failure.category == "authentication"
    assert result.failure.technical_code == "http_401"


@pytest.mark.parametrize(
    "response",
    [
        ticket_snapshot(),
        GlpiTransportError(GlpiOperation.GET_TICKET),
    ],
)
def test_glpi_stage_fences_the_remote_call_with_ownership_checks(
    response: object,
) -> None:
    ownership_checks: list[str] = []
    stage = GlpiCheckStage(
        FakeTicketReader(response),  # type: ignore[arg-type]
        ownership_guard=lambda: ownership_checks.append("checked"),
    )

    stage.execute(glpi_work())

    assert ownership_checks == ["checked", "checked"]


def test_audio_stage_binds_the_root_and_fences_the_filesystem_call(
    tmp_path: Path,
) -> None:
    calls: list[tuple[Path, str] | str] = []
    expected = AudioPurgeResult(
        storage_key=STORAGE_KEY,
        disposition=AudioPurgeDisposition.PURGED,
    )

    def purge(audio_root: Path, storage_key: str) -> AudioPurgeResult:
        calls.append((audio_root, storage_key))
        return expected

    adapter = CrashSafeAudioPurger(
        tmp_path,
        purge=purge,
        ownership_guard=lambda: calls.append("guard"),
    )

    result = adapter.purge(STORAGE_KEY)

    assert result is expected
    assert calls == ["guard", (tmp_path, STORAGE_KEY), "guard"]


def test_audio_stage_runs_the_final_ownership_check_after_a_failure(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    expected = AudioPurgeFilesystemError(
        code=AudioPurgeErrorCode.UNLINK_FAILED,
        retryable=True,
        errno=5,
    )

    def fail_purge(_audio_root: Path, _storage_key: str) -> AudioPurgeResult:
        calls.append("purge")
        raise expected

    adapter = CrashSafeAudioPurger(
        tmp_path,
        purge=fail_purge,
        ownership_guard=lambda: calls.append("guard"),
    )

    with pytest.raises(AudioPurgeFilesystemError) as captured:
        adapter.purge(STORAGE_KEY)

    assert captured.value is expected
    assert calls == ["guard", "purge", "guard"]
    assert str(tmp_path) not in repr(captured.value)
    assert str(JOB_UUID) not in repr(captured.value)
